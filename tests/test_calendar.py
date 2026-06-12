"""_parse_ical_buddy + _annotate_now_next — the ical-buddy output pipeline."""
from datetime import datetime, timedelta

from daemon import _annotate_now_next, _parse_ical_buddy

SAMPLE = """\
Standup
    2026-05-15 at 09:30 - 09:45
Focus block
    2026-05-15 at 10:00 - 12:00
Company offsite
    2026-05-15
Conference
    2026-05-15 at 09:00 - 2026-05-17 at 17:00
"""


def test_parse_timed_event():
    events = _parse_ical_buddy(SAMPLE)
    assert events[0] == {
        "title": "Standup",
        "start_date": "2026-05-15",
        "end_date": "2026-05-15",
        "start_time": "09:30",
        "end_time": "09:45",
        "is_all_day": False,
        "start": "09:30",
        "is_now": False,
        "is_next": False,
    }


def test_parse_all_day_event():
    offsite = _parse_ical_buddy(SAMPLE)[2]
    assert offsite["title"] == "Company offsite"
    assert offsite["is_all_day"] is True
    assert offsite["start"] == "ALL"
    assert offsite["start_time"] is None


def test_parse_multi_day_event():
    conf = _parse_ical_buddy(SAMPLE)[3]
    assert conf["start_date"] == "2026-05-15"
    assert conf["end_date"] == "2026-05-17"
    assert conf["start_time"] == "09:00"
    assert conf["end_time"] == "17:00"


def test_parse_strips_bullets_and_skips_dateless_lines():
    out = _parse_ical_buddy(
        "• Bulleted title\n"
        "    2026-01-02 at 08:00 - 09:00\n"
        "Orphan title\n"
        "    no dates on this line\n"
    )
    assert [e["title"] for e in out] == ["Bulleted title"]


def test_parse_indented_line_without_title_is_ignored():
    assert _parse_ical_buddy("    2026-01-02 at 08:00 - 09:00\n") == []


def _timed(start_dt: datetime, end_dt: datetime) -> dict:
    return {
        "title": "x",
        "start_date": start_dt.strftime("%Y-%m-%d"),
        "end_date": end_dt.strftime("%Y-%m-%d"),
        "start_time": start_dt.strftime("%H:%M"),
        "end_time": end_dt.strftime("%H:%M"),
        "is_all_day": False,
        "start": start_dt.strftime("%H:%M"),
        "is_now": False,
        "is_next": False,
    }


def test_annotate_now_and_next():
    now = datetime.now()
    current = _timed(now - timedelta(hours=1), now + timedelta(hours=1))
    soon = _timed(now + timedelta(hours=2), now + timedelta(hours=3))
    later = _timed(now + timedelta(hours=4), now + timedelta(hours=5))
    all_day = {**_timed(now, now), "is_all_day": True, "start_time": None, "end_time": None, "start": "ALL"}

    events = [current, soon, later, all_day]
    _annotate_now_next(events)

    assert current["is_now"] and not current["is_next"]
    assert soon["is_next"] and not soon["is_now"]
    assert not later["is_now"] and not later["is_next"]
    assert not all_day["is_now"] and not all_day["is_next"]
