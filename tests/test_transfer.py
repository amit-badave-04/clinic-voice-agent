"""Transfer-window logic (pure) — routing truth must live in code, not prompt."""
from datetime import datetime
from zoneinfo import ZoneInfo

from app.services.transfer import in_transfer_window

IST = ZoneInfo("Asia/Kolkata")


def _at(day: int, hour: int, minute: int = 0) -> datetime:
    # July 2026: the 20th is a Monday, the 25th a Saturday, the 26th a Sunday.
    return datetime(2026, 7, day, hour, minute, tzinfo=IST)


def test_weekday_business_hours_open():
    assert in_transfer_window(_at(20, 9, 0))
    assert in_transfer_window(_at(20, 18, 29))
    assert in_transfer_window(_at(25, 12, 0))  # Saturday is a working day


def test_closed_hours():
    assert not in_transfer_window(_at(20, 8, 59))
    assert not in_transfer_window(_at(20, 18, 30))
    assert not in_transfer_window(_at(20, 22, 0))


def test_sunday_closed_all_day():
    assert not in_transfer_window(_at(26, 11, 0))
