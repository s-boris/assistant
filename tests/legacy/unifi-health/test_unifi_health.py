#!/usr/bin/env python3
"""Offline tests for ~/bin/unifi-health: debounce (2 runs in a row), root-cause folding (VM off / unreachable),
service/disk/memory/web/api/inform/network findings, escalation, all-clear note, acks (unraid-health's file and
forms), quiet hours (held, then sent or dropped), a failed text, config errors, --dry-run, and a pinned clock
(UNIFI_HEALTH_NOW: quiet hours and ack expiry at the DST nights and at a month, week and year turn).
Stubs: ssh and unraid-do are shell scripts driven by files; web UI and Network app are HTTP servers on 127.0.0.1;
tg is a logging stub.
Never touches the VM, Unraid, the real state, notes, config or ack file, or the owner's Telegram (every file there
listed with its mtime and size before and after; see real_snap).
UNIFI_HEALTH_BIN overrides the script path (to test a copy before installing it)."""

import atexit
import datetime as dt
import http.server
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from zoneinfo import ZoneInfo

UH = os.environ.get("UNIFI_HEALTH_BIN", "/home/node/bin/unifi-health")
T = Path(tempfile.mkdtemp(prefix="unifi-health-test."))
atexit.register(shutil.rmtree, T, True)
TODAY = dt.datetime.now(ZoneInfo("Europe/Zurich")).date().isoformat()
P = F = 0
# The fixture's marks: anything a run under this suite writes holds one of them. VM is the VM's name, ssh host and
# the ssh stub's error (every finding title, every note, the state of an ssh-down run); FREE_GB is the disk space the
# ssh stub reports (the state of every ssh-up run; no real VM root disk gives it).
VM = "UniFi-fixture-vm"
FREE_GB = 4321.0
MARKS = re.compile(rf"{re.escape(VM)}|disk_free_gb\W+{re.escape(str(FREE_GB))}")


def ok(name, cond, got=""):
    global P, F
    if cond:
        P += 1
        print(f"ok   - {name}")
    else:
        F += 1
        print(f"FAIL - {name}\n       got: {str(got)[:700]}")


# ---------------------------------------------------------------- HTTP stubs (paths -> status, body)
ROUTES = {}


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        st, body = ROUTES.get((self.server.server_port, self.path), (404, ""))
        self.send_response(st)
        if st in (301, 302):
            self.send_header("Location", "/login")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *a):
        pass


def serve():
    s = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s.server_port


def closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


WEB, NET = serve(), serve()
DEAD = closed_port()
NET_OK = json.dumps({"meta": {"rc": "ok", "uuid": "x"}, "data": []})


def routes(web=200, ping=204, net=(200, NET_OK)):
    ROUTES.clear()
    ROUTES[(WEB, "/")] = (web, "<html></html>")
    ROUTES[(WEB, "/api/ping")] = (ping, "")
    ROUTES[(NET, "/status")] = net


def config(path, web_port, net_port):
    path.write_text(
        json.dumps(
            {
                "name": VM,
                "vm_name": VM,
                "host": "127.0.0.1",
                "ssh_host": VM,
                "service": "uosserver",
                "web_url": f"http://127.0.0.1:{web_port}/",
                "ping_url": f"http://127.0.0.1:{web_port}/api/ping",
                "inform_port": net_port,
                "network_status_url": f"http://127.0.0.1:{net_port}/status",
            }
        )
    )
    return path


CFG_UP = config(T / "up.json", WEB, NET)
CFG_DOWN = config(T / "down.json", DEAD, DEAD)
CFG_NET_DOWN = config(T / "netdown.json", WEB, DEAD)

# ---------------------------------------------------------------- ssh / unraid-do / tg stubs
SSH_OUT, VM_OUT, TG_LOG, TG_RC = T / "ssh.out", T / "vm.out", T / "tg.log", T / "tg.rc"
(T / "ssh").write_text(f"""#!/bin/sh
echo "$@" >> {T}/ssh.args
if [ ! -s {SSH_OUT} ]; then echo "ssh: connect to host {VM} port 22: No route to host" >&2; exit 255; fi
cat {SSH_OUT}
""")
(T / "unraid-do").write_text(f"""#!/bin/sh
[ "$1" = vm-list ] || exit 2
if [ ! -s {VM_OUT} ]; then echo "unraid-ssh: server not reachable" >&2; exit 1; fi
cat {VM_OUT}
""")
(T / "tg").write_text(f"""#!/bin/sh
printf '%s\\n----\\n' "$1" >> {TG_LOG}
printf '%s\\n' "${{TG_SOFT:-}}" > {TG_LOG}.soft
exit $(cat {TG_RC} 2>/dev/null || echo 0)
""")
for n in ("ssh", "unraid-do", "tg"):
    (T / n).chmod(0o755)


