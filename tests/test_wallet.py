"""Wallet billing against a fake Bigship wallet API (no network)."""
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.config import get_settings
from app.db import SessionLocal
from app.models import PhoneNumber, WalletCharge
from app.services import wallet
from tests.test_integration import KEY, approved, client, events_of  # noqa: F401  (fixture)

ADMIN = {"X-Admin-Key": "admin"}


class FakeBigship:
    def __init__(self):
        self.balances: dict[int, float] = {}
        self.deductions: list[dict] = []
        self.logins = 0
        self.deduct_mode = "ok"  # ok | timeout | refuse | connect

    def __call__(self, url, body, headers):
        req = httpx.Request("POST", url)
        if url.endswith("/api/get-auth-token"):
            self.logins += 1
            return httpx.Response(200, json={"data": {"token": "tok", "expires_in": "2099-01-01T00:00:00Z"}}, request=req)
        assert headers.get("x-api-key") == "tok"
        cid = body["customer_id"]
        if url.endswith("/api/shared/get-wallet-balance"):
            if cid not in self.balances:
                return httpx.Response(404, json={"message": "wallet not found"}, request=req)
            return httpx.Response(200, json={"data": {"wallet_balance": self.balances[cid]}}, request=req)
        if url.endswith("/api/shared/ai-wallet-deduction"):
            if self.deduct_mode == "connect":
                raise httpx.ConnectError("down", request=req)
            if self.deduct_mode == "timeout":
                self.balances[cid] -= body["amount"]  # it went through, but we never hear back
                raise httpx.ReadTimeout("slow", request=req)
            if self.deduct_mode == "refuse":
                return httpx.Response(400, json={"message": "bad mobile"}, request=req)
            self.balances[cid] = round(self.balances[cid] - body["amount"], 2)
            self.deductions.append(body)
            return httpx.Response(200, json={"message": "ok"}, request=req)
        raise AssertionError(url)


@pytest.fixture()
def bigship(monkeypatch):
    s = get_settings()
    for k, v in dict(wallet_billing_enabled=True, bigship_wallet_email="w@x", bigship_wallet_password="p",
                     number_monthly_price_inr=300.0, number_gst_percent=18.0, billing_period_days=30,
                     billing_grace_days=3, billing_release_after_days=0).items():
        monkeypatch.setattr(s, k, v)
    fake = FakeBigship()
    monkeypatch.setattr(wallet, "_http_post", fake)
    wallet._token.update(value=None, expires_at=None)
    return fake


_picked: dict[str, str] = {}


def buy_one(c, cid, key="order-1"):
    """Buy one number for `cid` (a different one per customer; a repeat call re-sends the same order)."""
    nums = [n for city in ("Bangalore", "Mumbai")
            for n in c.get(f"/v1/customers/{cid}/numbers/available?city={city}", headers=KEY).json()["numbers"]]
    if cid not in _picked:
        taken = set(_picked.values())
        _picked[cid] = next(n["e164"] for n in nums if n["e164"] not in taken)
    return c.post(f"/v1/customers/{cid}/numbers", headers={**KEY, "Idempotency-Key": key},
                  json={"number": _picked[cid]})


def shift_time(monkeypatch, days):
    t = datetime.now(timezone.utc) + timedelta(days=days)
    monkeypatch.setattr(wallet, "now", lambda: t)


def test_price():
    assert wallet.price_paise() == (30000, 5400, 35400)


def test_purchase_refused_when_balance_low(client, bigship, monkeypatch):
    approved(client, monkeypatch, "700001")
    bigship.balances[700001] = 353.99
    r = buy_one(client, "700001")
    assert r.status_code == 402 and "354.00" in r.json()["detail"]
    assert client.get("/v1/customers/700001/numbers", headers=KEY).json() == []
    assert bigship.deductions == []


def test_purchase_refused_when_wallet_unreadable(client, bigship, monkeypatch):
    approved(client, monkeypatch, "700002")  # no wallet at Bigship -> 404 on balance
    r = buy_one(client, "700002")
    assert r.status_code == 503
    assert client.get("/v1/customers/700002/numbers", headers=KEY).json() == []


