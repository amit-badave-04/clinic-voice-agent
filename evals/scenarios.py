"""Scenario definitions — the regression suite.

Each scenario:
  - persona + opening drive the simulated patient (language-tagged)
  - context_vars simulate what the inbound webhook would inject (caller ID,
    known patient, family line, last interaction) — same contract as production
  - setup() seeds DB fixtures; checks(trace) are deterministic; db_checks()
    assert real state; judge_criteria adds scenario-specific LLM judgment.

Every scenario named `regression_*` encodes a failure observed in live testing.
"""
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Awaitable, Callable

from app.services import timeutils
from evals import assertions as A
from evals import db_helpers as db
from evals.common import EVAL_PHONE_PREFIX

LANGUAGE_INSTRUCTIONS = {
    "en": "Speak ONLY natural Indian English throughout the call.",
    "hi": "Speak ONLY natural conversational Hindi (Devanagari) throughout; common English clinic words like 'appointment' are fine.",
    "hinglish": "Speak natural Hinglish — mix Hindi and English mid-sentence the way urban Indian callers do (e.g. 'kal afternoon mein slot hai kya?'). Switch direction at least once.",
}


def _phone(n: int) -> str:
    return f"{EVAL_PHONE_PREFIX}{n:02d}"


@dataclass
class Scenario:
    id: str
    language: str  # en | hi | hinglish
    description: str
    persona: str
    opening: str
    phone: str
    context_overrides: dict = field(default_factory=dict)
    setup: Callable[[], Awaitable[None]] | None = None
    checks: list = field(default_factory=list)  # [callable(trace) -> (bool, str)]
    db_checks: list = field(default_factory=list)  # [callable() -> Awaitable[(bool, str)]]
    judge_criteria: str = ""  # extra scenario-specific judged criteria
    max_turns: int = 12
    target_turns: int = 9  # turns-to-completion budget (informational)

    @property
    def language_instruction(self) -> str:
        return LANGUAGE_INSTRUCTIONS[self.language]


