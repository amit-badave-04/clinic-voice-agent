"""Browser web-call channel (WebRTC) — the free demo path for callers whose
phone plans block international dialing.

The caller-identity field is no longer free-form (anyone could assert any
patient's number and hear their appointments). Two modes instead:
  - persona: an allowlisted fictional demo patient (or a fresh caller); the
    server owns the persona→number mapping, and the fixed dev OTP shown on the
    page lets visitors experience the in-call verification flow.
  - real: the visitor's own number, proven by SMS OTP (Twilio Verify) BEFORE
    the call is minted; the resulting call starts pre-verified, so the agent
    won't ask again in-call.

Token minting is additionally gated by the kill switch, a daily web-call cap,
Cloudflare Turnstile (when configured), and the per-IP rate limit. The Retell
access token returned is itself single-use and expires in seconds if unused.
"""
import logging
import time
import uuid
from collections import defaultdict, deque
from datetime import timedelta
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from retell import AsyncRetell

from app.config import get_settings
from app.db.models import VerifiedSession
from app.db.session import SessionLocal
from app.services import guard, timeutils, verification
from app.services import sessions as sessions_svc
from app.services.phone import normalize_phone
from seed.arogya_data import DEMO_PATIENTS

log = logging.getLogger("web")
router = APIRouter()
settings = get_settings()

_retell: AsyncRetell | None = None


def get_retell() -> AsyncRetell:
    global _retell
    if _retell is None:
        _retell = AsyncRetell(api_key=settings.retell_api_key)
    return _retell


def _build_personas() -> dict[str, dict]:
    """Allowlisted demo identities, keyed by opaque ids. Numbers never leave
    the server; several patients on one number become one 'family line'."""
    by_phone: dict[str, list[str]] = {}
    for patient in DEMO_PATIENTS:
        by_phone.setdefault(patient["phone_e164"], []).append(patient["full_name"])
    personas: dict[str, dict] = {
        "new": {"label": "New caller — no patient record", "phone": None},
    }
    for index, (phone, names) in enumerate(sorted(by_phone.items()), start=1):
        label = (
            f"Family line — {' & '.join(names)} (shared number)"
            if len(names) > 1
            else f"Returning patient — {names[0]}"
        )
        personas[f"p{index}"] = {"label": label, "phone": phone}
    return personas


PERSONAS = _build_personas()

# Security headers for the browser demo page. The CSP constrains where SCRIPTS
# may load from (the actual injection surface — L3) while leaving connect-src
# open enough for the Retell WebRTC SDK's signalling/media. The SDK is pinned to
# an exact version in index.html.
_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://esm.sh https://challenges.cloudflare.com; "
        "style-src 'self' 'unsafe-inline'; "
        "connect-src 'self' https: wss:; "
        "frame-src https://challenges.cloudflare.com; "
        "img-src 'self' data:; "
        "media-src 'self' blob: mediastream:; "
        "base-uri 'none'; form-action 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}


class WebCallRequest(BaseModel):
    mode: str = "persona"  # persona | real
    persona: str | None = None
    phone: str | None = None  # real mode: the visitor's own number
    code: str | None = None  # real mode: the OTP they received
    turnstile_token: str | None = None


class VerifyStartRequest(BaseModel):
    phone: str
    turnstile_token: str | None = None


# In-process rate limit: each web call costs real Retell credit. Single
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
    # Behind Cloudflare the real address is CF-Connecting-IP; behind Fly's
    # proxy alone it is Fly-Client-IP.
    return (
        request.headers.get("cf-connecting-ip")
        or request.headers.get("fly-client-ip")
        or (request.client.host if request.client else "unknown")
    )


async def _gate(request: Request, turnstile_token: str | None) -> None:
    """Common admission checks for anything that mints cost or sends SMS."""
    async with SessionLocal() as session:
        open_, reason = await guard.web_channel_open(session)
    if not open_:
        raise HTTPException(status_code=503, detail=reason)
    if not await guard.verify_turnstile(turnstile_token, _client_ip(request)):
        raise HTTPException(status_code=403, detail="Bot check failed — please reload and retry.")
    if not _rate_limit_ok(_client_ip(request)):
        raise HTTPException(
            status_code=429,
            detail="Too many requests from this address — please wait a few minutes and try again.",
        )


