"""Real clinic data — Arogya Physiotherapy, Bengaluru (sourced, not invented).

Source: the clinic's own public website, snapshotted 16 July 2026:
  - https://arogyaphysiotherapy.com/team-new.html  (practitioners, timings)
  - https://arogyaphysiotherapy.com/contact.html   (branches, hours, fee)

This is an educational/portfolio demo. Not affiliated with, endorsed by, or
connected to Arogya Physiotherapy — see DISCLAIMER.md. Clinic phone numbers are
deliberately NOT reproduced.

Modeling notes (documented adaptations):
  - The clinic lists four locations; this demo models the two primary branches
    (Medax / Bannerghatta Road and Arc / Wilson Garden). Practitioners whose
    public timings reference the Jigani location have those blocks mapped onto
    the two modeled branches, preserving the source's split-day structure
    (e.g. Dr. Pooja genuinely works mornings at one location and 4-6 PM at
    Bannerghatta Road).
  - The public fee is ₹400 per consultation; all appointment types use it.
  - Sub-disciplines (ortho / neuro / paediatric / women's health) are modeled
    as appointment types ("departments"), matching the team's listed specialties.
"""

SNAPSHOT_DATE = "2026-07-16"
SOURCE_URLS = [
    "https://arogyaphysiotherapy.com/team-new.html",
    "https://arogyaphysiotherapy.com/contact.html",
]

BRANCHES = [
    {
        "key": "medax",
        "name": "Medax Arogya Physiotherapy, Bannerghatta Road",
        "address": "33/1 & 35/1, Kalena Agrahara, Bannerghatta Main Rd, Gottigere, Bengaluru 560076",
        "timezone": "Asia/Kolkata",
    },
    {
        "key": "arc",
        "name": "Arc Arogya Physiotherapy, Wilson Garden",
        "address": "3rd Floor, 210 Hombegowdanagar, Hosur Main Road, Wilson Garden, Bengaluru 560029",
        "timezone": "Asia/Kolkata",
    },
]

# Clinic hours (both branches): Mon-Sat 9:00-18:30, lunch 14:00-15:00, Sunday closed.
CLINIC_HOURS_NOTE = "Monday to Saturday, 9 AM to 6:30 PM; lunch 2 to 3 PM; closed Sunday."

# schedule = {branch_key: [(start, end), ...]} in clinic-local 24h times, Mon-Sat.
#
# `enabled` — the Cliniko 30-day trial caps ACTIVE practitioners at 5; the owner
# account must stay a practitioner for API access (Administrator + Practitioner),
# leaving 4 slots for doctors. Two roster members are therefore modeled but not
# activated on the trial. The kept four preserve every scenario the demo needs:
# both dual-branch doctors (cross-branch earliest-slot search) plus one
# single-branch doctor per location.
PRACTITIONERS = [
    {
        "name": "Dr. Pooja Pandey Tripathi",
        "specialties": ["women's health", "prenatal and postnatal care", "exercise therapy", "manual therapy"],
        "schedule": {"arc": [("10:30", "14:00")], "medax": [("16:00", "18:00")]},
        "enabled": True,
    },
    {
        "name": "Dr. Munesh Kumar Singh",
        "specialties": ["orthopaedics", "pain management", "dry needling", "cupping"],
        "schedule": {"medax": [("10:30", "14:00")], "arc": [("16:00", "18:00")]},
        "enabled": False,  # omitted on trial (5-active-practitioner cap)
    },
    {
        "name": "Dr. Gopika Nair",
        "specialties": ["orthopaedic manual therapy", "musculoskeletal", "pain management"],
        "schedule": {"medax": [("09:30", "14:00"), ("15:00", "18:00")]},
        "enabled": True,
    },
    {
        "name": "Dr. Anamika Lyngdoh",
        "specialties": ["paediatric physiotherapy", "geriatric care", "neurological rehabilitation"],
        "schedule": {"arc": [("11:00", "14:00"), ("15:00", "19:00")]},
        "enabled": True,
    },
    {
        "name": "Dr. Dilpreet Kaur",
        "specialties": ["musculoskeletal", "neurological rehabilitation", "cardiopulmonary physiotherapy"],
        "schedule": {"medax": [("09:30", "12:30")], "arc": [("15:00", "17:00")]},
        "enabled": True,
    },
    {
        "name": "Dr. Netaji D",
        "specialties": ["manual therapy", "general physiotherapy"],
        "schedule": {"arc": [("10:00", "14:00"), ("15:00", "19:00")]},
        "enabled": False,  # omitted on trial (5-active-practitioner cap)
    },
]

ENABLED_PRACTITIONERS = [p for p in PRACTITIONERS if p["enabled"]]

# duration told to the patient; buffer = charting/turnover gap enforced between
# appointments (Cliniko models both inside the appointment-type duration, so the
# Cliniko duration = duration + buffer).
APPOINTMENT_TYPES = [
    {
        "key": "initial_assessment",
        "name": "Initial Physiotherapy Assessment",
        "duration_minutes": 40,
        "buffer_minutes": 5,
        "fee_inr": 400,
    },
    {
        "key": "followup_session",
        "name": "Follow-up Physiotherapy Session",
        "duration_minutes": 30,
        "buffer_minutes": 15,
        "fee_inr": 400,
    },
    {
        "key": "sports_rehab",
        "name": "Sports Rehab Session",
        "duration_minutes": 45,
        "buffer_minutes": 15,
        "fee_inr": 400,
    },
    {
        "key": "paediatric_physio",
        "name": "Paediatric Physiotherapy",
        "duration_minutes": 30,
        "buffer_minutes": 10,
        "fee_inr": 400,
    },
]

CLINIC_POLICIES = {
    # A reschedule/cancellation fee applies only within this window before the
    # appointment (the agent must not mention fees outside the window).
    "change_fee_window_hours": "24",
    "change_fee_inr": "100",
}

# Demo patients (synthetic — NOT sourced from anywhere).
# Includes one family-shared-phone pair for the disambiguation scenario.
# date_of_birth is the per-patient verification factor for shared numbers
# (security/REVIEW.md M4): on the family line, DOB distinguishes the two
# co-tenants after the shared number is OTP-verified.
DEMO_PATIENTS = [
    {"full_name": "Rahul Sharma", "phone_e164": "+919000000001", "preferred_branch": "medax", "date_of_birth": "1988-04-12"},
    {"full_name": "Priya Sharma", "phone_e164": "+919000000001", "preferred_branch": "arc", "date_of_birth": "1991-11-03"},  # family line
    {"full_name": "Ananya Iyer", "phone_e164": "+919000000002", "preferred_branch": "arc", "date_of_birth": "1996-01-27"},
    {"full_name": "Mohammed Farhan", "phone_e164": "+919000000003", "preferred_branch": "medax", "date_of_birth": "1983-08-19"},
]