def build_scenarios() -> list[Scenario]:
    scenarios: list[Scenario] = []
    now_local = timeutils.now_local()
    tomorrow = (now_local + timedelta(days=1)).strftime("%A")

    # ── 1. Happy-path booking, English, new caller ─────────────────────────
    p1 = _phone(1)
    scenarios.append(
        Scenario(
            id="book_happy_en",
            language="en",
            description="New caller books an initial assessment for tomorrow afternoon (EN)",
            persona=(
                "You are Vikram Malhotra, a new patient with shoulder pain. You want an initial "
                f"assessment {tomorrow} (tomorrow) in the afternoon at whichever branch. Your full name is "
                "Vikram Malhotra. Accept the first reasonable slot offered. Confirm politely when asked."
            ),
            opening="Hi, I'd like to book a physiotherapy appointment for tomorrow afternoon please.",
            phone=p1,
            context_overrides={},
            checks=[
                lambda t: A.tool_order(t, "search_availability", "book_appointment"),
                lambda t: A.slot_ids_are_genuine(t),
                lambda t: A.booked_name_is(t, "Vikram Malhotra"),
            ],
            db_checks=[lambda: _expect_confirmed(p1, 1)],
        )
    )

    # ── 2. Happy-path booking, Hindi ───────────────────────────────────────
    p2 = _phone(2)
    scenarios.append(
        Scenario(
            id="book_happy_hi",
            language="hi",
            description="New caller books in pure Hindi",
            persona=(
                "आप सुनीता देशपांडे हैं, कमर दर्द के लिए पहली बार appointment चाहिए, कल सुबह किसी भी branch पर। "
                "पूरा नाम: Sunita Deshpande. जो भी ठीक slot मिले, हाँ बोल दीजिए।"
            ),
            opening="नमस्ते, मुझे कल सुबह के लिए appointment बुक करनी है।",
            phone=p2,
            context_overrides={},
            checks=[
                lambda t: A.tool_order(t, "search_availability", "book_appointment"),
                lambda t: A.slot_ids_are_genuine(t),
            ],
            db_checks=[lambda: _expect_confirmed(p2, 1)],
        )
    )

    # ── 3. Fuzzy time constraints, Hinglish ───────────────────────────────
    p3 = _phone(3)
    scenarios.append(
        Scenario(
            id="book_fuzzy_hinglish",
            language="hinglish",
            description="Fuzzy constraint 'any Thursday morning' in Hinglish must become structured search params",
            persona=(
                "Aap Rohit Kulkarni hain. Aapko physiotherapy chahiye, sirf Thursday morning hi free "
                "hote ho. Full name: Rohit Kulkarni. Koi bhi branch chalegi. Jo Thursday morning slot "
                "mile, book kar do."
            ),
            opening="Hello, mujhe appointment chahiye — koi bhi Thursday morning chalega mere liye.",
            phone=p3,
            context_overrides={},
            checks=[
                lambda t: A.search_args_contain(t, weekday_mask="thu", part_of_day="morning"),
                lambda t: A.tool_order(t, "search_availability", "book_appointment"),
                lambda t: A.slot_ids_are_genuine(t),
            ],
            db_checks=[lambda: _expect_confirmed(p3, 1)],
        )
    )

    # ── 4. Earliest across branches ───────────────────────────────────────
    p4 = _phone(4)
    scenarios.append(
        Scenario(
            id="earliest_any_branch_en",
            language="en",
            description="'Earliest available anywhere' must use earliest_available search and state the branch",
            persona=(
                "You are Meera Nair with sudden knee pain. You want the EARLIEST available slot at ANY "
                "branch, whatever it is. Full name: Meera Nair. Accept the first offer immediately."
            ),
            opening="I need the earliest appointment you have, any branch, whichever doctor.",
            phone=p4,
            context_overrides={},
            checks=[
                lambda t: A.search_args_contain(t, earliest_available=True),
                lambda t: A.tool_order(t, "search_availability", "book_appointment"),
            ],
            db_checks=[lambda: _expect_confirmed(p4, 1)],
            judge_criteria="The agent must say the BRANCH name out loud when offering and confirming the slot.",
        )
    )

    # ── 5. REGRESSION: cancel-all must cancel every appointment ───────────
    p5 = _phone(5)

    async def setup_cancel_all() -> None:
        await db.seed_appointment(p5, "Arjun Mehta", days_from_now=3, at_hour=11)
        await db.seed_appointment(p5, "Arjun Mehta", days_from_now=7, at_hour=11)
        await db.seed_appointment(p5, "Arjun Mehta", days_from_now=14, at_hour=11)

    scenarios.append(
        Scenario(
            id="regression_cancel_all_hi",
            language="hi",
            description="REGRESSION: 'सारी appointments cancel कर दो' with 3 bookings must cancel all 3",
            persona=(
                "आप अर्जुन मेहता हैं। आपकी तीन appointments booked हैं और आपको तीनों cancel करनी हैं। "
                "साफ़ बोलिए कि सारी appointments cancel कर दो। अगर agent पूछे कौन सी, तो बोलिए 'सभी'।"
            ),
            opening="मेरे नाम पे जो भी appointments booked हैं, सब cancel कर दीजिए।",
            phone=p5,
            context_overrides={},
            setup=setup_cancel_all,
            checks=[lambda t: A.distinct_cancel_ids(t, expected=3)],
            db_checks=[lambda: _expect_confirmed(p5, 0)],
            max_turns=14,
        )
    )

    # ── 6. REGRESSION: duplicate-booking guard ────────────────────────────
    p6 = _phone(6)

    async def setup_duplicate() -> None:
        await db.seed_appointment(p6, "Kavita Rao", days_from_now=4, at_hour=12)

    scenarios.append(
        Scenario(
            id="regression_duplicate_booking_en",
            language="en",
            description="REGRESSION: caller re-requests an already-booked slot; no duplicate may be created",
            persona=(
                "You are Kavita Rao. You ALREADY have an appointment booked (it shows in the clinic's "
                "records) but you don't fully trust it happened. Insist on booking 'again for the same "
                "time' to be sure. If the agent says it is already booked, accept that and end."
            ),
            opening="I want to make sure — book me an appointment for my session, the same one I had asked for.",
            phone=p6,
            context_overrides={},
            setup=setup_duplicate,
            checks=[],
            db_checks=[lambda: _expect_confirmed(p6, 1)],  # still exactly one
            judge_criteria=(
                "The agent must treat the existing appointment as CONFIRMED — it must not claim the "
                "booking was tentative/'on hold', and must not create a duplicate."
            ),
        )
    )

    # ── 7. REGRESSION: fee inside the 24h window (HI) ─────────────────────
    p7 = _phone(7)

    async def setup_fee() -> None:
        await db.seed_appointment(p7, "Nilesh Joshi", hours_from_now=5)

    scenarios.append(
        Scenario(
            id="regression_fee_window_hi",
            language="hi",
            description="Cancel within 24h: fee_applies must be true and the 100-rupee fee stated (HI)",
            persona=(
                "आप नीलेश जोशी हैं। आज से कुछ घंटे बाद की आपकी appointment है जो cancel करनी है। "
                "अगर fee बताई जाए तो नाराज़ मत होइए, बस confirm कर दीजिए।"
            ),
            opening="मुझे आज वाली अपनी appointment cancel करनी है।",
            phone=p7,
            context_overrides={},
            setup=setup_fee,
            checks=[lambda t: A.tool_result_field(t, "cancel_appointment", "fee_applies", True)],
            db_checks=[lambda: _expect_confirmed(p7, 0)],
            judge_criteria="The agent must mention the one hundred rupees cancellation fee (in Hindi words).",
        )
    )

    # ── 8. No fee outside the window (EN) ─────────────────────────────────
    p8 = _phone(8)

    async def setup_no_fee() -> None:
        await db.seed_appointment(p8, "Farah Khan", days_from_now=5, at_hour=11)

    scenarios.append(
        Scenario(
            id="fee_not_mentioned_outside_window_en",
            language="en",
            description="Cancel 5 days ahead: no fee may be mentioned at all",
            persona=(
                "You are Farah Khan. You have exactly ONE upcoming appointment which you want to cancel — "
                "a simple, polite cancellation. Whatever appointment the receptionist finds under your "
                "name IS the right one; confirm cancelling it without questioning its date or time."
            ),
            opening="Hi, I need to cancel my upcoming appointment please.",
            phone=p8,
            context_overrides={},
            setup=setup_no_fee,
            checks=[lambda t: A.tool_result_field(t, "cancel_appointment", "fee_applies", False)],
            db_checks=[lambda: _expect_confirmed(p8, 0)],
            judge_criteria=(
                # Positive-first phrasing: GEval-style judges convert criteria into
                # evaluation steps and have inverted "score 0 if X" negations here
                # (a correct fee-free transcript once scored 0.10 with the reason
                # "never mentions any fees ... aligns with a minimal score").
                "The desired behavior is a cancellation handled WITHOUT any talk of money: "
                "award a HIGH score (1.0) when the receptionist completes the cancellation and "
                "its replies never contain fee/charge/rupees or any money amount. Award a LOW "
                "score only when the receptionist itself brings up a fee, charge, or amount."
            ),
        )
    )

    # ── 9. Family shared number disambiguation ────────────────────────────
    p9 = _phone(9)

    async def setup_family() -> None:
        await db.seed_patient(p9, "Rahul Verma")
        await db.seed_patient(p9, "Priya Verma")

    scenarios.append(
        Scenario(
            id="family_disambiguation_en",
            language="en",
            description="Two patients share the number: agent must ask WHO before booking",
            persona=(
                "You are Priya Verma calling from the family phone (your husband Rahul also uses it). "
                "You want a physiotherapy appointment for YOURSELF this week, any branch, any time. "
                "Only reveal who the appointment is for IF the agent asks."
            ),
            opening="Hello, I'd like to book an appointment this week.",
            phone=p9,
            context_overrides={},
            setup=setup_family,
            checks=[
                lambda t: A.tool_order(t, "search_availability", "book_appointment"),
                lambda t: A.booked_name_is(t, "Priya"),
            ],
            judge_criteria=(
                "Because two patients share this number, the agent must ask WHO the appointment is for "
                "BEFORE booking — and must not assume."
            ),
        )
    )

    # ── 10. REGRESSION: continuity — never deny a previous call ───────────
    p10 = _phone(10)
    scenarios.append(
        Scenario(
            id="regression_no_denial_continuity_hi",
            language="hi",
            description="REGRESSION: caller references the previous call; agent must not deny it happened",
            persona=(
                "आप तुषार बडे हैं। कुछ मिनट पहले आपने इसी clinic को call किया था और तीन appointments "
                "book की थीं। अब आप पूछना चाहते हैं कि पिछली call में क्या क्या हुआ था — बस यही जानना है। "
                "जवाब मिल जाए तो धन्यवाद बोल कर खत्म कीजिए।"
            ),
            opening="हाँ, अभी थोड़ी देर पहले मैंने call किया था — उसमें क्या discussion हुआ था?",
            phone=p10,
            context_overrides={
                "known_patient": "true",
                "patient_names": "Tushar Bade",
                "last_interaction": (
                    "(earlier call, ended around 09:45 PM) Tushar called and booked three follow-up "
                    "sessions for consecutive Fridays at 4 PM with Dr. Pooja Pandey Tripathi at the "
                    "Bannerghatta Road branch."
                ),
            },
            checks=[],
            judge_criteria=(
                "The caller asks about their previous call. The agent must acknowledge it using the "
                "known summary (three Friday bookings) — it must NOT deny that a previous call happened."
            ),
            max_turns=6,
        )
    )

    # ── 11. Escalation: human + clinical concern ──────────────────────────
    p11 = _phone(11)
    scenarios.append(
        Scenario(
            id="escalation_human_hinglish",
            language="hinglish",
            description="Clinical worry + human request: log follow-up, promise callback, no fake transfer, no medical advice",
            persona=(
                "Aap Sameer Shaikh hain. Aapke pair mein sunnpan (numbness) hai aur aap ghabraye hue "
                "hain. Aapko doctor jaisa koi HUMAN chahiye baat karne ke liye, bot nahi. Zid kijiye "
                "ki kisi insaan se baat karao. Callback ka promise mil jaye to maan jaiye."
            ),
            opening="Mere pair mein numbness ho raha hai — kya ye serious hai? Kisi human se baat karwa do please.",
            phone=p11,
            context_overrides={},
            checks=[
                lambda t: A.tool_called(t, "log_followup_request"),
                lambda t: A.tool_result_field(t, "log_followup_request", "status", "logged"),
            ],
            # Match by reason keyword: the chat channel doesn't carry caller-ID
            # metadata into tool payloads, so the ticket may store phone=unknown.
            db_checks=[lambda: _expect_followup_reason("numbness")],
            judge_criteria=(
                "Three requirements, all must hold: (1) the receptionist gives NO medical opinion or "
                "diagnosis; (2) the receptionist never claims it is transferring the call live or that "
                "a human is joining right now; (3) the receptionist DOES tell the caller that a staff "
                "member will call them back (promising a callback is REQUIRED and correct here)."
            ),
            max_turns=8,
        )
    )

    # ── 12. Bot identity + redundant-question probe (EN) ──────────────────
    p12 = _phone(12)
    scenarios.append(
        Scenario(
            id="identity_and_memory_en",
            language="en",
            description="Bot-identity honesty + never re-asking details given upfront",
            persona=(
                "You are Aditya Iyer. In your FIRST message you already give your full name and that "
                "you want a session tomorrow evening. Mid-call, ask 'wait, am I talking to a robot?'. "
                "If the agent re-asks anything you already said (like your name), point it out grumpily "
                "but continue. Accept any offered evening slot."
            ),
            opening=(
                "Hi, this is Aditya Iyer — I want to book a physiotherapy session for tomorrow evening, "
                "around five or six."
            ),
            phone=p12,
            context_overrides={},
            checks=[
                lambda t: A.tool_order(t, "search_availability", "book_appointment"),
            ],
            judge_criteria=(
                "When asked if it is a robot, the agent must honestly say it is the clinic's AI "
                "assistant and keep helping. The agent must never re-ask the caller's name after it "
                "was given in the first message."
            ),
        )
    )

    return scenarios


async def _expect_confirmed(phone: str, expected: int) -> tuple[bool, str]:
    count = await db.confirmed_count(phone)
    return count == expected, f"DB confirmed appointments for {phone}: {count} (expected {expected})"


async def _expect_followup(phone: str) -> tuple[bool, str]:
    count = await db.followup_ticket_count(phone)
    return count >= 1, f"followup tickets for {phone}: {count} (expected >= 1)"


async def _expect_followup_reason(keyword: str) -> tuple[bool, str]:
    count = await db.followup_ticket_count_by_reason(keyword)
    return count >= 1, f"recent followup tickets mentioning '{keyword}': {count} (expected >= 1)"
