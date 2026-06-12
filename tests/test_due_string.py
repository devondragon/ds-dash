"""_due_string — the compact due-date display shared by Motion and Linear."""
from datetime import datetime, timedelta, timezone

from daemon import _due_string


def _iso_in(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def test_missing_or_unparseable_input():
    assert _due_string(None) == ""
    assert _due_string("") == ""
    assert _due_string("not-a-date") == ""


def test_near_term_buckets():
    assert _due_string(_iso_in(-1)) == "OVERDUE"
    assert _due_string(_iso_in(0)) == "TODAY"
    assert _due_string(_iso_in(1)) == "TMRW"
    assert _due_string(_iso_in(3)) == "3D"


def test_week_out_shows_weekday():
    due = datetime.now(timezone.utc) + timedelta(days=8)
    assert _due_string(due.isoformat()) == due.astimezone().strftime("%a").upper()


def test_far_out_shows_month_day():
    due = datetime.now(timezone.utc) + timedelta(days=30)
    assert _due_string(due.isoformat()) == due.astimezone().strftime("%b %d").upper()


def test_zulu_suffix_is_accepted():
    # The display converts to local time, so derive the expectation the same way.
    expected = datetime(2099, 1, 1, tzinfo=timezone.utc).astimezone().strftime("%b %d").upper()
    assert _due_string("2099-01-01T00:00:00Z") == expected
