import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.db import SessionLocal, init_db
from app.routers import api, internal, webhooks
from app.services.compliance import sync_pending

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


@asynccontextmanager
async def lifespan(_: FastAPI):
    s = get_settings()
    init_db()
    if s.plivo_mock:
        log.warning("PLIVO_MOCK=true: no real Plivo requests will be made")
    if s.dev_mode:
        log.warning("DEV_MODE=true: /api/dev/token is enabled. Disable in production.")
    task = asyncio.create_task(_poll_compliance(s.compliance_poll_seconds)) if s.compliance_poll_seconds > 0 else None
    yield
    if task:
        task.cancel()


app = FastAPI(title="Plivo Numbers for Clients", version="1.0.0", lifespan=lifespan)
app.include_router(api)
app.include_router(webhooks)
app.include_router(internal)


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True}


app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="ui")
