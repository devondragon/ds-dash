# cowork-dash

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
static/index.html    Single-page frontend (HTML + inline CSS + inline JS)
config.example.toml  Starter config; copied to ~/.cowork-dash/config.toml on first run
run.sh               Bootstraps .venv, copies starter config, runs the daemon
requirements.txt     fastapi, uvicorn, httpx, psutil, tomli (3.10 fallback)
```

Config lives **outside** the repo at `~/.cowork-dash/config.toml` so secrets
never get committed. Override with `COWORK_DASH_CONFIG=/path/to/file`.

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
        "message": "Add [myprovider] api_key to ~/.cowork-dash/config.toml",
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
| claude   | 60s      | full `~/.claude` scan; not worth more frequent          |
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
`config.example.toml`; the real file is at `~/.cowork-dash/config.toml`
(outside the repo, `chmod 600`).

### Time

Use `_now_iso()` for every `updated_at`. UTC, ISO-8601. The frontend
converts to "Xs ago" relative to `Date.now()`.

## Frontend (`static/index.html`)

Single file, no build step. Three Google Font families: Inter + Bebas
Neue + IBM Plex Mono.

### Layout

- **Header strip** — REC pulse · `OPERATOR://DEVON [ON STATION]` · gold
  Bebas Neue timecode · CPU/MEM/UPTIME chip · 60×12 net sparkline ·
  weather.
- **3-column grid** of panels — left: Services/Claude/CC sessions ·
  center: Calendar/Tasks · right: GitHub/Heatmap.
- **Ticker** along the bottom — scrolling rolling events.
- **Left + right rails** (visible only at ≥ 2400px) — decorative +
  real-data hybrid. Left: TRACE FREQ + FREQ TRACE + radar sweep. Right:
  NET TRACE oscilloscope + BUS STATUS chips.

### Render functions

Each panel has a `renderXxx(p)` function, all called from `poll()` every
5s with that panel's slice of state. Renderers must handle
`status === 'pending'` (show `loading…`) and `status === 'error'` (show
the `.err` chip). To add a panel: add the HTML, write `renderXxx`, wire
into `poll()`.

### Real-time traces

`tracePath(hist, key, W, H, maxVal)` builds an SVG path string from a
history array. `updateNetTraces(hist)` writes that path into **both**
the 240×80 right-rail oscilloscope **and** the 60×12 header sparkline,
using the same auto-scaled max. To add another trace target (e.g. a CPU
oscilloscope): add an `id` to the SVG `<path>`, then a row to
`updateNetTraces`'s `targets` array.

### Rail data binding

Decorative rail rows bind to live state via HTML data attributes:

| attribute                | source                                | handler          |
|--------------------------|---------------------------------------|------------------|
| `data-bus="cpu"` etc.    | `sys.{cpu_percent, mem_percent, …}`   | `renderRail`     |
| `data-real="cpu"`        | `sys.cpu_percent`                     | `renderLeftRail` |
| `data-real="cc-live"`    | `claude.cc_sessions.live`             | `renderLeftRail` |
| `data-service="anthropic"` (etc.) | `services.items[name].indicator` → OK/MIN/MAJ/UNK | `renderLeftRail` |

To bind a new rail row: add the `data-X` attribute, then handle in the
relevant render function with `setRailValue(selector, text, stateCls)`.
State classes: `state-ok` (cyan), `state-warn` (amber), `state-alert`
(magenta), `state-dim`.

### Responsive breakpoints

| viewport          | mode             | behavior                                                              |
|-------------------|------------------|-----------------------------------------------------------------------|
| `≥ 2400px`        | 4K + rails       | rails appear, body becomes 3-col body grid, all chrome scales up      |
| `1401 – 2399px`   | desktop          | default 3-col panel grid                                              |
| `1025 – 1400px`   | narrow-desktop   | drop heatmap, GitHub row spans full width (`1fr 2fr`)                 |
| `641 – 1024px`    | tablet (iPad)    | 2-col grid, body scrolls (`overflow-y: auto`), tighter padding/gaps   |
| `≤ 640px`         | mobile           | 1-col, hide CC sessions / weather / sys chip / sparkline              |

Each `@media` block is mostly `:root` token overrides — no per-component
restyling. To add a new viewport (e.g., an ultrawide block at ≥3440px),
override the component tokens in `:root` rather than restating every
`.panel { padding: ... }` rule.

Body uses `height: 100vh; height: 100dvh;` — the second line lets iOS
Safari respect the URL bar. Tablet/mobile blocks release the
viewport-lock so panels stack and scroll.

**Touch devices** (`(hover: none) and (pointer: coarse)`) get larger tap
targets on chips and tabs, and hover-lift on `[data-url]` rows is
disabled (sticks after a tap on iPad).

**`prefers-reduced-motion: reduce`** disables the REC pulse, ticker
scroll, radar sweep, and blink cursor. Color transitions (0.12s hover)
stay.

**`prefers-color-scheme: light`** (only when no theme is saved in
localStorage) defaults to `theme-tron-light`. An explicit pick via the
THEME chip always wins and persists.

### Container queries

`.panel` declares `container-type: inline-size` and `container-name:
panel`. Use `@container panel (max-width: …)` for layout that should
respond to the panel's own width rather than the viewport — the same
Linear panel is ~720px wide in 3-col desktop and ~410px in 2-col tablet
portrait, so viewport-keyed media queries can't catch the difference.
Currently used to collapse `.task` row grids when the panel is below
380px.

## Design system — NIGHTOPS

