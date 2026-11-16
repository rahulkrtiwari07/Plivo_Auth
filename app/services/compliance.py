"""KYC (Plivo PhoneNumber Compliance) workflow, per dashboard client.

We operate in Plivo's *reseller* model: one compliance application per customer, holding that
customer's own KYC (end user + documents). Numbers rented for the customer are tied to it.
"""
from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import CurrentClient, webhook_token
from app.config import get_settings
from app.models import ClientAccount, ComplianceApplication
from app.plivo_client import PlivoError, UploadFile, get_plivo

log = logging.getLogger(__name__)

# India: only India-registered businesses can rent numbers / call domestically (Plivo India Calling docs).
USER_TYPES = ("business",)
# A client with an application in one of these states may not open another for the same number type.
# 'suspended' is included so a client blocked for UCC complaints can't sidestep it with a fresh KYC.
BLOCKING_STATUSES = ("draft", "submitted", "accepted", "suspended")

# Accepted India registration documents: exactly ONE of GST certificate, COI or Udyam certificate.
REG_FORMATS = {
    "GSTIN": re.compile(r"\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]"),
    "CIN": re.compile(r"[LU]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}"),
    "Udyam": re.compile(r"UDYAM-[A-Z]{2}-\d{2}-\d{7}"),
}
_PAN = re.compile(r"[A-Z]{5}\d{4}[A-Z]")
MAX_FILE_BYTES = 5 * 1024 * 1024
END_USER_FIELDS = ("type", "name", "last_name", "email", "address_line1", "address_line2", "city",
                   "state", "postal_code", "country", "registration_number")

_req_cache: dict[tuple, tuple[float, dict]] = {}
_req_lock = threading.Lock()
_REQ_TTL = 3600


def _check_number_type(number_type: str) -> None:
    allowed = get_settings().number_types
    if number_type not in allowed:
        raise HTTPException(400, f"number_type must be one of {allowed}")


def get_requirements(number_type: str, user_type: str) -> dict:
    _check_number_type(number_type)
    if user_type not in USER_TYPES:
        raise HTTPException(400, f"user_type must be one of {USER_TYPES}")
    key = (get_settings().country_iso, number_type, user_type)
    with _req_lock:
        hit = _req_cache.get(key)
        if hit and time.time() - hit[0] < _REQ_TTL:
            return hit[1]
    data = get_plivo().get_requirements(*key)
    with _req_lock:
        _req_cache[key] = (time.time(), data)
    return data


# ---------------------------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------------------------
_MAGIC = {b"%PDF": "application/pdf", b"\x89PNG": "image/png", b"\xff\xd8\xff": "image/jpeg"}


def _sniff(content: bytes) -> str | None:
    for magic, ctype in _MAGIC.items():
        if content.startswith(magic):
            return ctype
    return None


def _safe_filename(name: str, ctype: str) -> str:
    ext = {"application/pdf": ".pdf", "image/png": ".png", "image/jpeg": ".jpg"}[ctype]
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", (name or "document").rsplit(".", 1)[0]).strip("._") or "document"
    return stem[: 99 - len(ext)] + ext


def validate_end_user(end_user: dict, user_type: str) -> dict:
    eu = {k: str(v).strip() for k, v in (end_user or {}).items() if k in END_USER_FIELDS and v not in (None, "")}
    eu["type"] = user_type
    eu.setdefault("country", get_settings().country_iso)
    if not eu.get("name"):
        raise HTTPException(400, "end_user.name is required (registered company name)")
    reg = re.sub(r"\s+", "", eu.get("registration_number", "")).upper()
    if not reg:
        raise HTTPException(400, "Registration number is required (GSTIN, CIN or Udyam number)")
    if _PAN.fullmatch(reg):
        raise HTTPException(400, "A business PAN alone is not accepted. Enter your GSTIN, CIN or Udyam number.")
    if not any(p.fullmatch(reg) for p in REG_FORMATS.values()):
        raise HTTPException(400, "Registration number must be a GSTIN (e.g. 29ABCDE1234F1Z5), "
                                 "CIN (e.g. U72200KA2020PTC123456) or Udyam number (e.g. UDYAM-KA-01-0000001)")
    eu["registration_number"] = reg
    if eu.get("email") and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", eu["email"]):
        raise HTTPException(400, "end_user.email is not a valid email address")
    if eu.get("postal_code") and eu["country"] == "IN" and not re.fullmatch(r"\d{6}", eu["postal_code"]):
        raise HTTPException(400, "end_user.postal_code must be a 6 digit PIN code")
    return eu


