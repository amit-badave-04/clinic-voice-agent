"""Drives one scenario conversation: an LLM simulated patient talks to the
production agent brain over Retell's Chat API (text-to-text).

Why Chat API: it executes the SAME published prompt + tools against the REAL
backend and database — so tool traces and DB effects are genuine. What it does
NOT exercise (ASR/TTS/telephony) is measured separately from real calls and
declared in the report's limitations.
"""
import asyncio
import json
import logging
from dataclasses import dataclass, field

from evals.common import CHAT_AGENT_NAME, PERSONA_MODEL, openai_async, retell_async, retell_sync, settings

log = logging.getLogger("evals.driver")

DONE_TOKEN = "[[DONE]]"


@dataclass
class ConversationResult:
    scenario_id: str
    chat_id: str = ""
    turns: list = field(default_factory=list)  # [{"role": "user"|"agent", "content": str}]
    tool_calls: list = field(default_factory=list)  # [{"name", "arguments"(dict), "result"(dict|str)}]
    user_turns: int = 0
    ended_reason: str = "max_turns"
    error: str = ""


# LLM fields (wire names) that can be replayed into llm.create()
_LLM_CLONE_FIELDS = {
    "model", "s2s_model", "model_temperature", "model_high_priority",
    "tool_call_strict_mode", "general_prompt", "general_tools", "begin_message",
    "default_dynamic_variables", "start_speaker", "begin_after_user_silence_ms",
    "states", "starting_state", "knowledge_base_ids", "kb_config", "mcps",
}


def ensure_chat_agent() -> str:
    """(Re)create the eval chat agent so evals test EXACTLY the live config.

    Chat agents can't pin an LLM version at creation (API: 'Cannot specify
    version > 0 for new agent') and default to v0 — which silently tests a
    stale prompt. So each run we clone the voice agent's current LLM (exact
    prompt + tools) into a dedicated eval LLM and bind a fresh chat agent to
    it, deleting the previous run's pair."""
    client = retell_sync()
    voice_agent = client.agent.retrieve(settings.retell_agent_id)
    engine = voice_agent.response_engine
    live_llm = client.llm.retrieve(engine.llm_id, version=int(engine.version or 0))

    # tear down previous eval pair (agent + its cloned llm)
    for summary in client.chat_agent.list().items:
        if summary.agent_name != CHAT_AGENT_NAME:
            continue
        try:
            full = client.chat_agent.retrieve(summary.agent_id)
            old_llm_id = full.response_engine.llm_id
            client.chat_agent.delete(summary.agent_id)
            if old_llm_id != engine.llm_id:
                client.llm.delete(old_llm_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("stale eval chat agent cleanup: %s", exc)

    dump = live_llm.model_dump(by_alias=True, exclude_none=True)
    clone_config = {k: v for k, v in dump.items() if k in _LLM_CLONE_FIELDS}
    eval_llm = client.llm.create(**clone_config)
    chat_agent = client.chat_agent.create(
        response_engine={"type": "retell-llm", "llm_id": eval_llm.llm_id},
        agent_name=CHAT_AGENT_NAME,
    )
    log.info("eval chat agent %s on cloned llm %s (from %s v%s)",
             chat_agent.agent_id, eval_llm.llm_id, engine.llm_id, engine.version)
    return chat_agent.agent_id


def _persona_system_prompt(scenario) -> str:
    return f"""You are role-playing a PATIENT calling a physiotherapy clinic's receptionist. Stay fully in character.

PERSONA AND GOAL:
{scenario.persona}

YOUR PHONE NUMBER (if the receptionist asks for it): {scenario.phone}

LANGUAGE: {scenario.language_instruction}

RULES:
- Output ONLY the patient's next spoken utterance — short and natural, like real phone speech (fillers ok).
- One thought per turn. Do not dump all information at once unless the persona says to.
- Never break character, never mention being an AI or a test.
- When your goal is fully achieved (or clearly impossible) and the receptionist has wrapped up, output exactly {DONE_TOKEN} instead of an utterance."""


async def run_conversation(scenario, chat_agent_id: str, max_turns: int = 12) -> ConversationResult:
    result = ConversationResult(scenario_id=scenario.id)
    retell = retell_async()
    openai = openai_async()
    try:
        chat = await retell.chat.create(
            agent_id=chat_agent_id,
            retell_llm_dynamic_variables=scenario.context_vars,
            # simulated_phone rides in metadata exactly like the web-call page,
            # so tool endpoints resolve caller identity the same way.
            metadata={"eval_scenario": scenario.id, "simulated_phone": scenario.phone},
        )
        result.chat_id = chat.chat_id

        persona_messages = [{"role": "system", "content": _persona_system_prompt(scenario)}]
        user_message = scenario.opening

        for _ in range(max_turns):
            result.turns.append({"role": "user", "content": user_message})
            result.user_turns += 1
            persona_messages.append({"role": "assistant", "content": user_message})

            completion = await retell.chat.create_chat_completion(
                chat_id=chat.chat_id, content=user_message
            )
            agent_texts = []
            for message in completion.messages or []:
                data = message.model_dump()
                role = data.get("role")
                if role == "agent" and data.get("content"):
                    agent_texts.append(data["content"])
                elif role == "tool_call_invocation":
                    args = data.get("arguments")
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            args = {"_raw": args}
                    result.tool_calls.append(
                        {"name": data.get("name"), "arguments": args or {}, "result": None,
                         "tool_call_id": data.get("tool_call_id")}
                    )
                elif role == "tool_call_result":
                    content = data.get("content")
                    parsed = content
                    if isinstance(content, str):
                        try:
                            parsed = json.loads(content)
                        except json.JSONDecodeError:
                            parsed = content
                    for call in reversed(result.tool_calls):
                        if call["result"] is None and call.get("tool_call_id") == data.get("tool_call_id"):
                            call["result"] = parsed
                            break
                    else:
                        for call in reversed(result.tool_calls):
                            if call["result"] is None:
                                call["result"] = parsed
                                break
            agent_reply = " ".join(agent_texts).strip()
            result.turns.append({"role": "agent", "content": agent_reply})
            persona_messages.append({"role": "user", "content": agent_reply or "(silence)"})

            response = await openai.chat.completions.create(
                model=PERSONA_MODEL, temperature=0.3, max_tokens=120, messages=persona_messages
            )
            user_message = (response.choices[0].message.content or "").strip()
            if DONE_TOKEN in user_message or not user_message:
                result.ended_reason = "goal_reached"
                break
    except Exception as exc:  # noqa: BLE001
        log.exception("scenario %s failed", scenario.id)
        result.error = str(exc)
    finally:
        await retell.close()
        await openai.close()
    return result