Visual language is a synthesis of safehouse FUI (deep navy + cyan +
amber, restrained / military), Cyberpunk 2077 (slashed angular corners,
selective glow), and Tron (cold geometric, crisp). Avoid the
neon-green-on-black "Matrix screensaver" trap.

CSS variables are organized in three tiers (see **Token architecture**
below). Themes override the raw palette + a small per-theme alias block;
components consume scale and component tokens that breakpoints retune.

### Palette

| token        | hex        | role                                                |
|--------------|------------|-----------------------------------------------------|
| `--bg`       | `#03060f`  | near-black navy field                               |
| `--panel`    | `#0a1530`  | panel fill                                          |
| `--rule`     | `#1a3060`  | thin borders / dividers                             |
| `--dim`      | `#3a7090`  | muted cyan — default label color                    |
| `--cyan`     | `#5fc8e8`  | primary readout                                     |
| `--amber`    | `#ffaa44`  | live values, active task chip                       |
| `--gold`     | `#ffe88a`  | biggest readouts (timecode, primary numbers)        |
| `--magenta`  | `#ff2a6d`  | alarms only — REC dot, OFFLINE, alert chips         |

**Selective brightness** — most of the page is `--dim`. Only the
timecode, the "next" calendar event, and any genuine alarm are bright.
Avoid uniform glow.

### Type stack

| variable  | family          | use                                  |
|-----------|-----------------|--------------------------------------|
| `--sans`  | Inter           | labels, chrome, all-caps headers     |
| `--vt`    | Bebas Neue      | display readouts (timecode)          |
| `--mono`  | IBM Plex Mono   | tabular data, bars, codes, lists     |

### Token architecture

Three tiers. Each has a specific job — don't mix them.

**Tier 1 — palette** (`--bg`, `--panel`, `--rule`, `--dim`, `--cyan`,
`--amber`, `--gold`, `--magenta`, `--primary-deep`, `--primary-deeper`,
`--alarm`) plus their `*-rgb` triplets (e.g. `--cyan-rgb: 95 200 232`)
for alpha tinting via `rgb(var(--cyan-rgb) / 0.18)`. Themes override
**only this tier**, plus the per-theme tint knobs `--scanline-alpha`
and `--glow-strong/--glow-soft` (light themes dial halos down).

**Tier 2 — aliases** (`--primary`, `--accent`, `--warn`, `--alert`,
`--border`, `--bg-panel`, `--primary-dim`, `--header-amber`). Reference
Tier 1 so components don't bind directly to color names. **GOTCHA — must
be redeclared inside every theme block.** CSS `var()` substitution
resolves on the *declaring element*, not at use site (CSS Variables
Level 1 §3.2). An alias declared only in `:root` locks to `:root`'s
value and never picks up theme overrides. Every theme re-declares the
alias block at the bottom — verified empirically across all four themes.
Keep it that way.

**Tier 3 — scale + component tokens** declared once in `:root`,
overridden in `@media` blocks. Two scales:

- `--fs-2xs..xl` (9–15px) + `--fs-display{,-sm,-md,-lg}` for the
  timecode tier.
- `--space-1..10` (2–32px on a roughly 2px step).

Component tokens consume the scales. Padding splits y/x so a viewport
can tighten one axis without touching the other:

- `--pad-body`, `--pad-panel-y`, `--pad-panel-x`
- `--pad-hdr-y`, `--pad-hdr-x`, `--pad-hdr-x-l` (asymmetric left to
  balance the slashed corner)
- `--pad-ticker-y`, `--pad-ticker-x`
- `--pad-tab-y`, `--pad-tab-x`
- `--gap-panels`
- `--fs-body`, `--fs-hdr-chip`, `--fs-hdr-display`, `--fs-panel-title`,
  `--fs-tab`, `--fs-ticker`

To retune at a new breakpoint: override the component tokens in `:root`
inside the `@media` block, never the components themselves.

### Panel chrome

Every `.panel` paints its slashed top-left corner via stacked `::before`
/ `::after` pseudos with matching `clip-path`. Outer pseudo (`inset: 0`)
= `--rule`; inner (`inset: 1px`) = `--bg-panel`. The 1px offset between
them paints the 1px slashed border ring. Children sit at `z-index: 1`
above the pseudos. `.panel` itself is `background: transparent; border:
none` — the chrome is entirely pseudo-painted.

The `.panel-nightops` modifier (currently only on GitHub) is a
tabbed-layout variant — it zeroes `.panel`'s padding so a `.panel-tabs`
strip can span edge-to-edge and an inner `.panel-body` wrapper provides
the content padding. Optional decorative `.panel-stamp` (e.g.
`LK-R3 · SP1-7`) sits bottom-right.

## Current provider state

All eight panels are real:

| panel         | source                                                                    |
|---------------|---------------------------------------------------------------------------|
| github        | api.github.com (PAT in config)                                            |
| services      | public status.json endpoints (Anthropic / OpenAI / GH / Linear / Vercel)  |
| system        | psutil — cpu, mem, disk, net (+ 5-min rolling history for trace)          |
| calendar      | ical-buddy (Homebrew, auto-detected)                                      |
| tasks         | api.usemotion.com /v1/tasks (with recurring-template dedup)               |
| linear        | api.linear.app GraphQL — one [[linear]] block per workspace, separate panel each |
| claude usage  | scan of `~/.claude/projects/**/*.jsonl` for user-message timestamps       |
| cc sessions   | same scan, jsonl mtime → live / idle / today                              |

Planned but not yet shipped: gmail.
