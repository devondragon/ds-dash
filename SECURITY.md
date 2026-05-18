# Security

## Threat model

cowork-dash is a **local-only** dashboard. Its security model is bind
address + filesystem perms — there is no authentication on any endpoint,
including the writable scratchpad. By default the daemon binds to
`127.0.0.1` so only the local machine can reach it.

You opt out of that protection by setting `[server].host` to anything
other than `127.0.0.1`. The daemon prints a startup warning when you do,
because anyone who can reach the host on the network can:

- Read everything the dashboard surfaces (GitHub data, calendar events,
  Linear/Motion task titles, scratchpad contents, Claude usage, WAN IP,
  process list).
- Overwrite the scratchpad.

This is fine on a trusted LAN; do not expose it to the public internet.

## Credentials

All provider credentials live in `~/.cowork-dash/config.toml`. The file
is outside the repo and not git-tracked. Recommendation: `chmod 600`. The
daemon never reads credentials from environment variables and never logs
tokens.

## Reporting a vulnerability

This is a personal-scratch project, so disclosure expectations are
informal:

- For local-only issues (a bug that lets a process on the same machine
  read something it shouldn't, etc.), open a GitHub issue.
- For anything that breaks the "local-only" assumption (e.g. a way for a
  remote caller to trick the loopback bind into responding), please
  **don't** file a public issue. Email the maintainer directly via the
  contact info on their GitHub profile.

No bounty, but credit in the fix commit if you want it.
