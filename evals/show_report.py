"""Print a readable digest of the latest eval report.
Run: python -m evals.show_report [--full]"""
import io
import json
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")


def main() -> None:
    report = json.loads((Path(__file__).parent / "out" / "report.json").read_text(encoding="utf-8"))
    full = "--full" in sys.argv
    for s in report["scenarios"]:
        print("=" * 70)
        print(f"{s['scenario']} [{s['language']}] -> {'PASS' if s['deterministic_pass'] else 'FAIL'} "
              f"(turns {s['turns_to_completion']}, ended: {s['ended_reason']})")
        for c in s["checks"]:
            print(f"  [{'ok' if c['passed'] else 'XX'}] {c['detail']}")
        for name, j in (s.get("judges") or {}).items():
            print(f"  judge {name}: {j.get('score')} {'pass' if j.get('passed') else 'fail'} — {j.get('reason', '')[:140]}")
        if full or not s["deterministic_pass"]:
            print("  --- tool trace ---")
            for t in s["tool_trace"]:
                print(f"    {t['name']} status={t['result_status']} args={json.dumps(t['arguments'], ensure_ascii=False)[:160]}")
            print("  --- transcript ---")
            for turn in s["transcript"]:
                print(f"    {turn['role'].upper()}: {(turn['content'] or '')[:200]}")


if __name__ == "__main__":
    main()
