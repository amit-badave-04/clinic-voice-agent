"""Best-effort call summarization (gpt-4o-mini) for dropped-call resume context."""
import logging

from openai import AsyncOpenAI

from app.config import get_settings

log = logging.getLogger("summarize")
settings = get_settings()

_client: AsyncOpenAI | None = None


def _openai() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=settings.openai_api_key)
    return _client


async def summarize_incomplete_call(transcript: str, collected: dict) -> str:
    """1-2 sentence handoff summary for the next call. Falls back to the raw
    collected entities if the LLM call fails — resume must still work."""
    fallback = (
        "Caller was mid-booking. Collected: "
        + ", ".join(f"{k}={v}" for k, v in collected.items())
        if collected
        else "Caller was mid-conversation; no details captured yet."
    )
    if not transcript:
        return fallback
    try:
        response = await _openai().chat.completions.create(
            model="gpt-4o-mini",
            temperature=0,
            max_tokens=120,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Summarize this incomplete clinic phone call in 1-2 short sentences "
                        "for the agent who takes the caller's next call: what the caller wanted, "
                        "what details were already given (name, branch, practitioner, day/time), "
                        "and what step remained. Write in English regardless of call language."
                    ),
                },
                {"role": "user", "content": transcript[:6000]},
            ],
        )
        return response.choices[0].message.content.strip()
    except Exception as exc:  # noqa: BLE001
        log.warning("summary failed, using fallback: %s", exc)
        return fallback
