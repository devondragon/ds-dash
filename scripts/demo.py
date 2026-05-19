"""
ds-dash demo server — serves the dashboard with a hand-crafted mock STATE
blob so the UI can be previewed (or screenshotted for the README) without
any real credentials or polling.

    python scripts/demo.py [--port 7777]

Then open http://localhost:7777/. No config file is read; nothing writes
to disk. Scratchpad POSTs are accepted but discarded.
"""

from __future__ import annotations

import argparse
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Body, FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "static"


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _ago(seconds: int) -> str:
    return _iso(datetime.now(timezone.utc) - timedelta(seconds=seconds))


def _today_ymd() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _heatmap_days(n: int = 365) -> list[dict]:
    # Plausible contribution graph: a baseline with occasional spikes,
    # weekend dips, and a stronger recent week.
    rng = random.Random(20260518)
    today = datetime.now().date()
    out = []
    for i in range(n):
        d = today - timedelta(days=n - 1 - i)
        weekend = d.weekday() >= 5
        base = rng.choices([0, 0, 1, 2, 3, 5, 8], weights=[3, 2, 4, 4, 3, 2, 1])[0]
        if weekend:
            base = max(0, base - 2)
        # boost the last 10 days a little for visual interest
        if i >= n - 10:
            base += rng.randint(0, 4)
        out.append({"date": d.isoformat(), "count": base})
    return out


def _net_history(samples: int = 60) -> list[dict]:
    rng = random.Random(7)
    now = datetime.now(timezone.utc)
    out = []
    for i in range(samples):
        ts = now - timedelta(seconds=(samples - i) * 5)
        up = max(0.05, rng.gauss(0.8, 0.4))
        down = max(0.1, rng.gauss(4.2, 1.5))
        # one synthetic spike near the right edge so the oscilloscope has shape
        if i == samples - 8:
            down += 6.0
        if i == samples - 7:
            up += 1.5
        out.append({"ts": _iso(ts), "up": round(up, 2), "down": round(down, 2)})
    return out