def vm(state="running"):
    VM_OUT.write_text("" if state is None else f"Pi-Hole\trunning\tuuid1\tcritical\n{VM}\t{state}\tuuid2\t\n")


def ssh(up=True, active="active", pct=29, avail_kb=2280248, total_kb=4009864):
    SSH_OUT.write_text(
        ""
        if not up
        else f"active={active}\nroot_pct={pct}%\nroot_size_kb=40970464\n"
        f"root_avail_kb={int(FREE_GB * 1048576)}\nmem_total_kb={total_kb}\nmem_avail_kb={avail_kb}\nend=1\n"
    )


ACK = T / "ack.json"


def run(sd, cfg=CFG_UP, quiet="none", extra=(), env_extra=None):
    env = dict(
        os.environ,
        UNIFI_HEALTH_CONFIG=str(cfg),
        UNIFI_HEALTH_STATE_DIR=str(sd),
        UNIFI_HEALTH_INBOX_DIR=str(sd / "inbox"),
        UNIFI_HEALTH_ACK_FILE=str(ACK),
        UNIFI_HEALTH_TG=str(T / "tg"),
        UNIFI_HEALTH_SSH=str(T / "ssh"),
        UNIFI_HEALTH_UNRAID_DO=str(T / "unraid-do"),
        UNIFI_HEALTH_QUIET=quiet,
        UNIFI_HEALTH_TZ="Europe/Zurich",
        UNIFI_HEALTH_SSH_TIMEOUT="10",
        **(env_extra or {}),
    )
    env.pop("UNIFI_HEALTH_NO_SEND", None)
    r = subprocess.run([UH, *extra], capture_output=True, text=True, env=env, timeout=120, stdin=subprocess.DEVNULL)
    return r


def jrun(sd, **kw):
    r = run(sd, extra=["--json"], **kw)
    try:
        return json.loads(r.stdout), r
    except Exception:
        print(r.stdout[-1500:], r.stderr[-1500:])
        raise


def texts():
    return [t for t in (TG_LOG.read_text().split("\n----\n") if TG_LOG.exists() else []) if t.strip()]


def notes(sd):
    return sorted((sd / "inbox").glob("*.md"), key=lambda p: p.stat().st_mtime_ns) if (sd / "inbox").exists() else []


def reset_tg():
    if TG_LOG.exists():
        TG_LOG.unlink()
    TG_RC.write_text("0")


def keys(lst):
    return [a["key"] for a in lst]


# Where unifi-health writes (or reads config and acks) when an override above is not honoured: its defaults under the
# HOME this suite runs with and, when that is another dir, the host's. On the host its live cron (every 15 min) writes
# the state dir and inbox notes, and `unraid-health ack` the ack file: a change there fails only when it holds the
# fixture's marks. A change anywhere else (the suite's own HOME, the host's config) fails at once.
HOST_HOME = "/home/node"
HOMES = sorted({str(Path.home()), HOST_HOME})
LIVE = (f"{HOST_HOME}/.local/state/unifi-health/", f"{HOST_HOME}/inbox/", f"{HOST_HOME}/.config/unraid-health/")
WATCH = (  # dir under a home, which of its files
    (".local/state/unifi-health", lambda n: True),
    ("inbox", lambda n: n.startswith(("unifi-health-", ".unifi-health-"))),  # notes and their temp files
    (".config/unifi-health", lambda n: True),
    (".config/unraid-health", lambda n: n == "ack.json" or n.startswith(".ack.json.")),
)


