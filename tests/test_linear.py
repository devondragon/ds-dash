"""Linear issue shaping, sort order, and active-cycle selection."""
from datetime import datetime, timedelta, timezone

from daemon import _linear_active_cycle, _linear_sort_key, _linear_to_item


def _item(pri: int) -> dict:
    return _linear_to_item({"priority": pri, "state": {}, "team": {}})


def test_priority_display_buckets():
    # urgent (1) and high (2) collapse to "high" for display…
    assert _item(1)["priority"] == "high"
    assert _item(2)["priority"] == "high"
    assert _item(3)["priority"] == "med"
    assert _item(4)["priority"] == "low"
    assert _item(0)["priority"] == "none"
    # …but urgent still sorts ahead of high
    assert _item(1)["_pri_rank"] < _item(2)["_pri_rank"]


def test_sort_orders_priority_then_due_with_none_last():
    urgent = _linear_to_item({"priority": 1, "dueDate": "2026-07-01", "state": {}, "team": {}})
    med_dated = _linear_to_item({"priority": 3, "dueDate": "2026-01-01", "state": {}, "team": {}})
    med_undated = _linear_to_item({"priority": 3, "dueDate": None, "state": {}, "team": {}})
    assert sorted([med_undated, med_dated, urgent], key=_linear_sort_key) == [urgent, med_dated, med_undated]


def _cycle(start_h: float, end_h: float, number: int = 1, **extra) -> dict:
    now = datetime.now(timezone.utc)
    c = {
        "number": number,
        "startsAt": (now + timedelta(hours=start_h)).isoformat(),
        "endsAt": (now + timedelta(hours=end_h)).isoformat(),
        "progress": 0.5,
        "issueCountHistory": [10, 12],
        "completedIssueCountHistory": [3, 6],
    }
    c.update(extra)
    return c


def test_no_active_cycle():
    assert _linear_active_cycle([]) == {"present": False}
    assert _linear_active_cycle([_cycle(1, 48)]) == {"present": False}     # future
    assert _linear_active_cycle([_cycle(-48, -1)]) == {"present": False}   # past


def test_active_cycle_shape():
    out = _linear_active_cycle([_cycle(-24, 30)])
    assert out["present"] is True
    assert out["number"] == 1
    assert out["name"] == "Cycle 1"           # default when unnamed
    assert out["progress_pct"] == 50
    assert out["completed"] == 6
    assert out["total"] == 12
    assert out["ends_in_days"] == 2           # 30h away → ceil to 2 days
    assert out["multi_team"] is False


def test_soonest_ending_cycle_wins_and_flags_multi_team():
    out = _linear_active_cycle([_cycle(-24, 100, number=1), _cycle(-24, 30, number=2)])
    assert out["number"] == 2
    assert out["multi_team"] is True


def test_unparseable_cycle_dates_are_skipped():
    assert _linear_active_cycle([{"startsAt": "garbage", "endsAt": None}]) == {"present": False}