@router.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    page = (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
    page = page.replace("__TURNSTILE_SITE_KEY__", settings.turnstile_site_key)
    return HTMLResponse(content=page, headers=_SECURITY_HEADERS)


@router.get("/demo-personas")
async def demo_personas() -> dict:
    return {
        "personas": [{"id": pid, "label": p["label"]} for pid, p in PERSONAS.items()],
        "dev_otp": settings.otp_dev_code,  # fictional patients only — lets the
        # demo exercise the in-call verification flow without SMS
    }


@router.post("/web-verify/start")
async def web_verify_start(body: VerifyStartRequest, request: Request) -> dict:
    """Send an OTP to the visitor's own number before a real-number call."""
    await _gate(request, body.turnstile_token)
    # Per-source-IP daily ceiling on OTP sends: the general _gate limit is a
    # 10-minute window; this bounds SMS-bomb / cost abuse across the day even
    # from a persistent IP (M1). Rotating IPs are additionally bounded by the
    # global per-day SMS ceiling enforced in verification.start_challenge.
    if not guard.rate_ok(
        f"webverify-ip:{_client_ip(request)}", settings.max_web_verify_per_ip_per_day, 86400
    ):
        raise HTTPException(status_code=429, detail="Too many code requests from this address today.")
    phone = normalize_phone(body.phone)
    if not phone:
        raise HTTPException(status_code=400, detail="Enter a valid number in international format.")
    if phone.startswith(settings.otp_dev_prefix):
        raise HTTPException(status_code=400, detail="That range is reserved for demo personas.")
    # Per-phone per-day challenge budget: the pseudo call-id scopes the
    # verification service's own 3-challenge limit.
    async with SessionLocal() as session:
        result = await verification.start_challenge(session, _verify_scope(phone), phone)
        await session.commit()
    if result["status"] != "code_sent":
        raise HTTPException(status_code=429, detail="Could not send a code — try again later.")
    return {"status": "code_sent"}


def _verify_scope(phone: str) -> str:
    return f"webverify:{phone}:{timeutils.now_local().date().isoformat()}"


@router.post("/create-web-call")
async def create_web_call(body: WebCallRequest, request: Request) -> dict:
    if not settings.retell_agent_id:
        raise HTTPException(status_code=503, detail="agent not configured yet")
    await _gate(request, body.turnstile_token)

    pre_verified = False
    if body.mode == "real":
        phone = normalize_phone(body.phone)
        if not phone or not body.code:
            raise HTTPException(status_code=400, detail="Number and SMS code are both required.")
        async with SessionLocal() as session:
            check = await verification.check_code(session, _verify_scope(phone), phone, body.code)
            await session.commit()
        if check["status"] != "verified":
            raise HTTPException(status_code=403, detail="That code is not correct.")
        pre_verified = True
    else:
        persona = PERSONAS.get(body.persona or "new")
        if persona is None:
            raise HTTPException(status_code=400, detail="Unknown persona.")
        phone = persona["phone"]

    async with SessionLocal() as session:
        variables = await sessions_svc.build_inbound_context(session, phone)
        await session.commit()
    web_call = await get_retell().call.create_web_call(
        agent_id=settings.retell_agent_id,
        retell_llm_dynamic_variables=variables,
        metadata={"channel": "web", "simulated_phone": phone or ""},
    )
    if pre_verified and phone:
        # Possession was just proven by SMS — carry it into the call so the
        # agent doesn't challenge again.
        async with SessionLocal() as session:
            session.add(
                VerifiedSession(
                    id=uuid.uuid4(),
                    call_id=web_call.call_id,
                    phone_e164=phone,
                    method="sms_otp",
                    expires_at=timeutils.now_utc()
                    + timedelta(minutes=settings.verified_session_ttl_minutes),
                )
            )
            await session.commit()
    return {"access_token": web_call.access_token, "call_id": web_call.call_id}
