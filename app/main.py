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


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    keepalive = asyncio.create_task(_db_keepalive())
    outbox = asyncio.create_task(outbox_worker_loop())
    yield
    keepalive.cancel()
    outbox.cancel()


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
