#!/bin/sh
# The brain container's boot hook (this deployment's own container: things outside $HOME are rebuilt on every start).
# Run by the image's /usr/local/bin/entrypoint as the bot user, before the main session starts. Keep it idempotent.
# Not part of a fresh install: that is ops/install.sh (uv, Python 3.13, venv, deploy, PATH) and ops/container/
# (QUICKSTART.md). Everything below serves optional features of this deployment (hub, voice, browser, vault tests,
# evals); nothing in the core needs it. Pinned tool venvs: ops/requirements/NAME.txt in the repo.
req=${ASSISTANT_REPO:-$HOME/opt/assistant}/ops/requirements
# Tests: tests/legacy/boot/run.sh (suite boot): a dry run in a temp HOME with stubbed sudo/apt/curl.

# apt: one `apt-get update` per boot, bounded network waits (a stalled mirror cannot hold boot for minutes), and no
# further apt tries once an update failed (no network: boot moves on). Background steps inherit the state when they
# fork. apt_refresh forces a new update after adding a repo. The lock wait covers a background install in parallel.
apt_o="-o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 -o Acquire::Retries=1 -o DPkg::Lock::Timeout=120"
apt_state=
apt_refresh() { [ "$apt_state" = dead ] || apt_state=; }
apti() {
  [ "$apt_state" = dead ] && return 1
  if [ -z "$apt_state" ]; then
    if sudo apt-get $apt_o update -qq >/dev/null 2>&1; then apt_state=ok; else apt_state=dead; return 1; fi
  fi
  sudo DEBIAN_FRONTEND=noninteractive apt-get $apt_o install -y -qq "$@" >/dev/null 2>&1
}

# Restart cause (~/bin/restart-cause): ONE ~/inbox note per container start that wasn't a deliberate `restart`, with
# what restarted (container, host) and a cause guess from read-only server verbs. First, so a slow boot
# (apt reinstalls after a rebuild) doesn't push it past its 15-min window; backgrounded (needs the network).
[ -x $HOME/bin/restart-cause ] && (timeout 180 $HOME/bin/restart-cause >/dev/null 2>&1 &)

# telegram-plus: allow the custom channel plugin via managed settings,
# and install the launcher that prefers it, falling back to the official plugin.
ops=$HOME/plugins/telegram-plus/ops
if [ -f "$ops/managed-settings.json" ] && [ -x "$ops/start-claude" ]; then
  cmp -s "$ops/managed-settings.json" /etc/claude-code/managed-settings.json ||
    sudo install -D -m 644 "$ops/managed-settings.json" /etc/claude-code/managed-settings.json
  # Keep the image's own start-claude before ours replaces it: ~/opt/image-override-tests hashes this copy to catch
  # image drift. Only a file that isn't ours is kept (not the source, not an older copy of it with its marker line,
  # not the official-plugin variant). A plain restart leaves ours in place, so only a rebuilt container refreshes it.
  sc=/usr/local/bin/start-claude orig=$HOME/.local/state/image-orig/start-claude
  if [ -f "$sc" ] && ! cmp -s "$ops/start-claude" "$sc" && ! cmp -s "$ops/start-claude.official" "$sc" &&
     ! grep -qF 'start-claude by ~/boot.sh' "$sc"; then
    cmp -s "$sc" "$orig" || install -D -m 644 "$sc" "$orig"
  fi
  cmp -s "$ops/start-claude" "$sc" || sudo install -m 755 "$ops/start-claude" "$sc"
fi
# Bot token file (2026-10-12): the entrypoint rewrites it from the docker env var at every start; keep it owner-only.
# start-claude keeps the env var itself out of the main session (tools read this file at call time).
chmod 600 $HOME/.claude/channels/telegram/.env 2>/dev/null