def _norm(name: str) -> str:
    return re.sub(r"\s+", " ", name or "").strip().casefold()


def validate_documents(requirements: dict, documents: list[dict], files: dict[str, UploadFile],
                       business_name: str | None = None) -> tuple[list[dict], list[tuple[int, UploadFile]], list[dict]]:
    """Returns (plivo documents[], [(document index, file)], local metadata).

    KYC_DOCUMENTS_MODE=all (default): every document type in Plivo's Requirements response is required.
      This is what the Compliance API page specifies for India: Registration Certificate (COI/Udyam)
      AND GST Certificate.
    KYC_DOCUMENTS_MODE=one: exactly one of the listed types. This is the "any one of GST/COI/Udyam" wording
      on Plivo's India console guide.
    """
    types = {d["document_type_id"]: d for d in requirements.get("document_types", [])}
    documents = documents or []
    names = " and ".join(d["name"] for d in types.values())
    if get_settings().kyc_documents_mode == "one":
        if len(documents) != 1:
            raise HTTPException(400, f"Upload exactly one of: {', '.join(d['name'] for d in types.values())}")
    else:
        missing = [types[t]["name"] for t in types if t not in {d.get("document_type_id") for d in documents}]
        if missing:
            raise HTTPException(400, f"All of these documents are required: {names}. Missing: {', '.join(missing)}")
    out_docs, out_files, meta, seen, hashes = [], [], [], set(), {}
    for doc in documents:
        dt_id = doc.get("document_type_id")
        dt = types.get(dt_id)
        if not dt:
            raise HTTPException(400, f"Unknown document_type_id {dt_id!r}")
        if dt_id in seen:
            raise HTTPException(400, f"Document type {dt['name']!r} submitted twice")
        seen.add(dt_id)

        given = {k: str(v).strip() for k, v in (doc.get("data_fields") or {}).items() if v not in (None, "")}
        # Plivo (India): "The business name must match exactly across both documents and the
        # end_user.name / business_name data field". Fill it in, and refuse a mismatch.
        if business_name and any(f["field_name"] == "business_name" for f in dt.get("required_fields") or []):
            if given.get("business_name") and _norm(given["business_name"]) != _norm(business_name):
                raise HTTPException(400, f"{dt['name']}: Business Name must match the registered business name "
                                         f"exactly ({business_name!r})")
            given["business_name"] = business_name
        clean: dict[str, str] = {}
        for f in dt.get("required_fields") or []:
            name, label, val = f["field_name"], f.get("friendly_name") or f["field_name"], given.get(f["field_name"])
            if not val:
                if f.get("required"):
                    raise HTTPException(400, f"{dt['name']}: '{label}' is required")
                continue
            if f.get("min_length") and len(val) < int(f["min_length"]):
                raise HTTPException(400, f"{dt['name']}: '{label}' must be at least {f['min_length']} characters")
            if f.get("max_length") and len(val) > int(f["max_length"]):
                raise HTTPException(400, f"{dt['name']}: '{label}' must be at most {f['max_length']} characters")
            if f.get("field_type") == "enum" and f.get("enums"):
                options = [o.strip() for o in str(f["enums"]).split(",")] if isinstance(f["enums"], str) else list(f["enums"])
                if val not in options:
                    raise HTTPException(400, f"{dt['name']}: '{label}' must be one of {options}")
            if f.get("field_type") == "date":
                try:
                    datetime.strptime(val, "%Y-%m-%d")
                except ValueError:
                    raise HTTPException(400, f"{dt['name']}: '{label}' must be a date (YYYY-MM-DD)") from None
            clean[name] = val

        entry: dict = {"document_type_id": dt_id}
        if clean:
            entry["data_fields"] = clean
        upload = files.get(dt_id)
        if dt.get("proof_required") and not upload:
            raise HTTPException(400, f"{dt['name']}: a file upload (PDF/JPG/PNG) is required")
        if upload:
            if len(upload.content) > MAX_FILE_BYTES:
                raise HTTPException(400, f"{dt['name']}: file is larger than 5 MB")
            ctype = _sniff(upload.content)
            if not ctype:
                raise HTTPException(400, f"{dt['name']}: only PDF, JPEG or PNG files are accepted")
            digest = hashlib.sha256(upload.content).hexdigest()
            if digest in hashes:  # Plivo rejects the same file in two document slots
                raise HTTPException(400, f"{dt['name']}: this is the same file as {hashes[digest]}. "
                                         "Upload the correct document for each slot.")
            hashes[digest] = dt["name"]
            upload = UploadFile(_safe_filename(upload.filename, ctype), upload.content, ctype)
            # Plivo pairs documents[i] (in `data`) with the multipart field documents[i].file.
            out_files.append((len(out_docs), upload))
        out_docs.append(entry)
        meta.append({"document_type_id": dt_id, "name": dt["name"], "filename": upload.filename if upload else None,
                     "data_fields": clean})
    return out_docs, out_files, meta


