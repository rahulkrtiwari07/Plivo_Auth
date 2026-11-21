"""Number rental billed to the seller's Bigship wallet.

Uses the same Bigship wallet API as AICall (~/AICall/bigagent/dispatcher/billing.py):
    POST {BIGSHIP_WALLET_BASE_URL}/api/get-auth-token           {email, password}  -> data.token
    POST {BIGSHIP_WALLET_BASE_URL}/api/shared/get-wallet-balance  {customer_id}      -> data.wallet_balance (₹)
    POST {BIGSHIP_WALLET_BASE_URL}/api/shared/ai-wallet-deduction {customer_id, amount (₹), mobile}
The token goes in the `x-api-key` header. customer_id is our client_id (Bigship's numeric customer id).

Lifecycle of one number:
  • buy: the balance must cover one period (₹ price + GST) or the purchase is refused (402). After Plivo
    rents the number, the first period is queued as a wallet_charges row and deducted right away.
  • every BILLING_PERIOD_DAYS: the next period is queued and deducted by the background loop.
  • balance too low: the charge waits as `insufficient` and is retried daily. After BILLING_GRACE_DAYS the
    number is marked overdue and taken off the dialer; it comes back as soon as the charge goes through.
    With BILLING_RELEASE_AFTER_DAYS > 0 it is released on Plivo after that many days (irreversible).

Safety, as in AICall: a charge row is saved before the deduction is attempted, and the unique
(number_id, period_start) key means one period can never be queued twice. Bigship's deduction API has no
idempotency key, so a request that may have reached it (timeout, 5xx) is marked `unknown` and never
retried automatically; resolve it by hand (POST /internal/billing/charges/{id}/resolve).
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import HTTPException
from sqlalchemy import exists, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import PhoneNumber, WalletCharge
from app.services import events

log = logging.getLogger(__name__)

_BACKOFF_S = (30, 60, 120, 300, 900, 1800, 3600)
_INSUFFICIENT_RETRY = timedelta(days=1)
BILLABLE_STATUSES = ("active", "pending_activation")


class WalletError(Exception):
    """The wallet could not be read or the request could not be made."""


def now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


def price_paise() -> tuple[int, int, int]:
    """(base, gst, total) for one period, in paise. ₹300 + 18% -> (30000, 5400, 35400)."""
    s = get_settings()
    base = round(s.number_monthly_price_inr * 100)
    gst = round(base * s.number_gst_percent / 100)
    return base, gst, base + gst


def price_out() -> dict:
    base, gst, total = price_paise()
    return {"currency": "INR", "period_days": get_settings().billing_period_days,
            "base": base / 100, "gst_percent": get_settings().number_gst_percent, "gst": gst / 100, "total": total / 100}


# ------------------------------------------------------------------------------------- Bigship API
_token: dict = {"value": None, "expires_at": None}
_token_lock = threading.Lock()


def _http_post(url: str, body: dict, headers: dict) -> httpx.Response:
    """Separate function so tests can replace the network call."""
    with httpx.Client(timeout=get_settings().wallet_http_timeout_seconds) as c:
        return c.post(url, json=body, headers=headers)


def _get_token(force: bool = False) -> str:
    s = get_settings()
    with _token_lock:
        exp = _token["expires_at"]
        if not force and _token["value"] and exp and now() < exp - timedelta(minutes=2):
            return _token["value"]
        if not (s.bigship_wallet_email and s.bigship_wallet_password):
            raise WalletError("Bigship wallet login is not configured (BIGSHIP_WALLET_EMAIL / _PASSWORD)")
        try:
            r = _http_post(f"{s.bigship_wallet_base_url.rstrip('/')}/api/get-auth-token",
                           {"email": s.bigship_wallet_email, "password": s.bigship_wallet_password},
                           {"Accept": "application/json"})
        except httpx.HTTPError as e:
            raise WalletError(f"Bigship login request failed: {type(e).__name__}: {e}") from e
        try:
            data = (r.json() or {}).get("data") or {}
        except ValueError:
            data = {}
        if r.status_code != 200 or not data.get("token"):
            raise WalletError(f"Bigship login failed: HTTP {r.status_code}")
        try:
            exp = datetime.fromisoformat(str(data.get("expires_in")).replace("Z", "+00:00"))
            exp = exp if exp.tzinfo else exp.replace(tzinfo=timezone.utc)
        except ValueError:
            exp = now() + timedelta(minutes=30)
        _token.update(value=data["token"], expires_at=exp)
        return data["token"]


def _post(path: str, body: dict) -> httpx.Response:
    """POST with a cached token; on 401 get a fresh token once (a 401 was refused, so repeating is safe).
    Raises httpx.HTTPError from the actual request, WalletError when we couldn't log in."""
    url = f"{get_settings().bigship_wallet_base_url.rstrip('/')}{path}"
    r = None
    for attempt in (1, 2):
        token = _get_token(force=attempt == 2)
        r = _http_post(url, body, {"x-api-key": token, "Accept": "application/json"})
        if r.status_code != 401:
            return r
    return r


