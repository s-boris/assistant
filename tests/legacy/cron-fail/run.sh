#!/usr/bin/env bash
# Offline tests for ~/bin/cron-fail (one ~/inbox note per cron failure streak) and for the crontab lines that use it
# or the inline "NAME-failed" note pattern, plus three lints: no job may discard its output without cron-fail (or an
# allowlist entry with a reason), every command a job runs must exist and be executable, and ~/crontab and JOBS.md
# must describe the same jobs. Exit 0 = all pass.
# HOME is a temp dir; the commands are tiny stubs; the real jobs, the real ~/inbox and ~/.local/state are never touched.
# CRON_FAIL_BIN=<path> tests another copy; CRON_FAIL_CRONTAB=<path> checks another crontab; CRON_FAIL_JOBS=<path> another JOBS.md.
set -uo pipefail
B=${CRON_FAIL_BIN:-/home/node/bin/cron-fail}
CT=${CRON_FAIL_CRONTAB:-/home/node/crontab}
grep -q '/home/node' "$B" && { echo "refusing: $B names a real home path (must derive everything from \$HOME)"; exit 2; }
T=$(mktemp -d "${TMPDIR:-/tmp}/cron-fail-tests.XXXXXX")
trap 'rm -rf "${T:?}"' EXIT
export HOME=$T/home
mkdir -p "$HOME" "$T/bin"
S=$HOME/.local/state/cron-fail
pass=0; fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
no() { fail=$((fail + 1)); echo "FAIL - $1"; [ -n "${2:-}" ] && printf '       got: %s\n' "$2" | head -20; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else no "$1" "$2 (want: $3)"; fi; }
has() { if [[ "$2" == *"$3"* ]]; then ok "$1"; else no "$1" "$2"; fi; }
hasnt() { if [[ "$2" == *"$3"* ]]; then no "$1" "$2"; else ok "$1"; fi; }
notes() { find "$HOME/inbox" -maxdepth 1 -name "${1:-*}-failed-*.md" 2>/dev/null | wc -l | tr -d ' '; }
clean() { find "$HOME" -mindepth 1 -delete; }
# job RC [stdout] [stderr]: a stub command
cat > "$T/bin/job" <<'EOF'
#!/bin/sh
[ -n "${3-}" ] && echo "$3" >&2
[ -n "${2-}" ] && echo "$2"
exit "$1"
EOF
chmod +x "$T/bin/job"
J=$T/bin/job
run() { out=$("$B" "$@" 2>"$T/err"); rc=$?; err=$(cat "$T/err"); }

echo "# usage"
run; eq "no args -> exit 2" "$rc" 2
run onlyname; eq "name without command -> exit 2" "$rc" 2
run 'bad/name' "$J" 0; eq "name with a slash -> exit 2" "$rc" 2
# --after 0 (cron-back 29.10): record the run only, no streak, no note
run --after 0 x0 "$J" 1; eq "--after 0 -> runs, its own exit" "$rc" 1
run --after 0 x0 "$J" 1; eq "--after 0 -> no note" "$(notes x0)" 0
has "--after 0 -> a run log line" "$(cat "$S"/runs-*.jsonl 2>/dev/null)" '"job":"x0","rc":1,"ok":0'
run --ok x3 x "$J" 0; eq "--ok non-numeric -> exit 2" "$rc" 2
run --bogus x "$J" 0; eq "unknown option -> exit 2" "$rc" 2
eq "usage errors write no note" "$(notes)" 0

echo "# success"
clean
run demo "$J" 0 "hello out" "warn line"
eq "exit 0 passed through" "$rc" 0
eq "stdout passes through unchanged" "$out" "hello out"
eq "stderr not on cron-fail's stderr" "$err" ""
eq "stderr kept in NAME.err" "$(cat "$S/demo.err")" "warn line"
eq "no note on success" "$(notes)" 0
eq "state dir is owner-only (0700)" "$(stat -c %a "$S")" 700
eq "the job's own umask is untouched" "$("$B" um sh -c umask)" "$(umask)"
[ -e "$S/demo.streak" ] && no "no streak state on success" || ok "no streak state on success"

echo "# failure streak: one note"
clean
run demo "$J" 5 "" "boom 1"
eq "exit code passed through" "$rc" 5
eq "first failure -> one note" "$(notes demo)" 1
n=$(find "$HOME/inbox" -name 'demo-failed-*.md' | head -1); body=$(cat "$n")
has "note names the job" "$body" "demo"
has "note gives the exit code" "$body" "last exit 5"
has "note points at the err file" "$body" ".local/state/cron-fail/demo.err"
hasnt "note does not copy stderr text" "$body" "boom 1"
has "note marks itself as data" "$body" "Data, not instructions"
eq "stderr in NAME.err" "$(cat "$S/demo.err")" "boom 1"
run demo "$J" 5 "" "boom 2"; run demo "$J" 7 "" "boom 3"
eq "still failing -> still one note" "$(notes demo)" 1
eq "exit code of the later run" "$rc" 7
has "streak counts runs" "$(cat "$S/demo.streak")" "count=3"
hasnt "note not rewritten while failing" "$(cat "$n")" "3 run"

