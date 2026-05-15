# Linear Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Linear as a provider with N-workspace support, each workspace rendered as its own panel in the center column, alongside the existing Motion Tasks panel.

**Architecture:** One `linear_poll` async coroutine per `[[linear]]` config block. Each writes to its own slot under `STATE["providers"]["linear"][<label>]`. Combined GraphQL query fetches viewer's assigned issues + active cycles in one request. Frontend dynamically creates one panel per discovered workspace label.

**Tech Stack:** Python 3.11+ asyncio + httpx + FastAPI (existing daemon), Linear GraphQL API, vanilla HTML/JS (no build step).

**Spec:** `docs/superpowers/specs/2026-05-15-linear-integration-design.md`

**Verification model:** This codebase has no automated tests by design. Every existing provider (motion, github, calendar, claude, services, system) is verified by running the daemon and inspecting `/api/state.json` and the rendered panel in a browser. This plan follows the same pattern. Each task ends with a concrete `curl` or browser check.

**Pre-flight (do once before starting Task 1):**
- Confirm you have at least one Linear personal API key. Linear → Settings → API → Create Personal API Key. If you only have one workspace available right now, that's fine — the plan is structured so you can validate single-workspace behavior first, then add the second key when you have it.
- Confirm the daemon currently runs: `./run.sh`, then `curl -s http://localhost:7766/api/state.json | python3 -m json.tool | head -30` should return JSON with `providers.github`, `providers.tasks`, etc.

---

## File Structure

| File                                   | Change kind | Responsibility                                                              |
|----------------------------------------|-------------|------------------------------------------------------------------------------|
| `daemon.py`                            | Modify      | New `linear_poll` coroutine + helpers; lifespan wiring; shared `_due_string` |
| `config.example.toml`                  | Modify      | Add commented `[[linear]]` example blocks                                    |
| `static/index.html`                    | Modify      | New `#linear-panels` container, `renderLinear` JS, panel render function     |
| `CLAUDE.md`                            | Modify      | Update provider state table; remove Linear from "planned"                    |

No new files. The existing convention is one daemon file + one HTML file; we follow it.

---

## Task 1: Extract shared `_due_string` helper

Motion has `_motion_due_string` that we'll reuse for Linear due dates. Lift it to a generic `_due_string` and update Motion to call it. This is a refactor with no behavior change.

**Files:**
- Modify: `/Users/devon/git/dash/daemon.py:396-415` (the existing `_motion_due_string` function and its call site at line 431)

- [ ] **Step 1: Add the new shared helper above `_motion_due_string`**

Insert this immediately above `_motion_due_string` (which currently lives around line 396):

```python
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
```

- [ ] **Step 2: Replace the body of `_motion_due_string` with a delegation**

Find the existing `_motion_due_string` (the function whose docstring is `"""Compact due display: OVERDUE / TODAY / TMRW / 3D / TUE / JUN 16."""`). Replace the entire function with:

```python
def _motion_due_string(due_iso: str | None) -> str:
    """Compat shim — delegates to the shared helper. Kept so existing
    callers don't need to change in the same diff."""
    return _due_string(due_iso)
```

- [ ] **Step 3: Verify Motion still renders due strings correctly**

If Motion is configured: restart the daemon, then:

```bash
curl -s http://localhost:7766/api/state.json | python3 -c "
import json, sys
d = json.load(sys.stdin)
for it in (d['providers']['tasks'].get('items') or []):
    print(it.get('due'), '|', it.get('title'))
"
```

Expected: same `due` strings as before the refactor (TODAY, 3D, TUE, etc.). If Motion is not configured, skip this step — the next task exercises `_due_string` directly.

- [ ] **Step 4: Quick sanity check on `_due_string` itself**

```bash
cd /Users/devon/git/dash && python3 -c "
from daemon import _due_string
from datetime import datetime, timedelta, timezone
now = datetime.now(timezone.utc)
print('none:    ', repr(_due_string(None)))
print('overdue: ', _due_string((now - timedelta(days=2)).isoformat()))
print('today:   ', _due_string(now.isoformat()))
print('tmrw:    ', _due_string((now + timedelta(days=1)).isoformat()))
print('3d:      ', _due_string((now + timedelta(days=3)).isoformat()))
print('week:    ', _due_string((now + timedelta(days=8)).isoformat()))
print('far:     ', _due_string((now + timedelta(days=30)).isoformat()))
"
```

Expected output (the day-of-week and date will vary):
```
none:     ''
overdue:  OVERDUE
today:    TODAY
tmrw:     TMRW
3d:       3D
week:     <DAY-OF-WEEK ABBREV>
far:      <MONTH DAY>
```

- [ ] **Step 5: Commit**

```bash
git add daemon.py
git commit -m "refactor: extract shared _due_string helper for cross-provider use"
```

---

## Task 2: Add Linear constants and pure helpers

