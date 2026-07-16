"""Shared eval plumbing: clients, constants, output paths."""
import os
from pathlib import Path

from openai import AsyncOpenAI
from retell import AsyncRetell, Retell

from app.config import get_settings

settings = get_settings()

# DeepEval reads OPENAI_API_KEY from the environment.
os.environ.setdefault("OPENAI_API_KEY", settings.openai_api_key)
# Keep DeepEval fully local/offline: no telemetry, no Confident AI upload.
os.environ.setdefault("DEEPEVAL_TELEMETRY_OPT_OUT", "YES")
os.environ.setdefault("CONFIDENT_TRACE_VERBOSE", "NO")

OUT_DIR = Path(__file__).parent / "out"
OUT_DIR.mkdir(exist_ok=True)

# All synthetic eval traffic uses this phone prefix so cleanup can never touch
# demo/live data.
EVAL_PHONE_PREFIX = "+9190000009"

CHAT_AGENT_NAME = "arogya-receptionist-eval-chat"

JUDGE_MODEL = "gpt-4o-mini"
PERSONA_MODEL = "gpt-4o-mini"


def retell_sync() -> Retell:
    return Retell(api_key=settings.retell_api_key)


def retell_async() -> AsyncRetell:
    return AsyncRetell(api_key=settings.retell_api_key)


def openai_async() -> AsyncOpenAI:
    return AsyncOpenAI(api_key=settings.openai_api_key)
