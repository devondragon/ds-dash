# Frontend & design system

Detailed reference for the dashboard frontend — layout, render
conventions, real-time traces, rail bindings, responsive breakpoints,
container queries, and the NIGHTOPS visual language (palette, type
stack, token tiers, panel chrome).

This file is extracted from `CLAUDE.md` and intentionally not auto-loaded
— read it when you're touching `static/index.html`, `static/app.css`, or
`static/app.js`.

## Frontend (`static/index.html` + `app.css` + `app.js`)

No build step — `index.html` references `/app.css` and `/app.js` directly
(both served by the same `StaticFiles` mount). The only inline script is
a tiny synchronous theme-loader at the top of `<body>` that runs
pre-paint to avoid a flash of the wrong theme; everything else lives in
`app.js`. Three Google Font families: Inter + Bebas
Neue + IBM Plex Mono.

### Layout

- **Header strip** — REC pulse · `OPERATOR://<NAME> [ON STATION]` · gold
  Bebas Neue timecode · CPU/MEM/UPTIME chip · 60×12 net sparkline ·
  weather. The operator label is read from `state.meta.operator`, which the
  daemon populates from `[ui].operator_name` (config) or `$USER`.
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

### iPad layout classes

Two body classes handle what media queries can't, both set by
`applyIpadLayout()` in `app.js` (on boot and on resize). `is-ipad` itself
comes from the pre-paint UA check in `index.html`.

| class            | when                                    | effect                                                                 |
|------------------|-----------------------------------------|------------------------------------------------------------------------|
| `linear-col1`    | `is-ipad` **and** col-1 has ≥105px free | `#linear-panels` is **moved in the DOM** to col-1; raises Tasks/Linear row caps |
| `ipad-landscape` | `is-ipad` + height < 1100px             | hides Network ISP/ASN + panel stamps, 6 proc rows, shorter header, deterministic column shares |

Why a DOM move: `is-ipad` hides CC Sessions and Scratchpad, so col-1 ends up
with dead space (~275px portrait, ~185px landscape) that no panel in it can
claim, while col-2 carries Calendar + Tasks + Linear. CSS cannot move a panel
between grid columns, so `applyIpadLayout()` reparents the wrapper;
`renderLinear()` is unaffected because it looks the wrapper up by id.

The placement is **measured, not assumed**: Safari's chrome makes an iPad
landscape viewport anywhere from ~740px to 834px tall, and at the short end
col-1's own panels fill it. So `applyIpadLayout()` tries col-1, reads back what
Linear actually got, and falls back to col-2 if it's under 105px (title only).

Row caps are per-layout, since column height differs by rotation — hide the
Nth `.task` onward with a sibling chain. Never re-show a capped row with
`display: revert`: that discards the author `.task { display: grid }` and the
row collapses to an unaligned block.

### Why panels stop clipping mid-row

`.panel` is `overflow: hidden`, so an over-subscribed column used to cut rows
through the middle — letter-tops of "VPN OFF", a usage bar sliced under its own
label. Three pieces prevent that, and they only work together:

1. **Small panels don't shrink.** `body.is-ipad` col-1's non-`.grow` panels are
   `flex: none`. They hold fixed-size reference data (4 service rows, 3 network
   rows, 2 usage bars) where a 9px deficit costs a whole row, so the shortfall
   is pushed onto the capped list below instead.
2. **`fitPanelRows()`** caps each list body's `max-height` at the bottom edge of
   the last row that fits. It is **phased** — clear all caps, measure all
   panels, then apply all cuts — because measure-then-apply one panel at a time
   reads a layout the previous panel's cap already changed.
3. **Deterministic shares in landscape.** `flex: N 1 0` on Calendar / Tasks /
   Linear. With a content-derived basis (`1 1 auto`) a cap shrinks the body,
   which shrinks the basis, which moves the boundary just measured — the cut
   wanders every poll. A 0 basis takes height from the column, so the box holds
   still and one pass lands exactly on a row boundary.

Also in landscape: `grid-template-rows: 1fr max-content`. Auto grid rows split
leftover space evenly, which handed the GitHub/procs row ~218px for ~180px of
content while the list panels above went hungry.

**Two-up rows in landscape.** Services, the GitHub overview counts
(`.gh-overview-stats`) and Top procs are `grid-template-columns: 1fr 1fr` — all
three are short label + value, so folding them halves their height with nothing
hidden. This is the space Linear runs on: Services frees ~44px directly in col-1,
and the other two shrink the bottom row (~38px) which the top row inherits and
col-1's only flexible panel absorbs. Worth ~4 Linear rows at a 700px viewport.
Fold all three or none — leaving Top procs stacked makes it the tallest panel in
the bottom row and it alone keeps that row tall, cancelling GitHub's saving.

All of this is scoped to `min-width: 1025px`. Below that the tablet tier
releases the viewport lock and the page scrolls, so panels should size to their
content and run off the bottom (Split View, Slide Over) rather than be squeezed
into a share of a short column.

iPad landscape is the primary real-world view of this dashboard; when
trading space, it wins.

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
alias block at the bottom — verified empirically across the themes.
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
