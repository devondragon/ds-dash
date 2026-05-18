#!/usr/bin/env python3
"""
cowork-dash: local dashboard daemon.

One FastAPI app, one in-memory STATE dict, one background asyncio task
per configured provider. Each provider writes to STATE["providers"][name]
with at least {status, updated_at}; the frontend polls GET /api/state.json
every 5s and renders defensively against missing fields.

The static frontend (HTML/CSS/JS) is served from ./static via a StaticFiles
mount. Cross-cutting events go to STATE["ticker"] via _push_ticker().

See CLAUDE.md for the provider polling pattern, status vocabulary, and how
to add a new panel.
"""
from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import math
import os
import re
import shutil
import socket
import subprocess
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
from fastapi import Body, FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

# --------------------------------------------------------------------------- #
# Config + shared state
# --------------------------------------------------------------------------- #

CONFIG_PATH = Path(os.environ.get("COWORK_DASH_CONFIG", str(Path.home() / ".cowork-dash" / "config.toml")))
SCRATCHPAD_PATH = Path(os.environ.get("COWORK_DASH_SCRATCHPAD", str(Path.home() / ".cowork-dash" / "scratchpad.txt")))
STATIC_DIR = Path(__file__).parent / "static"

# Everything the frontend renders lives in here. Each provider owns one
# top-level key under "providers" and writes a dict with at least {status,
# updated_at}. The frontend tolerates missing fields.
STATE: dict[str, Any] = {
    "started_at": datetime.now(timezone.utc).isoformat(),
    "meta": {
        # Operator label shown in the header. Populated at startup from
        # [ui].operator_name in config, with $USER as the fallback.
        "operator": "OPERATOR",
    },
    "providers": {
        "github": {"status": "pending"},
        "calendar": {"status": "pending"},
        "tasks": {"status": "pending"},
        "claude": {"status": "pending"},
        "services": {"status": "pending"},
        "system": {"status": "pending"},
        "network": {"status": "pending"},
        "weather": {"status": "pending"},
        "linear": {},   # populated per-workspace at startup; sentinel set if unconfigured
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

# Recent activity across PUBLIC + PRIVATE repos. The /users/{u}/events REST
# endpoint only returns public-timeline events (stars, public pushes,
# discussions); private-repo commits and PRs are invisible there even when
# authenticated. contributionsCollection is what powers the profile graph
# and is the only way to surface "what I actually did this week."
GRAPHQL_RECENT_ACTIVITY = """
query($login: String!, $from: DateTime!) {
  user(login: $login) {
    contributionsCollection(from: $from) {
      pullRequestContributions(first: 15, orderBy: {direction: DESC}) {
        nodes {
          occurredAt
          pullRequest {
            number title url state isDraft merged
            repository { nameWithOwner }
          }
        }
      }
      pullRequestReviewContributions(first: 15, orderBy: {direction: DESC}) {
        nodes {
          occurredAt
          pullRequestReview { url state }
          pullRequest {
            number title url
            repository { nameWithOwner }
          }
        }
      }
      issueContributions(first: 10, orderBy: {direction: DESC}) {
        nodes {
          occurredAt
          issue {
            number title url state
            repository { nameWithOwner }
          }
        }
      }
      commitContributionsByRepository(maxRepositories: 25) {
        repository { nameWithOwner url }
        contributions(first: 10, orderBy: {direction: DESC, field: OCCURRED_AT}) {
          nodes { commitCount occurredAt }
        }
      }
    }
  }
}
""".strip()


def _from_contribution_collection(data: dict) -> list[dict]:
    """Flatten a contributionsCollection payload into our event-row shape.

    Each opened PR, submitted review, opened issue, and per-repo commit-day
    bucket becomes one row keyed by occurredAt so the merged feed can be
    sorted chronologically alongside the public-events feed.
    """
    out: list[dict] = []
    cc = (((data.get("data") or {}).get("user") or {})
          .get("contributionsCollection")) or {}

    for n in (cc.get("pullRequestContributions") or {}).get("nodes") or []:
        pr = n.get("pullRequest") or {}
        repo = (pr.get("repository") or {}).get("nameWithOwner") or ""
        if pr.get("merged"):
            cls = "pr-merged"
        elif (pr.get("state") or "").lower() == "closed":
            cls = "pr-closed"
        else:
            cls = "pr-open"
        draft = " [draft]" if pr.get("isDraft") else ""
        out.append({
            "kind": "pr",
            "repo": repo,
            "detail": f"opened PR #{pr.get('number')} {pr.get('title') or ''}{draft}".strip(),
            "at": n.get("occurredAt") or "",
            "url": pr.get("url") or "",
            "cls": cls,
        })

    for n in (cc.get("pullRequestReviewContributions") or {}).get("nodes") or []:
        pr = n.get("pullRequest") or {}
        rev = n.get("pullRequestReview") or {}
        repo = (pr.get("repository") or {}).get("nameWithOwner") or ""
        state = (rev.get("state") or "").lower()
        verb = {"approved": "approved", "changes_requested": "requested changes on",
                "commented": "reviewed"}.get(state, "reviewed")
        cls = "review-approved" if state == "approved" else (
            "review-changes" if state == "changes_requested" else "review")
        out.append({
            "kind": "review",
            "repo": repo,
            "detail": f"{verb} PR #{pr.get('number')} {pr.get('title') or ''}".strip(),
            "at": n.get("occurredAt") or "",
            "url": rev.get("url") or pr.get("url") or "",
            "cls": cls,
        })

    for n in (cc.get("issueContributions") or {}).get("nodes") or []:
        iss = n.get("issue") or {}
        repo = (iss.get("repository") or {}).get("nameWithOwner") or ""
        cls = "issue-closed" if (iss.get("state") or "").lower() == "closed" else "issue-open"
        out.append({
            "kind": "issue",
            "repo": repo,
            "detail": f"opened #{iss.get('number')} {iss.get('title') or ''}".strip(),
            "at": n.get("occurredAt") or "",
            "url": iss.get("url") or "",
            "cls": cls,
        })

    for r in cc.get("commitContributionsByRepository") or []:
        repo_info = r.get("repository") or {}
        repo = repo_info.get("nameWithOwner") or ""
        repo_url = repo_info.get("url") or (f"https://github.com/{repo}" if repo else "")
        for n in (r.get("contributions") or {}).get("nodes") or []:
            c = n.get("commitCount") or 0
            if c == 0:
                continue
            out.append({
                "kind": "push",
                "repo": repo,
                "detail": f"{c} commit{'s' if c != 1 else ''}",
                "at": n.get("occurredAt") or "",
                "url": repo_url,
                "cls": "push",
            })

    return out


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
        # Tag of the in-flight call; surfaced in error messages so a transient
        # failure tells us which of the five endpoints actually broke.
        current_call: str = ""
        try:
            async with httpx.AsyncClient(timeout=30, headers=headers) as c:
                # PRs awaiting my review
                current_call = "search/issues (review-requested)"
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
                current_call = "search/issues (my open PRs)"
                r2 = await c.get(
                    f"{GITHUB_API}/search/issues",
                    params={"q": f"is:open is:pr author:{username} archived:false", "per_page": 10},
                )
                r2.raise_for_status()
                d2 = r2.json()
                result["my_open_prs"] = {
                    "count": d2.get("total_count", 0),
                    "items": [
                        {
                            "title": i["title"],
                            "url": i["html_url"],
                            "repo": _repo_short(i.get("repository_url", "")),
                            "updated_at": i["updated_at"],
                            "number": i.get("number"),
                            "draft": i.get("draft", False),
                        }
                        for i in d2.get("items", [])[:6]
                    ],
                }

                # Issues assigned to me
                current_call = "search/issues (assigned to me)"
                r3 = await c.get(
                    f"{GITHUB_API}/search/issues",
                    params={"q": f"is:open is:issue assignee:{username} archived:false", "per_page": 10},
                )
                r3.raise_for_status()
                d3 = r3.json()
                result["issues_assigned"] = {
                    "count": d3.get("total_count", 0),
                    "items": [
                        {
                            "title": i["title"],
                            "url": i["html_url"],
                            "repo": _repo_short(i.get("repository_url", "")),
                            "updated_at": i["updated_at"],
                            "number": i.get("number"),
                        }
                        for i in d3.get("items", [])[:6]
                    ],
                }

                # Recent public events — stars, public pushes, discussions,
                # forks. Misses private activity, which contributionsCollection
                # picks up below.
                current_call = "users/<login>/events"
                r5 = await c.get(
                    f"{GITHUB_API}/users/{username}/events",
                    params={"per_page": 30},
                )
                r5.raise_for_status()
                public_events = [_summarize_event(e) for e in r5.json() if _summarize_event(e)]

                # Recent contributions across public+private repos (PRs opened,
                # reviews submitted, issues opened, per-repo commit days).
                current_call = "graphql (recent activity)"
                since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
                r_act = await c.post(
                    GITHUB_GRAPHQL,
                    json={"query": GRAPHQL_RECENT_ACTIVITY,
                          "variables": {"login": username, "from": since}},
                )
                r_act.raise_for_status()
                act_payload = r_act.json()
                if act_payload.get("errors"):
                    raise RuntimeError(
                        f"graphql errors: {act_payload['errors'][0].get('message', '?')}"
                    )
                contrib_events = _from_contribution_collection(act_payload)

                merged = sorted(
                    contrib_events + public_events,
                    key=lambda e: e.get("at", ""),
                    reverse=True,
                )
                seen: set[tuple[str, str]] = set()
                deduped: list[dict] = []
                for e in merged:
                    key = (e.get("kind") or "", e.get("url") or e.get("detail") or "")
                    if key in seen:
                        continue
                    seen.add(key)
                    deduped.append(e)
                result["recent_events"] = {"items": deduped[:15]}

                # Contribution heatmap via GraphQL
                current_call = "graphql (contribution heatmap)"
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
            body = e.response.text[:200].strip() or "(empty body)"
            STATE["providers"]["github"] = {
                "status": "error",
                "error": f"HTTP {e.response.status_code} on {current_call or 'unknown'}: {body}",
                "updated_at": _now_iso(),
            }
        except Exception as e:
            STATE["providers"]["github"] = {
                "status": "error",
                "error": f"{type(e).__name__} on {current_call or 'unknown'}: {e}",
                "updated_at": _now_iso(),
            }
        await asyncio.sleep(interval)


def _repo_short(repo_url: str) -> str:
    # https://api.github.com/repos/owner/name -> owner/name
    parts = repo_url.rstrip("/").split("/")
    if len(parts) >= 2:
        return "/".join(parts[-2:])
    return repo_url


def _summarize_event(e: dict) -> dict | None:
    """Reduce a /users/{u}/events payload entry to a display-ready row.

    Returns {kind, repo, detail, at, url, cls} or None to filter out. Pushes
    with no distinct commits (force-push noise, branch deletes that surface
    twice) are dropped so the activity feed stays scannable.
    """
    t = e.get("type") or ""
    repo = (e.get("repo") or {}).get("name") or ""
    payload = e.get("payload") or {}
    at = e.get("created_at") or ""
    repo_url = f"https://github.com/{repo}" if repo else ""

    def out(kind: str, detail: str, url: str = "", cls: str = "") -> dict:
        return {
            "kind": kind,
            "repo": repo,
            "detail": detail.strip(),
            "at": at,
            "url": url or repo_url,
            "cls": cls or kind,
        }

    if t == "PushEvent":
        commits = payload.get("commits") or []
        distinct = sum(1 for c in commits if c.get("distinct", True))
        n = distinct or payload.get("distinct_size") or 0
        if n == 0:
            return None
        branch = (payload.get("ref") or "").rsplit("/", 1)[-1]
        msg = ""
        for c in reversed(commits):
            if c.get("distinct", True) and c.get("message"):
                msg = c["message"].split("\n", 1)[0].strip()
                break
        head_label = msg or f"{n} commit{'s' if n != 1 else ''}"
        more = f" +{n - 1}" if (msg and n > 1) else ""
        detail = head_label + (f" → {branch}" if branch else "") + more
        head = payload.get("head") or ""
        before = payload.get("before") or ""
        if repo and head and before:
            url = f"{repo_url}/compare/{before}...{head}"
        elif repo and branch:
            url = f"{repo_url}/commits/{branch}"
        else:
            url = repo_url
        return out("push", detail, url)

    if t == "PullRequestEvent":
        pr = payload.get("pull_request") or {}
        action = payload.get("action") or "?"
        if action == "closed" and pr.get("merged"):
            action = "merged"
        cls = {"opened": "pr-open", "merged": "pr-merged", "closed": "pr-closed",
               "reopened": "pr-open"}.get(action, "pr")
        detail = f"{action} PR #{pr.get('number', '?')} {pr.get('title') or ''}"
        return out("pr", detail, pr.get("html_url") or "", cls)

    if t == "PullRequestReviewEvent":
        pr = payload.get("pull_request") or {}
        review = payload.get("review") or {}
        state = (review.get("state") or "").lower()
        verb = {"approved": "approved", "changes_requested": "requested changes on",
                "commented": "reviewed"}.get(state, "reviewed")
        cls = "review-approved" if state == "approved" else (
            "review-changes" if state == "changes_requested" else "review")
        detail = f"{verb} PR #{pr.get('number', '?')} {pr.get('title') or ''}"
        return out("review", detail, review.get("html_url") or pr.get("html_url") or "", cls)

    if t == "IssuesEvent":
        iss = payload.get("issue") or {}
        action = payload.get("action") or "?"
        cls = "issue-open" if action == "opened" else (
            "issue-closed" if action == "closed" else "issue")
        detail = f"{action} #{iss.get('number', '?')} {iss.get('title') or ''}"
        return out("issue", detail, iss.get("html_url") or "", cls)

    if t == "IssueCommentEvent":
        iss = payload.get("issue") or {}
        cmt = payload.get("comment") or {}
        body = (cmt.get("body") or "").split("\n", 1)[0].strip()
        if len(body) > 80:
            body = body[:77] + "…"
        snippet = f": {body}" if body else ""
        detail = f"commented on #{iss.get('number', '?')} {iss.get('title') or ''}{snippet}"
        return out("comment", detail, cmt.get("html_url") or iss.get("html_url") or "")

    if t == "CreateEvent":
        rtype = payload.get("ref_type") or ""
        ref = payload.get("ref") or ""
        detail = f"created {rtype} {ref}".strip()
        if rtype == "branch" and ref:
            url = f"{repo_url}/tree/{ref}"
        elif rtype == "tag" and ref:
            url = f"{repo_url}/releases/tag/{ref}"
        else:
            url = repo_url
        return out("create", detail, url)

    if t == "DeleteEvent":
        rtype = payload.get("ref_type") or ""
        ref = payload.get("ref") or ""
        return out("delete", f"deleted {rtype} {ref}".strip())

    if t == "ForkEvent":
        forkee = (payload.get("forkee") or {}).get("html_url") or ""
        return out("fork", "forked", forkee)

    if t == "WatchEvent":
        return out("star", "starred")

    if t == "ReleaseEvent":
        rel = payload.get("release") or {}
        action = payload.get("action") or "?"
        detail = f"{action} release {rel.get('tag_name') or ''}".strip()
        return out("release", detail, rel.get("html_url") or "")

    if t == "PublicEvent":
        return out("public", "made public", cls="create")

    if t == "DiscussionEvent":
        disc = payload.get("discussion") or {}
        action = payload.get("action") or "?"
        detail = f"{action} discussion #{disc.get('number', '?')} {disc.get('title') or ''}"
        return out("discussion", detail, disc.get("html_url") or "")

    if t == "DiscussionCommentEvent":
        disc = payload.get("discussion") or {}
        cmt = payload.get("comment") or {}
        body = (cmt.get("body") or "").split("\n", 1)[0].strip()
        if len(body) > 80:
            body = body[:77] + "…"
        snippet = f": {body}" if body else ""
        detail = f"commented on discussion #{disc.get('number', '?')} {disc.get('title') or ''}{snippet}"
        return out("comment", detail, cmt.get("html_url") or disc.get("html_url") or "")

    if t:
        return out(t.replace("Event", "").lower(), t, cls="default")
    return None


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
            # Only bail permanently on errors that need user action (missing exec
            # perms, wrong-arch binary). Transient OSErrors — EMFILE during sleep/wake,
            # ENOMEM under pressure — fall through to retry on the next interval.
            if e.errno in (errno.EACCES, errno.ENOEXEC):
                STATE["providers"]["calendar"] = {
                    "status": "unconfigured",
                    "message": f"ical-buddy at {ical_buddy} can't exec ({e.strerror or e}). Reinstall: brew install ical-buddy",
                }
                return
            STATE["providers"]["calendar"] = {
                "status": "error",
                "error": f"ical-buddy exec failed: {e.strerror or type(e).__name__} (errno {e.errno})",
                "updated_at": _now_iso(),
            }
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
    task_id = t.get("id") or ""
    # Motion's web app deep-links use ?task=<id>. On macOS, this URL is a Universal
    # Link handled by the desktop Motion.app if installed.
    url = f"https://app.usemotion.com/web/?task={task_id}" if task_id else ""
    return {
        "source": "MOT",
        "id": task_id,
        "title": (t.get("name") or "").strip(),
        "due": _due_string(t.get("dueDate")),
        "priority": _MOTION_PRI_OUT.get(pri_raw, "med"),
        "url": url,
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


# --------------------------------------------------------------------------- #
# Linear provider (real — api.linear.app GraphQL)
# --------------------------------------------------------------------------- #
# One coroutine per workspace. Each writes to STATE["providers"]["linear"][label].
# Combined GraphQL query: viewer.assignedIssues (server-filtered by state.type
# to the workspace's show_state_types) + cycles(isActive).

LINEAR_API = "https://api.linear.app/graphql"
_LINEAR_MAX_ITEMS = 8           # how many issues we render per panel
_LINEAR_MIN_INTERVAL = 60        # floor for poll_seconds
_LINEAR_DEFAULT_INTERVAL = 90
# Default visible states. Linear's WorkflowState.type enum values:
# backlog | unstarted | started | completed | canceled
_LINEAR_DEFAULT_STATE_TYPES = ["backlog", "unstarted", "started"]

# Linear priority int -> (sort rank, frontend bucket).
# Rank distinguishes urgent (1) from high (2) for sort order; the OUT bucket collapses both to "high" for display.
_LINEAR_PRI_RANK = {1: 0, 2: 1, 3: 2, 4: 3, 0: 4}
_LINEAR_PRI_OUT  = {1: "high", 2: "high", 3: "med", 4: "low", 0: "none"}

LINEAR_GQL = """
query DashboardSnapshot($stateTypes: [String!]) {
  viewer {
    id
    name
    assignedIssues(
      first: 50
      orderBy: updatedAt
      filter: { state: { type: { in: $stateTypes } } }
    ) {
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


def _neg_epoch(iso: str | None) -> float:
    if not iso:
        return 0.0
    try:
        return -datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return 0.0


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
        "_updated_at_neg": _neg_epoch(node.get("updatedAt")),
    }


def _linear_sort_key(item: dict) -> tuple:
    return (item["_pri_rank"], item["_due_iso"], item["_updated_at_neg"])


def _linear_active_cycle(nodes: list[dict]) -> dict:
    """Pick the soonest-ending currently-active cycle from the workspace.

    Local sanity-check on startsAt/endsAt to defend against clock skew.
    Returns {"present": False} when no active cycle.
    """
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


async def linear_poll(label: str, api_key: str, interval: int, state_types: list[str]) -> None:
    headers = {
        "Authorization": api_key,            # personal keys: NO "Bearer " prefix
        "Content-Type": "application/json",
        "User-Agent": "cowork-dash/0.1",
    }
    variables = {"stateTypes": state_types}
    prev_ids: set[str] = set()
    prev_states: dict[str, str] = {}
    first_poll = True
    while True:
        try:
            async with httpx.AsyncClient(timeout=20, headers=headers) as c:
                r = await c.post(LINEAR_API, json={"query": LINEAR_GQL, "variables": variables})
                r.raise_for_status()
                payload = r.json()

            errs = payload.get("errors")
            if errs:
                msg = (errs[0].get("message") or "unknown")[:200]
                raise RuntimeError(f"GraphQL: {msg}")

            data = payload.get("data") or {}
            viewer = data.get("viewer") or {}
            raw_nodes = ((viewer.get("assignedIssues") or {}).get("nodes")) or []
            cycle_nodes = ((data.get("cycles") or {}).get("nodes")) or []

            shaped_all = [_linear_to_item(n) for n in raw_nodes]
            cur_ids = {it["id"] for it in shaped_all if it["id"]}
            cur_states = {it["id"]: it["state_name"] for it in shaped_all if it["id"]}

            if not first_poll:
                for it in shaped_all:
                    iid = it["id"]
                    if not iid:
                        continue
                    if iid not in prev_ids:
                        _push_ticker(
                            f"linear-{label}",
                            f"{label} · NEW: {it['identifier']} {it['title']}",
                            "info",
                        )
                        continue
                    new_name = it["state_name"]
                    if new_name == prev_states[iid]:
                        continue
                    if it["state_type"] == "completed":
                        _push_ticker(
                            f"linear-{label}",
                            f"{label} · {it['identifier']} ✓ DONE",
                            "info",
                        )
                    elif it["state_type"] == "started" and "review" in new_name.lower():
                        _push_ticker(
                            f"linear-{label}",
                            f"{label} · {it['identifier']} → IN REVIEW",
                            "info",
                        )

            prev_ids = cur_ids
            prev_states = cur_states
            first_poll = False
            shaped_all.sort(key=_linear_sort_key)
            open_count = len(shaped_all)
            items = []
            for it in shaped_all[:_LINEAR_MAX_ITEMS]:
                items.append({k: v for k, v in it.items() if not k.startswith("_")})

            STATE["providers"]["linear"][label] = {
                "status": "ok",
                "updated_at": _now_iso(),
                "label": label,
                "viewer": {"name": viewer.get("name") or ""},
                "issues": {"open_count": open_count, "items": items},
                "cycle": _linear_active_cycle(cycle_nodes),
            }
        except httpx.HTTPStatusError as e:
            STATE["providers"]["linear"][label] = {
                **(STATE["providers"]["linear"].get(label) or {}),
                "status": "error",
                "label": label,
                "error": f"HTTP {e.response.status_code}: {e.response.text[:200]}",
                "updated_at": _now_iso(),
            }
        except Exception as e:
            STATE["providers"]["linear"][label] = {
                **(STATE["providers"]["linear"].get(label) or {}),
                "status": "error",
                "label": label,
                "error": f"{type(e).__name__}: {e}",
                "updated_at": _now_iso(),
            }
        await asyncio.sleep(interval)


# --------------------------------------------------------------------------- #
# Claude usage + CC sessions
# --------------------------------------------------------------------------- #
# Usage windows (5h / 7d utilization + reset timestamps) are pulled directly
# from Anthropic's rate-limit response headers — the authoritative source.
# We use Claude Code's OAuth credentials (already on disk after `claude login`)
# to make a minimal Messages API request and read the
# `anthropic-ratelimit-unified-{5h,7d}-{utilization,reset}` headers.
#
# Why not message-count the local jsonl files? Two reasons:
#   1. Real usage is token-weighted by model — counting messages can't capture
#      that an Opus turn costs ~5x a Sonnet turn for the same message.
#   2. Real windows are anchored to the FIRST message of the window and reset
#      atomically — they don't slide like a rolling-from-now window does.
#
# CC sessions (live/idle/today) still come from a local jsonl mtime scan;
# those are local-only, free, and don't belong to Anthropic.

CLAUDE_PROJECTS_ROOT = Path.home() / ".claude" / "projects"
_CLAUDE_USAGE_URL = "https://api.anthropic.com/v1/messages"
# Cheapest valid probe: 1 Haiku token. Costs a fraction of a cent per poll.
# Pinned snapshot for stability; override via [claude].probe_model in config
# if/when this snapshot is retired by Anthropic (a 404 with model-not-found
# response will surface as the panel's error chip).
_CLAUDE_PROBE_MODEL_DEFAULT = "claude-haiku-4-5-20251001"
_CLAUDE_KEYCHAIN_SERVICE = "Claude Code-credentials"


def _read_claude_oauth() -> dict | None:
    """Read Claude Code's OAuth credentials from file or macOS keychain.

    Returns {"access_token": str, "expires_at_ms": int | None} on success, or
    None if no credentials are found. Raises on parse failure so the caller
    can surface the underlying error.
    """
    raw: str | None = None
    for f in (Path.home() / ".claude" / ".credentials.json",
              Path.home() / ".claude" / "credentials.json"):
        if f.is_file():
            try:
                raw = f.read_text(encoding="utf-8").strip()
                if raw:
                    break
            except OSError:
                continue

    if not raw and sys.platform == "darwin":
        try:
            r = subprocess.run(
                ["/usr/bin/security", "find-generic-password",
                 "-s", _CLAUDE_KEYCHAIN_SERVICE,
                 "-a", os.environ.get("USER", ""),
                 "-w"],
                capture_output=True, text=True, timeout=3,
            )
            if r.returncode == 0:
                raw = r.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            pass

    if not raw:
        return None

    data = json.loads(raw)
    oa = data.get("claudeAiOauth") or {}
    tok = oa.get("accessToken")
    if not tok:
        return None
    return {"access_token": tok, "expires_at_ms": oa.get("expiresAt")}


async def _fetch_claude_usage_headers(access_token: str, probe_model: str) -> dict:
    """POST a 1-token Messages request and parse usage from rate-limit headers.

    Returns {five_pct, five_resets_at, weekly_pct, weekly_resets_at} where
    percents are 0-100 ints and resets_at strings are formatted in local time.
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
        "anthropic-beta": "oauth-2025-04-20",
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    body = {
        "model": probe_model,
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "hi"}],
    }
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(_CLAUDE_USAGE_URL, headers=headers, json=body)
        r.raise_for_status()

    def _hf(name: str) -> float:
        try:
            return float(r.headers.get(name) or 0)
        except ValueError:
            return 0.0

    now_ts = time.time()
    five_util = _hf("anthropic-ratelimit-unified-5h-utilization")
    five_reset = _hf("anthropic-ratelimit-unified-5h-reset")
    week_util = _hf("anthropic-ratelimit-unified-7d-utilization")
    week_reset = _hf("anthropic-ratelimit-unified-7d-reset")

    # If a reset is already in the past, the server has rolled to a new window
    # but the headers may briefly lag — treat utilization as 0 defensively.
    if five_reset and five_reset < now_ts:
        five_util = 0.0
    if week_reset and week_reset < now_ts:
        week_util = 0.0

    return {
        "five_pct": min(100, max(0, int(round(five_util * 100)))),
        "five_resets_at": (
            datetime.fromtimestamp(five_reset).astimezone().strftime("%H:%M")
            if five_reset > 0 else "—"
        ),
        "weekly_pct": min(100, max(0, int(round(week_util * 100)))),
        "weekly_resets_at": (
            datetime.fromtimestamp(week_reset).astimezone().strftime("%a %H:%M").upper()
            if week_reset > 0 else "—"
        ),
    }


def _scan_cc_sessions() -> dict:
    """Walk ~/.claude/projects/**/*.jsonl mtimes for live/idle/today counts.
    Only stats files (no reads), so this is cheap even with hundreds of sessions.
    """
    out = {"cc_live": 0, "cc_idle": 0, "cc_today": 0}
    if not CLAUDE_PROJECTS_ROOT.is_dir():
        return out

    now_ts = datetime.now(timezone.utc).timestamp()
    today_midnight_ts = datetime.now().replace(
        hour=0, minute=0, second=0, microsecond=0
    ).timestamp()

    for proj in CLAUDE_PROJECTS_ROOT.iterdir():
        if not proj.is_dir():
            continue
        for f in proj.glob("*.jsonl"):
            try:
                mtime = f.stat().st_mtime
            except OSError:
                continue
            age_s = now_ts - mtime
            if age_s < 300:
                out["cc_live"] += 1
            elif age_s < 3600:
                out["cc_idle"] += 1
            if mtime >= today_midnight_ts:
                out["cc_today"] += 1
    return out


def _cc_sessions_payload(sessions: dict) -> dict:
    return {
        "live": sessions["cc_live"],
        "idle": sessions["cc_idle"],
        "total_today": sessions["cc_today"],
    }


async def claude_poll(interval: int = 300, probe_model: str = _CLAUDE_PROBE_MODEL_DEFAULT) -> None:
    """Real provider — powers both CLAUDE USAGE and CC SESSIONS panels.

    Usage data comes from Anthropic's rate-limit response headers, polled
    every `interval` seconds via a 1-token probe call. CC session counts
    come from a local jsonl mtime scan and are always emitted (even on
    usage-fetch failure) so that panel stays live.
    """
    while True:
        sessions = _scan_cc_sessions()
        try:
            creds = _read_claude_oauth()
            if not creds:
                STATE["providers"]["claude"] = {
                    "status": "error",
                    "error": "no Claude Code OAuth credentials found — run `claude` to authenticate",
                    "updated_at": _now_iso(),
                    "cc_sessions": _cc_sessions_payload(sessions),
                }
            elif creds.get("expires_at_ms") and creds["expires_at_ms"] / 1000 < time.time():
                STATE["providers"]["claude"] = {
                    "status": "error",
                    "error": "Claude Code OAuth token expired — open `claude` to refresh",
                    "updated_at": _now_iso(),
                    "cc_sessions": _cc_sessions_payload(sessions),
                }
            else:
                usage = await _fetch_claude_usage_headers(creds["access_token"], probe_model)
                STATE["providers"]["claude"] = {
                    "status": "ok",
                    "updated_at": _now_iso(),
                    "five_hour": {
                        "percent": usage["five_pct"],
                        "resets_at": usage["five_resets_at"],
                    },
                    "weekly": {
                        "percent": usage["weekly_pct"],
                        "resets_at": usage["weekly_resets_at"],
                    },
                    "cc_sessions": _cc_sessions_payload(sessions),
                }
        except httpx.HTTPStatusError as e:
            STATE["providers"]["claude"] = {
                "status": "error",
                "error": f"HTTP {e.response.status_code}: {e.response.text[:200]}",
                "updated_at": _now_iso(),
                "cc_sessions": _cc_sessions_payload(sessions),
            }
        except Exception as e:
            STATE["providers"]["claude"] = {
                "status": "error",
                "error": f"{type(e).__name__}: {e}",
                "updated_at": _now_iso(),
                "cc_sessions": _cc_sessions_payload(sessions),
            }
        await asyncio.sleep(interval)


NET_HISTORY_SIZE = 60  # 60 samples * 5s = 5min of net trace at the default poll interval


def _top_processes(n: int = 3) -> tuple[list[dict], list[dict]]:
    """Return (top_n_by_cpu, top_n_by_mem). CPU values are percent-of-one-core
    summed across threads, so an N-core machine can show values up to N*100.
    Memory is percent of physical RAM. Processes we can't read (zombies,
    System-protected) are skipped silently."""
    rows: list[dict] = []
    for p in psutil.process_iter(["pid", "name"]):
        try:
            cpu = p.cpu_percent(interval=None)
            mem = p.memory_percent()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        rows.append({
            "pid": p.info.get("pid"),
            "name": (p.info.get("name") or "?")[:40],
            "cpu": round(cpu, 1),
            "mem": round(mem, 1),
        })
    top_cpu = sorted(rows, key=lambda r: r["cpu"], reverse=True)[:n]
    top_mem = sorted(rows, key=lambda r: r["mem"], reverse=True)[:n]
    return top_cpu, top_mem


async def system_poll(interval: int = 5) -> None:
    """System stats via psutil. Network rates are computed between polls."""
    host = socket.gethostname().split(".")[0]
    boot_ts = psutil.boot_time()
    # On macOS Big Sur+ "/" is a tiny read-only system volume; the user's data
    # is on /System/Volumes/Data. Fall back to "/" on Linux / older macOS.
    disk_root = "/System/Volumes/Data" if os.path.isdir("/System/Volumes/Data") else "/"

    # Prime cpu_percent so the first real value isn't 0.0
    psutil.cpu_percent(interval=None)
    # Prime per-process CPU counters too so the first sampled poll has real values
    # rather than zeros for every PID.
    for _p in psutil.process_iter():
        try:
            _p.cpu_percent(interval=None)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
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

            top_cpu, top_mem = _top_processes()

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
                "top_cpu": top_cpu,
                "top_mem": top_mem,
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
        "anthropic": "https://status.claude.com/api/v2/summary.json",
        "openai":    "https://status.openai.com/api/v2/summary.json",
        "github":    "https://www.githubstatus.com/api/v2/summary.json",
        "linear":    "https://linearstatus.com/api/v2/summary.json",
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


_VPN_IFACE_RE = re.compile(r"^(utun|ipsec|wg|tun|tap|ppp)\d*$")


def _detect_vpn_interfaces() -> list[dict]:
    """Inspect local interfaces for likely VPN tunnels. Heuristic: any
    utun*/ipsec*/wg*/tun*/tap*/ppp* that's up AND has a non-link-local IPv4
    address. macOS uses several utun interfaces for Continuity/AirPlay even
    without a VPN, so we additionally require an IPv4 — those Apple services
    don't bind one.
    """
    found: list[dict] = []
    try:
        addrs = psutil.net_if_addrs()
        stats = psutil.net_if_stats()
    except Exception:
        return found
    for iface, ifaddrs in addrs.items():
        if not _VPN_IFACE_RE.match(iface):
            continue
        if iface not in stats or not stats[iface].isup:
            continue
        for a in ifaddrs:
            # AF_INET == 2 across platforms; comparing the family int avoids
            # importing socket constants here.
            if a.family != socket.AF_INET:
                continue
            ip = a.address or ""
            if not ip or ip.startswith("169.254."):
                continue
            found.append({"iface": iface, "ip": ip})
            break
    return found


async def network_poll(token: str | None = None, interval: int = 300) -> None:
    """Poll public IP / ISP / region via ipinfo.io and inspect local
    interfaces for VPN tunnels. ipinfo.io's free tier (no token) is ~1k/day
    and returns ip, city, region, country, org. A token raises the limit
    and adds optional fields.
    """
    url = "https://ipinfo.io/json"
    headers = {"User-Agent": "cowork-dash/0.1", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    prev_wan: str | None = None
    while True:
        try:
            async with httpx.AsyncClient(timeout=15, headers=headers) as c:
                r = await c.get(url)
                r.raise_for_status()
                data = r.json()
            wan_ip = data.get("ip") or ""
            org = data.get("org") or ""
            # ipinfo returns "AS1234 ISP Name"; split out the AS for compactness
            asn = ""
            isp = org
            if org and org.startswith("AS"):
                parts = org.split(" ", 1)
                if len(parts) == 2:
                    asn, isp = parts
            region_bits = [data.get("city"), data.get("region"), data.get("country")]
            region = ", ".join(b for b in region_bits if b)
            vpn_ifaces = _detect_vpn_interfaces()

            if prev_wan and wan_ip and wan_ip != prev_wan:
                _push_ticker("network", f"WAN ip changed {prev_wan} → {wan_ip}", "warn")
            prev_wan = wan_ip or prev_wan

            STATE["providers"]["network"] = {
                "status": "ok",
                "updated_at": _now_iso(),
                "wan_ip": wan_ip,
                "asn": asn,
                "isp": isp,
                "region": region,
                "vpn_active": bool(vpn_ifaces),
                "vpn_ifaces": vpn_ifaces,
            }
        except httpx.HTTPStatusError as e:
            # Even if ipinfo fails (rate-limit, offline), local VPN detection
            # still works — surface that and report the error separately.
            STATE["providers"]["network"] = {
                "status": "error",
                "updated_at": _now_iso(),
                "error": f"HTTP {e.response.status_code}: {e.response.text[:120]}",
                "vpn_active": bool(_detect_vpn_interfaces()),
                "vpn_ifaces": _detect_vpn_interfaces(),
            }
        except Exception as e:
            STATE["providers"]["network"] = {
                "status": "error",
                "updated_at": _now_iso(),
                "error": f"{type(e).__name__}: {e}",
                "vpn_active": bool(_detect_vpn_interfaces()),
                "vpn_ifaces": _detect_vpn_interfaces(),
            }
        await asyncio.sleep(interval)


# --------------------------------------------------------------------------- #
# Weather provider (real — zippopotam.us geocode + open-meteo forecast)
# --------------------------------------------------------------------------- #
# Two keyless APIs in series: (1) resolve postal code → lat/lon via
# zippopotam.us once at startup and cache it (zip → coords never changes),
# (2) poll Open-Meteo's /forecast endpoint for current temp + WMO weather
# code. Weather moves slowly so the default cadence is 15 minutes.

_ZIPPOPOTAM_URL = "https://api.zippopotam.us"
_OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

# WMO weather code → short uppercase label. Covers what Open-Meteo emits
# from `current.weather_code`. Unknown codes fall back to a numeric label
# so the chip stays non-empty.
_WMO_LABELS = {
    0:  "CLEAR",
    1:  "MOSTLY CLEAR",
    2:  "PARTLY CLOUDY",
    3:  "OVERCAST",
    45: "FOG",
    48: "FOG",
    51: "DRIZZLE",
    53: "DRIZZLE",
    55: "DRIZZLE",
    56: "FRZ DRIZZLE",
    57: "FRZ DRIZZLE",
    61: "RAIN",
    63: "RAIN",
    65: "HEAVY RAIN",
    66: "FRZ RAIN",
    67: "FRZ RAIN",
    71: "SNOW",
    73: "SNOW",
    75: "HEAVY SNOW",
    77: "SNOW GRAINS",
    80: "SHOWERS",
    81: "SHOWERS",
    82: "HEAVY SHOWERS",
    85: "SNOW SHOWERS",
    86: "SNOW SHOWERS",
    95: "T-STORM",
    96: "T-STORM W/ HAIL",
    99: "T-STORM W/ HAIL",
}


async def _geocode_zip(client: httpx.AsyncClient, zip_code: str, country: str) -> dict:
    """Resolve a postal code to lat/lon + place name via zippopotam.us.

    Returns {latitude, longitude, city, state}. Raises on HTTP error or
    when the API returns no places (unknown zip).
    """
    r = await client.get(f"{_ZIPPOPOTAM_URL}/{country.lower()}/{zip_code}")
    r.raise_for_status()
    data = r.json()
    places = data.get("places") or []
    if not places:
        raise ValueError(f"no place found for {country}/{zip_code}")
    place = places[0]
    return {
        "latitude": float(place["latitude"]),
        "longitude": float(place["longitude"]),
        "city": (place.get("place name") or "").strip(),
        "state": (place.get("state abbreviation") or place.get("state") or "").strip(),
    }


async def weather_poll(zip_code: str, country: str = "US",
                       units: str = "F", interval: int = 900,
                       label: str | None = None) -> None:
    """Poll current weather for a postal code via Open-Meteo.

    Geocoding (zip → lat/lon) is cached after the first successful resolution
    so subsequent polls only hit Open-Meteo. A transient geocoder failure
    just retries next cycle — it doesn't permanently disable the panel.
    """
    temp_unit = "fahrenheit" if units.upper().startswith("F") else "celsius"
    unit_label = "F" if temp_unit == "fahrenheit" else "C"
    geo: dict | None = None
    while True:
        try:
            async with httpx.AsyncClient(
                timeout=15, headers={"User-Agent": "cowork-dash/0.1"}
            ) as c:
                if geo is None:
                    geo = await _geocode_zip(c, zip_code, country)
                params = {
                    "latitude": geo["latitude"],
                    "longitude": geo["longitude"],
                    "current": "temperature_2m,weather_code",
                    "temperature_unit": temp_unit,
                    "timezone": "auto",
                }
                r = await c.get(_OPEN_METEO_URL, params=params)
                r.raise_for_status()
                data = r.json()
            current = data.get("current") or {}
            temp = current.get("temperature_2m")
            code = int(current.get("weather_code") or 0)
            place = (label or geo.get("city") or zip_code).strip()
            STATE["providers"]["weather"] = {
                "status": "ok",
                "updated_at": _now_iso(),
                "temp": round(float(temp), 1) if temp is not None else None,
                "unit": unit_label,
                "condition": _WMO_LABELS.get(code, f"WMO {code}"),
                "wmo_code": code,
                "location": place,
                "state": geo.get("state") or "",
                "zip": zip_code,
            }
        except httpx.HTTPStatusError as e:
            STATE["providers"]["weather"] = {
                "status": "error",
                "updated_at": _now_iso(),
                "error": f"HTTP {e.response.status_code}: {e.response.text[:120]}",
                "zip": zip_code,
            }
        except Exception as e:
            STATE["providers"]["weather"] = {
                "status": "error",
                "updated_at": _now_iso(),
                "error": f"{type(e).__name__}: {e}",
                "zip": zip_code,
            }
        await asyncio.sleep(interval)


# --------------------------------------------------------------------------- #
# Lifespan + app
# --------------------------------------------------------------------------- #

BACKGROUND_TASKS: list[asyncio.Task] = []


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    cfg = load_config()

    ui = cfg.get("ui") or {}
    operator = ui.get("operator_name") or os.environ.get("USER") or "OPERATOR"
    STATE["meta"]["operator"] = str(operator).upper()

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

    lin_blocks = cfg.get("linear") or []
    if not isinstance(lin_blocks, list):
        print("[cowork-dash] [linear] in config.toml must be an array of tables ([[linear]])", file=sys.stderr)
        lin_blocks = []
    seen_labels: set[str] = set()
    started_any = False
    for idx, block in enumerate(lin_blocks):
        if not isinstance(block, dict):
            print(f"[cowork-dash] [[linear]] entry #{idx} is not a table — skipping", file=sys.stderr)
            continue
        label = block.get("label")
        api_key = block.get("api_key")
        if not label or not api_key:
            print(f"[cowork-dash] [[linear]] entry #{idx} missing label or api_key — skipping", file=sys.stderr)
            continue
        if label in seen_labels:
            print(f"[cowork-dash] [[linear]] duplicate label '{label}' — keeping the first, skipping later", file=sys.stderr)
            continue
        seen_labels.add(label)
        STATE["providers"]["linear"][label] = {"status": "pending"}
        interval = max(_LINEAR_MIN_INTERVAL, block.get("poll_seconds", _LINEAR_DEFAULT_INTERVAL))
        state_types = block.get("show_state_types") or _LINEAR_DEFAULT_STATE_TYPES
        BACKGROUND_TASKS.append(asyncio.create_task(linear_poll(label, api_key, interval, state_types)))
        _push_ticker("daemon", f"linear provider online ({label})", "info")
        started_any = True

    if not started_any:
        STATE["providers"]["linear"] = {
            "status": "unconfigured",
            "message": "Add [[linear]] blocks to ~/.cowork-dash/config.toml",
        }

    cl = cfg.get("claude") or {}
    BACKGROUND_TASKS.append(asyncio.create_task(claude_poll(
        interval=cl.get("poll_seconds", 300),
        probe_model=cl.get("probe_model", _CLAUDE_PROBE_MODEL_DEFAULT),
    )))
    _push_ticker("daemon", "claude provider online", "info")
    sysc = cfg.get("system") or {}
    BACKGROUND_TASKS.append(asyncio.create_task(system_poll(sysc.get("poll_seconds", 5))))
    BACKGROUND_TASKS.append(asyncio.create_task(services_poll()))

    netc = cfg.get("network") or {}
    BACKGROUND_TASKS.append(asyncio.create_task(
        network_poll(netc.get("ipinfo_token"), netc.get("poll_seconds", 300))
    ))
    _push_ticker("daemon", "network provider online", "info")

    wx = cfg.get("weather") or {}
    if wx.get("zip"):
        BACKGROUND_TASKS.append(asyncio.create_task(weather_poll(
            zip_code=str(wx["zip"]),
            country=str(wx.get("country", "US")),
            units=str(wx.get("units", "F")),
            interval=max(60, int(wx.get("poll_seconds", 900))),
            label=wx.get("label"),
        )))
        _push_ticker("daemon", f"weather provider online ({wx['zip']})", "info")
    else:
        STATE["providers"]["weather"] = {
            "status": "unconfigured",
            "message": "Add [weather] zip to ~/.cowork-dash/config.toml",
        }

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


# Scratchpad endpoints are unauthenticated like the rest of the API — access
# control is the daemon's bind address ([server].host, loopback by default).
# Anyone who can reach the daemon can read and overwrite the scratchpad file,
# so only expose this on a trusted network. Size cap below stops a runaway
# client from filling the disk.
_SCRATCHPAD_MAX_BYTES = 256 * 1024


@app.get("/api/scratchpad")
async def scratchpad_get():
    try:
        text = SCRATCHPAD_PATH.read_text(encoding="utf-8") if SCRATCHPAD_PATH.exists() else ""
    except OSError as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    return {"content": text, "updated_at": _now_iso()}


@app.post("/api/scratchpad")
async def scratchpad_set(payload: dict = Body(...)):
    content = payload.get("content")
    if not isinstance(content, str):
        return JSONResponse({"error": "content must be a string"}, status_code=400)
    if len(content.encode("utf-8")) > _SCRATCHPAD_MAX_BYTES:
        return JSONResponse({"error": "content exceeds 256 KiB"}, status_code=413)
    try:
        SCRATCHPAD_PATH.parent.mkdir(parents=True, exist_ok=True)
        SCRATCHPAD_PATH.write_text(content, encoding="utf-8")
    except OSError as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    return {"ok": True, "bytes": len(content.encode("utf-8"))}


# Mount static last so /api/* takes priority.
if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


def main() -> None:
    import uvicorn
    cfg = load_config()
    server_cfg = cfg.get("server") or {}
    port = server_cfg.get("port", 7766)
    # Default to loopback only — this dashboard has no auth and surfaces
    # private data (GitHub, calendar, scratchpad). Set [server].host to
    # "0.0.0.0" or a specific LAN IP only on a trusted network.
    host = server_cfg.get("host", "127.0.0.1")
    if host not in ("127.0.0.1", "localhost"):
        print(
            f"[cowork-dash] WARNING: binding to {host} exposes this dashboard "
            "(GitHub data, calendar, scratchpad — no auth) to anyone who can "
            "reach this host on the network. Use only on a trusted LAN.",
            file=sys.stderr,
        )
    print(f"[cowork-dash] listening on http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