def customer_id_of(client_id: str) -> int:
    if not client_id.strip().isdigit():
        raise WalletError(f"customer id {client_id!r} is not a numeric Bigship customer id")
    return int(client_id.strip())


def balance_paise(client_id: str) -> int:
    try:
        r = _post("/api/shared/get-wallet-balance", {"customer_id": customer_id_of(client_id)})
    except httpx.HTTPError as e:
        raise WalletError(f"balance request failed: {type(e).__name__}: {e}") from e
    try:
        body = r.json()
    except ValueError:
        body = {}
    data = body.get("data") if isinstance(body, dict) else None
    if r.status_code != 200 or not isinstance(data, dict) or "wallet_balance" not in data:
        msg = body.get("message") if isinstance(body, dict) else ""
        raise WalletError(f"balance request -> HTTP {r.status_code}: {msg or r.text[:200]}")
    return int(round(float(data["wallet_balance"]) * 100))


def _ten_digit(number: str) -> str:
    digits = "".join(ch for ch in number if ch.isdigit())
    return digits[2:] if digits.startswith("91") and len(digits) == 12 else digits


# ------------------------------------------------------------------------------------- purchase gate
def require_balance_for_purchase(client_id: str) -> None:
    """Called before renting a number. 402 if the wallet can't pay one period, 503 if unreadable."""
    total = price_paise()[2]
    try:
        bal = balance_paise(client_id)
    except WalletError as e:
        log.warning("client=%s wallet check failed before purchase: %s", client_id, e)
        raise HTTPException(503, "Could not check the wallet balance. Nothing was bought; try again shortly.") from e
    if bal < total:
        raise HTTPException(402, f"Insufficient wallet balance: ₹{total / 100:.2f} (₹{price_paise()[0] / 100:.2f} "
                                 f"+ {get_settings().number_gst_percent:g}% GST) is needed, the balance is ₹{bal / 100:.2f}.")


def queue_charge(db: Session, row: PhoneNumber, period_start: datetime) -> WalletCharge | None:
    """Add the charge for one period. Returns None if that period is already queued. Commits."""
    s = get_settings()
    base, gst, total = price_paise()
    ch = WalletCharge(client_id=row.client_id, number_id=row.id, number=row.number, period_start=period_start,
                      period_end=period_start + timedelta(days=s.billing_period_days),
                      base_paise=base, gst_paise=gst, amount_paise=total, status="pending", next_attempt_at=now())
    db.add(ch)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return None
    log.info("client=%s %s: charge #%d queued ₹%.2f for %s .. %s", row.client_id, row.number, ch.id,
             total / 100, period_start.date(), ch.period_end.date())
    return ch


def charge_out(ch: WalletCharge) -> dict:
    return {"id": ch.id, "number_id": ch.number_id, "number": ch.number, "e164": "+" + ch.number,
            "period_start": ch.period_start, "period_end": ch.period_end, "currency": "INR",
            "base": ch.base_paise / 100, "gst": ch.gst_paise / 100, "amount": ch.amount_paise / 100,
            "status": ch.status, "attempts": ch.attempts, "last_error": ch.last_error,
            "created_at": ch.created_at, "confirmed_at": ch.confirmed_at}


