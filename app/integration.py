"""Integration API (v1) for Bigship's own backend.

Server-to-server only: authenticate with `X-API-Key: <INTEGRATION_API_KEY>`. The customer is named in
the URL (`/v1/customers/{customer_id}/...`) using YOUR customer id, so no per-customer tokens are needed.
Never call this from a browser or mobile app: the key gives access to every customer.

Errors are JSON `{"detail": "<human readable message>"}` with a meaningful HTTP status:
400 invalid input · 401 bad/missing key · 403 KYC not approved · 404 not found ·
409 conflict (already exists / limit reached / wrong state) · 502 Plivo failure · 503 API disabled.
"""
from __future__ import annotations

import hmac
import re

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import CurrentClient
from app.config import Settings, get_settings
from app.db import get_db
from app.models import ClientAccount, ComplianceApplication, PhoneNumber, WebhookEvent
from app.routers import _read_kyc_form
from app.serializers import app_out, number_out
from app.services import compliance as kyc
from app.services import events
from app.services import numbers as nums


def require_integration_key(x_api_key: str | None = Header(default=None, description="INTEGRATION_API_KEY"),
                            s: Settings = Depends(get_settings)) -> None:
    if not s.integration_api_key:
        raise HTTPException(503, "Integration API is disabled: set INTEGRATION_API_KEY")
    if not x_api_key or not hmac.compare_digest(x_api_key, s.integration_api_key):
        raise HTTPException(401, "Invalid or missing X-API-Key")


v1 = APIRouter(prefix="/v1", tags=["Integration v1"], dependencies=[Depends(require_integration_key)])

CustomerId = Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:@-]+$",
                  description="Your own customer id (letters, digits, _ . : @ -)")


def _customer(db: Session, customer_id: str, fallback_name: str | None = None) -> CurrentClient:
    acct = db.get(ClientAccount, customer_id)
    return CurrentClient(customer_id, (acct.name if acct else None) or fallback_name or customer_id)


def _number_of(db: Session, customer_id: str, number_id: int) -> PhoneNumber:
    row = db.get(PhoneNumber, number_id)
    if not row or row.client_id != customer_id or row.status in ("pending", "failed"):
        raise HTTPException(404, "Number not found")
    return row


# ------------------------------------------------------------------------------------- config
@v1.get("/config", summary="Service configuration")
def config(s: Settings = Depends(get_settings)):
    """What your UI needs to render the KYC and number-search screens."""
    return {"country_iso": s.country_iso, "number_types": s.number_types, "cities": s.cities,
            "kyc_documents_mode": s.kyc_documents_mode, "max_numbers_per_customer": s.max_numbers_per_client,
            "currency": "USD", "mock": s.plivo_mock}


@v1.get("/kyc/requirements", summary="KYC documents and fields Plivo requires")
def requirements(number_type: str = "local", s: Settings = Depends(get_settings)):
    """Live from Plivo (cached 1h). Render one upload per `document_types[]` (all of them when
    `kyc_documents_mode` is `all`, exactly one when `one`) plus its `required_fields`."""
    return {**kyc.get_requirements(number_type, "business"), "kyc_documents_mode": s.kyc_documents_mode}


# ------------------------------------------------------------------------------------- customers
class CustomerIn(BaseModel):
    name: str = Field(min_length=1, max_length=128, description="Display / company name")


def _summary(db: Session, customer_id: str) -> dict:
    s = get_settings()
    acct = db.get(ClientAccount, customer_id)
    apps = db.scalars(select(ComplianceApplication).where(ComplianceApplication.client_id == customer_id)
                      .order_by(ComplianceApplication.id.desc())).all()
    kyc_by_type = {}
    for t in s.number_types:
        latest = next((a for a in apps if a.number_type == t), None)
        kyc_by_type[t] = None if latest is None else {
            "application_id": latest.id, "status": latest.status, "rejection_reason": latest.rejection_reason,
            "updated_at": latest.updated_at}
    held = db.scalar(select(func.count()).select_from(PhoneNumber).where(
        PhoneNumber.client_id == customer_id, PhoneNumber.status.in_(nums.LIVE_STATUSES)))
    can_buy = {t: bool(v and v["status"] == "accepted") and held < s.max_numbers_per_client
               for t, v in kyc_by_type.items()}
    return {"customer_id": customer_id, "name": acct.name if acct else None, "exists": acct is not None,
            "plivo_subaccount_id": acct.plivo_subaccount_id if acct else None,
            "kyc": kyc_by_type, "can_buy": can_buy,
            "numbers": {"held": held, "limit": s.max_numbers_per_client}}


@v1.put("/customers/{customer_id}", summary="Create or update a customer")
def upsert_customer(body: CustomerIn, customer_id: str = CustomerId, db: Session = Depends(get_db)):
    """Optional: registers the name used for the customer's Plivo subaccount. If you skip it, the
    business name from the first KYC submission is used."""
    acct = db.get(ClientAccount, customer_id)
    if acct:
        acct.name = body.name.strip()
    else:
        db.add(ClientAccount(client_id=customer_id, name=body.name.strip()))
    db.commit()
    return _summary(db, customer_id)


