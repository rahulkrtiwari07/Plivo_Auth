"""Search / buy / release numbers on our master Plivo account on behalf of a client."""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import CurrentClient
from app.config import get_settings
from app.models import ComplianceApplication, LedgerEntry, PhoneNumber
from app.plivo_client import PlivoError, get_plivo
from app.services.compliance import accepted_application

log = logging.getLogger(__name__)

# Search results are remembered per client so that (a) a client can only buy a number they were
# actually offered and (b) we know its price without trusting the browser.
_SEARCH_TTL = 15 * 60
_offers: dict[str, dict[str, tuple[float, dict]]] = {}
_offers_lock = threading.Lock()

# A number occupies a client's slot (and can't be bought by anyone else) while in one of these states.
LIVE_STATUSES = ("pending", "pending_activation", "active")


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _price(cost: float) -> float:
    return round(cost * get_settings().price_markup, 4)


# Common alternative spellings -> Plivo's spelling.
CITY_ALIASES = {"bengaluru": "Bangalore", "bengalooru": "Bangalore", "blr": "Bangalore",
                "bombay": "Mumbai", "navi mumbai": "Mumbai", "new delhi": "Delhi",
                "gurugram": "Gurgaon", "madras": "Chennai", "calcutta": "Kolkata"}


def normalize_city(city: str | None) -> str | None:
    """Map a city to the configured Plivo spelling; reject cities we don't offer."""
    if not city or not city.strip():
        return None
    wanted = CITY_ALIASES.get(city.strip().casefold(), city.strip())
    for c in get_settings().cities:
        if c.casefold() == wanted.casefold():
            return c
    raise HTTPException(400, f"Numbers are available in: {', '.join(get_settings().cities)}")


def search(db: Session, client: CurrentClient, number_type: str, pattern: str | None, city: str | None,
           region: str | None, offset: int = 0) -> dict:
    s = get_settings()
    if number_type not in s.number_types:
        raise HTTPException(400, f"number_type must be one of {s.number_types}")
    if not accepted_application(db, client.client_id, number_type):
        raise HTTPException(403, f"Your KYC for {number_type} numbers must be approved before you can search numbers.")
    city = normalize_city(city) if number_type == "local" else None
    if pattern and not re.fullmatch(r"\d{1,10}", pattern):
        raise HTTPException(400, "pattern must be 1-10 digits")
    try:
        # Plivo: `city` is for local numbers only, `region` for fixed only. `pattern` is a prefix after
        # the country code ("415 returns numbers starting with 1415"), so 80 -> +91 80... (Bangalore).
        data = get_plivo().search_numbers(country_iso=s.country_iso, type=number_type, pattern=pattern,
                                          city=city if number_type == "local" else None,
                                          region=region if number_type == "fixed" else None,
                                          services="voice", limit=20, offset=max(0, offset))
    except PlivoError as exc:
        raise HTTPException(exc.status_code, f"Number search failed: {exc.message}") from exc

    now, results = time.time(), []
    with _offers_lock:
        mine = _offers.setdefault(client.client_id, {})
        for k in [k for k, (ts, _) in mine.items() if now - ts > _SEARCH_TTL]:
            del mine[k]
        for obj in data.get("objects", []):
            if obj.get("voice_enabled") is False:
                continue
            if obj.get("restriction"):
                # city-address / country-address / terms-and-conditions need extra verification that this
                # service doesn't collect; buying would leave the number pending, so don't offer it.
                log.info("skipping %s: restriction=%s", obj.get("number"), obj.get("restriction"))
                continue
            mine[obj["number"]] = (now, {**obj, "_number_type": number_type})
            results.append({
                "number": obj["number"], "city": obj.get("city"), "region": obj.get("region"),
                "type": obj.get("type") or number_type, "setup_price": _price(_f(obj.get("setup_rate"))),
                "monthly_price": _price(_f(obj.get("monthly_rental_rate"))),
                "voice_rate": _f(obj.get("voice_rate")), "sms_enabled": bool(obj.get("sms_enabled")),
            })
    return {"numbers": results, "offset": offset, "has_more": bool((data.get("meta") or {}).get("next"))}