# ------------------------------------------------------------------------------------- sending one charge
def _confirm(db: Session, ch: WalletCharge) -> None:
    ch.status, ch.confirmed_at, ch.last_error = "confirmed", now(), None
    row = db.get(PhoneNumber, ch.number_id)
    if row is not None:
        end = _aware(ch.period_end)
        if row.paid_until is None or _aware(row.paid_until) < end:
            row.paid_until = end
        if row.billing_status != "ok" and not db.scalar(select(exists().where(
                WalletCharge.number_id == row.id, WalletCharge.id != ch.id, WalletCharge.status == "insufficient"))):
            row.billing_status = "ok"
            log.info("%s is paid again: back on the dialer", row.number)
    events.emit(db, "billing.charged", ch.client_id, {"charge": charge_out(ch)})


def send_charge(db: Session, ch: WalletCharge) -> str:
    """Try to deduct one pending/insufficient charge. Returns its new status. Commits."""
    if ch.status not in ("pending", "insufficient"):
        return ch.status
    row = db.get(PhoneNumber, ch.number_id)
    if row is None or row.status not in BILLABLE_STATUSES:
        # Released (or never activated) since this charge was queued: never bill it.
        ch.status, ch.last_error = "cancelled", f"number is {row.status if row else 'gone'}; not charged"
        db.commit()
        log.info("charge #%d cancelled: %s", ch.id, ch.last_error)
        return ch.status
    ch.attempts += 1
    was_insufficient = ch.status == "insufficient"

    def retry_later(err: str) -> str:
        ch.last_error = err[:1000]
        ch.next_attempt_at = now() + timedelta(seconds=_BACKOFF_S[min(ch.attempts, len(_BACKOFF_S)) - 1])
        db.commit()
        log.warning("charge #%d not sent (attempt %d): %s", ch.id, ch.attempts, err)
        return ch.status

    try:
        bal = balance_paise(ch.client_id)
    except WalletError as e:
        if "not a numeric" in str(e):
            ch.status, ch.last_error = "failed", str(e)
            events.emit(db, "billing.needs_review", ch.client_id, {"charge": charge_out(ch)})
            db.commit()
            return ch.status
        return retry_later(str(e))
    if bal < ch.amount_paise:
        ch.status = "insufficient"
        ch.last_error = f"balance ₹{bal / 100:.2f} < ₹{ch.amount_paise / 100:.2f}"
        ch.next_attempt_at = now() + _INSUFFICIENT_RETRY
        if not was_insufficient:
            events.emit(db, "billing.payment_due", ch.client_id, {"charge": charge_out(ch), "balance": bal / 100})
        db.commit()
        log.info("charge #%d: insufficient balance (%s); retry tomorrow", ch.id, ch.last_error)
        return ch.status

    body = {"customer_id": customer_id_of(ch.client_id), "amount": round(ch.amount_paise / 100, 2),
            "mobile": _ten_digit(ch.number)}
    try:
        r = _post("/api/shared/ai-wallet-deduction", body)
    except WalletError as e:                                     # couldn't log in: nothing was sent
        return retry_later(str(e))
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:     # never reached Bigship: safe to retry
        return retry_later(f"{type(e).__name__}: {e}")
    except httpx.HTTPError as e:                                 # may have been deducted
        r = None
        err = f"no clear answer from the deduction API ({type(e).__name__}: {e})"
    else:
        err = f"deduction API HTTP {r.status_code}: {r.text[:300]}" if r.status_code >= 500 else None
    if err:
        ch.status, ch.last_error = "unknown", err[:1000]
        events.emit(db, "billing.needs_review", ch.client_id, {"charge": charge_out(ch)})
        db.commit()
        log.error("charge #%d outcome UNKNOWN (%s): not retried to avoid a double charge; check the wallet "
                  "and resolve it with POST /internal/billing/charges/%d/resolve", ch.id, err, ch.id)
        return ch.status
    if r.status_code == 200:
        _confirm(db, ch)
        db.commit()
        log.info("charge #%d confirmed: ₹%.2f from customer %s for %s", ch.id, ch.amount_paise / 100,
                 ch.client_id, ch.number)
        return ch.status
    if r.status_code == 401:
        return retry_later(f"deduction API refused our login (HTTP 401): {r.text[:200]}")
    ch.status, ch.last_error = "failed", f"HTTP {r.status_code}: {r.text[:500]}"
    events.emit(db, "billing.needs_review", ch.client_id, {"charge": charge_out(ch)})
    db.commit()
    log.error("charge #%d REJECTED by Bigship: %s. Not retried; needs review.", ch.id, ch.last_error)
    return ch.status


