import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.db import SessionLocal, init_db
from app.integration import v1
from app.routers import api, internal, webhooks
from app.services.compliance import sync_pending
from app.services.numbers import sync_with_plivo
from app.services.events import deliver_due
from app.services.wallet import run_billing

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")


async def _poll_compliance(interval: int) -> None:
    """Fallback for missed/absent Plivo callbacks: refresh every 'submitted' application."""
    while True:
        await asyncio.sleep(interval)
        try:
            def run():
                with SessionLocal() as db:
                    return sync_pending(db)
            n = await asyncio.to_thread(run)
            if n:
                log.info("compliance poller refreshed %d pending application(s)", n)
        except Exception:  # keep the poller alive
            log.exception("compliance poller failed")


async def _sync_numbers(interval: int) -> None:
    """Mark numbers deleted directly in the Plivo console as released."""
    while True:
        try:
            def run():
                with SessionLocal() as db:
                    return sync_with_plivo(db)
            n = await asyncio.to_thread(run)
            if n:
                log.info("number sync released %d number(s) no longer on Plivo", n)
        except Exception:  # keep the loop alive
            log.exception("number sync failed")
        await asyncio.sleep(interval)


async def _deliver_webhooks(interval: int) -> None:
    """Send queued webhook events (with retries) to WEBHOOK_URL."""
    while True:
        await asyncio.sleep(interval)
        try:
            def run():
                with SessionLocal() as db:
                    return deliver_due(db)
            await asyncio.to_thread(run)
        except Exception:
            log.exception("webhook delivery loop failed")


async def _wallet_billing(interval: int) -> None:
    """Queue monthly rentals and deduct them from sellers' Bigship wallets."""
    while True:
        await asyncio.sleep(interval)
        try:
            def run():
                with SessionLocal() as db:
                    return run_billing(db)
            result = await asyncio.to_thread(run)
            if result["queued"] or result["sent"]:
                log.info("wallet billing: %s", result)
        except Exception:
            log.exception("wallet billing loop failed")


@asynccontextmanager
async def lifespan(_: FastAPI):
    s = get_settings()
    init_db()
    if s.plivo_mock:
        log.warning("PLIVO_MOCK=true: no real Plivo requests will be made")
    if s.dev_mode:
        log.warning("DEV_MODE=true: /api/dev/token is enabled. Disable in production.")
    if s.webhook_url and not s.webhook_secret:
        raise RuntimeError("WEBHOOK_URL is set but WEBHOOK_SECRET is empty; webhooks must be signed")
    if s.wallet_billing_enabled:
        if not (s.bigship_wallet_email and s.bigship_wallet_password):
            raise RuntimeError("WALLET_BILLING_ENABLED=true needs BIGSHIP_WALLET_EMAIL and BIGSHIP_WALLET_PASSWORD")
        log.info("wallet billing on: ₹%g + %g%% GST per number every %d days (grace %d days, auto-release %s)",
                 s.number_monthly_price_inr, s.number_gst_percent, s.billing_period_days, s.billing_grace_days,
                 f"after {s.billing_release_after_days} days" if s.billing_release_after_days else "off")
    if not s.integration_api_key:
        log.warning("INTEGRATION_API_KEY is empty: the /v1 integration API is disabled")
    tasks = []
    if s.compliance_poll_seconds > 0:
        tasks.append(asyncio.create_task(_poll_compliance(s.compliance_poll_seconds)))
    if s.number_sync_seconds > 0 and not s.plivo_mock:
        tasks.append(asyncio.create_task(_sync_numbers(s.number_sync_seconds)))
    if s.webhook_url:
        tasks.append(asyncio.create_task(_deliver_webhooks(s.webhook_poll_seconds)))
    if s.wallet_billing_enabled:
        tasks.append(asyncio.create_task(_wallet_billing(s.wallet_poll_seconds)))
    yield
    for task in tasks:
        task.cancel()


app = FastAPI(title="Plivo Numbers for Clients", version="1.1.0", lifespan=lifespan,
              description="Integration API for your backend: see the **Integration v1** section and docs/INTEGRATION.md.")
if get_settings().cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=get_settings().cors_origins,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["X-API-Key", "X-Admin-Key", "Authorization", "Content-Type", "Idempotency-Key"],
        max_age=600,
    )

app.include_router(v1)
app.include_router(api)
app.include_router(webhooks)
app.include_router(internal)


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True}


app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="ui")
