# Linear Integration — Design

**Date:** 2026-05-15
**Status:** Approved (brainstorming)
**Scope:** Add Linear as a provider supporting multiple workspaces (two for now, more allowed by config), each rendered as its own dashboard panel.

---

## Goal

Surface my open Linear assignments and current cycle progress for each
configured Linear workspace in the cowork-dash, alongside the existing
Motion Tasks panel. Two workspaces required at launch, but the design
must accept N without daemon or frontend code changes — only config.

## Non-goals

- OAuth flow. Personal API keys per workspace are sufficient for a local-only dashboard.
- Cross-workspace aggregation (no unified "all my Linear work" view).
- Issue mutations (read-only).
- Auto-fetching Linear's workspace name; user-defined labels only.
- Merging Linear into the existing Motion `tasks` panel (the original
  CLAUDE.md vision of one blended task list is explicitly superseded by
  this spec — Motion stays as-is, Linear is separate).

---

## Architecture

```
config.toml [[linear]] blocks
        │
        ▼
lifespan: one asyncio.Task per [[linear]] entry
        │
        ▼  (per workspace)
linear_poll(label, api_key, interval)
        │  GraphQL POST → api.linear.app/graphql
        ▼
STATE["providers"]["linear"][label] = {status, updated_at, issues, cycle, ...}
        │
        ▼
Frontend poll() iterates providers.linear keys, ensures one panel per label
```

One coroutine per workspace means one slow/erroring workspace cannot stall
its siblings. Each writes only to its own slot under
`STATE["providers"]["linear"][<label>]`.

## Config

Repeated `[[linear]]` blocks (TOML array of tables). Each block is a full
workspace definition.

```toml
[[linear]]
label = "WORK"             # short, all-caps, used in panel title
api_key = "lin_api_..."
poll_seconds = 90          # default 90; minimum enforced at 60

[[linear]]
label = "PERSONAL"
api_key = "lin_api_..."
poll_seconds = 90
```

**Validation at startup:**
- Skip blocks missing `label` or `api_key` (log to stderr, do not crash).
- Reject duplicate labels (log error to stderr, keep the first occurrence
  and skip subsequent duplicates) — two pollers writing to the same
  `STATE["providers"]["linear"][label]` slot would race.
- Clamp `poll_seconds` to `max(60, configured)`.

`config.example.toml` will include the example above with both blocks
commented out.

## State shape

`STATE["providers"]["linear"]` is one of two shapes:

**Configured** — dict keyed by workspace label:

```json
{
  "WORK": {
    "status": "ok" | "pending" | "error",
    "updated_at": "2026-05-15T...Z",
    "label": "WORK",
    "viewer": {"name": "Devon"},
    "issues": {
      "open_count": 14,
      "items": [
        {
          "id": "uuid…",
          "identifier": "ENG-412",
          "title": "Fix race condition",
          "team": "ENG",
          "priority": "high",      // high | med | low | none
          "due": "TODAY",          // OVERDUE / TODAY / TMRW / 3D / TUE / JUN 16 / ""
          "state_name": "In Progress",
          "state_type": "started", // backlog | unstarted | started | completed | canceled
          "url": "https://linear.app/..."
        }
      ]
    },
    "cycle": {
      "present": true,
      "number": 27,
      "name": "Cycle 27",
      "ends_in_days": 3,           // negative if past endsAt
      "progress_pct": 62,
      "completed": 18,
      "total": 29,
      "multi_team": false          // true if >1 active cycle in workspace
    }
  },
  "PERSONAL": { ... }
}
```

**Unconfigured** — single sentinel:

```json
{ "status": "unconfigured", "message": "Add [[linear]] blocks to ~/.cowork-dash/config.toml" }
```

The frontend distinguishes shapes by checking for a top-level `status` key.

Seed state: for each label discovered at startup, write
`{"status": "pending"}` so the panel exists from the first render.
Errors during a poll set `status: "error"` and an `error` string but
leave prior `issues` / `cycle` data in place (panel keeps showing last
good data with a red ERR chip).

## Linear API

- **Endpoint:** `POST https://api.linear.app/graphql`
- **Auth:** Header `Authorization: <api_key>` (no `Bearer` prefix for personal keys).
- **One query per poll** (combined viewer + cycles).

The `assignedIssues` filter is intentionally NOT applied server-side
(completed/canceled states included). Reason: ticker events need to
detect transitions *into* completed before the issue would otherwise
disappear from the assigned set. The server-side filter would skip
them entirely. Completed/canceled issues are filtered client-side
after the diff against `prev_states`.

```graphql
query DashboardSnapshot {
  viewer {
    id
    name
    assignedIssues(
      first: 50
      orderBy: updatedAt
    ) {
      nodes {
        id
        identifier
        title
        priority
        dueDate
        url
        state { name type }
        team { key }
        cycle { number id }
      }
    }
  }
  cycles(
    first: 5
    filter: { isActive: { eq: true } }
  ) {
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
```

**Priority mapping** (Linear int → frontend bucket):

| Linear | Meaning | Output bucket |
|--------|---------|---------------|
| 1      | Urgent  | `high`        |
| 2      | High    | `high`        |
| 3      | Medium  | `med`         |
| 4      | Low     | `low`         |
| 0      | None    | `none`        |

**Filter & sort pipeline** (after fetch):

1. Run ticker diff against `prev_states` / `prev_ids` using the *full*
   raw set (so we see "transitioned into completed" before filtering).
2. Update `prev_states` / `prev_ids` from the *full* raw set.
3. Filter out issues with `state.type in ("completed", "canceled")` —
   these don't belong in the visible panel.
4. Sort the remaining list:
   - Priority bucket asc (high → med → low → none)
   - `dueDate` asc (null sorts last)
   - `updatedAt` desc as tiebreaker
