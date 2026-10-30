"""The early-exit pipe lint (ops/lint/pipe_early_exit.py): the shell suites under tests/ carry no pipe into grep -q
(or a status-tested head / grep -m) under pipefail, and the lint catches the shapes that raced: the old has/hasnt
helper, `check '… | grep -q …'` strings, `ls | grep -q`, a negated pipe, a pipe continued on the next line."""

from __future__ import annotations

import fnmatch
import importlib.util
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LEGACY = "tests/legacy/*"  # every legacy suite file: a change to one selects this test


def _lint():
    spec = importlib.util.spec_from_file_location("pipe_early_exit", REPO / "ops/lint/pipe_early_exit.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _check(body: str, pipefail: bool = True) -> list[int]:
    src = ("set -uo pipefail\n" if pipefail else "set -u\n") + textwrap.dedent(body)
    return [int(f.split(":")[1]) for f in _lint().check(src, "s.sh")]


def test_the_shell_suites_are_clean():
    lint = _lint()
    files = lint.shell_files(str(REPO))
    legacy = [f for f in files if fnmatch.fnmatchcase(f, LEGACY)]
    assert len(legacy) > 100, "the scan no longer finds the legacy suites"
    piped = [f for f in legacy if lint.PIPEFAIL.search((REPO / f).read_text(errors="replace"))]
    assert len(piped) > 40
    assert lint.scan(str(REPO)) == []


def test_flags_the_shapes_that_raced():
    old = r"""
    has() { printf '%s' "$2" | grep -qF -- "$3"; }
    check "sonarr not listed" '! note | grep -q sonarr'
    [ $rc = 3 ] && ls "$T/inbox" | grep -q '^voicecheck-phonegw-down-' && ok "warn" || no "warn"
    echo "$out" | grep -q SECRET && no "message text leaked" || ok "no message text in status"
    if grep -q '^GET' "$L" && ! grep -v '^GET' "$L" | grep -qv '/tg/bot'; then ok "GET-only"; fi
    grep -nE '(/usr)?/bin/ssh\b' "$P" | grep -v '^[0-9]*:#' | grep -q . \
      && { echo "refusing"; exit 2; }
    printf '%s' "$x" |
      grep -Eq 'a|b' && ok
    tail -1 "$n1" | grep -Eq '^Recovered'
    """
    assert _check(old) == [3, 4, 5, 6, 7, 8, 10, 12]


def test_quiet_and_list_options_count_in_any_spelling():
    for g in ("grep -q x", "grep -Fxq x", "grep -iEq x", "grep -l x", "grep -L x", "grep --quiet x",
              "grep --silent x", "grep -e a -q", "egrep -q x", "LC_ALL=C grep -q x", "grep -A2 -q x"):  # fmt: skip
        assert _check(f'out=1; printf "%s" "$out" | {g}\n') == [2], g
    for g in ("grep x", "grep -c x", "grep -v x", "grep -o x", "grep -e -q x", "grep -A 2 x", "grep -- -q"):
        assert _check(f'printf "%s" "$out" | {g}\n') == [], g


def test_head_and_max_count_only_when_the_line_tests_their_status():
    assert _check("if cmd | head -1; then ok; fi\n") == [2]
    assert _check("cmd | grep -m1 x && ok || no\n") == [2]
    assert _check("! cmd | head -n 2\n") == [2]
    assert _check("while cmd | grep --max-count=1 x; do :; done\n") == [2]
    value_only = """
        no() { fail=$((fail + 1)); [ -n "${2:-}" ] && printf '  got: %s\\n' "$2" | head -30; }
        eq "header" "$(printf '%s\\n' "$out" | head -1)" "x" && ok
        first=$(grep '^- ' "$f" | head -1)
        if [ "$(cmd | grep -m1 x)" = y ]; then ok; fi
        cmd | head -n -1 && ok
        """
    assert _check(value_only) == []


def test_skips_what_does_not_run_under_pipefail():
    assert _check("printf x | grep -q x\n", pipefail=False) == []
    elsewhere = """
        # printf "$x" | grep -q x  (a comment)
        check "c" bash -c '"$1" log | grep -q "^pre"' _ "$B"
        check "d" bash -c "! python3 -c 'print(1)' | grep -Eq '>[^&]'"
        cat > "$T/fake" <<'EOF'
        #!/bin/sh
        cat "$1" | grep -q x
        EOF
        printf x | grep -q x  # pipe-ok: pipefail is off in this function
        grep -q x <<<"$out"; [[ $out == *x* ]]; grep -q x "$file"
        """
    assert _check(elsewhere) == []


def test_main_exit_codes(tmp_path):
    lint = _lint()
    bad, good = tmp_path / "bad.sh", tmp_path / "good.sh"
    bad.write_text("set -uo pipefail\nprintf '%s' \"$x\" | grep -q y\n")
    good.write_text('set -uo pipefail\ngrep -q y <<<"$x"\n')
    assert lint.main([str(good)]) == 0
    assert lint.main([str(bad), str(good)]) == 1