def test_purchase_charges_first_month_and_renews(client, bigship, monkeypatch):
    approved(client, monkeypatch, "700003")
    bigship.balances[700003] = 1000.0
    r = buy_one(client, "700003")
    assert r.status_code == 201, r.text
    n = r.json()
    assert n["price_inr"]["total"] == 354.0
    assert len(bigship.deductions) == 1
    d = bigship.deductions[0]
    assert d == {"customer_id": 700003, "amount": 354.0, "mobile": n["number"][2:]}
    assert bigship.balances[700003] == 646.0
    charges = client.get("/v1/customers/700003/charges", headers=KEY).json()
    assert [c["status"] for c in charges] == ["confirmed"] and charges[0]["gst"] == 54.0
    assert "billing.charged" in events_of("700003")
    paid_until = client.get(f"/v1/customers/700003/numbers/{n['id']}", headers=KEY).json()["paid_until"]
    assert paid_until is not None

    # Same day: nothing more to charge. Retrying the purchase with the same key doesn't charge again.
    assert client.post("/internal/billing/run", headers=ADMIN).status_code == 200
    assert len(client.get("/v1/customers/700003/charges", headers=KEY).json()) == 1
    assert buy_one(client, "700003").status_code == 200
    assert len(bigship.deductions) == 1

    # 30 days later: one renewal, charged once even if the loop runs twice.
    shift_time(monkeypatch, 30)
    with SessionLocal() as db:
        wallet.run_billing(db)
        wallet.run_billing(db)
    assert [c["status"] for c in client.get("/v1/customers/700003/charges", headers=KEY).json()] == ["confirmed"] * 2
    assert len(bigship.deductions) == 2 and bigship.balances[700003] == 292.0


def test_unpaid_renewal_goes_overdue_then_recovers(client, bigship, monkeypatch):
    approved(client, monkeypatch, "700004")
    bigship.balances[700004] = 400.0
    n = buy_one(client, "700004").json()
    assert bigship.balances[700004] == 46.0
    caller_ids = lambda: client.get("/v1/customers/700004/caller-ids", headers=KEY).json()["numbers"]  # noqa: E731
    assert len(caller_ids()) == 1

    shift_time(monkeypatch, 30)
    with SessionLocal() as db:
        wallet.run_billing(db)
    ch = client.get("/v1/customers/700004/charges", headers=KEY).json()[0]
    assert ch["status"] == "insufficient"
    assert "billing.payment_due" in events_of("700004")
    assert len(caller_ids()) == 1  # still within the grace period

    shift_time(monkeypatch, 34)  # 4 days unpaid > 3-day grace
    with SessionLocal() as db:
        wallet.run_billing(db)
    assert client.get(f"/v1/customers/700004/numbers/{n['id']}", headers=KEY).json()["billing_status"] == "overdue"
    assert caller_ids() == []
    assert "number.suspended_nonpayment" in events_of("700004")

    bigship.balances[700004] = 500.0  # seller recharges; the daily retry picks it up
    shift_time(monkeypatch, 35)
    with SessionLocal() as db:
        wallet.run_billing(db)
    assert client.get(f"/v1/customers/700004/numbers/{n['id']}", headers=KEY).json()["billing_status"] == "ok"
    assert len(caller_ids()) == 1


def test_auto_release_when_enabled(client, bigship, monkeypatch):
    monkeypatch.setattr(get_settings(), "billing_release_after_days", 7)
    approved(client, monkeypatch, "700005")
    bigship.balances[700005] = 354.0
    n = buy_one(client, "700005").json()
    shift_time(monkeypatch, 38)  # renewal due at day 30, unpaid for 8 days
    with SessionLocal() as db:
        wallet.run_billing(db)  # queues + marks insufficient
        wallet.run_billing(db)
    with SessionLocal() as db:
        assert db.get(PhoneNumber, n["id"]).status == "released"
    assert "number.released_nonpayment" in events_of("700005")