Drop in the Linear API endpoint, GraphQL query, priority mapping, and pure parser helpers. No coroutine yet — just the building blocks. Keep them in a clearly delimited section of `daemon.py` matching the existing convention (each provider has its own `# ---` banner).

**Files:**
- Modify: `/Users/devon/git/dash/daemon.py` (insert a new `# Linear provider` section between Motion and Claude sections — find the Claude section header `# Claude usage + CC sessions (real ...)` and insert above it)

- [ ] **Step 1: Add the Linear section banner and constants**

Insert immediately above the Claude section banner (the one that starts `# --------------------------------------------------------------------------- #` followed by `# Claude usage + CC sessions ...`):

```python
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
```

- [ ] **Step 2: Add the issue-shaping helper**

Immediately below the constants in the same Linear section:

```python
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
```

- [ ] **Step 3: Add the cycle-shaping helper**

Below `_linear_sort_key`:

```python
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
```

- [ ] **Step 4: Verify the helpers parse a synthetic payload correctly**

```bash
cd /Users/devon/git/dash && python3 -c "
from daemon import _linear_to_item, _linear_sort_key, _linear_active_cycle
from datetime import datetime, timezone, timedelta

issue = {
    'id': 'abc', 'identifier': 'ENG-412', 'title': '  Fix race  ',
    'priority': 1, 'dueDate': None, 'updatedAt': '2026-05-15T10:00:00Z',
    'url': 'https://linear.app/x', 'state': {'name': 'In Progress', 'type': 'started'},
    'team': {'key': 'ENG'}, 'cycle': None,
}
it = _linear_to_item(issue)
assert it['priority'] == 'high', it
assert it['identifier'] == 'ENG-412'
assert it['title'] == 'Fix race'
assert it['team'] == 'ENG'
print('item OK')

now = datetime.now(timezone.utc)
cycle_nodes = [{
    'id': 'c1', 'number': 27, 'name': 'Cycle 27',
    'startsAt': (now - timedelta(days=2)).isoformat().replace('+00:00','Z'),
    'endsAt':   (now + timedelta(days=3)).isoformat().replace('+00:00','Z'),
    'progress': 0.62,
    'issueCountHistory': [10, 20, 29],
    'completedIssueCountHistory': [3, 10, 18],
}]
c = _linear_active_cycle(cycle_nodes)
assert c['present'] is True, c
assert c['number'] == 27
assert c['progress_pct'] == 62
assert c['completed'] == 18 and c['total'] == 29
assert c['ends_in_days'] in (3, 4), c   # depending on time-of-day rounding
assert c['multi_team'] is False
print('cycle OK')

empty = _linear_active_cycle([])
assert empty == {'present': False}
print('no-cycle OK')
"
```

Expected output:
```
item OK
cycle OK
no-cycle OK
```

- [ ] **Step 5: Commit**

```bash
git add daemon.py
git commit -m "linear: add api endpoint, graphql query, and parsing helpers"
```

---

## Task 3: Implement `linear_poll` coroutine (single workspace, no ticker yet)

Build the polling loop. Defer ticker diffing to Task 4 — get the basic state-write working first.

**Files:**
- Modify: `/Users/devon/git/dash/daemon.py` (add `linear_poll` immediately below the helpers from Task 2, in the same Linear section)

- [ ] **Step 1: Add `linear_poll`**

Append to the Linear section, below `_linear_active_cycle`:

```python
async def linear_poll(label: str, api_key: str, interval: int) -> None:
    """Poll one Linear workspace via GraphQL. Writes to
    STATE["providers"]["linear"][label]. Ticker diffing is added separately."""
    headers = {
        "Authorization": api_key,            # personal keys: NO "Bearer " prefix
        "Content-Type": "application/json",
        "User-Agent": "cowork-dash/0.1",
    }
    while True:
        try:
            async with httpx.AsyncClient(timeout=20, headers=headers) as c:
                r = await c.post(LINEAR_API, json={"query": LINEAR_GQL})
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

            # Shape ALL issues (including completed/canceled) — needed for
            # ticker diffs in Task 4. Then filter for the visible panel.
            shaped_all = [_linear_to_item(n) for n in raw_nodes]
            visible = [
                it for it in shaped_all
                if it["state_type"] not in ("completed", "canceled")
            ]
            visible.sort(key=_linear_sort_key)
            open_count = len(visible)
            items = []
            for it in visible[:_LINEAR_MAX_ITEMS]:
                # Strip the internal sort fields before serializing.
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
```

- [ ] **Step 2: Seed the Linear state slot at module load**

Find the `STATE` dict definition near the top of `daemon.py` (around line 48). Add a `"linear": {}` entry inside `"providers"`. The block should look like:

