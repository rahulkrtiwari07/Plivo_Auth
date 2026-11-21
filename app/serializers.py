"""JSON shapes shared by the HTTP APIs and webhook payloads."""
from app.config import get_settings
from app.models import ComplianceApplication, PhoneNumber
from app.services import wallet


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
            "setup_price": round(n.setup_rate * s.price_markup, 4), "currency": "USD",
            "created_at": n.created_at, "released_at": n.released_at,
            "paid_until": n.paid_until, "billing_status": n.billing_status or "ok",
            **({"price_inr": wallet.price_out()} if s.wallet_billing_enabled else {})}
