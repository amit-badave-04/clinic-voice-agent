"""Deterministic checks over tool traces — no LLM judgment involved.
Each returns (passed: bool, detail: str)."""
import json


def calls_of(trace: list, name: str) -> list:
    return [c for c in trace if c["name"] == name]


def tool_order(trace: list, before: str, after: str) -> tuple[bool, str]:
    """Every `after` call must be preceded by at least one `before` call."""
    seen_before = False
    for call in trace:
        if call["name"] == before:
            seen_before = True
        elif call["name"] == after and not seen_before:
            return False, f"{after} was called before any {before}"
    if not calls_of(trace, after):
        return False, f"{after} was never called"
    return True, f"{after} always preceded by {before}"


def slot_ids_are_genuine(trace: list) -> tuple[bool, str]:
    """Every slot_id passed to book/reschedule must have been returned by a
    prior search_availability result (catches fabricated slot ids)."""
    offered: set[str] = set()
    for call in trace:
        if call["name"] == "search_availability" and isinstance(call.get("result"), dict):
            for slot in call["result"].get("slots", []):
                offered.add(slot.get("slot_id", ""))
        if call["name"] in ("book_appointment", "reschedule_appointment"):
            slot_id = call["arguments"].get("slot_id") or call["arguments"].get("new_slot_id")
            if slot_id and slot_id not in offered:
                return False, f"{call['name']} used a slot_id never returned by a search"
    return True, "all slot_ids came from search results"


def distinct_cancel_ids(trace: list, expected: int) -> tuple[bool, str]:
    """Regression (cancel-all bug): N cancellations require N distinct
    appointment_ids actually cancelled."""
    cancelled_ids = {
        json.dumps(c["arguments"].get("appointment_id"))
        for c in calls_of(trace, "cancel_appointment")
        if isinstance(c.get("result"), dict) and c["result"].get("status") == "cancelled"
    }
    ok = len(cancelled_ids) >= expected
    return ok, f"{len(cancelled_ids)} distinct cancellations (expected >= {expected})"


def search_args_contain(trace: list, **expectations) -> tuple[bool, str]:
    """At least one search call must reflect the caller's stated constraints
    (e.g. weekday_mask contains 'thu', part_of_day contains 'morning')."""
    for call in calls_of(trace, "search_availability"):
        args = call["arguments"]
        ok = True
        for key, expected in expectations.items():
            value = args.get(key)
            if isinstance(expected, bool):
                ok &= bool(value) == expected
            elif isinstance(expected, str):
                container = value if isinstance(value, (list, str)) else ""
                ok &= expected in container
            else:
                ok &= value == expected
        if ok:
            return True, f"search args matched {expectations}"
    return False, f"no search call matched {expectations}"


def tool_result_field(trace: list, tool: str, field: str, expected) -> tuple[bool, str]:
    for call in calls_of(trace, tool):
        result = call.get("result")
        if isinstance(result, dict) and result.get(field) == expected:
            return True, f"{tool}.{field} == {expected!r}"
    return False, f"no {tool} result had {field} == {expected!r}"


def tool_called(trace: list, name: str, min_count: int = 1) -> tuple[bool, str]:
    count = len(calls_of(trace, name))
    return count >= min_count, f"{name} called {count}x (expected >= {min_count})"


def tool_not_needed(trace: list, name: str) -> tuple[bool, str]:
    """The tool may be called, but must not produce a successful state change
    (used for duplicate-booking guard: booking may be attempted but must be
    rejected)."""
    for call in calls_of(trace, name):
        result = call.get("result")
        if isinstance(result, dict) and result.get("status") == "confirmed":
            return False, f"{name} produced a confirmed result but should not have"
    return True, f"{name} produced no confirmed result"


def booked_name_is(trace: list, expected_name: str) -> tuple[bool, str]:
    for call in calls_of(trace, "book_appointment"):
        result = call.get("result")
        if isinstance(result, dict) and result.get("status") == "confirmed":
            actual = call["arguments"].get("patient_full_name", "")
            if expected_name.lower() in actual.lower():
                return True, f"booked under '{actual}'"
            return False, f"booked under '{actual}', expected '{expected_name}'"
    return False, "no confirmed booking found"
