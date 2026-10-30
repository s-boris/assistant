---
name: container-watch
description: Answer "are my containers up to date", "any security issues / vulnerabilities / CVEs on the server", "is Seerr/Radarr/… vulnerable", "does X bundle a vulnerable Node/Bun/Python", "is the HA add-on X affected", "should I update X" for the owner's Unraid server and Home Assistant add-ons, and handle the weekly container-watch alert notes (container-watch-*, container-watch-own-*) in ~/inbox. Uses ~/bin/container-watch (read-only inventory + GitHub releases/advisories + NVD + CISA KEV + bundled runtimes).
---
# Container updates & security advisories

**Never update, pull, recreate, restart or re-tag a container without the owner's explicit OK** for that specific change. That includes Unraid's "apply update", `docker pull`, editing a template's repository/tag and auto-update settings. The tool and this skill only read.

Tool: `container-watch` (~/bin), strictly read-only on the server. One call of the dispatcher's read-only `containers` verb (`unraid-ssh run containers`) reads docker metadata and Unraid's update status; with the home_assistant feature on, `ha list update --json` reads the HA add-ons. Installed versions come from the apps' own endpoints, or image labels/env as a fallback. Upstream data: GitHub releases and repository advisories, NVD, CISA's Known Exploited Vulnerabilities (KEV) catalogue, image registries, nodejs.org and endoflife.date, cached for 7 days in ~/.cache/container-watch.

## Answering a question
1. `container-watch` prints only actionable items (exit 1 if any, 0 and no output if none). It takes a few seconds and uses the cache; `--max-age 1d` for fresh upstream data. Don't loop it: unauthenticated GitHub allows 60 requests per hour and a cold run costs dozens.
2. `container-watch --report` gives the full table: installed vs latest, releases behind, image age, Unraid status, advisories (affecting/total) and notes. `--json` returns everything.
3. "Is the Sonarr/Radarr update safe for my scripts?": `container-watch --arr-api [--tag sonarr=TAG] [--tag radarr=TAG]` (GitHub only) checks the repo's `/api/v3` calls against the app's API spec at the tag: gone paths, methods or parameters, with script:line.
4. Reply in 2–5 lines: what's affected (container, installed → fixed version, severity, a one-line why, link), or "all current, nothing affecting your versions". Don't recite the table unless asked.

## What counts as actionable
- An advisory or CVE whose range includes the installed version: medium+ severity with a stable fix out for at least 3 days, or high/critical even with no fix.
- A newer release, out for at least 3 days, whose notes mention security/CVE/GHSA.
- Unraid has shown "update available" for 30+ days while 3+ releases piled up.
- Any affecting CVE in CISA's KEV catalogue: exploited in the wild, alerts at once as CRITICAL (its own `kev:` key). Say "exploited in the wild" when you tell the owner.
- Routine version bumps are never actionable when auto-update applies them. Medium issues fixed only in a beta are report-only.

## Bundled runtimes (the Node/Bun/Deno/Python/PHP/Ruby/Go/Java inside an image)
- Versions come from the container's `*VERSION` env, the image config in its registry and base-image tags.
- Actionable: Node missing a security release of its major line; Bun/Deno GitHub advisories (same rules as apps); any runtime on an end-of-life branch (MEDIUM); a KEV-listed runtime CVE at once.
- Report-only: other runtimes behind a release of a security-only branch, bugfix-branch lag, runtimes without a version variable.
- The fix is a rebuilt image from the app's maintainers (or a newer add-on in HA), not a container update: say so, and check whether a newer image tag already exists before suggesting anything.
- Findings on the assistant's own containers go to a `container-watch-own` inbox note, not to the owner: rebuild those yourself.

## Context
- Auto-updated `:latest` containers are usually current. The real risks are pinned tags, apps that update outside Docker (Home Assistant updates inside HA), and advisories without a fix.
- Repository advisories are often sloppy (open-ended ranges, "All"/"TBD"). An open-ended advisory with no fix version counts as fixed when the installed release is newer than the advisory. The report lists these, plus advisories it couldn't judge.

## Weekly alert (cron, `--notify --max-age 1d`)
- Silent unless something is actionable. Then ONE short message to the owner and a note in ~/inbox, once per item; a newer security release re-arms it.
- For an inbox alert note the owner already has the text: don't resend, do nothing unless asked. If they ask for an update, write a plan first (backup appdata, breaking changes, the exact tag/template change) and wait for their explicit OK before touching anything.
- If you tell the owner about an item yourself, run `container-watch --ack` afterwards so the weekly run doesn't repeat it.

Files: ~/.local/state/container-watch/ (state.json, last.json, last-collect.txt, runs.log, cron.log) and ~/.cache/container-watch/. The script is managed: edit ~/opt/assistant/scripts/container-watch on a branch, test with tests/legacy/container-watch, `assistant land`. To track a new app, add an entry to `APPS` (image regex, GitHub repo, version sources).
