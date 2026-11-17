import json
import os
import tempfile

_db = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ.update(DATABASE_URL=f"sqlite:///{_db}", PLIVO_MOCK="true", JWT_SECRET="test-secret-0123456789-abcdefghijklmnop",
                  ADMIN_API_KEY="admin", DEV_MODE="true", COMPLIANCE_POLL_SECONDS="0",
                  ALLOWED_NUMBER_TYPES="local,tollfree", MAX_NUMBERS_PER_CLIENT="2", PRICE_MARKUP="1.5",
                  PUBLIC_BASE_URL="https://numbers.example.com", PLIVO_USE_SUBACCOUNTS="true",
                  KYC_DOCUMENTS_MODE="all")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import plivo_client  # noqa: E402
from app.auth import mint_client_token, webhook_token  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models import ComplianceApplication  # noqa: E402
from app.services import compliance as kyc  # noqa: E402

PDF = b"%PDF-1.4\n%registration certificate\n"
PDF2 = b"%PDF-1.4\n%gst certificate\n"
ADMIN = {"X-Admin-Key": "admin"}
FILES = {"file_mock-doc-registration": ("coi.pdf", PDF, "application/pdf"),
         "file_mock-doc-gst": ("gst.pdf", PDF2, "application/pdf")}


@pytest.fixture()
def client(monkeypatch):
    plivo_client.set_plivo(plivo_client.MockPlivoClient())
    monkeypatch.setattr(kyc, "SYNC_MIN_INTERVAL", 0)
    with TestClient(app) as c:
        yield c


def auth(cid):
    return {"Authorization": f"Bearer {mint_client_token(cid, cid.title())}"}


def business_payload(name="Acme Technologies Pvt Ltd", number_type="local", reg="U72200KA2020PTC123456"):
    return {
        "number_type": number_type, "user_type": "business",
        "end_user": {"name": name, "registration_number": reg, "email": "kyc@acme.in",
                     "address_line1": "1 MG Road", "city": "Bangalore", "state": "Karnataka", "postal_code": "560001"},
        "documents": [{"document_type_id": "mock-doc-registration", "data_fields": {"business_name": name}},
                      {"document_type_id": "mock-doc-gst"}],
    }


def submit(c, cid, payload, files=None):
    return c.post("/api/compliance/applications", headers=auth(cid),
                  data={"payload": json.dumps(payload)}, files=FILES if files is None else files)


def approve_all(monkeypatch):
    monkeypatch.setattr(plivo_client, "MOCK_ACCEPT_AFTER", 0)


def test_requires_auth(client):
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/me", headers={"Authorization": "Bearer junk"}).status_code == 401
    assert client.get("/internal/numbers").status_code == 401


def test_requirements(client):
    r = client.get("/api/compliance/requirements?number_type=local&user_type=business", headers=auth("c1"))
    assert r.status_code == 200
    assert {d["name"] for d in r.json()["document_types"]} == {"Registration Certificate", "GST Certificate"}
    assert client.get("/api/compliance/requirements?number_type=mobile&user_type=business",
                      headers=auth("c1")).status_code == 400  # not in ALLOWED_NUMBER_TYPES
    assert client.get("/api/compliance/requirements?number_type=local&user_type=individual",
                      headers=auth("c1")).status_code == 400  # India: businesses only


@pytest.mark.parametrize("change,expect", [
    (lambda p: p["end_user"].update(registration_number="ABCDE1234F"), "PAN alone is not accepted"),
    (lambda p: p["end_user"].update(registration_number="12345"), "must be a GSTIN"),
    (lambda p: p.update(user_type="individual"), "India-registered businesses"),
    (lambda p: p["documents"].pop(), "Missing: GST Certificate"),
    (lambda p: p.update(documents=[]), "All of these documents are required"),
    (lambda p: p["documents"].append({"document_type_id": "mock-doc-gst"}), "submitted twice"),
    (lambda p: p["documents"][0]["data_fields"].update(business_name="Acme Tech"), "must match"),
])
def test_kyc_validation(client, change, expect):
    p = business_payload()
    change(p)
    r = submit(client, "v1", p)
    assert r.status_code == 400 and expect in r.json()["detail"], r.text


