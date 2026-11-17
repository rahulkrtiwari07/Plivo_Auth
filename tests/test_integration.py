import hashlib
import hmac
import json
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from app import plivo_client
from app.config import get_settings
from app.db import SessionLocal
from app.main import app
from app.models import WebhookEvent
from app.services import compliance as kyc
from app.services import events

KEY = {"X-API-Key": "int-key"}
PDF = b"%PDF-1.4\n%registration certificate\n"
PDF2 = b"%PDF-1.4\n%gst certificate\n"
FILES = {"file_mock-doc-registration": ("coi.pdf", PDF, "application/pdf"),
         "file_mock-doc-gst": ("gst.pdf", PDF2, "application/pdf")}


@pytest.fixture()
def client(monkeypatch):
    plivo_client.set_plivo(plivo_client.MockPlivoClient())
    monkeypatch.setattr(kyc, "SYNC_MIN_INTERVAL", 0)
    with TestClient(app) as c:
        yield c


def kyc_payload(name="Bigship Customer Pvt Ltd"):
    return {"number_type": "local",
            "end_user": {"name": name, "registration_number": "U72200KA2020PTC123456", "email": "ops@cust.in",
                         "address_line1": "1 MG Road", "city": "Bangalore", "state": "Karnataka",
                         "postal_code": "560001"},
            "documents": [{"document_type_id": "mock-doc-registration"}, {"document_type_id": "mock-doc-gst"}]}


def submit(c, cid, payload=None):
    return c.post(f"/v1/customers/{cid}/kyc", headers=KEY, data={"payload": json.dumps(payload or kyc_payload())},
                  files=FILES)


def events_of(cid):
    with SessionLocal() as db:
        return [e.type for e in db.query(WebhookEvent).filter(WebhookEvent.client_id == cid)
                .order_by(WebhookEvent.created_at).all()]


def approved(c, monkeypatch, cid):
    assert submit(c, cid).status_code == 201
    monkeypatch.setattr(plivo_client, "MOCK_ACCEPT_AFTER", 0)
    assert c.get(f"/v1/customers/{cid}", headers=KEY).json()["kyc"]["local"]["status"] == "accepted"


