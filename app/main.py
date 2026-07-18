import asyncio
import contextlib
import logging

from fastapi import FastAPI
from sqlalchemy import text

from app.config import get_settings
from app.db.session import engine
from app.retell.webhooks import router as retell_router
from app.services.outbox import outbox_worker_loop
from app.tools.router import router as tools_router
from app.web.router import router as web_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("app")

settings = get_settings()


async def _db_keepalive() -> None:
    """Neon free tier autosuspends after ~5 min idle; a cold DB start during a live
    call is unacceptable, so ping every 60s to keep the compute warm."""
    while True:
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception as exc:  # noqa: BLE001 — keepalive must never die
            log.warning("db keepalive failed: %s", exc)
        await asyncio.sleep(60)


async def _reconcile_loop() -> None:
    """Periodic Cliniko drift reconciliation. Healthchecks.io (when configured)
    watchdogs this loop from outside — a dead loop misses its pings and pages."""
    import httpx

    from app.services import reconcile

    while True:
        try:
            summary = await reconcile.run_once()
            log.info("reconcile: %s", summary)
            if settings.healthchecks_reconcile_url:
                async with httpx.AsyncClient(timeout=6.0) as client:
                    await client.get(settings.healthchecks_reconcile_url)
        except Exception as exc:  # noqa: BLE001 — the loop must never die
            log.warning("reconcile pass failed: %s", exc)
        await asyncio.sleep(settings.reconcile_interval_minutes * 60)


if settings.sentry_dsn:
    import sentry_sdk

    sentry_sdk.init(dsn=settings.sentry_dsn, traces_sample_rate=0.1)
    log.info("sentry enabled")


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    keepalive = asyncio.create_task(_db_keepalive())
    outbox = asyncio.create_task(outbox_worker_loop())
    reconcile_task = asyncio.create_task(_reconcile_loop())
    yield
    keepalive.cancel()
    outbox.cancel()
    reconcile_task.cancel()


app = FastAPI(title="clinic-voice-agent", lifespan=lifespan)

app.include_router(retell_router, prefix="/retell", tags=["retell-webhooks"])
app.include_router(tools_router, prefix="/tools", tags=["agent-tools"])
app.include_router(web_router, tags=["web-call"])


@app.get("/healthz")
async def healthz():
    """Health = can we serve a tool call, which requires the database. A 503
    lets Fly's checks see (and eventually restart) a machine whose DB
    connectivity is wedged, instead of routing live calls into failures."""
    from fastapi.responses import JSONResponse

    try:
        async with asyncio.timeout(2):
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        return {"status": "ok", "db": True}
    except Exception:  # noqa: BLE001 — any failure means not ready
        return JSONResponse(status_code=503, content={"status": "degraded", "db": False})