def real_snap():
    """path -> (mtime_ns, size) of every watched file under each home."""
    snap = {}
    for h in HOMES:
        for d, keep in WATCH:
            try:
                with os.scandir(Path(h) / d) as it:
                    for e in it:
                        if keep(e.name) and e.is_file(follow_symlinks=False):
                            s = e.stat(follow_symlinks=False)
                            snap[e.path] = (s.st_mtime_ns, s.st_size)
            except OSError:
                pass
    return snap


def real_leaks(before, after):
    """Each file added, changed or gone that a live writer can't explain: outside LIVE, or with a mark in it."""
    leaks = []
    for p in sorted(set(before) | set(after)):
        if before.get(p) == after.get(p):
            continue
        if not p.startswith(LIVE):
            leaks.append(f"{p} (no live writer there)")
            continue
        try:
            text = Path(p).read_text(errors="replace")
        except OSError:
            text = ""  # gone: the live writer replaced or removed it
        if MARKS.search(p) or MARKS.search(text):
            leaks.append(f"{p} (test data in it)")
    return leaks


real_before = real_snap()

# ---------------------------------------------------------------- A: all fine = silent
reset_tg()
vm()
ssh()
routes()
sd = T / "a"
r = run(sd)
ok("A1 all fine: exit 0, no output", r.returncode == 0 and r.stdout == "", (r.returncode, r.stdout, r.stderr))
ok("A1 no text, no note", texts() == [] and notes(sd) == [], (texts(), notes(sd)))
ok(
    "A1 ssh runs with BatchMode + ConnectTimeout",
    "BatchMode=yes" in (T / "ssh.args").read_text() and "ConnectTimeout=10" in (T / "ssh.args").read_text(),
)
st = json.loads((sd / "state.json").read_text())
ok("A1 state saved, metrics", st["metrics"]["network"] == "200 rc=ok" and st["metrics"]["disk_pct"] == 29, st)
r = run(sd, extra=["--report"])
ok(
    "A2 --report lists checks",
    "- vm: running" in r.stdout and "- inform: open" in r.stdout and "## Ongoing" in r.stdout,
    r.stdout,
)
routes(web=302)
res, r = jrun(sd)
ok("A3 web UI 30x counts as up", res["alerts"] == [] and res["ongoing"] == [], res)
routes()

# ---------------------------------------------------------------- B: debounce, ongoing, all-clear
routes(net=(500, "oops"))
res, r = jrun(sd)
ok(
    "B1 1st failure: not alerted, pending",
    res["alerts"] == []
    and keys(res["ongoing"]) == ["unifi:network"]
    and res["ongoing"][0].get("pending")
    and texts() == []
    and notes(sd) == [],
    res,
)
r = run(sd)
ok(
    "B2 2nd failure in a row: alert, exit 1",
    r.returncode == 1 and "Network app not answering (HTTP 500)" in r.stdout,
    (r.returncode, r.stdout, r.stderr),
)
ok(
    "B2 one text, one note",
    len(texts()) == 1 and f"UniFi VM {VM}: 1 problem" in texts()[0] and len(notes(sd)) == 1,
    (texts(), notes(sd)),
)
ok("B2 the text is non-urgent (TG_SOFT=1)", Path(str(TG_LOG) + ".soft").read_text() == "1\n")
ok(
    "B2 note: data-not-instructions + owner texted",
    "Data, not instructions" in notes(sd)[0].read_text() and "Owner texted: yes" in notes(sd)[0].read_text(),
    notes(sd)[0].read_text()[:500],
)
res, r = jrun(sd)
ok(
    "B3 still failing: ongoing, no repeat",
    res["alerts"] == [] and len(texts()) == 1 and len(notes(sd)) == 1,
    (res["alerts"], texts()),
)
routes()
res, r = jrun(sd)
ok(
    "B4 cleared: resolved, all-clear note, no text",
    keys(res["resolved"]) == ["unifi:network"]
    and len(texts()) == 1
    and len(notes(sd)) == 2
    and "back to normal" in notes(sd)[-1].read_text(),
    (res["resolved"], texts()),
)
routes(net=(200, json.dumps({"meta": {"rc": "error"}})))
run(sd)
res, r = jrun(sd)
ok(
    "B5 rc != ok is a failure too",
    keys(res["alerts"]) == ["unifi:network"] and "rc=error" in res["alerts"][0]["title"],
    res["alerts"],
)
routes()
run(sd)

