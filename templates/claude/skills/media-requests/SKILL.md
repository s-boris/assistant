---
name: media-requests
description: Request movies and TV shows for the owner's media server (Seerr → Radarr/Sonarr → Emby), report download status, and answer read-only library questions. Use for "add/download/request/get <movie or show>", "is X downloaded / available yet", "how far is X", "what's downloading", "what's new on Emby", "what's airing this week", "what's missing / stuck", "why isn't X on Emby yet", "/doctor", "are Sonarr/Radarr/Seerr ok", "what's trending", "something like X", "what's eating the space", "/tonight", "movie night", "what should I watch tonight", "/movienight", "/continue", "continue watching", "/vibe", "/finish", "complete my film series", and button presses with callback data `media_req:…`, `mt:req:…`, `mt:play:…`, `mt:more` or `fr:…`, and franchise poll answers.
---
# Media requests (Seerr, Radarr, Sonarr; Emby plays it)

Tool: `media` (~/bin/media; `media --help` has every option). Base URLs come from the profile (`services.seerr_url`, `radarr_url`, `sonarr_url`, `emby_url`), keys from `~/.secrets/media.json` by handle. Never print or paste keys.

| Command | Does |
|---|---|
| `media search "title" [--type movie\|tv] [-n 8]` | `Title (year) [type] tmdb:ID -- status` |
| `media request TMDB_ID --type movie\|tv [--seasons all\|1,2] [--dry-run]` | requests via Seerr; refuses if already available/requested and prints the status |
| `media status "title" [--type T]` | Seerr status, per-season state, Emby link, queue progress/ETA, import problems |
| `media queue` | everything downloading, % and ETA, stuck imports flagged `!!` |
| `media doctor ["title"] [--dry-run] [--json]` | why a request isn't on Emby yet + routine fixes (below) |
| `media recent [-n 15]` | recently imported movies and episodes |

Read-only extras (GET only; all take `--json`; exit 0 ok, 1 error, 2 health problems, 3 partial): `media airing [--days 7]` · `media missing` · `media cutoff` · `media health [--quiet]` · `media trending [--new]` · `media similar "title" [--new]` · `media hogs [--sort size|stale]` · `media unwatched [--report]`.

## "Add / download X"
1. `media search "X"` (add `--type` if they said movie or show).
2. One clear match (exact title, a given year, or obviously the top hit): request it straight away. Ambiguous (remakes, same-name movie and show): `reply` with 2–4 buttons, text `Title (year)`, `data` = `media_req:<tmdb>:<movie|tv>`. Already available or requested: don't request; say so (with the Emby link).
3. `media request <tmdb> --type <t>`. TV defaults to all missing seasons; `--seasons` if they asked for specific ones.
4. Confirm in one line: `🎬 Requested <title> (year), Radarr will grab it`.
A press `media_req:<tmdb>:<type>` → step 3 directly. Cron `media ready-watch` pings the owner when their request is on Emby (held during quiet hours), so don't promise to check back.

Requests need no extra confirmation (the owner's own server). Removing, deleting or blocklisting anything always needs the owner's explicit OK, and the script has no delete command on purpose.

## Picks: /tonight, /movienight, /continue, /vibe, /finish
Each runs one command and sends ONE `reply` with `text` = the JSON's `message`, `format: "rich"`, `buttons` = its `buttons`, `on_press: "keep"`. Exit 3 = a part failed (a ⚠️ line says which); exit 1 = one short line with the reason.
- `/tonight [words]`, "movie night": `media tonight --json --args "<their words>"` (moods, genres, a time budget like `90m`). Three picks: a library movie, a show, a movie to request. `mt:more` → `media tonight --json --more` as a NEW reply.
- `/movienight [names] [words]`: `media group --json --args "…"`: library movies nobody in the group has seen; when `poll` isn't null, also ONE `send_poll` from it. Votes (`poll_answer`) need no reply. Name only the members, never who saw what.
- `/continue`: `media continue --json` (partly watched + next episodes).
- `/vibe <mood or film>`: `media vibe --json "<their words>"` (library picks; a bare `/vibe` → ask for a mood or a film).
- `/finish`: `media finish --json` (shows started and paused for 14+ days).
- `mt:req:<tmdb>` → `media request <tmdb> --type movie`, then one line.
- `mt:play:<id>` → react ✍, `media play <id> --json`; one reply: exit 0 `▶ Playing <title> on the TV`, 3 `Sent to the TV, but it isn't playing yet`, 2 play on TV is off (set up in ~/.config/media/play.json), 1 the error. Never retry on your own.

## Franchise completer
For "what's missing from my film series": `media franchise --poll-json`, then ONE plain `reply` with `message` and ONE `send_poll` from `poll`. The answer (`poll_answer`) → `media franchise request --picked "<the options joined by ' | '>"`; one short line per title. "Stop suggesting X" → `media franchise ignore "X"`. Never pitch this unasked more than every few weeks, never in quiet hours.

## Status and the request doctor
- `media status "X"` → available (+ Emby link), downloading NN% with ETA, or waiting for release (unreleased titles show as requested with no queue entry: normal).
- `media queue` → a short list. `!!` stuck imports: `arr-unblock` handles the clean cases; anything else is the owner's call.
- "Why isn't X there yet", `/doctor [title]`: `media doctor "X"` (Bash timeout 200 s; `media doctor` alone = all open requests). It fixes the routine cases itself (re-monitor, search again, drop a failed download, retry a failed Seerr request); library files are never deleted or replaced. Reply in 1–3 lines: the verdict, what was fixed, what needs the owner. A title-mismatch verdict → offer `media doctor "X" --grab` and run it only after their yes.

## Library questions (read-only)
- "What's on this week": `media airing`; a short day-by-day list.
- "What's missing / stuck": `media missing`. Released movies still missing after months usually have no usable release: say so, don't re-request.
- "Is everything OK with Sonarr/Radarr": `media health`. Problems need the owner or the arr UI.
- "Trending / something like X": `media trending --new` or `media similar "X" --new`, then `media_req:` buttons.
- "What's eating the space": `media hogs`, `media unwatched --report`. Deleting, downgrading or unmonitoring is the owner's call; per-person viewing is other people's data: counts, not who watched what.

## Notes
- Friends use the same Seerr; requests show who asked. Friends' playback trouble: skill media-help.
- After changing ~/opt/assistant/scripts/media, run its offline tests (`tests/legacy/media/run.sh`) and land it.