@v1.get("/customers/{customer_id}", summary="Customer status: KYC, can-buy, number count")
def get_customer(customer_id: str = CustomerId, db: Session = Depends(get_db)):
    """One call to decide what to show: KYC status per number type, whether buying is allowed now,
    and how many numbers are held. Refreshes a pending KYC from Plivo (throttled)."""
    for a in db.scalars(select(ComplianceApplication).where(ComplianceApplication.client_id == customer_id,
                                                            ComplianceApplication.status == "submitted")).all():
        kyc.sync_application(db, a)
    return _summary(db, customer_id)


# ------------------------------------------------------------------------------------- KYC
@v1.post("/customers/{customer_id}/kyc", status_code=201, summary="Submit KYC")
async def submit_kyc(request: Request, customer_id: str = CustomerId, db: Session = Depends(get_db)):
    """`multipart/form-data`:
    - `payload`: JSON string `{"number_type": "local", "end_user": {...}, "documents": [{"document_type_id": "...", "data_fields": {...}}]}`
    - one file per document: field name `file_<document_type_id>` (PDF/JPEG/PNG, max 5 MB)
    - only one document (e.g. just a GST certificate)? Plivo accepts the same file in every slot: either
      attach it under each field name, or attach it once and set `"same_file_for_all": true` in `payload`.

    Returns the application with status `submitted`. The outcome arrives as a `kyc.accepted` /
    `kyc.rejected` webhook (or poll `GET /v1/customers/{id}`)."""
    payload, files = await _read_kyc_form(request)
    end_user = payload.get("end_user") or {}
    client = _customer(db, customer_id, fallback_name=end_user.get("name"))
    a = await run_in_threadpool(kyc.create_application, db, client, payload.get("number_type") or "local",
                                payload.get("user_type") or "business", end_user,
                                payload.get("documents") or [], files, bool(payload.get("same_file_for_all")))
    return app_out(a)


@v1.get("/customers/{customer_id}/kyc", summary="List KYC applications")
def list_kyc(customer_id: str = CustomerId, db: Session = Depends(get_db)):
    apps = db.scalars(select(ComplianceApplication).where(ComplianceApplication.client_id == customer_id)
                      .order_by(ComplianceApplication.id.desc())).all()
    for a in apps:
        if a.status == "submitted":
            kyc.sync_application(db, a)
    return [app_out(a) for a in apps]


@v1.get("/customers/{customer_id}/kyc/{application_id}", summary="Get one KYC application")
def get_kyc(application_id: int, customer_id: str = CustomerId, db: Session = Depends(get_db)):
    a = kyc.get_client_application(db, _customer(db, customer_id), application_id)
    return app_out(kyc.sync_application(db, a))


@v1.post("/customers/{customer_id}/kyc/{application_id}/resubmit", summary="Correct a rejected KYC")
async def resubmit_kyc(application_id: int, request: Request, customer_id: str = CustomerId,
                       db: Session = Depends(get_db)):
    """Same multipart format as submit. Only for status `rejected`. Plivo replaces ALL documents, so
    upload every document again, not just the one that was wrong."""
    a = kyc.get_client_application(db, _customer(db, customer_id), application_id)
    payload, files = await _read_kyc_form(request)
    a = await run_in_threadpool(kyc.resubmit_application, db, a, payload.get("end_user") or {},
                                payload.get("documents") or [], files, bool(payload.get("same_file_for_all")))
    return app_out(a)


# ------------------------------------------------------------------------------------- numbers
@v1.get("/customers/{customer_id}/numbers/available", summary="Search numbers the customer can buy")
def available_numbers(customer_id: str = CustomerId, number_type: str = "local",
                      city: str | None = Query(None, description="One of /v1/config cities; empty = any"),
                      starts_with: str | None = Query(None, pattern=r"^\d{1,10}$",
                                                      description="Digits right after +91, e.g. 80 or 8065"),
                      offset: int = Query(0, ge=0), db: Session = Depends(get_db)):
    """Requires accepted KYC (403 otherwise). Results stay buyable by this customer for 15 minutes."""
    return nums.search(db, _customer(db, customer_id), number_type, starts_with, city, None, offset)


class BuyIn(BaseModel):
    number: str = Field(min_length=6, max_length=20, description="From /numbers/available, with or without +")


