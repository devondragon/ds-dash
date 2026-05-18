# Contributing

This is a personal-scratch dashboard, not a product. Issues and small PRs
are welcome but no SLAs — replies happen when they happen.

## Ground rules

- Local-only. Everything runs on `localhost` (or a trusted LAN). Don't add
  features that assume reachability from the public internet.
- No build step. The frontend is one static HTML page plus a CSS file and
  a JS file. Don't add a bundler, framework, or transpiler.
- No database. State is the daemon's process memory; on restart, providers
  re-poll from scratch.
- One in-memory state blob, one polling pattern per provider. Read
  [`CLAUDE.md`](CLAUDE.md) before adding a provider — the conventions
  (status vocabulary, ticker events, error handling, secrets) are
  load-bearing.

## Adding a provider

1. Read the **Provider polling pattern** section of [`CLAUDE.md`](CLAUDE.md)
   — it has a working skeleton you can copy.
2. Add a `[<name>]` block to `config.example.toml` with the credentials
   the provider needs (or document that it works keyless).
3. Seed `STATE["providers"]["<name>"] = {"status": "pending"}` so the
   frontend sees the key from the first render.
4. Write the `async def <name>_poll(...)` coroutine. It must catch broadly
   and write `status: "error"` rather than letting an exception escape the
   `while True` loop.
5. Wire it up in `lifespan()` with config gating: spawn the task if
   credentials are present, otherwise set `status: "unconfigured"` and
   include a `message` pointing at the relevant config key.
6. Add the corresponding `renderXxx()` function in `static/app.js` and
   call it from `poll()`. Handle `status === 'pending'` /
   `'unconfigured'` / `'error'` defensively.
7. Update the provider tables in `README.md` and `CLAUDE.md`.

## Style

Python is unopinionated — match what's already there. The codebase uses:

- `async def` provider pollers with `while True` + `await asyncio.sleep`.
- `httpx.AsyncClient` for HTTP; never `requests`.
- F-strings, type hints on function signatures, no docstring novels.

Frontend JS is plain ES modules-free vanilla; the renderers all live in
`static/app.js`. Don't introduce a framework. The CSS uses a three-tier
token architecture documented in `docs/FRONTEND.md` — touch tokens, not
component selectors, when retuning.

## Testing

There aren't any tests yet. A `python -m py_compile daemon.py` syntax
check and an eyeball of the dashboard at <http://localhost:7766> is the
current bar. Adding tests is welcome, especially for the GraphQL/JSON
shape-mapping functions.

## Commits

Conventional-ish prefix preferred (`github:`, `frontend:`, `docs:`,
`fix:`, etc.) but not enforced. Imperative present tense; tell the reader
what the commit does, not what you did.
