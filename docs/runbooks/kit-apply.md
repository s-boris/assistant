# One-tap kit apply (dispatcher 3.3)

Routine claude-hub container changes apply without a root console paste. A fixed runner on the server
(`/boot/config/ssh/hub-kit-runner.sh`, reached only through the dispatcher's `hub-kit-*` verbs) applies a kit to
one hub container, and only within limits the server checks itself. The brain asks; the server decides the tier,
writes the card and, for tier 1, verifies the owner's tap in the hub's database. The paste kits remain the path for
everything the runner refuses.

Code: server `server/hub-kit-runner.sh` + the verbs in `src/claude-bot-cmd.in` of the staged dispatcher package
(`~/opt/staged/unraid-v33`, tests `~/opt/staged/unraid-v33-tests`); brain `packages/hubkit/kitapply.py`
(`hub kit apply-request | apply | apply-log`), tests `tests/hub/test_kitapply.py`.

## Tiers

The runner diffs `docker compose config --format json` of what is installed against the same files with the kit's
copy swapped in (installed `.env`, all profiles, same project directory), plus the kit files and the copied files.
The tier comes from that diff, never from the brain.

| Tier | What | Approval |
|---|---|---|
| 0 | Only the image changes, to the digest upstream publishes now for a newer tag of the same repo and tag family (checked by a pull), or back to a digest this server ran before. Or nothing changes: recreate. Applies to tg, voice, phone, phoenix, ci; a shared pin (python for voice + phone) recreates both. | None. At most 6 a day; audit line + Unraid notification. |
| 1 | voice/phone: environment, kit files (loader, live.json), secret files copied from the brain's home, mounts inside the part's own folder, networks the stack already has, benign compose keys (healthcheck, command, limits...). Any adapter: Telegram access that only narrows (no new id, same approver). Image changes that are not tier 0. `tg-off`, `voice-off` (stop + remove, data stays). | His tap on the hub approval for exactly these facts. At most 10 a day. |
| paste | First installs; anything that changes another container (claude-hub, hub-pg, or any service besides the part and an image-only shared pin); top-level compose sections (networks, volumes, secrets); privileges and exposure (user, cap_*, privileged, devices, ports, network_mode, pid/ipc, security_opt, read_only, secrets, volumes_from, env_file, extends, include, build, hooks, unknown keys); mounts outside the part's folder; Telegram access that widens or changes the approver; any other change to claude-hub-tg (it decides approvals); phoenix/ci changes besides the image; the phone install and rollback (they touch claude-bot's ports). | Refused by the runner: the owner pastes. |

Never through this path: claude-hub and hub-pg (approvals, verifier, Cedar, the loader `hubloader.py`, `hub.env`,
`secrets.copy`), claude-bot, the vault, the dispatcher itself. The runner has no code that writes those paths, and
the diff refuses any kit that would change them.

## Flow

1. `hub kit apply-request PART` runs `unraid-ssh run hub-kit-review PART`. The server copies the kit and the files to
   copy from the brain's home into a root-only snapshot (plain files only, no symlink in the path, at most 1 MiB,
   read as the home's owner through `setpriv`, never as root), diffs, and answers: tier, `facts` (sha256 of the
   reviewed facts, which include the installed files' hash), an ASCII summary of at most 80 characters, the card
   lines and the action string `{"action":"hub.kit.apply","facts":"<sha>","part":"<part>"}`.
2. Tier 0: `hub kit apply PART --facts SHA`. Tier 1: the brain mints a hub approval (`approvals new --action -` with
   exactly that action string and summary, ttl 30 min, ref `hubkit:<part>:<facts12>`) and sends the server's card
   with the nonce (outbox row `{"text": card, "approval": {"nonce": N}}`; the adapter adds ✅/❌). Paste: the
   reasons and the paste's name (`hub kit paste …`).
3. After his ✅: `hub kit apply PART --facts SHA --nonce N`. The server reviews again from a fresh snapshot; the facts
   must be identical (kit, installed files, upstream digest), else "something changed: review again".