```python
STATE: dict[str, Any] = {
    "started_at": datetime.now(timezone.utc).isoformat(),
    "providers": {
        "github": {"status": "pending"},
        "calendar": {"status": "pending"},
        "tasks": {"status": "pending"},
        "claude": {"status": "pending"},
        "services": {"status": "pending"},
        "system": {"status": "pending"},
        "linear": {},   # populated per-workspace at startup; sentinel set if unconfigured
    },
    "ticker": [],
}
```

- [ ] **Step 3: Wire ONE workspace in lifespan (temporary — Task 5 generalizes this)**

Find the `lifespan` function. Immediately after the Motion wiring (the block ending with the `_push_ticker("daemon", "motion provider online", "info")` line), add a temporary single-workspace block:

```python
    # Temporary single-workspace wiring — Task 5 replaces this with
    # multi-workspace iteration. Reads first [[linear]] block only.
    lin_blocks = cfg.get("linear") or []
    if lin_blocks and isinstance(lin_blocks, list):
        first = lin_blocks[0] or {}
        if first.get("label") and first.get("api_key"):
            label = first["label"]
            STATE["providers"]["linear"][label] = {"status": "pending"}
            BACKGROUND_TASKS.append(asyncio.create_task(linear_poll(
                label, first["api_key"],
                max(_LINEAR_MIN_INTERVAL, first.get("poll_seconds", _LINEAR_DEFAULT_INTERVAL)),
            )))
            _push_ticker("daemon", f"linear provider online ({label})", "info")
    if not STATE["providers"]["linear"]:
        STATE["providers"]["linear"] = {
            "status": "unconfigured",
            "message": "Add [[linear]] blocks to ~/.cowork-dash/config.toml",
        }
```

- [ ] **Step 4: Add at least one `[[linear]]` block to your local config**

Edit `~/.cowork-dash/config.toml` and add (replace `lin_api_...` with your real key):

```toml
[[linear]]
label = "WORK"
api_key = "lin_api_..."
poll_seconds = 90
```

- [ ] **Step 5: Restart daemon and verify the workspace polls successfully**

Restart `./run.sh` (Ctrl-C and re-run). Wait ~5 seconds, then:

```bash
curl -s http://localhost:7766/api/state.json | python3 -c "
import json, sys
d = json.load(sys.stdin)
lin = d['providers']['linear']
print('shape:', list(lin.keys()))
for label, ws in lin.items():
    if label == 'status': continue
    print(f'--- {label} ---')
    print('  status:', ws.get('status'))
    print('  open_count:', (ws.get('issues') or {}).get('open_count'))
    print('  items:', len((ws.get('issues') or {}).get('items') or []))
    print('  cycle:', ws.get('cycle'))
    if ws.get('error'): print('  error:', ws['error'])
"
```

Expected:
- `status: ok`
- `open_count` is a non-negative integer
- `items` is a list of up to 8
- `cycle.present` is `True` or `False`
- No `error` field

If you see `status: error`, the `error` string tells you what's wrong (HTTP 401 = bad key; HTTP 400 with GraphQL message = malformed query).

- [ ] **Step 6: Commit**

```bash
git add daemon.py
git commit -m "linear: implement polling coroutine with single-workspace lifespan wiring"
```

---

## Task 4: Add ticker diff logic to `linear_poll`

Detect new assignments and state transitions, emit ticker events. Skip diffing on the first successful poll so daemon restart doesn't flood the ticker.

**Files:**
- Modify: `/Users/devon/git/dash/daemon.py` (the `linear_poll` function added in Task 3)

- [ ] **Step 1: Add coroutine-local diff state at the top of `linear_poll`**

Inside `linear_poll`, immediately after the `headers = {...}` dict and BEFORE the `while True:` line, add:

```python
    prev_ids: set[str] = set()
    prev_states: dict[str, str] = {}   # id -> state_name
    first_poll = True
```

- [ ] **Step 2: Add the diff block inside the try, after `shaped_all` is built and BEFORE the `visible = [...]` filter line**

Insert this block immediately after `shaped_all = [_linear_to_item(n) for n in raw_nodes]`:

```python
            # --- ticker diff (against the FULL raw set, before filtering) ---
            cur_ids = {it["id"] for it in shaped_all if it["id"]}
            cur_states = {it["id"]: it["state_name"] for it in shaped_all if it["id"]}

            if not first_poll:
                # New assignments — id present now, absent before.
                for it in shaped_all:
                    if it["id"] and it["id"] not in prev_ids:
                        text = f"{label} · NEW: {it['identifier']} {it['title']}"[:140]
                        _push_ticker(f"linear-{label}", text, "info")
                # State transitions for ids we already knew about.
                for it in shaped_all:
                    iid = it["id"]
                    if not iid or iid not in prev_ids:
                        continue
                    new_name = it["state_name"]
                    old_name = prev_states.get(iid, "")
                    if new_name == old_name:
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
            # --- end ticker diff ---
```

