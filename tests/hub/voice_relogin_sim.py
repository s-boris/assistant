#!/usr/bin/env python3
"""The voice-relogin paste simulated offline: tests/hub/test_voice_relogin.py runs it on a source tree (A) in a
scratch dir (R). The procedure: docs/runbooks/voice-relogin.md.

  voice_relogin_sim.py A R [PY] [--only NAME[,NAME]]     exit 0 = every step passed

Each scenario builds its own world under R/<name> (never the real home, hub, Docker, account or Telegram):
  home/       the brain's home as the voice kit needs it: tg adapter config + allowlist, ~/opt/voice-relay, the switch
              file ~/.voice-relay-off (the relay runs in the hub)
  kit/        the voice kit rendered from A (hubkit.kit.build_voice); the paste under test is kit/voice-relogin.sh
  hub/        HUB_ROOT as the voice install paste left it: compose.voice.yaml, loader/voiceloader.py, voice/config
              (live.json, access.json), voice/state/voiceloader-status.json (running RID), secrets/voice_tg_json (a
              dummy api config, 0600) and secrets/voice/ (the session dir, empty = today's "no session file")
  releases/   RID/voice: the relay's tree from A (hubkit.release.voice_sources) plus a fake `pyrogram` in voice/vendor,
              release.json with every file's sha256 (the loader's own --check-release passes)
  bin/docker  a stand-in for the docker CLI: `exec` runs the command HERE with the container paths mapped into the world
              (the -e values too) and stdin passed through; inspect/top/restart answer from docker.json; a restart
              records whether a login waiter still ran and turns the bridge "on" only for an authorized session;
              `run.py status` (the relay's own status) answers {"tg_calls": ...}; every argv goes to docker.log
  tg/         the fake Telegram: script.json (code, 2FA password, the account) and events.jsonl (connect, code request,
              sign-in, password, disconnect, restart: per client instance, name and pid)
The paste runs under a pseudo-terminal (its `read ... </dev/tty` prompts) with stdin /dev/null, as
`bash /tmp/x.sh </dev/null` runs in his terminal; the driver types the answers and checks every prompt it sees.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pty
import select
import shutil
import signal
import stat
import sys
import time
from pathlib import Path

_ap = argparse.ArgumentParser(prog="voice_relogin_sim.py")
_ap.add_argument("src")
_ap.add_argument("root")
_ap.add_argument("py", nargs="?", default=sys.executable)
_ap.add_argument("--only", default="", help="comma-separated scenario names")
_ap.add_argument("--show", action="store_true", help="print each paste's terminal text (what he would see)")
_a = _ap.parse_args()
A, ROOT, PY = Path(_a.src).resolve(), Path(_a.root).resolve(), _a.py
ONLY = {n for n in _a.only.split(",") if n}
sys.path[:0] = [str(A), str(A / "packages")]
from hubkit import kit as K  # noqa: E402  (the snapshot's own)
from hubkit import release as REL  # noqa: E402

RID = "0123456789ab"
OWNER, ACCOUNT = 111222333, 4242000
API_HASH = "".join(["f00d", "feed", "cafe", "0123", "4567", "89ab", "cdef", "0123"])
PHONE = "".join(["+41", " 79", " 55", "5 1", "2 3", "4"])  # built: no number-shaped literal
PHONE_DIGITS = "".join(["41", "795", "551", "234"])
CODE, PW = "54321", "correct horse battery staple"
FAILED: list[str] = []
NO_SESSION = "refused: no session file: log in first (voice-tg login start / finish)"


def step(name: str, ok: object, detail: str = "") -> bool:
    if not ok:
        FAILED.append(name)
    print(("ok   " if ok else "FAIL ") + name + (f" ({detail})" if detail and not ok else ""), flush=True)
    return bool(ok)


# ------------------------------------------------------------------ the fake Telegram (pyrogram stand-in)
FAKE_INIT = '''"""Fake pyrogram: a scripted Telegram (TGDIR/script.json), events in TGDIR/events.jsonl."""
import itertools, json, os, time
from . import errors

TGDIR = @@TGDIR@@
_n = itertools.count(1)


def _script():
    with open(os.path.join(TGDIR, "script.json")) as f:
        return json.load(f)


def _ev(**kw):
    with open(os.path.join(TGDIR, "events.jsonl"), "a") as f:
        f.write(json.dumps(dict(kw, pid=os.getpid(), t=time.time())) + "\\n")


class User:
    def __init__(self, d):
        self.id, self.first_name, self.is_bot = int(d["id"]), d.get("first_name"), bool(d.get("is_bot"))


class _Kind:
    name = "SMS"


class Sent:
    def __init__(self):
        self.phone_code_hash, self.type = "fakehash" + os.urandom(6).hex(), _Kind()


class Client:
    def __init__(self, name, api_id=None, api_hash=None, workdir=".", no_updates=False, **kw):
        self.name, self.session = name, os.path.join(workdir, name + ".session")
        self.inst, self.sent, self.is_connected = "%d-%d" % (os.getpid(), next(_n)), None, False

    async def connect(self):
        if not os.path.exists(self.session):
            open(self.session, "w").close()
        with open(self.session) as f:
            ok = f.read().startswith("authorized")
        self.is_connected = True
        _ev(ev="connect", inst=self.inst, name=self.name, authorized=ok)
        return ok

    async def disconnect(self):
        self.is_connected = False
        _ev(ev="disconnect", inst=self.inst, name=self.name)

    async def send_phone_number_code(self, phone):
        self.sent = Sent()
        _ev(ev="send_code", inst=self.inst, name=self.name, phone_ok=phone == "+" + _script()["phone_digits"])
        return self.sent

    def _authorize(self):
        with open(self.session, "w") as f:
            f.write("authorized\\n")
        return User(_script()["user"])

    async def sign_in(self, phone, code_hash, code):
        s = _script()
        if s.get("expired") or self.sent is None or code_hash != self.sent.phone_code_hash:
            _ev(ev="sign_in", inst=self.inst, ok="expired")
            raise errors.PhoneCodeExpired()
        if code != s["code"]:
            _ev(ev="sign_in", inst=self.inst, ok=False)
            raise errors.PhoneCodeInvalid()
        _ev(ev="sign_in", inst=self.inst, ok=True)
        if s.get("password"):
            raise errors.SessionPasswordNeeded()
        return self._authorize()

    async def check_password(self, pw):
        ok = pw == _script().get("password")
        _ev(ev="check_password", inst=self.inst, ok=ok)
        if not ok:
            raise errors.PasswordHashInvalid()
        return self._authorize()

    async def log_out(self):
        _ev(ev="log_out", inst=self.inst)

    async def get_me(self):
        return User(_script()["user"])

    async def set_privacy(self, key, rules):
        _ev(ev="set_privacy", inst=self.inst, key=str(key))

    async def get_dialogs(self, limit=0):
        _ev(ev="get_dialogs", inst=self.inst)
        if False:
            yield None

    async def add_contact(self, uid, first):
        _ev(ev="add_contact", inst=self.inst, owner=int(uid) == int(_script()["owner"]))

    async def get_contacts(self):
        return [User({"id": _script()["owner"]})]
'''
FAKE_MODS = {
    "errors.py": "class RPCError(Exception):\n    ID = 'RPC_ERROR'\n\n\n"
    + "".join(
        f"class {n}(RPCError):\n    ID = '{i}'\n\n\n"
        for n, i in (
            ("SessionPasswordNeeded", "SESSION_PASSWORD_NEEDED"),
            ("PhoneCodeInvalid", "PHONE_CODE_INVALID"),
            ("PhoneCodeExpired", "PHONE_CODE_EXPIRED"),
            ("PasswordHashInvalid", "PASSWORD_HASH_INVALID"),
            ("PeerIdInvalid", "PEER_ID_INVALID"),
        )
    ),
    "enums.py": "class PrivacyKey:\n    PHONE_CALL = 'PHONE_CALL'\n    PHONE_P2P = 'PHONE_P2P'\n",
    "types.py": "class InputPrivacyRuleAllowContacts:\n    pass\n\n\nclass InputPrivacyRuleDisallowAll:\n    pass\n",
}

# ------------------------------------------------------------------ the docker stand-in
DOCKER = r'''#!@@PY@@ -I
"""docker stand-in (voice-relogin simulation): exec runs here with the container's paths mapped into the world."""
import json, os, subprocess, sys
W = @@W@@
MAP = [("/srv/releases", W + "/releases"), ("/srv/voice/state", W + "/hub/voice/state"),
       ("/srv/voice/config", W + "/hub/voice/config"), ("/srv/voice/session", W + "/hub/secrets/voice"),
       ("/run/secrets", W + "/hub/secrets"), ("/opt/hub-loader", W + "/hub/loader")]