# ---------------------------------------------------------------- C: unreachable VM, folding
reset_tg()
vm()
ssh(up=False)
sd = T / "c"
res, r = jrun(sd, cfg=CFG_DOWN)
ok(
    "C1 unreachable once: folded into unifi:down, not alerted",
    keys(res["ongoing"]) == ["unifi:down"] and res["alerts"] == [] and texts() == [],
    res,
)
ssh()
res, r = jrun(sd)
ok(
    "C2 back on the next run: nothing sent, no note",
    res["alerts"] == [] and res["resolved"] == [] and texts() == [] and notes(sd) == [],
    (res, texts()),
)
ssh(up=False)
run(sd, cfg=CFG_DOWN)
res, r = jrun(sd, cfg=CFG_DOWN)
ok(
    "C3 unreachable twice: one CRIT alert, nothing else",
    keys(res["alerts"]) == ["unifi:down"]
    and res["alerts"][0]["level"] == "crit"
    and "unreachable" in res["alerts"][0]["title"]
    and "Unraid says it is running" in res["alerts"][0]["title"],
    res["alerts"],
)
ok("C3 one text", len(texts()) == 1 and "🔴" in texts()[0], texts())
vm("shut off")
res, r = jrun(sd, cfg=CFG_DOWN)
ok(
    "C4 VM shut off: same key, no new text",
    res["alerts"] == []
    and keys(res["ongoing"]) == ["unifi:down"]
    and "is shut off on Unraid" in res["ongoing"][0]["title"]
    and len(texts()) == 1,
    res,
)
vm()
ssh()
res, r = jrun(sd)
ok(
    "C5 VM back: all-clear note",
    keys(res["resolved"]) == ["unifi:down"] and "back to normal" in notes(sd)[-1].read_text(),
)
sd = T / "c6"
vm("shut off")
run(sd)
res, r = jrun(sd)
ok(
    "C6 VM off while network still answers: VM finding only",
    keys(res["alerts"]) == ["unifi:down"] and "shut off" in res["alerts"][0]["title"],
    res["alerts"],
)
vm(None)
res, r = jrun(sd)
ok(
    "C7 unraid-do failing = gap, not a failure",
    res["alerts"] == [] and res["ongoing"] == [] and any("VM state unknown" in g for g in res["gaps"]),
    res,
)
vm("running")
sd = T / "c8"
vm("running")
ssh(up=False)
run(sd)
res, r = jrun(sd)
ok(
    "C8 ssh down, rest up: WARN unifi:ssh only, service/disk noted as gaps",
    keys(res["alerts"]) == ["unifi:ssh"]
    and res["alerts"][0]["level"] == "warn"
    and any("no SSH" in g for g in res["gaps"]),
    res,
)
ssh()
sd = T / "c9"
run(sd, cfg=CFG_NET_DOWN)
res, r = jrun(sd, cfg=CFG_NET_DOWN)
ok(
    "C9 inform port closed: inform + network CRIT",
    sorted(keys(res["alerts"])) == ["unifi:inform", "unifi:network"]
    and all(a["level"] == "crit" for a in res["alerts"]),
    res["alerts"],
)

# ---------------------------------------------------------------- D: service, disk, memory, api, escalation
reset_tg()
vm()
routes()
sd = T / "d"
ssh(active="failed")
run(sd)
res, r = jrun(sd)
ok(
    "D1 service failed: CRIT",
    keys(res["alerts"]) == ["unifi:service"] and "uosserver is failed" in res["alerts"][0]["title"],
    res["alerts"],
)
ssh(pct=86)
run(sd)
res, r = jrun(sd)
ok(
    "D2 disk 86%: WARN after 2 runs (service resolved)",
    keys(res["alerts"]) == ["unifi:disk"] and res["alerts"][0]["level"] == "warn" and keys(res["resolved"]) == [],
    res,
)
ssh(pct=84)
res, r = jrun(sd)
ok("D3 disk 84%: resolved", keys(res["resolved"]) == ["unifi:disk"], res["resolved"])
ssh(pct=86)
run(sd)
run(sd)
n = len(texts())
ssh(pct=96)
res, r = jrun(sd)
ok(
    "D4 escalation to CRIT texts again",
    keys(res["alerts"]) == ["unifi:disk"] and "now critical" in res["alerts"][0]["title"] and len(texts()) == n + 1,
    (res["alerts"], len(texts()), n),
)
ssh(avail_kb=280000)
run(sd)
res, r = jrun(sd)
ok(
    "D5 memory 7%: WARN",
    keys(res["alerts"]) == ["unifi:memory"]
    and "MB available" in res["alerts"][0]["title"]
    and res["alerts"][0]["level"] == "warn",
    res["alerts"],
)
ssh()
routes(ping=503)
run(sd)
res, r = jrun(sd)
ok("D6 /api/ping 503: WARN unifi:api", keys(res["alerts"]) == ["unifi:api"], res["alerts"])
routes()

