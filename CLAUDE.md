# cowork-dash

A local-only personal dashboard. A Python daemon polls a handful of providers
on a schedule and writes their state into a single in-memory blob; a single
static HTML page polls that blob every 5s and renders a cyberpunk-themed grid
of panels.

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
static/index.html    Single-page frontend (HTML + inline CSS + inline JS)
config.example.toml  Starter config; copied to ~/.cowork-dash/config.toml on first run
run.sh               Bootstraps .venv, copies starter config, runs the daemon
requirements.txt     fastapi, uvicorn, httpx, tomli (3.10 fallback)
```

Config lives **outside** the repo at `~/.cowork-dash/config.toml` so secrets
never get committed. Override with `COWORK_DASH_CONFIG=/path/to/file`.

## Provider polling pattern

Every provider is an `async def` coroutine that loops forever, writes a
result dict to `STATE["providers"][name]`, and sleeps. The result dict must
include `status` and `updated_at`; everything else is provider-specific and
the frontend renderer for that panel decides how to use it.

Skeleton for a new provider:

```python
async def myprovider_poll(api_key: str, interval: int = 60) -> None:
    """One-line description of what this polls."""
    while True:
        result: dict[str, Any] = {"updated_at": _now_iso()}
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.get("https://api.example.com/thing",
                                headers={"Authorization": f"Bearer {api_key}"})
                r.raise_for_status()
                data = r.json()
                # ...shape `data` into the keys the frontend renderer expects...
                result["items"] = [...]
            result["status"] = "ok"
            STATE["providers"]["myprovider"] = result
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
    BACKGROUND_TASKS.append(
        asyncio.create_task(myprovider_poll(mp["api_key"], mp.get("poll_seconds", 60)))
    )
    _push_ticker("daemon", "myprovider online", "info")
else:
    STATE["providers"]["myprovider"] = {
        "status": "unconfigured",
        "message": "Add [myprovider] api_key to ~/.cowork-dash/config.toml",
    }
```

Also add the corresponding `[myprovider]` block to `config.example.toml` and
seed `"myprovider": {"status": "pending"}` in the `STATE["providers"]` dict
so the frontend sees the key from the first poll cycle.

## Status state vocabulary

Every `STATE["providers"][name]["status"]` is one of:

| status         | meaning                                                                    | frontend treatment        |
|----------------|----------------------------------------------------------------------------|---------------------------|
| `pending`      | seeded at startup, no poll has completed yet                               | shows `loading…`          |
| `ok`           | last poll succeeded with real data                                         | shows `Xs/Xm ago`         |
| `mocked`       | provider is returning placeholder data (real impl not yet wired)           | shows amber `MOCK` chip   |
| `unconfigured` | no credentials in config; daemon skipped starting the real poller         | shows `OFFLINE` + message |
| `error`        | last poll raised; result also carries an `error` string                    | shows red `ERR` chip      |

Reserve these five strings — don't invent new ones. The frontend's `metaTag`
helper in `static/index.html` only knows these.

## Conventions

**Poll intervals** — pick one that respects the upstream's rate limit and
matches how fast the data actually changes. Current defaults:

| provider | interval | reason                                                  |
|----------|----------|---------------------------------------------------------|
| github   | 180s     | search API is rate-limited; activity moves in minutes   |
| services | 300s     | public status pages change rarely                       |
| calendar | 60s      | "next event" needs minute-level freshness               |
| tasks    | 30s      | feels responsive when checking things off               |
| claude   | 15s      | usage bars should feel alive                            |
| system   | 5s       | CPU/MEM are local + cheap                               |

Anything that hits a third-party API: minimum 30s, prefer 60s+. Anything
local-only: 5–15s is fine.

**Error handling** — catch broadly, write `status: "error"` with a short
`error` string. Never let an exception escape the `while True` — that kills
the task and the panel goes stale forever.

**Ticker events** — only push when something actually happened (a new PR
review request, a contribution count delta, a service going from `ok` to
`major`). Don't push on every poll. Levels are `info` / `warn` / `alert`
and map to color in the frontend.

**Secrets** — read from `config.toml` only. Never read from environment
variables for provider credentials, never log a token. Config sample lives
in `config.example.toml`; the real file is at `~/.cowork-dash/config.toml`
(outside the repo).

**Time** — use `_now_iso()` for every `updated_at`. UTC, ISO-8601. The
frontend converts to "Xs ago" relative to `Date.now()`.

**Frontend** — single file, no build step, no dependencies beyond two
Google Fonts. If a panel needs a new renderer, add a `renderFoo(p)`
function alongside the others and call it from `poll()`. Renderers must
handle `status === 'pending'` (show `loading…`) and `status === 'error'`
(show the `err` chip).
