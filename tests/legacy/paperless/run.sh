#!/usr/bin/env bash
# Offline smoke tests for ~/bin/paperless (the thin CLI that execs `~/bin/vault paperless ...`). Exit 0 = all pass.
# A temp HOME holds a fake bin/vault that logs argv and stdin and answers with a canned line and exit code, so
# nothing reaches the real vault or paperless. Checks: argv passthrough (spaces, quotes, flags), exit code,
# stdout/stderr and stdin passthrough, usage text, every documented subcommand exists in vault's paperless usage,
# no direct API path or token in the script, missing vault fails without output.
set -uo pipefail
P=${PAPERLESS_BIN:-/home/node/bin/paperless}   # PAPERLESS_BIN: test a copy
# the deployed vault client (not in this repo): its usage text is cross-checked where it exists (this host), skipped
# where it does not (a CI runner's HOME)
VAULT_REAL=${PAPERLESS_VAULT_CLIENT:-$HOME/bin/vault}
T=$(mktemp -d "${TMPDIR:-/tmp}/paperless-tests.XXXXXX")
trap 'find "$T" -mindepth 1 -delete; rmdir "$T"' EXIT
mkdir -p "$T/home/bin"
cat > "$T/home/bin/vault" <<'EOF'
#!/usr/bin/env python3
import json, os, sys
d = os.environ["FAKE_DIR"]
stdin = "" if sys.stdin.isatty() else sys.stdin.read()
with open(os.path.join(d, "calls.jsonl"), "a") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "stdin": stdin}) + "\n")
print(os.environ.get("FAKE_OUT", "fake-vault ok"))
print(os.environ.get("FAKE_ERR", "fake-vault err"), file=sys.stderr)
sys.exit(int(os.environ.get("FAKE_RC", "0")))
EOF
chmod +x "$T/home/bin/vault"
export FAKE_DIR=$T
unset FAKE_OUT FAKE_ERR FAKE_RC

pass=0; fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
no() { fail=$((fail + 1)); echo "FAIL - $1"; [ -n "${2:-}" ] && printf '       got: %s\n' "$2" | head -20; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else no "$1 (want: $3)" "$2"; fi; }
has() { case $2 in *"$3"*) ok "$1" ;; *) no "$1 (missing: $3)" "$2" ;; esac; }
run() { : > "$T/calls.jsonl"; HOME=$T/home timeout 20 python3 "$P" "$@"; }
last_argv() { python3 -c 'import json,sys; print(json.dumps(json.loads(open(sys.argv[1]).read().splitlines()[-1])["argv"]))' "$T/calls.jsonl"; }
last_stdin() { python3 -c 'import json,sys; print(json.loads(open(sys.argv[1]).read().splitlines()[-1])["stdin"], end="")' "$T/calls.jsonl"; }

echo "# A. usage and static checks"
doc=$(python3 -I -c 'import ast,sys; print(ast.get_docstring(ast.parse(open(sys.argv[1]).read())))' "$P")
has "docstring: usage line for search" "$doc" "paperless search QUERY"
has "docstring: says there is no direct API path" "$doc" "no direct API path"
code=$(python3 -I -c 'import ast,sys; t=ast.parse(open(sys.argv[1]).read()); t.body=t.body[1:]; print(ast.unparse(t))' "$P")
if grep -qiE 'requests|urllib|http|socket|token|api_key|PAPERLESS_URL' <<<"$code"; then
  no "code has no network client or token" "$code"; else ok "code has no network client or token"; fi
vusage=; [ -f "$VAULT_REAL" ] && vusage=$(grep -E 'vault paperless' "$VAULT_REAL")
[ -f "$VAULT_REAL" ] || echo "skip - vault usage cross-check: no vault client at $VAULT_REAL"
for sub in upload search get download meta recent task fields suggest "rule test" set "rule set" rules; do
  if grep -qE "^ +paperless $sub( |$)" <<<"$doc"; then
    [ -f "$VAULT_REAL" ] || { ok "docstring documents '$sub'"; continue; }
    case $sub in
      "rule test"|"rule set"|set|suggest|rules|get|fields) has "vault usage knows '$sub'" "$vusage" "vault paperless $sub" ;;
      *) has "vault usage knows '$sub'" "$vusage" "$sub" ;;
    esac
  else no "docstring documents '$sub'" ""; fi
done

echo "# B. passthrough to vault paperless"
out=$(run search "Rechnung Strom" -n 3 --json 2>/dev/null); rc=$?
eq "search: rc 0" "$rc" "0"
eq "search: argv" "$(last_argv)" '["paperless", "search", "Rechnung Strom", "-n", "3", "--json"]'
eq "search: vault stdout passes through" "$out" "fake-vault ok"
err=$(run meta 2>&1 >/dev/null)
eq "meta: vault stderr passes through" "$err" "fake-vault err"
run get 42 --content --reason 'he asked: "what does it say?"' >/dev/null 2>&1
eq "get --content: argv with quotes intact" "$(last_argv)" '["paperless", "get", "42", "--content", "--reason", "he asked: \"what does it say?\""]'
run --help >/dev/null 2>&1
eq "--help goes to vault paperless --help" "$(last_argv)" '["paperless", "--help"]'
run >/dev/null 2>&1
eq "no args: plain vault paperless (its usage)" "$(last_argv)" '["paperless"]'
FAKE_RC=10 run get 7 --content --reason r >/dev/null 2>&1; rc=$?
eq "exit 10 (waiting for his OK) passes through" "$rc" "10"
FAKE_RC=2 run bogus >/dev/null 2>&1; rc=$?
eq "exit 2 (bad arguments in vault) passes through" "$rc" "2"
printf 'line1\nline2\n' | run upload - --title T >/dev/null 2>&1
eq "stdin passes through" "$(last_stdin)" "$(printf 'line1\nline2\n')"
eq "one vault call per run" "$(wc -l < "$T/calls.jsonl")" "1"

echo "# C. errors"
mkdir -p "$T/empty"
out=$(HOME=$T/empty timeout 20 python3 "$P" search x 2>"$T/err"); rc=$?
[ "$rc" -ne 0 ] && ok "missing ~/bin/vault: non-zero exit ($rc)" || no "missing ~/bin/vault: non-zero exit" "$rc"
eq "missing ~/bin/vault: nothing on stdout" "$out" ""

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
