import hmac
import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import CurrentClient, get_current_client, mint_client_token, require_admin, webhook_token
from app.config import Settings, get_settings
from app.db import get_db
from app.models import ClientAccount, ComplianceApplication, LedgerEntry, PhoneNumber
from app.plivo_client import UploadFile
from app.services import compliance as kyc
from app.services import numbers as nums

api = APIRouter(prefix="/api", tags=["client"])
webhooks = APIRouter(prefix="/webhooks", tags=["webhooks"])
internal = APIRouter(prefix="/internal", tags=["internal"], dependencies=[Depends(require_admin)])


# ------------------------------------------------------------------------------------- serializers
def app_out(a: ComplianceApplication) -> dict:
    return {"id": a.id, "number_type": a.number_type, "user_type": a.user_type, "status": a.status,
            "rejection_reason": a.rejection_reason, "end_user": a.end_user, "documents": a.documents_meta,
            "plivo_compliance_id": a.plivo_compliance_id, "created_at": a.created_at, "updated_at": a.updated_at,
            "last_synced_at": a.last_synced_at}


def number_out(n: PhoneNumber) -> dict:
    s = get_settings()
    return {"id": n.id, "number": n.number, "e164": "+" + n.number, "number_type": n.number_type,
            "city": n.city, "region": n.region, "alias": n.alias, "status": n.status,
            "monthly_price": round(n.monthly_rental_rate * s.price_markup, 4),
            "setup_price": round(n.setup_rate * s.price_markup, 4),
            "created_at": n.created_at, "released_at": n.released_at}


# ------------------------------------------------------------------------------------- client API
@api.get("/me")
def me(client: CurrentClient = Depends(get_current_client), s: Settings = Depends(get_settings)):
    return {"client_id": client.client_id, "name": client.name, "country_iso": s.country_iso,
            "number_types": s.number_types, "max_numbers": s.max_numbers_per_client, "mock": s.plivo_mock,
            "kyc_documents_mode": s.kyc_documents_mode, "cities": s.cities}


@api.get("/compliance/requirements")
def requirements(number_type: str, user_type: str, _: CurrentClient = Depends(get_current_client)):
    return kyc.get_requirements(number_type, user_type)


@api.get("/compliance/applications")
def list_applications(client: CurrentClient = Depends(get_current_client), db: Session = Depends(get_db)):
    apps = db.scalars(select(ComplianceApplication).where(ComplianceApplication.client_id == client.client_id)
                      .order_by(ComplianceApplication.id.desc())).all()
    for a in apps:  # cheap freshness: pending ones are re-checked when the client looks at them
        if a.status == "submitted":
            kyc.sync_application(db, a)
    return [app_out(a) for a in apps]


async def _read_kyc_form(request: Request) -> tuple[dict, dict[str, UploadFile]]:
    """multipart/form-data: `payload` = JSON string, files as `file_<document_type_id>`."""
    form = await request.form()
    try:
        payload = json.loads(form.get("payload") or "{}")
    except ValueError:
        raise HTTPException(400, "payload must be valid JSON") from None
    files: dict[str, UploadFile] = {}
    for key, value in form.multi_items():
        if key.startswith("file_") and hasattr(value, "read"):
            content = await value.read(kyc.MAX_FILE_BYTES + 1)
            if content:
                files[key[5:]] = UploadFile(value.filename or "document", content, value.content_type or "")
    return payload, files


@api.post("/compliance/applications", status_code=201)
async def create_application(request: Request, client: CurrentClient = Depends(get_current_client),
                             db: Session = Depends(get_db)):
    payload, files = await _read_kyc_form(request)
    a = await run_in_threadpool(kyc.create_application, db, client, payload.get("number_type", ""),
                                payload.get("user_type", ""), payload.get("end_user") or {},
                                payload.get("documents") or [], files)
    return app_out(a)


@api.get("/compliance/applications/{app_id}")
def get_application(app_id: int, client: CurrentClient = Depends(get_current_client), db: Session = Depends(get_db)):
    return app_out(kyc.sync_application(db, kyc.get_client_application(db, client, app_id)))


@api.post("/compliance/applications/{app_id}/resubmit")
async def resubmit_application(app_id: int, request: Request, client: CurrentClient = Depends(get_current_client),
                               db: Session = Depends(get_db)):
    a = kyc.get_client_application(db, client, app_id)
    payload, files = await _read_kyc_form(request)
    a = await run_in_threadpool(kyc.resubmit_application, db, a, payload.get("end_user") or {},
                                payload.get("documents") or [], files)
    return app_out(a)


@api.get("/numbers/search")
def search_numbers(number_type: str = "local", pattern: str | None = None, city: str | None = None,
                   region: str | None = None, offset: int = Query(0, ge=0),
                   client: CurrentClient = Depends(get_current_client), db: Session = Depends(get_db)):
    return nums.search(db, client, number_type, pattern or None, city or None, region or None, offset)


class BuyIn(BaseModel):
    number: str = Field(min_length=6, max_length=20)


