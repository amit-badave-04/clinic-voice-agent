"""Digest of calls that need a human eye.

Run: python -m scripts.qa_review [N]   (default: last 30 calls)

Reads the structured post-call analysis Retell extracts for every call
(fields defined in agent/agent_config.py POST_CALL_ANALYSIS) and prints the
calls flagged by any of: needs_human_review, call_successful false, redundant
questions, skipped name read-back, severe branch friction, awkward language.
Sends a one-line summary to the alert sinks when anything is flagged.
"""
import sys

from retell import Retell

from app.config import get_settings
from app.services import alerts

settings = get_settings()

FLAG_RULES = [
    ("needs_human_review", lambda v: v is True),
    ("asked_redundant_questions", lambda v: v is True),
    ("name_read_back_done", lambda v: v is False),
    ("branch_switch_friction", lambda v: v == "severe_friction"),
    ("language_quality", lambda v: v == "awkward"),
]


def main() -> None:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    client = Retell(api_key=settings.retell_api_key)
    calls = client.call.list(limit=limit)
    items = getattr(calls, "items", calls)

    flagged = []
    for call in items:
        analysis = getattr(call, "call_analysis", None)
        custom = (getattr(analysis, "custom_analysis_data", None) or {}) if analysis else {}
        reasons = [name for name, bad in FLAG_RULES if bad(custom.get(name))]
        if analysis is not None and getattr(analysis, "call_successful", True) is False:
            reasons.append("call_unsuccessful")
        if reasons:
            flagged.append((call, reasons))

    print(f"reviewed {len(items)} calls — {len(flagged)} flagged")
    for call, reasons in flagged:
        analysis = getattr(call, "call_analysis", None)
        summary = (getattr(analysis, "call_summary", "") or "")[:140] if analysis else ""
        print(f"\n  {call.call_id}  [{', '.join(reasons)}]")
        print(f"    {summary}")

    if flagged:
        import asyncio

        asyncio.run(alerts.notify(f"🔍 QA review: {len(flagged)}/{len(items)} recent calls flagged — run scripts/qa_review.py for detail"))


if __name__ == "__main__":
    main()