def test_ambiguous_deduction_is_not_retried(client, bigship, monkeypatch):
    approved(client, monkeypatch, "700006")
    bigship.balances[700006] = 1000.0
    bigship.deduct_mode = "timeout"
    assert buy_one(client, "700006").status_code == 201  # the number is still bought
    ch = client.get("/v1/customers/700006/charges", headers=KEY).json()[0]
    assert ch["status"] == "unknown"
    assert "billing.needs_review" in events_of("700006")
    with SessionLocal() as db:
        wallet.run_billing(db)
    assert client.get("/v1/customers/700006/charges", headers=KEY).json()[0]["status"] == "unknown"
    assert bigship.balances[700006] == 646.0  # deducted exactly once

    # A human checks the wallet statement and confirms it.
    listed = client.get("/internal/billing/charges?status=unknown", headers=ADMIN).json()
    assert [c["id"] for c in listed] == [ch["id"]]
    r = client.post(f"/internal/billing/charges/{ch['id']}/resolve", headers=ADMIN, json={"status": "confirmed"})
    assert r.json()["status"] == "confirmed"


def test_connection_error_is_retried(client, bigship, monkeypatch):
    approved(client, monkeypatch, "700007")
    bigship.balances[700007] = 1000.0
    bigship.deduct_mode = "connect"
    assert buy_one(client, "700007").status_code == 201
    assert client.get("/v1/customers/700007/charges", headers=KEY).json()[0]["status"] == "pending"
    bigship.deduct_mode = "ok"
    shift_time(monkeypatch, 0.01)  # past the 30 s backoff
    with SessionLocal() as db:
        wallet.run_billing(db)
    assert client.get("/v1/customers/700007/charges", headers=KEY).json()[0]["status"] == "confirmed"
    assert len(bigship.deductions) == 1


def test_refused_deduction_needs_review(client, bigship, monkeypatch):
    approved(client, monkeypatch, "700008")
    bigship.balances[700008] = 1000.0
    bigship.deduct_mode = "refuse"
    buy_one(client, "700008")
    ch = client.get("/v1/customers/700008/charges", headers=KEY).json()[0]
    assert ch["status"] == "failed" and "bad mobile" in ch["last_error"]


def test_billing_off_makes_no_wallet_calls(client, monkeypatch):
    calls = []
    monkeypatch.setattr(wallet, "_http_post", lambda *a: calls.append(a))
    approved(client, monkeypatch, "700009")
    assert buy_one(client, "700009").status_code == 201
    assert calls == []
    with SessionLocal() as db:
        assert db.query(WalletCharge).filter(WalletCharge.client_id == "700009").count() == 0


def test_release_stops_billing_immediately(client, bigship, monkeypatch):
    approved(client, monkeypatch, "700010")
    bigship.balances[700010] = 354.0
    n = buy_one(client, "700010").json()
    assert len(bigship.deductions) == 1

    shift_time(monkeypatch, 30)  # renewal due, wallet empty -> waiting as "insufficient"
    with SessionLocal() as db:
        wallet.run_billing(db)
    assert client.get("/v1/customers/700010/charges", headers=KEY).json()[0]["status"] == "insufficient"

    # Seller releases the number, then tops up. The waiting renewal must never be deducted.
    assert client.delete(f"/v1/customers/700010/numbers/{n['id']}", headers=KEY).json()["status"] == "released"
    assert client.get("/v1/customers/700010/charges", headers=KEY).json()[0]["status"] == "cancelled"
    bigship.balances[700010] = 5000.0
    for days in (31, 61, 91):
        shift_time(monkeypatch, days)
        with SessionLocal() as db:
            wallet.run_billing(db)
    assert len(bigship.deductions) == 1 and bigship.balances[700010] == 5000.0
    assert [c["status"] for c in client.get("/v1/customers/700010/charges", headers=KEY).json()] == ["cancelled", "confirmed"]


def test_charge_for_released_number_is_skipped_at_send_time(client, bigship, monkeypatch):
    approved(client, monkeypatch, "700011")
    bigship.balances[700011] = 1000.0
    n = buy_one(client, "700011").json()
    with SessionLocal() as db:  # simulate a charge queued just before the number was released elsewhere
        row = db.get(PhoneNumber, n["id"])
        ch = wallet.queue_charge(db, row, datetime.now(timezone.utc) + timedelta(days=30))
        row.status = "released"
        db.commit()
        assert wallet.send_charge(db, ch) == "cancelled"
    assert len(bigship.deductions) == 1
