# ds-dash

A local-only personal dashboard. A Python daemon polls a handful of providers
on a schedule and writes their state into a single in-memory blob; a single
static HTML page polls that blob every 5s and renders a cinematic ops
console.

Everything runs on `localhost`. No build step. No database. State is the
process memory; on restart, providers re-poll from scratch.

## Architecture

```
┌─────────────────┐   asyncio.create_task per provider   ┌──────────────┐
│   daemon.py     │ ──────────────────────────────────▶  │  STATE dict  │
│  (FastAPI app)  │ ◀────── GET /api/state.json ──────── │   in-memory  │
└────────┬────────┘                                       └──────────────┘
         │ StaticFiles mount on "/"
         ▼
   static/index.html  ── fetch /api/state.json every 5s ──▶ renders panels
```

- `daemon.py` owns one `FastAPI` app. Its `lifespan` handler kicks off a
  background `asyncio.Task` for each provider, then yields. Tasks are
  cancelled on shutdown.
- Every provider writes to `STATE["providers"][<name>]`. The frontend reads
  the whole blob via `GET /api/state.json` and renders defensively (missing
  fields and missing providers are tolerated).
- Cross-cutting events go to `STATE["ticker"]` via `_push_ticker(source,
  text, level)`. The ticker is a capped list of the 50 most recent events;
  the frontend scrolls them across the bottom strip.

## File map

```
daemon.py            FastAPI app, STATE blob, all provider pollers
static/index.html    Frontend markup (references /app.css and /app.js)
static/app.css       All styles, themes, responsive blocks
static/app.js        All client JS — render functions, polling, theme cycler
config.example.toml  Starter config; copied to ~/.ds-dash/config.toml on first run
run.sh               Bootstraps .venv, copies starter config, runs the daemon
requirements.txt     fastapi, uvicorn, httpx, psutil, tomli (3.10 fallback)
docs/FRONTEND.md     Layout, rendering, NIGHTOPS palette + tokens (load on demand)
```

Config lives **outside** the repo at `~/.ds-dash/config.toml` so secrets
never get committed. Override with `DS_DASH_CONFIG=/path/to/file`.

## Provider polling pattern

Every provider is an `async def` coroutine that loops forever, writes a
result dict to `STATE["providers"][name]`, and sleeps. The result dict
must include `status` and `updated_at`; everything else is
provider-specific and the renderer for that panel decides how to use it.

### API-backed provider (skeleton)

```python
async def myprovider_poll(api_key: str, interval: int = 60) -> None:
    """One-line description of what this polls."""
    headers = {"Authorization": f"Bearer {api_key}"}
    while True:
        try:
            async with httpx.AsyncClient(timeout=20, headers=headers) as c:
                r = await c.get("https://api.example.com/thing")
                r.raise_for_status()
                data = r.json()
            STATE["providers"]["myprovider"] = {
                "status": "ok",
                "updated_at": _now_iso(),
                "items": [...],   # shape per the renderer
            }
        except httpx.HTTPStatusError as e:
            STATE["providers"]["myprovider"] = {
                "status": "error",
                "error": f"HTTP {e.response.status_code}: {e.response.text[:200]}",
                "updated_at": _now_iso(),
            }
        except Exception as e:
            STATE["providers"]["myprovider"] = {
                "status": "error",
                "error": f"{type(e).__name__}: {e}",
                "updated_at": _now_iso(),
            }
        await asyncio.sleep(interval)
```

Wiring it up in `lifespan`:

```python
mp = cfg.get("myprovider") or {}
if mp.get("api_key"):
    BACKGROUND_TASKS.append(asyncio.create_task(
        myprovider_poll(mp["api_key"], mp.get("poll_seconds", 60))
    ))
    _push_ticker("daemon", "myprovider online", "info")
else:
    STATE["providers"]["myprovider"] = {
        "status": "unconfigured",
        "message": "Add [myprovider] api_key to ~/.ds-dash/config.toml",
    }
```

Also: add the `[myprovider]` block to `config.example.toml`, and seed
`"myprovider": {"status": "pending"}` in `STATE["providers"]` so the
frontend sees the key from the first render.

### Local-only provider

`system_poll` (psutil), `claude_poll` (`~/.claude/projects` jsonl scan),
and `calendar_poll` (auto-detected ical-buddy) follow the same shape but
skip the API-key check. They either always run or auto-detect their
requirements. See those functions in `daemon.py` for working patterns.

### Subprocess provider

