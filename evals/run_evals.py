"""Eval harness entrypoint.

    python -m evals.run_evals                 # full suite
    python -m evals.run_evals book_happy_en   # one scenario (debugging)
    python -m evals.run_evals --skip-judges   # deterministic checks only

Pipeline per scenario: seed fixtures -> simulated patient converses with the
production agent brain (Retell Chat API; real tools, real DB, real Cliniko)
-> deterministic tool-trace + DB assertions -> DeepEval judges -> cleanup.
Outputs evals/out/report.json and evals/out/report.md. Exit code 1 if any
deterministic check fails (CI-friendly).
"""
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone

from evals import db_helpers
from evals.chat_driver import ensure_chat_agent, run_conversation
from evals.common import OUT_DIR, settings
from evals.latency_report import build_latency_report
from evals.scenarios import build_scenarios

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("evals")

CONCURRENCY = 3

LIMITATIONS = """\
## Where this harness gives false confidence (read before trusting the numbers)

1. **Text-mode blind spot.** Scenario conversations run text-to-text through the same LLM,
   prompt, tools, backend and database as live calls — but they bypass ASR, TTS and telephony.
   Production voice failures (mishearing Indian names/entities, TTS mispronunciation,
   barge-in glitches) are invisible here. Real-call latency is reported separately, and live
   scripted calls (scripts/live_call_checklist.md) remain the truth tier.
2. **Cooperative simulated users.** The gpt-4o-mini patient follows its persona and rarely
   mumbles, interrupts, or changes its mind erratically the way real callers do.
3. **LLM judges are imperfect.** Judged scores (language discipline, scenario rubrics) carry
   known biases; deterministic tool-trace and database assertions are the load-bearing checks,
   judges are advisory.
4. **Latency under no load.** All latency data comes from single concurrent calls; evaluator
   traffic patterns may differ. Retell's e2e also excludes the caller-side network leg
   (~+250-350ms from India).
5. **Availability nondeterminism.** Scenarios run against live Cliniko availability; a scenario
   could fail if the clinic calendar is fully booked for the searched window. Fixtures pin what
   can be pinned; searches use near-term windows that are normally open.
"""


async def run_scenario(scenario, chat_agent_id: str, skip_judges: bool) -> dict:
    log.info("running scenario %s", scenario.id)
    if scenario.setup:
        try:
            await scenario.setup()
        except Exception as exc:  # noqa: BLE001 — one bad fixture must not kill the suite
            log.exception("setup failed for %s", scenario.id)
            return {
                "scenario": scenario.id, "language": scenario.language,
                "description": scenario.description, "deterministic_pass": False,
                "checks": [{"passed": False, "detail": f"setup failed: {exc}"}],
                "judges": {}, "turns_to_completion": 0, "target_turns": scenario.target_turns,
                "ended_reason": "setup_error", "chat_id": "", "transcript": [], "tool_trace": [],
            }

    # Context comes from the PRODUCTION inbound-context builder (real
    # appointment IDs, real patient state) — exactly what a phone call would
    # inject — plus scenario-specific overrides (e.g. a simulated prior call).
    from app.db.session import SessionLocal
    from app.services import sessions as sessions_svc

    async with SessionLocal() as db_session:
        context_vars = await sessions_svc.build_inbound_context(db_session, scenario.phone)
        await db_session.commit()
    context_vars.update(scenario.context_overrides)

    result = await run_conversation(
        scenario, chat_agent_id, context_vars, max_turns=scenario.max_turns
    )

    checks = []
    for check in scenario.checks:
        try:
            passed, detail = check(result.tool_calls)
        except Exception as exc:  # noqa: BLE001
            passed, detail = False, f"check crashed: {exc}"
        checks.append({"passed": passed, "detail": detail})
    for db_check in scenario.db_checks:
        try:
            passed, detail = await db_check()
        except Exception as exc:  # noqa: BLE001
            passed, detail = False, f"db check crashed: {exc}"
        checks.append({"passed": passed, "detail": detail})
    if result.error:
        checks.append({"passed": False, "detail": f"conversation error: {result.error}"})

    judges = {}
    if not skip_judges and result.turns:
        from evals.judges import judge_conversation  # import here: deepeval is heavy

        judges = await asyncio.to_thread(judge_conversation, result, scenario)

    deterministic_pass = all(c["passed"] for c in checks) if checks else bool(result.turns)
    return {
        "scenario": scenario.id,
        "language": scenario.language,
        "description": scenario.description,
        "deterministic_pass": deterministic_pass,
        "checks": checks,
        "judges": judges,
        "turns_to_completion": result.user_turns,
        "target_turns": scenario.target_turns,
        "ended_reason": result.ended_reason,
        "chat_id": result.chat_id,
        "transcript": result.turns,
        "tool_trace": [
            {"name": c["name"], "arguments": c["arguments"],
             "result_status": (c["result"] or {}).get("status") if isinstance(c.get("result"), dict) else None}
            for c in result.tool_calls
        ],
    }


