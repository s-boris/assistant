# Call account relogin inside claude-hub-voice

When the call bridge in `claude-hub-voice` refuses with `refused: no session file` (`voice-relay status`:
`tg_calls: refused: no session file`), the call account has no session. The relay still loads the hub's api config
(`/run/secrets/voice_tg_json`); only the session is missing.

The fix is one owner paste, `hub kit paste voice-relogin`. It logs the account in inside the container, on the hub's
own key copies, then restarts the container once. Nothing is copied from the brain.

## What the paste does (Unraid > Terminal, as root)

1. Checks before anything runs in the container. Each one stops the paste with a message:
   - the kit's sum ("the kit changed since this paste was made");
   - `claude-hub-voice` is installed and running;
   - `~/.voice-relay-off` exists and no `voice_relay.server serve` runs in claude-bot (one runner per session);
   - the loader's status says `running` with a release id;
   - that release passes the installed loader's own check (`voiceloader.py --check-release`).
2. `docker exec -i claude-hub-voice python3 -I -B /srv/releases/<rid>/voice/run.py tg …` runs voice-tg from the release
   on the relay's container paths (`VOICE_TG_CONFIG_FILE=/run/secrets/voice_tg_json`,
   `VOICE_TG_SESSION_DIR=/srv/voice/session`, `VOICE_TG_ACCESS_FILE=/srv/voice/config/access.json`).
3. `tg status --offline` must say `session: none`. A present session, or a release without the `tg` entry, stops the
   paste.
4. `tg login cancel` clears a login a broken earlier paste left behind. Then `tg login start --phone-stdin`: the phone
   comes from the api config; when the config has none, the paste asks for it.
5. The paste prompts for the code: `Login code (Telegram/SMS):`, a hidden prompt on his terminal (`read -s </dev/tty`),
   piped into `tg login finish --code-stdin`. A wrong code: it asks again while the login still waits, 5 tries at
   most. A 2FA account: it asks `2FA password of the call account:` and pipes it into `--password-stdin`.
6. `tg harden`: calls from contacts only, P2P never, the owner as a contact, recent dialogs loaded (caches his peer in
   the session, so outbound rings work at once). If harden fails, the paste says so and the login still holds.
7. `docker restart -t 20 claude-hub-voice` once, then the paste waits for `healthy` and asks the relay (`run.py status`
   with the container-local token) until `tg_calls` answers.
8. End line: `claude-hub voice <kit>: logged in again; claude-hub-voice restarted; call bridge on`. Anything else
   exits 1 with the bridge's own words.

### Safety properties
- One connection from code request to sign-in: `login start` spawns a detached waiter inside the container (through
  `run.py tg _wait`) that holds one client from `send_code` through `sign_in` / `check_password`; `login finish` only
  hands it the code through a 0600 file in the session dir. `init: true` reaps orphaned exec children.
- Never a second client on the session: the relay starts its bridge only at its own start; the login uses its own
  `voice-tg-login.session`, moved into place only after its client disconnected; harden connects and disconnects
  before the single restart; the paste refuses while the brain's relay is switched on or running, and when a session
  already exists.
- No secret on a command line: phone, code and password go through stdin (`docker exec -i`), never argv (Docker events
  log exec argv). The code lives in the waiter's 0600 input file (read once, deleted), the password in a 0600 file
  until `login finish` returns. Output goes through voice-tg's scrubber.
- Everything that runs is the release the loader already verifies; the paste runs nothing from the kit dir.

## Brain steps
1. `hub voice status` → `not held · voiceloader running <rid>`; `voice-relay status` → `live 0`, `tg_calls: refused:
   no session file …`.
2. `hub kit paste voice-relogin` (rendered by the last hub deploy). The first line names the voice kit.
3. Send it to him with "wait for my go". Before the go, check `voice-relay status` shows `live 0` again (the restart
   cuts a live app call). Hold every hub deploy until he reports done: a relay restart between the login and harden
   would put two clients on the session.