def resolve(db: Session, ch: WalletCharge, status: str) -> WalletCharge:
    """Manual fix for unknown/failed charges after checking the wallet: confirmed (it was deducted),
    failed (it wasn't; give up) or retry (send it again)."""
    if ch.status not in ("unknown", "failed", "insufficient"):
        raise HTTPException(409, f"Charge is {ch.status}; only unknown, failed or insufficient charges can be resolved.")
    if status == "confirmed":
        _confirm(db, ch)
    elif status == "retry":
        ch.status, ch.next_attempt_at, ch.last_error = "pending", now(), None
    elif status == "failed":
        ch.status = "failed"
    else:
        raise HTTPException(400, "status must be confirmed, failed or retry")
    db.commit()
    return ch


def cancel_open_charges(db: Session, row: PhoneNumber) -> int:
    """On release: drop this number's charges that haven't been deducted yet. Caller commits."""
    open_charges = db.scalars(select(WalletCharge).where(
        WalletCharge.number_id == row.id, WalletCharge.status.in_(("pending", "insufficient")))).all()
    for ch in open_charges:
        ch.status, ch.last_error = "cancelled", "number released; not charged"
    return len(open_charges)


# ------------------------------------------------------------------------------------- background loop
def _queue_renewals(db: Session) -> int:
    queued = 0
    for row in db.scalars(select(PhoneNumber).where(PhoneNumber.status.in_(BILLABLE_STATUSES))).all():
        if row.paid_until is not None:
            if _aware(row.paid_until) <= now() and queue_charge(db, row, _aware(row.paid_until)):
                queued += 1
        elif not db.scalar(select(exists().where(WalletCharge.number_id == row.id))):
            # Bought before wallet billing was switched on: start billing now.
            if queue_charge(db, row, now()):
                queued += 1
    return queued


def _enforce_overdue(db: Session) -> None:
    s = get_settings()
    unpaid = db.scalars(select(WalletCharge).where(WalletCharge.status == "insufficient")).all()
    for ch in unpaid:
        row = db.get(PhoneNumber, ch.number_id)
        if row is None or row.status not in BILLABLE_STATUSES:
            continue
        late = now() - _aware(ch.period_start)
        if s.billing_release_after_days > 0 and late >= timedelta(days=s.billing_release_after_days):
            from app.services import numbers as nums           # local import: numbers imports this module
            log.warning("releasing %s of customer %s: unpaid for %s", row.number, row.client_id, late)
            try:
                nums.release(db, row)
            except HTTPException as e:
                log.error("could not release %s for non-payment: %s", row.number, e.detail)
                continue
            ch.status, ch.last_error = "failed", "number released for non-payment"
            events.emit(db, "number.released_nonpayment", row.client_id, {"charge": charge_out(ch)})
            db.commit()
        elif late >= timedelta(days=s.billing_grace_days) and row.billing_status != "overdue":
            row.billing_status = "overdue"
            events.emit(db, "number.suspended_nonpayment", row.client_id, {"charge": charge_out(ch)})
            db.commit()
            log.warning("%s of customer %s is overdue: taken off the dialer", row.number, row.client_id)


def run_billing(db: Session) -> dict:
    """One pass: queue due renewals, send due charges, enforce overdue rules."""
    queued = _queue_renewals(db)
    due = db.scalars(select(WalletCharge).where(WalletCharge.status.in_(("pending", "insufficient")),
                                                WalletCharge.next_attempt_at <= now())
                     .order_by(WalletCharge.id)).all()
    results: dict[str, int] = {}
    for ch in due:
        st = send_charge(db, ch)
        results[st] = results.get(st, 0) + 1
    _enforce_overdue(db)
    return {"queued": queued, "sent": results}