4. Tier 1 check, all on the server: the approval row in hub-pg (`docker exec claude-hub-pg psql`) is `approved`,
   unused, `action_sha` = sha256 of the server's own action string, `summary` = the server's summary, `decided_by`
   = `allowFrom[0]` of the installed `tg/config/access.json` (root-owned; the hub can't write it), decided before its
   expiry, not expired now, window at most 24 h, nonce not in the runner's own used list. Then one atomic
   `UPDATE … SET used_ts, used_by='kit-runner' … AND used_ts IS NULL RETURNING nonce` consumes it.
5. Apply, fixed steps only: back up every file that changes, install the new ones (fixed destinations, modes and
   owners; always a temp file + `mv -T` rename; a root-owned file only inside a folder chain only root can write,
   a file in the hub's own folders such as `secrets/` written as its owner uid through `setpriv`), `docker compose pull` + `up -d --no-deps --wait` for the part (and an image-only shared pin), with
   `--force-recreate` whenever a bind-mounted file changes (loader, live.json, a secret or access copy: compose can't
   see those, so a plain `up -d` would keep the old loader running), or `--force-recreate` for a no-change recreate,
   or `stop` + `rm` for `-off`. A failure puts the old files back and starts the old container (recreated on the old
   files when it already ran on the new ones).
   Nothing from the kit is executed; the kit's paste scripts are never read.
6. Every review, apply and refusal: a line in `STATE/audit.log` (`hub kit apply-log`); every apply and every refused
   apply: an Unraid notification (event "claude-hub kit"). One run at a time (flock), at most 60 reviews a day.

## Trust root

- The runner and the dispatcher live on the flash, root-owned, installed by the owner's paste and checked against
  SHA256SUMS. The brain can't change them; it reaches them only through the forced SSH command with checked words
  and an empty environment.
- The tier and the card text come from the runner. The approval binds to the facts' sha256, so a tap can only ever
  apply what the server reviewed.
- His decision is read from the hub's approvals table: only the hub's tg caller decides (his user id, his DM, before
  the expiry); the brain can mint and send, never decide (Cedar).

## What an attacker who controls the brain can and can't do

Can:
- Apply tier 0 on its own: move an adapter, phoenix or ci to a newer upstream build of the same image family, back
  to a digest the server ran, or recreate one. Six a day, each one logged and notified.
- Show a misleading card. The adapter does not bind a tap to the card's message, and the brain still holds the bot
  token, so it can send any text with a real nonce's buttons. What a tap then applies is still only the reviewed,
  tier-1 facts (bounded by the table above), and the stored summary must equal the server's.
- Forge a tier-1 approval by first deploying a malicious hub release. Hub code that is not in the protected set is
  the brain's to deploy, runs in the same process as hubapprovals and can write the approvals table; hubverify says
  so for the protected gate itself. Such a forgery buys at most tier-1 changes to voice/phone (their config, env,
  secrets copied from the brain's own files, mounts inside their own folders), narrowing Telegram access, or stopping
  an adapter: no new privileges, no host path, no other container, no new approver. Ten a day, each notified.
- Fill the runner's snapshot area or audit log within the daily review limit.

Can't:
- Change claude-hub, hub-pg, the loader, the protected set, Cedar, claude-bot, the vault or the dispatcher through
  this path, or reuse a kit approval for anything else (its action_sha only ever matches `hub.kit.apply` for these
  facts; the protected-digest approvals use `hub.release.protected`).
- Add a Telegram id or change the approver: that is the trust root of every later approval, so it stays a paste.
- Give a container privileges, ports, devices, the docker socket, host paths, other containers' volumes or
  secrets, or another container's name.
- Replay an approval, use one after its expiry, or have the runner run anything written by the brain.
- Read host files through the snapshot: reads run as the brain home's owner, not root.
- Redirect a root write or a new mount through a symlink or hard link planted by hub code (uid 10100 can write
  `secrets/` and its state folders): a destination that is a symlink or has more than one link is a paste, root
  writes only into root-only folder chains, other files are written as their owner, and every part of a new bind
  mount's host path must be a real folder only root can write.
- Hide a refused compose keyword in flow style: the raw check matches keywords and YAML tags anywhere on a line,
  and the normalized diff catches what `include`/`extends` would merge in anyway.

## Image pins (tier 0) and the residual risk

The brain chooses the pin, so tier 0 accepts only: the same repo (fixed per service in the runner: oven/bun,
python, arizephoenix/phoenix), the same tag family (text before and after the version unchanged), a version at least
the running one, and a digest equal to what `docker pull repo:tag` resolves to right now; or a digest this server
ran before (rollback). A different repo is a paste; an older version, another family or an unverified digest needs
his tap. Remaining risk, stated: a malicious or broken upstream release in an allowed repo (the same as any update);
the brain picks the timing. It is bounded because compose keeps every privilege setting unchanged (user, cap_drop
ALL, no-new-privileges, read-only root) and the code these containers run comes from the brain's releases anyway, so
an image swap adds no capability the brain lacks.

## Hardening, not built

- A second Telegram bot whose token only the server holds: the runner sends its own card and reads his tap itself.
  Then neither the brain nor the hub can forge or mislead a tier-1 approval. Costs: a new bot (his account), the
  server polling Telegram.
- The tg adapter appends the approval's stored summary to every approval card and binds a tap to the card's
  message id. Then a brain-written card can't hide what the server's summary says.
- The loader runs hubverify's gate itself (hubverify's own note): then hub-release forgery needs his ✅ too.

## Operations

- Install: the owner's one-time paste (part D of the staged dispatcher package's INSTALL.md): dispatcher 3.3 + the runner + its config
  (paths and the copy list) on the flash; state in `<appdata>/claude-hub-kitrunner` (the profile's server.appdata) (root, 0700).
- Pause: rename or remove `/boot/config/ssh/hub-kit-runner.sh`; the verbs then refuse ("not installed") and the
  pastes keep working.
- Look: `hub kit apply-log`; on the server `STATE/audit.log`, backups per apply in `STATE/backup/`.
- Undo one apply: the paste for that part, or the backup files back plus `docker compose up -d <service>`.
