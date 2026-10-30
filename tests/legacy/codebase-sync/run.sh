#!/usr/bin/env bash
# Tests for ~/bin/codebase-sync (sanitized export -> git remote). Fully offline: a fake HOME, a fake scrub
# list, a local bare repo as "origin". Values that look like PII or tokens are assembled at runtime so this
# file itself stays clean when it is exported. Run: bash tests/legacy/codebase-sync/run.sh (CODEBASE_SYNC_BIN=<copy>)
set -u
CS=${CODEBASE_SYNC_BIN:-/home/node/bin/codebase-sync}
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
H=$T/home; X=$T/export; R=$T/remote.git
export CODEBASE_SYNC_HOME=$H CODEBASE_SYNC_EXPORT=$X CODEBASE_SYNC_SCRUB=$T/scrub.txt \
       CODEBASE_SYNC_INBOX=$T/inbox CODEBASE_SYNC_STATE=$T/state SECRETSCAN_SOURCES=$H CODEBASE_SYNC_MODE=report \
       CODEBASE_SYNC_GATE_CONF=$T/gate.conf   # no such file until section 9: no gate
pass=0; fail=0
ok() { if eval "$1"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $2"; fi; }
rhead() { git --git-dir="$R" rev-parse -q --verify main 2>/dev/null || echo none; }
PB=$T/state/pending-block.json
# age the pending block by 31 min (the default persistence window is 30)
age() { python3 -c 'import json,sys; p=sys.argv[1]; d=json.load(open(p)); d["first"]-=31*60; json.dump(d,open(p,"w"))' "$PB"; }
notes() { ls "$T"/inbox/codebase-sync-*.md 2>/dev/null | wc -l; }

# --- fake values (built at runtime) ---
EMAIL="jane.owner@""mailbox.ch"
PHONE="+41 79 ""987 65 43"
PHONE2="079 ""222 33 44"
CHAT=9876""54321
NAME="Zorroname"
STREET="Fakestrasse"" 7"
TOKEN="$(printf '%s:%s' 7654321 AAHfakefakefakefakefakefakefake12345)"
SVALUE="svcvalue""Q8w7e6r5t4y3u2i1"

mkdir -p "$H"/{bin,.claude/skills/demo,.claude/agents,.claude/hooks,.claude/channels/telegram,.secrets,workspace} \
         "$H"/.claude/projects/-home-node-workspace/memory "$H"/opt/demo-tests/fixtures "$H"/opt/dash \
         "$H"/plugins/p1/node_modules/x "$T/inbox"
cat > "$H/bin/tool" <<EOF
#!/bin/sh
# owner: $NAME, mail $EMAIL, phone $PHONE, chat $CHAT, home $STREET, 1234 Exampletown
echo hi
EOF
chmod 755 "$H/bin/tool"
echo "# skill about $NAME" > "$H/.claude/skills/demo/SKILL.md"
echo "agent" > "$H/.claude/agents/a.md"
echo "hook" > "$H/.claude/hooks/h.sh"
printf '{"env": {"FOO": "bar-%s"}, "model": "x"}\n' "$NAME" > "$H/.claude/settings.json"
printf '{"allowFrom": ["%s"]}\n' "$CHAT" > "$H/.claude/channels/telegram/access.json"
printf '{"key": "%s"}\n' "$SVALUE" > "$H/.secrets/svc.json"
echo "private memory about $NAME" > "$H/.claude/projects/-home-node-workspace/memory/m.md"
echo "# how I operate, chat $CHAT" > "$H/workspace/CLAUDE.md"
echo "- 2026-01-01: did things" > "$H/workspace/CHANGELOG.md"
echo "secret todo" > "$H/workspace/TODO.md"
echo "* * * * * true" > "$H/crontab"
echo "#!/bin/sh" > "$H/boot.sh"
printf '<html><script id="dash-data" type="application/json" data-mode="fallback">{"todo":"%s"}</script></html>\n' "$NAME" > "$H/opt/dash/index.html"
echo '{"private": 1}' > "$H/opt/dash/data.json"
echo "fixture for $NAME" > "$H/opt/demo-tests/fixtures/f.txt"
printf 'shop page %s\n' "$NAME" | gzip -n > "$H/opt/demo-tests/fixtures/page.html.gz"
printf '\x89PNG\r\n\x1a\nfake' > "$H/opt/demo-tests/fixtures/pic.png"
echo "x" > "$H/opt/demo-tests/data.env"
ln -s /etc/passwd "$H/opt/demo-tests/outside-link"
# ~/plugins comes from the harness repo (~/opt/assistant plugins/, deployed byte for byte): its tracked names say
# which live files are exported; a live-only file (generated, e.g. typings) stays out
printf 'node_modules/\n' > "$H/plugins/.gitignore"; echo "plugin $NAME" > "$H/plugins/p1/server.ts"
echo junk > "$H/plugins/p1/node_modules/x/i.js"; echo '{}' > "$H/plugins/p1/tsconfig.json"
A=$H/opt/assistant; mkdir -p "$A/plugins/p1"
cp "$H/plugins/.gitignore" "$A/plugins/"; echo "plugin" > "$A/plugins/p1/server.ts"
git -C "$A" init -q && git -C "$A" add -A && git -C "$A" -c user.name=t -c user.email=t@t commit -qm init
git init -q --bare --initial-branch=main "$R"
mkdir -p "$X" && git -C "$X" init -q --initial-branch=main && git -C "$X" remote add origin "$R"
cat > "$T/scrub.txt" <<EOF
# test scrub list
$NAME	PersonX
i:$EMAIL	owner@example.com
re:(?i)fakestr(?:asse|\.)?\s*7\b	Examplestrasse 1
re:\+41 79 98[7] 65 43	<phone>
EOF

# work copies (edit on a copy + atomic mv): a dot-copy in bin, *.new / *.tmp anywhere; plus a WIP file an
# older sync already swept into the export (unify S7b: bin/.ha-audit.new): the next sync must drop it
echo "#!/bin/sh" > "$H/bin/.tool.new"; echo "#!/bin/sh" > "$H/bin/.tool.edit"; echo "#!/bin/sh" > "$H/bin/tool.tmp"
echo "draft" > "$H/.claude/skills/demo/SKILL.md.new"
mkdir -p "$X/bin" && echo old > "$X/bin/.swept.new" && echo "#!/bin/sh" > "$H/bin/.swept.new"   # still live

# 1. first sync: replaced, excluded, pushed
out=$("$CS" 2>&1); rc=$?
ok '[ $rc = 0 ]' "first sync exit 0 (got $rc: $out)"
ok '[ ! -e "$X/bin/.tool.new" ] && [ ! -e "$X/bin/.tool.edit" ] && [ ! -e "$X/bin/tool.tmp" ] && [ ! -e "$X/claude/skills/demo/SKILL.md.new" ]' "work copies (bin dot-files, *.new, *.tmp) not exported"
ok '[ ! -e "$X/bin/.swept.new" ] && ! git --git-dir="$R" show main:bin/.swept.new >/dev/null 2>&1' "a work copy already in the export is dropped by the next sync"
ok '! echo "$out" | grep -q "tool.new\|tool.tmp\|tool.edit"' "work copies never reported as would-add"
ok '[ "$(rhead)" != none ]' "pushed to the remote"
ok 'grep -q "PersonX" "$X/bin/tool" && ! grep -q "$NAME" "$X/bin/tool"' "name replaced"
ok 'grep -q "owner@example.com" "$X/bin/tool" && ! grep -qi "$EMAIL" "$X/bin/tool"' "email replaced"
ok 'grep -q "<phone>" "$X/bin/tool" && ! grep -q "987 65 43" "$X/bin/tool"' "phone replaced"
ok 'grep -q 100000001 "$X/bin/tool" && ! grep -rq "$CHAT" "$X" --exclude-dir=.git' "chat id from access.json replaced everywhere"
ok 'grep -q "Examplestrasse 1" "$X/bin/tool" && ! grep -q "Fakestrasse" "$X/bin/tool"' "street replaced"
ok 'grep -q "$NAME" "$H/bin/tool" && grep -q "$EMAIL" "$H/bin/tool"' "original untouched"
ok '[ -x "$X/bin/tool" ]' "exec bit kept"
ok 'grep -q PersonX "$X/claude/skills/demo/SKILL.md" && [ -f "$X/claude/agents/a.md" ] && [ -f "$X/claude/hooks/h.sh" ]' "skills/agents/hooks exported + scrubbed"
ok '[ "$(python3 -c "import json;print(json.load(open(\"$X/claude/settings.json\"))[\"env\"][\"FOO\"])")" = "<redacted>" ]' "settings env redacted"
ok 'grep -q "data-mode=\"fallback\">{}</script>" "$X/dash/index.html" && [ ! -e "$X/dash/data.json" ]' "dashboard data emptied, data.json not exported"
ok '[ -f "$X/workspace/CLAUDE.md" ] && [ ! -e "$X/workspace/TODO.md" ]' "workspace: CLAUDE.md in, TODO.md out"
ok '! grep -rq "private memory" "$X" --exclude-dir=.git && [ ! -e "$X/.secrets" ] && ! grep -rqF "$SVALUE" "$X" --exclude-dir=.git' "memory and .secrets not exported"
ok '[ ! -e "$X/claude/channels" ] && ! find "$X" -name access.json | grep -q .' "access.json not exported"
ok '[ -f "$X/tests/demo/fixtures/f.txt" ] && [ ! -e "$X/tests/demo/fixtures/pic.png" ] && [ ! -e "$X/tests/demo/data.env" ]' "tests: text in, png and *.env out"
ok '[ ! -e "$X/tests/demo/outside-link" ]' "symlink leaving its dir dropped"
ok '[ "$(gzip -dc "$X/tests/demo/fixtures/page.html.gz")" = "shop page PersonX" ]' ".gz rewritten and scrubbed"
ok 'grep -q PersonX "$X/plugins/p1/server.ts" && [ -f "$X/plugins/.gitignore" ] && [ ! -e "$X/plugins/p1/node_modules" ]' "plugins: the repo's plugin files in (live copy, scrubbed), node_modules out"
ok '[ ! -e "$X/plugins/p1/tsconfig.json" ]' "plugins: a live-only file the repo does not track stays out"
ok '[ -x "$X/.git/hooks/pre-commit" ]' "secretscan pre-commit hook installed"
h1=$(rhead)

# 2. idempotent + repo-owned README kept
"$CS" >/dev/null 2>&1
ok '[ "$(rhead)" = "$h1" ]' "second sync: no new commit"
echo "# readme" > "$X/README.md"
"$CS" >/dev/null 2>&1; "$CS" >/dev/null 2>&1
ok '[ -f "$X/README.md" ] && git --git-dir="$R" show main:README.md | grep -q readme' "README.md kept and pushed"
h2=$(rhead)

# 3. unlisted PII (phone not in the scrub list): blocked, nothing pushed, note without the value
echo "call $PHONE2" > "$H/bin/tool2"
out=$("$CS" --cron 2>&1); rc=$?
ok '[ $rc = 3 ]' "unlisted phone -> exit 3 (got $rc)"
ok '[ "$(rhead)" = "$h2" ]' "unlisted phone -> nothing pushed"
ok '[ ! -e "$X/bin/tool2" ]' "unlisted phone -> export work tree untouched"
ok '[ "$(notes)" = 0 ] && [ -s "$PB" ]' "first cron block -> no note yet, pending state kept"
"$CS" --cron >/dev/null 2>&1; rc=$?
ok '[ $rc = 3 ] && [ "$(notes)" = 0 ]' "same block again within 30 min -> still no note (got $rc)"
age; "$CS" --cron >/dev/null 2>&1; rc=$?
ok '[ $rc = 3 ] && [ "$(notes)" = 1 ]' "same block, next cron run >= 30 min later -> note (got $rc)"
ok '[ "$(rhead)" = "$h2" ]' "persisting block -> still nothing pushed"
ok '! grep -rq "222 33 44" "$T/inbox"' "note never carries the value"
n1=$(ls "$T"/inbox | wc -l); "$CS" --cron >/dev/null 2>&1
ok '[ "$(ls "$T"/inbox | wc -l)" = "$n1" ]' "same problem again -> no second note"
echo "allow:$PHONE2" >> "$T/scrub.txt"
"$CS" >/dev/null 2>&1
ok '[ "$(rhead)" != "$h2" ] && grep -q "222 33 44" "$X/bin/tool2"' "allow: line lets a known fake through"
h3=$(rhead)

# 4. a token: push refused
echo "BOT_TOKEN=$TOKEN" > "$H/bin/tool3"
out=$("$CS" --cron 2>&1); rc=$?
ok '[ $rc = 3 ]' "fake bot token -> exit 3 (got $rc)"
ok '[ "$(rhead)" = "$h3" ]' "fake bot token -> push refused"
ok '! grep -rqF "$TOKEN" "$X" "$T/inbox" "$T/state"' "token nowhere in export, note or state"
rm -f "$H/bin/tool3"

# 5. one of my real secret values (by value, from SECRETSCAN_SOURCES/.secrets): push refused
echo "v=$SVALUE" > "$H/bin/tool4"
"$CS" >/dev/null 2>&1; rc=$?
ok '[ $rc = 3 ] && [ "$(rhead)" = "$h3" ]' "secret value -> push refused (got $rc)"
rm -f "$H/bin/tool4"

# 6. check mode never commits; missing scrub list refuses to run
echo "new" > "$H/bin/tool5"
"$CS" check >/dev/null 2>&1; rc=$?
ok '[ $rc = 0 ] && [ ! -e "$X/bin/tool5" ] && [ "$(rhead)" = "$h3" ]' "check mode: clean, nothing written"
CODEBASE_SYNC_SCRUB=$T/missing "$CS" >/dev/null 2>&1; rc=$?
ok '[ $rc = 1 ] && [ "$(rhead)" = "$h3" ]' "no scrub list -> refuses (got $rc)"

# 7. publish manifest, report mode (default): export unchanged; report lists what enforce would do
mkdir -p "$H/plugins/p1/ops" "$X/docs" "$X/tools" "$X/.github/workflows"
echo "gate" > "$H/plugins/p1/ops/hb"; mkdir -p "$A/plugins/p1/ops" && cp "$H/plugins/p1/ops/hb" "$A/plugins/p1/ops/hb"
git -C "$A" add -A && git -C "$A" -c user.name=t -c user.email=t@t commit -qm hb
ln -s ../plugins/p1/ops/hb "$H/bin/hb"                       # leaves bin/, but maps onto an exported file
echo '{"type": "user", "text": "synthetic"}' > "$H/opt/demo-tests/fixtures/s.jsonl"
echo '{"x": 1}' > "$H/opt/demo-tests/other.jsonl"
echo "# guide" > "$X/docs/guide.md"; echo "echo t" > "$X/tools/t.sh"; echo "name: ci" > "$X/.github/workflows/ci.yml"
echo "license" > "$X/LICENSE"
h4=$(rhead)
out=$("$CS" 2>&1); rc=$?
ok '[ $rc = 0 ] && [ "$(rhead)" != "$h4" ]' "report mode: sync ok (got $rc: $out)"
ok '[ -f "$X/docs/guide.md" ] && [ -f "$X/tools/t.sh" ] && [ -f "$X/.github/workflows/ci.yml" ] && [ -f "$X/LICENSE" ]' "repo-owned dirs and files kept across a sync"
ok 'git --git-dir="$R" show main:docs/guide.md | grep -q guide && git --git-dir="$R" show main:.github/workflows/ci.yml | grep -q ci' "repo-owned files pushed"
ok '[ ! -e "$X/tests/demo/fixtures/s.jsonl" ] && [ ! -L "$X/bin/hb" ] && [ -f "$X/plugins/p1/ops/hb" ]' "report mode: jsonl fixture and cross-dir link still left out"
mout=$("$CS" manifest 2>&1); rc=$?
ok '[ $rc = 0 ] && echo "$mout" | grep -q "would block  bin/tool " && echo "$mout" | grep -q "would block  claude/skills/demo/SKILL.md"' "manifest: public files with a term hit listed (got $rc)"
ok '! echo "$mout" | grep -q "would block  workspace/" ' "manifest: private paths are scrubbed, never listed as blocking"
ok 'echo "$mout" | grep -q "would add    tests/demo/fixtures/s.jsonl" && ! echo "$mout" | grep -q other.jsonl' "manifest: only tests/*/fixtures/*.jsonl would be added"
ok 'echo "$mout" | grep -q "would keep   bin/hb (symlink)" && ! echo "$mout" | grep -q outside-link' "manifest: mapped link would be kept, outside link not"
ok '! echo "$mout" | grep -q "$NAME" && ! grep -rq "$NAME" "$T/state"' "manifest report and state never carry a value"
ok 'python3 -c "import json,sys; m=json.load(open(\"$T/state/last-run.json\"))[\"manifest\"]; assert m[\"mode\"]==\"report\" and m[\"private\"]>=2 and m[\"would_block\"]>=4"' "state records the manifest counts"
echo "# doc for $NAME" > "$X/docs/leak.md"
"$CS" >/dev/null 2>&1; rc=$?
ok '[ $rc = 3 ] && ! git --git-dir="$R" cat-file -e main:docs/leak.md 2>/dev/null' "repo-owned file with a term is checked: blocked, not pushed (got $rc)"
rm -f "$X/docs/leak.md"

# 8. enforce mode: public paths verbatim, a term hit blocks; private paths still scrubbed
h5=$(rhead)
out=$(CODEBASE_SYNC_MODE=enforce "$CS" --cron 2>&1); rc=$?
ok '[ $rc = 3 ] && [ "$(rhead)" = "$h5" ]' "enforce: term in a public path -> exit 3, nothing pushed (got $rc)"
ok '! grep -rq "$NAME" "$X" --exclude-dir=.git && ! grep -rq "$NAME" "$T/inbox" "$T/state"' "enforce: blocked value reaches neither export nor note"
printf '#!/bin/sh\necho hi\n' > "$H/bin/tool"
for f in "$H/.claude/skills/demo/SKILL.md" "$H/opt/demo-tests/fixtures/f.txt" "$H/plugins/p1/server.ts"; do sed -i "s/$NAME/PersonX/" "$f"; done
printf 'shop page PersonX\n' | gzip -n > "$H/opt/demo-tests/fixtures/page.html.gz"
out=$(CODEBASE_SYNC_MODE=enforce "$CS" 2>&1); rc=$?
ok '[ $rc = 0 ] && [ "$(rhead)" != "$h5" ]' "enforce: clean public paths -> synced (got $rc: $out)"
ok 'grep -q 100000001 "$X/workspace/CLAUDE.md" && ! grep -q "$CHAT" "$X/workspace/CLAUDE.md"' "enforce: private path still scrubbed"
ok '[ -f "$X/tests/demo/fixtures/s.jsonl" ] && [ ! -e "$X/tests/demo/other.jsonl" ]' "enforce: fixture jsonl exported, other jsonl not"
ok '[ -L "$X/bin/hb" ] && [ "$(readlink "$X/bin/hb")" = ../plugins/p1/ops/hb ] && [ ! -e "$X/tests/demo/outside-link" ]' "enforce: mapped symlink kept, outside link dropped"
printf 'page %s\n' "$NAME" | gzip -n > "$H/opt/demo-tests/fixtures/p2.html.gz"
CODEBASE_SYNC_MODE=enforce "$CS" >/dev/null 2>&1; rc=$?
ok '[ $rc = 3 ] && [ ! -e "$X/tests/demo/fixtures/p2.html.gz" ]' "enforce: public .gz with a hit blocks instead of a silent drop (got $rc)"
rm -f "$H/opt/demo-tests/fixtures/p2.html.gz"

# 9. enforce mode + public-path gate (~/.config/codebase-sync/gate): the staged tree must pass it before the commit
cat > "$T/fakegate" <<'EOF'
#!/bin/sh
d=$(dirname "$0"); printf '%s\n' "$*" > "$d/gate-args"
[ -e "$d/gate-block" ] || exit 0
echo "  gate denylist (1 private rules): 1 hits"; echo "    bin/tool:2 rule#7"; echo "BLOCKED (1)"; exit 3
EOF
chmod +x "$T/fakegate"; printf '# gate command\n%s gate demo\n' "$T/fakegate" > "$T/gate.conf"; touch "$T/gate-block"
h9=$(rhead); x9=$(git -C "$X" rev-parse HEAD); rm -rf "$T/inbox"; printf 'echo two\n' >> "$H/bin/tool"
CODEBASE_SYNC_MODE=enforce "$CS" --cron >/dev/null 2>&1; rc=$?
ok '[ $rc = 3 ] && [ "$(notes)" = 0 ] && grep -q gate "$PB"' "first gate block -> no note yet, pending (got $rc)"
age; out=$(CODEBASE_SYNC_MODE=enforce "$CS" --cron 2>&1); rc=$?
ok '[ $rc = 3 ] && [ "$(rhead)" = "$h9" ] && [ "$(git -C "$X" rev-parse HEAD)" = "$x9" ]' "gate blocks: exit 3, nothing committed or pushed (got $rc: $out)"
ok 'grep -Eq "^gate demo --src $X --rev [0-9a-f]{40}\$" "$T/gate-args"' "gate called with --src EXPORT --rev TREE"
ok '[ -z "$(git -C "$X" diff --cached --name-only)" ]' "gate block leaves nothing staged"
ok 'grep -q "public-path gate blocked it" "$T"/inbox/codebase-sync-*.md && grep -q "bin/tool:2 rule#7" "$T"/inbox/codebase-sync-*.md' "gate block -> inbox note with file:line + rule label"
ok 'python3 -c "import json; assert \"gate\" in json.load(open(\"$T/state/last-run.json\"))[\"result\"]"' "state records the gate block"
CODEBASE_SYNC_MODE=report "$CS" check >/dev/null 2>&1; rc=$?
ok '[ $rc = 0 ]' "gate is enforce-only: report-mode check unaffected (got $rc)"
rm -f "$T/gate-block"
out=$(CODEBASE_SYNC_MODE=enforce "$CS" 2>&1); rc=$?
ok '[ $rc = 0 ] && [ "$(rhead)" != "$h9" ]' "gate passes -> committed and pushed (got $rc: $out)"
printf '%s gate demo\n' "$T/no-such-gate" > "$T/gate.conf"; h9=$(rhead); printf 'echo three\n' >> "$H/bin/tool"
out=$(CODEBASE_SYNC_MODE=enforce "$CS" 2>&1); rc=$?
ok '[ $rc = 3 ] && [ "$(rhead)" = "$h9" ] && echo "$out" | grep -q "could not run"' "gate that cannot run blocks too (got $rc)"
printf '# only a comment\n' > "$T/gate.conf"
out=$(CODEBASE_SYNC_MODE=enforce "$CS" 2>&1); rc=$?
ok '[ $rc = 3 ] && echo "$out" | grep -q "no gate command"' "gate config without a command blocks (got $rc)"
rm -f "$T/gate.conf"
out=$(CODEBASE_SYNC_MODE=enforce "$CS" 2>&1); rc=$?
ok '[ $rc = 0 ] && [ "$(rhead)" != "$h9" ]' "no gate config -> no gate (got $rc)"
# 10. precheck: check + the gate on a throwaway tree; no lock, no state, no note, the export repo untouched
cat > "$T/pgate" <<'EOF'
#!/bin/bash
# fake gate: records its args and the files of the tree it was given; blocks while pgate-block exists
d=$(dirname "$0"); printf '%s\n' "$*" > "$d/pgate-args"
git -C "$4" ls-tree -r --name-only "$6" > "$d/pgate-files" 2>&1
[ -e "$d/pgate-block" ] || { echo "demo @tree: all fast gates passed"; exit 0; }
echo "  gate denylist (1 private rules): 1 hits"; echo "    bin/tool:2 rule#7"; echo "BLOCKED (1)"; exit 3
EOF
chmod +x "$T/pgate"; printf '%s gate demo\n' "$T/pgate" > "$T/gate.conf"; touch "$T/pgate-block"
x10=$(git -C "$X" rev-parse HEAD); h10=$(rhead); rm -rf "$T/inbox"; st10=$(md5sum < "$T/state/last-run.json")
printf 'echo four\n' >> "$H/bin/tool"
out=$(CODEBASE_SYNC_MODE=enforce "$CS" precheck --cron 2>&1); rc=$?
ok '[ $rc = 3 ] && echo "$out" | grep -q "^  bin/tool:2 rule#7$" && echo "$out" | grep -q "WOULD BLOCK"' "precheck: gate block -> exit 3 + file:line rule label (got $rc: $out)"
ok 'grep -Eq "^gate demo --src /[^ ]+ --rev [0-9a-f]{40}\$" "$T/pgate-args" && ! grep -q -- "--src $X " "$T/pgate-args"' "precheck: gate gets a throwaway repo, never the export"
ok 'grep -qx "bin/tool" "$T/pgate-files"' "precheck: the throwaway tree holds the staged files"
ok '[ ! -e "$T/inbox" ] || [ -z "$(ls "$T/inbox")" ]' "precheck: never writes an inbox note (even with --cron)"
ok '[ "$(git -C "$X" rev-parse HEAD)" = "$x10" ] && [ "$(rhead)" = "$h10" ] && [ -z "$(git -C "$X" status --porcelain)" ]' "precheck: export repo and remote untouched"
ok '[ "$(md5sum < "$T/state/last-run.json")" = "$st10" ]' "precheck: state file untouched"
ok '! find /tmp -maxdepth 1 -name "codebase-precheck-*" -user "$(id -u)" -newer "$T/pgate" 2>/dev/null | grep -q .' "precheck: throwaway repo removed"
rm -f "$T/pgate-block"
out=$(CODEBASE_SYNC_MODE=enforce "$CS" precheck 2>&1); rc=$?
ok '[ $rc = 0 ] && echo "$out" | grep -q "clean .*gate passed"' "precheck: gate passes -> exit 0 (got $rc: $out)"
rm -f "$T/pgate-args"
out=$(CODEBASE_SYNC_MODE=report "$CS" precheck 2>&1); rc=$?
ok '[ $rc = 0 ] && [ ! -e "$T/pgate-args" ]' "precheck: report mode -> no gate, like a sync (got $rc)"
printf '%s gate demo\n' "$T/no-such-gate" > "$T/gate.conf"
out=$(CODEBASE_SYNC_MODE=enforce "$CS" precheck 2>&1); rc=$?
ok '[ $rc = 1 ] && echo "$out" | grep -q "could not run"' "precheck: gate that cannot run -> exit 1 (got $rc: $out)"
rm -f "$T/gate.conf"
out=$(CODEBASE_SYNC_MODE=enforce "$CS" precheck 2>&1); rc=$?
ok '[ $rc = 0 ] && echo "$out" | grep -q "not configured"' "precheck: no gate config -> check only (got $rc)"
printf 'echo %s\n' "$EMAIL" >> "$H/bin/tool"
out=$(CODEBASE_SYNC_MODE=enforce "$CS" precheck 2>&1); rc=$?
ok '[ $rc = 3 ] && echo "$out" | grep -Eq "^  bin/tool:[0-9]+ " && ! echo "$out" | grep -qF "$EMAIL"' "precheck: a check hit -> exit 3, file:line label, never the value (got $rc)"
sed -i '$d' "$H/bin/tool"
CODEBASE_SYNC_MODE=bogus "$CS" check >/dev/null 2>&1; rc=$?
ok '[ $rc = 1 ]' "unknown mode -> exit 1 (got $rc)"

# 11. --cron block debounce: same hit set (file:line:rule) over 2 runs >= 30 min apart; a clean run clears it;
#     other hits restart the clock; manual runs report at once (stderr); --recheck runs only while pending
rm -rf "$T/inbox"; rm -f "$PB"
out=$("$CS" --cron --recheck 2>&1); rc=$?; st11=$(md5sum < "$T/state/last-run.json")
ok '[ $rc = 0 ] && [ -z "$out" ] && [ ! -e "$PB" ]' "recheck with nothing pending -> exit 0, no run (got $rc)"
PHONE3="079 ""333 44 55"; PHONE4="079 ""444 55 66"
echo "call $PHONE3" > "$H/bin/tool5"
out=$("$CS" 2>&1); rc=$?
ok '[ $rc = 3 ] && echo "$out" | grep -q "BLOCKED" && [ ! -e "$PB" ] && [ "$(notes)" = 0 ]' "manual run: block reported on stderr at once, no pending state, no note (got $rc)"
"$CS" --cron >/dev/null 2>&1; rc=$?
ok '[ $rc = 3 ] && [ "$(notes)" = 0 ] && [ -s "$PB" ]' "first cron block -> no note (got $rc)"
ok '! grep -q "333 44 55" "$PB"' "pending state never carries the value"
rm -f "$H/bin/tool5"; h11=$(rhead)
"$CS" --cron >/dev/null 2>&1; rc=$?
ok '[ $rc = 0 ] && [ ! -e "$PB" ] && [ "$(notes)" = 0 ]' "block then clean -> no note, pending cleared (got $rc)"
echo "call $PHONE3" > "$H/bin/tool5"; "$CS" --cron >/dev/null 2>&1; age
"$CS" --cron --recheck >/dev/null 2>&1; rc=$?
ok '[ $rc = 3 ] && [ "$(notes)" = 1 ]' "pending block, recheck >= 30 min later -> note (got $rc)"
rm -rf "$T/inbox"; rm -f "$PB"
"$CS" --cron >/dev/null 2>&1; age; echo "call $PHONE4" >> "$H/bin/tool5"
"$CS" --cron >/dev/null 2>&1; rc=$?
ok '[ $rc = 3 ] && [ "$(notes)" = 0 ]' "changed hit set -> clock restarts, no note (got $rc)"
age; "$CS" --cron >/dev/null 2>&1
ok '[ "$(notes)" = 1 ]' "changed hit set persists >= 30 min -> note"
f1=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["first"])' "$PB")
sed -i 's/^call /ring /' "$H/bin/tool5"; "$CS" --cron >/dev/null 2>&1
f2=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["first"])' "$PB")
ok '[ "$f1" = "$f2" ]' "same file:line:rule, other text -> same block (clock kept)"
echo "x" > "$H/bin/tool6"; mv "$H/bin/tool5" "$H/bin/tool6"; "$CS" --cron >/dev/null 2>&1
f3=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["first"])' "$PB")
ok '[ "$f3" != "$f1" ]' "same values in another file -> other hit set (clock restarts)"
rm -f "$H/bin/tool6"; "$CS" --cron >/dev/null 2>&1; rc=$?
ok '[ $rc = 0 ] && [ ! -e "$PB" ] && [ "$(rhead)" != "$h11" ]' "clean again -> pushed, pending cleared (got $rc)"

