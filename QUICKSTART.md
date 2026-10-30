# Quickstart: your own assistant from this repo

> **For humans.** You don't run these steps yourself; a Claude Code agent does, and asks you when it needs you.
> 1. Install Claude Code (https://docs.claude.com/en/docs/claude-code) and log in.
> 2. Open it in an empty folder and say: "follow QUICKSTART.md from <repo URL>".
> 3. Have ready: a Telegram bot token from @BotFather, and about an hour. Everything else is asked step by step.
> 4. The agent stops and asks before anything outside your machine, and never asks you to paste secrets into chat.

Written for an agent from here on. Work through the steps in order, run each **Check** exactly, and move on only when
it shows the **Success** line; **Stop if** names the cases where you stop, show the user the output and ask.
Sections marked *Optional* can be skipped as a whole; nothing in the core depends on them.

Status: the host route (steps 1-8), step 9 (without the Claude Code login), step 11 and *Undo* were run end to end on
an empty home, as written here, with a placeholder profile, no accounts and no server. Of step 10 only the offline
parts ran (Bun, the token file check with a fake token, the adapter's config check, its cron line); the bot, the
plugin install and the first run need a real bot and a logged-in Claude Code. The container route (C) is written from
the same steps but has not been built end to end yet. Step 12 and the optional sections are written from the code:
if a command does not exist or behaves differently, stop and tell the user.

Rules for the agent:
- Ask the user before anything outside their own machine: creating accounts, spending money, writing to a server.
- Never print, paste or commit a secret. Ask the user to place secret files themselves (see `docs/secrets.md`).
- Never run a Telegram sender (`tg`, `tg-soft`, `tg-buttons`, `tg-voice`, `event-card`, `tg-outbox send`) before
  step 10.9: they are allowed only after the user's "ping" was answered on Telegram.
- If a check fails, stop at that step, show the error and the likely cause, and ask the user.

What you get with the core steps: a Claude Code session that runs all the time, talks to you over a Telegram bot,
and has about 200 command-line tools in `~/bin`, plus scheduled jobs in your crontab. Step 9 installs a neutral
starter `CLAUDE.md`, its workspace files, the hooks and the skills and agents of the features the user turns on
(`assistant init`, from `templates/`); the assistant's memory starts empty and grows as it works.

## Requirements

| | What | Notes |
|---|---|---|
| Hard | Linux x86_64 (Debian 12 or similar), a user account with sudo | the lock files are for linux x86_64 only |
| Hard | Claude Code and a Claude plan (or an API key) | the main session and its workers |
| Hard | A Telegram bot (token from @BotFather) and your Telegram user id | the only chat channel |
| Hard | Docker (container route), or a Debian-like host with sudo (host route) | see *Core setup* |
| Hard | git, curl, `uv`, Python 3.13 | the container has them; on a host, steps 1-4 |
| Hard | Bun | the Telegram plugin and its adapter run on it; step 10.2 |
| Optional | Home Assistant | lights, media players, presence |
| Optional | *arr stack, Emby, Seerr, SABnzbd, Bazarr | media requests and status |
| Optional | The hub containers (written for an Unraid server) | approvals, reminders, workflows, voice, phone app, headed browser; the core runs without them (see *Hub containers*) |
| Optional | A separate privacy vault | private mail and chat questions; not part of this repo |

## Core setup

Two routes; pick one with the user, then follow its steps in order:
- **Container** (recommended on a server or NAS with Docker): § *Container route* below replaces steps 1-8.
- **Host**: a Debian 12 (or similar) machine or VM where the user has sudo: steps 1-8.

Every step below has a **Check** (run it exactly) and a **Success** line. **Stop if** names the cases where you stop,
show the user the output and ask; never work around a failed check with another route.

### C. Container route (instead of steps 1-8)
Needs Docker on the machine (`docker version` works for the user). The image is Debian 12 with Python 3.13, Node,
the Claude Code CLI and this repo, installed by `ops/install.sh` for a user `bot` whose home is a Docker volume
(login, profile, secrets and state survive image rebuilds). Nothing personal is baked into the image.
1. Clone and build (from the clone's root):
   ```sh
   git clone <this repo's URL> assistant && cd assistant
   docker build -f ops/container/Dockerfile -t my-assistant .
   ```
   **Check:** `docker image inspect my-assistant --format '{{.Id}}'`
   **Success:** one `sha256:...` line. **Stop if:** the build fails (show the last 30 lines of its output).
2. Start it (the time zone is the one scheduled jobs run in; use the user's, as in their profile's `locale.tz`):
   ```sh
   docker run -d --name my-assistant --restart unless-stopped -e TZ=<Area/City> -v my-assistant-home:/home/bot my-assistant
   ```
   **Check:** `docker exec -u bot my-assistant sh -lc 'sh ~/opt/assistant/ops/install.sh --check && assistant --version'`
   **Success:** six `ok` lines and `assistant (release ...)`.
3. From now on run every command of this guide inside the container, as `bot`:
   `docker exec -it -u bot my-assistant bash -l`. Continue with step 6 (profile), then 7, 8 and 9 onwards.
   Updating later, inside the container: `git -C ~/opt/assistant pull && sh ~/opt/assistant/ops/install.sh`. The
   home volume keeps its own copy of the repo; rebuild the image only for a newer system layer (packages, Node, CLI).

### 1. System packages
```sh
sudo apt-get update && sudo apt-get install -y git curl ca-certificates cron procps ffmpeg poppler-utils age jq
```
**Check:** `git --version && curl --version | head -1 && command -v crontab`
**Success:** three lines (git, curl, a crontab path).
**Stop if:** `apt-get` is missing (not Debian-like: ask the user for the equivalent packages) or sudo asks for a
password the user does not want to give (use the container route instead).

### 2. Python 3.13 at /usr/local/bin/python3.13
The managed tools start with `#!/usr/local/bin/python3.13 -I`. If that file already runs Python 3.13, skip to step 3.
Otherwise `ops/install.sh` (step 4) installs uv's CPython 3.13 and links it there with `sudo -n`; when sudo needs a
password it stops and prints the one `sudo ln -sfn ...` command: show it to the user, let them run it, re-run step 4.
**Check:** `/usr/local/bin/python3.13 -c 'import sys; print(sys.version_info[:2])' || echo "not yet (step 4)"`
**Success:** `(3, 13)`, or `not yet (step 4)`.

### 3. The repo in ~/opt/assistant
The tools look for the repo at `~/opt/assistant` (or wherever `$ASSISTANT_REPO` points; then export it in
`~/.profile` too).
```sh
mkdir -p ~/opt && git clone <this repo's URL> ~/opt/assistant
```
**Check:** `git -C ~/opt/assistant log --oneline -1`
**Success:** one commit line.
**Stop if:** `~/opt/assistant` already exists and is not this repo (ask before moving it).

### 4. Install: uv, Python 3.13, dev venv, the tools into ~/bin, PATH
One idempotent script does the generic install (re-running it repairs what is missing, nothing else). It writes only
under your home (plus the one Python link of step 2).
```sh
sh ~/opt/assistant/ops/install.sh
```
It prints one line per step: `uv`, `python`, `repo`, `venv`, `deploy`, `path`, each `ok`. The deploy line names the
release and the number of files. A `warning: brain-commit exit 2` inside the deploy output is expected (no brain git).
**Check:** `sh ~/opt/assistant/ops/install.sh --check`
**Success:** six lines, each `ok` (deploy: `ok (no drift)`), exit 0.
**Stop if:** a line reads `FAIL ...` (show it and the lines before it). Typical causes: no network to github.com or
pypi.org (uv, packages), not x86_64 (the uv pin and the lock files are x86_64 Linux only), the python link (step 2).

### 5. PATH in this shell
Step 4 added `~/bin` and `~/.local/bin` to `~/.profile`; new login shells have them. For the rest of this session:
```sh
export PATH="$HOME/bin:$HOME/.local/bin:$PATH"
```
**Check:** `sh -lc 'for t in assistant profile-get events memory; do command -v "$t"; done'`
**Success:** four paths under `~/bin`.

### 6. Your profile
Every personal value the code needs (names, home town, time zone, server addresses) comes from one file. Without
it the code runs on placeholder defaults.
```sh
install -d -m 700 ~/.config/assistant
install -m 600 ~/opt/assistant/docs/profile.example.json ~/.config/assistant/profile.json
```
Then ask the user for each value in the **Profile keys** table below and edit the file. Keys for integrations the
user skips keep their placeholders.
**Check:** `profile-get --check && profile-get locale.tz`
**Success:** `profile-get: ok (<home>/.config/assistant/profile.json, N keys)` with no "unknown key" lines, then the
user's time zone.

### 7. Tool venvs (optional, per feature)
A few tools run in their own venv, pinned in `ops/requirements/NAME.txt`: `letter` (letters and PDF forms),
`research` (research reports), `trackid`, `trackinfo`, `dive`, `drive`, `piper` (voice answers). Install only the
ones for features the user wants, for example:
```sh
sh ~/opt/assistant/ops/install.sh --venvs letter,research
```
**Check:** `sh ~/opt/assistant/ops/install.sh --check --venvs letter,research` (the same names)
**Success:** every line `ok`, including one `venvs ok (NAME)` per name.

### 8. Smoke test
```sh
assistant --version && assistant drift && events --help >/dev/null && memory --help >/dev/null && echo smoke-ok
cd ~/opt/assistant && HOME=$(mktemp -d) .venv/bin/python -m pytest tests/core -q -x -p no:cacheprovider
```
**Success:** `assistant (release <release>)`, `no drift`, `smoke-ok`; pytest ends with `passed` and no `failed`.
**Stop if:** a test fails: show its name and the last 20 lines.

### 9. Claude Code, features and the starter files
1. Claude Code: if `claude --version` fails, install it (https://docs.claude.com/en/docs/claude-code). Logging in is
   the **user's** step (`claude`, then `/login`); an agent cannot do it.
   **Check:** `claude --version`
   **Success:** a version line. **Stop if:** the user has no Claude plan or API key yet.
2. Features. Show the user this list and ask which they want now (more can be turned on later). Turn on only what
   they also set up in *Optional integrations*; a feature that is off installs nothing and its tools say
   `feature X is off`.

   | Switch | Brings | Needs |
   |---|---|---|
   | `telegram` | the chat channel (buttons, polls and drafts with the telegram-plus plugin) | step 10 (on in the example profile) |
   | `calendar` | calendar skills | the claude.ai Google Calendar connector |
   | `home_assistant` | lights, media players, shopping list | § Home Assistant |
   | `media` | movie and show requests, media help | § Media stack |
   | `hub` | reminders, approvals, workflows, the pinned status board | § Hub containers |
   | `browser` | headed Chrome, web tasks with approval | `hub` |
   | `unraid` | server health, capacity, container updates, exposure checks | § Unraid server |
   | `swiss` | Swiss shop price watches (the deals skill and its daily drive-price check) | nothing |
   | `vault`, `phone`, `voice` | private data, phone app, calls | not in this repo: leave off |

   Write the answers into the `features` object of `~/.config/assistant/profile.json` (`true` = on). Keep every
   switch in that object, set to `true` or `false`: a switch left out counts as on (an older install without a
   `features` section keeps everything on). A profile with no `features` section at all gets one from
   `assistant init` (step 3) before it installs anything: core and `telegram` on, every other switch `false`.
   **Check:** `profile-get --check && profile-get --features`
   **Success:** `profile-get: ok (...)`, then `core` and the features the user chose, one per line.
   **Stop if:** `unknown key features.X` (a typo: the known names are in the table).
3. Starter files: a dry run first, then the install. It writes `~/workspace` (`CLAUDE.md`, `TOOLS.md`, `JOBS.md`,
   `TODO.md`, `GOALS.md`, `PROBLEMS.md`, `FACTS.md`, `CHANGELOG.md`, `PROACTIVE.md`), `~/.claude/settings.json`
   (the hooks, and a deny rule for the browser's unsafe code tool),
   `~/.claude/skills/` and `~/.claude/agents/`, with the profile's values filled in. It never overwrites a file
   that exists (`--force` does, keeping `<file>.bak`); running it again changes nothing.
   ```sh
   assistant init --dry-run
   assistant init
   ```
   **Check:** `test -s ~/workspace/CLAUDE.md && python3 -c 'import json, os; json.load(open(os.path.expanduser("~/.claude/settings.json")))' && ls ~/.claude/skills ~/.claude/agents`
   **Success:** no error, then the skill folders (with only `telegram` on: kb, night-log, research, voice-action,
   waiting-card, watch) and the agents (idea-scan, self-cleanup, weekly-digest).
   A line `installed     profile   ~/.config/assistant/profile.json  (features: core, telegram on; ...)` means the
   profile had no `features` section and init wrote one: show the user, and go back to step 2 if they want more.
   **Stop if:** a line `kept ... settings.json`: the user already had a settings file. Show them the hooks and the
   `permissions` deny rule in `~/.claude/settings.starter.json` and merge them only with their OK. A line
   `unknown placeholder(s)`: show it.
4. Make `CLAUDE.md` theirs: read its "Owner's rules" and "Talking to the owner" sections to the user in a few lines
   and ask what to change (how to address them, quiet hours, what needs their OK). Edit only those lines; keep the
   file short. Facts about them go through `memory set`, never into `CLAUDE.md`.
   **Check:** `memory status`
   **Success:** a line ending in `file matches rows: yes`.
5. Brain git: a local git history of the assistant's own files (`~/workspace/*.md`, `~/.claude` skills, agents and
   settings, `~/bin`, the crontab), so its changes can be diffed and undone. Self-cleanup restores from it, night-log
   reads it, security-watch checks its hooks, and every deploy and cron edit commits to it (`brain-commit`). It lives
   in `~/.brain.git`, never leaves the machine, and a secret scan blocks any commit that holds a secret.
   ```sh
   brain init && brain commit "first snapshot"
   ```
   **Check:** `brain log --oneline | head -1`
   **Success:** `brain: ready (...)` and `brain: committed <id> first snapshot` from the commands, then the check
   prints `<id> first snapshot`.
   **Stop if:** `brain: secretscan blocked ...`: a file under the allowlist holds something that looks like a secret.
   Show the file name (never its content) and ask the user.

Later: a feature switched on → set it in the profile and run `assistant init` again (it adds the new files only). A
feature switched off keeps its installed skills until you delete their folders in `~/.claude/skills` (ask first).

### 10. Telegram and the first run
Two plugins can carry the chat. **telegram-plus** is the recommended one: it ships in this repo
(`plugins/telegram-plus`; step 4 deployed it to the `~/plugins` marketplace) and has what the starter skills use:
`reply` with format `rich` (Markdown) and folded `details`, inline buttons with `on_press` (what happens to the
keyboard after a tap) and URL buttons, polls (`send_poll`), live progress drafts (`draft`), pins, locations and menu
commands. The official plugin (`telegram@claude-plugins-official`; version 0.0.7 checked) has plain or MarkdownV2
replies, files, reactions and edits only; with it the skills fall back to plain text with numbered options.
telegram-plus itself never polls Telegram: the repo's `tg-adapter` polls the bot and sends, and the plugin hands its
messages to the session. It does not need the hub. Steps 1-9 below are for telegram-plus; the official plugin is the
fallback at the end. This step was written from the code and has not been run end to end (it needs a real bot).

The `/...` commands are typed by the **user** at the Claude Code prompt in `~/workspace`; tell them what to type. The
bot token is theirs: never ask for it in a message, never read, print or edit the token file. From the access file
(`~/.claude/channels/telegram/access.json`) read only the `allowFrom` list, nothing else, and never edit it yourself.
1. Bot: the user creates one with @BotFather (`/newbot`), keeps the token, and tells you the bot's **username** (not
   the token). Their numeric Telegram user id comes from @userinfobot.
2. Bun (both plugins and the adapter run on it): if `bun --version` fails, the user installs it with their OK
   (https://bun.sh: `curl -fsSL https://bun.sh/install | bash`), then opens a new shell.
   **Check:** `bun --version` **Success:** a version line.
3. Token file, written by the user, never through you:
   ```sh
   install -d -m 700 ~/.secrets && install -m 600 /dev/null ~/.secrets/telegram-bot.env
   ```
   The user then opens `~/.secrets/telegram-bot.env` in an editor and writes one line `TELEGRAM_BOT_TOKEN=<token>`.
   **Check:** `stat -c %a ~/.secrets/telegram-bot.env && grep -c '^TELEGRAM_BOT_TOKEN=.' ~/.secrets/telegram-bot.env`
   **Success:** `600`, then `1`.
4. The plugin (the same marketplace serves the heartbeat in step 12):
   ```sh
   claude plugin marketplace add ~/plugins && claude plugin install telegram-plus@home-plugins
   ```
   **Check:** `claude plugin list | grep -c telegram-plus` **Success:** `1` or more.
5. Access: the user runs `/telegram-plus:access allow <their id>`, then `/telegram-plus:access policy allowlist`.
   Unknown senders are dropped silently (there are no pairing codes).
   **Check:** the user runs `/telegram-plus:access` (status) and reads you the policy and the allowed ids.
   **Success:** policy `allowlist`, one allowed id (theirs). **Stop if:** another id is allowed (ask whose it is).
6. The adapter's config, `~/.config/tg-adapter/live.json` (no secret in it; `bot` is the username from 1):
   ```json
   {"mode": "live", "bot": "<bot username>", "token_file": "~/.secrets/telegram-bot.env",
    "allow_from_file": "~/.claude/channels/telegram/access.json", "send": true}
   ```
   ```sh
   (cd ~/opt/assistant/adapters/tg && bun install --frozen-lockfile)
   tg-adapter check --instance live && tg-adapter ensure --instance live
   ```
   **Check:** `tg-adapter status --instance live`
   **Success:** `check` prints `config ok: @<bot username>, instance live, mode live, ...`; the status shows
   `configured yes · token present yes` and `adapter: running, pid N`. **Stop if:** `check` fails (it names the key),
   or `ensure` reports exit 4 (Telegram refused the token), 5 (the username does not match the token's bot) or 3
   (another program polls this bot, for example the official plugin in another session).
7. Keep the adapter up (it restarts it after a crash or a reboot; a no-op while it runs):
   ```sh
   assistant cron add tg-adapter-live '* * * * *' '$HOME/bin/tg-adapter ensure --cron --instance live >/dev/null 2>&1'
   ```
   **Check:** `assistant cron list | grep -c tg-adapter-live` **Success:** `1`. (`assistant cron check` lists this
   line as `untracked`: it is the user's own, which is fine.)
8. Start the long-running session (in tmux, or a service the user prefers):
   ```sh
   cd ~/workspace && claude --channels plugin:telegram-plus@home-plugins
   ```
   **Stop if:** Claude Code refuses the channel because the plugin is not from the official marketplace: show the
   message to the user (not verified on every Claude Code version).
9. First run: the user sends "ping" to the bot from their own account.
   **Success:** the session receives it and answers on Telegram, in the user's language.
   From here on the Telegram senders (`tg`, `tg-soft`, `tg-buttons`, `tg-voice`, `event-card`, `tg-outbox send`) may
   be used: the ping proved that the adapter sends, and they reach only the allowlisted user. Before this point they
   stay off, also when a later step fails.
   Then, in the same session: `memory core write` (creates `~/workspace/FACTS-core.md`, which `CLAUDE.md` imports),
   and ask the user for one first chore to hand over; put it in `~/workspace/TODO.md` under `## Due`.
   **Check:** `test -s ~/workspace/FACTS-core.md && echo ok`
   **Success:** `ok`.

*Fallback: the official plugin* (instead of 2-8; buttons, polls, drafts and rich format are then unavailable). The
plugin polls the bot itself, so the adapter of 6-7 must not run for the same bot. `/plugin install
telegram@claude-plugins-official`, then `/telegram:configure <token>` (typed by the user; the plugin keeps the token
in its own `.env`). Start with `cd ~/workspace && claude --channels plugin:telegram@claude-plugins-official`. Pairing:
the user messages the bot, gets a code, runs `/telegram:access pair <code>`, then `/telegram:access policy
allowlist`. Check as in 5 (one allowed id, policy `allowlist`), then continue with 9.

### 11. Scheduled jobs
`core/schedules.toml` lists every periodic job with its `feature`. `assistant cron install` writes the lines of the
enabled features into `~/crontab` (each tagged `# assistant:NAME`) and installs it with `crontab ~/crontab`. The
features come from `--features a,b` (switch names; `--features all` = every switch, the same as turning them all on
in the profile), else from the switches set `true` in the profile's `features` (`core/schedules.py`
FEATURE_SWITCHES maps each switch to its jobs; a switch without periodic jobs schedules nothing), else core only.
Core is always in: `housekeeping` (log rotation, temp tidy, transcript compaction), `events` (the inbox notes' event
log), `watch` (page watches) and the weekly cues of the starter agents (`goals`: idea scan, `self-cleanup`,
`weekly-digest`). Jobs marked `owner_only` in the registry belong to the deployment this repo comes from (their
skills are not shipped); they are never scheduled, and the dry run names them. A crontab the user already has is
kept: its lines stay, new lines are appended, and a re-run adds only what is missing. Cron runs in the machine's time zone (`date +%Z`): it must be the user's
(`profile-get locale.tz`); in the container, `-e TZ=...` (step C.2) sets it.
1. Dry run; it writes nothing and lists each line it would add:
   ```sh
   assistant cron install --dry-run
   ```
   **Success:** `cron install: features <names> (profile|default: core only); N line(s) to add, ...`, the lines,
   then `cron install: dry run, nothing written`. Show the user the feature list and the number of lines.
   **Stop if:** `unknown feature(s)` (only for names given with `--features`; the error lists the known names).
2. With the user's OK, install:
   ```sh
   assistant cron install
   ```
**Check:** `assistant cron check`
**Success:** `N active lines · N tagged · ...`; the jobs of features the user did not enable are listed as
`missing` (owner-only ones as `missing NAME (owner-only)`), which is fine. Lines of the user's own (not from the
registry, such as step 10.7's adapter line) show as `untracked`, also fine.
**Stop if:** a line shows as `changed` (someone edited a tagged line: show it to the user).

### 12. Heartbeat (recommended)
The assistant does its routine when it receives a `[heartbeat]` prompt. The `heartbeat` plugin in `~/plugins`
(deployed in step 4) submits one when the session has been idle. It acts only in a session whose working directory
is `~/workspace`. The `home-plugins` marketplace was added in step 10.4 (on the official-plugin route, add it first:
`claude plugin marketplace add ~/plugins`).
```sh
claude plugin install heartbeat@home-plugins
```
**Check:** after 10 idle minutes the session shows a `[heartbeat]` prompt.

## Optional integrations

### Home Assistant
1. Profile: `services.ha_url`, `services.ha_user`, `services.ha_phone`.
2. Secret: `~/.secrets/ha-admin-token` (a long-lived token the user creates in HA).
**Check:** `ha state <an entity id the user names>`
**Success:** the entity's state, not `token file not readable`.

### Media stack (*arr, Emby, Seerr)
1. Profile: `services.radarr_url`, `sonarr_url`, `seerr_url`, `emby_url`, `bazarr_url`, `sabnzbd_url`, `seerr_user`.
2. Secret: `~/.secrets/media.json` (API keys, see `docs/secrets.md`).
**Check:** `media --help`, then a read-only status command from it.
**Success:** a status answer, no "key missing" error.

### Unraid server
Server reads and writes go through `unraid-ssh` / `unraid-do`, which need a server-side command dispatcher that is
**not in this repo**. Skip this section unless that dispatcher is provided separately.

### Hub containers (optional; written for an Unraid server)
The core runs without the hub. Without it, the events ledger, approvals and the outbox use a local SQLite store
under `~/.local/state`, and the tools that need the hub stop with a one-line reason and a non-zero exit instead of a
traceback, for example `board: store unreachable: no hub token`, `hub wf: hub unreachable (no hub token)`,
`claude-hub-browser: no control token`. `hub status` shows `token: missing`. Leave the profile switch `hub` off and
`assistant cron install` schedules none of the hub's jobs (`hub`, `reminders`, `web-agent`, `status-board`).

The hub (`packages/hub`, `ops/hub`, CLI `hub kit ...`) runs approvals, reminders, workflows, the Telegram adapter,
voice, the phone gateway and the headed browser as Docker containers next to the assistant's own container. The kit
targets an Unraid host: it is installed by pasting a rendered script into the Unraid terminal as root. Other Docker
hosts are not supported yet.
1. Profile: `server.appdata`, `server.bot_home`, `server.bot_network`.
2. `hub kit render`, then `hub kit show` (no server access).
3. The user reviews and pastes `hub kit paste` output on the server. The agent never runs it.
**Check:** `hub status`
**Success:** the hub answers with a release and `token: ok`; `~/.secrets/hub-token` exists.
**Stop if:** the user has no Unraid server: skip this section.

### Privacy vault
A separate project, not in this repo. The tools that need it (`vault`, `tgread`, `llm-local`, `securefill`) report
that it is missing. Skip.

### Swiss admin helpers
`deadlines`, `kk-compare`, `bills` and the letter tools are built for Switzerland (`locale.country` = `CH`,
`home.canton`, `home.municipality_bfs`). Elsewhere, leave them unused. Their skills are not in `templates/`, and their
periodic jobs are `owner_only` in the registry: `assistant cron install` never schedules them (the `swiss` switch
schedules only the deals skill's daily drive-price check).

## Profile keys

Required for the core: `owner.first_name`, `locale.tz`, `locale.lang`, `home.town`. Everything else enables one
feature and may keep its placeholder until that feature is set up.

| Key | Type | Used for |
|---|---|---|
| `owner.first_name`, `owner.last_name` | string | greetings, letters, filling forms |
| `owner.email`, `owner.phone` | string | letters, forms (admin helpers) |
| `owner.birth_year` | number | insurance and tax helpers |
| `owner.github` | string | GitHub helpers (release checks, publishing) |
| `home.street`, `home.postcode`, `home.town`, `home.town_aliases` | string, list | weather, trips, local events, letters |
| `home.canton`, `home.municipality_bfs` | string, number | Swiss deadlines and tax helpers |
| `home.lat`, `home.lon` | number | weather, sunrise, travel times |
| `work.town`, `work.lat`, `work.lon` | string, number | commute and travel helpers |
| `work.precision` | string | `"town"` when only the work town is known (travel helpers stay coarse) |
| `locale.tz`, `locale.lang`, `locale.country` | string | schedules and quiet hours (time zone), language, country-specific helpers |
| `network.server_name`, `network.server_host`, `network.lan_prefix` | string | server health and security checks |
| `network.domain`, `network.hosts.web_vm` | string | your public domain and a web VM (dashboards, certificate checks) |
| `server.appdata`, `server.bot_home`, `server.bot_network` | string | hub install paths on the Unraid host |
| `services.ha_url`, `services.ha_user`, `services.ha_phone`, `services.ha_keep_models` | string, list | Home Assistant |
| `services.radarr_url` ... `services.sabnzbd_url`, `services.seerr_user` | string | media stack |
| `repos.assistant` | string | where `assistant-publish` pushes snapshots of your copy (optional) |
| `repos.private_code` | string | where `codebase-sync` keeps a sanitized export (optional) |
| `repos.private_vault` | string | the privacy vault project's repo (optional) |
| `interests.topics`, `interests.events` | list | news digests and event radar |
| `features.<switch>` | true / false | which optional features are installed and allowed to run (step 9; a switch left out is on, `core` always is; a profile without the section gets one from `assistant init`: core and `telegram` on, the rest `false`) |

## Undo
`assistant rollback` restores the previous release in `~/bin`. To remove the install, ask the user first, then:
1. `assistant uninstall --dry-run` lists what would go: only the files of the deployed release whose bytes are still
   the deployed ones (in `~/bin`, `~/plugins` and the deployed hooks in `~/.claude/hooks`), the core link, and the
   crontab lines tagged `# assistant:`. A file changed since the deploy is kept and named. Show the user the list.
2. With their OK: `assistant uninstall --yes`. Other files in those folders and the user's own cron lines stay.
   **Check:** `crontab -l | grep -c '# assistant:'` **Success:** `0`.
3. What stays, each removed only if the user asks for it by name: the session (stop it; `claude plugin uninstall
   telegram-plus@home-plugins` and `heartbeat@home-plugins`), the hooks in `~/.claude/settings.json` (they call the
   removed tools: take them out, or restore the user's own settings), `~/workspace`, the rest of `~/.claude`,
   `~/.config/assistant` (the profile), `~/.config/tg-adapter`, `~/.secrets`, `~/.brain.git`, `~/.local/state`, the
   PATH line `cron install` put at the top of the crontab, the PATH lines `ops/install.sh` added to `~/.profile`, the
   `~/bin/secretscan` link `assistant init` made, the clone in `~/opt/assistant` and the uv Python link of step 2.
   Never `rm -rf` a whole folder such as `~/bin`: it may hold the user's own files.
