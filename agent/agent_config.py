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
from seed.arogya_data import PRACTITIONERS

AGENT_NAME = "arogya-receptionist"
MODEL = "gpt-4.1"  # strong tool-calling + low TTFT; natively hosted by Retell

settings = get_settings()

# Bias the transcriber toward entities the streaming ASR mangles on Indian calls
# (observed live: "Wilson Garden" → garble, caller names → wrong Devanagari).
# "{{patient_names}}" uses Retell's dynamic-variable support so the known
# caller's own name(s) are boosted per call at no webhook cost.
_LOCALITY_KEYWORDS = [
    "Arogya", "Medax", "Arc", "physiotherapy",
    "Wilson Garden", "Bannerghatta Road", "Gottigere",
    "Hombegowdanagar", "Kalena Agrahara", "Hosur Main Road", "Indiranagar",
]


def build_boosted_keywords() -> list[str]:
    practitioner_names = [p["name"].removeprefix("Dr. ") for p in PRACTITIONERS]
    return _LOCALITY_KEYWORDS + practitioner_names + ["{{patient_names}}"]

DEFAULT_DYNAMIC_VARIABLES = {
    "current_datetime_ist": "unknown — ask naturally if the caller mentions relative dates",
    "caller_phone": "unknown",
    "known_patient": "false",
    "patient_names": "",
    "multiple_patients": "false",
    "upcoming_appointments": "none",
    "resume_context": "none",
    "owed_callback_context": "none",
    "last_interaction": "none",
}


def _prompt_text() -> str:
    return (Path(__file__).parent / "prompt.md").read_text(encoding="utf-8")


def agent_settings(client: "Retell") -> dict:
    """Voice-layer settings shared by create and update (v9 turn-taking tuning:
    faster barge-in, no backchannels on telephony, short idle nudges, ASR
    keyword boosting)."""
    voice_id = pick_voice(client)
    return dict(
        voice_id=voice_id,
        # TTS degradation path: Retell fails over mid-call to the fallback
        # voice (different provider) for the rest of the call.
        fallback_voice_ids=pick_fallback_voices(client, voice_id),
        post_call_analysis_data=POST_CALL_ANALYSIS,
        language=["en-IN", "hi-IN"],
        webhook_url=f"{settings.app_base_url}/retell/webhook",
        interruption_sensitivity=0.85,
        responsiveness=0.8,
        enable_backchannel=False,
        enable_dynamic_responsiveness=True,
        # 8s fired during phone-pickup and mid-thought (live calls 18 July:
        # doubled greetings, re-asked questions over the caller's answer).
        reminder_trigger_ms=12000,
        reminder_max_count=2,
        # Live Hindi audio was repeatedly transcribed as Spanish in fast mode
        # ("¿Es la hora del dos?"); accuracy-first STT is the only in-platform
        # lever against language drift. Revert if e2e p50 degrades >300ms.
        stt_mode="accurate",
        denoising_mode="noise-cancellation",
        boosted_keywords=build_boosted_keywords(),
        vocab_specialization="medical",
        timezone="Asia/Kolkata",
        # OTP entry for identity verification: 6 digits, # to finish early.
        allow_user_dtmf=True,
        user_dtmf_options={"digit_limit": 6, "termination_key": "#"},
        # Cost containment: a receptionist call has no honest reason to run
        # 15 minutes or sit silent for 2 (billing is per-minute).
        max_call_duration_ms=15 * 60 * 1000,
        end_call_after_silence_ms=2 * 60 * 1000,
    )


def _client() -> Retell:
    if not settings.retell_api_key:
        sys.exit("RETELL_API_KEY missing in .env")
    return Retell(api_key=settings.retell_api_key)


# Structured post-call QA, extracted by Retell after every call. The selectors
# encode exactly the failure modes found in live-call reviews (redundant
# questions, skipped name read-back, branch-switch friction).
POST_CALL_ANALYSIS = [
    {
        "type": "enum",
        "name": "task_outcome",
        "description": "What the caller walked away with.",
        "choices": ["booked", "rescheduled", "cancelled", "callback_requested", "info_only", "incomplete"],
    },
    {
        "type": "boolean",
        "name": "asked_redundant_questions",
        "description": "Did the agent re-ask anything the caller had already said or that was in context?",
    },
    {
        "type": "boolean",
        "name": "name_read_back_done",
        "description": "If a NEW patient name was collected: was it read back for confirmation before booking?",
    },
    {
        "type": "enum",
        "name": "branch_switch_friction",
        "description": "If the caller changed branch mid-call, how smoothly was it handled?",
        "choices": ["no_switch", "smooth", "mild_friction", "severe_friction"],
    },
    {
        "type": "enum",
        "name": "language_quality",
        "description": "Naturalness of the agent's language mirroring (English/Hindi/Hinglish).",
        "choices": ["natural", "acceptable", "awkward"],
    },
    {
        "type": "boolean",
        "name": "needs_human_review",
        "description": "Anything odd, unsafe, or unresolved a human should look at (scripts/qa_review.py digests these).",
    },
]


def pick_fallback_voices(client: Retell, primary_voice_id: str) -> list[str]:
    """One female Hindi/Indian voice from a DIFFERENT provider than the
    primary, so a TTS-provider outage degrades the voice instead of the call.
    Empty list when no cross-provider candidate exists."""
    primary_provider = ""
    candidates = []
    for voice in client.voice.list():
        provider = (getattr(voice, "provider", "") or "").lower()
        if voice.voice_id == primary_voice_id:
            primary_provider = provider
            continue
        name = (getattr(voice, "voice_name", "") or "").lower()
        accent = (getattr(voice, "accent", "") or "").lower()
        gender = (getattr(voice, "gender", "") or "").lower()
        if gender == "female" and any(k in name + " " + accent for k in ("hindi", "hinglish", "indian")):
            candidates.append((provider, voice.voice_id))
    fallback = next((vid for prov, vid in candidates if prov and prov != primary_provider), "")
    return [fallback] if fallback else []


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
        "model_high_priority": True,  # dedicated pool: lower + more consistent TTFT
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
        # Retell versioning: published versions are immutable; create_version
        # makes a draft agent version AND auto-drafts the pinned LLM at the
        # same version number. Update both drafts in place, then publish.
        if getattr(existing, "is_published", True):
            draft = client.agent.create_version(existing.agent_id, base_version=existing.version)
            draft_version = draft.version
        else:
            draft_version = existing.version  # latest is already an editable draft
        full_draft = client.agent.retrieve(existing.agent_id, version=draft_version)
        engine = full_draft.response_engine
        client.llm.update(engine.llm_id, version=int(engine.version), **llm_config)
        agent = client.agent.update(
            existing.agent_id,
            version=draft_version,
            **agent_settings(client),
        )
        print(f"updated agent {agent.agent_id} draft v{draft_version} (llm {engine.llm_id} v{int(engine.version)})")
    else:
        llm = client.llm.create(**llm_config)
        agent = client.agent.create(
            response_engine={"type": "retell-llm", "llm_id": llm.llm_id},
            agent_name=AGENT_NAME,
            **agent_settings(client),
        )
        print(f"created agent {agent.agent_id} (llm {llm.llm_id})")

    publish_version = agent.version
    try:
        client.agent.publish(agent.agent_id, version=publish_version)
        print(f"agent published (version {publish_version})")
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