@v1.post("/customers/{customer_id}/numbers", status_code=201, summary="Buy a number")
def buy_number(body: BuyIn, response: Response, customer_id: str = CustomerId,
               idempotency_key: str | None = Header(default=None, max_length=128,
                                                    description="Recommended. Retries with the same key never buy twice."),
               db: Session = Depends(get_db)):
    """Charge your customer BEFORE calling this. Returns the number with `status`:
    `active` (ready for calls; webhook `number.purchased`) or `pending_activation` (Plivo accepted the
    order but has not activated it yet; webhook `number.activation_pending`; not given to the dialer).
    On failure: 4xx/5xx, nothing is rented, so refund the customer.

    Replaying an `Idempotency-Key` returns the original result with HTTP 200."""
    client = _customer(db, customer_id)
    existing = idempotency_key and db.scalars(select(PhoneNumber).where(
        PhoneNumber.client_id == customer_id, PhoneNumber.idempotency_key == idempotency_key)).first()
    row = nums.buy(db, client, body.number, idempotency_key=idempotency_key)
    if existing:
        response.status_code = 200
        if row.status == "failed":
            raise HTTPException(409, f"An earlier request with this Idempotency-Key failed: {row.failure_reason}")
    return number_out(row)


@v1.get("/customers/{customer_id}/numbers", summary="List the customer's numbers")
def list_numbers(customer_id: str = CustomerId, include_released: bool = False, db: Session = Depends(get_db)):
    return [number_out(n) for n in nums.list_numbers(db, customer_id, include_released)]


@v1.get("/customers/{customer_id}/numbers/{number_id}", summary="Get one number")
def get_number(number_id: int, customer_id: str = CustomerId, db: Session = Depends(get_db)):
    return number_out(_number_of(db, customer_id, number_id))


class NumberPatch(BaseModel):
    alias: str | None = Field(default=None, max_length=99, description="Label shown to the customer")


@v1.patch("/customers/{customer_id}/numbers/{number_id}", summary="Label a number")
def label_number(number_id: int, body: NumberPatch, customer_id: str = CustomerId, db: Session = Depends(get_db)):
    row = _number_of(db, customer_id, number_id)
    if row.status == "released":
        raise HTTPException(409, "Number is released")
    row.alias = (body.alias or "").strip() or None
    db.commit()
    return number_out(row)


@v1.delete("/customers/{customer_id}/numbers/{number_id}", summary="Release a number (irreversible)")
def release_number(number_id: int, customer_id: str = CustomerId, db: Session = Depends(get_db)):
    """Returns the number to Plivo. Rental stops; the number cannot be recovered."""
    row = _number_of(db, customer_id, number_id)
    if row.status == "released":
        return number_out(row)
    return number_out(nums.release(db, row))


# ------------------------------------------------------------------------------------- dialer
@v1.get("/customers/{customer_id}/caller-ids", summary="Numbers the dialer may call from")
def caller_ids(customer_id: str = CustomerId, db: Session = Depends(get_db)):
    """Active numbers whose KYC is currently accepted. Empty if KYC is suspended or expired."""
    acct = db.get(ClientAccount, customer_id)
    return {"customer_id": customer_id, "plivo_subaccount_id": acct.plivo_subaccount_id if acct else None,
            "numbers": [{"id": n.id, "number": n.number, "e164": "+" + n.number, "alias": n.alias,
                         "city": n.city} for n in nums.callable_numbers(db, customer_id)]}


@v1.get("/caller-ids/validate", summary="May this customer call from this number right now?")
def validate_caller_id(customer_id: str = Query(min_length=1, max_length=64), number: str = Query(min_length=6),
                       db: Session = Depends(get_db)):
    wanted = number.strip().lstrip("+")
    return {"allowed": any(n.number == wanted for n in nums.callable_numbers(db, customer_id))}


# ------------------------------------------------------------------------------------- webhooks
@v1.get("/webhook-events", summary="Recent webhook events (debugging)")
def list_events(status: str | None = Query(None, pattern="^(pending|delivered|failed)$"),
                customer_id: str | None = None, limit: int = Query(50, ge=1, le=200), db: Session = Depends(get_db)):
    q = select(WebhookEvent).order_by(WebhookEvent.created_at.desc()).limit(limit)
    if status:
        q = q.where(WebhookEvent.status == status)
    if customer_id:
        q = q.where(WebhookEvent.client_id == customer_id)
    return [events.event_out(e) for e in db.scalars(q).all()]


@v1.post("/webhook-events/{event_id}/redeliver", summary="Send an event again")
def redeliver_event(event_id: str, db: Session = Depends(get_db)):
    evt = db.get(WebhookEvent, event_id)
    if not evt:
        raise HTTPException(404, "Event not found")
    return events.event_out(events.redeliver(db, evt))


@v1.post("/webhook-events/test", status_code=201, summary="Queue a test event (webhook.test)")
def test_event(db: Session = Depends(get_db), s: Settings = Depends(get_settings)):
    if not s.webhook_url:
        raise HTTPException(409, "WEBHOOK_URL is not configured")
    evt = events.emit(db, "webhook.test", None, {"message": "Webhook delivery works"})
    db.commit()
    return events.event_out(evt)