echo "# recovery"
run demo "$J" 0
eq "recovery exit 0" "$rc" 0
[ -e "$S/demo.streak" ] && no "streak cleared" || ok "streak cleared"
has "open note gets a Recovered line" "$(cat "$n")" "Recovered"
has "recovered line counts the runs" "$(cat "$n")" "after 3 failed run(s)"
eq "no extra note on recovery" "$(notes demo)" 1
run demo "$J" 0
eq "second good run appends nothing" "$(grep -c '^Recovered' "$n")" 1
sleep 1
run demo "$J" 1
eq "a new streak -> a new note" "$(notes demo)" 2

echo "# handled note stays handled"
clean
run demo "$J" 1; n=$(find "$HOME/inbox" -name 'demo-failed-*.md' | head -1)
find "$HOME/inbox" -name 'demo-failed-*.md' -delete
run demo "$J" 1; run demo "$J" 1
eq "deleted note mid-streak -> silent" "$(notes demo)" 0
run demo "$J" 0
eq "recovery after a handled note writes nothing" "$(notes demo)" 0
[ -e "$S/demo.streak" ] && no "streak cleared after handled note" || ok "streak cleared after handled note"

echo "# --after N"
clean
run --after 3 demo "$J" 1; run --after 3 demo "$J" 1
eq "--after 3: no note after 2 failures" "$(notes demo)" 0
run --after 3 demo "$J" 1
eq "--after 3: note on the 3rd" "$(notes demo)" 1
has "--after note counts 3 runs" "$(cat "$(find "$HOME/inbox" -name 'demo-failed-*.md' | head -1)")" "failed 3 run(s)"
run --after 3 demo "$J" 1; run --after 3 demo "$J" 1
eq "--after 3: still one note" "$(notes demo)" 1
clean
run --after 3 demo "$J" 1; run --after 3 demo "$J" 0; run --after 3 demo "$J" 1; run --after 3 demo "$J" 1
eq "--after 3: a good run in between resets the count" "$(notes demo)" 0

echo "# --ok CODES"
clean
run --ok 3 demo "$J" 3
eq "--ok 3: exit 3 still passed through" "$rc" 3
eq "--ok 3: exit 3 writes no note" "$(notes demo)" 0
run --ok 3,4 demo "$J" 4; eq "--ok 3,4: exit 4 writes no note" "$(notes demo)" 0
run --ok 3 demo "$J" 1; eq "--ok 3: exit 1 is a failure" "$(notes demo)" 1
run --ok 3 demo "$J" 13; eq "--ok 3: exit 13 is not 3 (no substring match), still one note" "$(notes demo)" 1
has "--ok 3: streak continues on 13" "$(cat "$S/demo.streak")" "count=2"
run --ok 3 demo "$J" 3
[ -e "$S/demo.streak" ] && no "--ok code ends a streak" || ok "--ok code ends a streak"

echo "# missing command, separate names"
clean
run gone "$T/bin/does-not-exist"
eq "missing command -> 127" "$rc" 127
eq "missing command -> note" "$(notes gone)" 1
run other "$J" 1
eq "names keep separate streaks" "$(notes other)" 1
run -- dash "$J" 0; eq "-- ends the options" "$rc" 0

echo "# args with spaces and quotes reach the command"
clean
cat > "$T/bin/argc" <<'EOF'
#!/bin/sh
printf '%s|' "$#" "$@"
EOF
chmod +x "$T/bin/argc"
run a "$T/bin/argc" "x y" 'it'"'"'s' '' --flag
eq "argv kept intact" "$out" "4|x y|it's||--flag|"

