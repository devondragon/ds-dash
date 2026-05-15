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
import json
import os
import re
import shutil
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
                    weeks = cal.get("weeks", [])[-20:]
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

# --------------------------------------------------------------------------- #
# Calendar provider (real — shells out to ical-buddy on macOS)
# --------------------------------------------------------------------------- #

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_TIME_RE = re.compile(r"\d{2}:\d{2}")
_MAX_EVENTS = 10  # cap what we send to the frontend


def _find_ical_buddy() -> str | None:
    """Locate a runnable icalBuddy binary. Prefer Homebrew paths; fall back to PATH."""
    for candidate in ("/opt/homebrew/bin/icalBuddy", "/usr/local/bin/icalBuddy"):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return shutil.which("icalBuddy")


def _parse_ical_buddy(output: str) -> list[dict]:
    """Parse ical-buddy's line-based output into structured events.

    Output (with -po 'title,datetime' -nrd -tf %H:%M -df %Y-%m-%d -b ''):
        Event title
            2026-05-15 at 14:30 - 15:00         # timed, same day
            2026-05-15                          # all-day
            2026-05-15 at 09:00 - 2026-05-17 at 17:00   # multi-day timed

    Title lines start at column 0; date lines are indented. We don't trust any
    particular keyword (e.g. "at"); we just pull YYYY-MM-DD and HH:MM tokens
    out of the indented lines.
    """
    events: list[dict] = []
    current_title: str | None = None
    for line in output.splitlines():
        if not line.strip():
            continue
        if line.startswith((" ", "\t")):
            if not current_title:
                continue
            dates = _DATE_RE.findall(line)
            times = _TIME_RE.findall(line)
            if not dates:
                continue
            start_date = dates[0]
            end_date = dates[1] if len(dates) > 1 else start_date
            start_time = times[0] if times else None
            end_time = times[1] if len(times) > 1 else None
            events.append({
                "title": current_title,
                "start_date": start_date,
                "end_date": end_date,
                "start_time": start_time,
                "end_time": end_time,
                "is_all_day": start_time is None,
                "start": start_time or "ALL",
                "is_now": False,
                "is_next": False,
            })
            current_title = None
        else:
            current_title = line.lstrip("•·*- \t").strip() or None
    return events


def _annotate_now_next(events: list[dict]) -> None:
    """Set is_now and is_next flags based on current local time."""
    now = datetime.now()

    def parse_dt(date_str: str, time_str: str | None) -> datetime | None:
        if not time_str:
            return None
        try:
            return datetime.fromisoformat(f"{date_str}T{time_str}")
        except ValueError:
            return None

    for ev in events:
        if ev["is_all_day"]:
            continue
        start_dt = parse_dt(ev["start_date"], ev["start_time"])
        end_dt = parse_dt(ev["end_date"], ev["end_time"])
        if start_dt and end_dt and start_dt <= now < end_dt:
            ev["is_now"] = True

    upcoming = []
    for ev in events:
        if ev["is_now"] or ev["is_all_day"]:
            continue
        start_dt = parse_dt(ev["start_date"], ev["start_time"])
        if start_dt and start_dt > now:
            upcoming.append((start_dt, ev))
    if upcoming:
        upcoming.sort(key=lambda x: x[0])
        upcoming[0][1]["is_next"] = True