def test_auth(client, monkeypatch):
    assert client.get("/v1/config").status_code == 401
    assert client.get("/v1/config", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/v1/config", headers=KEY).status_code == 200
    # client JWTs are not accepted here, and the /v1 key is not accepted on /internal
    assert client.get("/internal/numbers", headers=KEY).status_code == 401
    monkeypatch.setattr(get_settings(), "integration_api_key", "")
    assert client.get("/v1/config", headers=KEY).status_code == 503


def test_config_and_requirements(client):
    cfg = client.get("/v1/config", headers=KEY).json()
    assert cfg["cities"] == ["Bangalore", "Mumbai"] and cfg["currency"] == "USD"
    req = client.get("/v1/kyc/requirements?number_type=local", headers=KEY).json()
    assert [d["document_type_id"] for d in req["document_types"]] == ["mock-doc-registration", "mock-doc-gst"]
    assert req["kyc_documents_mode"] == "all"


def test_customer_id_validation(client):
    assert client.get("/v1/customers/bad id!", headers=KEY).status_code == 422
    assert client.get("/v1/customers/" + "x" * 65, headers=KEY).status_code == 422


def test_full_flow(client, monkeypatch):
    cid = "bs_1001"
    r = client.put(f"/v1/customers/{cid}", headers=KEY, json={"name": "Acme Logistics"})
    assert r.status_code == 200 and r.json()["exists"] and r.json()["kyc"]["local"] is None

    # before KYC: can't search
    assert client.get(f"/v1/customers/{cid}/numbers/available", headers=KEY).status_code == 403

    r = submit(client, cid)
    assert r.status_code == 201, r.text
    app_id = r.json()["id"]
    summary = client.get(f"/v1/customers/{cid}", headers=KEY).json()
    assert summary["kyc"]["local"]["status"] == "submitted" and summary["can_buy"]["local"] is False
    assert summary["plivo_subaccount_id"].startswith("SA")

    monkeypatch.setattr(plivo_client, "MOCK_ACCEPT_AFTER", 0)
    summary = client.get(f"/v1/customers/{cid}", headers=KEY).json()
    assert summary["kyc"]["local"]["status"] == "accepted" and summary["can_buy"]["local"] is True
    assert [a["id"] for a in client.get(f"/v1/customers/{cid}/kyc", headers=KEY).json()] == [app_id]
    assert client.get(f"/v1/customers/{cid}/kyc/{app_id}", headers=KEY).json()["status"] == "accepted"

    avail = client.get(f"/v1/customers/{cid}/numbers/available?city=Bengaluru&starts_with=8091", headers=KEY).json()
    nums = avail["numbers"]
    assert nums and all(n["number"].startswith("918091") and n["city"] == "Bangalore" for n in nums)

    # buy with idempotency: retry returns the same number, no second purchase
    h = {**KEY, "Idempotency-Key": "order-555"}
    r1 = client.post(f"/v1/customers/{cid}/numbers", headers=h, json={"number": "+" + nums[0]["number"]})
    assert r1.status_code == 201, r1.text
    r2 = client.post(f"/v1/customers/{cid}/numbers", headers=h, json={"number": nums[0]["number"]})
    assert r2.status_code == 200 and r2.json()["id"] == r1.json()["id"]
    r3 = client.post(f"/v1/customers/{cid}/numbers", headers=h, json={"number": nums[1]["number"]})
    assert r3.status_code == 409
    ledger = client.get(f"/internal/ledger?client_id={cid}", headers={"X-Admin-Key": "admin"}).json()
    assert len(ledger) == 2  # one setup + one monthly rental, not doubled by the retry

    num = r1.json()
    assert num["status"] == "active" and num["currency"] == "USD"
    assert client.patch(f"/v1/customers/{cid}/numbers/{num['id']}", headers=KEY,
                        json={"alias": "Sales line"}).json()["alias"] == "Sales line"
    assert client.get(f"/v1/customers/{cid}/numbers/{num['id']}", headers=KEY).json()["alias"] == "Sales line"
    assert [n["id"] for n in client.get(f"/v1/customers/{cid}/numbers", headers=KEY).json()] == [num["id"]]

    ids = client.get(f"/v1/customers/{cid}/caller-ids", headers=KEY).json()
    assert [n["number"] for n in ids["numbers"]] == [num["number"]]
    assert client.get(f"/v1/caller-ids/validate?customer_id={cid}&number=%2B{num['number']}",
                      headers=KEY).json()["allowed"] is True

    released = client.delete(f"/v1/customers/{cid}/numbers/{num['id']}", headers=KEY).json()
    assert released["status"] == "released"
    assert client.get(f"/v1/customers/{cid}/numbers", headers=KEY).json() == []
    assert client.get(f"/v1/caller-ids/validate?customer_id={cid}&number={num['number']}",
                      headers=KEY).json()["allowed"] is False

    assert events_of(cid) == ["kyc.submitted", "kyc.accepted", "number.purchased", "number.released"]


def test_customers_are_isolated(client, monkeypatch):
    approved(client, monkeypatch, "iso_a")
    app_id = client.get("/v1/customers/iso_a/kyc", headers=KEY).json()[0]["id"]
    n = client.get("/v1/customers/iso_a/numbers/available?starts_with=8092", headers=KEY).json()["numbers"][0]
    num = client.post("/v1/customers/iso_a/numbers", headers=KEY, json={"number": n["number"]}).json()
    assert client.get(f"/v1/customers/iso_b/kyc/{app_id}", headers=KEY).status_code == 404
    assert client.get(f"/v1/customers/iso_b/numbers/{num['id']}", headers=KEY).status_code == 404
    assert client.delete(f"/v1/customers/iso_b/numbers/{num['id']}", headers=KEY).status_code == 404
    # B can't buy a number that was only offered to A
    assert client.post("/v1/customers/iso_b/numbers", headers=KEY, json={"number": n["number"]}).status_code == 400


def test_rejected_kyc_and_resubmit(client, monkeypatch):
    cid = "bs_rej"
    app_id = submit(client, cid, kyc_payload(name="Reject Me Pvt Ltd")).json()["id"]
    monkeypatch.setattr(plivo_client, "MOCK_ACCEPT_AFTER", 0)
    summary = client.get(f"/v1/customers/{cid}", headers=KEY).json()
    assert summary["kyc"]["local"]["status"] == "rejected" and summary["kyc"]["local"]["rejection_reason"]
    r = client.post(f"/v1/customers/{cid}/kyc/{app_id}/resubmit", headers=KEY,
                    data={"payload": json.dumps(kyc_payload(name="Good Name Pvt Ltd"))}, files=FILES)
    assert r.status_code == 200 and r.json()["status"] == "submitted"
    assert client.get(f"/v1/customers/{cid}", headers=KEY).json()["kyc"]["local"]["status"] == "accepted"
    assert events_of(cid) == ["kyc.submitted", "kyc.rejected", "kyc.submitted", "kyc.accepted"]


def test_failed_purchase_with_idempotency(client, monkeypatch):
    cid = "bs_fail"
    approved(client, monkeypatch, cid)
    n = client.get(f"/v1/customers/{cid}/numbers/available?starts_with=8093", headers=KEY).json()["numbers"][0]

    def boom(*a, **k):
        raise plivo_client.PlivoError("Number is no longer available", 400)
    monkeypatch.setattr(plivo_client.get_plivo(), "buy_number", boom)
    h = {**KEY, "Idempotency-Key": "order-fail-1"}
    assert client.post(f"/v1/customers/{cid}/numbers", headers=h, json={"number": n["number"]}).status_code == 400
    r = client.post(f"/v1/customers/{cid}/numbers", headers=h, json={"number": n["number"]})
    assert r.status_code == 409 and "earlier request" in r.json()["detail"]
    assert "number.purchase_failed" in events_of(cid)


def test_webhook_delivery_signature_and_retry(client, monkeypatch):
    cid = "bs_hook"
    with SessionLocal() as db:  # start from a clean outbox
        db.query(WebhookEvent).delete()
        db.commit()
    submit(client, cid)

    sent = []

    def ok_post(url, body, headers):
        sent.append((url, body, headers))
        return httpx.Response(200)
    monkeypatch.setattr(events, "_post", ok_post)
    with SessionLocal() as db:
        assert events.deliver_due(db) == 1
    url, body, headers = sent[0]
    assert url == "https://bigship.example.com/hooks/plivo-numbers"
    assert headers["X-Webhook-Event"] == "kyc.submitted"
    expected = "sha256=" + hmac.new(b"whsec-test", headers["X-Webhook-Timestamp"].encode() + b"." + body,
                                    hashlib.sha256).hexdigest()
    assert hmac.compare_digest(headers["X-Webhook-Signature"], expected)
    payload = json.loads(body)
    assert payload["type"] == "kyc.submitted" and payload["customer_id"] == cid
    assert payload["id"] == headers["X-Webhook-Id"] and payload["data"]["application"]["status"] == "submitted"
    with SessionLocal() as db:  # nothing left to send
        assert events.deliver_due(db) == 0

    # failing endpoint -> retried later, visible via the API, redeliverable
    created = client.post("/v1/webhook-events/test", headers=KEY).json()
    monkeypatch.setattr(events, "_post", lambda url, body, headers: httpx.Response(500, text="down"))
    with SessionLocal() as db:
        assert events.deliver_due(db) == 0
        evt = db.get(WebhookEvent, created["id"])
        nxt = evt.next_attempt_at if evt.next_attempt_at.tzinfo else evt.next_attempt_at.replace(tzinfo=timezone.utc)
        assert evt.status == "pending" and evt.attempts == 1 and "HTTP 500" in evt.last_error
        assert nxt > datetime.now(timezone.utc)
        assert events.deliver_due(db) == 0  # not due yet
    listed = client.get("/v1/webhook-events?status=pending", headers=KEY).json()
    assert created["id"] in [e["id"] for e in listed]
    client.post(f"/v1/webhook-events/{created['id']}/redeliver", headers=KEY)
    monkeypatch.setattr(events, "_post", ok_post)
    with SessionLocal() as db:
        assert events.deliver_due(db) == 1
    assert client.get("/v1/webhook-events?status=delivered&limit=5", headers=KEY).json()[0]["type"] == "webhook.test"


def test_single_document_used_for_every_slot(client, monkeypatch):
    """Plivo support: both slots are required, but the same file may fill both."""
    sent = {}
    mock = plivo_client.get_plivo()
    real = mock.create_compliance

    def spy(data, files):
        sent["docs"], sent["files"] = data["documents"], files
        return real(data, files)
    monkeypatch.setattr(mock, "create_compliance", spy)

    payload = {**kyc_payload(), "same_file_for_all": True}
    only_gst = {"file_mock-doc-gst": ("gst.pdf", PDF2, "application/pdf")}
    r = client.post("/v1/customers/bs_onefile/kyc", headers=KEY, data={"payload": json.dumps(payload)}, files=only_gst)
    assert r.status_code == 201, r.text
    assert [d["document_type_id"] for d in sent["docs"]] == ["mock-doc-registration", "mock-doc-gst"]
    assert [i for i, _ in sent["files"]] == [0, 1]                      # documents[0].file and documents[1].file
    assert all(f.content == PDF2 for _, f in sent["files"])            # both are the GST file

    # without the flag, a missing file is still an error
    r = client.post("/v1/customers/bs_onefile2/kyc", headers=KEY, data={"payload": json.dumps(kyc_payload())},
                    files=only_gst)
    assert r.status_code == 400 and "file upload" in r.json()["detail"]
