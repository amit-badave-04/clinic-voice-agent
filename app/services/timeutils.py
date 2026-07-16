"""Timezone discipline in one place.

Rules (see ARCHITECTURE.md):
  - Postgres stores UTC timestamptz only.
  - Every human-facing computation ("today", "tomorrow", "morning") happens in
    the clinic's IANA timezone (Asia/Kolkata, UTC+5:30, no DST).
  - Never trust the server's local clock/timezone.
"""
from datetime import date, datetime, time, timedelta, timezone

from app.config import get_settings

settings = get_settings()

# Cliniko's day-part boundaries: morning < 12:00, afternoon 12:00-17:00, evening >= 17:00
PART_OF_DAY_WINDOWS = {
    "morning": (time(0, 0), time(12, 0)),
    "afternoon": (time(12, 0), time(17, 0)),
    "evening": (time(17, 0), time(23, 59, 59)),
}

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_local() -> datetime:
    return now_utc().astimezone(settings.tz)


def today_local() -> date:
    """The clinic's 'today' — NOT the server's. This is where the classic
    'same-day booking shifts to tomorrow' UTC bug lives."""
    return now_local().date()


def local_to_utc(dt_naive_local: datetime) -> datetime:
    """Interpret a naive datetime as clinic-local and convert to UTC."""
    return dt_naive_local.replace(tzinfo=settings.tz).astimezone(timezone.utc)


def utc_to_local(dt_utc: datetime) -> datetime:
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    return dt_utc.astimezone(settings.tz)


def speakable_datetime(dt_utc: datetime) -> str:
    """Human/TTS-friendly clinic-local rendering: 'Thursday, 16 July, 4:30 PM'.
    (Portable — %-d/%#d are platform-specific, so strip zeros manually.)"""
    local = utc_to_local(dt_utc)
    day = str(local.day)
    hour = local.strftime("%I").lstrip("0") or "12"
    return f"{local.strftime('%A')}, {day} {local.strftime('%B')}, {hour}:{local.strftime('%M %p')}"


def format_local(dt_utc: datetime, fmt: str = "%A %d %B %Y, %I:%M %p") -> str:
    return utc_to_local(dt_utc).strftime(fmt)


def current_datetime_prompt_string() -> str:
    """Injected into the agent prompt every call so 'today'/'tomorrow' resolve correctly."""
    local = now_local()
    return local.strftime("%A, %d %B %Y, %I:%M %p") + " IST"


def slot_matches_filters(
    slot_start_utc: datetime,
    weekday_mask: list[str] | None = None,
    part_of_day: list[str] | None = None,
    earliest: time | None = None,
    latest: time | None = None,
) -> bool:
    """Apply caller preferences ('Mondays and Wednesdays', 'afternoon', 'around 4:30')
    to a slot, computed in clinic-local time."""
    local = utc_to_local(slot_start_utc)
    if weekday_mask:
        if WEEKDAYS[local.weekday()] not in [w.lower()[:3] for w in weekday_mask]:
            return False
    if part_of_day:
        ok = False
        for part in part_of_day:
            window = PART_OF_DAY_WINDOWS.get(part.lower())
            if window and window[0] <= local.time() < window[1]:
                ok = True
                break
        if not ok:
            return False
    if earliest and local.time() < earliest:
        return False
    if latest and local.time() > latest:
        return False
    return True


def parse_time_hhmm(value: str | None) -> time | None:
    if not value:
        return None
    value = value.strip()
    for fmt in ("%H:%M", "%I:%M %p", "%I %p"):
        try:
            return datetime.strptime(value, fmt).time()
        except ValueError:
            continue
    return None


def daterange_days(date_from: date, date_to: date) -> list[tuple[date, date]]:
    """Split an arbitrary range into <=7-day windows (Cliniko available_times limit)."""
    windows = []
    cursor = date_from
    while cursor <= date_to:
        end = min(cursor + timedelta(days=6), date_to)
        windows.append((cursor, end))
        cursor = end + timedelta(days=1)
    return windows