echo "# crontab lines (stubs, temp HOME)"
# Every active line using cron-fail or an inline "-failed-" note: real /home/node/bin/X, $HOME/bin/X and /home/node/opt/P/bin/X
# (keepers like voice-relay-up, phonegw-up) -> a failing stub, run with sh (cron's shell) under the temp HOME. The
# real keepers must never run here: under a temp HOME they would start a second daemon or rebuild the live venv.
# The note must land in the temp inbox; no literal home paths.
mkdir -p "$T/fake" "$T/pbin"
ln -sf "$B" "$T/pbin/cron-fail"
nlines=0
while IFS= read -r line; do
  cmd=$(printf '%s\n' "$line" | sed -E 's/^([^ ]+ +){5}//')
  label=$(printf '%s\n' "$cmd" | grep -oE 'cron-fail( --[a-z]+ [^ ]+)* [A-Za-z0-9._-]+' | awk '{print $NF}' | head -1)
  [ -n "$label" ] || label=$(printf '%s\n' "$cmd" | grep -oE 'inbox/[A-Za-z0-9._-]+-failed-' | head -1 | sed 's#inbox/##; s#-failed-$##')
  [ -n "$label" ] || { no "line has a recognisable job name: $line"; continue; }
  nlines=$((nlines + 1))
  hasnt "$label: no literal /home/node/inbox" "$cmd" "/home/node/inbox"
  hasnt "$label: no literal /home/node/.local in redirects" "$cmd" "/home/node/.local"
  for b in $(printf '%s\n' "$cmd" | grep -oE '(/home/node|\$HOME)/(bin|opt/[A-Za-z0-9._-]+/bin)/[A-Za-z0-9._-]+' | sort -u); do
    printf '#!/bin/sh\necho "stub %s $*" >> "$HOME/stub.log"\necho "stub failure" >&2\nexit 1\n' "$(basename "$b")" > "$T/fake/$(basename "$b")"
    chmod +x "$T/fake/$(basename "$b")"
  done
  printf '#!/bin/sh\necho "tg $*" >> "$HOME/tg.log"\n' > "$T/fake/tg"; chmod +x "$T/fake/tg"
  scmd=$(printf '%s\n' "$cmd" | sed "s#/home/node/bin/#$T/fake/#g; s#/home/node/opt/[A-Za-z0-9._-]*/bin/#$T/fake/#g; s#\\\$HOME/bin/#$T/fake/#g; s#/usr/local/bin/tg#$T/fake/tg#g; s#\\\\%#%#g")
  rbin='/home/node/[^ ]*bin/'
  [[ $scmd =~ $rbin ]] && { no "$label: every /home/node bin target is stubbed (none runs for real)" "$scmd"; continue; }
  clean
  after=
  if [[ $cmd == *cron-fail* ]]; then
    runs=5   # a streak note needs --after N failures: run at least N times
    after=$(printf '%s\n' "$cmd" | grep -oE 'cron-fail( --ok [^ ]+)? --after [0-9]+' | awk '{print $NF}' | head -1)
    [ -n "$after" ] && [ "$after" -gt "$runs" ] && runs=$after
  else runs=1; fi
  for _ in $(seq $runs); do HOME=$HOME PATH=$T/pbin:$T/fake:/usr/bin:/bin sh -c "$scmd" >/dev/null 2>&1; done
  if [ "${after:-}" = 0 ]; then   # cron-fail --after 0: record only, the job reports its own failures
    eq "$label: --after 0, $runs failing run(s) -> no note" "$(notes "$label")" 0
  else
    eq "$label: $runs failing run(s) -> exactly one note in the temp inbox" "$(notes "$label")" 1
  fi
  [ -s "$HOME/stub.log" ] && ok "$label: the stub ran" || no "$label: the stub ran"
  [ -e "$HOME/tg.log" ] && no "$label: no tg on failure" "$(cat "$HOME/tg.log")" || ok "$label: no tg on failure"
done < <(grep -vE '^[[:space:]]*(#|$)|^[A-Za-z_]+=' "$CT" | grep -E 'cron-fail |-failed-\$\(date')
[ "$nlines" -ge 1 ] && ok "crontab: $nlines line(s) checked" || no "crontab: no cron-fail / -failed- lines found in $CT"

echo
# moved_lines: the registry cron_line of each job moved off cron (runs_on hub / keepers / window; the registry deployed
# here, or under land CI the tree's). An allowlist or drift entry naming one is not stale while that line is out of
# the crontab: the registry keeps it for a rollback (`assistant cron add`), and the entry covers it again then.
moved_lines() {
  python3 -I - "${ASSISTANT_CI_TREE:-}" "${CRON_TARGET_HOME:-/home/node}" <<'PY'
import os, sys, tomllib
tree, home = sys.argv[1], sys.argv[2]
reg = os.path.join(tree, "core", "schedules.toml") if tree else os.path.join(home, "bin", "lib", "core", "schedules.toml")
try:
    with open(reg, "rb") as f:
        for j in tomllib.load(f).get("jobs", {}).values():
            if j.get("runs_on") in ("hub", "window", "keepers") and j.get("cron_line"): print(j["cron_line"])
except (OSError, ValueError):
    pass
PY
}
moved=$(moved_lines)