def m(s):
    for a, b in MAP:
        s = s.replace(a, b)
    return s


def ev(**kw):
    with open(W + "/tg/events.jsonl", "a") as f:
        f.write(json.dumps(kw) + "\n")


def waiters():
    out = []
    for p in os.listdir("/proc"):
        if p.isdigit():
            try:
                cl = open("/proc/%s/cmdline" % p, "rb").read().replace(b"\0", b" ").decode(errors="replace")
            except OSError:
                continue
            if W in cl and " _wait " in cl + " ":
                out.append(int(p))
    return out


argv = sys.argv[1:]
with open(W + "/docker.log", "a") as f:
    f.write(json.dumps(argv) + "\n")
ctl = json.load(open(W + "/docker.json"))
if argv[:1] == ["inspect"]:
    print({"{{.State.Running}}": "true" if ctl["running"] else "false",
           "{{.State.Health.Status}}": ctl["health"]}.get(argv[2], "?"))
    sys.exit(0)
if argv[:1] == ["top"]:
    sys.stdout.write(ctl["top"])
    sys.exit(0)
if argv[:1] == ["restart"]:
    sess = W + "/hub/secrets/voice/voice-tg.session"
    ok = os.path.exists(sess) and open(sess).read().startswith("authorized")
    ctl["bridge"] = "on" if ok and oct(os.stat(sess).st_mode & 0o777) == "0o600" else ctl["bridge"]
    ctl["restarts"] = ctl.get("restarts", 0) + 1
    json.dump(ctl, open(W + "/docker.json", "w"))
    ev(ev="restart", waiters=waiters(), session=ok)
    if ctl["bridge"] == "on":  # the relay's own client on the session from now on (it never lets go)
        ev(ev="connect", inst="relay", name="voice-tg", authorized=True)
    sys.exit(0)
