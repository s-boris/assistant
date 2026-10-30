#!/bin/sh
# install.sh: the generic install of this repo into the current user's home. Any home path works ($HOME, never a
# fixed layout); idempotent, so a re-run repairs what is missing and changes nothing else. QUICKSTART.md runs it;
# ops/container/Dockerfile runs it at image build. The brain container's own boot hook (ops/boot.sh) is separate.
#
#   ops/install.sh [--venvs NAME,...] [--no-deploy] [--check]
#
# Steps, one line each ("ok", "skip" or "FAIL ..."; the first FAIL stops with exit 1):
#   uv        the pinned uv in ~/.local/bin (ops/ci/install-uv.sh: release tarball, sha256-checked)
#   python    /usr/local/bin/python3.13, the managed tools' shebang: present (python:3.13 images), else a link to
#             uv's CPython 3.13 (sudo -n; without sudo it stops and prints the one command to run)
#   repo      the repo at ~/opt/assistant or at $ASSISTANT_REPO (the tools look there)
#   venv      the dev environment: `uv sync --locked` in the repo (pytest, ruff)
#   deploy    `ops/assistant.py deploy`: the tools into ~/bin, plugins into ~/plugins (skipped with --no-deploy)
#   path      ~/bin and ~/.local/bin on PATH in ~/.profile (login shells; cron gets its own PATH line from
#             `assistant cron install`)
#   venvs     optional tool venvs ~/opt/NAME-venv from ops/requirements/NAME.txt (letter, research, trackid, drive,
#             dive, trackinfo, piper); only the names given with --venvs
# --check: run nothing that changes anything; report each step as ok or "missing" (exit 1 when one is missing).
# Needs: sh, curl, git, tar, sha256sum. Exit: 0 ok, 1 a step failed or is missing (--check), 2 usage.
set -u

venvs= deploy=1 check=0
while [ $# -gt 0 ]; do
  case $1 in
    --venvs) venvs=${2:-}; shift 2 || { echo "install.sh: --venvs needs NAME,..." >&2; exit 2; } ;;
    --venvs=*) venvs=${1#--venvs=}; shift ;;
    --no-deploy) deploy=0; shift ;;
    --check) check=1; shift ;;
    -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "install.sh: unknown argument $1 (see --help)" >&2; exit 2 ;;
  esac
done

here=$(cd "$(dirname "$0")/.." && pwd)
repo=${ASSISTANT_REPO:-$HOME/opt/assistant}
uv=$HOME/.local/bin/uv
py=/usr/local/bin/python3.13
missing=0
say() { printf '%-8s %s\n' "$1" "$2"; }
fail() { say "$1" "FAIL $2"; exit 1; }
need() { if [ "$check" = 1 ]; then say "$1" "missing: $2"; missing=1; return 1; fi; return 0; }

# uv
if sh "$here/ops/ci/install-uv.sh" --check; then say uv ok
elif need uv "$uv (pinned)"; then
  sh "$here/ops/ci/install-uv.sh" >/dev/null 2>&1 || true
  sh "$here/ops/ci/install-uv.sh" --check || fail uv "ops/ci/install-uv.sh did not install the pinned uv"
  say uv ok
fi

# python
if [ -x "$py" ] && "$py" -c 'import sys; sys.exit(sys.version_info[:2] != (3, 13))' 2>/dev/null; then say python ok
elif need python "$py"; then
  "$uv" python install 3.13 >/dev/null 2>&1 || fail python "uv python install 3.13"
  src=$("$uv" python find 3.13) || fail python "uv python find 3.13"
  sudo -n ln -sfn "$src" "$py" 2>/dev/null ||
    fail python "no $py and no passwordless sudo: run   sudo ln -sfn '$src' $py   then re-run ops/install.sh"
  say python "ok (linked to $src)"
fi

# repo
if [ -d "$repo/.git" ] || [ -f "$repo/.git" ]; then
  [ "$(cd "$repo" && pwd -P)" = "$(cd "$here" && pwd -P)" ] || say repo "note: running from $here, the tools use $repo"
  say repo "ok ($repo)"
else
  fail repo "no git checkout at $repo: clone the repo there, or set ASSISTANT_REPO"
fi

# venv
if [ -x "$repo/.venv/bin/pytest" ] && (cd "$repo" && "$uv" sync --locked --check >/dev/null 2>&1); then say venv ok
elif need venv "$repo/.venv (uv sync --locked)"; then
  (cd "$repo" && "$uv" sync --locked -q) || fail venv "uv sync --locked in $repo"
  say venv ok
fi

# deploy
if [ "$deploy" = 0 ]; then say deploy "skip (--no-deploy)"
elif [ -x "$HOME/bin/assistant" ] && "$HOME/bin/assistant" drift >/dev/null 2>&1; then say deploy "ok (no drift)"
elif need deploy "~/bin (ops/assistant.py deploy)"; then
  out=$(ASSISTANT_REPO=$repo "$py" -I "$repo/ops/assistant.py" deploy 2>&1) ||
    { printf '%s\n' "$out" | tail -n 5 >&2; fail deploy "ops/assistant.py deploy (output above)"; }
  say deploy "ok ($(printf '%s\n' "$out" | grep -m1 '^deployed' || echo deployed))"
fi

# path
if grep -qs 'HOME/bin' "$HOME/.profile"; then say path ok
elif need path "~/bin on PATH in ~/.profile"; then
  printf '\n# assistant tools (ops/install.sh)\nexport PATH="$HOME/bin:$HOME/.local/bin:$PATH"\n' >> "$HOME/.profile"
  say path "ok (added to ~/.profile; new login shells get it)"
fi

# venvs
for n in $(printf '%s' "$venvs" | tr ',' ' '); do
  req=$repo/ops/requirements/$n.txt v=$HOME/opt/$n-venv
  [ -f "$req" ] || fail venvs "no ops/requirements/$n.txt (have: $(ls "$repo/ops/requirements" | sed 's/\.txt$//' | tr '\n' ' '))"
  case $n in tgread*) fail venvs "$n is the privacy vault's (not part of this repo's install)" ;; esac
  if [ -x "$v/bin/python" ] && [ "$v/.req" -nt "$req" ] 2>/dev/null; then say venvs "ok ($n)"; continue; fi
  need venvs "$v" || continue
  { "$uv" venv -q --clear --python "$py" "$v" && "$uv" pip install -q --python "$v/bin/python" -r "$req" && touch "$v/.req"; } ||
    fail venvs "$n: uv pip install -r ops/requirements/$n.txt"
  say venvs "ok ($n)"
done

[ "$missing" = 0 ] || exit 1
exit 0
