# assistant

A personal assistant built on Claude Code: one long-running Claude Code session that talks to its owner over
Telegram, plus the tools, hooks, plugins and services it works with. This repository is that harness: about 200
command-line tools (deployed into `~/bin`), shared Python libraries, Claude Code hooks and plugins, the hub services
(approvals, reminders, workflows, voice, a phone app gateway, a headed browser), schemas and the tests.

It is one working setup, shared as a reference and a starting point, not a packaged product. Many tools assume the
environment they were written for: an Unraid home server, Home Assistant, a media stack, Swiss admin helpers. Every
personal value (names, places, addresses, hosts) comes from a private profile file; the code ships placeholders only.
The hub services are optional and target an Unraid host: the core (the tools, the scheduled jobs, the Telegram
session) runs without them on any Linux machine or in the container from `ops/container/` (QUICKSTART.md says how).

## Start here

- [QUICKSTART.md](QUICKSTART.md): your own instance from zero, written so that a Claude Code agent can follow it
  step by step, with a check after each step. Hard requirements first, every integration optional.
- [docs/profile.example.json](docs/profile.example.json): the profile with placeholder values.
- [docs/secrets.md](docs/secrets.md): which secret file each feature needs (names only, never values).
- [docs/tools/](docs/tools): the hub's typed tools; [docs/runbooks/](docs/runbooks): operational runbooks.
- Every tool documents itself: `<tool> --help`.

The assistant's starter instructions (`CLAUDE.md` and its workspace files), skills and agents ship in
[`templates/`](templates); `assistant init` installs the ones for the features the user turns on (QUICKSTART step 9),
and each instance then makes them its own. Its memory starts empty. The separate privacy vault project is not in
this repository.

**Related:** [s-boris/privacy-vault](https://github.com/s-boris/privacy-vault), the privacy vault this
assistant talks to: it keeps the raw private data (chats, mail, documents) in its own container and gives the
assistant answers, not text.

## Layout

| Path | What |
|---|---|
| `scripts/` | the command-line tools; `ops/deploy/manifest.toml` maps each file to its place in `~/bin` |
| `core/` | shared Python library: paths, notes, the schedules registry (`core/schedules.toml`), events, locks |
| `packages/` | the hub and its parts: gateway, approvals, the install kit, tool policy, inbox, memory rows |
| `adapters/` | Telegram, voice, phone app gateway, browser |
| `hooks/` | Claude Code hooks: command guards, conduct checks, the agenda |
| `plugins/` | Claude Code plugins: `telegram-plus` (Telegram channel) and `heartbeat` (idle-time routine) |
| `ops/` | deploy, land and CI tooling (`ops/assistant.py`), `install.sh` (fresh install), `container/` (base image), `requirements/` (tool venvs), this deployment's boot script, hub images |
| `schemas/`, `web/`, `docs/`, `tests/` | data schemas, the dashboard page, docs, tests |

## How it is built and tested

- Python 3.13 with `uv sync --locked`; the Telegram and voice parts are TypeScript on Bun.
- `assistant land BRANCH` rebases a branch onto main, runs CI (the pytest files the changed paths reach plus a smoke
  set; the whole suite for shared core, CI config and gated paths), runs a privacy scan of the new commits, then
  deploys into `~/bin` and rolls back on failure.
- `assistant ci [--full]` runs the tests by hand and `assistant ci-nightly` runs the full set every night. Known
  flakes live in `tests/quarantine.txt` with an owner and an expiry.

## How this copy is published

`scripts/assistant-publish` publishes snapshots of the private main branch: one commit per publish, with a neutral
author and a message generated from the change itself. Each snapshot passes a privacy scan first (gitleaks, secret
patterns, a personal-value denylist, PII detectors) and any hit blocks it. The private development history is not
published.

## Security and licence

See [SECURITY.md](SECURITY.md) for how to report a problem. Licensed under the Apache License, Version 2.0 (see
[LICENSE](LICENSE) and [NOTICE](NOTICE)). `plugins/telegram-plus` and `adapters/tg` keep their own Apache-2.0 licence
files.
