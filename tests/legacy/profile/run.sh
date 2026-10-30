#!/usr/bin/env bash
# Tests for bin/profile-get and bin/lib/assistant_profile.py (the owner's shared settings file).
# Offline; only the synthetic fixture and temp files. Run: bash tests/legacy/profile/run.sh (PROFILE_GET=<copy>).
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
PG=${PROFILE_GET:-$(cd "$HERE/../../../scripts" && pwd)/profile-get}   # repo: tests/legacy/profile -> scripts/
LIB=$(dirname "$(readlink -f "$PG")")/lib
FIX=$HERE/fixtures/profile.json
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
pass=0; fail=0
ok() { if eval "$1"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $2"; fi; }
sha() { printf '%s' "$1" | sha256sum | cut -d' ' -f1; }

# 1. no file: the public placeholder defaults
export ASSISTANT_PROFILE=$T/none.json
ok '[ "$("$PG" home.town)" = Exampletown ]' "no file -> default town"
ok '[ "$("$PG" network.lan_prefix)" = 198.51.100. ]' "no file -> default LAN prefix"
ok '[ "$("$PG" --source)" = defaults ]' "no file -> source defaults"
ok '"$PG" --check >/dev/null 2>&1' "no file -> --check ok"
ok '[ "$("$PG" interests.topics)" = "[]" ]' "empty list prints as []"

# 2. the fixture overrides every key
export ASSISTANT_PROFILE=$FIX
ok '[ "$("$PG" home.town)" = Testhausen ]' "fixture town"
ok '[ "$("$PG" network.hosts.web_vm)" = web@192.0.2.20 ]' "nested key"
ok '[ "$("$PG" owner.birth_year)" = 1985 ]' "number prints plain"
ok '[ "$("$PG" home.lat)" = 46.1 ]' "float prints plain"
ok '[ "$("$PG" interests.topics)" = "[\"robotics\",\"astronomy\"]" ]' "list prints as compact JSON"
ok '[ "$("$PG" --source)" = "$FIX" ]' "source is the fixture"
ok '"$PG" network | python3 -c "import json,sys; d=json.load(sys.stdin); assert d[\"server_name\"]==\"Testsrv\""' "object prints as JSON"

# 3. --hash: sha256 of exactly the printed text, never the value
h=$("$PG" home.town --hash)
ok '[ "$h" = "$(sha "Testhausen")" ]' "--hash = sha256 of the value"
ok '[ "$("$PG" --hash interests.events)" = "$(sha "[\"Testfest\"]")" ]' "--hash of a list = sha256 of its JSON"
ok '! "$PG" home.town --hash | grep -q Testhausen' "--hash never prints the value"

# 4. --keys lists names only
ok '"$PG" --keys | grep -qx network.hosts.web_vm && "$PG" --keys | grep -qx home.town' "--keys lists dotted names"
ok '! "$PG" --keys | grep -q Testhausen' "--keys never prints values"

# 5. partial file: other keys keep the defaults; unknown key exit 2
printf '{"home": {"town": "Elsewhere"}}\n' > "$T/p.json"; chmod 600 "$T/p.json"
export ASSISTANT_PROFILE=$T/p.json
ok '[ "$("$PG" home.town)" = Elsewhere ] && [ "$("$PG" home.street)" = "Examplestrasse 1" ]' "partial file merges onto defaults"
"$PG" home.nope >/dev/null 2>&1; rc=$?
ok '[ $rc = 2 ]' "unknown key -> exit 2 (got $rc)"
"$PG" >/dev/null 2>&1; rc=$?
ok '[ $rc = 2 ]' "no key -> usage, exit 2 (got $rc)"

# 6. broken files: exit 1, the message names the key, never the value
printf '{"home": {"town": ' > "$T/bad.json"
export ASSISTANT_PROFILE=$T/bad.json
out=$("$PG" home.town 2>&1); rc=$?
ok '[ $rc = 1 ]' "invalid JSON -> exit 1 (got $rc)"
printf '{"home": {"postcode": 8000, "town": "Secretville"}}\n' > "$T/type.json"
export ASSISTANT_PROFILE=$T/type.json
out=$("$PG" home.town 2>&1); rc=$?
ok '[ $rc = 1 ] && echo "$out" | grep -q home.postcode && ! echo "$out" | grep -q 8000' "wrong type -> exit 1, key named, value not shown"
printf '[1, 2]\n' > "$T/list.json"
ASSISTANT_PROFILE=$T/list.json "$PG" home.town >/dev/null 2>&1; rc=$?
ok '[ $rc = 1 ]' "top level not an object -> exit 1"

# 7. --check: unknown keys (names only) and file mode; free-form hosts are fine
printf '{"home": {"twon": "Typo"}, "network": {"hosts": {"nas": "192.0.2.30"}}}\n' > "$T/c.json"; chmod 644 "$T/c.json"
export ASSISTANT_PROFILE=$T/c.json
out=$("$PG" --check 2>&1); rc=$?
ok '[ $rc = 1 ] && echo "$out" | grep -q "mode 644"' "--check: group/world-readable file -> exit 1"
chmod 600 "$T/c.json"
out=$("$PG" --check 2>&1); rc=$?
ok '[ $rc = 0 ] && echo "$out" | grep -q "unknown key home.twon" && ! echo "$out" | grep -q "hosts.nas" && ! echo "$out" | grep -q Typo' "--check: unknown key named, hosts free-form, no values"
ok '[ "$("$PG" network.hosts.nas)" = 192.0.2.30 ] && [ "$("$PG" network.hosts.web_vm)" = "user@198.51.100.3" ]' "extra host merged next to the default one"

# 8. the library: default arg, KeyError, copies, reload on change, ProfileError
export ASSISTANT_PROFILE=$T/lib.json
printf '{"home": {"town": "One"}}\n' > "$T/lib.json"
ok 'python3 -I "$HERE/test_lib.py" "$LIB" "$T/lib.json"' "library: get/default/KeyError/copies/reload/ProfileError/tz fallback"
ok 'python3 -I -c "import sys; sys.path.insert(0, \"$LIB\"); import profile, assistant_profile; assert hasattr(profile, \"Profile\")"' "stdlib profile module not shadowed"

# 9. owner-zone lint: no hardcoded zone in bin, plugins or hooks code (allowed: lint_zone.py's ALLOW constants and
#    DATA tables, each with its reason; comments/docstrings, docs, tests; release snapshots skipped, the active
#    core scanned via bin/lib/core); first synthetic trees, then the real one
LZ=$T/lz; mkdir -p "$LZ/bin/lib" "$LZ/plugins/p/test" "$LZ/.claude/hooks"
printf 'FALLBACK_TZ = "Europe/Zurich"\n' > "$LZ/bin/lib/assistant_profile.py"
printf '#!/usr/bin/env python3\n"""Times in Europe/Zurich."""\nx = 1  # e.g. Europe/Zurich\n' > "$LZ/bin/good"
printf "expect(z).toBe('Europe/Zurich')\n" > "$LZ/plugins/p/test/z.test.ts"
ok 'python3 -I "$HERE/lint_zone.py" "$LZ" >/dev/null' "zone lint: helper constant, comments, docstrings, tests pass"
printf '#!/bin/bash\n# fine: Europe/Zurich\nexport TZ=${X:-Europe/Zurich}\n' > "$LZ/bin/badtool"
printf 'z = "Europe/Zurich"  # Europe/Zurich\n' > "$LZ/.claude/hooks/hk.py"
printf "const Z = 'America/New_York'\n" > "$LZ/plugins/p/x.ts"
out=$(python3 -I "$HERE/lint_zone.py" "$LZ" 2>&1); rc=$?
ok '[ $rc = 1 ] && echo "$out" | grep -q "bin/badtool:3" && echo "$out" | grep -q "hooks/hk.py:1" && echo "$out" | grep -q "plugins/p/x.ts:1" && ! echo "$out" | grep -q "badtool:2" && ! echo "$out" | grep -q "TZ="' "zone lint: code hits named (path:line, never the line)"
printf 'FALLBACK_TZ = "Europe/Zurich"\nOTHER = "Europe/Zurich"\n' > "$LZ/bin/lib/assistant_profile.py"
ok '! python3 -I "$HERE/lint_zone.py" "$LZ" >/dev/null' "zone lint: only the named constant is allowed in the helper"
# reference-data table and protocol constant: only the named literal, only the pinned zone
LZ2=$T/lz2; mkdir -p "$LZ2/bin/lib"
printf 'AIRPORTS = {\n    "AAA": ("Asia/Tokyo", "aaa"),\n}\nHOME_TZ = "Asia/Tokyo"\n' > "$LZ2/bin/lib/flights.py"
printf 'TZ = "Europe/Berlin"\n' > "$LZ2/bin/lib/trackid_shazam.py"
out=$(python3 -I "$HERE/lint_zone.py" "$LZ2" 2>&1); rc=$?
ok '[ $rc = 1 ] && echo "$out" | grep -q "flights.py:4" && ! echo "$out" | grep -q "flights.py:2" && ! echo "$out" | grep -q trackid_shazam' "zone lint: data table + pinned protocol constant allowed, a zone beside the table is not"
printf 'TZ = "Asia/Tokyo"\n' > "$LZ2/bin/lib/trackid_shazam.py"
ok 'python3 -I "$HERE/lint_zone.py" "$LZ2" 2>&1 | grep -q "trackid_shazam.py:1"' "zone lint: the protocol constant may hold only its pinned zone"
# release snapshots skipped; the active core scanned once, through its link
LZ3=$T/lz3; mkdir -p "$LZ3/bin/lib/core.releases/r1/core" "$LZ3/bin/lib/core.releases/r2/core"
printf 'tz = "Asia/Tokyo"\n' > "$LZ3/bin/lib/core.releases/r1/core/s.toml"
printf '# Asia/Tokyo\ntz = "Asia/Tokyo"\n' > "$LZ3/bin/lib/core.releases/r2/core/s.toml"
ln -s core.releases/r2/core "$LZ3/bin/lib/core"
out=$(python3 -I "$HERE/lint_zone.py" "$LZ3" 2>&1)
ok '[ "$out" != "${out#*bin/lib/core/s.toml:2}" ] && [ "$(echo "$out" | grep -c .)" = 1 ] && ! echo "$out" | grep -q releases' "zone lint: release snapshots skipped, the active core once via bin/lib/core"
ROOT=$(dirname "$(dirname "$(readlink -f "$PG")")")   # live: ~ ; repo: the checkout
out=$(python3 -I "$HERE/lint_zone.py" "$ROOT" 2>&1); rc=$?
ok '[ $rc = 0 ]' "owner-zone lint: no hardcoded zone in $ROOT bin/plugins/hooks${out:+ -- $out}"
# profile-get locale.tz never fails: an unknown zone or a broken profile prints the helper's fallback
printf '{"locale": {"tz": "Mars/Base"}}\n' > "$T/z.json"
ok '[ "$(ASSISTANT_PROFILE=$T/z.json "$PG" locale.tz)" = "$(python3 -I -c "import sys; sys.path.insert(0, \"$LIB\"); import assistant_profile as p; print(p.FALLBACK_TZ)")" ]' "profile-get locale.tz: unknown zone -> fallback"
ok 'ASSISTANT_PROFILE=$T/bad.json "$PG" locale.tz >/dev/null 2>&1' "profile-get locale.tz: broken profile -> still exit 0"

echo "profile tests: $pass passed, $fail failed"
[ $fail = 0 ]