# Cron heartbeat fallback (2026-10-13): the entrypoint writes /etc/cron.d/heartbeat AFTER this script, with
# ~/.local/bin first on its PATH, so `heartbeat N` runs this mod-aware wrapper, which execs the image's
# /usr/local/bin/heartbeat only when the heartbeat mod is dead, overdue (nextDue + 3 min) or failing, and keeps its
# stuck-dialog alert. Keep the installed copy current. Rollback: rm ~/.local/bin/heartbeat and drop these lines.
hbw=$HOME/plugins/heartbeat/ops/heartbeat-cron
[ -f "$hbw" ] && ! cmp -s "$hbw" $HOME/.local/bin/heartbeat && install -D -m 755 "$hbw" $HOME/.local/bin/heartbeat

# Python 3.13 for the managed scripts (29.10, owner: CI runs 3.13 only): their shebang is
# `#!/usr/local/bin/python3.13 -I`, the hub CI runner's interpreter (python:3.13 image) at the same path here: a link
# to uv's ~/.local/bin/python3.13 (no PATH involved). Offline. Rollback: the scripts' old `#!/usr/bin/python3 -I`
# shebang (git revert in ~/opt/assistant), then sudo rm /usr/local/bin/python3.13 and drop these lines.
py313=$HOME/.local/bin/python3.13
[ -x "$py313" ] && [ "$(readlink /usr/local/bin/python3.13 2>/dev/null)" != "$py313" ] && [ ! -e /usr/local/bin/python3.13 -o -L /usr/local/bin/python3.13 ] &&
  sudo ln -sfn "$py313" /usr/local/bin/python3.13

