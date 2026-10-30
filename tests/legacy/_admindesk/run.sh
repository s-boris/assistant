#!/usr/bin/env bash
# Offline smoke tests for ~/bin/_admindesk.py (shared helpers of ~/bin/letter and ~/bin/pdfform). Exit 0 = all pass.
# Fakes for paperless (meta + upload) and remind log their argv; synthetic owner profile; fixed clock; temp dirs.
# Checks: date parsing/formatting/workdays, slug, sender layering, paperless meta parsing + upload command,
# reminder slot rules, signature cleanup, no sending code. letter-tests covers the CLIs end to end.
set -uo pipefail
_pf="$(cd "$(dirname "$0")/.." && pwd)"; [ -f "$_pf/profile/fixtures/profile.json" ] && _pf=$_pf/profile || _pf=$_pf/profile-tests
export ASSISTANT_PROFILE="$_pf/fixtures/profile.json"
HERE=$(cd "$(dirname "$0")" && pwd)
T=$(mktemp -d "${TMPDIR:-/tmp}/admindesk-tests.XXXXXX")
trap 'find "$T" -mindepth 1 -delete; rmdir "$T"' EXIT

cat > "$T/paperless" <<'EOF'
#!/usr/bin/env python3
import json, os, sys
with open(os.path.join(os.environ["ADT_DIR"], "paperless.log"), "a") as f:
    f.write(json.dumps({"argv": sys.argv[1:]}) + "\n")
if sys.argv[1:2] == ["meta"]:
    mode = os.environ.get("FAKE_META", "")
    if mode == "fail":
        print("vault: paperless unreachable", file=sys.stderr); sys.exit(1)
    tags = ["Rechnung"] if mode == "notag" else ["Korrespondenz", "Rechnung"]
    print(f"tags ({len(tags)}):"); [print(f"  {t}  [id {i}, 3 docs]") for i, t in enumerate(tags, 1)]
    print("correspondents (1):\n  Testkasse  [id 9]")
    print("document types (2):\n  Brief  [id 4]\n  Formular  [id 5]")
    sys.exit(0)
if sys.argv[1:2] == ["upload"]:
    rc = int(os.environ.get("FAKE_UPLOAD_RC", "0"))
    if rc:
        print("upload rejected", file=sys.stderr); sys.exit(rc)
    print("queued\ndocument 123: http://paperless.test/123"); sys.exit(0)
sys.exit(2)
EOF
cat > "$T/remind" <<'EOF'
#!/usr/bin/env python3
import json, os, sys
with open(os.path.join(os.environ["ADT_DIR"], "remind.log"), "a") as f:
    f.write(json.dumps({"argv": sys.argv[1:]}) + "\n")
rc = int(os.environ.get("FAKE_REMIND_RC", "0"))
print("remind: bad time" if rc else "reminder r1 set", file=sys.stderr if rc else sys.stdout)
sys.exit(rc)
EOF
chmod +x "$T/paperless" "$T/remind"
unset LETTER_SIGNATURE FAKE_META FAKE_UPLOAD_RC FAKE_REMIND_RC
export ADT_DIR=$T ADMIN_DESK_NOW="2026-10-10 11:00" ADMIN_DESK_PAPERLESS=$T/paperless ADMIN_DESK_REMIND=$T/remind \
       LETTER_CONFIG_DIR=$T/cfg KK_PROFILE=$T/kk-profile.json
timeout 120 python3 -I -B "$HERE/test_admindesk.py" </dev/null
