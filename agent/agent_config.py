"""Agent-as-code: creates/updates the Retell LLM + Agent from files in git.

Usage:
    python -m agent.agent_config voices   # list candidate Hindi/Indian voices
    python -m agent.agent_config sync     # create or update the agent, print its id

The agent's entire definition lives in this repo (prompt.md + tools_schema.py +
the settings below); the dashboard is never the source of truth.
"""
import sys
from pathlib import Path

from retell import Retell

from agent.tools_schema import build_tools
from app.config import get_settings

AGENT_NAME = "arogya-receptionist"
MODEL = "gpt-4.1"  # strong tool-calling + low TTFT; natively hosted by Retell

settings = get_settings()

DEFAULT_DYNAMIC_VARIABLES = {
    "current_datetime_ist": "unknown — ask naturally if the caller mentions relative dates",
    "caller_phone": "unknown",
    "known_patient": "false",
    "patient_names": "",
    "multiple_patients": "false",
    "upcoming_appointments": "none",
    "resume_context": "none",
    "owed_callback_context": "none",
}


def _prompt_text() -> str:
    return (Path(__file__).parent / "prompt.md").read_text(encoding="utf-8")


def _client() -> Retell:
    if not settings.retell_api_key:
        sys.exit("RETELL_API_KEY missing in .env")
    return Retell(api_key=settings.retell_api_key)


def pick_voice(client: Retell) -> str:
    """Explicit RETELL_VOICE_ID wins; otherwise auto-pick a female Hindi/Hinglish
    voice (Cartesia preferred for latency), and print what was chosen."""
    if settings.retell_voice_id:
        return settings.retell_voice_id
    voices = client.voice.list()
    scored = []
    for voice in voices:
        name = (getattr(voice, "voice_name", "") or "").lower()
        provider = (getattr(voice, "provider", "") or "").lower()
        gender = (getattr(voice, "gender", "") or "").lower()
        accent = (getattr(voice, "accent", "") or "").lower()
        score = 0
        if "hinglish" in name:
            score += 100
        if "hindi" in name or "hindi" in accent or "indian" in name or "indian" in accent:
            score += 40
        if gender == "female":
            score += 10
        if provider == "cartesia":
            score += 5
        if score > 0:
            scored.append((score, voice))
    if not scored:
        sys.exit(
            "No Hindi/Indian voice auto-detected. Run `python -m agent.agent_config voices`, "
            "pick one, and set RETELL_VOICE_ID in .env"
        )
    scored.sort(key=lambda pair: -pair[0])
    best = scored[0][1]
    print(f"voice auto-selected: {best.voice_id} ({getattr(best, 'voice_name', '?')}, "
          f"{getattr(best, 'provider', '?')}, {getattr(best, 'accent', '?')})")
    return best.voice_id


def list_voices() -> None:
    client = _client()
    for voice in client.voice.list():
        name = (getattr(voice, "voice_name", "") or "").lower()
        accent = (getattr(voice, "accent", "") or "").lower()
        if any(k in name + " " + accent for k in ("hindi", "hinglish", "indian", "multilingual")):
            print(
                f"{voice.voice_id:40s} {getattr(voice, 'voice_name', '?'):24s} "
                f"{getattr(voice, 'provider', '?'):12s} {getattr(voice, 'gender', '?'):8s} "
                f"{getattr(voice, 'accent', '?')}"
            )


def sync() -> None:
    client = _client()
    if not settings.app_base_url.startswith("https://"):
        print(f"WARNING: APP_BASE_URL is {settings.app_base_url} — Retell needs a public https URL.")
    if not settings.tool_shared_secret:
        sys.exit("TOOL_SHARED_SECRET missing in .env")

    llm_config = {
        "model": MODEL,
        "model_temperature": 0,
        "start_speaker": "agent",
        "begin_message": None,  # greeting is generated from the prompt (context-aware)
        "general_prompt": _prompt_text(),
        "general_tools": build_tools(settings.app_base_url, settings.tool_shared_secret),
        "default_dynamic_variables": DEFAULT_DYNAMIC_VARIABLES,
    }

    # agent.list() returns paginated {items, has_more} since the 2026 v3 API,
    # and list items are slim summaries — retrieve the full agent for its LLM id.
    summary = next((a for a in client.agent.list().items if a.agent_name == AGENT_NAME), None)
    existing = client.agent.retrieve(summary.agent_id) if summary else None

    if existing:
        llm_id = existing.response_engine.llm_id
        client.llm.update(llm_id, **llm_config)
        agent = client.agent.update(
            existing.agent_id,
            voice_id=pick_voice(client),
            language=["en-IN", "hi-IN"],
            webhook_url=f"{settings.app_base_url}/retell/webhook",
            interruption_sensitivity=0.7,
            enable_backchannel=True,
            vocab_specialization="medical",
            timezone="Asia/Kolkata",
        )
        print(f"updated agent {agent.agent_id} (llm {llm_id})")
    else:
        llm = client.llm.create(**llm_config)
        agent = client.agent.create(
            response_engine={"type": "retell-llm", "llm_id": llm.llm_id},
            agent_name=AGENT_NAME,
            voice_id=pick_voice(client),
            language=["en-IN", "hi-IN"],
            webhook_url=f"{settings.app_base_url}/retell/webhook",
            interruption_sensitivity=0.7,
            enable_backchannel=True,
            vocab_specialization="medical",
            timezone="Asia/Kolkata",
        )
        print(f"created agent {agent.agent_id} (llm {llm.llm_id})")

    try:
        client.agent.publish(agent.agent_id, version=agent.version)
        print(f"agent published (version {agent.version})")
    except Exception as exc:  # noqa: BLE001 — publish API optional depending on account
        print(f"publish skipped ({exc})")

    print("\nNext steps:")
    print(f"  1. Set RETELL_AGENT_ID={agent.agent_id} in .env AND as a Fly secret")
    print("  2. Attach this agent + the inbound webhook to your phone number:")
    print(f"     inbound webhook URL: {settings.app_base_url}/retell/inbound")
    print(f"  3. Web-call test page: {settings.app_base_url}/")


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "sync"
    if command == "voices":
        list_voices()
    else:
        sync()