# 12. the harness repo ~/opt/assistant has its own GitHub repo (assistant-publish): only a pointer README under
#     assistant/, never its code (tracked, untracked or a worktree's); the deployed core.releases copies stay out
mkdir -p "$A/core" "$H/bin/lib/core.releases/r1/core"
( cd "$A" && git init -q && echo 'X = 1' > core/paths.py && git add -A \
  && git -c user.name=t -c user.email=t@t commit -qm init )
echo "owner $NAME" > "$A/core/new.py"; echo 'X = 1' > "$H/bin/lib/core.releases/r1/core/paths.py"
out=$(CODEBASE_SYNC_ASSISTANT_SRC=$A CODEBASE_SYNC_MODE=enforce "$CS" 2>&1); rc=$?
ok '[ $rc = 0 ] && [ -f "$X/assistant/README.md" ] && [ "$(ls "$X/assistant")" = README.md ]' "assistant: only the pointer README is exported (got $rc: $out)"
ok 'grep -q "assistant-publish" "$X/assistant/README.md" && [ ! -e "$X/bin/lib/core.releases" ]' "assistant: pointer names assistant-publish; deployed core.releases left out"
rm -f "$A/core/new.py"

echo "codebase-sync tests: $pass passed, $fail failed"
[ $fail = 0 ]