def test_kyc_file_validation(client):
    r = submit(client, "v1", business_payload(), files={**FILES, "file_mock-doc-gst": ("g.pdf", b"not a pdf", "application/pdf")})
    assert r.status_code == 400 and "PDF, JPEG or PNG" in r.json()["detail"]
    r = submit(client, "v1", business_payload(), files={"file_mock-doc-registration": FILES["file_mock-doc-registration"]})
    assert r.status_code == 400 and "file upload" in r.json()["detail"]
    # Plivo support: a business with a single document may upload the same file in both slots.
    r = submit(client, "v1", business_payload(), files={**FILES, "file_mock-doc-gst": ("gst.pdf", PDF, "application/pdf")})
    assert r.status_code == 201, r.text


@pytest.mark.parametrize("reg", ["29ABCDE1234F1Z5", "udyam-ka-01-0000001"])
def test_other_registration_numbers(client, reg):
    r = submit(client, f"ok-{reg[:5]}", business_payload(reg=reg))
    assert r.status_code == 201, r.text
    assert r.json()["end_user"]["registration_number"] == reg.upper()


def test_business_name_filled_from_end_user(client):
    p = business_payload()
    p["documents"][0]["data_fields"] = {}
    r = submit(client, "autoname", p)
    assert r.status_code == 201, r.text
    assert r.json()["documents"][0]["data_fields"]["business_name"] == "Acme Technologies Pvt Ltd"


def test_one_document_mode(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "kyc_documents_mode", "one")
    p = business_payload()
    assert submit(client, "one1", p).status_code == 400  # two docs not allowed in "one" mode
    p["documents"].pop()
    r = submit(client, "one1", p, files={"file_mock-doc-registration": FILES["file_mock-doc-registration"]})
    assert r.status_code == 201, r.text


def test_full_flow(client, monkeypatch):
    h = auth("acme")
    # cannot search before KYC
    assert client.get("/api/numbers/search?number_type=local", headers=h).status_code == 403

    r = submit(client, "acme", business_payload())
    assert r.status_code == 201, r.text
    app_id = r.json()["id"]
    assert r.json()["status"] == "submitted"
    # filed under a dedicated Plivo subaccount for this client
    apps = client.get("/internal/applications?client_id=acme", headers=ADMIN).json()
    sub = apps[0]["plivo_subaccount_id"]
    assert sub and sub.startswith("SA")
    # duplicate open application blocked
    assert submit(client, "acme", business_payload()).status_code == 409
    # still under review
    assert client.get("/api/numbers/search?number_type=local", headers=h).status_code == 403

    approve_all(monkeypatch)
    # Plivo callback triggers a re-sync; bad token rejected
    assert client.post(f"/webhooks/plivo/compliance/{app_id}?token=nope").status_code == 403
    r = client.post(f"/webhooks/plivo/compliance/{app_id}?token={webhook_token(app_id)}")
    assert r.json()["status"] == "accepted"

    res = client.get("/api/numbers/search?number_type=local", headers=h).json()["numbers"]
    assert res and res[0]["monthly_price"] == 2.5 * 1.5

    # cannot buy a number you weren't offered; another client can't buy your offer either
    assert client.post("/api/numbers", headers=h, json={"number": "919999999999"}).status_code == 400
    assert client.post("/api/numbers", headers=auth("evil"), json={"number": res[0]["number"]}).status_code == 400

    r = client.post("/api/numbers", headers=h, json={"number": res[0]["number"]})
    assert r.status_code == 201, r.text  # mock enforces: bought into the same subaccount as the KYC
    n1 = r.json()
    assert n1["status"] == "active"
    assert client.post("/api/numbers", headers=h, json={"number": res[1]["number"]}).status_code == 201
    # limit (MAX_NUMBERS_PER_CLIENT=2)
    assert client.post("/api/numbers", headers=h, json={"number": res[2]["number"]}).status_code == 409

    # tollfree needs its own KYC
    assert client.get("/api/numbers/search?number_type=tollfree", headers=h).status_code == 403

    # dialer view
    ids = client.get("/internal/clients/acme/caller-ids", headers=ADMIN).json()
    assert ids["plivo_subaccount_id"] == sub
    assert {i["number"] for i in ids["numbers"]} == {res[0]["number"], res[1]["number"]}
    assert client.get(f"/internal/caller-ids/validate?client_id=acme&number=%2B{n1['number']}", headers=ADMIN).json()["allowed"]
    assert not client.get(f"/internal/caller-ids/validate?client_id=other&number={n1['number']}", headers=ADMIN).json()["allowed"]

    # rename + other client can't touch it
    assert client.patch(f"/api/numbers/{n1['id']}", headers=h, json={"alias": "Sales"}).json()["alias"] == "Sales"
    assert client.delete(f"/api/numbers/{n1['id']}", headers=auth("evil")).status_code == 404

    # release
    assert client.delete(f"/api/numbers/{n1['id']}", headers=h).json()["status"] == "released"
    assert len(client.get("/api/numbers", headers=h).json()) == 1
    assert not client.get(f"/internal/caller-ids/validate?client_id=acme&number={n1['number']}", headers=ADMIN).json()["allowed"]

    ledger = client.get("/internal/ledger?client_id=acme", headers=ADMIN).json()
    assert {e["kind"] for e in ledger} == {"setup", "monthly_rental"} and len(ledger) == 4
    assert client.post("/internal/billing/accrue", headers=ADMIN).json()["entries_created"] == 0

    # UCC suspension: dialer must stop using the numbers, and a fresh KYC can't bypass it
    with SessionLocal() as db:
        db.get(ComplianceApplication, app_id).status = "suspended"
        db.commit()
    monkeypatch.setattr(kyc, "sync_application", lambda db, a, **kw: a)
    assert client.get("/internal/clients/acme/caller-ids", headers=ADMIN).json()["numbers"] == []
    r = submit(client, "acme", business_payload())
    assert r.status_code == 409 and "UCC" in r.json()["detail"]


