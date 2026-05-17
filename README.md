# cowork-dash

A local personal dashboard. Cyberpunk-themed single-pane-of-glass for
GitHub, calendar, tasks, Linear, Claude Code usage, service status, and
system stats. Runs as a small Python daemon on `localhost`; the frontend
is one static HTML page.

macOS-first (calendar reads via `ical-buddy`, Claude usage reads OAuth
creds from the macOS Keychain). Other panels work cross-platform.

## Install + run

```bash
./run.sh
```

First run creates `.venv/`, installs deps, and copies a starter config to
`~/.cowork-dash/config.toml`. Edit that file (at minimum add a GitHub
token + username), then re-run. The dashboard is served at
<http://localhost:7766>.

A fine-grained GitHub PAT with read-only access to **Issues**, **Pull
requests**, and **Metadata** is enough. See `config.example.toml` for the
full list of provider blocks.

## Providers

All panels read live data. Each provider polls on its own interval and
writes into a shared in-memory blob; the frontend fetches that blob every
5s. A provider whose credentials aren't configured shows an `OFFLINE`
chip rather than failing — you can run the dashboard with as few or as
many panels live as you like.

| panel          | source                                                                 | needs                            |
|----------------|------------------------------------------------------------------------|----------------------------------|
| GitHub         | api.github.com                                                         | fine-grained PAT                 |
| Service status | public status.json endpoints (Anthropic, OpenAI, GitHub, Linear, Vercel) | nothing                        |
| Calendar       | `ical-buddy` (macOS Calendar.app)                                      | `brew install ical-buddy` (macOS) |
| Tasks          | Motion API (`/v1/tasks`)                                               | Motion API key                   |
| Linear         | Linear GraphQL API (one panel per workspace)                           | personal API key per workspace   |
| Claude usage   | Anthropic rate-limit headers via 1-token probe; OAuth read from `~/.claude/.credentials.json` or `Claude Code-credentials` keychain item | Claude Code installed + signed in |
| CC sessions    | scan of `~/.claude/projects/**/*.jsonl`                                | Claude Code installed            |
| System stats   | `psutil` (CPU, mem, disk, net, processes)                              | nothing                          |
| Network        | local interfaces + ipinfo.io                                           | optional ipinfo token            |

The macOS Keychain access for Claude usage uses the standard `security`
CLI to read your own credentials — same item `claude` itself reads.
Nothing is written back.

## Customizing

- **Operator label** — set `[ui].operator_name` in
  `~/.cowork-dash/config.toml` to change the `OPERATOR://NAME` header
  text. Falls back to `$USER` if unset.
- **Themes** — click the THEME chip in the header (or press `T`) to
  cycle: NIGHTOPS · TRON·DARK · TRON·LIGHT · CYBER·DARK · CYBER·LIGHT.
  Choice persists in `localStorage`.

## License

[Apache 2.0](LICENSE).

## Architecture

See `CLAUDE.md` for the provider polling pattern, status vocabulary,
frontend rendering conventions, and how to add a new panel.