def buy(db: Session, client: CurrentClient, number: str) -> PhoneNumber:
    s = get_settings()
    number = number.lstrip("+").strip()
    with _offers_lock:
        offer = (_offers.get(client.client_id) or {}).get(number)
    if not offer or time.time() - offer[0] > _SEARCH_TTL:
        raise HTTPException(400, "This number is not in your recent search results. Search again and pick a number.")
    obj = offer[1]
    number_type = obj["_number_type"]

    app = accepted_application(db, client.client_id, number_type)
    if not app:
        raise HTTPException(403, f"Your KYC for {number_type} numbers is not approved.")
    held = db.scalar(select(func.count()).select_from(PhoneNumber).where(
        PhoneNumber.client_id == client.client_id, PhoneNumber.status.in_(LIVE_STATUSES)))
    if held >= s.max_numbers_per_client:
        raise HTTPException(409, f"You can hold at most {s.max_numbers_per_client} numbers.")

    row = PhoneNumber(client_id=client.client_id, number=number, number_type=number_type,
                      country_iso=s.country_iso, city=obj.get("city"), region=obj.get("region"),
                      monthly_rental_rate=_f(obj.get("monthly_rental_rate")), setup_rate=_f(obj.get("setup_rate")),
                      status="pending", application_id=app.id)
    db.add(row)
    try:
        db.commit()  # the partial unique index makes this our lock against double-buys
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "This number is already being purchased or owned.") from None

    try:
        resp = get_plivo().buy_number(number, s.plivo_app_id or None, app.plivo_compliance_id,
                                      subaccount=app.plivo_subaccount_id) or {}
    except PlivoError as exc:
        row.status, row.failure_reason = "failed", exc.message
        db.commit()
        raise HTTPException(exc.status_code, f"Purchase failed: {exc.message}") from exc

    # Documented success: {"status": "fulfilled", "numbers": [{"number": ..., "status": "Success"}]}.
    # Plivo also says a number needing verification stays "pending" until approved.
    overall = str(resp.get("status", "")).lower()
    item = next((n for n in resp.get("numbers") or [] if str(n.get("number", "")).lstrip("+") == number), {})
    item_status = str(item.get("status", "")).lower()
    if overall == "fulfilled" and item_status in ("success", ""):
        row.status = "active"
    elif "fail" in overall or "fail" in item_status:
        row.status, row.failure_reason = "failed", json.dumps(resp)[:1000]
        db.commit()
        raise HTTPException(502, "Purchase failed at the carrier. Please try another number.")
    else:
        row.status, row.failure_reason = "pending_activation", json.dumps(resp)[:1000]
        log.warning("%s bought but not fulfilled yet: %s", number, resp)
    db.add_all([
        LedgerEntry(client_id=client.client_id, number=number, kind="setup",
                    plivo_cost=row.setup_rate, amount=_price(row.setup_rate)),
        LedgerEntry(client_id=client.client_id, number=number, kind="monthly_rental",
                    plivo_cost=row.monthly_rental_rate, amount=_price(row.monthly_rental_rate)),
    ])
    db.commit()
    with _offers_lock:
        (_offers.get(client.client_id) or {}).pop(number, None)
    log.info("client=%s bought %s under compliance %s", client.client_id, number, app.plivo_compliance_id)

    # Tag the number with the owning client in the Plivo console. Best-effort.
    try:
        get_plivo().update_number(number, alias=f"client:{client.client_id}"[:99])
    except PlivoError as exc:
        log.warning("could not set alias on %s: %s", number, exc.message)
    return row


def list_numbers(db: Session, client_id: str, include_released: bool = False) -> list[PhoneNumber]:
    q = select(PhoneNumber).where(PhoneNumber.client_id == client_id)
    statuses = ("pending_activation", "active", "released") if include_released else ("pending_activation", "active")
    q = q.where(PhoneNumber.status.in_(statuses))
    return list(db.scalars(q.order_by(PhoneNumber.created_at.desc())).all())


def callable_numbers(db: Session, client_id: str) -> list[PhoneNumber]:
    """Active numbers whose KYC is currently accepted. Plivo rejects India calls when the linked
    compliance application is suspended (UCC complaints) or expired, so don't hand those to the dialer."""
    q = (select(PhoneNumber).join(ComplianceApplication, PhoneNumber.application_id == ComplianceApplication.id)
         .where(PhoneNumber.client_id == client_id, PhoneNumber.status == "active",
                ComplianceApplication.status == "accepted")
         .order_by(PhoneNumber.created_at.desc()))
    return list(db.scalars(q).all())


def get_client_number(db: Session, client: CurrentClient, number_id: int) -> PhoneNumber:
    row = db.get(PhoneNumber, number_id)
    if not row or row.client_id != client.client_id or row.status not in ("active", "pending_activation"):
        raise HTTPException(404, "Number not found")
    return row


def release(db: Session, row: PhoneNumber) -> PhoneNumber:
    try:
        get_plivo().delete_number(row.number)
    except PlivoError as exc:
        if exc.status_code != 404 and "not found" not in exc.message.lower():
            raise HTTPException(exc.status_code, f"Release failed: {exc.message}") from exc
        log.warning("%s was already gone from Plivo; marking released", row.number)
    row.status, row.released_at = "released", datetime.now(timezone.utc)
    db.commit()
    return row


def accrue_monthly_rentals(db: Session) -> int:
    """Add a monthly_rental ledger line for every active number whose last one is >= 30 days old.
    Run daily (cron -> POST /internal/billing/accrue)."""
    now, created = datetime.now(timezone.utc), 0
    for row in db.scalars(select(PhoneNumber).where(PhoneNumber.status == "active")).all():
        last = db.scalar(select(func.max(LedgerEntry.created_at)).where(
            LedgerEntry.number == row.number, LedgerEntry.client_id == row.client_id,
            LedgerEntry.kind == "monthly_rental"))
        if last is not None and last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if last is None or now - last >= timedelta(days=30):
            db.add(LedgerEntry(client_id=row.client_id, number=row.number, kind="monthly_rental",
                               plivo_cost=row.monthly_rental_rate, amount=_price(row.monthly_rental_rate)))
            created += 1
    db.commit()
    return created