`calendar_poll` uses `asyncio.create_subprocess_exec` (argv array, no
shell) to call ical-buddy. On `FileNotFoundError` or `OSError` (e.g.
wrong-arch binary), it sets `status: "unconfigured"` and **`return`**s.
Permanent config issues shouldn't loop forever — retry only transient
failures.

## Status state vocabulary

Every `STATE["providers"][name]["status"]` is one of:

| status         | meaning                                                                    | frontend treatment        |
|----------------|----------------------------------------------------------------------------|---------------------------|
| `pending`      | seeded at startup, no poll has completed yet                               | shows `loading…`          |
| `ok`           | last poll succeeded with real data                                         | shows `Xs/Xm ago`         |
| `mocked`       | provider is returning placeholder data (real impl not yet wired)           | shows amber `MOCK` chip   |
| `unconfigured` | no credentials in config; daemon skipped starting the real poller         | shows `OFFLINE` + message |
| `error`        | last poll raised; result also carries an `error` string                    | shows red `ERR` chip      |

Reserve these five strings — don't invent new ones. The frontend's
`metaTag` helper in `static/index.html` only recognizes these.

## Conventions

### Poll intervals

Current defaults:

| provider | interval | reason                                                  |
|----------|----------|---------------------------------------------------------|
| github   | 180s     | search API is rate-limited; activity moves in minutes   |
| services | 300s     | public status pages change rarely                       |
| calendar | 60s      | "next event" needs minute-level freshness               |
| motion   | 60s      | API rate-limit friendly; tasks change at minute scale   |
| linear   | 90s      | per-workspace; complexity-based rate limit, well under budget |
| claude   | 300s     | each poll burns one Anthropic API token (1-token probe) |
| network  | 300s     | ipinfo.io free tier is ~1k/day; 288/day at this rate    |
| weather  | 900s     | weather moves slowly; Open-Meteo is generous regardless |
| system   | 5s       | local + cheap, feeds net-trace history samples          |

Anything hitting a third-party API: minimum 30s, prefer 60s+. Local-only:
5–60s is fine.

### Error handling

Catch broadly, write `status: "error"` with a short `error` string.
**Never let an exception escape the `while True`** — that kills the
task and the panel goes stale forever. Permanent config errors should
set `status: "unconfigured"` and `return` instead.

### Ticker events

Only push when something actually happened (a new PR review request,
contribution-count delta, service going from `ok` to `major`). Don't
push on every poll. Levels are `info` / `warn` / `alert` and map to
color in the frontend.

### Secrets

Read from `config.toml` only. Never read environment variables for
provider credentials, never log a token. Config sample lives in
`config.example.toml`; the real file is at `~/.ds-dash/config.toml`
(outside the repo, `chmod 600`).

### Time

Use `_now_iso()` for every `updated_at`. UTC, ISO-8601. The frontend
converts to "Xs ago" relative to `Date.now()`.

## Frontend & design system

The frontend lives in `static/index.html` + `static/app.css` +
`static/app.js` (no build step — `index.html` references the two assets
directly, both served by the same `StaticFiles` mount). A tiny inline
theme-loader at the top of `<body>` runs pre-paint to avoid theme flash;
everything else is in `app.js`.

**Detailed reference: `docs/FRONTEND.md`.** Read it when you're touching
the UI. Covers layout, render-function conventions, real-time trace
plumbing, rail data binding, responsive breakpoints + container
queries, the NIGHTOPS palette, type stack, three-tier token
architecture, and panel chrome.

## Current provider state

Every provider is real — there are no mock pollers in the codebase.

| panel         | source                                                                    |
|---------------|---------------------------------------------------------------------------|
| github        | api.github.com (PAT in config) — REST search + GraphQL contributions      |
| services      | public status.json endpoints (Anthropic / OpenAI / GH / Linear / Vercel)  |
| system        | psutil — cpu, mem, disk, net (+ 5-min rolling history for trace)          |
| calendar      | ical-buddy (Homebrew, auto-detected on macOS)                             |
| tasks         | api.usemotion.com /v1/tasks (with recurring-template dedup)               |
| linear        | api.linear.app GraphQL — one [[linear]] block per workspace, separate panel each |
| claude usage  | Anthropic rate-limit headers via 1-token probe; OAuth from disk / keychain |
| cc sessions   | scan of `~/.claude/projects/**/*.jsonl` — jsonl mtime → live / idle / today |
| network       | local interfaces (psutil) + ipinfo.io for WAN/ISP/region                  |
| weather       | zippopotam.us (zip → lat/lon, cached) + Open-Meteo current forecast       |

Planned but not yet shipped: gmail.