async def calendar_poll(ical_buddy: str, look_ahead_days: int = 1, interval: int = 60) -> None:
    """Poll macOS Calendar via ical-buddy. Uses argv (no shell) — args are
    trusted (path from config or auto-detect; rest are static flags)."""
    window = "eventsToday" if look_ahead_days <= 0 else f"eventsToday+{look_ahead_days}"
    args = [
        ical_buddy,
        "-nc",
        "-nrd",
        "-iep", "title,datetime",
        "-po",  "title,datetime",
        "-b",   "",
        "-tf",  "%H:%M",
        "-df",  "%Y-%m-%d",
        window,
    ]
    while True:
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
            if proc.returncode != 0:
                STATE["providers"]["calendar"] = {
                    "status": "error",
                    "error": stderr.decode("utf-8", errors="replace").strip()[:200] or f"ical-buddy exit {proc.returncode}",
                    "updated_at": _now_iso(),
                }
            else:
                events = _parse_ical_buddy(stdout.decode("utf-8", errors="replace"))
                _annotate_now_next(events)
                # Drop events that have already ended — the panel is "what's left today",
                # not a log of everything you missed. All-day events stay.
                now = datetime.now()
                def _still_active(ev: dict) -> bool:
                    if ev["is_all_day"] or not ev.get("end_time"):
                        return True
                    try:
                        end_dt = datetime.fromisoformat(f"{ev['end_date']}T{ev['end_time']}")
                    except ValueError:
                        return True
                    return end_dt > now
                events = [e for e in events if _still_active(e)]
                STATE["providers"]["calendar"] = {
                    "status": "ok",
                    "updated_at": _now_iso(),
                    "events": events[:_MAX_EVENTS],
                }
        except FileNotFoundError:
            STATE["providers"]["calendar"] = {
                "status": "unconfigured",
                "message": f"ical-buddy not found at {ical_buddy}. Install: brew install ical-buddy",
            }
            return
        except OSError as e:
            STATE["providers"]["calendar"] = {
                "status": "unconfigured",
                "message": f"ical-buddy at {ical_buddy} can't exec ({e}). Reinstall: brew install ical-buddy",
            }
            return
        except asyncio.TimeoutError:
            STATE["providers"]["calendar"] = {
                "status": "error",
                "error": "ical-buddy timed out after 10s",
                "updated_at": _now_iso(),
            }
        except Exception as e:
            STATE["providers"]["calendar"] = {
                "status": "error",
                "error": f"{type(e).__name__}: {e}",
                "updated_at": _now_iso(),
            }
        await asyncio.sleep(interval)


# --------------------------------------------------------------------------- #
# Motion provider (real — usemotion.com /v1/tasks)
# --------------------------------------------------------------------------- #

MOTION_TASKS_URL = "https://api.usemotion.com/v1/tasks"
_MOTION_MAX_ITEMS = 12  # how many tasks we surface to the panel