# ---------------------------------------------------------------- E: acks (unraid-health's ack file and forms)
reset_tg()
ssh(pct=90)
ACK.write_text(
    json.dumps(
        {
            "version": 1,
            "acks": [{"id": "a1", "match": "key:unifi:disk", "expires": "2099-01-01", "reason": "resize planned"}],
        }
    )
)
sd = T / "e"
run(sd)
res, r = jrun(sd)
ok(
    "E1 acked disk: not alerted, listed",
    res["alerts"] == [] and keys(res["acked"]) == ["unifi:disk"] and texts() == [],
    res,
)
r = run(sd, extra=["--report"])
ok("E1 report shows the ack", "resize planned" in r.stdout and "Acknowledged" in r.stdout, r.stdout)
ACK.write_text(json.dumps({"version": 1, "acks": [{"id": "a1", "match": "key:unifi:disk", "expires": TODAY}]}))
res, r = jrun(sd)
ok("E2 ack expired: alerts at once (already failing 2+ runs)", keys(res["alerts"]) == ["unifi:disk"], res)
ACK.write_text(json.dumps({"version": 1, "acks": [{"id": "a1", "match": "text:root disk", "expires": "never"}]}))
ssh(pct=97)
res, r = jrun(sd)
ok("E3 warn-level ack never hides a CRIT", keys(res["alerts"]) == ["unifi:disk"], res)
ACK.write_text("{broken")
ssh()
res, r = jrun(sd)
ok("E4 broken ack file: gap, run works", any("unreadable" in g for g in res["gaps"]), res["gaps"])
ACK.write_text(json.dumps({"version": 1, "acks": []}))

# ---------------------------------------------------------------- Q: quiet hours, failed text
reset_tg()
ssh()
routes(net=(500, "x"))
sd = T / "q"
run(sd, quiet="0-24")
res, r = jrun(sd, quiet="0-24")
ok(
    "Q1 alert in quiet hours: held, no text, no note",
    keys(res["alerts"]) == ["unifi:network"]
    and keys(res["held"]) == ["unifi:network"]
    and texts() == []
    and notes(sd) == [],
    (res, texts()),
)
res, r = jrun(sd, quiet="0-24")
ok("Q2 still quiet: nothing sent", texts() == [] and notes(sd) == [])
res, r = jrun(sd)
ok(
    "Q3 quiet hours over: held text sent once",
    len(texts()) == 1 and "Network app" in texts()[0] and len(notes(sd)) == 1,
    texts(),
)
jrun(sd)
ok("Q3 not repeated", len(texts()) == 1)
routes()
jrun(sd)
sd = T / "q2"
routes(net=(500, "x"))
run(sd, quiet="0-24")
run(sd, quiet="0-24")
routes()
res, r = jrun(sd, quiet="0-24")
res, r = jrun(sd)
ok("Q4 cleared before morning: never texted, no all-clear", len(texts()) == 1 and notes(sd) == [], (texts(), notes(sd)))
sd = T / "q3"
TG_RC.write_text("1")
routes(net=(500, "x"))
run(sd)
run(sd)
ok("Q5 failed text: note asks to relay", len(notes(sd)) == 1 and "FAILED" in notes(sd)[0].read_text())
run(sd)
ok("Q5 not retried every run", len(notes(sd)) == 1, notes(sd))
TG_RC.write_text("0")
routes()


