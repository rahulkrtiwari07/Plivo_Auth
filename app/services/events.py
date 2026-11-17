"""Outbound webhooks to the integrating system.

Events are written to the `webhook_events` table in the same DB transaction as the change that caused
them (outbox pattern), then delivered by a background loop with retries. Each POST is signed:

    X-Webhook-Id:        evt_...            (dedupe on this: delivery is at-least-once)
    X-Webhook-Event:     kyc.accepted
    X-Webhook-Timestamp: 1763366400         (unix seconds)
    X-Webhook-Signature: sha256=<hex HMAC-SHA256(WEBHOOK_SECRET, f"{timestamp}.{raw_body}")>
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import time
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import WebhookEvent

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 15  # ~30s, 1m, 2m ... capped at 6h between tries: roughly 2 days in total
_LEASE = timedelta(seconds=60)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


def _json_default(o):
    if isinstance(o, datetime):
        return _aware(o).isoformat()
    raise TypeError(f"not JSON serializable: {type(o)}")


def emit(db: Session, event_type: str, client_id: str | None, data: dict) -> WebhookEvent | None:
    """Queue an event. The caller commits (so the event is saved atomically with the change)."""
    if not get_settings().webhook_url:
        return None
    evt_id = "evt_" + secrets.token_hex(12)
    payload = {"id": evt_id, "type": event_type, "created_at": _now(), "customer_id": client_id, "data": data}
    evt = WebhookEvent(id=evt_id, type=event_type, client_id=client_id,
                       payload=json.loads(json.dumps(payload, default=_json_default)))
    db.add(evt)
    return evt


def sign(secret: str, timestamp: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


def _post(url: str, body: bytes, headers: dict) -> httpx.Response:
    """Separate function so tests can replace the network call."""
    with httpx.Client(timeout=10.0) as client:
        return client.post(url, content=body, headers=headers)


def _deliver(db: Session, evt: WebhookEvent) -> bool:
    s = get_settings()
    body = json.dumps(evt.payload, separators=(",", ":")).encode()
    ts = str(int(time.time()))
    headers = {"Content-Type": "application/json", "User-Agent": "plivo-numbers-service/1.0",
               "X-Webhook-Id": evt.id, "X-Webhook-Event": evt.type, "X-Webhook-Timestamp": ts,
               "X-Webhook-Signature": sign(s.webhook_secret, ts, body)}
    evt.attempts += 1
    try:
        resp = _post(s.webhook_url, body, headers)
        ok, err = resp.is_success, None if resp.is_success else f"HTTP {resp.status_code}: {resp.text[:300]}"
    except httpx.HTTPError as exc:
        ok, err = False, f"{type(exc).__name__}: {exc}"
    evt.locked_until = None
    if ok:
        evt.status, evt.delivered_at, evt.last_error = "delivered", _now(), None
    else:
        evt.last_error = err
        if evt.attempts >= MAX_ATTEMPTS:
            evt.status = "failed"
            log.error("webhook %s (%s) gave up after %d attempts: %s", evt.id, evt.type, evt.attempts, err)
        else:
            delay = min(30 * 2 ** (evt.attempts - 1), 6 * 3600)
            evt.next_attempt_at = _now() + timedelta(seconds=delay)
            log.warning("webhook %s (%s) attempt %d failed (%s); retry in %ss", evt.id, evt.type, evt.attempts, err, delay)
    db.commit()
    return ok


def deliver_due(db: Session, limit: int = 50) -> int:
    """Send events that are due. Safe with several workers: each row is leased before sending."""
    s = get_settings()
    if not s.webhook_url:
        return 0
    now = _now()
    ids = db.scalars(select(WebhookEvent.id).where(
        WebhookEvent.status == "pending", WebhookEvent.next_attempt_at <= now,
        or_(WebhookEvent.locked_until.is_(None), WebhookEvent.locked_until < now),
    ).order_by(WebhookEvent.created_at).limit(limit)).all()
    sent = 0
    for evt_id in ids:
        claimed = db.execute(update(WebhookEvent).where(
            WebhookEvent.id == evt_id, WebhookEvent.status == "pending",
            or_(WebhookEvent.locked_until.is_(None), WebhookEvent.locked_until < now),
        ).values(locked_until=now + _LEASE)).rowcount
        db.commit()
        if claimed:
            sent += _deliver(db, db.get(WebhookEvent, evt_id))
    return sent


def redeliver(db: Session, evt: WebhookEvent) -> WebhookEvent:
    evt.status, evt.next_attempt_at, evt.locked_until = "pending", _now(), None
    if evt.attempts >= MAX_ATTEMPTS:
        evt.attempts = 0
    db.commit()
    return evt


def event_out(e: WebhookEvent) -> dict:
    return {"id": e.id, "type": e.type, "customer_id": e.client_id, "status": e.status, "attempts": e.attempts,
            "last_error": e.last_error, "next_attempt_at": _aware(e.next_attempt_at),
            "created_at": _aware(e.created_at), "delivered_at": _aware(e.delivered_at), "payload": e.payload}
