"""Browser web-call channel (WebRTC) — the fallback for evaluators whose phone
plans block international dialing. Functionally equivalent to a PSTN call: an
optional phone-number field simulates caller ID so returning-patient /
dropped-call flows are testable from the browser too."""
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
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


@router.get("/", response_class=HTMLResponse)
async def index() -> str:
    page = Path(__file__).parent / "static" / "index.html"
    return page.read_text(encoding="utf-8")


@router.post("/create-web-call")
async def create_web_call(body: WebCallRequest) -> dict:
    if not settings.retell_agent_id:
        raise HTTPException(status_code=503, detail="agent not configured yet")
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