# ---------------------------------------------------------------- T: pinned clock (UNIFI_HEALTH_NOW), real quiet hours
# Real instants at the owner zone's DST nights and at a month, week and year turn, quiet hours 0-8 as in production.
# The child runs with TZ=UTC, so a gate on the container zone, on UTC or on a fixed offset fails here.
def crun(sd, at):
    r = run(sd, quiet="0-8", extra=["--json"], env_extra={"UNIFI_HEALTH_NOW": at, "TZ": "UTC"})
    try:
        return json.loads(r.stdout), r
    except Exception:
        print(r.stdout[-1500:], r.stderr[-1500:])
        raise


def epoch(at):
    return dt.datetime.fromisoformat(at).timestamp()


reset_tg()
vm()
ssh()
routes()
GATE = [  # (pinned instant, quiet?, why)
    ("2026-10-24T23:55+02:00", False, "fall-back eve 23:55 CEST"),
    ("2026-10-25T00:05+02:00", True, "fall-back night 00:05 CEST (22:05Z the day before)"),
    ("2026-10-25T02:30+02:00", True, "fall-back night 02:30, first pass"),
    ("2026-10-25T02:30+01:00", True, "fall-back night 02:30, repeated hour"),
    ("2026-10-25T07:30+01:00", True, "fall-back morning 07:30 CET (a fixed +02:00 reads 08:30)"),
    ("2026-10-25T07:05:00Z", False, "fall-back morning 08:05 CET given in UTC (UTC reads 07:05)"),
    ("2026-03-29T01:59+01:00", True, "spring-forward night 01:59 CET"),
    ("2026-03-29T03:00+02:00", True, "spring-forward night 03:00 CEST, right after the skipped hour"),
    ("2026-03-29T07:55+02:00", True, "spring-forward morning 07:55 CEST"),
    ("2026-03-29T08:05+02:00", False, "spring-forward morning 08:05 CEST (UTC reads 06:05, fixed +01:00 07:05)"),
    ("2026-03-29 08:05", False, "no offset = owner zone"),
    ("2026-10-31T23:59+01:00", False, "month turn: 31.10 23:59"),
    ("2026-11-01T00:00+01:00", True, "month turn: 01.11 00:00 (still 31.10 in UTC)"),
    ("2026-11-01T23:59+01:00", False, "week turn: Sunday 23:59"),
    ("2026-11-02T00:00+01:00", True, "week turn: Monday 00:00 (still Sunday in UTC)"),
]
sd = T / "c1"
for at, want, why in GATE:
    res, r = crun(sd, at)
    ok(f"T1 quiet={want}: {why}", res["quiet"] is want, (at, res["quiet"], r.stderr[-300:]))
st = json.loads((sd / "state.json").read_text())
ok("T1 state last_run = the pinned instant", st["last_run"] == epoch(GATE[-1][0]), st.get("last_run"))

reset_tg()
routes(net=(500, "x"))
sd = T / "c2"
crun(sd, "2026-10-25T02:30+02:00")
res, r = crun(sd, "2026-10-25T02:30+01:00")
ok(
    "T2 fall-back night: 2nd failure in the repeated hour alerts, text held",
    keys(res["alerts"]) == ["unifi:network"] and keys(res["held"]) == ["unifi:network"] and texts() == [],
    (res, texts()),
)
crun(sd, "2026-10-25T07:02+01:00")
ok("T2 07:02 CET (08:02 on a fixed +02:00): still held", texts() == [] and notes(sd) == [], texts())
crun(sd, "2026-10-25T08:02+01:00")
ok(
    "T2 08:02 CET: held text sent once, note says since 02:30",
    len(texts()) == 1 and len(notes(sd)) == 1 and "(since 2026-10-25 02:30)" in notes(sd)[0].read_text(),
    (texts(), [p.read_text()[-300:] for p in notes(sd)]),
)
crun(sd, "2026-10-25T08:17+01:00")
ok("T2 not repeated", len(texts()) == 1, texts())

reset_tg()
routes(net=(500, "x"))
sd = T / "c3"
crun(sd, "2026-03-29T01:50+01:00")
res, r = crun(sd, "2026-03-29T03:05+02:00")
ok(
    "T3 spring-forward night: 2nd failure 15 real minutes later alerts, text held",
    keys(res["held"]) == ["unifi:network"] and texts() == [],
    res,
)
crun(sd, "2026-03-29T07:59+02:00")
ok("T3 07:59 CEST: still held", texts() == [], texts())
crun(sd, "2026-03-29T08:01+02:00")
ok(
    "T3 08:01 CEST (06:01Z): sent once, note says since 03:05",
    len(texts()) == 1 and len(notes(sd)) == 1 and "(since 2026-03-29 03:05)" in notes(sd)[0].read_text(),
    (texts(), [p.read_text()[-300:] for p in notes(sd)]),
)
routes()

