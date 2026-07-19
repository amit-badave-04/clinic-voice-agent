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
            # prompt-mode filler: generated in the language the caller is
            # currently speaking (static text played English fillers into
            # Hindi conversations — caught by the eval language judge).
            "execution_message_type": "prompt",
            "execution_message_description": (
                f"Say one very short natural holding phrase meaning '{filler}' in the language the "
                "caller is currently speaking (Hindi, English, or Hinglish to match the conversation). "
                "A few words only, no new information."
            ),
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
                        "description": "A practitioner's NAME (e.g. 'Gopika Nair') — only when the caller asked for a specific doctor by name. Never pass attributes like 'female' (all our physiotherapists are women; omit this field instead).",
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
            timeout_ms=10000,
            filler="one moment, let me check the schedule",
        ),
        tool(
            "book_appointment",
            "Book a specific slot returned by search_availability. Call ONLY after the caller said yes to "
            "one exact slot AND you have their full name (first and last). The backend re-checks the slot "
            "live; if it returns status 'conflict', apologize briefly and offer the returned alternatives. "
            "It also validates the name: status 'implausible_name' or 'need_name_confirmation' means "
            "resolve the name with the caller first, then call again (with name_confirmed true only if "
            "the caller insisted their unusual name is correct).",
            {
                "type": "object",
                "properties": {
                    "slot_id": {"type": "string", "description": "slot_id of the chosen slot from search_availability."},
                    "patient_full_name": {"type": "string", "description": "Caller-confirmed FULL name, first and last, in English (Latin) letters."},
                    "patient_phone": {"type": "string", "description": "Caller's mobile number. ALWAYS pass the caller's phone from Call context when it shows a real number (never ask for it in that case); ask the caller only when context shows 'unknown'."},
                    "name_confirmed": {"type": "boolean", "description": "true ONLY after the caller explicitly re-confirmed an unusual name, or rejected the suggested existing name."},
                },
                "required": ["slot_id", "patient_full_name"],
            },
            timeout_ms=12000,
            filler="one moment",
        ),
        tool(
            "reschedule_appointment",
            "Move ONE existing upcoming appointment to a new slot found via search_availability. "
            "If the caller has multiple upcoming appointments you MUST pass appointment_id (from the "
            "call context or get_patient_record). The response includes fee_applies — mention a fee "
            "ONLY if it is true.",
            {
                "type": "object",
                "properties": {
                    "new_slot_id": {"type": "string", "description": "slot_id of the new slot, copied EXACTLY from search_availability."},
                    "appointment_id": {"type": "string", "description": "appointment_id of the appointment being moved — required when the caller has more than one upcoming appointment."},
                    "patient_name": {"type": "string", "description": "Which patient, when multiple share the phone number."},
                    "patient_dob": {"type": "string", "description": "Patient's date of birth (YYYY-MM-DD) — required to act on an appointment when the number is shared by more than one patient."},
                    "patient_phone": {"type": "string", "description": "Pass the caller_phone from Call context when it shows a real number; ask only when context shows: unknown."},
                },
                "required": ["new_slot_id"],
            },
            timeout_ms=12000,
            filler="one moment",
        ),
        tool(
            "cancel_appointment",
            "Cancel ONE upcoming appointment. Confirm the caller really wants to cancel first. If the "
            "caller has multiple upcoming appointments you MUST pass appointment_id — and to cancel "
            "several, call this tool once PER appointment, each with its own appointment_id. The "
            "response includes fee_applies — mention a fee ONLY if it is true.",
            {
                "type": "object",
                "properties": {
                    "appointment_id": {"type": "string", "description": "appointment_id to cancel (from call context or get_patient_record) — required when the caller has more than one upcoming appointment."},
                    "patient_name": {"type": "string", "description": "Which patient, when multiple share the phone number."},
                    "patient_dob": {"type": "string", "description": "Patient's date of birth (YYYY-MM-DD) — required to act on an appointment when the number is shared by more than one patient."},
                    "patient_phone": {"type": "string", "description": "Pass the caller_phone from Call context when it shows a real number; ask only when context shows: unknown."},
                },
                "required": [],
            },
            timeout_ms=10000,
            filler="one moment",
        ),
        tool(
            "send_verification_code",
            "Send a six-digit verification code by SMS to the caller's number on file (never to any "
            "other number). Use when a tool answered 'verification_required', or before discussing "
            "existing appointments on an unverified call.",
            {"type": "object", "properties": {}, "required": []},
            timeout_ms=10000,
            filler="one moment",
        ),
        tool(
            "check_verification_code",
            "Check the six-digit code the caller entered on the keypad or read out. On 'verified', "
            "continue their request without re-verifying this call; on 'wrong_code', follow the "
            "response message.",
            {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "The six digits, e.g. '482913'."},
                },
                "required": ["code"],
            },
            timeout_ms=10000,
            filler="one moment",
        ),
        tool(
            "get_patient_record",
            "Look up the caller's upcoming appointments AFTER identity is verified on this call. The "
            "response is a single patient's record; if it returns 'need_patient_identification', more "
            "than one person uses this number — ask for the specific patient's full name AND date of "
            "birth, then call again with patient_name and patient_dob.",
            {
                "type": "object",
                "properties": {
                    "patient_phone": {"type": "string", "description": "Phone number to look up. Pass the caller_phone from Call context when it shows a real number; ask the caller only when context shows 'unknown'."},
                    "patient_name": {"type": "string", "description": "The specific patient's full name (required when the number has more than one patient)."},
                    "patient_dob": {"type": "string", "description": "The patient's date of birth as YYYY-MM-DD — required only when the number is shared by more than one patient."},
                },
                "required": [],
            },
            timeout_ms=6000,
            filler="let me pull that up",
        ),
        tool(
            "resolve_live_transfer",
            "Ask the backend whether a live transfer to clinic staff is possible RIGHT NOW "
            "(hours, channel, availability). Call this BEFORE offering or attempting any "
            "transfer, then follow the message in the response exactly.",
            {
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "description": "One sentence: who is calling and why they need a human."},
                },
                "required": ["reason"],
            },
            timeout_ms=6000,
            filler="one moment",
        ),
        {
            # Retell built-in warm transfer. Only used after resolve_live_transfer
            # approves; the destination comes from backend config via the
            # {{transfer_number}} dynamic variable, never from conversation.
            "type": "transfer_call",
            "name": "transfer_to_front_desk",
            "description": (
                "Warm-transfer the caller to clinic staff. Use ONLY after resolve_live_transfer "
                "answered allow_transfer_now true."
            ),
            "transfer_destination": {"type": "predefined", "number": "{{transfer_number}}"},
            "transfer_option": {
                "type": "warm_transfer",
                "agent_detection_timeout_ms": 20000,
                "on_hold_music": "relaxing_sound",
                "private_handoff_option": {
                    "type": "prompt",
                    "prompt": (
                        "Brief the staff member in two short sentences: caller name (if known), "
                        "their number, and what they need. Speak English."
                    ),
                },
                "public_handoff_option": {
                    "type": "static_message",
                    "message": "You're connected now — the front desk will take it from here.",
                },
            },
            "execution_message_type": "prompt",
            "execution_message_description": (
                "Say one short natural phrase meaning 'connecting you to the front desk, please "
                "hold' in the language the caller is currently speaking."
            ),
        },
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
            timeout_ms=6000,
            filler="let me note that down",
        ),
    ]