def _state() -> dict:
    return {
        "started_at": _ago(3 * 3600 + 17 * 60),
        "meta": {"operator": "AGENT.K"},
        "providers": {
            "github": {
                "status": "ok",
                "updated_at": _ago(42),
                "username": "octocat",
                "review_requested": {
                    "count": 2,
                    "items": [
                        {"number": 1142, "title": "auth: rotate session keys on logout", "repo": "octocat/api-gateway", "url": "#", "draft": False},
                        {"number": 87,   "title": "ui: tidy up the empty state on /inbox", "repo": "octocat/console-ui", "url": "#", "draft": False},
                    ],
                },
                "my_open_prs": {
                    "count": 3,
                    "items": [
                        {"number": 311, "title": "feat: weather provider via Open-Meteo", "repo": "octocat/ds-dash", "url": "#", "draft": False},
                        {"number": 248, "title": "perf: cache contribution graph for 24h", "repo": "octocat/api-gateway", "url": "#", "draft": False},
                        {"number": 19,  "title": "wip: extract render funcs to modules", "repo": "octocat/console-ui", "url": "#", "draft": True},
                    ],
                },
                "issues_assigned": {
                    "count": 4,
                    "items": [
                        {"number": 502, "title": "intermittent 502 on /api/state under load", "repo": "octocat/api-gateway", "url": "#"},
                        {"number": 488, "title": "calendar panel: handle DST rollover gracefully", "repo": "octocat/ds-dash", "url": "#"},
                        {"number": 471, "title": "linear: dedupe issues across cycles", "repo": "octocat/ds-dash", "url": "#"},
                        {"number": 410, "title": "docs: document the scratchpad endpoint", "repo": "octocat/ds-dash", "url": "#"},
                    ],
                },
                "recent_events": {
                    "items": [
                        {"kind": "pr",       "cls": "pr",     "detail": "merged #311 weather provider", "repo": "octocat/ds-dash",      "at": _ago(60 * 7),       "url": "#"},
                        {"kind": "review",   "cls": "review", "detail": "approved #87 inbox empty state","repo": "octocat/console-ui",  "at": _ago(60 * 22),      "url": "#"},
                        {"kind": "push",     "cls": "push",   "detail": "pushed 3 commits to main",      "repo": "octocat/ds-dash",      "at": _ago(60 * 48),      "url": "#"},
                        {"kind": "issue",    "cls": "issue",  "detail": "opened #502 502 under load",    "repo": "octocat/api-gateway", "at": _ago(60 * 90),      "url": "#"},
                        {"kind": "release",  "cls": "release","detail": "released v0.4.2",              "repo": "octocat/api-gateway", "at": _ago(60 * 60 * 4),  "url": "#"},
                        {"kind": "comment",  "cls": "comment","detail": "commented on #471",            "repo": "octocat/ds-dash",      "at": _ago(60 * 60 * 6),  "url": "#"},
                        {"kind": "create",   "cls": "create", "detail": "created branch feat/widgets",   "repo": "octocat/console-ui",  "at": _ago(60 * 60 * 9),  "url": "#"},
                        {"kind": "star",     "cls": "star",   "detail": "starred fastapi/fastapi",       "repo": "fastapi/fastapi",      "at": _ago(60 * 60 * 14), "url": "#"},
                    ],
                },
                "commits_today": 7,
                "heatmap": {
                    "total_year": 1842,
                    "recent_days": _heatmap_days(365),
                },
            },
            "calendar": {
                "status": "ok",
                "updated_at": _ago(15),
                "events": [
                    {"start": "09:30", "start_date": _today_ymd(), "title": "Standup — platform pod",         "is_now": False, "is_next": False},
                    {"start": "11:00", "start_date": _today_ymd(), "title": "1:1 with Morgan",                "is_now": True,  "is_next": False},
                    {"start": "13:30", "start_date": _today_ymd(), "title": "API review — auth refactor",     "is_now": False, "is_next": True},
                    {"start": "15:00", "start_date": _today_ymd(), "title": "Office hours",                   "is_now": False, "is_next": False},
                    {"start": "10:00", "start_date": (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d"), "title": "Quarterly planning",     "is_now": False, "is_next": False},
                    {"start": "14:00", "start_date": (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d"), "title": "Design review — console", "is_now": False, "is_next": False},
                ],
            },
            "tasks": {
                "status": "ok",
                "updated_at": _ago(28),
                "open_count": 6,
                "items": [
                    {"priority": "high", "source": "MOTION", "title": "Draft Q3 roadmap deck",                 "due": "TODAY",  "url": "#"},
                    {"priority": "high", "source": "MOTION", "title": "Reply to security questionnaire",      "due": "TODAY",  "url": "#"},
                    {"priority": "med",  "source": "MOTION", "title": "Schedule on-call rotation for August", "due": "1D",     "url": "#"},
                    {"priority": "med",  "source": "MOTION", "title": "Review vendor renewals",                "due": "2D",     "url": "#"},
                    {"priority": "low",  "source": "MOTION", "title": "Write up postmortem for #502",          "due": "3D",     "url": "#"},
                    {"priority": "low",  "source": "MOTION", "title": "Refresh onboarding doc",                "due": "5D",     "url": "#"},
                ],
            },
            "linear": {
                "platform": {
                    "status": "ok",
                    "updated_at": _ago(33),
                    "cycle": {
                        "present": True,
                        "number": 42,
                        "ends_in_days": 4,
                        "progress_pct": 64,
                        "completed": 16,
                        "total": 25,
                        "multi_team": False,
                    },
                    "issues": {
                        "open_count": 9,
                        "items": [
                            {"priority": "high", "team": "PLT", "identifier": "PLT-218", "title": "Investigate 502 spike on /api/state", "due": "TODAY",   "url": "#"},
                            {"priority": "high", "team": "PLT", "identifier": "PLT-214", "title": "Stand up read replica for analytics",  "due": "1D",      "url": "#"},
                            {"priority": "med",  "team": "PLT", "identifier": "PLT-209", "title": "Retire legacy auth middleware",        "due": "3D",      "url": "#"},
                            {"priority": "med",  "team": "PLT", "identifier": "PLT-204", "title": "Bump runtime to Python 3.12",          "due": "5D",      "url": "#"},
                            {"priority": "low",  "team": "PLT", "identifier": "PLT-196", "title": "Document the on-call runbook",         "due": "",        "url": "#"},
                        ],
                    },
                },
            },
            "claude": {
                "status": "ok",
                "updated_at": _ago(91),
                "five_hour": {"percent": 38, "resets_at": "18:00"},
                "weekly":    {"percent": 62, "resets_at": "Mon 00:00"},
                "cc_sessions": {"live": 1, "idle": 3, "total_today": 12},
            },
            "services": {
                "status": "ok",
                "updated_at": _ago(120),
                "items": {
                    "anthropic": {"indicator": "none",  "description": "All Systems Operational"},
                    "openai":    {"indicator": "minor", "description": "Elevated error rates on Assistants API"},
                    "github":    {"indicator": "none",  "description": "All Systems Operational"},
                    "linear":    {"indicator": "none",  "description": "All Systems Operational"},
                    "vercel":    {"indicator": "none",  "description": "All Systems Operational"},
                },
            },
            "system": {
                "status": "ok",
                "updated_at": _ago(2),
                "cpu_percent": 23,
                "mem_percent": 47,
                "disk_percent": 61,
                "net_up_mbps": 0.84,
                "net_down_mbps": 5.21,
                "net_history": _net_history(60),
                "uptime": "4d 11h 32m",
                "host": "agent-k.local",
                "top_cpu": [
                    {"name": "claude",     "pid": 4821, "cpu": 18.4, "mem": 6.2},
                    {"name": "Code",       "pid": 1109, "cpu": 11.2, "mem": 12.7},
                    {"name": "Chrome",     "pid": 2244, "cpu":  8.6, "mem": 18.3},
                    {"name": "python3.12", "pid": 7733, "cpu":  3.1, "mem":  2.4},
                    {"name": "kernel_task","pid":    0, "cpu":  2.7, "mem":  1.1},
                ],
                "top_mem": [
                    {"name": "Chrome",     "pid": 2244, "cpu":  8.6, "mem": 18.3},
                    {"name": "Code",       "pid": 1109, "cpu": 11.2, "mem": 12.7},
                    {"name": "Slack",      "pid": 3372, "cpu":  0.4, "mem":  9.8},
                    {"name": "claude",     "pid": 4821, "cpu": 18.4, "mem":  6.2},
                    {"name": "Spotify",    "pid": 5510, "cpu":  0.2, "mem":  4.1},
                ],
            },
            "network": {
                "status": "ok",
                "updated_at": _ago(60),
                "wan_ip": "203.0.113.42",
                "isp": "Example Networks",
                "region": "Portland, OR · US",
                "asn": "AS64500",
                "vpn_active": True,
                "vpn_ifaces": [{"iface": "utun4", "ip": "10.8.0.6"}],
            },
            "weather": {
                "status": "ok",
                "updated_at": _ago(180),
                "temp": 68,
                "unit": "F",
                "location": "Portland",
                "condition": "PARTLY CLOUDY",
            },
        },
        "ticker": [
            {"ts": _ago(15),       "source": "github",   "text": "review requested: PR #1142 octocat/api-gateway", "level": "warn"},
            {"ts": _ago(120),      "source": "linear",   "text": "PLT-218 marked high priority",                   "level": "warn"},
            {"ts": _ago(420),      "source": "github",   "text": "merged PR #311 octocat/ds-dash",                 "level": "info"},
            {"ts": _ago(780),      "source": "services", "text": "openai status → minor (Assistants API)",          "level": "warn"},
            {"ts": _ago(1320),     "source": "calendar", "text": "1:1 with Morgan starts in 5m",                   "level": "info"},
            {"ts": _ago(1800),     "source": "daemon",   "text": "ds-dash online",                                  "level": "info"},
            {"ts": _ago(3600),     "source": "github",   "text": "approved PR #87 octocat/console-ui",             "level": "info"},
            {"ts": _ago(5400),     "source": "claude",   "text": "5-hour usage crossed 30%",                       "level": "info"},
        ],
    }


app = FastAPI(title="ds-dash (demo)")

_SCRATCHPAD = (
    "demo notes — the scratchpad autosaves locally to ~/.ds-dash/scratchpad.txt.\n"
    "in this demo build, writes are accepted but discarded.\n\n"
    "- finish the auth refactor RFC\n"
    "- prep talking points for 1:1\n"
    "- file follow-up for #502\n"
)


@app.get("/api/state.json")
async def state_endpoint():
    return JSONResponse(_state())


@app.get("/api/health")
async def health():
    return {"ok": True, "demo": True}


@app.get("/api/scratchpad")
async def scratchpad_get():
    return {"content": _SCRATCHPAD, "updated_at": _iso(datetime.now(timezone.utc))}


@app.post("/api/scratchpad")
async def scratchpad_set(payload: dict = Body(...)):
    # Accept and discard — demo is read-only.
    content = payload.get("content", "")
    return {"ok": True, "bytes": len(content.encode("utf-8")), "demo": True}


if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="ds-dash demo server (mock data)")
    parser.add_argument("--port", type=int, default=7777)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    if not STATIC_DIR.exists():
        print(f"[demo] static dir not found at {STATIC_DIR}", file=sys.stderr)
        sys.exit(1)

    print(f"[demo] mock dashboard at http://{args.host}:{args.port}/")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