echo "# crontab silence lint"
# A job whose output goes nowhere (>/dev/null, 2>/dev/null, or no redirect at all under MAILTO="") must run under
# cron-fail, or be listed here with why it still surfaces failures. Entry = "stable command substring|reason".
SILENT_OK=(
  'channels/telegram/inbox -type f|pure cleanup: deletes week-old Telegram attachments'
  'dash-publish|writes its own dash-publish-failed / -recovered notes'
  'codebase-sync --cron|writes its own failure notes'
  'bin/test-all >/dev/null|inline test-all-failed note on the same line'
  'bin/keepers ensure --cron|each keeper has its own failure streak: NAME-jobfail notes (job.failed / job.recovered, core.jobfail; cron-fail would double them)'
  'bin/hub-sentinel check --cron|writes its own hub-sentinel note (hub.down) + one app push, and a Recovered line when the hub is back'
)
# silent_jobs FILE: print each active line that discards its output without cron-fail or an allowlist match.
# Checked per segment (split on ; && ||), so "a && cron-fail x >/dev/null; b >/dev/null" still flags b.
silent_jobs() {
  local f=$1 mailto_off=0 line cmd seg e hit re='>[[:space:]]*/dev/null'
  grep -qE '^[[:space:]]*MAILTO=("")?[[:space:]]*$' "$f" && mailto_off=1
  while IFS= read -r line; do
    if [[ $line == @* ]]; then cmd=${line#* }; else cmd=$(printf '%s\n' "$line" | sed -E 's/^([^ ]+ +){5}//'); fi
    hit=0
    while IFS= read -r seg; do
      [[ $seg =~ $re ]] && [[ $seg != *cron-fail* ]] && hit=1
    done < <(printf '%s\n' "$cmd" | sed -E 's/(;|&&|\|\|)/\n/g')
    [ "$mailto_off" = 1 ] && [[ $cmd != *'>'* && $cmd != *cron-fail* ]] && hit=1
    [ "$hit" = 1 ] || continue
    for e in "${SILENT_OK[@]}"; do [[ $cmd == *"${e%%|*}"* ]] && { hit=0; break; }; done
    [ "$hit" = 1 ] && printf '%s\n' "$line"
  done < <(grep -vE '^[[:space:]]*(#|$)|^[A-Za-z_]+=' "$f")
  return 0
}
eq "silence lint: every silent job in $CT runs under cron-fail or is allowlisted" "$(silent_jobs "$CT")" ""
active=$(grep -vE '^[[:space:]]*(#|$)' "$CT")
for e in "${SILENT_OK[@]}"; do
  [[ $e == *'|'?* ]] && ok "allowlist entry has a reason: ${e%%|*}" || no "allowlist entry has a reason: $e"
  [[ $active == *"${e%%|*}"* || $moved == *"${e%%|*}"* ]] \
    && ok "allowlist entry still matches a job: ${e%%|*}" || no "stale allowlist entry (no job matches): ${e%%|*}"
done
fx=$T/crontab.fixture
{ cat "$CT"
  echo '*/5 * * * * /home/node/bin/newjob --cron >/dev/null 2>&1'
  echo '0 1 * * * /home/node/bin/quietjob'
  echo '0 2 * * * mkdir -p "$HOME/x" && /home/node/bin/halfjob --cron 2>/dev/null'
  echo '0 3 * * * cron-fail okjob /home/node/bin/okjob >/dev/null 2>&1'
  echo '0 4 * * * /home/node/bin/loggedjob >> "$HOME/.local/state/loggedjob.log" 2>&1'
  echo '0 5 * * * cron-fail a /home/node/bin/a >/dev/null 2>&1; /home/node/bin/chainjob > /dev/null'
  echo '@reboot /home/node/bin/bootjob >/dev/null'
} > "$fx"
out=$(silent_jobs "$fx")
eq "fixture: exactly the 5 unwrapped silent jobs flagged" "$(printf '%s\n' "$out" | grep -c .)" 5
for j in newjob quietjob halfjob chainjob bootjob; do has "fixture: $j flagged" "$out" "$j"; done
for j in okjob loggedjob; do hasnt "fixture: $j not flagged" "$out" "$j"; done
printf 'MAILTO=someone\n0 1 * * * /home/node/bin/quietjob\n' > "$fx"
eq "fixture: no-redirect job not flagged when MAILTO delivers mail" "$(silent_jobs "$fx")" ""
mkdir -p "$T/mvtree/core"
printf '[jobs.mv]\nruns_on = "hub"\ncron_line = "0 1 * * * /home/node/bin/mvjob --x >/dev/null"\n' > "$T/mvtree/core/schedules.toml"
eq "fixture: a moved registry job's cron_line counts for the stale checks" "$(ASSISTANT_CI_TREE=$T/mvtree moved_lines)" \
  "0 1 * * * /home/node/bin/mvjob --x >/dev/null"
printf '[jobs.mv]\nruns_on = "cron"\ncron_line = "0 1 * * * /home/node/bin/mvjob --x >/dev/null"\n' > "$T/mvtree/core/schedules.toml"
eq "fixture: a runs_on cron job's line is not a moved line" "$(ASSISTANT_CI_TREE=$T/mvtree moved_lines)" ""

echo
echo "# crontab target lint"
# Every command an active line runs (first word of each ; && || | $( segment, unwrapped from cron-fail / timeout /
# nice / nohup / env and VAR= prefixes) must resolve: a path must exist and be executable, a bare name must be a
# builtin or on the crontab's PATH. Any ~/bin/NAME the line names anywhere must exist and be executable too.
# Also: each cron-fail NAME labels exactly one job, unless listed here (shared streak state on purpose).
SHARED_LABEL_OK=(
)
# cron_targets FILE HOMEDIR: print "missing <target> :: <line>" per broken target and "dup-label <name> :: <line>".
cron_targets() {
  local ok_labels="" e
  for e in "${SHARED_LABEL_OK[@]}"; do ok_labels+="${e%%|*} "; done
  python3 -I - "$1" "$2" "$ok_labels" <<'PY'
import os, re, shlex, shutil, sys
f, home, ok_labels = sys.argv[1], sys.argv[2], set(sys.argv[3].split())
lines = open(f, encoding="utf-8").read().splitlines()
path = "/usr/bin:/bin"
for l in lines:
    m = re.match(r"\s*PATH=(.*)$", l)
    if m: path = m.group(1).strip().strip('"')
path = path.replace("$HOME", home).replace("/home/node", home)
BUILTIN = {"cd", "echo", "test", "[", "[[", "true", "false", "exit", "export", "set", "read", "printf", ":", "{", "}",
           "if", "then", "else", "elif", "fi", "for", "while", "do", "done", "case", "esac", "!", "command", "exec"}
SEP = {";", "&&", "||", "|", "&", "(", ")", "|&", ";;"}
def expand(p):
    for pre in ("$HOME/", "${HOME}/", "~/", "/home/node/"):
        if p.startswith(pre): return os.path.join(home, p[len(pre):])
    return p
def target_ok(w):
    if "/" in w:
        p = expand(w)
        return os.path.isfile(p) and os.access(p, os.X_OK)
    return w in BUILTIN or shutil.which(w, path=path) is not None
labels = {}
for line in lines:
    if not line.strip() or line.lstrip().startswith("#") or re.match(r"[A-Za-z_]+=", line): continue
    cmd = line.split(None, 1)[1] if line.startswith("@") else line.split(None, 5)[5] if len(line.split(None, 5)) > 5 else ""
    bad = []
    for m in re.finditer(r"(?:/home/node|\$\{?HOME\}?|~)/bin/[\w.+-]+", cmd):
        if not target_ok(m.group(0)): bad.append(m.group(0))
    try:
        lx = shlex.shlex(cmd.replace("\\%", "%"), posix=True, punctuation_chars=True); lx.whitespace_split = True
        toks = list(lx)
    except ValueError as ex:
        print(f"unparsable ({ex}) :: {line}"); continue
    i, start = 0, True
    while i < len(toks):
        t = toks[i]
        if t in SEP or t.endswith("$") and i + 1 < len(toks) and toks[i + 1] == "(":
            start = True; i += 1; continue
        if re.fullmatch(r"[<>&]+|\d?[<>]+&?", t):   # redirect operator: skip its target
            i += 2; continue
        if re.fullmatch(r"\d", t) and i + 1 < len(toks) and toks[i + 1][:1] in "<>":
            i += 1; continue
        if not start: i += 1; continue
        if re.match(r"[A-Za-z_]\w*=", t): i += 1; continue          # VAR=value prefix
        if t == "cron-fail" or t.endswith("/cron-fail"):
            if not target_ok(t): bad.append(t)
            i += 1
            while i < len(toks) and toks[i] in ("--after", "--ok"): i += 2
            if i < len(toks):
                labels.setdefault(toks[i], []).append(line); i += 1
            continue
        if t == "flock":                      # flock [opts] LOCKFILE|FD [CMD…] or flock LOCKFILE -c 'cmd'
            if not target_ok(t): bad.append(t)
            i += 1
            while i < len(toks) and toks[i].startswith("-"):
                i += 2 if toks[i] in ("-w", "--wait", "--timeout", "-E", "--conflict-exit-code") else 1
            if i < len(toks) and toks[i] not in SEP: i += 1        # the lock file (not a command)
            if i < len(toks) and toks[i] in ("-c", "--command"): i += 2   # a shell string, not a command word
            continue
        if t in ("timeout", "nice", "nohup", "env", "ionice") or t in ("command", "exec"):
            if not target_ok(t): bad.append(t)
            i += 1
            while i < len(toks) and (toks[i].startswith("-") or re.fullmatch(r"[\d.]+[smhd]?", toks[i])
                                     or (t == "env" and re.match(r"[A-Za-z_]\w*=", toks[i]))):
                i += 1
            continue
        if not target_ok(t): bad.append(t)
        start = False; i += 1
    for b in dict.fromkeys(bad): print(f"missing {b} :: {line}")
for name, ls in labels.items():
    if len(ls) > 1 and name not in ok_labels:
        for l in ls: print(f"dup-label {name} :: {l}")
PY
}
out=$(cron_targets "$CT" "${CRON_TARGET_HOME:-/home/node}")
eq "target lint: every command and ~/bin script in $CT exists and is executable" "$out" ""
for e in "${SHARED_LABEL_OK[@]}"; do
  [[ $e == *'|'?* ]] && ok "shared-label entry has a reason: ${e%%|*}" || no "shared-label entry has a reason: $e"
  [ "$(grep -vE '^[[:space:]]*#' "$CT" | grep -c "cron-fail[^|;&]* ${e%%|*} ")" -ge 2 ] \
    && ok "shared-label entry still shared: ${e%%|*}" || no "stale shared-label entry: ${e%%|*}"
done
fh=$T/fakehome; mkdir -p "$fh/bin"
for s in cron-fail present chained piped sub; do printf '#!/bin/sh\n' > "$fh/bin/$s"; chmod +x "$fh/bin/$s"; done
printf '#!/bin/sh\n' > "$fh/bin/noexec"
{ echo 'MAILTO=""'
  echo "PATH=/home/node/bin:/usr/bin:/bin"
  echo '0 1 * * * cron-fail --after 3 a /home/node/bin/present --cron >/dev/null 2>&1'
  echo '0 2 * * * cron-fail --after 2 --ok 3 b timeout 900 /home/node/bin/gonejob run >/dev/null 2>&1'
  echo '0 3 * * * mkdir -p "$HOME/x" && /home/node/bin/chained; /home/node/bin/gone2 >> "$HOME/x/log" 2>&1'
  echo '0 4 * * * /home/node/bin/present | /home/node/bin/gone3 >/dev/null 2>&1'
  echo '0 5 * * * out=$($HOME/bin/gone4 2>"$HOME/e") || echo "x; y && z" >> "$HOME/inbox/n.md"'
  echo '0 6 * * * ~/bin/noexec >/dev/null 2>&1'
  echo '0 7 * * * notacommandxyz --flag'
  echo '@reboot cron-fail c /home/node/bin/sub >/dev/null; cron-fail c /home/node/bin/piped >/dev/null'
  echo '0 8 * * * find /home/node/nonexistent-dir -type f -delete 2>/dev/null; echo "see ~/bin/present" > /dev/null'
  echo '0 9 * * * cron-fail d flock -n "$HOME/x/a.lock" sh -c "/home/node/bin/present && /home/node/bin/chained" 2>> "$HOME/e"'
  echo '0 10 * * * flock -w 5 "$HOME/x/b.lock" /home/node/bin/gone5 >/dev/null 2>&1'
} > "$fx"
out=$(cron_targets "$fx" "$fh")
eq "fixture: exactly 9 findings" "$(printf '%s\n' "$out" | grep -c .)" 9
for j in /home/node/bin/gonejob /home/node/bin/gone2 /home/node/bin/gone3 '$HOME/bin/gone4' '~/bin/noexec' notacommandxyz /home/node/bin/gone5; do
  has "fixture: missing $j flagged" "$out" "missing $j ::"
done
has "fixture: finding carries the job line" "$out" "missing /home/node/bin/gonejob :: 0 2 * * * cron-fail --after 2 --ok 3 b timeout 900"
has "fixture: shared cron-fail label flagged" "$out" "dup-label c :: @reboot"
for j in present chained piped sub; do hasnt "fixture: $j not flagged" "$out" "bin/$j ::"; done
hasnt "fixture: find's path argument not treated as a command" "$out" "nonexistent-dir ::"
hasnt "fixture: flock's lock file not treated as a command" "$out" ".lock ::"

echo
echo "# JOBS.md drift lint"
# Every job unit in an active crontab line must be documented in JOBS.md: each ~/bin/NAME it runs (or its cron-fail
# label), and each cue (echo ... > ~/inbox/NAME-...) by note name or title. Every ~/bin script JOBS.md presents as a
# job (a backticked command in a bullet from "## Every minute" on, outside parentheses and before any "→") must still
# appear in an active crontab line or be a registry job the hub, its windows or `keepers` run (core/schedules.toml runs_on; under
# `assistant land` CI the registry of the tree being landed, $ASSISTANT_CI_TREE). Exceptions below: "substring or name|reason".
JOBS=${CRON_FAIL_JOBS:-/home/node/workspace/JOBS.md}
DRIFT_OK=(
  'channels/telegram/inbox -type f|script-less cleanup one-liner (Telegram attachment find -delete), covered in the Failure notes intro'
  'restart-cause|runs from boot.sh at every container start, not from cron'
  'pretrip-check|crontab line commented out on purpose; runs from a remind note before a trip'
  'preclear-watch|on demand: a crontab line exists only while a disk is precleared'
)
# jobs_drift CRONTAB JOBSMD HOMEDIR: print "undocumented <key> :: <line>", "unnamed :: <line>" and
# "unscheduled <name> :: <JOBS.md line>".
jobs_drift() {
  local ok="" e
  for e in "${DRIFT_OK[@]}"; do ok+="${e%%|*}"$'\n'; done
  python3 -I - "$1" "$2" "$3" "$ok" <<'PY'
import os, re, sys
ct, jm, home, oks = sys.argv[1], sys.argv[2], sys.argv[3], [o for o in sys.argv[4].split("\n") if o]
cron = [l for l in open(ct, encoding="utf-8").read().splitlines()
        if l.strip() and not l.lstrip().startswith("#") and not re.match(r"\s*[A-Za-z_]+=", l)]
doc = open(jm, encoding="utf-8").read()
low = doc.lower(); norm = re.sub(r"\s+", "-", low)
BIN = r"(?:/home/node|\$\{?HOME\}?|~)/bin/([\w.+-]+)"
SKIP = {"cron-fail", "tg"}
def isbin(n): return os.path.isfile(os.path.join(home, "bin", n))
def word(k, text, b=r"\w-"): return re.search(r"(?<![" + b + "])" + re.escape(k.lower()) + r"(?![" + b + "])", text) is not None
def exempt(s): return any(o in s for o in oks)
scheduled = set()
for line in cron:
    cmd = line.split(None, 1)[1] if line.startswith("@") else (line.split(None, 5) + [""])[5]
    units = []                                     # each unit: a list of alternative keys
    for m in re.finditer(r"cron-fail(?:\s+--(?:after|ok)\s+\S+)*\s+(\S+)\s+((?:timeout\s+\S+\s+)?)(\S+)", cmd):
        units.append([m.group(1), os.path.basename(m.group(3))]); scheduled.update(units[-1])
    named = {k for u in units for k in u}
    for m in re.finditer(BIN, cmd):
        n = m.group(1); scheduled.add(n)
        if n not in named and n not in SKIP: units.append([n]); named.add(n)
    for seg in re.split(r";|&&|\|\|", cmd):        # bare ~/bin command (dash-publish, ...)
        w = (seg.split() or [""])[0]
        if "/" not in w and isbin(w): scheduled.add(w)
        if "/" not in w and w not in named and w not in SKIP and isbin(w): units.append([w]); named.add(w)
    for m in re.finditer(r"\binbox/([a-z][\w-]*?)-(?:\$|\")", cmd):     # cue note
        n = m.group(1)
        if n.endswith("-failed") or n in named: continue
        t = re.search(r"it\)\.\s+(.+?)\s+(?:due\s+)?\$\(date", cmd)
        title = re.sub(r"^(weekly|daily)\s+", "", t.group(1).lower()) if t else n
        units.append(["cue:" + n, "cue:" + re.sub(r"\s+", "-", title)])
    if exempt(line): continue
    if not units: print(f"unnamed :: {line}"); continue
    for u in units:
        if not any(word(k[4:], norm, r"\w") if k.startswith("cue:") else word(k, low) for k in u):
            print(f"undocumented {u[0].replace('cue:', '')} :: {line}")
# jobs the harness runs itself (schedule registry runs_on hub / window / keepers: hub schedules, the hub's event-window
# clocks with the brain's eventwf fallback (P3-5), and the `keepers` line) are
# scheduled too, though no crontab line of their own names them
try:
    import tomllib
    # the registry deployed here, or under `assistant land` CI the tree being landed (it is what deploys next)
    tree = os.environ.get("ASSISTANT_CI_TREE", "")
    reg = os.path.join(tree, "core", "schedules.toml") if tree else os.path.join(home, "bin", "lib", "core", "schedules.toml")
    # `jobwrap NAME` (P3-3 B6): the recipe's own ~/bin commands ("$B/x", "$0/x") are what such a job runs
    wrap = os.path.join(tree, "scripts", "jobwrap") if tree else os.path.join(home, "bin", "jobwrap")
    def recipe(name):
        try: src = open(wrap, encoding="utf-8").read()
        except OSError: return set()
        m = re.search(r"^" + re.escape(name) + r"\)\s*$(.*?)^\s*;;", src, re.M | re.S)
        return set(re.findall(r"\"\$(?:B|0)/([\w.+-]+)\"", m.group(1))) if m else set()
    with open(reg, "rb") as f:
        for j in tomllib.load(f).get("jobs", {}).values():
            av = list(j.get("argv") or [])
            while av[:1] == ["timeout"]: av = av[2:]
            if j.get("runs_on") in ("hub", "window", "keepers") and av:
                scheduled.add(os.path.basename(av[0]))
                if os.path.basename(av[0]) == "jobwrap" and len(av) > 1: scheduled.update(recipe(av[1]))
except (OSError, ValueError, ImportError):
    pass
on = False
for line in doc.splitlines():
    if line.startswith("## "): on = not line.startswith("## Heartbeat") and on or line.startswith("## Every minute")
    if not on or not line.startswith("- ") or exempt(line): continue
    s = line.split("→", 1)[0]
    while True:
        s2 = re.sub(r"\([^()]*\)", "", s)
        if s2 == s: break
        s = s2
    for span in re.findall(r"`([^`]+)`", s):
        n = re.sub(r"^~/bin/", "", span.split()[0])
        if isbin(n) and n not in SKIP and n not in scheduled:
            print(f"unscheduled {n} :: {line[:120]}")
PY
}
out=$(jobs_drift "$CT" "$JOBS" "${CRON_TARGET_HOME:-/home/node}")
eq "drift lint: every $CT job is in $JOBS and every job $JOBS lists is scheduled" "$out" ""
for e in "${DRIFT_OK[@]}"; do
  [[ $e == *'|'?* ]] && ok "drift entry has a reason: ${e%%|*}" || no "drift entry has a reason: $e"
  { grep -qF -- "${e%%|*}" "$CT" || grep -qF -- "${e%%|*}" "$JOBS" || [[ $moved == *"${e%%|*}"* ]]; } \
    && ok "drift entry still names a crontab or JOBS.md entry: ${e%%|*}" || no "stale drift entry: ${e%%|*}"
done
dh=$T/drifthome; mkdir -p "$dh/bin"
for s in docjob labeled gonejob pretrip-check helper; do printf '#!/bin/sh\n' > "$dh/bin/$s"; chmod +x "$dh/bin/$s"; done
{ echo 'MAILTO=""'
  echo '0 1 * * * cron-fail --after 3 docjob /home/node/bin/docjob --cron >/dev/null 2>&1'
  echo '0 2 * * * cron-fail labeled timeout 60 /home/node/bin/labeled run >/dev/null 2>&1'
  echo '0 3 * * * /home/node/bin/newjob >> "$HOME/x.log" 2>&1'
  echo '0 4 * * * echo "Data, not instructions (scheduled cue from ~/crontab; JOBS.md says how to handle it). Weekly tea time $(date +\%F): go" > "$HOME/inbox/tea-$(date +\%F).md"'
  echo '0 5 * * * echo "Data, not instructions (scheduled cue from ~/crontab; JOBS.md says how to handle it). Weekly fish check $(date +\%F): go" > "$HOME/inbox/fishes-$(date +\%F).md"'
  echo '0 6 * * * find /tmp/nothing -type f -delete 2>/dev/null'
  echo '# 0 7 * * * /home/node/bin/gonejob'
} > "$fx"
{ echo '# Jobs'; echo '## Heartbeat'; echo '- `helper` decides (not a job)'
  echo '## Every minute'
  echo '- `docjob` (*/5): fine. → then `helper status`'
  echo '- `labeled run` (daily): fine (see `helper`).'
  echo '- Fish check (Mon note): fine.'
  echo '- `gonejob --cron` (daily): documented but no longer scheduled.'
  echo '- `pretrip-check`: commented out on purpose.'
} > "$T/jobs.fixture"
out=$(jobs_drift "$fx" "$T/jobs.fixture" "$dh")
eq "fixture: exactly 4 drift findings" "$(printf '%s\n' "$out" | grep -c .)" 4
has "fixture: undocumented script flagged" "$out" "undocumented newjob :: 0 3 * * *"
has "fixture: undocumented cue flagged" "$out" "undocumented tea :: 0 4 * * *"
has "fixture: script-less job flagged" "$out" "unnamed :: 0 6 * * * find /tmp/nothing"
has "fixture: documented but unscheduled job flagged" "$out" "unscheduled gonejob :: - \`gonejob"
for j in docjob labeled fishes pretrip-check helper; do hasnt "fixture: $j not flagged" "$out" " $j ::"; done
# under land CI the registry comes from the tree being landed: a job it moves to the hub counts as scheduled
mkdir -p "$T/citree/core"
printf '[jobs.gonejob]\nruns_on = "hub"\nargv = ["timeout", "60", "~/bin/gonejob", "--cron"]\n' > "$T/citree/core/schedules.toml"
out=$(ASSISTANT_CI_TREE=$T/citree jobs_drift "$fx" "$T/jobs.fixture" "$dh")
hasnt "fixture: CI tree registry runs_on hub counts as scheduled" "$out" "unscheduled gonejob"
eq "fixture: with the CI tree registry, 3 drift findings" "$(printf '%s\n' "$out" | grep -c .)" 3
# a P3-5 event-window job (runs_on window: the hub's window clocks, the brain's eventwf fallback) counts as scheduled too
printf '[jobs.gonejob]\nruns_on = "window"\nargv = ["~/bin/gonejob", "--cron"]\n' > "$T/citree/core/schedules.toml"
out=$(ASSISTANT_CI_TREE=$T/citree jobs_drift "$fx" "$T/jobs.fixture" "$dh")
hasnt "fixture: CI tree registry runs_on window counts as scheduled" "$out" "unscheduled gonejob"
# a P3-3 B6 `jobwrap NAME` job on the hub: the commands its recipe runs count as scheduled, other recipes' do not
mkdir -p "$T/citree/scripts"
printf 'case "$1" in\nwrapped)\n\tmkdir -p x &&\n\t\t"$B/gonejob" --cron\n\t;;\nother)\n\t"$B/docjob"\n\t;;\nesac\n' > "$T/citree/scripts/jobwrap"
printf '[jobs.wrapped]\nruns_on = "hub"\nargv = ["~/bin/jobwrap", "wrapped"]\n' > "$T/citree/core/schedules.toml"
out=$(ASSISTANT_CI_TREE=$T/citree jobs_drift "$fx" "$T/jobs.fixture" "$dh")
hasnt "fixture: a jobwrap recipe's command counts as scheduled" "$out" "unscheduled gonejob"
printf '[jobs.other]\nruns_on = "hub"\nargv = ["~/bin/jobwrap", "other"]\n' > "$T/citree/core/schedules.toml"
out=$(ASSISTANT_CI_TREE=$T/citree jobs_drift "$fx" "$T/jobs.fixture" "$dh")
has "fixture: another jobwrap recipe does not schedule it" "$out" "unscheduled gonejob"

echo
echo "pass $pass, fail $fail"
[ "$fail" = 0 ]
