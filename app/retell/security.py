"""Webhook authenticity.

Every request Retell sends (webhooks AND custom-function tool calls) carries an
X-Retell-Signature computed over the raw request body with your API key.
Verification uses the SDK helper against the RAW body — never re-serialized JSON.

The eval harness may also call tool endpoints directly; it authenticates with
the X-Tool-Secret shared-secret header instead.
"""
import hmac
import logging

from fastapi import HTTPException, Request
from retell.lib.webhook_auth import verify as retell_verify

from app.config import get_settings

log = logging.getLogger("retell.security")
settings = get_settings()

# Legitimate Retell payloads (webhooks, tool calls with transcripts) stay well
# under this; anything bigger is garbage and gets rejected before the HMAC
# work. Retell's signature scheme carries no timestamp, so replay protection
# stays where it already lives: the idempotency-key layer on mutating tools.
MAX_BODY_BYTES = 512 * 1024


async def verify_retell_request(request: Request) -> bytes:
    """Returns the raw body if the request is authentic; raises 401 otherwise."""
    raw_body = await request.body()
    if len(raw_body) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="body too large")

    secret = request.headers.get("X-Tool-Secret", "")
    if settings.tool_shared_secret and secret:
        if hmac.compare_digest(secret, settings.tool_shared_secret):
            return raw_body
        raise HTTPException(status_code=401, detail="bad tool secret")

    signature = request.headers.get("X-Retell-Signature", "")
    if not signature:
        raise HTTPException(status_code=401, detail="missing signature")
    valid = retell_verify(
        raw_body.decode("utf-8"),
        api_key=settings.retell_api_key,
        signature=signature,
    )
    if not valid:
        raise HTTPException(status_code=401, detail="invalid signature")
    return raw_body