5. `open_count` reflects this filtered, pre-truncation count.
6. Truncate to top 8 for `items`.

**Due-string formatting** reuses Motion's existing `_motion_due_string`
shape (extract to a shared `_due_string(iso)` helper).

**Active cycle selection:**
- Filter `cycles.nodes` to those whose `[startsAt, endsAt)` covers now (the API filter is the source of truth, but double-check locally to avoid clock skew).
- If multiple: pick the one with the soonest `endsAt`, set `multi_team: True`.
- If none: `cycle.present = False`.
- `progress_pct` = `round(progress * 100)`.
- `completed`/`total` = last entries of the `*History` arrays.
- `ends_in_days` = `ceil((endsAt - now) / 1day)`.

## Frontend

**HTML:** the center column gets a stable container after the existing
`#panel-tasks`:

```html
<div id="linear-panels"></div>
```

The `renderLinear(state)` function is called from `poll()` with the
`providers.linear` slice. It:

1. If state has top-level `status === "unconfigured"`, render a single
   placeholder panel inside the container with the message.
2. Otherwise, iterate keys (workspace labels). For each label:
   - Ensure `#panel-linear-<label>` exists in `#linear-panels` (create
     and append if not). Use the same panel structure as `#panel-tasks`.
   - Render the body via `renderLinearPanel(label, workspaceState)`.
3. Remove any panels whose label is no longer in state (config edited,
   workspace removed, daemon restarted).

**Body layout** (per workspace), rendered into `#linear-body-<label>`:

```
CYCLE 27 · ENDS 3D     ████████░░░░  62%  18/29

! ENG-412  Fix race condition          TODAY
  ENG-419  Add rate limits             3D
  DES-104  Polish settings page        TUE
  ...
```

- Cycle strip uses existing `fmtBar(percent, width)` helper.
  - `ends_in_days <= 1` → amber; `> 1` → cyan; negative → magenta `PAST DUE`.
  - Hidden entirely when `cycle.present === false`.
  - When `multi_team === true`, append a small `+N MORE` chip in `--dim`.
- Issue rows reuse the existing `.task` row classes (no new CSS):
  - `.pri` cell shows `!` for `high`, blank otherwise.
  - `.src` cell shows the team key (`ENG`).
  - `.title` cell prefixes the identifier in `--dim`: `<span class="dim">ENG-412</span> Fix race condition`.
  - `.due` cell uses the existing color rules (TODAY = warn, `\d+D$` = alert, else dim).
- Meta header: `<open_count> OPEN`, plus the standard "Xs/Xm ago" age chip from `setMeta`.

**Status handling** mirrors the existing convention table:

| status         | render                                                    |
|----------------|-----------------------------------------------------------|
| `pending`      | `loading…`                                                |
| `ok`           | full body                                                 |
| `error`        | red `.err` chip + error string; previous body retained if available |
| `unconfigured` | placeholder panel with config message (only top-level)    |

## Ticker events

Per workspace, label-prefixed:

1. **New assignment** — when an issue id appears that wasn't in the
   previous successful poll's set:
   `linear-WORK · NEW: ENG-412 Fix race condition` (level: info)
2. **State transition** — diffed against `prev_states` (the *raw* full
   set, before client-side filtering removes completed/canceled):
   - Into review: `state_type` becomes `"started"` AND new state name
     (case-insensitive) contains "review" → `linear-WORK · ENG-412 → IN REVIEW`
   - Into done:   `state_type` becomes `"completed"` →
     `linear-WORK · ENG-412 ✓ DONE`
   - Cancellation (`state_type` becomes `"canceled"`) is ignored.
   - Each transition only fires once: it requires the previous state on
     record to differ from the new one.

State tracked in coroutine-local scope (not in `STATE`):

```python
prev_ids: set[str] = set()
prev_states: dict[str, str] = {}   # issue id -> state_name
```

**First-poll suppression:** the first successful poll sets `prev_*` and
emits no ticker events. Otherwise every existing assigned issue would
flood the ticker every time the daemon restarts.

The transition detection only fires while the issue is still in the
assigned set — once it leaves the set (e.g. completed → drops off), the
"DONE" event will have already fired on the prior poll where it
transitioned, then it disappears naturally.

## Error handling

Per CLAUDE.md conventions, scoped to one workspace's slot:

- `httpx.HTTPStatusError` → `status: "error"`, `error: "HTTP <code>: <body[:200]>"`.
- GraphQL `200 OK` with an `errors` array → `status: "error"`,
  `error: "GraphQL: <first error message[:200]>"`.
- Other exception → `status: "error"`, `error: f"{type(e).__name__}: {e}"`.
- Exceptions never escape `while True`.
- `prev_ids` / `prev_states` are NOT reset on error — once the next
  successful poll lands, ticker diffing resumes against the last good
  baseline.

## Poll interval

Default 90s, minimum 60s, configurable per workspace.

Linear's complexity-based rate limit for personal API keys is
1.5k–2.5k complexity points per minute. The combined query above is
well under that budget even at 60s polling across both workspaces.

## File changes

| File                    | Change                                                                        |
|-------------------------|-------------------------------------------------------------------------------|
| `daemon.py`             | Add `linear_poll`, GraphQL constant, helpers; wire `[[linear]]` in lifespan; share `_due_string` with Motion |
| `config.example.toml`   | Add commented `[[linear]]` example with both blocks                          |
| `static/index.html`     | Add `#linear-panels` container after `#panel-tasks`; add `renderLinear` and dynamic panel management; wire into `poll()` |
| `requirements.txt`      | No change (httpx already present)                                            |
| `CLAUDE.md`             | Update the "Current provider state" table and the "Planned" line to reflect Linear shipped |

## Open questions

None. Ready to plan implementation.