# ---------------------------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------------------------
def _callback(app_id: int) -> dict:
    base = get_settings().public_base_url.rstrip("/")
    if not base.startswith("https://"):
        return {}  # Plivo requires HTTPS callbacks; the poller covers us otherwise.
    return {"callback_url": f"{base}/webhooks/plivo/compliance/{app_id}?token={webhook_token(app_id)}",
            "callback_method": "POST"}


def ensure_subaccount(db: Session, client: CurrentClient) -> str | None:
    """Plivo reseller flow: each customer gets its own subaccount, holding its KYC and numbers.
    Returns the subaccount auth_id, or None when PLIVO_USE_SUBACCOUNTS=false (everything on main)."""
    acct = db.get(ClientAccount, client.client_id)
    if acct is None:
        acct = ClientAccount(client_id=client.client_id, name=client.name[:128])
        db.add(acct)
        db.commit()
    if not get_settings().plivo_use_subaccounts:
        return None
    if not acct.plivo_subaccount_id:
        try:
            resp = get_plivo().create_subaccount(f"{client.name} ({client.client_id})")
        except PlivoError as exc:
            raise HTTPException(exc.status_code, f"Could not create Plivo subaccount: {exc.message}") from exc
        # Only the auth_id is stored. The main account can always read the subaccount's token from Plivo.
        acct.plivo_subaccount_id = resp["auth_id"]
        db.commit()
        log.info("client=%s -> plivo subaccount %s", client.client_id, acct.plivo_subaccount_id)
    return acct.plivo_subaccount_id


def create_application(db: Session, client: CurrentClient, number_type: str, user_type: str,
                       end_user: dict, documents: list[dict], files: dict[str, UploadFile]) -> ComplianceApplication:
    s = get_settings()
    _check_number_type(number_type)
    user_type = user_type or "business"
    if user_type not in USER_TYPES:
        raise HTTPException(400, "Only India-registered businesses can rent Indian numbers")
    existing = db.scalars(select(ComplianceApplication).where(
        ComplianceApplication.client_id == client.client_id,
        ComplianceApplication.number_type == number_type,
        ComplianceApplication.status.in_(BLOCKING_STATUSES),
    )).first()
    if existing:
        if existing.status == "suspended":
            raise HTTPException(409, "Your KYC is suspended because of unresolved spam (UCC) complaints. "
                                     "Submit the consent proof to support to restore it.")
        raise HTTPException(409, f"You already have a {existing.status} KYC application for {number_type} numbers "
                                 f"(#{existing.id}). Wait for review or use it to buy numbers.")

    # Validate everything before creating anything on Plivo.
    reqs = get_requirements(number_type, user_type)
    eu = validate_end_user(end_user, user_type)
    plivo_docs, plivo_files, meta = validate_documents(reqs, documents, files, eu["name"])
    subaccount = ensure_subaccount(db, client)

    app = ComplianceApplication(client_id=client.client_id, alias="pending", country_iso=s.country_iso,
                                number_type=number_type, user_type=user_type, status="draft",
                                end_user=eu, documents_meta=meta, plivo_subaccount_id=subaccount)
    db.add(app)
    db.flush()
    # Alias must be unique per end user on Plivo; include our row id + client id so it is unique and traceable.
    app.alias = f"{client.client_id}|{number_type}|{app.id}"[:99]
    payload = {"country_iso": s.country_iso, "number_type": number_type, "alias": app.alias,
               "end_user": eu, "documents": plivo_docs, **_callback(app.id)}
    if subaccount:
        payload["subaccount"] = subaccount
    try:
        resp = get_plivo().create_compliance(payload, plivo_files)
    except PlivoError as exc:
        db.rollback()
        raise HTTPException(exc.status_code, f"Plivo rejected the KYC submission: {exc.message}") from exc
    app.plivo_compliance_id = resp.get("compliance_id")
    app.status = "submitted"
    db.commit()
    log.info("client=%s submitted compliance %s (local #%s)", client.client_id, app.plivo_compliance_id, app.id)
    return app


