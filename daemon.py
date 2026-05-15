#!/usr/bin/env python3
"""
cowork-dash: local dashboard daemon.

Polls configured providers on a schedule and exposes their state at
GET /api/state.json. Also serves a static HTML frontend at /.

For v0 we wire up GitHub for real and return mock data for the other
panels so the frontend layout is complete end-to-end. Replace each
`_mock_*` function with a real poller as we add providers.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import sys
import time
from datetime import datetime, timezone, timedelta

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover
    import tomli as tomllib  # type: ignore
from pathlib import Path
from typing import Any

import httpx
import psutil
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

# --------------------------------------------------------------------------- #
# Config + shared state
# --------------------------------------------------------------------------- #

CONFIG_PATH = Path(os.environ.get("COWORK_DASH_CONFIG", str(Path.home() / ".cowork-dash" / "config.toml")))
STATIC_DIR = Path(__file__).parent / "static"

# Everything the frontend renders lives in here. Each provider owns one
# top-level key under "providers" and writes a dict with at least {status,
# updated_at}. The frontend tolerates missing fields.
STATE: dict[str, Any] = {
    "started_at": datetime.now(timezone.utc).isoformat(),
    "providers": {
        "github": {"status": "pending"},
        "calendar": {"status": "pending"},
        "tasks": {"status": "pending"},
        "claude": {"status": "pending"},
        "services": {"status": "pending"},
        "system": {"status": "pending"},
    },
    "ticker": [],  # list of {ts, source, text, level}
}


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        print(f"[cowork-dash] no config at {CONFIG_PATH} — running with empty config", file=sys.stderr)
        return {}
    with CONFIG_PATH.open("rb") as f:
        return tomllib.load(f)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _push_ticker(source: str, text: str, level: str = "info") -> None:
    """Add an event to the rolling ticker (keeps the most recent 50)."""
    STATE["ticker"].insert(0, {"ts": _now_iso(), "source": source, "text": text, "level": level})
    del STATE["ticker"][50:]


# --------------------------------------------------------------------------- #
# GitHub provider (real)
# --------------------------------------------------------------------------- #

GITHUB_API = "https://api.github.com"
GITHUB_GRAPHQL = "https://api.github.com/graphql"

GRAPHQL_CONTRIBUTIONS = """
query($login: String!) {
  user(login: $login) {
    contributionsCollection {
      contributionCalendar {
        totalContributions
        weeks {
          contributionDays {
            date
            contributionCount
            weekday
          }
        }
      }
    }
  }
}
""".strip()


async def github_poll(token: str, username: str, interval: int) -> None:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "cowork-dash/0.1",
    }
    prev_total = None
    while True:
        result: dict[str, Any] = {"username": username, "updated_at": _now_iso()}
        try:
            async with httpx.AsyncClient(timeout=30, headers=headers) as c:
                # PRs awaiting my review
                r1 = await c.get(
                    f"{GITHUB_API}/search/issues",
                    params={"q": f"is:open is:pr review-requested:{username} archived:false", "per_page": 10},
                )
                r1.raise_for_status()
                d1 = r1.json()
                result["review_requested"] = {
                    "count": d1.get("total_count", 0),
                    "items": [
                        {
                            "title": i["title"],
                            "url": i["html_url"],
                            "repo": _repo_short(i.get("repository_url", "")),
                            "updated_at": i["updated_at"],
                            "number": i.get("number"),
                        }
                        for i in d1.get("items", [])[:6]
                    ],
                }

                # PRs I authored, still open
                r2 = await c.get(
                    f"{GITHUB_API}/search/issues",
                    params={"q": f"is:open is:pr author:{username} archived:false", "per_page": 1},
                )
                r2.raise_for_status()
                result["my_open_prs"] = {"count": r2.json().get("total_count", 0)}

                # Issues assigned to me
                r3 = await c.get(
                    f"{GITHUB_API}/search/issues",
                    params={"q": f"is:open is:issue assignee:{username} archived:false", "per_page": 1},
                )
                r3.raise_for_status()
                result["issues_assigned"] = {"count": r3.json().get("total_count", 0)}

                # Contribution heatmap via GraphQL
                r4 = await c.post(
                    GITHUB_GRAPHQL,
                    json={"query": GRAPHQL_CONTRIBUTIONS, "variables": {"login": username}},
                )
                r4.raise_for_status()
                d4 = r4.json()
                cal = (
                    d4.get("data", {}).get("user", {}).get("contributionsCollection", {}).get("contributionCalendar")
                )
                if cal:
                    weeks = cal.get("weeks", [])[-8:]
                    days = []
                    for w in weeks:
                        for d in w.get("contributionDays", []):
                            days.append({"date": d["date"], "count": d["contributionCount"]})
                    result["heatmap"] = {
                        "total_year": cal.get("totalContributions", 0),
                        "recent_days": days,
                    }
                    today_iso = datetime.now().strftime("%Y-%m-%d")
                    result["commits_today"] = next((d["count"] for d in days if d["date"] == today_iso), 0)
                    # Surface a delta to the ticker
                    if prev_total is not None and cal["totalContributions"] > prev_total:
                        diff = cal["totalContributions"] - prev_total
                        _push_ticker("github", f"+{diff} contribution{'s' if diff != 1 else ''}", "info")
                    prev_total = cal["totalContributions"]

            result["status"] = "ok"
            STATE["providers"]["github"] = result
        except httpx.HTTPStatusError as e:
            STATE["providers"]["github"] = {
                "status": "error",
                "error": f"HTTP {e.response.status_code}: {e.response.text[:200]}",
                "updated_at": _now_iso(),
            }
        except Exception as e:
            STATE["providers"]["github"] = {
                "status": "error",
                "error": f"{type(e).__name__}: {e}",
                "updated_at": _now_iso(),
            }
        await asyncio.sleep(interval)


def _repo_short(repo_url: str) -> str:
    # https://api.github.com/repos/owner/name -> owner/name
    parts = repo_url.rstrip("/").split("/")
    if len(parts) >= 2:
        return "/".join(parts[-2:])
    return repo_url


# --------------------------------------------------------------------------- #
# Mock providers (replace one at a time as we add real ones)
# --------------------------------------------------------------------------- #

async def mock_calendar_poll() -> None:
    """Mocked calendar data until we wire up ical-buddy."""
    while True:
        now = datetime.now()
        STATE["providers"]["calendar"] = {
            "status": "mocked",
            "updated_at": _now_iso(),
            "events": [
                {"start": (now + timedelta(minutes=12)).strftime("%H:%M"), "title": "Eng standup", "is_now": False, "is_next": True},
                {"start": (now + timedelta(hours=1)).strftime("%H:%M"), "title": "1:1 w/ Sarah", "is_now": False, "is_next": False},
                {"start": (now + timedelta(hours=2)).strftime("%H:%M"), "title": "Design review", "is_now": False, "is_next": False},
                {"start": (now + timedelta(hours=3, minutes=30)).strftime("%H:%M"), "title": "Deep work block", "is_now": False, "is_next": False},
                {"start": (now + timedelta(hours=5)).strftime("%H:%M"), "title": "Dinner w/ Mona", "is_now": False, "is_next": False},
            ],
        }
        await asyncio.sleep(60)


async def mock_tasks_poll() -> None:
    """Mocked unified task list until Motion + Linear are wired up."""
    while True:
        STATE["providers"]["tasks"] = {
            "status": "mocked",
            "updated_at": _now_iso(),
            "open_count": 11,
            "items": [
                {"source": "MOT", "title": "Ship dashboard v0", "due": "TODAY", "priority": "high"},
                {"source": "GH",  "title": "Review PR #2841",  "due": "3D",    "priority": "high"},
                {"source": "LIN", "title": "ENG-412 Fix race condition", "due": "P2", "priority": "med"},
                {"source": "LIN", "title": "ENG-419 Add rate limits",    "due": "P3", "priority": "med"},
                {"source": "MOT", "title": "Reply to investor email",    "due": "TUE", "priority": "med"},
                {"source": "LIN", "title": "ENG-401 Spike auth provider","due": "P3", "priority": "low"},
            ],
        }
        await asyncio.sleep(30)


async def mock_claude_poll() -> None:
    """Approximate Claude usage and CC session count. Real impl walks ~/.claude/."""
    while True:
        # Slowly drift the percentages so the bars feel alive on screen.
        five = 60 + int(20 * abs(_sin_seconds(0.001)))
        weekly = 45 + int(15 * abs(_sin_seconds(0.0003, phase=1.0)))
        STATE["providers"]["claude"] = {
            "status": "mocked",
            "updated_at": _now_iso(),
            "five_hour": {"percent": five, "resets_at": (datetime.now() + timedelta(hours=4)).strftime("%H:%M")},
            "weekly":    {"percent": weekly, "resets_at": "MON 00:00"},
            "cc_sessions": {"live": 3, "idle": 1},
        }
        await asyncio.sleep(15)


async def system_poll(interval: int = 5) -> None:
    """System stats via psutil. Network rates are computed between polls."""
    host = socket.gethostname().split(".")[0]
    boot_ts = psutil.boot_time()
    # On macOS Big Sur+ "/" is a tiny read-only system volume; the user's data
    # is on /System/Volumes/Data. Fall back to "/" on Linux / older macOS.
    disk_root = "/System/Volumes/Data" if os.path.isdir("/System/Volumes/Data") else "/"

    # Prime cpu_percent so the first real value isn't 0.0
    psutil.cpu_percent(interval=None)
    prev_net = psutil.net_io_counters()
    prev_ts = time.monotonic()

    while True:
        try:
            now_ts = time.monotonic()
            dt = max(1e-3, now_ts - prev_ts)
            net = psutil.net_io_counters()
            up_mbps = ((net.bytes_sent - prev_net.bytes_sent) * 8) / dt / 1_000_000
            down_mbps = ((net.bytes_recv - prev_net.bytes_recv) * 8) / dt / 1_000_000
            prev_net, prev_ts = net, now_ts

            uptime_secs = int(time.time() - boot_ts)
            days, rem = divmod(uptime_secs, 86400)
            hours, rem = divmod(rem, 3600)
            mins = rem // 60

            STATE["providers"]["system"] = {
                "status": "ok",
                "updated_at": _now_iso(),
                "cpu_percent": int(psutil.cpu_percent(interval=None)),
                "mem_percent": int(psutil.virtual_memory().percent),
                "disk_percent": int(psutil.disk_usage(disk_root).percent),
                "net_up_mbps": round(max(0.0, up_mbps), 1),
                "net_down_mbps": round(max(0.0, down_mbps), 1),
                "uptime": f"{days}d {hours:02d}h {mins:02d}m",
                "host": host,
            }
        except Exception as e:
            STATE["providers"]["system"] = {
                "status": "error",
                "error": f"{type(e).__name__}: {e}",
                "updated_at": _now_iso(),
            }
        await asyncio.sleep(interval)


async def services_poll(interval: int = 300) -> None:
    """Polls public status pages (no auth required)."""
    endpoints = {
        "anthropic": "https://status.anthropic.com/api/v2/summary.json",
        "openai":    "https://status.openai.com/api/v2/summary.json",
        "github":    "https://www.githubstatus.com/api/v2/summary.json",
        "linear":    "https://status.linear.app/api/v2/summary.json",
        "vercel":    "https://www.vercel-status.com/api/v2/summary.json",
    }
    while True:
        results = {}
        async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "cowork-dash/0.1"}) as c:
            for name, url in endpoints.items():
                try:
                    r = await c.get(url)
                    r.raise_for_status()
                    data = r.json()
                    indicator = (data.get("status") or {}).get("indicator", "unknown")
                    description = (data.get("status") or {}).get("description", "")
                    results[name] = {"indicator": indicator, "description": description}
                except Exception as e:
                    results[name] = {"indicator": "unknown", "description": str(e)[:100]}
        STATE["providers"]["services"] = {
            "status": "ok",
            "updated_at": _now_iso(),
            "items": results,
        }
        await asyncio.sleep(interval)


def _sin_seconds(rate: float, phase: float = 0.0) -> float:
    """Smooth oscillator based on wall clock. Used by mock providers."""
    import math
    return math.sin(datetime.now().timestamp() * rate + phase)


# --------------------------------------------------------------------------- #
# Lifespan + app
# --------------------------------------------------------------------------- #

BACKGROUND_TASKS: list[asyncio.Task] = []


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    cfg = load_config()

    gh = cfg.get("github") or {}
    if gh.get("token") and gh.get("username") and gh["token"] != "ghp_replace_me":
        BACKGROUND_TASKS.append(
            asyncio.create_task(github_poll(gh["token"], gh["username"], gh.get("poll_seconds", 180)))
        )
        _push_ticker("daemon", f"github provider online ({gh['username']})", "info")
    else:
        STATE["providers"]["github"] = {
            "status": "unconfigured",
            "message": "Add [github] token and username to ~/.cowork-dash/config.toml",
        }

    # Always start the mocks and the services poller.
    BACKGROUND_TASKS.append(asyncio.create_task(mock_calendar_poll()))
    BACKGROUND_TASKS.append(asyncio.create_task(mock_tasks_poll()))
    BACKGROUND_TASKS.append(asyncio.create_task(mock_claude_poll()))
    sysc = cfg.get("system") or {}
    BACKGROUND_TASKS.append(asyncio.create_task(system_poll(sysc.get("poll_seconds", 5))))
    BACKGROUND_TASKS.append(asyncio.create_task(services_poll()))

    _push_ticker("daemon", "cowork-dash online", "info")

    try:
        yield
    finally:
        for t in BACKGROUND_TASKS:
            t.cancel()
        for t in BACKGROUND_TASKS:
            with contextlib.suppress(BaseException):
                await t


app = FastAPI(title="cowork-dash", lifespan=lifespan)


@app.get("/api/state.json")
async def state_endpoint():
    return JSONResponse(STATE)


@app.get("/api/health")
async def health():
    return {"ok": True, "started_at": STATE["started_at"]}


# Mount static last so /api/* takes priority.
if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


def main() -> None:
    import uvicorn
    cfg = load_config()
    port = (cfg.get("server") or {}).get("port", 7766)
    print(f"[cowork-dash] listening on http://localhost:{port}")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    main()
