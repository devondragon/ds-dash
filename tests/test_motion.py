"""Motion task shaping, sort order, and recurring-occurrence dedup."""
from daemon import _motion_dedup_recurring, _motion_sort_key, _motion_to_item


def _task(task_id: str | None, name: str = "t", pri: str = "MEDIUM",
          due: str | None = None, parent: str | None = None) -> dict:
    t: dict = {"name": name, "priority": pri, "dueDate": due}
    if task_id is not None:
        t["id"] = task_id
    if parent:
        t["parentRecurringTaskId"] = parent
    return t


def test_to_item_maps_priority_strips_title_and_builds_url():
    it = _motion_to_item(_task("abc", name=" Pay rent ", pri="ASAP"))
    assert it["priority"] == "high"
    assert it["title"] == "Pay rent"
    assert it["url"] == "https://app.usemotion.com/web/?task=abc"
    assert it["source"] == "MOT"


def test_to_item_without_id_has_no_url():
    assert _motion_to_item({"name": "x"})["url"] == ""


def test_sort_key_priority_then_due_with_none_last():
    tasks = [
        _task("1", pri="LOW", due="2026-01-01"),
        _task("2", pri="ASAP", due="2026-12-01"),
        _task("3", pri="HIGH", due="2026-01-01"),
        _task("4", pri="HIGH"),  # undated HIGH sorts after dated HIGH
    ]
    assert [t["id"] for t in sorted(tasks, key=_motion_sort_key)] == ["2", "3", "4", "1"]


def test_dedup_keeps_earliest_recurring_instance():
    tasks = [
        _task("a1", parent="rec1", due="2026-06-20"),
        _task("a2", parent="rec1", due="2026-06-13"),
        _task("b", due="2026-06-15"),
        _task(None),  # no id, no parent — unkeyable, dropped
    ]
    out = _motion_dedup_recurring(tasks)
    assert [t.get("id") for t in out] == ["a2", "b"]  # sorted by due date (all MEDIUM)


def test_dedup_instance_without_due_loses_to_dated_one():
    tasks = [
        _task("a1", parent="rec1"),
        _task("a2", parent="rec1", due="2026-06-13"),
    ]
    assert [t["id"] for t in _motion_dedup_recurring(tasks)] == ["a2"]