def resubmit_application(db: Session, app: ComplianceApplication, end_user: dict, documents: list[dict],
                         files: dict[str, UploadFile]) -> ComplianceApplication:
    sync_application(db, app, force=True)
    if app.status != "rejected":
        raise HTTPException(409, f"Only rejected applications can be corrected (current status: {app.status})")
    reqs = get_requirements(app.number_type, app.user_type)
    eu = validate_end_user({**app.end_user, **(end_user or {})}, app.user_type)
    plivo_docs, plivo_files, meta = validate_documents(reqs, documents, files, eu["name"])
    payload = {"end_user": {k: v for k, v in eu.items() if k != "type"}, "documents": plivo_docs, **_callback(app.id)}
    try:
        get_plivo().update_compliance(app.plivo_compliance_id, payload, plivo_files)
    except PlivoError as exc:
        raise HTTPException(exc.status_code, f"Plivo rejected the update: {exc.message}") from exc
    app.end_user, app.documents_meta = eu, meta
    app.status, app.rejection_reason = "submitted", None
    db.commit()
    return app


SYNC_MIN_INTERVAL = 20  # seconds; Plivo allows 60 compliance requests/minute per account


def sync_application(db: Session, app: ComplianceApplication, force: bool = False) -> ComplianceApplication:
    """Pull the authoritative status from Plivo. Page views are throttled per application so many
    clients refreshing their KYC page can't exhaust Plivo's rate limit; callbacks/poller pass force=True."""
    if not app.plivo_compliance_id:
        return app
    last = app.last_synced_at
    if last is not None and last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    if not force and last and (datetime.now(timezone.utc) - last).total_seconds() < SYNC_MIN_INTERVAL:
        return app
    try:
        data = get_plivo().get_compliance(app.plivo_compliance_id,
                                          subaccount=app.plivo_subaccount_id).get("compliance", {})
    except PlivoError as exc:
        log.warning("sync of compliance %s failed: %s", app.plivo_compliance_id, exc.message)
        return app
    new_status = data.get("status") or app.status
    if new_status != app.status:
        log.info("compliance %s: %s -> %s", app.plivo_compliance_id, app.status, new_status)
    app.status = new_status
    app.rejection_reason = data.get("rejection_reason")
    app.last_synced_at = datetime.now(timezone.utc)
    db.commit()
    return app


def get_client_application(db: Session, client: CurrentClient, app_id: int) -> ComplianceApplication:
    app = db.get(ComplianceApplication, app_id)
    if not app or app.client_id != client.client_id:
        raise HTTPException(404, "Application not found")
    return app


def accepted_application(db: Session, client_id: str, number_type: str) -> ComplianceApplication | None:
    def find(status):
        return db.scalars(select(ComplianceApplication).where(
            ComplianceApplication.client_id == client_id,
            ComplianceApplication.number_type == number_type,
            ComplianceApplication.status == status,
        ).order_by(ComplianceApplication.id.desc())).all()

    found = find("accepted")
    if not found:  # approval may have landed since the last callback/poll
        for pending in find("submitted"):
            sync_application(db, pending)
        found = find("accepted")
    return found[0] if found else None


def sync_pending(db: Session, max_per_run: int = 30) -> int:
    """Refresh pending applications, least-recently-synced first. Capped to stay well inside Plivo's
    60 compliance requests/minute; anything left over is picked up on the next run."""
    apps = db.scalars(select(ComplianceApplication)
                      .where(ComplianceApplication.status == "submitted")
                      .order_by(ComplianceApplication.last_synced_at.is_not(None), ComplianceApplication.last_synced_at)
                      .limit(max_per_run)).all()
    for app in apps:
        sync_application(db, app, force=True)
    return len(apps)
