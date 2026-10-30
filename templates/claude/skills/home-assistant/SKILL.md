---
name: home-assistant
description: Control and query the owner's Home Assistant. Use for "turn off the TV", "lights on/off", "living room lights off", "dim the bedroom to 30%", "warmer light", "pause the music", "TV volume 20", "is the TV on", "which lights are on", "run scene/script X", "all lights off", "start the vacuum", "when did X run / what did X do today" (history, logbook), "why didn't automation X fire" (traces), and /warm, /lights, /party. Uses ~/bin/ha (privacy-gated reads, allowlisted writes with read-back). Not for HA cleanup or config changes (ha-audit, ha-apply) or dashboards.
---
# Home Assistant control

Tool: `ha` (~/bin) is the ONLY way I read or switch things in Home Assistant. Never call the HA API with ad-hoc scripts or curl, and never patch around its allowlist: that bypasses the privacy gate. Config and registry changes go through `ha-apply` and need the owner's OK.

Setup: profile `services.ha_url` (and `services.ha_user`), token in `~/.secrets/ha-admin-token` (see docs/secrets.md in the repo).

Typed tools (with the hub; they run `ha` itself: same gate, allowlist and read-back): MCP `mcp__hub__ha_*`, or from Bash `tool call ha.switch '{"action": "off", "targets": ["light.x"]}'`.
- Reads: `ha_state` {entity} · `ha_find` {words, domain} · `ha_list` {domain, area} · `ha_areas` · `ha_history` · `ha_logbook` · `ha_trace`.
- Writes: `ha_switch` {action on|off|toggle, targets | area} · `ha_set` {target | area, brightness, color_temp, color} · `ha_media` {player, action, volume} · `ha_run` {kind scene|script, target} · `ha_call` {service, entity, data}; each takes `dry_run`.
- `confirmed=false` = sent but not read back in time: say so and check the state, never resend blindly.

## Steps
1. Find the entity. A name that matches several entities is refused and ha lists the candidates: pick one, or ask the owner if it's unclear.
   `ha find "words" [--domain light]`, `ha list light --area "Bedroom"`, `ha areas`, `ha state <id|name>`
2. Act:
   - `ha on|off|toggle <id|name>`; a room: `ha off --area "Living Room"` (lights; `--domain switch|fan|media_player` for others)
   - `ha set <light> --brightness 30 --color-temp 2700` (`--color red`; `--area` works too)
   - `ha media <player> play|pause|stop|next|prev|mute|unmute|volume 20`
   - `ha scene run <x>`, `ha script run <x>`
   - anything else on the allowlist: `ha call media_player.select_source <id> '{"source": "HDMI1"}'`, `ha call vacuum.start <id>`
   - `-n` = dry run (resolves and shows what would be sent). Use it for testing, especially at night.
3. Read the result: ha prints `old -> new ok`. Exit 3 = sent but not confirmed within 8 s (say so, check `ha state` again). Exit 2 = refused (ambiguous, not allowed or unavailable), nothing sent. Exit 1 = HA unreachable or an error.
4. Reply in 1–2 lines ("TV off ✓"), or just react 👍.

## History, logbook, traces (read-only)
- `ha history <entity> [--since 24h|7d|HH:MM] [--attrs a,b] [--json]`: state changes and how long each lasted.
- `ha logbook [entity] [--since …]`: what changed and who did it.
- `ha trace <automation|script> [--last N]`: per run the trigger, conditions (ok/FAILED with the failing state), actions and errors.
- Location, network, notification and Bluetooth entities stay hidden.

## The owner's setup
- Live names come from ha itself: `ha areas`, `ha list media_player`, `ha list script`, `ha find "tv"`.
- "TV" often matches several entities (the TV, a streamer, its remote and cast entities). A TV that is off often shows unavailable: then it's already off; check the streamer.
- Optional private shortcut table (ids and rooms): `~/.config/assistant/ha-shortcuts.md`. Read it first for speed; when an id there is missing, trust the live names.
- Shopping list (`todo.shopping_list`): skill shopping, never `ha` directly.

## Safety (built into ha, and my rules)
- Only allowlisted domains and services; no reloads, no scene create/apply.
- Locks and alarms are never controlled by ha, with no override: point the owner to the HA app.
- Garage, gate and door openers need the owner's explicit yes each time; only then `--owner-ok "their words"` (logged).
- HA updates: `ha call update.install <update entity> '{"backup": true}' --owner-ok "their words"`, one update after their yes for that update.
- Buttons only via `ha call button.press <id>`; ask first for restart, reboot or factory buttons.
- Switch only what was asked for. No live experiments during quiet hours.

## Privacy
ha's output passes a privacy gate (masked values, trackers as zone or home/not_home). Don't relay person or tracker states unless the owner asks.

## Presets
- "/warm", "make the lights warm/dim": `lights-preset warm`; react 👍.
- "/lights": one message "💡 Lights" with buttons (on_press keep): [🕯 Warm & dim lt:warm] [🌙 Dim lt:dim] / [☀️ Full lt:full] [✨ Ambient lt:amb] / [🎬 Movie lt:movie] [⚫ All off lt:off]. A press: `lights-preset lt:<x>` (presets in ~/.config/assistant/lights-presets.json); no reply when it worked, one line when it failed.
- "/party", "party lights": `party on` (WLED lights follow the music; ends by itself after 4 h); `party off` restores them.

## Log
Every write goes to `~/.local/state/ha/actions.log` (JSON lines). For "what did you switch?", tail it.
