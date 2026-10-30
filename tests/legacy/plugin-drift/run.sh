#!/usr/bin/env bash
# Tests for ~/bin/plugin-drift (offline, synthetic marketplace + cache in a temp dir; PLUGIN_DRIFT_BIN=/copy tests a copy).
set -uo pipefail
B=${PLUGIN_DRIFT_BIN:-$(cd "$(dirname "$0")/../../../scripts" && pwd)/plugin-drift}
T=$(mktemp -d "${TMPDIR:-/tmp}/plugin-drift-tests.XXXXXX")
trap 'rm -rf "$T"' EXIT
fail=0; ok() { echo "ok   $1"; }; no() { echo "FAIL $1"; fail=1; }
S=$T/src C=$T/cache
mkdir -p "$S/.claude-plugin" "$S/alpha/.claude-plugin" "$S/alpha/node_modules/x" "$S/beta/.claude-plugin" \
         "$C/alpha/1.0.0/.claude-plugin" "$C/beta/1.0.0/.claude-plugin"
cat > "$S/.claude-plugin/marketplace.json" <<J
{"name":"mk","plugins":[{"name":"alpha","source":"./alpha"},{"name":"beta","source":"./beta"}]}
J
for p in alpha beta; do
  echo '{"name":"'$p'","version":"1.0.0"}' > "$S/$p/.claude-plugin/plugin.json"
  cp "$S/$p/.claude-plugin/plugin.json" "$C/$p/1.0.0/.claude-plugin/plugin.json"
  printf 'a\nb\nc\n' > "$S/$p/main.ts"; cp "$S/$p/main.ts" "$C/$p/1.0.0/main.ts"
done
cat > "$T/installed.json" <<J
{"version":2,"plugins":{"alpha@mk":[{"installPath":"$C/alpha/1.0.0","version":"1.0.0"}],
 "beta@mk":[{"installPath":"$C/beta/1.0.0","version":"1.0.0"}]}}
J
echo 'TOKEN=SECRET-XYZZY' > "$S/alpha/.env"            # must never be listed or read
echo 'junk' > "$S/alpha/node_modules/x/i.js"           # skipped
# the harness repo: plugins/ on its main is what deploys byte for byte to the live folder ($S)
G=$T/repo; mkdir -p "$G/plugins"
printf 'node_modules/\ntsconfig.json\n' > "$S/.gitignore"
(cd "$S" && tar --exclude=node_modules --exclude=.env -cf - .) | tar -xf - -C "$G/plugins"
git -C "$G" init -q -b main && git -C "$G" add -A && git -C "$G" -c user.name=t -c user.email=t@t commit -qm init
echo "{}" | tee "$S/alpha/tsconfig.json" > "$C/alpha/1.0.0/tsconfig.json"   # ignored by the repo: never listed
export PLUGIN_DRIFT_SRC=$S PLUGIN_DRIFT_REPO=$G PLUGIN_DRIFT_INSTALLED=$T/installed.json PLUGIN_DRIFT_SINCE=0 \
       PLUGIN_DRIFT_DEPLOYS=$T/deploys.jsonl
run() { out=$("$B" "$@" 2>&1); rc=$?; }

PLUGIN_DRIFT_ROOTS="alpha=$C/alpha/1.0.0" run
[ $rc = 0 ] && ok "in sync: exit 0" || no "in sync: exit $rc"
grep -q 'beta: loads from cache (inferred)' <<<"$out" && ok "mode inferred from same marketplace" || no "inferred: $out"
grep -q -e '.env' -e XYZZY -e node_modules <<<"$out" && no ".env/node_modules leaked" || ok ".env and node_modules skipped"
grep -q -e "not as on the repo" -e 'not compared' <<<"$out" && no "repo in sync but listed: $out" || ok "live = repo main: nothing listed (ignored file left out)"

printf 'a\nB\nc\nd\n' > "$S/alpha/main.ts"; echo new > "$S/beta/new.ts"
PLUGIN_DRIFT_ROOTS="alpha=$C/alpha/1.0.0" run
[ $rc = 1 ] && ok "cache mode + drift: exit 1" || no "cache drift exit $rc"
grep -q 'main.ts  +2 -1' <<<"$out" && ok "numstat" || no "numstat: $out"
grep -q 'new.ts  only in source' <<<"$out" && ok "new file listed" || no "new file: $out"
grep -q 'claude plugin update alpha@mk' <<<"$out" && ok "fix hint" || no "fix hint"
grep -q "alpha: .*" <<<"$out" && grep -q "not as on the repo's main (1): main.ts (edited live)" <<<"$out" \
  && grep -q "not as on the repo's main (1): new.ts (live only)" <<<"$out" && ok "repo compare: edited live, live only" \
  || no "repo compare: $out"