# Motion priority enum -> sort rank + frontend bucket.
_MOTION_PRI_RANK = {"ASAP": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
_MOTION_PRI_OUT  = {"ASAP": "high", "HIGH": "high", "MEDIUM": "med", "LOW": "low"}


def _due_string(due_iso: str | None) -> str:
    """Compact due display: OVERDUE / TODAY / TMRW / 3D / TUE / JUN 16.

    Generic — used by both Motion and Linear (and any future provider with
    ISO due timestamps).
    """
    if not due_iso:
        return ""
    try:
        due_dt = datetime.fromisoformat(due_iso.replace("Z", "+00:00")).astimezone()
    except (ValueError, AttributeError):
        return ""
    delta = (due_dt.date() - datetime.now().date()).days
    if delta < 0:
        return "OVERDUE"
    if delta == 0:
        return "TODAY"
    if delta == 1:
        return "TMRW"
    if delta < 7:
        return f"{delta}D"
    if delta < 14:
        return due_dt.strftime("%a").upper()
    return due_dt.strftime("%b %d").upper()


def _motion_sort_key(t: dict) -> tuple:
    """Priority rank asc, then due date asc (None last), then name."""
    pri = _MOTION_PRI_RANK.get((t.get("priority") or "MEDIUM").upper(), 4)
    due = t.get("dueDate") or "9999-12-31"
    return (pri, due, t.get("name") or "")


def _motion_to_item(t: dict) -> dict:
    pri_raw = (t.get("priority") or "MEDIUM").upper()
    return {
        "source": "MOT",
        "title": (t.get("name") or "").strip(),
        "due": _due_string(t.get("dueDate")),
        "priority": _MOTION_PRI_OUT.get(pri_raw, "med"),
    }


async def motion_poll(api_key: str, interval: int = 60) -> None:
    """Poll Motion tasks (pageSize ~50). Filters to active (not completed,
    not in a resolved status), sorts by priority + due, sends top N to
    the panel while reporting the full active count in open_count.
    """
    headers = {"X-API-Key": api_key, "Accept": "application/json", "User-Agent": "cowork-dash/0.1"}
    while True:
        try:
            async with httpx.AsyncClient(timeout=20, headers=headers) as c:
                r = await c.get(MOTION_TASKS_URL)
                r.raise_for_status()
                data = r.json()
            tasks = data.get("tasks") or []
            active = [
                t for t in tasks
                if not t.get("completed")
                and not (t.get("status") or {}).get("isResolvedStatus")
            ]
            # Collapse recurring task occurrences to one row per template:
            # keep only the earliest-due instance per parentRecurringTaskId.
            # Non-recurring tasks get their own task id as the dedup key.
            by_key: dict[str, dict] = {}
            for t in active:
                key = t.get("parentRecurringTaskId") or t.get("id") or ""
                if not key:
                    continue
                existing = by_key.get(key)
                if not existing or (t.get("dueDate") or "9999") < (existing.get("dueDate") or "9999"):
                    by_key[key] = t
            deduped = sorted(by_key.values(), key=_motion_sort_key)
            STATE["providers"]["tasks"] = {
                "status": "ok",
                "updated_at": _now_iso(),
                "open_count": len(deduped),
                "items": [_motion_to_item(t) for t in deduped[:_MOTION_MAX_ITEMS]],
            }
        except httpx.HTTPStatusError as e:
            STATE["providers"]["tasks"] = {
                "status": "error",
                "error": f"HTTP {e.response.status_code}: {e.response.text[:200]}",
                "updated_at": _now_iso(),
            }
        except Exception as e:
            STATE["providers"]["tasks"] = {
                "status": "error",
                "error": f"{type(e).__name__}: {e}",
                "updated_at": _now_iso(),
            }
        await asyncio.sleep(interval)


# Legacy mock kept for reference; not started by lifespan when [motion].api_key is set.
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


# --------------------------------------------------------------------------- #
# Linear provider (real — api.linear.app GraphQL)
# --------------------------------------------------------------------------- #
# One coroutine per workspace. Each writes to STATE["providers"]["linear"][label].
# Combined GraphQL query: viewer.assignedIssues + cycles(isActive). The assigned
# filter is intentionally NOT applied server-side so we can detect transitions
# *into* completed/canceled before client-side filtering hides those issues.

LINEAR_API = "https://api.linear.app/graphql"
_LINEAR_MAX_ITEMS = 8           # how many issues we render per panel
_LINEAR_MIN_INTERVAL = 60        # floor for poll_seconds
_LINEAR_DEFAULT_INTERVAL = 90

# Linear priority int -> (sort rank, frontend bucket).
_LINEAR_PRI_RANK = {1: 0, 2: 1, 3: 2, 4: 3, 0: 4}
_LINEAR_PRI_OUT  = {1: "high", 2: "high", 3: "med", 4: "low", 0: "none"}

LINEAR_GQL = """
query DashboardSnapshot {
  viewer {
    id
    name
    assignedIssues(first: 50, orderBy: updatedAt) {
      nodes {
        id
        identifier
        title
        priority
        dueDate
        updatedAt
        url
        state { name type }
        team { key }
        cycle { number id }
      }
    }
  }
  cycles(first: 5, filter: { isActive: { eq: true } }) {
    nodes {
      id
      number
      name
      startsAt
      endsAt
      progress
      issueCountHistory
      completedIssueCountHistory
    }
  }
}
""".strip()


def _linear_to_item(node: dict) -> dict:
    """Convert a Linear issue node to the frontend item shape."""
    pri_int = node.get("priority") or 0
    state = node.get("state") or {}
    team = node.get("team") or {}
    return {
        "id": node.get("id") or "",
        "identifier": node.get("identifier") or "",
        "title": (node.get("title") or "").strip(),
        "team": team.get("key") or "",
        "priority": _LINEAR_PRI_OUT.get(pri_int, "none"),
        "due": _due_string(node.get("dueDate")),
        "state_name": state.get("name") or "",
        "state_type": state.get("type") or "",
        "url": node.get("url") or "",
        # internal — used only for sorting, NOT serialized to the panel:
        "_pri_rank": _LINEAR_PRI_RANK.get(pri_int, 4),
        "_due_iso": node.get("dueDate") or "9999-12-31",
        "_updated_at": node.get("updatedAt") or "",
    }


def _linear_sort_key(item: dict) -> tuple:
    """Priority rank asc, due date asc (None last), updatedAt desc."""
    return (item["_pri_rank"], item["_due_iso"], item["_updated_at"])


def _linear_active_cycle(nodes: list[dict]) -> dict:
    """Pick the soonest-ending currently-active cycle from the workspace.

    Local sanity-check on startsAt/endsAt to defend against clock skew.
    Returns {"present": False} when no active cycle.
    """
    import math
    now = datetime.now(timezone.utc)
    actives: list[tuple] = []
    for c in nodes or []:
        try:
            starts = datetime.fromisoformat((c.get("startsAt") or "").replace("Z", "+00:00"))
            ends = datetime.fromisoformat((c.get("endsAt") or "").replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        if starts <= now < ends:
            actives.append((ends, c))
    if not actives:
        return {"present": False}
    actives.sort(key=lambda t: t[0])
    ends, c = actives[0]
    issued = c.get("issueCountHistory") or []
    completed = c.get("completedIssueCountHistory") or []
    total = int(issued[-1]) if issued else 0
    done = int(completed[-1]) if completed else 0
    progress_pct = int(round((c.get("progress") or 0.0) * 100))
    days_to_end = (ends - now).total_seconds() / 86400.0
    # Round toward "humans say 'in X days'": ceil for future, floor for past.
    ends_in_days = math.ceil(days_to_end) if days_to_end >= 0 else math.floor(days_to_end)
    return {
        "present": True,
        "number": c.get("number"),
        "name": c.get("name") or f"Cycle {c.get('number')}",
        "ends_in_days": ends_in_days,
        "progress_pct": progress_pct,
        "completed": done,
        "total": total,
        "multi_team": len(actives) > 1,
    }


# --------------------------------------------------------------------------- #
# Claude usage + CC sessions (real — walks ~/.claude/projects)
# --------------------------------------------------------------------------- #
# Each .jsonl under ~/.claude/projects/<project>/ is one Claude Code session.
# Lines with `type: 'user'` and an ISO `timestamp` represent user messages.
# We approximate usage by counting user messages in 5h and 7d windows, and
# classify sessions as live / idle by file mtime.

CLAUDE_PROJECTS_ROOT = Path.home() / ".claude" / "projects"
_DEFAULT_FIVE_H_LIMIT = 200    # approximate per-plan ceiling for the 5h gauge
_DEFAULT_WEEKLY_LIMIT = 1500   # ditto for the 7d gauge


def _scan_claude_data(five_h_limit: int, weekly_limit: int) -> dict:
    """Walk ~/.claude/projects/**/*.jsonl, return usage + session stats.
    Files older than the 7d cutoff are stat'd but not opened (cheap).
    """
    out = {
        "five_pct": 0, "five_h_count": 0, "five_resets_at": "—",
        "weekly_pct": 0, "weekly_count": 0, "weekly_resets_at": "—",
        "cc_live": 0, "cc_idle": 0, "cc_today": 0,
    }
    if not CLAUDE_PROJECTS_ROOT.is_dir():
        return out

    now_utc = datetime.now(timezone.utc)
    now_local = datetime.now()
    cutoff_5h = now_utc - timedelta(hours=5)
    cutoff_7d = now_utc - timedelta(days=7)
    today_midnight_ts = now_local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    five_h_count = 0
    weekly_count = 0
    cc_live = cc_idle = cc_today = 0
    oldest_in_5h: datetime | None = None
    oldest_in_7d: datetime | None = None

    for proj in CLAUDE_PROJECTS_ROOT.iterdir():
        if not proj.is_dir():
            continue
        for f in proj.glob("*.jsonl"):
            try:
                mtime = f.stat().st_mtime
            except OSError:
                continue
            age_s = now_utc.timestamp() - mtime
            if age_s < 300:
                cc_live += 1
            elif age_s < 3600:
                cc_idle += 1
            if mtime >= today_midnight_ts:
                cc_today += 1

            # Skip full read for files outside the 7-day message window.
            if mtime < cutoff_7d.timestamp():
                continue
            try:
                with f.open(encoding="utf-8", errors="replace") as fp:
                    for line in fp:
                        # Cheap pre-filter; most lines aren't user messages.
                        if '"type":"user"' not in line:
                            continue
                        try:
                            d = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if d.get("type") != "user":
                            continue
                        ts = d.get("timestamp")
                        if not ts:
                            continue
                        try:
                            msg_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        except ValueError:
                            continue
                        if msg_dt >= cutoff_5h:
                            five_h_count += 1
                            if oldest_in_5h is None or msg_dt < oldest_in_5h:
                                oldest_in_5h = msg_dt
                        if msg_dt >= cutoff_7d:
                            weekly_count += 1
                            if oldest_in_7d is None or msg_dt < oldest_in_7d:
                                oldest_in_7d = msg_dt
            except OSError:
                continue

    out["five_h_count"] = five_h_count
    out["weekly_count"] = weekly_count
    out["five_pct"] = min(100, int(round(100 * five_h_count / five_h_limit))) if five_h_limit else 0
    out["weekly_pct"] = min(100, int(round(100 * weekly_count / weekly_limit))) if weekly_limit else 0
    out["cc_live"] = cc_live
    out["cc_idle"] = cc_idle
    out["cc_today"] = cc_today

    # "Resets at" — when the oldest message in the window slides out of it.
    if oldest_in_5h:
        out["five_resets_at"] = (oldest_in_5h + timedelta(hours=5)).astimezone().strftime("%H:%M")
    if oldest_in_7d:
        out["weekly_resets_at"] = (oldest_in_7d + timedelta(days=7)).astimezone().strftime("%a %H:%M").upper()
    return out


async def claude_poll(
    interval: int = 60,
    five_h_limit: int = _DEFAULT_FIVE_H_LIMIT,
    weekly_limit: int = _DEFAULT_WEEKLY_LIMIT,
) -> None:
    """Real provider — powers both CLAUDE USAGE and CC SESSIONS panels."""
    while True:
        try:
            stats = _scan_claude_data(five_h_limit, weekly_limit)
            STATE["providers"]["claude"] = {
                "status": "ok",
                "updated_at": _now_iso(),
                "five_hour": {
                    "percent": stats["five_pct"],
                    "count": stats["five_h_count"],
                    "limit": five_h_limit,
                    "resets_at": stats["five_resets_at"],
                },
                "weekly": {
                    "percent": stats["weekly_pct"],
                    "count": stats["weekly_count"],
                    "limit": weekly_limit,
                    "resets_at": stats["weekly_resets_at"],
                },
                "cc_sessions": {
                    "live": stats["cc_live"],
                    "idle": stats["cc_idle"],
                    "total_today": stats["cc_today"],
                },
            }
        except Exception as e:
            STATE["providers"]["claude"] = {
                "status": "error",
                "error": f"{type(e).__name__}: {e}",
                "updated_at": _now_iso(),
            }
        await asyncio.sleep(interval)


# Legacy mock kept for reference; not started by lifespan.
async def mock_claude_poll() -> None:
    """Original drift mock — superseded by claude_poll."""
    while True:
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


NET_HISTORY_SIZE = 60  # 60 samples * 5s = 5min of net trace at the default poll interval


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
    history: list[dict] = []

    while True:
        try:
            now_ts = time.monotonic()
            dt = max(1e-3, now_ts - prev_ts)
            net = psutil.net_io_counters()
            up_mbps = round(max(0.0, ((net.bytes_sent - prev_net.bytes_sent) * 8) / dt / 1_000_000), 2)
            down_mbps = round(max(0.0, ((net.bytes_recv - prev_net.bytes_recv) * 8) / dt / 1_000_000), 2)
            prev_net, prev_ts = net, now_ts

            history.append({"up": up_mbps, "down": down_mbps})
            del history[:-NET_HISTORY_SIZE]

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
                "net_up_mbps": up_mbps,
                "net_down_mbps": down_mbps,
                "net_history": list(history),  # snapshot — frontend reads, backend keeps mutating
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

    cal = cfg.get("calendar") or {}
    ical_buddy = cal.get("ical_buddy_path") or _find_ical_buddy()
    if ical_buddy and os.path.isfile(ical_buddy):
        BACKGROUND_TASKS.append(asyncio.create_task(calendar_poll(
            ical_buddy,
            look_ahead_days=cal.get("look_ahead_days", 1),
            interval=cal.get("poll_seconds", 60),
        )))
        _push_ticker("daemon", "calendar provider online", "info")
    else:
        STATE["providers"]["calendar"] = {
            "status": "unconfigured",
            "message": "Install ical-buddy (brew install ical-buddy) or set [calendar].ical_buddy_path",
        }

    mot = cfg.get("motion") or {}
    if mot.get("api_key"):
        BACKGROUND_TASKS.append(asyncio.create_task(
            motion_poll(mot["api_key"], mot.get("poll_seconds", 60))
        ))
        _push_ticker("daemon", "motion provider online", "info")
    else:
        STATE["providers"]["tasks"] = {
            "status": "unconfigured",
            "message": "Add [motion] api_key to ~/.cowork-dash/config.toml",
        }

    cl = cfg.get("claude") or {}
    BACKGROUND_TASKS.append(asyncio.create_task(claude_poll(
        interval=cl.get("poll_seconds", 60),
        five_h_limit=cl.get("five_h_limit", _DEFAULT_FIVE_H_LIMIT),
        weekly_limit=cl.get("weekly_limit", _DEFAULT_WEEKLY_LIMIT),
    )))
    _push_ticker("daemon", "claude provider online", "info")
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