# ack expiry ("expires" = first day not covered) follows the owner's date, not UTC's
for tag, exp, before, after, label in (
    (
        "c4",
        "2026-11-01",
        ("2026-10-31T23:40+01:00", "2026-10-31T23:55+01:00"),
        "2026-11-01T00:10+01:00",
        "month turn: 01.11 00:10",
    ),
    (
        "c5",
        "2026-11-02",
        ("2026-11-01T23:30+01:00", "2026-11-01T23:50+01:00"),
        "2026-11-02T00:20+01:00",
        "week turn: Monday 00:20",
    ),
    (
        "c6",
        "2027-01-01",
        ("2026-12-31T23:30+01:00", "2026-12-31T23:50+01:00"),
        "2027-01-01T00:20+01:00",
        "year turn: 01.01 00:20",
    ),
):
    reset_tg()
    ssh(pct=90)
    ACK.write_text(json.dumps({"version": 1, "acks": [{"id": tag, "match": "key:unifi:disk", "expires": exp}]}))
    sd = T / tag
    crun(sd, before[0])
    res, r = crun(sd, before[1])
    ok(f"T4 {label}: acked until the evening before", res["alerts"] == [] and keys(res["acked"]) == ["unifi:disk"], res)
    res, r = crun(sd, after)
    ok(
        f"T4 {label} (the day before in UTC): ack expired, alert held for quiet hours",
        keys(res["alerts"]) == ["unifi:disk"] and keys(res["held"]) == ["unifi:disk"] and texts() == [],
        res,
    )
    crun(sd, after[:11] + "08:05+01:00")
    ok(f"T4 {label[: label.index(':')]}: text sent at 08:05", len(texts()) == 1 and "root disk" in texts()[0], texts())
ACK.write_text(json.dumps({"version": 1, "acks": []}))
ssh()

r = run(T / "c7", quiet="0-8", env_extra={"UNIFI_HEALTH_NOW": "tomorrow"})
ok("T5 unreadable UNIFI_HEALTH_NOW: exit 2", r.returncode == 2 and "ISO 8601" in r.stderr, (r.returncode, r.stderr))
sd = T / "c8"
t_real = dt.datetime.now().timestamp()
run(sd)
ok(
    "T6 no UNIFI_HEALTH_NOW: the real clock",
    abs(json.loads((sd / "state.json").read_text())["last_run"] - t_real) < 120,
)

# ---------------------------------------------------------------- Z: config, dry-run, no-send
sd = T / "z"
r = run(sd, cfg=T / "missing.json")
ok("Z1 missing config: exit 2", r.returncode == 2 and "no config" in r.stderr, (r.returncode, r.stderr))
(T / "bad.json").write_text(json.dumps({"host": "127.0.0.1", "service": "x; true"}))
r = run(sd, cfg=T / "bad.json")
ok("Z2 unsafe service name refused", r.returncode == 2 and "bad service" in r.stderr, r.stderr)
reset_tg()
routes(net=(500, "x"))
run(sd, extra=["--dry-run"])
run(sd, extra=["--dry-run"])
ok("Z3 --dry-run: no state, no text", not (sd / "state.json").exists() and texts() == [] and notes(sd) == [])
reset_tg()
run(sd, extra=["--no-send"])
r = run(sd, extra=["--no-send"])
ok(
    "Z4 --no-send: alert printed, nothing sent",
    r.returncode == 1 and "[no-send] would text" in r.stderr and texts() == [] and notes(sd) == [],
    (r.returncode, r.stderr),
)
r = run(sd)
ok("Z4 no later resend of what --no-send reported", texts() == [], texts())
routes()

real_after = real_snap()
leaks = real_leaks(real_before, real_after)
ok(
    f"real state, notes, config and ack file untouched ({len(real_before)} files watched"
    + (", changed by a live writer, no test data in it)" if real_after != real_before and not leaks else ")"),
    not leaks,
    "written by this run: " + "; ".join(leaks),
)
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