mv "$S/beta/main.ts" "$T/beta-main.ts"; PLUGIN_DRIFT_ROOTS="alpha=$C/alpha/1.0.0" run
grep -q "main.ts (missing live), new.ts (live only)" <<<"$out" && ok "repo compare: missing live" || no "missing live: $out"
mv "$T/beta-main.ts" "$S/beta/main.ts"
PLUGIN_DRIFT_REPO=$T/norepo PLUGIN_DRIFT_ROOTS="alpha=$C/alpha/1.0.0" run
[ $rc = 1 ] && grep -q 'not compared with the repo' <<<"$out" && ok "no repo: said, exit unchanged" || no "no repo: rc=$rc $out"

PLUGIN_DRIFT_ROOTS="alpha=$S/alpha" run
[ $rc = 0 ] && ok "source mode: drift is info only" || no "source mode exit $rc"
grep -q 'cache copy stale' <<<"$out" && ok "stale cache noted" || no "stale note: $out"
PLUGIN_DRIFT_ROOTS="alpha=$S/alpha" run --strict
[ $rc = 1 ] && ok "--strict flags stale cache" || no "strict exit $rc"
PLUGIN_DRIFT_ROOTS="alpha=$S/alpha" run --quiet
[ $rc = 0 ] && [ -z "$out" ] && ok "--quiet silent when fine" || no "quiet: rc=$rc $out"

PLUGIN_DRIFT_ROOTS=none run
[ $rc = 1 ] && grep -q 'loads from unknown' <<<"$out" && ok "no process: unknown + drift = exit 1" || no "unknown: rc=$rc"

touch -d '2000-01-01' "$S/alpha/main.ts"; PLUGIN_DRIFT_SINCE=$(date -d '2001-01-01' +%s) PLUGIN_DRIFT_ROOTS="alpha=$S/alpha" run --json
python3 -c "import json,sys;d=json.loads(sys.argv[1]);a,b=d['plugins'];assert 'main.ts' not in a['pending'];assert 'new.ts' in b['pending']" "$out" \
  && ok "pending = changed since session start (json)" || no "pending json"

# by content: the commit deployed at session start (deploys.jsonl); a deploy rewrites files, so a fresh file time
# with the same bytes is not pending, while an old file time with other bytes is
since=$(date -d '2001-01-01' +%s); touch "$S/beta/main.ts"
printf '{"ok": false, "sha": "bad", "ts": %s}\n{"ok": true, "sha": "%s", "ts": %s}\n{"ok": true, "sha": "later", "ts": %s}\n' \
  $((since - 5)) "$(git -C "$G" rev-parse main)" $((since - 10)) $((since + 10)) > "$T/deploys.jsonl"
PLUGIN_DRIFT_SINCE=$since PLUGIN_DRIFT_ROOTS="alpha=$S/alpha" run --json
python3 -c "import json,sys;d=json.loads(sys.argv[1]);a,b=d['plugins'];assert a['pending_by']=='repo' and a['pending']==['main.ts'] and b['pending']==['new.ts'], (a['pending'], b['pending'])" "$out" \
  && ok "pending by content against the session's deploy" || no "pending by content: $out"
PLUGIN_DRIFT_SINCE=$since PLUGIN_DRIFT_ROOTS="alpha=$S/alpha" run
grep -q 'by file time' <<<"$out" && no "content mode said file time" || ok "content mode: no file-time note"
rm -f "$T/deploys.jsonl"; PLUGIN_DRIFT_SINCE=$since PLUGIN_DRIFT_ROOTS="alpha=$S/alpha" run
grep -q "pending at next restart (3, by file time: the session predates the repo's plugins/): .claude-plugin/plugin.json, main.ts, new.ts" <<<"$out" \
  && ok "no deploy record: file times, said" || no "file-time fallback: $out"

PLUGIN_DRIFT_INSTALLED=$T/missing.json run
[ $rc = 2 ] && ok "missing installed list: exit 2" || no "missing: exit $rc"
run --bogus; [ $rc = 2 ] && ok "bad flag: exit 2" || no "bad flag exit $rc"
exit $fail
