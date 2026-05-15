# cowork-dash

A local personal dashboard. Cyberpunk-themed single-pane-of-glass for GitHub,
calendar, tasks, Claude usage, service status, and system stats. Runs as a
small Python daemon on `localhost`; the frontend is one static HTML page.

## Install + run

```bash
./run.sh
```

First run creates `.venv/`, installs deps, and copies a starter config to
`~/.cowork-dash/config.toml`. Edit that file (add a GitHub token + username),
then re-run. The dashboard is served at <http://localhost:7766>.

A fine-grained GitHub PAT with read-only access to **Issues**, **Pull
requests**, and **Metadata** is enough. See `config.example.toml` for the
full list of provider blocks.

## What's real vs mocked in v0

| panel            | status      |
|------------------|-------------|
| GitHub           | real        |
| Service status   | real (public status.json endpoints — Anthropic, OpenAI, GitHub, Linear, Vercel) |
| Calendar         | mocked      |
| Tasks            | mocked      |
| Claude usage     | mocked      |
| CC sessions      | mocked      |
| System stats     | mocked      |

Mocked panels show an amber `MOCK` chip in their header so you can tell at a
glance what's real.

## Planned provider queue

In rough ship order:

- **Calendar** via `ical-buddy` (macOS Calendar.app → shell out → parse)
- **System stats** via `psutil` (CPU, mem, network, uptime)
- **CC sessions** by scanning `~/Library/Application Support/Claude/local-agent-mode-sessions/`
- **Linear** via personal API key
- **Motion** via personal API key
- **Claude usage** approximated from a local message counter (5h + weekly windows)

See `CLAUDE.md` for the provider polling pattern and how to add a new one.