def test_reject_and_resubmit(client, monkeypatch):
    h = auth("bad")
    r = submit(client, "bad", business_payload(name="Reject Me Pvt Ltd"))
    app_id = r.json()["id"]
    approve_all(monkeypatch)
    a = client.get(f"/api/compliance/applications/{app_id}", headers=h).json()
    assert a["status"] == "rejected" and a["rejection_reason"]

    r = client.post(f"/api/compliance/applications/{app_id}/resubmit", headers=h,
                    data={"payload": json.dumps(business_payload(name="Good Name Pvt Ltd"))}, files=FILES)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "submitted"
    assert client.get(f"/api/compliance/applications/{app_id}", headers=h).json()["status"] == "accepted"
    # other clients can't see it
    assert client.get(f"/api/compliance/applications/{app_id}", headers=auth("x")).status_code == 404


def test_sync_is_throttled(client, monkeypatch):
    monkeypatch.setattr(kyc, "SYNC_MIN_INTERVAL", 60)
    app_id = submit(client, "thr", business_payload()).json()["id"]
    client.get(f"/api/compliance/applications/{app_id}", headers=auth("thr"))  # first sync
    approve_all(monkeypatch)
    # page views within the window don't hit Plivo again...
    assert client.get(f"/api/compliance/applications/{app_id}", headers=auth("thr")).json()["status"] == "submitted"
    # ...but a Plivo callback always does
    r = client.post(f"/webhooks/plivo/compliance/{app_id}?token={webhook_token(app_id)}")
    assert r.json()["status"] == "accepted"


def test_main_account_mode(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "plivo_use_subaccounts", False)
    approve_all(monkeypatch)
    assert submit(client, "solo", business_payload()).status_code == 201
    apps = client.get("/internal/applications?client_id=solo", headers=ADMIN).json()
    assert apps[0]["plivo_subaccount_id"] is None
    n = client.get("/api/numbers/search?number_type=local", headers=auth("solo")).json()["numbers"][0]
    assert client.post("/api/numbers", headers=auth("solo"), json={"number": n["number"]}).status_code == 201


