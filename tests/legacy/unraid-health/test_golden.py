"""Golden characterization test for ~/bin/unraid-health (slimming batch 2, 2026-10-14).
Runs a fixed scenario sequence on the fixture with a frozen clock (time.time) and --no-send, normalizes the
real date and temp paths, and compares rc/stdout/stderr/state/report to golden/<scenario>.txt.
`python3 test_golden.py --update` rewrites the goldens: only after checking that a change is intended."""
import datetime as dt
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

UH = os.environ.get("UH_BIN", "/home/node/bin/unraid-health")
HERE = Path(__file__).resolve().parent
FIX = (HERE / "fixtures" / "collect-2026-10-12.txt").read_text()
GOLD = HERE / "golden"
NOW = int(re.search(r"^now=(\d+)", FIX, re.M).group(1)) + 60
FREEZE = ("import sys, time, runpy; t = float(sys.argv[1]); time.time = lambda: t; p = sys.argv[2]; "
          "sys.argv = [p] + sys.argv[3:]; runpy.run_path(p, run_name='__main__')")


def mutate(text, pat, rep):
    new, n = re.subn(pat, rep, text, count=1, flags=re.M)
    assert n, f"mutation found nothing: {pat}"
    return new


def scenarios():
    hot = mutate(FIX, r'^temp="38"$', 'temp="58"')
    hot = mutate(hot, r"^(199 UDMA_CRC_Error_Count .* -\s+)0$", r"\g<1>7")
    hot = mutate(hot, r"^([0-9a-f]{64}\|/Firefly-III\|)running\|0\|", r"\1exited|137|")
    hot = mutate(hot, r"^( 2 +Webserver 2 +)running", r"\1paused")
    hot = mutate(hot, r"^(=====UH syslog_errors.*\n)", r"\1Oct 12 10:59:01 Homesrv kernel: ata3.00: exception Emask 0x10\n")
    broken = FIX.split("\n=====UH end")[0] + "\n"
    return [("1-baseline", FIX, ["--json"]), ("2-same-report", FIX, ["--report"]), ("3-changes", hot, []),
            ("4-changes-json", hot, ["--json"]), ("5-fail", broken, []), ("6-fail-again", broken, ["--json"]),
            ("7-back-report", FIX, ["--report"])]


def run_all():
    out = {}
    today = dt.datetime.now(ZoneInfo("Europe/Zurich")).date().isoformat()
    with tempfile.TemporaryDirectory() as T:
        sd, rd, ib = Path(T, "state"), Path(T, "reports"), Path(T, "inbox")
        for i, (name, text, extra) in enumerate(scenarios()):
            f = Path(T, f"{name}.txt")
            f.write_text(text)
            p = subprocess.run([sys.executable, "-I", "-c", FREEZE, str(NOW + 30 * i), UH, "--no-send", "--input",
                                f.name, "--state-dir", str(sd), "--reports-dir", str(rd), "--inbox-dir", str(ib)]
                               + extra, cwd=T, capture_output=True, text=True, timeout=120,
                               env=dict(os.environ, UNRAID_HEALTH_TZ="Europe/Zurich",
                                        UNRAID_HEALTH_HDDHEAT_LOG=str(Path(T, "no-hddheat.log")),
                                        UNRAID_HEALTH_ACK_FILE=str(Path(T, "no-ack.json")),
                                        UNRAID_HEALTH_CPUTEMP=str(Path(T, "no-cputemp-log"))))
            parts = [f"## rc {p.returncode}", "## stdout", p.stdout, "## stderr", p.stderr]
            for g in (sd / "state.json", rd / f"{today}.md", sd / "runs.log"):
                parts += [f"## {g.name}", g.read_text() if g.exists() else "(missing)"]
            parts += ["## inbox", "\n".join(sorted(x.name for x in ib.glob("*"))) if ib.exists() else "(none)"]
            s = "\n".join(parts).replace(T, "T").replace(today, "TODAY")
            out[name] = re.sub(r"TODAYT\d\d:\d\d:\d\d[+-]\d\d:\d\d", "TODAY-TS", s)
    return out


if __name__ == "__main__":
    got = run_all()
    if "--update" in sys.argv:
        GOLD.mkdir(exist_ok=True)
        for k, v in got.items():
            (GOLD / f"{k}.txt").write_text(v)
        print(f"golden: wrote {len(got)} files")
        sys.exit(0)
    bad = [k for k, v in got.items() if not (GOLD / f"{k}.txt").exists() or (GOLD / f"{k}.txt").read_text() != v]
    for k in bad:
        import difflib
        old = (GOLD / f"{k}.txt").read_text().splitlines() if (GOLD / f"{k}.txt").exists() else []
        print(f"FAIL {k}:\n" + "\n".join(list(difflib.unified_diff(old, got[k].splitlines(), lineterm=""))[:30]))
    print(f"golden: {len(got) - len(bad)} passed, {len(bad)} failed")
    sys.exit(1 if bad else 0)
