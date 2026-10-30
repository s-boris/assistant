#!/usr/bin/env python3
"""phoneloader: runs the phone gateway of the brain-built hub release that RELEASES/CURRENT names (v2 Phase 3 P3-8).
Python stdlib only, one file: the twin of voiceloader.py. The owner's phone paste copies it into HUB_ROOT/loader
(root-owned) and claude-hub-phone mounts it read-only; the brain changes releases, never this loader.

  python3 -I -B phoneloader.py --releases /srv/releases --state /srv/phone/state --config /srv/phone/config/live.json
  python3 -I -B phoneloader.py --check-release RID      verify only (exit 0 ok, 1 not)

CURRENT holds "<release id> <attempt>". Every --poll seconds the loader reads it; a release it runs must carry
release.json (format 1, its own id, entry.phone = phone/run.py, every listed phone/ file a regular file inside the
release with its sha256, no symlinks). It runs `python3 -I -B <release>/phone/run.py serve` (the gateway as copied
from adapters/phone, the app protocol schemas and the lock-screen filter, plus its pinned wheels in phone/vendor),
stdin closed, cwd phone/, with a fixed environment: the config's "env" (non-secret PHONEGW_* values), then the
container paths (state, key dir, gateway config, hub url and token) which always win.
The gateway's own config (config.json: allowlist, front, public route, routines, quiet hours ...) lives in
<state>/gw/config.json, which the gateway itself rewrites (`phone trip`, `front on|off`). The first start seeds it
from the copy the paste made of the brain's config (<config dir>/gw.json; the owner's values, never logged); every
start then lays the live config's "gw" keys over it (hub mode: listen addresses, the voice link, no plugin inbox).
Healthy = still running --healthy-after seconds after the start: that release becomes last_good. A release that fails
to verify or exits before it is healthy is marked bad for that CURRENT line and the last good one runs again; a
running gateway that exits is restarted with backoff (2 s .. 60 s); 3 exits within 10 min mark a release bad, except
the last good one (retried every 60 s). Exit 2 (config/usage) and 7 (switch file) stop the restarts until CURRENT
changes.
One gateway per device key set: the app's mTLS pairing (CA, server and public keys) must answer from one place, and a
second gateway would race the first on pushes and acks. So the loader starts nothing while the hub's hold is set, and
the paste starts this container only after the in-container gateway is stopped and switched off.
The brain's hold (`hub phone stop`): before every step the loader posts its state to the hub (POST /phone/hold/seen,
the config's hub.url + hub.token_file, 3 s) and gets the flag back. Held = SIGTERM to the gateway (SIGKILL after 7 s),
nothing started, not even the last good one; the answer is kept in phoneloader.json, so a restart while the hub is
away stays down. No hub answer and no kept hold = not held (the paste's own checks guard the first start).
State: <state>/phoneloader.json (last_good, bad, hold); status <state>/phoneloader-status.json {state: starting|
running|down|idle|held, release, pid, since, why, ts} (the healthcheck reads it; rewritten on change and every 30 s).
Log: one JSON line per event on stdout (ids, reasons, codes; never config, env, key or token values).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

FORMAT = 1
RID = re.compile(r"^[0-9a-f]{7,40}$")
ATTEMPT = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
SEGMENT = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")
ENTRY = "phone/run.py"
STOP_CODES = {2: "config or usage error", 7: "switch file present (phone.off)"}
RESTART_WINDOW, RESTARTS_BAD, LAST_GOOD_RETRY, STOP_WAIT, BAD_KEEP, HOLD_ASK = 600.0, 3, 60.0, 7.0, 20, 3.0
ENV_KEY = re.compile(r"^PHONEGW_[A-Z0-9_]{1,64}$")
# container paths: always win over the config and the owner's env file
FIXED = {
    "PHONEGW_STATE": "{state}/gw",  # phonegw.db, gw.log, pairing state, geo cache, shares (hub appdata)
    "PHONEGW_SECRETS": "/srv/phone/secrets",  # ca/server/public keys + internal.token (the brain CLI's own token)
    "PHONEGW_CONFIG": "{state}/gw/config.json",  # the gateway rewrites it; seeded from <config dir>/gw.json
    "PHONEGW_INBOX": "{state}/gw/outbox",  # notes the hub mode did not ship yet (never the brain's ~/inbox)
    "PHONEGW_PROTOCOL": "{release}/phone/app/protocol",  # the app's JSON schemas (snapshot in the release)
    "PHONEGW_FILTER": "{release}/phone/app/answer_filter.py",  # the lock-screen linter's filter (from adapters/voice)
    "PHONEGW_USAGE_LIB": "",  # no usage lib here: the app's analytics go to the brain as phone.usage events
    "PHONEGW_HUB_MODE": "1",
    "PHONEGW_HUB_URL": "{hub_url}",
    "PHONEGW_HUB_TOKEN_FILE": "{hub_token}",
    "PHONEGW_HUB_GW_URL": "{gw_url}",
}


def log(event: str, **kw: object) -> None:
    print(json.dumps({"ts": round(time.time()), "event": event, **kw}), flush=True)


def parse_current(text: str) -> tuple[str, str] | None:
    parts = text.split()
    if not parts or len(parts) > 2 or not RID.match(parts[0]) or (len(parts) == 2 and not ATTEMPT.match(parts[1])):
        return None
    return parts[0], " ".join(parts)


def _sha(p: str) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def verify_release(releases: str, rid: str) -> str | None:
    """None when RELEASES/<rid> carries a complete, untouched phone tree; else why not."""
    if not RID.match(rid):
        return "bad id"
    root = os.path.realpath(releases)
    d = os.path.join(root, rid)
    try:
        with open(os.path.join(d, "release.json"), encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError):
        return "no readable release.json"
    if not isinstance(meta, dict) or meta.get("format") != FORMAT or meta.get("rid") != rid:
        return "release.json: wrong format or id"
    files = meta.get("files") if isinstance(meta.get("files"), dict) else {}
    if (meta.get("entry") or {}).get("phone") != ENTRY or ENTRY not in files:
        return "release.json: no phone entry (a release without the gateway)"
    n = 0
    for rel, info in files.items():
        if not rel.startswith("phone/"):
            continue
        segs = rel.split("/")
        if not all(SEGMENT.match(s) and s not in (".", "..") for s in segs):
            return f"release.json: bad path {rel[:80]}"
        p = os.path.join(d, *segs)
        q = d
        for s in segs:
            q = os.path.join(q, s)
            if os.path.islink(q):
                return f"{rel}: a symlink"
        if not os.path.isfile(p):
            return f"{rel}: missing"
        if _sha(p) != (info or {}).get("sha256"):
            return f"{rel}: sha256 mismatch"
        n += 1
    return None if n else "no phone files"


def read_env_file(path: str) -> dict[str, str]:
    """PHONEGW_* KEY=VALUE lines of an env file (comments, export and quotes allowed); other keys
    dropped. Never logged."""
    out: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return out
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        k, sep, v = line.partition("=")
        k, v = k.strip(), v.strip()
        if not sep or not ENV_KEY.match(k):
            continue
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        out[k] = v
    return out


class Loader:
    def __init__(self, releases: str, state: str, config: str, poll: float = 5.0, healthy: float = 20.0,
                 python: str = sys.executable) -> None:  # fmt: skip
        self.releases, self.state, self.config, self.poll, self.healthy = releases, state, config, poll, healthy
        self.python = python
        os.makedirs(state, mode=0o700, exist_ok=True)
        self.st = self._load()
        self.child: subprocess.Popen | None = None
        self.running: dict | None = None
        self.exits: list[float] = []
        self.stopped_for: str | None = None
        self.retry_at = 0.0
        self.backoff = 2.0
        self.status: dict = {"state": "idle"}
        self.stopping = False

    # ---------------------------------------------------------------- state
    def _load(self) -> dict:
        try:
            with open(os.path.join(self.state, "phoneloader.json"), encoding="utf-8") as f:
                d = json.load(f)
            h = d.get("hold") if isinstance(d.get("hold"), dict) else None
            return {"last_good": d.get("last_good") if isinstance(d.get("last_good"), str) else None,
                    "bad": d.get("bad") if isinstance(d.get("bad"), dict) else {},
                    "hold": {"why": str(h.get("why") or ""), "ts": h.get("ts")} if h else None}  # fmt: skip
        except (OSError, ValueError, AttributeError):
            return {"last_good": None, "bad": {}, "hold": None}

    def save(self) -> None:
        bad = sorted(self.st["bad"].items(), key=lambda kv: kv[1].get("ts", 0))
        self.st["bad"] = dict(bad[-BAD_KEEP:])
        _atomic(os.path.join(self.state, "phoneloader.json"), json.dumps(self.st))

    def set_status(self, state: str, why: str = "") -> None:
        rid = (self.running or {}).get("rid")
        if (self.status.get("state"), self.status.get("release"), self.status.get("why")) != (state, rid, why):
            self.status = {"state": state, "release": rid, "since": round(time.time()), "why": why[:200]}
            log("status", state=state, release=rid, why=why[:200])
        pid = self.child.pid if self.child else None
        _atomic(os.path.join(self.state, "phoneloader-status.json"),
                json.dumps({**self.status, "pid": pid, "ts": round(time.time())}))  # fmt: skip

    def mark_bad(self, line: str, why: str) -> None:
        self.st["bad"][line] = {"why": why[:120], "ts": round(time.time())}
        self.save()
        log("bad", attempt=line, why=why[:120])

    def cfg(self) -> dict:
        try:
            with open(self.config, encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    # ---------------------------------------------------------------- the hub's hold
    def ask_hub(self) -> dict | None:
        hub = self.cfg().get("hub") or {}
        url, tf = hub.get("url"), hub.get("token_file")
        if not isinstance(url, str) or not re.match(r"^https?://[^\s/?#]+", url) or not isinstance(tf, str):
            return None
        try:
            with open(tf, encoding="utf-8") as f:
                tok = f.read().strip()
        except OSError:
            return None
        if not tok:
            return None
        body = json.dumps({"state": self.status.get("state", "idle"), "release": self.status.get("release"),
                           "why": str(self.status.get("why") or "")[:200]}).encode()  # fmt: skip
        req = urllib.request.Request(
            url.rstrip("/") + "/phone/hold/seen",
            data=body,
            method="POST",
            headers={"authorization": f"Bearer {tok}", "content-type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=HOLD_ASK) as r:  # noqa: S310 (config url, http only)
                d = json.loads(r.read(65536) or b"{}")
        except (OSError, ValueError, urllib.error.URLError):
            return None
        if not isinstance(d, dict) or not isinstance(d.get("held"), bool):
            return None
        return {"held": d["held"], "why": str(d.get("why") or "")[:200], "ts": d.get("ts")}

    def hold_now(self) -> dict | None:
        got = self.ask_hub()
        if got and got["held"]:
            h = {"why": got["why"], "ts": got["ts"]}
            if self.st["hold"] != h:
                self.st["hold"] = h
                self.save()
                log("hold", ts=h["ts"])
        elif got and self.st["hold"]:
            self.st["hold"] = None
            self.save()
            self.stopped_for, self.retry_at, self.exits, self.backoff = None, 0.0, [], 2.0
            log("hold_released")
        return self.st["hold"]

    def hold_down(self, h: dict) -> None:
        had = self.child is not None
        if had:
            log("hold_stop", release=(self.running or {}).get("rid"), pid=self.child.pid)
            self.stop_child()
        self.set_status("held", f"held by the brain{': ' + h['why'] if h.get('why') else ''}")
        if had:
            self.ask_hub()  # report "held" at once (`hub phone stop` waits for it)

    # ---------------------------------------------------------------- the gateway process
    def env(self, rid: str) -> dict[str, str]:
        c = self.cfg()
        hub = c.get("hub") or {}
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "TZ": os.environ.get("TZ", "UTC"),
            "HOME": os.environ.get("HOME", "/tmp"),
            "LANG": "C.UTF-8",
            "HUB_RELEASE": rid,
            "PHONEGW_HUB_LOADER": "1",
        }
        for k, v in (c.get("env") or {}).items():
            if ENV_KEY.match(str(k)) and isinstance(v, (str, int, float, bool)):
                env[k] = str(v).lower() if isinstance(v, bool) else str(v)
        ef = c.get("env_file")
        if isinstance(ef, str) and ef:
            env.update(read_env_file(ef))
        vals = {"state": self.state, "hub_url": str(hub.get("url") or ""),
                "hub_token": str(hub.get("token_file") or ""),
                "gw_url": str(hub.get("gw_url") or ""), "config_dir": os.path.dirname(self.config),
                "release": os.path.join(os.path.realpath(self.releases), rid)}  # fmt: skip
        for k, v in FIXED.items():
            env[k] = v.format(**vals)
        for k, v in (c.get("paths") or {}).items():  # tests and the rehearsal: other container paths
            if k in FIXED and isinstance(v, str):
                env[k] = v
        return env

    def seed_config(self, dest: str) -> str:
        """The gateway's config.json: seeded once from <config dir>/gw.json, then the live config's "gw" keys laid
        over it at every start. -> "" or why it cannot start (values are never logged)."""
        over = self.cfg().get("gw") or {}
        if not isinstance(over, dict):
            return "live.json: gw is not an object"
        try:
            with open(dest, encoding="utf-8") as f:
                cur = json.load(f)
        except FileNotFoundError:
            try:
                with open(os.path.join(os.path.dirname(self.config), "gw.json"), encoding="utf-8") as f:
                    cur = json.load(f)
            except (OSError, ValueError):
                return "no gateway config to seed from (gw.json)"
            log("config_seeded")
        except (OSError, ValueError):
            return "the gateway config is unreadable"
        if not isinstance(cur, dict):
            return "the gateway config is not an object"
        for k, v in over.items():
            if isinstance(v, dict) and isinstance(cur.get(k), dict):
                cur[k] = {**cur[k], **v}
            else:
                cur[k] = v
        _atomic(dest, json.dumps(cur, indent=1, sort_keys=True))
        os.chmod(dest, 0o600)
        return ""

    def start(self, rid: str, line: str) -> bool:
        why = verify_release(self.releases, rid)
        if why:
            if rid != self.st["last_good"]:
                self.mark_bad(line, f"verify: {why}")
            log("verify_failed", release=rid, why=why)
            return False
        rdir = os.path.join(os.path.realpath(self.releases), rid)
        env = self.env(rid)
        for k in ("PHONEGW_STATE", "PHONEGW_INBOX"):
            os.makedirs(env[k], mode=0o700, exist_ok=True)
        why = self.seed_config(env["PHONEGW_CONFIG"])
        if why:
            self.set_status("down", f"exit 2: {why}")
            self.stopped_for = line
            log("config_failed", release=rid, why=why)
            return True
        if os.path.exists(os.path.join(os.path.dirname(self.config), "phone.off")):
            self.set_status("down", f"exit 7: {STOP_CODES[7]}")
            self.stopped_for = line
            return True
        args = self.cfg().get("args")  # the rehearsal only (the paste writes none)
        args = args if isinstance(args, list) and all(isinstance(x, str) for x in args) else ["serve"]
        self.child = subprocess.Popen([self.python, "-I", "-B", os.path.join(rdir, ENTRY), *args],
                                      cwd=os.path.join(rdir, "phone"), env=env, stdin=subprocess.DEVNULL)  # fmt: skip
        self.running = {"rid": rid, "line": line, "started": time.monotonic(), "healthy": False}
        log("start", release=rid, pid=self.child.pid)
        self.set_status("starting")
        return True

    def stop_child(self) -> None:
        c, self.child, self.running = self.child, None, None
        if c is None or c.poll() is not None:
            return
        c.send_signal(signal.SIGTERM)
        try:
            c.wait(STOP_WAIT)
        except subprocess.TimeoutExpired:
            c.kill()
            c.wait(5)

    def reap(self) -> None:
        c, r = self.child, self.running
        if c is None or r is None:
            return
        code = c.poll()
        if code is None:
            return
        self.child, self.running = None, None
        log("exit", release=r["rid"], code=code)
        if self.stopping:
            return
        if code in STOP_CODES:
            self.stopped_for = r["line"]
            self.set_status("down", f"exit {code}: {STOP_CODES[code]}")
            return
        now = time.monotonic()
        self.exits = [t for t in self.exits if now - t < RESTART_WINDOW] + [now]
        if not r["healthy"] and r["rid"] != self.st["last_good"]:
            self.mark_bad(r["line"], f"exited before healthy (code {code})")
        elif len(self.exits) >= RESTARTS_BAD and r["rid"] != self.st["last_good"]:
            self.mark_bad(r["line"], f"{RESTARTS_BAD} exits in {RESTART_WINDOW / 60:g} min")
        wait = LAST_GOOD_RETRY if r["rid"] == self.st["last_good"] and len(self.exits) >= RESTARTS_BAD else self.backoff
        self.backoff = min(self.backoff * 2, 60.0)
        self.retry_at = now + wait
        self.set_status("down", f"exit {code}; retry in {round(wait)} s")

    def step(self) -> None:
        self.reap()
        hold = self.hold_now()
        if hold:
            self.hold_down(hold)
            return
        cur = None
        try:
            with open(os.path.join(self.releases, "CURRENT"), encoding="utf-8") as f:
                cur = parse_current(f.read())
        except OSError:
            pass
        want = cur if cur and cur[1] not in self.st["bad"] else None
        r = self.running
        if r and self.child and not r["healthy"] and time.monotonic() - r["started"] >= self.healthy:
            r["healthy"], self.backoff = True, 2.0
            if self.st["last_good"] != r["rid"]:
                self.st["last_good"] = r["rid"]
                self.save()
            log("healthy", release=r["rid"])
        if self.child and self.running:
            if want and want[0] != self.running["rid"]:
                why = verify_release(self.releases, want[0])
                if why is None:
                    log("switch", **{"from": self.running["rid"], "to": want[0]})
                    self.stopped_for = None
                    self.stop_child()
                    self.start(*want)
                    return
                if want[1] not in self.st["bad"]:
                    self.mark_bad(want[1], f"verify: {why}")
            self.set_status("running" if self.running and self.running["healthy"] else "starting")
            return
        if self.stopped_for:
            if cur and cur[1] != self.stopped_for:
                log("retry_after_stop", attempt=cur[1])
                self.stopped_for = None
            else:
                self.set_status(self.status.get("state", "down"), str(self.status.get("why") or ""))
                return
        if time.monotonic() < self.retry_at:
            self.set_status("down", str(self.status.get("why") or ""))
            return
        if want and self.start(*want):
            return
        lg = self.st["last_good"]
        if lg and (not want or want[0] != lg) and self.start(lg, f"{lg} last_good"):
            return
        self.set_status("idle", "no runnable release (CURRENT bad, no last good)" if cur else "no CURRENT yet")

    def run(self) -> int:
        log("loader_start", last_good=self.st["last_good"])

        def stop(*_a: object) -> None:
            self.stopping = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        beat = 0.0
        while not self.stopping:
            try:
                self.step()
            except Exception as e:  # noqa: BLE001 (a step never kills the loader; the type only)
                log("step_error", error=type(e).__name__)
            end = time.monotonic() + self.poll
            while not self.stopping and time.monotonic() < end:
                time.sleep(0.2)
                self.reap()
            beat += self.poll
            if beat >= 30:
                beat = 0.0
                self.set_status(self.status.get("state", "idle"), str(self.status.get("why") or ""))
        log("loader_stop")
        self.stop_child()
        self.set_status("idle", "stopped")
        return 0


def _atomic(path: str, text: str) -> None:
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text + "\n")
    os.replace(tmp, path)


def main(argv: list[str]) -> int:
    a = {"--releases": "/srv/releases", "--state": "/srv/phone/state", "--config": "/srv/phone/config/live.json",
         "--poll": "5", "--healthy-after": "20", "--check-release": ""}  # fmt: skip
    it = iter(argv)
    for k in it:
        if k not in a:
            print(f"phoneloader: unknown option {k}", file=sys.stderr)
            return 2
        v = next(it, None)
        if v is None:
            print(f"phoneloader: {k}: a value is missing", file=sys.stderr)
            return 2
        a[k] = v
    if a["--check-release"]:
        why = verify_release(a["--releases"], a["--check-release"])
        print(why or "ok")
        return 1 if why else 0
    if not os.path.exists(a["--config"]):
        log("config_missing", config=a["--config"])
    return Loader(a["--releases"], a["--state"], a["--config"], float(a["--poll"]), float(a["--healthy-after"])).run()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
