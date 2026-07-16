"""Dump recent calls (transcript + tool calls + latency) for manual review.
Run: python -m scripts.dump_calls [N]"""
import json
import sys

from retell import Retell

from app.config import get_settings

settings = get_settings()


def main() -> None:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    client = Retell(api_key=settings.retell_api_key)
    calls = client.call.list(limit=limit)
    items = calls.items if hasattr(calls, "items") else calls
    for call in items:
        data = call.model_dump()
        print("=" * 78)
        print(f"call_id: {data.get('call_id')}  type: {data.get('call_type')}  "
              f"status: {data.get('call_status')}  reason: {data.get('disconnection_reason')}")
        ms = (data.get("end_timestamp") or 0) - (data.get("start_timestamp") or 0)
        print(f"duration: {ms/1000:.0f}s  agent: {data.get('agent_id')}")
        latency = data.get("latency") or {}
        for comp in ("e2e", "llm", "tts"):
            stats = latency.get(comp) or {}
            if stats.get("p50") is not None:
                print(f"latency {comp}: p50={stats.get('p50')}ms p90={stats.get('p90')}ms max={stats.get('max')}ms n={stats.get('num')}")
        dyn = data.get("retell_llm_dynamic_variables") or {}
        if dyn:
            print(f"dynamic vars: { {k: v for k, v in dyn.items() if v not in ('none', 'unknown', '', 'false')} }")
        print("--- transcript with tool calls ---")
        for utt in data.get("transcript_with_tool_calls") or []:
            role = utt.get("role", "?")
            if role == "tool_call_invocation":
                print(f"  [TOOL CALL] {utt.get('name')}({utt.get('arguments')})")
            elif role == "tool_call_result":
                content = (utt.get("content") or "")[:300]
                print(f"  [TOOL RESULT] {content}")
            else:
                print(f"  {role}: {utt.get('content')}")
        analysis = data.get("call_analysis") or {}
        if analysis.get("call_summary"):
            print(f"--- summary: {analysis['call_summary']}")


if __name__ == "__main__":
    main()