if argv[:1] != ["exec"]:
    print("docker stand-in: no %s here" % argv[:1], file=sys.stderr)
    sys.exit(125)
i, env = 1, {"PATH": "/usr/bin:/bin", "HOME": W + "/ctmp", "TZ": "UTC", "LANG": "C.UTF-8"}
while argv[i].startswith("-"):
    if argv[i] == "-e":
        k, _, v = argv[i + 1].partition("=")
        env[k] = m(v)
        i += 2
    else:
        i += 1
if argv[i] != "claude-hub-voice":
    print("docker stand-in: no container %s" % argv[i], file=sys.stderr)
    sys.exit(1)
cmd = [m(a) for a in argv[i + 1:]]
if cmd[0] == "python3":
    cmd[0] = "@@PY@@"
run = [n for n, a in enumerate(cmd) if a.endswith("/voice/run.py")]
if run and cmd[run[0] + 1:run[0] + 2] == ["status"]:  # the relay's own status: the bridge as the last restart left it
    print(json.dumps({"call_ids": [], "tg_calls": ctl["bridge"], "live": 0}))
    sys.exit(0)
sys.exit(subprocess.run(cmd, env=env, cwd=W + "/ctmp").returncode)
'''


def _q(p: Path) -> str:
    return json.dumps(str(p))


def world(name: str, *, phone_in_creds: bool = True, password: str | None = None, expired: bool = False,
          old_release: bool = False) -> Path:  # fmt: skip
    """A fresh world under ROOT/name (see the module doc)."""
    w = ROOT / name
    if w.exists():
        shutil.rmtree(w)
    home, kit, hub, rel, tg, b = (w / n for n in ("home", "kit", "hub", "releases", "tg", "bin"))
    for d in (home, kit, hub, rel, tg, b, w / "ctmp", w / "roothome"):
        d.mkdir(parents=True)
    # the brain's home: what build_voice needs, and the relay's switch file
    (home / ".config/tg-adapter").mkdir(parents=True)
    (home / ".config/tg-adapter/live.json").write_text(json.dumps({
        "bot": "relogin_test_bot", "token_file": "~/.secrets/tg-live-token",
        "allow_from_file": "~/.claude/channels/telegram/access.json"}))  # fmt: skip
    (home / "opt/voice-relay").mkdir(parents=True)
    (home / ".voice-relay-off").write_text("")
    os.environ["HOME"] = str(home)
    files, info = K.build_voice(A, {"KIT_DIR": str(kit), "HUB_ROOT": str(hub), "BOT_HOME": str(home)})
    assert info["state"] == "rendered", info
    for n, data in files.items():
        (kit / n).parent.mkdir(parents=True, exist_ok=True)
        (kit / n).write_bytes(data)
    # HUB_ROOT as the voice install paste left it
    for d in ("loader", "voice/config", "voice/state", "secrets/voice"):
        (hub / d).mkdir(parents=True)
    (hub / "secrets/voice").chmod(0o700)
    shutil.copy(kit / "compose.voice.yaml", hub / "compose.voice.yaml")
    shutil.copy(kit / "loader/voiceloader.py", hub / "loader/voiceloader.py")
    shutil.copy(kit / "voice/live.json", hub / "voice/config/live.json")
    (hub / "voice/config/access.json").write_text(json.dumps({"allowFrom": [str(OWNER)]}))
    (hub / "voice/state/voiceloader-status.json").write_text(json.dumps(
        {"state": "running", "release": RID, "since": 1, "why": "", "pid": 7, "ts": int(time.time())}))  # fmt: skip
    creds = {"api_id": 7654321, "api_hash": API_HASH, **({"phone": PHONE} if phone_in_creds else {})}
    fd = os.open(hub / "secrets/voice_tg_json", os.O_WRONLY | os.O_CREAT, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(creds, f)
    # the release: the relay's tree from A, a fake pyrogram in vendor, release.json as the loader checks it
    v = rel / RID
    for relp, src in REL.voice_sources(A).items():
        (v / relp).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, v / relp)
    if old_release:  # a release built before this branch: run.py knows no `tg` (argparse: invalid choice)
        (v / "voice/run.py").write_text("import sys\nprint(\"voice-relay: error: argument cmd: invalid choice: "
                                        "%r\" % sys.argv[1], file=sys.stderr)\nsys.exit(2)\n")  # fmt: skip
    fake = v / "voice/vendor/pyrogram"
    fake.mkdir(parents=True)
    (fake / "__init__.py").write_text(FAKE_INIT.replace("@@TGDIR@@", _q(tg)))
    for n, body in FAKE_MODS.items():
        (fake / n).write_text(body)
    listed = {}
    for p in sorted((v / "voice").rglob("*")):
        if p.is_file():
            data = p.read_bytes()
            listed[str(p.relative_to(v))] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
    (v / "release.json").write_text(json.dumps({"format": 1, "rid": RID, "entry": {"voice": "voice/run.py"},
                                                "files": listed}))  # fmt: skip
    # the fake Telegram's script, the docker stand-in and its state
    (tg / "script.json").write_text(json.dumps({
        "code": CODE, "password": password, "expired": expired, "phone_digits": PHONE_DIGITS, "owner": OWNER,
        "user": {"id": ACCOUNT, "first_name": "Claude", "is_bot": False}}))  # fmt: skip
    (tg / "events.jsonl").write_text("")
    (w / "docker.json").write_text(
        json.dumps({"running": True, "health": "healthy", "top": "UID PID ARGS\nnode 1 claude\n", "bridge": NO_SESSION})
    )
    (b / "docker").write_text(DOCKER.replace("@@PY@@", PY).replace("@@W@@", _q(w)))
    (b / "docker").chmod(0o755)
    (w / "paste.sh").write_text(inner((kit / "voice-relogin.sh").read_text()))
    return w


def inner(paste: str) -> str:
    return paste.split("<<'HUB'\n", 1)[1].split("\nHUB\n", 1)[0] + "\n"


def ctl(w: Path, **kw: object) -> None:
    d = json.loads((w / "docker.json").read_text())
    d.update(kw)
    (w / "docker.json").write_text(json.dumps(d))


PROMPTS = {"code": "Login code (Telegram/SMS): ", "password": "2FA password of the call account: ",
           "phone": "Phone number of the call account (+country code): "}  # fmt: skip


def paste(w: Path, answers: list[tuple[str, str]], timeout: float = 120) -> tuple[int, str, list[str]]:
    """Runs the paste's script as root's shell would (stdin /dev/null, his terminal as /dev/tty); answers each prompt
    in order. Returns (exit code, terminal text, the prompts seen in order)."""
    env = {"PATH": f"{w / 'bin'}:/usr/bin:/bin", "HOME": str(w / "roothome"), "LANG": "C.UTF-8", "TERM": "dumb"}
    pid, fd = pty.fork()
    if pid == 0:  # the child: a session leader with the pty as its terminal, stdin from /dev/null
        dn = os.open(os.devnull, os.O_RDONLY)
        os.dup2(dn, 0)
        os.execve("/bin/bash", ["bash", str(w / "paste.sh")], env)
    out, seen, todo, mark = b"", [], list(answers), 0
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        r, _, _ = select.select([fd], [], [], 0.2)
        if r:
            try:
                data = os.read(fd, 4096)
            except OSError:
                data = b""
            if not data:
                break
            out += data
        text = out.decode(errors="replace")
        hit = next((k for k, p in PROMPTS.items() if p in text[mark:]), None)
        if hit:
            seen.append(hit)
            mark = len(text)
            want, val = todo.pop(0) if todo else ("(none)", "")
            if want != hit:
                seen.append(f"!expected {want}")
            os.write(fd, (val + "\n").encode())
    else:
        os.kill(pid, signal.SIGKILL)
        seen.append("!timeout")
    _, status = os.waitpid(pid, 0)
    os.close(fd)
    if _a.show:
        print(f"---- {w.name}: terminal ----\n{out.decode(errors='replace').rstrip()}\n---- exit "
              f"{os.waitstatus_to_exitcode(status)}", flush=True)  # fmt: skip
    return os.waitstatus_to_exitcode(status), out.decode(errors="replace"), seen


def events(w: Path) -> list[dict]:
    return [json.loads(x) for x in (w / "tg/events.jsonl").read_text().splitlines() if x.strip()]


def docker_log(w: Path) -> str:
    return (w / "docker.log").read_text() if (w / "docker.log").exists() else ""


def leaks(w: Path, text: str, secrets: list[str]) -> list[str]:
    """Secret values found in the terminal text, docker's argv log, or any file of the session dir except the account's
    own api config copy."""
    blob = text + docker_log(w)
    for p in (w / "hub/secrets/voice").iterdir():
        if p.is_file() and p.name != "voice-tg.session":
            blob += p.read_text(errors="replace")
    return [s for s in secrets if s in blob]


def no_waiter(w: Path) -> bool:
    for p in Path("/proc").iterdir():
        if p.name.isdigit():
            try:
                cl = (p / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
            except OSError:
                continue
            if str(w) in cl and " _wait " in cl + " ":
                return False
    return True


def wait_no_waiter(w: Path, secs: float = 8) -> bool:
    end = time.monotonic() + secs
    while time.monotonic() < end and not no_waiter(w):
        time.sleep(0.1)
    return no_waiter(w)


def one_connection(evs: list[dict]) -> tuple[bool, str]:
    """The code request and every sign-in / password check ran on ONE client (instance and pid), with no disconnect
    of it in between; the main session's clients never overlapped; none was connected at the restart."""
    sends = [e for e in evs if e["ev"] == "send_code"]
    if len(sends) != 1:
        return False, f"{len(sends)} code requests"
    inst = sends[0]["inst"]
    signs = [e for e in evs if e["ev"] in ("sign_in", "check_password")]
    if not signs or any(e["inst"] != inst for e in signs):
        return False, "a sign-in on another connection"
    i0 = evs.index(sends[0])
    i1 = max(evs.index(e) for e in signs)
    if any(e["ev"] == "disconnect" and e["inst"] == inst for e in evs[i0:i1]):
        return False, "the code's connection dropped before the sign-in"
    open_ = set()
    for e in evs:
        if e["ev"] == "connect" and e["name"] == "voice-tg":
            if open_:
                return False, "two clients on the session at once"
            open_.add(e["inst"])
        elif e["ev"] == "disconnect":
            open_.discard(e["inst"])
        elif e["ev"] == "restart" and (open_ or e["waiters"]):
            return False, "a client or the login waiter still ran at the restart"
    return True, ""


def session_ok(w: Path) -> bool:
    s = w / "hub/secrets/voice/voice-tg.session"
    return s.is_file() and stat.S_IMODE(s.stat().st_mode) == 0o600 and s.read_text().startswith("authorized")


def leftovers(w: Path) -> list[str]:
    d = w / "hub/secrets/voice"
    return sorted(p.name for p in d.iterdir() if p.name.startswith("voice-tg-login") and p.suffix in (
        ".session", ".in", ".pw", ".tmp"))  # fmt: skip


# ------------------------------------------------------------------ scenarios
def s_refusals() -> None:
    """Every guard stops before anything runs in the container (or before the login, for the checks that need it)."""
    cases = [
        ("switch file missing", lambda w: (w / "home/.voice-relay-off").unlink(), "switched on", False),
        ("brain relay running", lambda w: ctl(w, top="ARGS\npython -m voice_relay.server serve --tg-calls\n"),
         "own relay is running", False),
        ("kit changed", lambda w: (w / "kit/voice/live.json").write_text("{}\n"), "the kit changed", False),
        ("container down", lambda w: ctl(w, running=False), "not installed or not running", False),
        ("relay held", lambda w: (w / "hub/voice/state/voiceloader-status.json").write_text(
            json.dumps({"state": "held", "release": None})), "not running (held", True),
        ("release tampered", lambda w: (w / f"releases/{RID}/voice/app/voice_relay/userbot.py").write_text("x=1\n"),
         "fails the loader's check", True),
        ("session present", lambda w: (w / "hub/secrets/voice/voice-tg.session").write_text("authorized\n"),
         "a session is already there", True),
    ]  # fmt: skip
    for name, prep, msg, exec_ok in cases:
        w = world("refuse-" + name.replace(" ", "-"))
        prep(w)
        rc, out, seen = paste(w, [])
        log = docker_log(w)
        execs = [x for x in log.splitlines() if x.startswith('["exec"')]
        step(f"refuses: {name}", rc == 1 and msg in out and not seen and (exec_ok or not execs)
             and not any('"login"' in x for x in execs) and json.loads((w / "docker.json").read_text()).get(
                 "restarts", 0) == 0 and not events(w), f"rc {rc}, seen {seen}, out {out[-300:]!r}")  # fmt: skip


def s_old_release() -> None:
    w = world("old-release", old_release=True)
    rc, out, seen = paste(w, [])
    step(
        "refuses a release without the relogin step (deploy first)",
        rc == 1 and "has no relogin step" in out and not events(w) and not seen,
        f"rc {rc}, {out[-300:]!r}",
    )


def s_login() -> None:
    """Phone in the api config, no 2FA: one wrong code, then the right one; harden; one restart; bridge on."""
    w = world("login")
    rc, out, seen = paste(w, [("code", "11111"), ("code", CODE)])
    evs = events(w)
    step("login: exit 0 and the paste's OK line", rc == 0 and "call bridge on" in out, f"rc {rc}, {out[-400:]!r}")
    step("login: prompts = code, code (no phone prompt: it is in the api config)", seen == ["code", "code"], str(seen))
    step(
        "login: a wrong code keeps the same login waiting",
        "wrong code" in out and [e["ok"] for e in evs if e["ev"] == "sign_in"] == [False, True],
        str([e for e in evs if e["ev"] == "sign_in"]),
    )
    ok, why = one_connection(evs)
    step("login: one connection from the code request to the sign-in; no client at the restart", ok, why)
    step(
        "login: the login waiter ran detached (not the CLI's pid) and is gone",
        wait_no_waiter(w) and {e["pid"] for e in evs if e["ev"] == "send_code"}.isdisjoint({os.getpid()}),
        "",
    )
    step(
        "login: session in place (0600, authorized), no login leftovers",
        session_ok(w) and not leftovers(w),
        str(leftovers(w)),
    )
    st = json.loads((w / "docker.json").read_text())
    step("login: restarted once, after the login; bridge on (the relay's client is the last one)",
         st.get("restarts") == 1 and st["bridge"] == "on" and [(e["ev"], e.get("inst")) for e in evs][-2:] == [
             ("restart", None), ("connect", "relay")], str(st))  # fmt: skip
    step("login: harden ran on the new session (contacts only, P2P never, owner contact) before the restart",
         [e["ev"] for e in evs if e["ev"] in ("set_privacy", "add_contact")] == ["set_privacy"] * 2 + ["add_contact"],
         "")  # fmt: skip
    step("login: phone right; code, phone, api_hash never on argv, screen or in the session dir's files",
         all(e.get("phone_ok") for e in evs if e["ev"] == "send_code") and not leaks(w, out, [CODE, PHONE_DIGITS,
         API_HASH, "+41 79"]), str(leaks(w, out, [CODE, PHONE_DIGITS, API_HASH])))  # fmt: skip


def s_twofa() -> None:
    """No phone in the api config, 2FA on: phone prompt, code, a wrong password, the right one."""
    w = world("twofa", phone_in_creds=False, password=PW)
    rc, out, seen = paste(w, [("phone", PHONE), ("code", CODE), ("password", "wrong horse"), ("password", PW)])
    evs = events(w)
    step("2fa: exit 0, bridge on", rc == 0 and "call bridge on" in out, f"rc {rc}, {out[-400:]!r}")
    step("2fa: prompts = phone, code, password, password", seen == ["phone", "code", "password", "password"], str(seen))
    step("2fa: password checks on the code's connection (wrong, then right)",
         [e["ok"] for e in evs if e["ev"] == "check_password"] == [False, True] and one_connection(evs)[0],
         one_connection(evs)[1])  # fmt: skip
    step(
        "2fa: no password file left; password and code never on argv, screen or in files",
        not leftovers(w) and not leaks(w, out, [PW, CODE, API_HASH]),
        str(leaks(w, out, [PW, CODE])),
    )
    step("2fa: the typed phone went in through stdin, never argv",
         PHONE_DIGITS not in docker_log(w) and PHONE not in docker_log(w) and
         all(e.get("phone_ok") for e in evs if e["ev"] == "send_code"), "")  # fmt: skip
    step("2fa: session in place, restarted once", session_ok(w) and json.loads((w / "docker.json").read_text())[
        "restarts"] == 1, "")  # fmt: skip


def s_expired() -> None:
    """The code expires: the waiter ends, the next try finds no login waiting: no session, no restart, nothing left."""
    w = world("expired", expired=True)
    rc, out, seen = paste(w, [("code", CODE), ("code", CODE)])
    st = json.loads((w / "docker.json").read_text())
    step("expired: exit 1, not logged in, nothing restarted", rc == 1 and "not logged in" in out and
         not st.get("restarts") and st["bridge"] == NO_SESSION, f"rc {rc}, {out[-400:]!r}")  # fmt: skip
    step("expired: no session, no login files, no waiter", not (w / "hub/secrets/voice/voice-tg.session").exists()
         and not leftovers(w) and wait_no_waiter(w), str(leftovers(w)))  # fmt: skip


def s_bridge_refuses() -> None:
    """Logged in, but the relay still refuses after the restart (here: the session the stand-in sees is not 0600):
    the paste says so and exits 1 instead of claiming OK."""
    w = world("bridge-refuses")
    d = json.loads((w / "docker.json").read_text())
    d["bridge"] = "refused: the session file must be 0600"
    (w / "docker.json").write_text(json.dumps(d))
    real = (w / "bin/docker").read_text()
    (w / "bin/docker").write_text(real.replace('ctl["bridge"] = "on" if ok', 'ctl["bridge"] = "on" if False'))
    rc, out, _ = paste(w, [("code", CODE)])
    step(
        "bridge still refused after the restart: exit 1, the bridge's words shown, no OK line",
        rc == 1 and "must be 0600" in out and "call bridge on" not in out,
        f"rc {rc}, {out[-300:]!r}",
    )


SCENARIOS = {"refusals": s_refusals, "old-release": s_old_release, "login": s_login, "twofa": s_twofa,
             "expired": s_expired, "bridge": s_bridge_refuses}  # fmt: skip


def main() -> int:
    os.environ["ASSISTANT_TEST"] = "1"
    ROOT.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    try:
        for name, fn in SCENARIOS.items():
            if not ONLY or name in ONLY:
                fn()
    finally:
        for p in Path("/proc").iterdir():  # a scenario that died early leaves its login waiter behind (only those)
            if p.name.isdigit():
                try:
                    cl = (p / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
                except OSError:
                    continue
                if str(ROOT) in cl and " _wait " in cl + " " and int(p.name) != os.getpid():
                    try:
                        os.kill(int(p.name), signal.SIGKILL)
                    except OSError:
                        pass
    n = len(FAILED)
    print(f"{'all steps passed' if not n else f'{n} step(s) failed: ' + ', '.join(FAILED)} "
          f"({time.monotonic() - t0:.0f} s)")  # fmt: skip
    return 1 if n else 0


if __name__ == "__main__":
    sys.exit(main())