- [ ] **Step 3: Restart daemon and verify no ticker spam on first poll**

Restart `./run.sh`. Wait ~5 seconds:

```bash
curl -s http://localhost:7766/api/state.json | python3 -c "
import json, sys
d = json.load(sys.stdin)
linear_events = [t for t in d['ticker'] if t.get('source','').startswith('linear-')]
print('linear ticker events on first poll:', len(linear_events))
for t in linear_events[:5]: print(' ', t)
"
```

Expected: `linear ticker events on first poll: 0` (the daemon-online event is from `_push_ticker("daemon", ...)` and won't match this filter).

- [ ] **Step 4: Verify the second poll still emits zero events when nothing changed**

Wait one full poll interval (default 90s) past the first successful poll, then re-run the command from Step 3. Expected: still `0`. The diff loop runs but nothing changed, so nothing is pushed.

- [ ] **Step 5: Optional manual transition check**

If you have a Linear issue you can move: assign yourself a new issue, or move one of your existing assigned issues to "In Review" or close it. Wait one poll interval, then:

```bash
curl -s http://localhost:7766/api/state.json | python3 -c "
import json, sys
d = json.load(sys.stdin)
for t in [x for x in d['ticker'] if x.get('source','').startswith('linear-')][:5]:
    print(t.get('text'))
"
```

Expected: one line matching what you did, e.g. `WORK · NEW: ENG-X New issue title` or `WORK · ENG-X → IN REVIEW` or `WORK · ENG-X ✓ DONE`. Skip this step if you don't want to manipulate real issues — the next task continues regardless.

- [ ] **Step 6: Commit**

```bash
git add daemon.py
git commit -m "linear: ticker events for new assignments and state transitions"
```

---

## Task 5: Multi-workspace lifespan wiring with validation

Replace the temporary single-workspace wiring from Task 3 with the real multi-workspace iteration including duplicate-label rejection and missing-field skipping.

**Files:**
- Modify: `/Users/devon/git/dash/daemon.py` (the temporary block in `lifespan` from Task 3 step 3)

- [ ] **Step 1: Replace the temporary block with the real wiring**

Find the temporary block from Task 3 (begins with the comment `# Temporary single-workspace wiring`) and replace the ENTIRE block (down to and including the `if not STATE["providers"]["linear"]: ...` sentinel) with:

```python
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
        BACKGROUND_TASKS.append(asyncio.create_task(linear_poll(label, api_key, interval)))
        _push_ticker("daemon", f"linear provider online ({label})", "info")
        started_any = True

    if not started_any:
        STATE["providers"]["linear"] = {
            "status": "unconfigured",
            "message": "Add [[linear]] blocks to ~/.cowork-dash/config.toml",
        }
```

- [ ] **Step 2: Verify single-workspace config still behaves correctly**

Restart `./run.sh` with your existing one-workspace config:

```bash
curl -s http://localhost:7766/api/state.json | python3 -c "
import json, sys
d = json.load(sys.stdin)
lin = d['providers']['linear']
print(json.dumps({k: (v.get('status') if isinstance(v, dict) else v) for k, v in lin.items()}, indent=2))
"
```

Expected: `{"WORK": "ok"}` (or whatever your label is).

- [ ] **Step 3: Add a second workspace block to config and restart**

If you have a second Linear API key, add a second `[[linear]]` block to `~/.cowork-dash/config.toml`. If you don't have a second key yet, you can still validate multi-workspace handling by adding a deliberately-bad second block with a real label but a fake key — the daemon should start both pollers, the bad one will land in `status: error`, and the good one stays in `status: ok`.

Example (second block uses a fake key for validation purposes):

```toml
[[linear]]
label = "WORK"
api_key = "lin_api_REAL_KEY_HERE"
poll_seconds = 90

[[linear]]
label = "PERSONAL"
api_key = "lin_api_FAKE_KEY_FOR_TESTING"
poll_seconds = 90
```

Restart `./run.sh` and wait ~5 seconds:

```bash
curl -s http://localhost:7766/api/state.json | python3 -c "
import json, sys
d = json.load(sys.stdin)
for label, ws in d['providers']['linear'].items():
    if not isinstance(ws, dict): continue
    print(f'{label}: {ws.get(\"status\")} {ws.get(\"error\",\"\")[:80]}')"
```

Expected: two lines, one `ok`, one `error: HTTP 401: ...` (if you used a fake key) — proving the second workspace doesn't poison the first.

Replace the fake key with the real second key once you have it, restart, expect both `ok`.

- [ ] **Step 4: Verify duplicate-label rejection**

Temporarily add a third block with a duplicate label:

```toml
[[linear]]
label = "WORK"   # duplicate of first block
api_key = "anything"
```

Restart. The daemon should print `[cowork-dash] [[linear]] duplicate label 'WORK' — keeping the first, skipping later` to stderr (visible in your terminal where `./run.sh` is running). State should still show only two workspaces. Remove the duplicate block before continuing.

- [ ] **Step 5: Verify unconfigured sentinel**

Comment out ALL `[[linear]]` blocks. Restart:

```bash
curl -s http://localhost:7766/api/state.json | python3 -c "
import json, sys
print(json.load(sys.stdin)['providers']['linear'])
"
```

Expected: `{'status': 'unconfigured', 'message': 'Add [[linear]] blocks to ~/.cowork-dash/config.toml'}`. Restore your blocks and restart before continuing.

- [ ] **Step 6: Commit**

```bash
git add daemon.py
git commit -m "linear: multi-workspace lifespan wiring with label validation"
```

---

## Task 6: Update `config.example.toml`

Document the new config shape so users copying the example know how to configure Linear.

**Files:**
- Modify: `/Users/devon/git/dash/config.example.toml`

- [ ] **Step 1: Replace the existing commented Linear stub**

Find these lines in `config.example.toml`:

```toml
# [linear]
# api_key = "lin_api_..."
# poll_seconds = 60
```

Replace them with:

```toml
# Linear — repeat one [[linear]] block per workspace. You can have N workspaces;
# each gets its own panel labeled with the `label` value. Get a personal API key
# at: Linear → Settings → API → Create Personal API Key
#
# [[linear]]
# label = "WORK"             # short, all-caps; shown as "LINEAR · WORK" panel title
# api_key = "lin_api_..."
# poll_seconds = 90          # default 90; minimum enforced at 60
#
# [[linear]]
# label = "PERSONAL"
# api_key = "lin_api_..."
# poll_seconds = 90
```

- [ ] **Step 2: Verify the example file still parses as TOML**

```bash
cd /Users/devon/git/dash && python3 -c "
try:
    import tomllib
except ImportError:
    import tomli as tomllib
with open('config.example.toml', 'rb') as f:
    cfg = tomllib.load(f)
print('parsed keys:', list(cfg.keys()))
print('linear (commented out, so should be missing):', 'linear' in cfg)
"
```

Expected: `parsed keys: [...]` (a list) and `linear (commented out, so should be missing): False`.

- [ ] **Step 3: Commit**

```bash
git add config.example.toml
git commit -m "linear: document [[linear]] config shape in example"
```

---

## Task 7: Add `#linear-panels` container to HTML

Static container in the center column. The frontend JS will populate it with one panel per workspace.

**Files:**
- Modify: `/Users/devon/git/dash/static/index.html` (around line 811, immediately after the closing `</div>` of `#panel-tasks`)

- [ ] **Step 1: Add the container**

Find this block in `static/index.html` (around line 805-811):

```html
      <div class="panel grow" id="panel-tasks">
        <div class="panel-title">
          <span class="ttl">TASKS · ACTIVE</span>
          <span class="meta" id="tasks-meta">—</span>
        </div>
        <div id="tasks-body" class="dim">Loading…</div>
      </div>
```

Immediately AFTER the closing `</div>` of that panel (still inside the same parent column `<div class="col">`), add:

```html
      <!-- Linear workspaces — one panel per [[linear]] block, populated by renderLinear() -->
      <div id="linear-panels"></div>
```

- [ ] **Step 2: Verify the container appears in the rendered page**

Reload `http://localhost:7766` in the browser. Open devtools → Elements. Search for `linear-panels`. It should be present, empty, immediately after `#panel-tasks` in the center column. No visual change yet because no JS populates it.

- [ ] **Step 3: Commit**

```bash
git add static/index.html
git commit -m "linear: add static #linear-panels container in center column"
```

---

## Task 8: Add `renderLinear` dispatcher and panel lifecycle JS

The function that:
1. Handles the `unconfigured` sentinel
2. Creates / removes panel DOM nodes per discovered workspace label
3. Delegates per-workspace body rendering to `renderLinearPanel` (added in Task 9)

Panel creation uses `createElement` + `textContent` + `appendChild` (no string-to-DOM conversion). Body rendering routes through the existing `setHtml(id, html)` helper, which already wraps the actual DOM write — same pattern as every other panel renderer in this file.

**Files:**
- Modify: `/Users/devon/git/dash/static/index.html` (insert in the `<script>` block, immediately AFTER the existing `renderTasks` function, around line 1015)

- [ ] **Step 1: Add the panel-creation helper**

Find `renderTasks` function (the one ending around line 1015 with `setHtml('tasks-body', rows);` then `}`). Immediately AFTER it, insert:

```javascript
function _safeId(s) {
  // Labels become element ids — restrict to safe chars only.
  return String(s).replace(/[^A-Za-z0-9_-]/g, '_');
}

function _ensureLinearPanel(label) {
  const safe = _safeId(label);
  const panelId = 'panel-linear-' + safe;
  if (el(panelId)) return safe;

  const panel = document.createElement('div');
  panel.className = 'panel grow';
  panel.id = panelId;

  const title = document.createElement('div');
  title.className = 'panel-title';
  const ttl = document.createElement('span');
  ttl.className = 'ttl';
  ttl.textContent = 'LINEAR · ' + label;
  const meta = document.createElement('span');
  meta.className = 'meta';
  meta.id = 'linear-meta-' + safe;
  meta.textContent = '—';
  title.appendChild(ttl);
  title.appendChild(meta);

  const body = document.createElement('div');
  body.id = 'linear-body-' + safe;
  body.className = 'dim';
  body.textContent = 'Loading…';

  panel.appendChild(title);
  panel.appendChild(body);
  el('linear-panels').appendChild(panel);
  return safe;
}

function _ensureUnconfiguredPanel(message) {
  const container = el('linear-panels');
  // Remove any real workspace panels first.
  Array.from(container.children).forEach(child => {
    if (child.id && child.id.startsWith('panel-linear-') && child.id !== 'panel-linear-unconfigured') {
      child.remove();
    }
  });
  let panel = el('panel-linear-unconfigured');
  if (!panel) {
    panel = document.createElement('div');
    panel.className = 'panel grow';
    panel.id = 'panel-linear-unconfigured';
    const title = document.createElement('div');
    title.className = 'panel-title';
    const ttl = document.createElement('span');
    ttl.className = 'ttl';
    ttl.textContent = 'LINEAR';
    const meta = document.createElement('span');
    meta.className = 'meta';
    meta.textContent = 'OFFLINE';
    title.appendChild(ttl);
    title.appendChild(meta);
    const body = document.createElement('div');
    body.id = 'linear-unconfigured-body';
    body.className = 'dim';
    panel.appendChild(title);
    panel.appendChild(body);
    container.appendChild(panel);
  }
  el('linear-unconfigured-body').textContent = message || 'unconfigured';
}
```

- [ ] **Step 2: Add the dispatcher**

Immediately AFTER the helpers from Step 1:

```javascript
function renderLinear(state) {
  const container = el('linear-panels');
  if (!container) return;

  // Unconfigured sentinel — wipe real panels and show one placeholder.
  if (state && state.status === 'unconfigured') {
    _ensureUnconfiguredPanel(state.message || '');
    return;
  }

  if (!state || typeof state !== 'object') {
    Array.from(container.children).forEach(c => c.remove());
    return;
  }

  // Real shape: { LABEL: workspaceState, ... }
  const labels = Object.keys(state).filter(k => k !== 'status' && k !== 'message');
  const wantedIds = new Set(labels.map(l => 'panel-linear-' + _safeId(l)));

  // Remove the unconfigured placeholder if present (we now have real workspaces).
  const placeholder = el('panel-linear-unconfigured');
  if (placeholder) placeholder.remove();

  // Remove panels for labels no longer present.
  Array.from(container.children).forEach(child => {
    if (child.id && child.id.startsWith('panel-linear-') && !wantedIds.has(child.id)) {
      child.remove();
    }
  });

  // Ensure a panel exists for each label, then render its body.
  labels.forEach(label => {
    const safe = _ensureLinearPanel(label);
    renderLinearPanel(label, safe, state[label]);
  });
}
```

- [ ] **Step 3: Add a stub for `renderLinearPanel` (Task 9 fills it in)**

Immediately AFTER `renderLinear`:

```javascript
function renderLinearPanel(label, safe, p) {
  // Stub — real body rendering lives in Task 9.
  const metaId = 'linear-meta-' + safe;
  const bodyId = 'linear-body-' + safe;
  if (!el(bodyId)) return;
  setMeta(metaId, p);
  if (!p || p.status === 'pending') {
    setHtml(bodyId, '<div class="dim">loading…</div>');
    return;
  }
  setHtml(bodyId, '<div class="dim">stub — workspace=' + escapeHtml(label) + ' status=' + escapeHtml(p.status||'?') + '</div>');
}
```

- [ ] **Step 4: Wire `renderLinear` into the `poll()` function**

Find `poll()` (around line 1230). It calls `renderTasks(p.tasks);` — immediately after that line, add:

```javascript
  renderLinear(p.linear);
```

- [ ] **Step 5: Verify panels appear**

Reload `http://localhost:7766`. You should see one or two new panels titled `LINEAR · WORK` (and `LINEAR · PERSONAL` if configured) below the Motion Tasks panel. Their bodies show the stub line: `stub — workspace=WORK status=ok`.

If you commented out all `[[linear]]` blocks for testing: a single `LINEAR / OFFLINE` panel shows the unconfigured message.

- [ ] **Step 6: Verify panels removed when label disappears**

In `~/.cowork-dash/config.toml`, comment out the second `[[linear]]` block. Restart `./run.sh`. Reload the browser. The PERSONAL panel should disappear from the DOM (verify in devtools → Elements). Restore the block and restart before continuing.

- [ ] **Step 7: Commit**

```bash
git add static/index.html
git commit -m "linear: dynamic panel lifecycle and dispatcher"
```

---

## Task 9: Implement `renderLinearPanel` body (cycle strip + issue rows)

Replace the stub from Task 8 with the real body rendering. Uses the existing `setHtml` helper to inject escaped HTML strings — identical pattern to `renderTasks`, `renderCalendar`, and every other panel renderer in this file.

**Files:**
- Modify: `/Users/devon/git/dash/static/index.html` (the `renderLinearPanel` stub from Task 8)

- [ ] **Step 1: Replace the `renderLinearPanel` stub**

Find the `renderLinearPanel` function added in Task 8. Replace its entire body with:

```javascript
function renderLinearPanel(label, safe, p) {
  const metaId = 'linear-meta-' + safe;
  const bodyId = 'linear-body-' + safe;
  const meta = el(metaId);
  const body = el(bodyId);
  if (!meta || !body) return;

  setMeta(metaId, p);

  if (!p || p.status === 'pending') {
    setHtml(bodyId, '<div class="dim">loading…</div>');
    return;
  }
  if (p.status === 'error') {
    // Show error chip in meta. Keep prior body if we have one.
    setHtml(metaId, '<span class="err">ERR</span> <span class="dim">' + escapeHtml((p.error||'').slice(0,80)) + '</span>');
    if (body.textContent.trim() === '' || body.textContent.toLowerCase().includes('loading')) {
      setHtml(bodyId, '<div class="err">' + escapeHtml(p.error || 'error') + '</div>');
    }
    return;
  }

  const issues = p.issues || { open_count: 0, items: [] };
  const cycle = p.cycle || { present: false };

  meta.textContent = (issues.open_count ?? 0) + ' OPEN';

  let html = '';

  if (cycle.present) {
    const days = cycle.ends_in_days;
    let endsTxt, endsCls;
    if (days < 0)       { endsTxt = 'PAST DUE'; endsCls = 'alert'; }
    else if (days <= 1) { endsTxt = 'ENDS ' + (days === 0 ? 'TODAY' : 'TMRW'); endsCls = 'warn'; }
    else                { endsTxt = 'ENDS ' + days + 'D'; endsCls = ''; }
    const bar = fmtBar(cycle.progress_pct || 0, 12);
    const multi = cycle.multi_team ? ' <span class="dim">+MORE</span>' : '';
    html += '<div class="cycle-strip">'
      + '<span class="dim">CYCLE ' + escapeHtml(String(cycle.number ?? '')) + '</span>'
      + '<span class="' + endsCls + '">' + escapeHtml(endsTxt) + '</span>'
      + '<span class="bar">' + escapeHtml(bar) + '</span>'
      + '<span class="dim">' + (cycle.progress_pct || 0) + '%</span>'
      + '<span class="dim">' + (cycle.completed || 0) + '/' + (cycle.total || 0) + '</span>'
      + multi
      + '</div>';
  }

  const items = issues.items || [];
  if (items.length === 0) {
    html += '<div class="dim">no open issues</div>';
  } else {
    html += items.map(it => {
      const pri = it.priority === 'high' ? '!' : ' ';
      const dueCls = it.due === 'TODAY' ? 'warn'
                    : (it.due === 'OVERDUE' ? 'alert'
                    : (/\d+D$/.test(it.due || '') ? 'alert' : 'dim'));
      const ident = it.identifier ? '<span class="dim">' + escapeHtml(it.identifier) + '</span> ' : '';
      return '<div class="task">'
        + '<span class="pri">' + pri + '</span>'
        + '<span class="src">' + escapeHtml(it.team || '') + '</span>'
        + '<span class="title">' + ident + escapeHtml(it.title || '') + '</span>'
        + '<span class="due ' + dueCls + '">' + escapeHtml(it.due || '') + '</span>'
        + '</div>';
    }).join('');
  }

  setHtml(bodyId, html);
}
```

- [ ] **Step 2: Add minimal CSS for the cycle strip**

Find the `<style>` block in `static/index.html`. Search for the `.task` class (it should already exist, used by Motion). Immediately AFTER the existing `.task` rules, add:

```css
.cycle-strip {
  display: flex;
  gap: 8px;
  align-items: baseline;
  font-family: var(--mono);
  font-size: 12px;
  padding: 4px 0 8px;
  border-bottom: 1px solid var(--rule);
  margin-bottom: 6px;
}
.cycle-strip .bar { color: var(--cyan); letter-spacing: -1px; }
.cycle-strip .warn { color: var(--amber); }
.cycle-strip .alert { color: var(--magenta); }
```

- [ ] **Step 3: Reload and verify**

Reload `http://localhost:7766`. Each Linear panel should now show:
- A `CYCLE N · ENDS XD` strip with a progress bar (only if there's an active cycle)
- A list of issue rows: `! ENG-412 Fix race condition  TODAY`
- The meta header shows `<N> OPEN` plus the standard age chip (e.g. `47s ago`)

If a workspace has no active cycle: no cycle strip, just the issue rows.
If a workspace has no open issues: shows `no open issues`.

- [ ] **Step 4: Verify error rendering by breaking a key**

Edit `~/.cowork-dash/config.toml`, change the second workspace's `api_key` to something invalid. Restart `./run.sh`. Wait ~5s, reload the browser. The PERSONAL panel meta should show a red `ERR` chip with the truncated error string. Restore the real key and restart before continuing.

- [ ] **Step 5: Verify pending → ok transition**

Restart `./run.sh`. Within the first ~2 seconds (before the first poll completes), reload the browser. Each Linear panel should show `loading…`. Within ~5s the bodies should populate.

- [ ] **Step 6: Commit**

```bash
git add static/index.html
git commit -m "linear: cycle strip and issue row rendering"
```

---

## Task 10: Update CLAUDE.md

Reflect that Linear has shipped and update the provider state table.

**Files:**
- Modify: `/Users/devon/git/dash/CLAUDE.md`

- [ ] **Step 1: Add Linear to the "Current provider state" table**

Find the table in CLAUDE.md under `## Current provider state` (it has columns `panel | source`). Add a row for `linear` immediately after the `tasks` row:

```markdown
| linear        | api.linear.app GraphQL — one [[linear]] block per workspace, separate panel each |
```

- [ ] **Step 2: Update the "Planned but not yet shipped" line**

Find the line beginning `Planned but not yet shipped:`. Currently it says:

```
Planned but not yet shipped: linear (would merge with motion in tasks),
gmail.
```

Replace with:

```
Planned but not yet shipped: gmail.
```

- [ ] **Step 3: Add Linear to the poll-interval table**

Find the table under `### Poll intervals`. Add a row immediately after the `motion` row:

```markdown
| linear   | 90s      | per-workspace; complexity-based rate limit, well under budget |
```

- [ ] **Step 4: Verify the file still reads cleanly**

```bash
head -200 /Users/devon/git/dash/CLAUDE.md | grep -A2 -E "(linear|Planned)"
```

Expected: the new lines appear in context.

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: reflect linear provider shipped in CLAUDE.md"
```

---

## Final verification

- [ ] **Step 1: End-to-end smoke test**

Restart the daemon. With both real Linear workspaces configured:

```bash
curl -s http://localhost:7766/api/state.json | python3 -c "
import json, sys
d = json.load(sys.stdin)
lin = d['providers']['linear']
assert isinstance(lin, dict) and 'status' not in lin, lin
print('workspaces:', list(lin.keys()))
for label, ws in lin.items():
    assert ws.get('status') == 'ok', f'{label}: {ws.get(\"status\")} {ws.get(\"error\")}'
    issues = ws.get('issues') or {}
    cycle = ws.get('cycle') or {}
    print(f'  {label}: {issues.get(\"open_count\")} open, cycle.present={cycle.get(\"present\")}')
print('OK')
"
```

Expected: `OK` and a line per workspace showing open count.

- [ ] **Step 2: Visual check**

Open `http://localhost:7766` in a browser. Confirm:
- Both Linear panels visible in the center column, below Motion Tasks.
- Each shows the correct workspace label in the title (`LINEAR · WORK`, `LINEAR · PERSONAL`).
- Issue rows render with priority, team key, identifier, title, due chip.
- Cycle strip renders if the workspace has an active cycle.
- Meta shows `<N> OPEN · Xs ago`.
- No console errors in devtools.

- [ ] **Step 3: Confirm no regressions**

Verify all other panels still render: Calendar, Motion Tasks, GitHub, Heatmap, Services, Claude Usage, CC Sessions, system stats. None should show error states they didn't have before this work.

- [ ] **Step 4: Final commit if any cleanup needed**

If the previous tasks left any stray whitespace or doc inconsistencies you spotted during verification, fix and commit:

```bash
git diff
git add -p
git commit -m "linear: post-verification cleanup"
```

If `git diff` is empty, you're done.

---

## Done criteria

- [ ] Two `[[linear]]` workspaces configured and both polling successfully (`status: "ok"`)
- [ ] Each workspace renders as its own panel in the center column with correct label
- [ ] Cycle strip renders when an active cycle exists; hidden otherwise
- [ ] Issue rows show priority indicator, team key, identifier, title, due chip
- [ ] Meta header shows open count and standard age chip
- [ ] Adding/removing/breaking one workspace's config has no effect on the other
- [ ] No ticker spam on daemon restart (first-poll suppression works)
- [ ] CLAUDE.md and `config.example.toml` updated
- [ ] All commits land cleanly on `main`
