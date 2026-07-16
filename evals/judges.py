"""LLM-as-judge scoring via DeepEval (temp-0 gpt-4o-mini judges).

Judged dimensions complement the deterministic checks:
  - knowledge_retention  -> redundant-question detection (never re-ask)
  - language_discipline  -> replies mirror the caller's language, no drift
  - scenario_criteria    -> scenario-specific rubric (fees, disclosure, ...)
Judges are imperfect (bias/variance) — scores are reported alongside, never
instead of, the deterministic results. See the report's limitations section.
"""
import logging

from deepeval.metrics import ConversationalGEval, KnowledgeRetentionMetric
from deepeval.test_case import ConversationalTestCase, Turn, TurnParams

from evals.common import JUDGE_MODEL

log = logging.getLogger("evals.judges")

LANGUAGE_CRITERIA = {
    "en": (
        "The caller speaks English throughout. The receptionist must respond in natural English and "
        "must not drift into Hindi (a Namaste greeting or clinic terms are acceptable)."
    ),
    "hi": (
        "The caller speaks Hindi throughout. The receptionist must respond in natural conversational "
        "Hindi (Devanagari), keeping only common clinic words in English (appointment, slot, branch, "
        "doctor names, times). Full English sentences to a Hindi speaker are a violation."
    ),
    "hinglish": (
        "The caller mixes Hindi and English mid-sentence. The receptionist must mirror this natural "
        "code-switching — responses should feel like one fluent bilingual speaker, not stitched "
        "translations, and must follow the caller's language of the moment."
    ),
}


def _to_test_case(result, scenario) -> ConversationalTestCase:
    turns = [
        Turn(role=("user" if t["role"] == "user" else "assistant"), content=t["content"] or "(empty)")
        for t in result.turns
    ]
    return ConversationalTestCase(
        name=scenario.id,
        turns=turns,
        scenario=scenario.description,
        chatbot_role="Bilingual (English/Hindi) AI receptionist for a physiotherapy clinic",
    )


def _run_metric(metric, test_case) -> dict:
    try:
        metric.measure(test_case, _show_indicator=False)
        return {"score": round(float(metric.score or 0), 3), "passed": bool(metric.is_successful()), "reason": (metric.reason or "")[:400]}
    except TypeError:
        metric.measure(test_case)
        return {"score": round(float(metric.score or 0), 3), "passed": bool(metric.is_successful()), "reason": (metric.reason or "")[:400]}
    except Exception as exc:  # noqa: BLE001
        log.warning("judge failed: %s", exc)
        return {"score": None, "passed": None, "reason": f"judge error: {exc}"[:200]}


def judge_conversation(result, scenario) -> dict:
    test_case = _to_test_case(result, scenario)
    scores: dict[str, dict] = {}

    scores["knowledge_retention"] = _run_metric(
        KnowledgeRetentionMetric(threshold=0.6, model=JUDGE_MODEL, include_reason=True, async_mode=False),
        test_case,
    )
    scores["language_discipline"] = _run_metric(
        ConversationalGEval(
            name="language_discipline",
            criteria=LANGUAGE_CRITERIA[scenario.language],
            evaluation_params=[TurnParams.CONTENT, TurnParams.ROLE],
            model=JUDGE_MODEL,
            threshold=0.6,
            async_mode=False,
        ),
        test_case,
    )
    if scenario.judge_criteria:
        scores["scenario_criteria"] = _run_metric(
            ConversationalGEval(
                name="scenario_criteria",
                criteria=scenario.judge_criteria,
                evaluation_params=[TurnParams.CONTENT, TurnParams.ROLE],
                model=JUDGE_MODEL,
                threshold=0.6,
                async_mode=False,
            ),
            test_case,
        )
    return scores
