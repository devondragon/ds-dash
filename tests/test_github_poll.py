"""github_poll — per-call fault tolerance and the last-known-state merge.

Runs exactly one loop iteration by patching asyncio.sleep to raise; the
five HTTP endpoints are mocked with respx.
"""
import httpx
import pytest
import respx

import daemon
from daemon import GITHUB_API, GITHUB_GRAPHQL, STATE, github_poll


class StopLoop(Exception):
    pass


async def _stop_sleep(_seconds):
    raise StopLoop


def _search_json(n: int) -> dict:
    return {"total_count": n, "items": []}


def _empty_cc() -> dict:
    return {"data": {"user": {"contributionsCollection": {}}}}


@respx.mock
async def test_partial_failure_keeps_previous_section_data(monkeypatch):
    monkeypatch.setattr(daemon.asyncio, "sleep", _stop_sleep)
    # previous good state that the failing heatmap call must not clobber
    STATE["providers"]["github"] = {
        "status": "ok",
        "heatmap": {"total_year": 42, "recent_days": []},
        "commits_today": 3,
    }

    respx.get(f"{GITHUB_API}/search/issues").mock(return_value=httpx.Response(200, json=_search_json(2)))
    respx.get(f"{GITHUB_API}/users/octo/events").mock(return_value=httpx.Response(200, json=[]))
    # first graphql call (recent activity) succeeds, second (heatmap) blows up
    respx.post(GITHUB_GRAPHQL).mock(side_effect=[
        httpx.Response(200, json=_empty_cc()),
        httpx.Response(500, text="boom"),
    ])

    with pytest.raises(StopLoop):
        await github_poll("tok", "octo", interval=1)

    gh = STATE["providers"]["github"]
    assert gh["status"] == "error"
    assert "graphql-heatmap" in gh["error"]
    assert "HTTP 500" in gh["error"]
    # fresh sections landed
    assert gh["review_requested"]["count"] == 2
    assert gh["recent_events"] == {"items": []}
    # failed section retained its previous data
    assert gh["heatmap"]["total_year"] == 42
    assert gh["commits_today"] == 3


@respx.mock
async def test_clean_poll_sets_ok_and_clears_error(monkeypatch):
    monkeypatch.setattr(daemon.asyncio, "sleep", _stop_sleep)
    STATE["providers"]["github"] = {"status": "error", "error": "old failure"}

    heatmap_cc = {
        "contributionCalendar": {
            "totalContributions": 5,
            "weeks": [{"contributionDays": [
                {"date": "2026-06-11", "contributionCount": 5, "weekday": 4},
            ]}],
        }
    }
    respx.get(f"{GITHUB_API}/search/issues").mock(return_value=httpx.Response(200, json=_search_json(0)))
    respx.get(f"{GITHUB_API}/users/octo/events").mock(return_value=httpx.Response(200, json=[]))
    respx.post(GITHUB_GRAPHQL).mock(side_effect=[
        httpx.Response(200, json=_empty_cc()),
        httpx.Response(200, json={"data": {"user": {"contributionsCollection": heatmap_cc}}}),
    ])

    with pytest.raises(StopLoop):
        await github_poll("tok", "octo", interval=1)

    gh = STATE["providers"]["github"]
    assert gh["status"] == "ok"
    assert "error" not in gh
    assert gh["heatmap"]["total_year"] == 5
    assert gh["username"] == "octo"


@respx.mock
async def test_graphql_error_payload_is_caught(monkeypatch):
    monkeypatch.setattr(daemon.asyncio, "sleep", _stop_sleep)
    STATE["providers"]["github"] = {"status": "pending"}

    respx.get(f"{GITHUB_API}/search/issues").mock(return_value=httpx.Response(200, json=_search_json(0)))
    respx.get(f"{GITHUB_API}/users/octo/events").mock(return_value=httpx.Response(200, json=[]))
    respx.post(GITHUB_GRAPHQL).mock(side_effect=[
        httpx.Response(200, json={"errors": [{"message": "rate limited"}]}),
        httpx.Response(200, json=_empty_cc()),
    ])

    with pytest.raises(StopLoop):
        await github_poll("tok", "octo", interval=1)

    gh = STATE["providers"]["github"]
    assert gh["status"] == "error"
    assert "graphql-activity" in gh["error"]
    assert "rate limited" in gh["error"]
    # the other sections still landed
    assert gh["review_requested"]["count"] == 0
