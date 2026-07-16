"""Tool definitions for the Retell LLM (general_tools).

The JSON-schema layer of the agent↔backend contract. Descriptions are written
FOR THE LLM: they encode when to call, freshness rules, and what to say while
executing (bilingual latency-masking fillers, guaranteed by the platform)."""


def build_tools(base_url: str, shared_secret: str) -> list[dict]:
    def tool(name: str, description: str, parameters: dict, *, timeout_ms: int, filler: str) -> dict:
        return {
            "type": "custom",
            "name": name,
            "description": description,
            "url": f"{base_url}/tools/{name}",
            "method": "POST",
            "headers": {"X-Tool-Secret": shared_secret},
            "parameters": parameters,
            "speak_during_execution": True,
            "execution_message_type": "static_text",
            "execution_message_description": filler,
            "speak_after_execution": True,
            "timeout_ms": timeout_ms,
            "enable_typing_sound": False,
        }

    return [
        tool(
            "search_availability",
            "Search LIVE open appointment slots across practitioners and branches. Call this EVERY time "
            "before mentioning availability — earlier results go stale within minutes. Call it again "
            "whenever the caller changes day, time, branch, practitioner, or appointment type. "
            "For 'earliest possible' requests set earliest_available=true and branch='any'.",
            {
                "type": "object",
                "properties": {
                    "branch": {
                        "type": "string",
                        "enum": ["medax", "arc", "any"],
                        "description": "medax = Bannerghatta Road branch, arc = Wilson Garden branch. Use 'any' unless the caller named a branch.",
                    },
                    "appointment_type": {
                        "type": "string",
                        "description": "One of: initial_assessment, followup_session, sports_rehab, paediatric_physio. Default initial_assessment.",
                    },
                    "practitioner_preference": {
                        "type": "string",
                        "description": "Practitioner name, only if the caller asked for someone specific.",
                    },
                    "date_from": {"type": "string", "description": "Search window start, YYYY-MM-DD (clinic local). Compute from the current date in your context."},
                    "date_to": {"type": "string", "description": "Search window end, YYYY-MM-DD. For a single day use the same date. Keep windows to 7 days or less."},
                    "weekday_mask": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["mon", "tue", "wed", "thu", "fri", "sat"]},
                        "description": "Only when the caller prefers certain weekdays ('Mondays and Wednesdays work').",
                    },
                    "part_of_day": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["morning", "afternoon", "evening"]},
                        "description": "morning = before 12, afternoon = 12-5, evening = after 5.",
                    },
                    "time_earliest": {"type": "string", "description": "Earliest acceptable time HH:MM 24h, e.g. '16:30' for 'after four thirty'."},
                    "time_latest": {"type": "string", "description": "Latest acceptable start time HH:MM 24h."},
                    "earliest_available": {"type": "boolean", "description": "true when the caller wants the soonest slot anywhere."},
                    "max_results": {"type": "integer", "description": "How many options to fetch. Default 3."},
                },
                "required": ["date_from", "date_to"],
            },
            timeout_ms=12000,
            filler="One moment, let me check the schedule for you.",
        ),
        tool(
            "book_appointment",
            "Book a specific slot returned by search_availability. Call ONLY after the caller said yes to "
            "one exact slot AND you have their full name (first and last). The backend re-checks the slot "
            "live; if it returns status 'conflict', apologize briefly and offer the returned alternatives.",
            {
                "type": "object",
                "properties": {
                    "slot_id": {"type": "string", "description": "slot_id of the chosen slot from search_availability."},
                    "patient_full_name": {"type": "string", "description": "Caller-confirmed FULL name, first and last."},
                    "patient_phone": {"type": "string", "description": "Caller's mobile number. Only needed if their phone is not already known from caller ID."},
                },
                "required": ["slot_id", "patient_full_name"],
            },
            timeout_ms=15000,
            filler="Booking that for you now, one moment please.",
        ),
        tool(
            "reschedule_appointment",
            "Move the caller's existing upcoming appointment to a new slot found via search_availability. "
            "The response includes fee_applies — mention a fee ONLY if it is true.",
            {
                "type": "object",
                "properties": {
                    "new_slot_id": {"type": "string", "description": "slot_id of the new slot from search_availability."},
                    "patient_name": {"type": "string", "description": "Which patient, when multiple share the phone number."},
                    "patient_phone": {"type": "string", "description": "Only if caller's phone is not known from caller ID."},
                },
                "required": ["new_slot_id"],
            },
            timeout_ms=15000,
            filler="Let me move that appointment for you.",
        ),
        tool(
            "cancel_appointment",
            "Cancel the caller's upcoming appointment. Confirm they really want to cancel first. "
            "The response includes fee_applies — mention a fee ONLY if it is true.",
            {
                "type": "object",
                "properties": {
                    "patient_name": {"type": "string", "description": "Which patient, when multiple share the phone number."},
                    "patient_phone": {"type": "string", "description": "Only if caller's phone is not known from caller ID."},
                },
                "required": [],
            },
            timeout_ms=10000,
            filler="Cancelling that for you, one moment.",
        ),
        tool(
            "get_patient_record",
            "Look up patients and upcoming appointments for a phone number. Use when the caller mentions an "
            "existing appointment that is not already in your call context, or gives a different number.",
            {
                "type": "object",
                "properties": {
                    "patient_phone": {"type": "string", "description": "Phone number to look up. Omit to use caller ID."},
                    "patient_name": {"type": "string", "description": "Patient name if given."},
                },
                "required": [],
            },
            timeout_ms=8000,
            filler="Let me pull that up.",
        ),
        tool(
            "log_followup_request",
            "Log that a human staff member must call the patient back. Use for: caller insists on a human, "
            "medical/clinical questions, complaints, or anything outside booking. After calling this, tell "
            "the caller someone will ring them back — never claim a live transfer.",
            {
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "description": "Short reason for the follow-up."},
                    "urgency": {"type": "string", "enum": ["normal", "urgent"], "description": "urgent only for clinical concerns."},
                    "callback_number": {"type": "string", "description": "Number to call back, if different from caller ID."},
                    "patient_name": {"type": "string"},
                },
                "required": ["reason"],
            },
            timeout_ms=8000,
            filler="Let me note that down for the team.",
        ),
    ]
