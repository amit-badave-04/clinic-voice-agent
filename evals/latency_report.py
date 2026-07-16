"""Per-language latency report from REAL calls (phone + web).

Retell's Get Call latency object gives per-call percentiles for e2e / llm / tts
(ms). We bucket calls by language via a Devanagari-script heuristic over the
transcript and aggregate. IMPORTANT caveat (also printed in the report):
Retell's e2e metric EXCLUDES the caller-side network leg — an India-based
caller experiences roughly +250-350ms on top of these numbers.
"""
import re
import statistics

from evals.common import retell_sync

DEVANAGARI = re.compile(r"[ऀ-ॿ]")


def classify_language(transcript: str) -> str:
    if not transcript:
        return "unknown"
    letters = [c for c in transcript if c.isalpha()]
    if not letters:
        return "unknown"
    ratio = sum(1 for c in letters if DEVANAGARI.match(c)) / len(letters)
    if ratio > 0.45:
        return "hi"
    if ratio > 0.05:
        return "mixed"
    return "en"


def _aggregate(values: list[float]) -> dict:
    if not values:
        return {}
    values = sorted(values)
    return {
        "n": len(values),
        "p50": round(statistics.median(values)),
        "p90": round(values[min(len(values) - 1, int(0.9 * len(values)))]),
        "max": round(max(values)),
    }


def build_latency_report(limit: int = 50) -> dict:
    client = retell_sync()
    listing = client.call.list(limit=limit)
    calls = listing.items if hasattr(listing, "items") else listing

    buckets: dict[str, dict[str, list[float]]] = {}
    call_count = 0
    for call_summary in calls:
        data = call_summary.model_dump()
        if not data.get("call_id"):
            continue
        # list() items can be slim — retrieve for transcript + latency
        call = client.call.retrieve(data["call_id"]).model_dump()
        latency = call.get("latency") or {}
        if not latency.get("e2e"):
            continue
        language = classify_language(call.get("transcript", ""))
        bucket = buckets.setdefault(language, {"e2e_p50": [], "llm_p50": [], "tts_p50": [], "e2e_max": []})
        e2e, llm, tts = latency.get("e2e") or {}, latency.get("llm") or {}, latency.get("tts") or {}
        if e2e.get("p50") is not None:
            bucket["e2e_p50"].append(e2e["p50"])
            bucket["e2e_max"].append(e2e.get("max", e2e["p50"]))
        if llm.get("p50") is not None:
            bucket["llm_p50"].append(llm["p50"])
        if tts.get("p50") is not None:
            bucket["tts_p50"].append(tts["p50"])
        call_count += 1

    report = {"calls_analyzed": call_count, "by_language": {}}
    for language, metrics in buckets.items():
        report["by_language"][language] = {
            "e2e_p50_ms": _aggregate(metrics["e2e_p50"]),
            "e2e_max_ms": _aggregate(metrics["e2e_max"]),
            "llm_p50_ms": _aggregate(metrics["llm_p50"]),
            "tts_p50_ms": _aggregate(metrics["tts_p50"]),
        }
    report["caveat"] = (
        "Latency values are Retell-measured per-call percentiles aggregated across calls. "
        "Retell's e2e EXCLUDES the caller-side network leg: callers dialing from India over the "
        "US number (or WebRTC from India) experience roughly +250-350ms on top of these figures. "
        "ASR component latency is not separately exposed by the platform; it is included in e2e."
    )
    return report