def _judge_cell(judges: dict, key: str) -> str:
    entry = judges.get(key) or {}
    if entry.get("score") is None:
        return "—"
    return f"{entry['score']:.2f} {'✓' if entry.get('passed') else '✗'}"


def write_markdown(report: dict) -> str:
    lines = [
        "# Eval Report — clinic voice agent",
        "",
        f"Generated: {report['generated_at']}  ·  Agent: `{report['agent_id']}`  ·  Scenarios: {len(report['scenarios'])}",
        "",
        "## Scenario results",
        "",
        "| Scenario | Lang | Deterministic | Retention | Language | Rubric | Turns |",
        "|---|---|---|---|---|---|---|",
    ]
    for s in report["scenarios"]:
        lines.append(
            f"| {s['scenario']} | {s['language']} | {'PASS' if s['deterministic_pass'] else '**FAIL**'} "
            f"| {_judge_cell(s['judges'], 'knowledge_retention')} "
            f"| {_judge_cell(s['judges'], 'language_discipline')} "
            f"| {_judge_cell(s['judges'], 'scenario_criteria')} "
            f"| {s['turns_to_completion']}/{s['target_turns']} |"
        )
    lines += ["", "### Check details (failures only)", ""]
    any_fail = False
    for s in report["scenarios"]:
        failed = [c for c in s["checks"] if not c["passed"]]
        if failed:
            any_fail = True
            lines.append(f"- **{s['scenario']}**:")
            lines += [f"  - {c['detail']}" for c in failed]
    if not any_fail:
        lines.append("_All deterministic checks passed._")

    lines += ["", "## Per-language aggregates", "", "| Language | Scenarios | Deterministic pass | Avg turns |", "|---|---|---|---|"]
    for language, agg in report["per_language"].items():
        lines.append(
            f"| {language} | {agg['count']} | {agg['pass']}/{agg['count']} | {agg['avg_turns']:.1f} |"
        )

    lines += ["", "## Latency (real calls, per language)", ""]
    latency = report["latency"]
    lines.append(f"Calls analyzed: {latency.get('calls_analyzed', 0)}")
    lines += ["", "| Language | Calls | e2e p50 (ms) | e2e max (ms) | LLM p50 (ms) | TTS p50 (ms) |", "|---|---|---|---|---|---|"]
    for language, m in latency.get("by_language", {}).items():
        e2e, e2em, llm, tts = m["e2e_p50_ms"], m["e2e_max_ms"], m["llm_p50_ms"], m["tts_p50_ms"]
        lines.append(
            f"| {language} | {e2e.get('n', 0)} | {e2e.get('p50', '—')} | {e2em.get('max', '—')} "
            f"| {llm.get('p50', '—')} | {tts.get('p50', '—')} |"
        )
    lines += ["", f"> {latency.get('caveat', '')}", "", LIMITATIONS]
    return "\n".join(lines)


async def main() -> int:
    args = [a for a in sys.argv[1:]]
    skip_judges = "--skip-judges" in args
    only = [a for a in args if not a.startswith("--")]

    scenarios = build_scenarios()
    if only:
        scenarios = [s for s in scenarios if s.id in only]
        if not scenarios:
            log.error("no scenario matched %s", only)
            return 2

    log.info("cleaning previous eval data...")
    await db_helpers.cleanup_eval_data()

    log.info("ensuring eval chat agent...")
    chat_agent_id = await asyncio.to_thread(ensure_chat_agent)

    semaphore = asyncio.Semaphore(CONCURRENCY)

    async def bounded(scenario):
        async with semaphore:
            return await run_scenario(scenario, chat_agent_id, skip_judges)

    results = await asyncio.gather(*[bounded(s) for s in scenarios])

    per_language: dict[str, dict] = {}
    for r in results:
        agg = per_language.setdefault(r["language"], {"count": 0, "pass": 0, "turns": []})
        agg["count"] += 1
        agg["pass"] += 1 if r["deterministic_pass"] else 0
        agg["turns"].append(r["turns_to_completion"])
    for agg in per_language.values():
        agg["avg_turns"] = sum(agg["turns"]) / len(agg["turns"])
        del agg["turns"]

    log.info("building latency report from real calls...")
    try:
        latency = await asyncio.to_thread(build_latency_report)
    except Exception as exc:  # noqa: BLE001
        log.warning("latency report failed: %s", exc)
        latency = {"error": str(exc)}

    log.info("cleaning up eval bookings...")
    cleanup = await db_helpers.cleanup_eval_data()

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "agent_id": settings.retell_agent_id,
        "scenarios": results,
        "per_language": per_language,
        "latency": latency,
        "cleanup": cleanup,
    }
    (OUT_DIR / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    (OUT_DIR / "report.md").write_text(write_markdown(report), encoding="utf-8")

    failures = [r["scenario"] for r in results if not r["deterministic_pass"]]
    print(f"\n{'='*60}\nEval complete: {len(results) - len(failures)}/{len(results)} scenarios passed deterministically")
    if failures:
        print(f"FAILED: {', '.join(failures)}")
    print(f"Reports: {OUT_DIR / 'report.md'} | report.json")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