## Owner steps (~3 min, the call account's SIM at hand)
1. Unraid > Terminal (as root): paste the whole block and press Enter if it waits.
2. If asked `Phone number of the call account`, type the second SIM's number with its country code (+…).
3. The code arrives in the Telegram app where the call account is logged in, else as an SMS to the second SIM. Type it
   at `Login code (Telegram/SMS):`; nothing echoes; press Enter. **Never forward the code in a Telegram message**:
   Telegram expires a code shared in a chat.
4. If asked, type the account's 2FA password the same way.
5. Wait for the end line `… logged in again; claude-hub-voice restarted; call bridge on` (≤ ~1 min after the code).
6. Tell the brain "done". If the paste stops, send the brain its last two lines.

## Brain checks after
- `voice-relay status` → `tg_calls: on`, `live 0`. `voicecheck run --no-tg` → all ok.
- One inbound test call from him (greeting ≤ 1.5 s, one lookup). An outbound ring only with his OK.
- If harden printed `owner NOT added yet`, his one inbound call teaches the account his peer.

## When it stops
| Message | Meaning / next |
|---|---|
| the kit changed since this paste was made | re-render (`hub kit render` or a deploy), send a fresh `hub kit paste voice-relogin` |
| claude-hub-voice is not installed or not running | `docker ps -a` on the server via the brain; not a relogin problem |
| the brain's own relay is switched on / running | `touch ~/.voice-relay-off`, stop the in-container relay |
| the hub's relay is not running (held or starting) | `hub voice start` (held) or wait ~1 min (starting), resend |
| release … fails the loader's check | the release folder was changed: redeploy |
| a session is already there, or release … has no relogin step | `voice-tg: invalid choice: 'tg'` above = deploy first; `session: present` = the bridge's problem is elsewhere (`tg_calls` text) |
| `voice-tg: Telegram refused the code request: FLOOD_WAIT_…` | wait as long as Telegram says, then paste again |
| not logged in (see above); nothing restarted | code expired, too many tries or 2FA failed: paste again (a new code) |
| logged in, but after the restart the call bridge says … | the session is in place; read the `tg_calls` text (`voice-relay status`), no second relogin needed |

## Why a paste, not a hub tool
A `voice.tg_login` tool would route the code through Telegram, the brain and the gateway's usage and trace rows.
Telegram expires login codes sent in a chat, and the brain must never hold the code. The paste keeps the code on his
terminal and inside the container.

## Code and tests
- `adapters/voice/voice_relay/userbot.py`: `Paths` reads `VOICE_TG_CONFIG_FILE` / `VOICE_TG_SESSION_DIR` /
  `VOICE_TG_ACCESS_FILE` when no dirs are given; `--phone-stdin`, `--code-stdin`, `--password-stdin`; `ENTRY`: the
  waiter starts through `run.py tg`.
- `adapters/voice/run.py`: `run.py tg ARGS` = voice-tg from the release.
- `ops/hub/voice-relogin.sh` (template), `packages/hubkit/kit.py` (VOICE_TEMPLATES, `relogin_lines`),
  `packages/hubkit/cli.py` (`hub kit paste voice-relogin`).
- `adapters/voice/tests/test_userbot.py` (env paths, stdin values, password file life, run.py spawn);
  `tests/hub/test_voice_relogin.py` (render, no argv secrets, CLI, and the offline simulation
  `tests/hub/voice_relogin_sim.py`: 7 refusals, a release without the relogin step, login with one wrong code, no
  phone in the api config plus 2FA with one wrong password, an expired code, the bridge still refusing after the
  restart; it catches harden after the restart and the code on argv).
- Not covered offline: real Docker (the detached waiter surviving the end of `docker exec`, health after `docker
  restart`), the real Telegram code flow and Kurigram in the image. The status probe assumes the relay's internal
  port 8791 (the default); a moved port only makes the paste end with "no answer" after a good login.
