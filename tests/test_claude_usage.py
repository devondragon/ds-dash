"""_fetch_claude_usage_headers — rate-limit header parsing, incl. 429 handling."""
import time

import httpx
import pytest
import respx

from daemon import _CLAUDE_USAGE_URL, _fetch_claude_usage_headers


def _headers(five_util: str = "0.42", week_util: str = "0.07",
             five_reset: float | None = None, week_reset: float | None = None) -> dict:
    now = time.time()
    return {
        "anthropic-ratelimit-unified-5h-utilization": five_util,
        "anthropic-ratelimit-unified-5h-reset": str(five_reset if five_reset is not None else now + 3600),
        "anthropic-ratelimit-unified-7d-utilization": week_util,
        "anthropic-ratelimit-unified-7d-reset": str(week_reset if week_reset is not None else now + 86400),
    }


@respx.mock
async def test_parses_usage_headers():
    respx.post(_CLAUDE_USAGE_URL).mock(return_value=httpx.Response(200, headers=_headers(), json={}))
    out = await _fetch_claude_usage_headers("tok", "model")
    assert out["five_pct"] == 42
    assert out["weekly_pct"] == 7
    assert out["five_resets_at"] != "—"
    assert out["weekly_resets_at"] != "—"


@respx.mock
async def test_429_with_headers_is_parsed_not_raised():
    # A 429 is when the panel matters most — usage must render, not error out.
    respx.post(_CLAUDE_USAGE_URL).mock(
        return_value=httpx.Response(429, headers=_headers(five_util="1.0"), json={}))
    out = await _fetch_claude_usage_headers("tok", "model")
    assert out["five_pct"] == 100


@respx.mock
async def test_429_without_usage_headers_raises():
    respx.post(_CLAUDE_USAGE_URL).mock(return_value=httpx.Response(429, json={}))
    with pytest.raises(httpx.HTTPStatusError):
        await _fetch_claude_usage_headers("tok", "model")


@respx.mock
async def test_past_reset_zeroes_utilization():
    respx.post(_CLAUDE_USAGE_URL).mock(
        return_value=httpx.Response(200, headers=_headers(five_util="0.9", five_reset=time.time() - 10), json={}))
    out = await _fetch_claude_usage_headers("tok", "model")
    assert out["five_pct"] == 0


@respx.mock
async def test_missing_headers_render_as_dashes():
    respx.post(_CLAUDE_USAGE_URL).mock(return_value=httpx.Response(200, json={}))
    out = await _fetch_claude_usage_headers("tok", "model")
    assert out["five_pct"] == 0
    assert out["five_resets_at"] == "—"
    assert out["weekly_resets_at"] == "—"