def test_multipart_file_indexes_match_documents():
    """documents[i].file must line up with data.documents[i]."""
    from app.plivo_client import PlivoClient, UploadFile

    reqs = {"document_types": [
        {"document_type_id": "a", "name": "A", "proof_required": False, "required_fields": []},
        {"document_type_id": "b", "name": "B", "proof_required": True, "required_fields": []},
    ]}
    docs, files, _ = kyc.validate_documents(reqs, [{"document_type_id": "a"}, {"document_type_id": "b"}],
                                            {"b": UploadFile("b.pdf", PDF, "application/pdf")})
    form, parts = PlivoClient._multipart({"documents": docs}, files)
    assert [p[0] for p in parts] == ["documents[1].file"]
    assert json.loads(form["data"])["documents"][1]["document_type_id"] == "b"


def _approved_client(client, monkeypatch, cid):
    approve_all(monkeypatch)
    assert submit(client, cid, business_payload()).status_code == 201
    return auth(cid)


def test_search_prefix_and_restricted_numbers(client, monkeypatch):
    h = _approved_client(client, monkeypatch, "srch")
    res = client.get("/api/numbers/search?number_type=local&pattern=80", headers=h).json()["numbers"]
    assert res and all(n["number"].startswith("9180") for n in res)

    mock = plivo_client.get_plivo()
    real = mock.search_numbers

    def with_restricted(**kw):
        data = real(**kw)
        data["objects"][0]["restriction"] = "city-address"
        return data
    monkeypatch.setattr(mock, "search_numbers", with_restricted)
    res2 = client.get("/api/numbers/search?number_type=local&pattern=80", headers=h).json()["numbers"]
    assert len(res2) == len(res) - 1  # restricted number not offered


@pytest.mark.parametrize("resp,expect_code,expect_status", [
    ({"status": "fulfilled", "numbers": [{"status": "Success"}]}, 201, "active"),
    ({"status": "pending", "numbers": [{"status": "Pending"}]}, 201, "pending_activation"),
    ({"status": "failed", "numbers": [{"status": "Failed"}]}, 502, "failed"),
])
def test_buy_response_handling(client, monkeypatch, resp, expect_code, expect_status):
    h = _approved_client(client, monkeypatch, f"buy-{expect_status}")
    prefix = {"active": "8071", "pending_activation": "8072", "failed": "8073"}[expect_status]  # unique numbers
    n = client.get(f"/api/numbers/search?number_type=local&pattern={prefix}", headers=h).json()["numbers"][0]["number"]
    mock = plivo_client.get_plivo()
    monkeypatch.setattr(mock, "buy_number", lambda number, *a, **k: {
        **resp, "numbers": [{**resp["numbers"][0], "number": number}]})
    r = client.post("/api/numbers", headers=h, json={"number": n})
    assert r.status_code == expect_code, r.text
    rows = client.get(f"/internal/numbers?client_id=buy-{expect_status}", headers=ADMIN).json()
    assert rows[0]["status"] == expect_status
    ids = client.get(f"/internal/clients/buy-{expect_status}/caller-ids", headers=ADMIN).json()["numbers"]
    assert [i["number"] for i in ids] == ([n] if expect_status == "active" else [])


def test_city_dropdown_and_aliases(client, monkeypatch):
    h = _approved_client(client, monkeypatch, "city")
    assert client.get("/api/me", headers=h).json()["cities"] == ["Bangalore", "Mumbai"]

    seen = {}
    real = plivo_client.get_plivo().search_numbers

    def spy(**kw):
        seen.update(kw)
        return real(**kw)
    monkeypatch.setattr(plivo_client.get_plivo(), "search_numbers", spy)

    for given, sent in [("Bengaluru", "Bangalore"), ("bombay", "Mumbai"), ("MUMBAI", "Mumbai")]:
        r = client.get(f"/api/numbers/search?number_type=local&city={given}", headers=h)
        assert r.status_code == 200, r.text
        assert seen["city"] == sent
        assert all(n["city"] == sent for n in r.json()["numbers"])
    r = client.get("/api/numbers/search?number_type=local&city=Delhi", headers=h)
    assert r.status_code == 400 and "Bangalore, Mumbai" in r.json()["detail"]
    client.get("/api/numbers/search?number_type=local", headers=h)
    assert seen["city"] is None  # "Any city"