@api.post("/numbers", status_code=201)
def buy_number(body: BuyIn, client: CurrentClient = Depends(get_current_client), db: Session = Depends(get_db)):
    return number_out(nums.buy(db, client, body.number))


@api.get("/numbers")
def my_numbers(include_released: bool = False, client: CurrentClient = Depends(get_current_client),
               db: Session = Depends(get_db)):
    return [number_out(n) for n in nums.list_numbers(db, client.client_id, include_released)]


class NumberPatch(BaseModel):
    alias: str | None = Field(default=None, max_length=99)


@api.patch("/numbers/{number_id}")
def rename_number(number_id: int, body: NumberPatch, client: CurrentClient = Depends(get_current_client),
                  db: Session = Depends(get_db)):
    row = nums.get_client_number(db, client, number_id)
    row.alias = (body.alias or "").strip() or None
    db.commit()
    return number_out(row)


@api.delete("/numbers/{number_id}")
def release_number(number_id: int, client: CurrentClient = Depends(get_current_client), db: Session = Depends(get_db)):
    return number_out(nums.release(db, nums.get_client_number(db, client, number_id)))


@api.get("/dev/token", tags=["dev"])
def dev_token(client_id: str = Query(min_length=1, max_length=64), name: str = "", s: Settings = Depends(get_settings)):
    """Local testing only. In production your dashboard backend mints this token."""
    if not s.dev_mode:
        raise HTTPException(404, "Not found")
    return {"token": mint_client_token(client_id, name, ttl_seconds=8 * 3600)}


# ------------------------------------------------------------------------------------- Plivo webhook
@webhooks.api_route("/plivo/compliance/{app_id}", methods=["GET", "POST"])
def compliance_callback(app_id: int, token: str = "", db: Session = Depends(get_db)):
    """Plivo calls this on compliance status changes. The body is NOT trusted: we use it only as a
    trigger and re-read the status from Plivo with our own credentials."""
    if not hmac.compare_digest(token, webhook_token(app_id)):
        raise HTTPException(403, "bad token")
    a = db.get(ComplianceApplication, app_id)
    if not a:
        raise HTTPException(404, "unknown application")
    kyc.sync_application(db, a, force=True)
    return {"ok": True, "status": a.status}


# ------------------------------------------------------------------------------------- internal (dialer / admin)
@internal.get("/clients/{client_id}/caller-ids")
def caller_ids(client_id: str, db: Session = Depends(get_db)):
    """Numbers your AI dialer may use as caller ID (`from`) for this client, plus the Plivo
    subaccount they live in (fetch its auth token from Plivo with main-account credentials if needed)."""
    acct = db.get(ClientAccount, client_id)
    return {"client_id": client_id,
            "plivo_subaccount_id": acct.plivo_subaccount_id if acct else None,
            "numbers": [{"number": n.number, "e164": "+" + n.number, "number_type": n.number_type,
                         "alias": n.alias} for n in nums.callable_numbers(db, client_id)]}


@internal.get("/caller-ids/validate")
def validate_caller_id(client_id: str, number: str, db: Session = Depends(get_db)):
    """Call before dialing: does `client_id` own `number`, and is its KYC currently accepted?"""
    wanted = number.strip().lstrip("+")
    return {"allowed": any(n.number == wanted for n in nums.callable_numbers(db, client_id))}


@internal.get("/applications")
def all_applications(status: str | None = None, client_id: str | None = None, db: Session = Depends(get_db)):
    q = select(ComplianceApplication).order_by(ComplianceApplication.id.desc())
    if status:
        q = q.where(ComplianceApplication.status == status)
    if client_id:
        q = q.where(ComplianceApplication.client_id == client_id)
    return [{**app_out(a), "client_id": a.client_id, "plivo_subaccount_id": a.plivo_subaccount_id}
            for a in db.scalars(q).all()]


@internal.get("/numbers")
def all_numbers(client_id: str | None = None, db: Session = Depends(get_db)):
    q = select(PhoneNumber).order_by(PhoneNumber.id.desc())
    if client_id:
        q = q.where(PhoneNumber.client_id == client_id)
    return [{**number_out(n), "client_id": n.client_id, "failure_reason": n.failure_reason} for n in db.scalars(q).all()]


@internal.post("/compliance/sync")
def sync_all(db: Session = Depends(get_db)):
    return {"synced": kyc.sync_pending(db)}


@internal.post("/billing/accrue")
def accrue(db: Session = Depends(get_db)):
    return {"entries_created": nums.accrue_monthly_rentals(db)}


@internal.get("/ledger")
def ledger(client_id: str | None = None, db: Session = Depends(get_db)):
    q = select(LedgerEntry).order_by(LedgerEntry.id.desc())
    if client_id:
        q = q.where(LedgerEntry.client_id == client_id)
    return [{"id": e.id, "client_id": e.client_id, "number": e.number, "kind": e.kind,
             "plivo_cost": e.plivo_cost, "amount": e.amount, "currency": e.currency, "created_at": e.created_at}
            for e in db.scalars(q).all()]
