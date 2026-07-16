"""Browser web-call channel (WebRTC) — the fallback for callers whose phone
plans block international dialing. Functionally equivalent to a PSTN call: an
optional phone-number field simulates caller ID so returning-patient /
dropped-call flows are testable from the browser too."""
import logging
import time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from retell import AsyncRetell

from app.config import get_settings
from app.db.session import SessionLocal
from app.services import sessions as sessions_svc
from app.services.phone import normalize_phone

log = logging.getLogger("web")
router = APIRouter()
settings = get_settings()

_retell: AsyncRetell | None = None


def get_retell() -> AsyncRetell:
    global _retell
    if _retell is None:
        _retell = AsyncRetell(api_key=settings.retell_api_key)
    return _retell


class WebCallRequest(BaseModel):
    phone: str | None = None  # optional caller-ID simulation


# In-process rate limit: each web call costs real Retell credit, and the phone
# field asserts caller identity — both are abusable without a cap. Single
# machine (fly.toml min_machines_running=1), so process-local state suffices.
RATE_LIMIT_CALLS = 6
RATE_LIMIT_WINDOW_SECONDS = 600
_rate: dict[str, deque] = defaultdict(deque)


def _rate_limit_ok(client_ip: str) -> bool:
    now = time.monotonic()
    bucket = _rate[client_ip]
    while bucket and now - bucket[0] > RATE_LIMIT_WINDOW_SECONDS:
        bucket.popleft()
    if len(bucket) >= RATE_LIMIT_CALLS:
        return False
    bucket.append(now)
    return True


def _client_ip(request: Request) -> str:
    # Fly's proxy provides the real client address in Fly-Client-IP.
    return request.headers.get("fly-client-ip") or (request.client.host if request.client else "unknown")


@router.get("/", response_class=HTMLResponse)
async def index() -> str:
    page = Path(__file__).parent / "static" / "index.html"
    return page.read_text(encoding="utf-8")


@router.post("/create-web-call")
async def create_web_call(body: WebCallRequest, request: Request) -> dict:
    if not settings.retell_agent_id:
        raise HTTPException(status_code=503, detail="agent not configured yet")
    if not _rate_limit_ok(_client_ip(request)):
        raise HTTPException(
            status_code=429,
            detail="Too many calls from this address — please wait a few minutes and try again.",
        )
    phone = normalize_phone(body.phone)
    async with SessionLocal() as session:
        variables = await sessions_svc.build_inbound_context(session, phone)
        await session.commit()
    web_call = await get_retell().call.create_web_call(
        agent_id=settings.retell_agent_id,
        retell_llm_dynamic_variables=variables,
        metadata={"channel": "web", "simulated_phone": phone or ""},
    )
    return {"access_token": web_call.access_token, "call_id": web_call.call_id}