# Runtime pins (security updates): verified Node and Bun binaries live in ~/opt/runtimes (persists; bump by hand:
# fetch, check the sha256 against the release's signed SHASUMS, extract, new version + binary sha256 below). Each
# replaces the image's /usr/local/bin copy only when that one is older (a newer image wins) and the pinned binary's
# sha256 matches; the image's own binary is kept once in ~/.local/state/image-orig/runtimes for rollback. Offline.
# Debian's nodejs 18 (EOL) is no longer installed (its one user, the old in-container noVNC, went 30.10); should a
# package pull it in again, its /usr/bin/node is diverted away and points at the pinned Node.
# Rollback: drop these lines; sudo install -m 755 ~/.local/state/image-orig/runtimes/node-<v> /usr/local/bin/node
# (bun likewise); sudo rm /usr/bin/node; sudo dpkg-divert --local --remove /usr/bin/node.
rt=$HOME/opt/runtimes rto=$HOME/.local/state/image-orig/runtimes
pin_rt() {  # pin_rt NAME VERSION SHA256 SRC
  cur=$(/usr/local/bin/$1 --version 2>/dev/null); cur=${cur#v}
  [ "$cur" != "$2" ] && [ "$( { printf '%s\n%s\n' "${cur:-0}" "$2" | sort -V | tail -n 1; } 2>/dev/null)" = "$2" ] ||
    return 0
  [ -f "$4" ] && [ "$(sha256sum "$4" 2>/dev/null | cut -d' ' -f1)" = "$3" ] || return 0
  [ -z "$cur" ] || [ -e "$rto/$1-$cur" ] || install -D -m 755 "/usr/local/bin/$1" "$rto/$1-$cur"
  sudo install -m 755 "$4" "/usr/local/bin/$1"
}
pin_rt node 24.22.1 e32e2d16081cf644f88392934288086e7662529d0d43277c34f7aad2207cfafb "$rt/node-v24.22.1-linux-x64/bin/node"
pin_rt bun 1.4.4 10702245766b225e70bbf6235fd6b47214fd94b4760d00ffaed8ab0df57ac235 "$rt/bun-1.4.4/bun-linux-x64/bun"
if [ -f /usr/bin/node ] && [ ! -L /usr/bin/node ] && [ -x /usr/local/bin/node ]; then
  sudo dpkg-divert --quiet --local --rename --divert /usr/bin/node.debian --add /usr/bin/node &&
    sudo ln -sf /usr/local/bin/node /usr/bin/node
fi
if [ -e /usr/bin/node.debian ]; then sudo rm -f /usr/bin/node.debian; fi

# OpenSSH client (for the owner's home server; key in ~/.ssh persists, the package doesn't).
command -v ssh >/dev/null || apti openssh-client

# img2pdf (paperless skill: merge several photos of one document into a single PDF before upload).
command -v img2pdf >/dev/null || apti img2pdf
# python3-venv: letter-venv and research-venv below are built with `python3 -m venv`
{ command -v python3 >/dev/null && python3 -c "import ensurepip" 2>/dev/null; } >/dev/null 2>&1 || apti python3-venv
command -v age >/dev/null || apti age
# faketime: run test suites at fixed dates (date-dependence sweeps)
command -v faketime >/dev/null || apti faketime
# nmap: security-watch's weekly connect scan of his own public IP (hairpin view)
command -v nmap >/dev/null || apti nmap
command -v smbclient >/dev/null || apti smbclient   # SMB share probes (dispatcher has no smb verb yet)
# gcc: evals and test-all rebuild their network guard (.so) when its source changed
command -v gcc >/dev/null || apti gcc libc6-dev
# postgresql-15, binaries only: the hub store tests (~/opt/assistant tests/hubstore) run throwaway clusters with
# initdb. The createcluster.d drop-in (create_main_cluster = false) keeps the package from creating a main cluster.
dpkg -s postgresql-15 >/dev/null 2>&1 || {
  sudo mkdir -p /etc/postgresql-common/createcluster.d
  echo 'create_main_cluster = false' | sudo tee /etc/postgresql-common/createcluster.d/no-main-cluster.conf >/dev/null
  apti --no-install-recommends postgresql-15; }
# libnss-wrapper: the hub CI toolchain ships it beside its Postgres copy (packages/hubkit/citool.py); initdb in the
# ci container (uid 10101, no passwd entry) runs with it preloaded
[ -f /usr/lib/x86_64-linux-gnu/libnss_wrapper.so ] || apti --no-install-recommends libnss-wrapper

# Voice notes (~/bin/transcribe, 2026-10-09): whisper.cpp + models live in ~/opt and persist; only the apt
# runtime deps need reinstalling. Rebuilding whisper.cpp would also need build-essential + cmake.
# ffmpeg also serves vidmontage and trackinfo; python3-numpy = recall's semantic search (system python3 -I).
missing=; for p in ffmpeg libgomp1 rsync python3-numpy; do dpkg -s "$p" >/dev/null 2>&1 || missing="$missing $p"; done
[ -z "$missing" ] || apti $missing
# Warm whisper-server (127.0.0.1 only) so transcribe skips the model load; transcribe falls back to the CLI
# if it's down, and cron re-checks every 5 min. Backgrounded so boot isn't delayed by the ~1 s model load.
[ -x $HOME/bin/whisper-server-ctl ] && ($HOME/bin/whisper-server-ctl ensure >/dev/null 2>&1 &)
# Voice answers (~/bin/tts, local Piper TTS): the venv (~/opt/piper-venv, pinned in ops/requirements/piper.txt) and
# the voices (~/opt/piper-voices) persist; they need only the image's python3 and ffmpeg (above). `tts --check` fails
# after the image's python changed or files went missing: then `tts --setup` rebuilds the venv (needs python3-venv)
# and refetches the voices (md5-checked), in the background.
$HOME/bin/tts --check >/dev/null 2>&1 || ( { python3 -c "import ensurepip" 2>/dev/null || apti python3-venv
  $HOME/bin/tts --setup; } >/dev/null 2>&1 & )

# Admin desk (~/bin/letter, ~/bin/pdfform, 2026-10-10): poppler-utils (pdftotext/pdftoppm/pdfimages: checks, grids,
# tests) and the venv in ~/opt/letter-venv. The venv persists; it's rebuilt (in the background) only if it no longer
# imports, e.g. after the image's python changed.
command -v pdftotext >/dev/null || apti poppler-utils
# tesseract + deu/fra/ita: the vault photo OCR tests (tests/test_photos.py) run here too (2026-10-16)
command -v tesseract >/dev/null && dpkg -s tesseract-ocr-ita >/dev/null 2>&1 || apti tesseract-ocr tesseract-ocr-deu tesseract-ocr-fra tesseract-ocr-ita
lv=$HOME/opt/letter-venv
"$lv/bin/python" -c 'import reportlab, pypdf, pikepdf, PIL' 2>/dev/null || \
  ( { python3 -m venv --clear "$lv" && "$lv/bin/pip" install -q -r "$req/letter.txt"; } >/dev/null 2>&1 & )

# The headed browser runs in the hub container claude-hub-browser (skill browser; its own Xvfb/noVNC). Here only
# Google Chrome stable for headless runs (the dashboard and browsersvc suites) + locales and fonts; Chrome's .deb adds
# Google's apt repo itself and pulls its own libraries.
missing=; for p in locales fonts-liberation fonts-dejavu-core fonts-noto-core fonts-noto-color-emoji fonts-noto-cjk; do
  dpkg -s "$p" >/dev/null 2>&1 || missing="$missing $p"; done
[ -z "$missing" ] || apti --no-install-recommends $missing
# media posters renders with the system python3's Pillow (apt python3-pil).
dpkg -s python3-pil >/dev/null 2>&1 || apti --no-install-recommends python3-pil
if ! dpkg -s google-chrome-stable >/dev/null 2>&1; then
  d=$(mktemp -d) && curl -fsSL --connect-timeout 20 --max-time 600 -o "$d/chrome.deb" https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
    && apti --no-install-recommends "$d/chrome.deb"
  rm -rf "$d"
fi
locale -a 2>/dev/null | grep -q '^de_CH.utf8$' || { sudo sed -i -E 's/^# *((de_CH|en_US)\.UTF-8 UTF-8)/\1/' /etc/locale.gen && sudo locale-gen >/dev/null 2>&1; }
# Start it in the background (skipped if I stopped it on purpose with `browser stop`); cron re-checks every 5 min.
[ -x $HOME/bin/browser ] && ($HOME/bin/browser ensure >/dev/null 2>&1 &)
# flowd: live data-flow stream for the dashboard (SSE on 127.0.0.1:8790 + ssh reverse tunnel to the nginx VM); `flowd off` = kill switch, kept across reboots
[ -x $HOME/bin/flowd ] && ($HOME/bin/flowd ensure >/dev/null 2>&1 &)

# research (~/bin/research render): markdown-it-py lives in ~/opt/research-venv (pinned in ops/requirements/research.txt;
# persists). Rebuilt in the background only if it no longer imports (e.g. the image's Python changed).
rsv=$HOME/opt/research-venv
"$rsv/bin/python3" -I -c 'import markdown_it' >/dev/null 2>&1 || ( {
  { dpkg -s python3-venv >/dev/null 2>&1 || apti python3-venv; } &&
  python3 -m venv --clear "$rsv" && "$rsv/bin/pip" install -q --disable-pip-version-check -r "$req/research.txt"
} >/dev/null 2>&1 & )

# trackid (~/bin/trackid): shazamio (Shazam fingerprint lookup; shazamio_core = SongRec's algorithm in Rust) in
# ~/opt/trackid-venv, pinned in ops/requirements/trackid.txt (persists). Rebuilt in the background only if it no
# longer imports (e.g. the image's Python changed). ffmpeg: above.
tiv=$HOME/opt/trackid-venv
"$tiv/bin/python" -I -c 'import shazamio_core' >/dev/null 2>&1 || ( {
  { dpkg -s python3-venv >/dev/null 2>&1 || apti python3-venv; } &&
  python3 -m venv --clear "$tiv" && "$tiv/bin/pip" install -q --disable-pip-version-check -r "$req/trackid.txt"
} >/dev/null 2>&1 & )

# drive-watch (~/bin/drive-watch): pywaze (Waze routes, no key) in ~/opt/drive-venv, pinned in
# ops/requirements/drive.txt (persists). Rebuilt in the background only if it no longer imports.
dwv=$HOME/opt/drive-venv
"$dwv/bin/python" -I -c 'import pywaze.route_calculator' >/dev/null 2>&1 || ( {
  { dpkg -s python3-venv >/dev/null 2>&1 || apti python3-venv; } &&
  python3 -m venv --clear "$dwv" && "$dwv/bin/pip" install -q --disable-pip-version-check -r "$req/drive.txt"
} >/dev/null 2>&1 & )

# divelog + uwfix (~/bin): numpy + opencv-python-headless (uwfix colour analysis, before/after stills) and reportlab
# (divelog PDF export) in ~/opt/dive-venv, pinned in ops/requirements/dive.txt (persists). Both scripts re-exec
# into the venv when the system python lacks them. Rebuilt in the background only if it no longer imports.
dvv=$HOME/opt/dive-venv
"$dvv/bin/python" -I -c 'import cv2, numpy, reportlab' >/dev/null 2>&1 || ( {
  { dpkg -s python3-venv >/dev/null 2>&1 || apti python3-venv; } &&
  python3 -m venv --clear "$dvv" && "$dvv/bin/pip" install -q --disable-pip-version-check -r "$req/dive.txt"
} >/dev/null 2>&1 & )

# trackinfo (~/bin/trackinfo): librosa for BPM/key/energy in ~/opt/trackinfo-venv (pinned in
# ops/requirements/trackinfo.txt; persists); trackinfo re-execs into it. Rebuilt in the background only if it no
# longer imports.
tkv=$HOME/opt/trackinfo-venv
"$tkv/bin/python3" -I -c 'import numpy, scipy, librosa' >/dev/null 2>&1 || ( {
  { dpkg -s python3-venv >/dev/null 2>&1 || apti python3-venv; } &&
  python3 -m venv --clear "$tkv" && "$tkv/bin/pip" install -q --disable-pip-version-check -r "$req/trackinfo.txt"
} >/dev/null 2>&1 & )

# bills QR blur retry: Pillow lives in ~/opt/bills-venv (persists); rebuild in the background only if it no longer imports.
bv=$HOME/opt/bills-venv
"$bv/bin/python3" -I -c 'import PIL' >/dev/null 2>&1 || ( {
  { dpkg -s python3-venv >/dev/null 2>&1 || apti python3-venv; } &&
  python3 -m venv --clear "$bv" && "$bv/bin/pip" install -q --disable-pip-version-check "Pillow==12.3.0"
} >/dev/null 2>&1 & )

# Local LLM for private data (~/bin/llm-local, 2026-10-10): llama.cpp (pinned tag, built in ~/opt/llama.cpp) + Qwen3.5-9B
# Q4_K_M with MTP in ~/opt/llm-models, both persist. `install` is a no-op unless the binary is broken/missing (then it
# rebuilds, installing build-essential/cmake) or the model is missing (sha256-checked download). Then start llama-server
# on 127.0.0.1:8179 (nice 19, 8 threads) unless I stopped it on purpose; cron re-checks every 5 min. Backgrounded.
# moved to claude-vault 2026-10-10 (rollback: uncomment + rm ~/.config/llm-local/backend): [ -x $HOME/bin/llm-server-ctl ] && ( { $HOME/bin/llm-server-ctl install && $HOME/bin/llm-server-ctl ensure; } >/dev/null 2>&1 & )

# tgread (2026-10-10): runs inside the privacy vault (container claude-vault) since 2026-10-10; ~/bin/tgread is a wrapper
# around `vault tg`. The local venv is only for its offline tests (~/opt/tgread-tests/run.sh, against the canonical
# copy ~/opt/claude-vault/vendor/tgread). Telethon 1.45.0 + deps pinned with sha256 in ops/requirements/tgread*.txt;
# rebuilt in the background only if the selfcheck fails (e.g. the image's Python changed).
tv=$HOME/opt/tgread-venv
"$tv/bin/python3" -I $HOME/opt/claude-vault/vendor/tgread selfcheck >/dev/null 2>&1 || ( {
  { dpkg -s python3-venv >/dev/null 2>&1 || apti python3-venv; } &&
  python3 -m venv --clear "$tv" &&
  "$tv/bin/pip" install -q --disable-pip-version-check --require-hashes --no-deps -r "$req/tgread-build.txt" &&
  "$tv/bin/pip" install -q --disable-pip-version-check --require-hashes --no-deps --no-build-isolation --use-pep517 -r "$req/tgread.txt"
} >/dev/null 2>&1 & )

# GitHub CLI (create repos and push to his account; login lives in ~/.config/gh)
command -v gh >/dev/null || {
  sudo mkdir -p -m 755 /etc/apt/keyrings &&
  curl -fsSL --connect-timeout 20 --max-time 60 https://cli.github.com/packages/githubcli-archive-keyring.gpg | sudo tee /etc/apt/keyrings/githubcli-archive-keyring.gpg >/dev/null &&
  sudo chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg &&
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | sudo tee /etc/apt/sources.list.d/github-cli.list >/dev/null &&
  apt_refresh && apti gh; } >/dev/null 2>&1

# Voice relay and phone gateway run in claude-hub-voice / claude-hub-phone (P3-7 / P3-8). The in-container rollback
# checkouts ~/opt/voice-relay and ~/opt/phonegw (and their start hooks here) were retired 29.10 (v1 cleanup, moved to
# ~/backups/v1-cleanup-2910). ~/bin/voice-relay (managed) runs the hub release's voice copy for its local subcommands.
# Test venvs for the adapters' own suites (adapters/{phone,voice}/run-tests.sh), pinned by the repo's
# requirements.lock; built with uv (no python3-venv needed), rebuilt in the background only when they no longer import.
for n in phone voice; do
  av=$HOME/opt/$n-venv; al=${ASSISTANT_REPO:-$HOME/opt/assistant}/adapters/$n/requirements.lock
  [ -f "$al" ] && ! "$av/bin/python" -c "import aiohttp, pytest" >/dev/null 2>&1 &&
    ( { "$HOME/.local/bin/uv" venv -q --clear --python /usr/bin/python3 "$av" &&
        "$HOME/.local/bin/uv" pip install -q --python "$av/bin/python" -r "$al"; } >/dev/null 2>&1 & )
done
# voicecheck after a container start (JOBS.md): 5 min later, once the relay and its call bridge are up; the Live
# billing probe still runs at most once a day.
[ -x $HOME/bin/voicecheck ] && ( sleep 300; mkdir -p $HOME/.local/state/voicecheck
  $HOME/bin/voicecheck run >> $HOME/.local/state/voicecheck/cron.log 2>&1 ) &

# Self-evals (`evals`): unprivileged user for headless eval workers. No sudo, no cron/at, no access to
# node's 0700/0600 secrets; evals runs `claude -p` as this user in a sandbox HOME (stubs, fake MCP, net guard).
# Checked with plain reads first (the deny files are world-readable), so a warm boot makes no sudo calls.
if ! id evalbot >/dev/null 2>&1; then
  sudo -n useradd --uid 1990 --user-group --home-dir /nonexistent --no-create-home --shell /bin/bash evalbot >/dev/null 2>&1 || true
fi
for f in /etc/cron.deny /etc/at.deny; do
  grep -qx evalbot "$f" 2>/dev/null ||
    sudo -n sh -c "grep -qx evalbot $f 2>/dev/null || echo evalbot >> $f" 2>/dev/null || true
done

# /etc/timezone matches /etc/localtime (the image ships Etc/UTC there; readers of /etc/timezone get the real zone).
# Lives outside $HOME, so it is redone on every container start; silent and a no-op when they already match.
lz=$(readlink /etc/localtime 2>/dev/null); lz=${lz#*/zoneinfo/}
[ -n "$lz" ] && [ "$(cat /etc/timezone 2>/dev/null)" != "$lz" ] && printf '%s\n' "$lz" | sudo tee /etc/timezone >/dev/null 2>&1

# Boot never fails because a step did: the entrypoint goes on either way.
exit 0
