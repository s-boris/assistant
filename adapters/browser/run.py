#!/usr/bin/env python3
"""claude-hub-browser's runner: the owner's headed Chrome in its own container. Stdlib only.

browserloader (ops/hub/browserloader.py) runs this file from the verified release (browser/run.py, browser/app on
sys.path). It starts and keeps up:
  Xvfb (DISPLAY, -nolisten tcp) + fluxbox; Google Chrome with the profile on the hub volume, its CDP on 127.0.0.1
  only (CDP_PORT, never on a network); browsersvc (browser/app/browsersvc) on svc.bind:svc.port: the ONLY CDP way in
  from the brain or the hub, sealed (no cookie dumps) and gating committing clicks with this container's own hub
  token (hubgate); the control API on ctl.port (bearer browser_ctl_token): status, the owner view on/off, a display
  screenshot, a Chrome restart; the owner view on demand: x11vnc (loopback, his VNC password) + noVNC/websockify on
  view.port, switched off at its deadline.
State: STATE/status.json (the healthcheck reads it), STATE/svc/ (browsersvc's log), STATE/logs/*.log.

  run.py --config live.json --state DIR
Control API (JSON; Authorization: Bearer <ctl token>):
  GET  /status            chrome up/version, pids, view {on, until, conns}, svc {mode, up}, display
  POST /view {"on": true, "minutes": 1-240} | {"on": false}
  GET  /audit?hours=N     browsersvc's counts (commits with / without a token, denials, would-denials; no labels)
  GET  /screen            the whole display as PNG (what he would see in the view)
  POST /chrome/restart    clean stop (CDP Browser.close, then SIGTERM) and start
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "app"))

DEFAULTS: dict[str, Any] = {
    "display": ":99",
    "size": "1920x1080",
    "chrome": "/usr/bin/google-chrome-stable",
    "profile": "/srv/browser/profile",
    "cdp_port": 9222,
    "lang": "de-CH",
    "svc": {"port": 9223, "bind": "0.0.0.0", "advertise": "claude-hub-browser:9223", "mode": "enforce"},
    "ctl": {"port": 9224, "bind": "0.0.0.0", "token_file": "/run/secrets/browser_ctl_token"},
    "view": {
        "port": 6080,
        "bind": "0.0.0.0",
        "vnc_port": 5900,
        "password_file": "/run/secrets/browser_vnc_pass",
        "web": "/usr/share/novnc",
        "max_minutes": 240,
    },  # fmt: skip
    "webagent": {"lib": "/srv/webagent/lib", "state": "/srv/webagent/state"},
    "hub": {"url": "http://claude-hub:8700", "token_file": "/run/secrets/token_browser"},
}
LOCKS = ("SingletonLock", "SingletonSocket", "SingletonCookie")


def merged(cfg: dict[str, Any]) -> dict[str, Any]:
    out = json.loads(json.dumps(DEFAULTS))
    for k, v in cfg.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k].update(v)
        else:
            out[k] = v
    return out


def log(event: str, **kw: Any) -> None:
    print(json.dumps({"ts": round(time.time(), 3), "event": event, **kw}), flush=True)


def chrome_args(c: dict[str, Any]) -> list[str]:
    w, h = c["size"].split("x")
    return [
        c["chrome"],
        f"--user-data-dir={c['profile']}",
        f"--remote-debugging-port={c['cdp_port']}",
        "--remote-debugging-address=127.0.0.1",
        "--no-first-run", "--no-default-browser-check", "--password-store=basic",
        "--no-sandbox", "--disable-dev-shm-usage", "--use-angle=gl", "--ignore-gpu-blocklist",
        f"--lang={c['lang']}", "--window-position=0,0", f"--window-size={w},{h}", "--start-maximized",
        "about:blank",
    ]  # fmt: skip


class Runner:
    def __init__(self, cfg: dict[str, Any], state: str) -> None:
        self.c, self.state = merged(cfg), state
        self.logs = os.path.join(state, "logs")
        os.makedirs(self.logs, mode=0o700, exist_ok=True)
        self.p: dict[str, subprocess.Popen[bytes] | None] = {}
        self.view_until = 0.0
        self.lock = threading.RLock()
        self.stopping = False
        self.version = ""
        self.started = time.time()
        self.svc_ok = False
        self.env = {**os.environ, "DISPLAY": self.c["display"], "LANG": "de_CH.UTF-8",
                    "LANGUAGE": "de_CH:de:en_US:en"}  # fmt: skip

    # ---------------------------------------------------------------------------------------------- processes
    def spawn(self, name: str, argv: list[str]) -> None:
        out = open(os.path.join(self.logs, f"{name}.log"), "ab")  # noqa: SIM115
        self.p[name] = subprocess.Popen(argv, stdout=out, stderr=out, stdin=subprocess.DEVNULL, env=self.env,
                                        start_new_session=True)  # fmt: skip
        log("start", proc=name, pid=self.p[name].pid)  # type: ignore[union-attr]

    def alive(self, name: str) -> bool:
        q = self.p.get(name)
        return q is not None and q.poll() is None

    def kill(self, name: str, wait: float = 5.0) -> None:
        q = self.p.pop(name, None)
        if q is None or q.poll() is not None:
            return
        q.terminate()
        try:
            q.wait(wait)
        except subprocess.TimeoutExpired:
            q.kill()
            q.wait(2)
        log("stop", proc=name)

    def cdp_up(self) -> bool:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.c['cdp_port']}/json/version", timeout=1.5) as r:
                v = json.loads(r.read())
            self.version = str(v.get("Browser") or self.version)
            return True
        except (OSError, ValueError):
            return False

    def x_up(self) -> bool:
        return os.path.exists(f"/tmp/.X11-unix/X{self.c['display'].lstrip(':')}")

    def start_x(self) -> None:
        if not self.alive("xvfb"):
            self.spawn("xvfb", ["Xvfb", self.c["display"], "-screen", "0", f"{self.c['size']}x24", "-dpi", "96",
                                "-nolisten", "tcp", "+extension", "RANDR"])  # fmt: skip
            for _ in range(50):
                if self.x_up():
                    break
                time.sleep(0.1)
        if not self.alive("wm") and shutil.which("fluxbox"):
            self.spawn("wm", ["fluxbox", "-display", self.c["display"]])

    def start_chrome(self) -> None:
        if self.alive("chrome"):
            return
        os.makedirs(self.c["profile"], mode=0o700, exist_ok=True)
        for n in LOCKS:  # only this container runs Chrome on this profile: a lock left by a crash is stale
            p = os.path.join(self.c["profile"], n)
            if os.path.lexists(p):
                os.unlink(p)
        self.spawn("chrome", chrome_args(self.c))
        for _ in range(60):
            if self.cdp_up():
                log("chrome up", version=self.version)
                return
            time.sleep(0.5)
        log("chrome slow", why="no CDP after 30 s")

    def stop_chrome(self) -> None:
        if self.alive("chrome") and self.cdp_up():
            with_close(int(self.c["cdp_port"]))
            q = self.p.get("chrome")
            for _ in range(40):
                if q is None or q.poll() is not None:
                    break
                time.sleep(0.25)
        self.kill("chrome", wait=8.0)

    # ---------------------------------------------------------------------------------------------- the view
    def view_on(self, minutes: int) -> None:
        v = self.c["view"]
        with self.lock:
            self.view_until = time.time() + 60 * max(1, min(int(minutes), int(v["max_minutes"])))
            if not self.alive("vnc"):
                pw = os.path.join(self.state, "vnc.pass")  # x11vnc wants a file it can read: a 0600 copy
                with open(v["password_file"], encoding="utf-8") as f:
                    word = f.read().strip()[:8]
                fd = os.open(pw, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as f:
                    f.write(word + "\n")
                self.spawn("vnc", ["x11vnc", "-display", self.c["display"], "-localhost", "-rfbport",
                                   str(v["vnc_port"]), "-passwdfile", pw, "-forever", "-shared", "-noxdamage",
                                   "-noxrecord", "-quiet"])  # fmt: skip
                time.sleep(0.8)
            if not self.alive("novnc"):
                self.spawn("novnc", ["websockify", "--web", v["web"], f"{v['bind']}:{v['port']}",
                                     f"127.0.0.1:{v['vnc_port']}"])  # fmt: skip
        log("view on", minutes=int(minutes))

    def view_off(self) -> None:
        with self.lock:
            self.view_until = 0.0
            self.kill("novnc", 2)
            self.kill("vnc", 2)

    def view(self) -> dict[str, Any]:
        on = self.alive("vnc") and self.alive("novnc")
        return {"on": on, "until": round(self.view_until) if on else None, "conns": ws_conns(self.logs)}

    # ---------------------------------------------------------------------------------------------- the service
    def start_svc(self) -> None:
        s = self.c["svc"]
        os.environ.update({"BROWSERSVC_BIND": s["bind"], "BROWSERSVC_ADVERTISE": s["advertise"],
                           "BROWSERSVC_SEAL": "1", "BROWSERSVC_PORT": str(s["port"]),
                           "BROWSERSVC_UPSTREAM": f"127.0.0.1:{self.c['cdp_port']}",
                           "BROWSERSVC_STATE": os.path.join(self.state, "svc")})  # fmt: skip
        import browsersvc as bs  # noqa: PLC0415  (browser/app, after the env above)
        import hubgate  # noqa: PLC0415

        mode = s["mode"] if s["mode"] in ("shadow", "enforce") else "enforce"
        g = hubgate.gate(bs, self.c, lambda: mode)
        svc = bs.Service(g, listen=int(s["port"]), up_addr=("127.0.0.1", int(self.c["cdp_port"])))

        def serve() -> None:
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(svc.start())
                self.svc_ok = True
                log("svc up", port=int(s["port"]), mode=mode)
                loop.run_forever()
            except Exception as e:  # the service is the only way in: no service = nothing reaches Chrome
                self.svc_ok = False
                log("svc failed", error=type(e).__name__)

        threading.Thread(target=serve, name="browsersvc", daemon=True).start()

    # ---------------------------------------------------------------------------------------------- status
    def status(self) -> dict[str, Any]:
        up = self.cdp_up()
        return {"ts": round(time.time(), 3), "chrome": "up" if up else "down", "version": self.version,
                "display": self.c["display"], "x": self.x_up(), "svc": {"up": self.svc_ok,
                "mode": self.c["svc"]["mode"], "port": int(self.c["svc"]["port"])}, "view": self.view(),
                "pids": {k: q.pid for k, q in self.p.items() if q is not None and q.poll() is None},
                "since": round(self.started)}  # fmt: skip

    def write_status(self) -> None:
        tmp = os.path.join(self.state, "status.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.status(), f)
        os.replace(tmp, os.path.join(self.state, "status.json"))

    def screen(self) -> bytes:
        xwd = subprocess.run(["xwd", "-display", self.c["display"], "-root", "-silent"], capture_output=True,
                             timeout=20, check=True)  # fmt: skip
        pnm = subprocess.run(["xwdtopnm"], input=xwd.stdout, capture_output=True, timeout=20, check=True)
        return subprocess.run(["pnmtopng"], input=pnm.stdout, capture_output=True, timeout=20, check=True).stdout

    # ---------------------------------------------------------------------------------------------- main loop
    def run(self) -> int:
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "stopping", True))
        signal.signal(signal.SIGINT, lambda *_: setattr(self, "stopping", True))
        self.start_x()
        self.start_chrome()
        self.start_svc()
        ctl = control(self)
        threading.Thread(target=ctl.serve_forever, name="ctl", daemon=True).start()
        last = 0.0
        while not self.stopping:
            with self.lock:
                if not self.alive("xvfb"):
                    self.start_x()
                if not self.alive("chrome"):
                    self.start_chrome()
                if self.view_until and time.time() > self.view_until:
                    log("view off", why="deadline")
                    self.view_off()
            if time.time() - last > 10:
                self.write_status()
                last = time.time()
            time.sleep(1)
        ctl.shutdown()
        self.view_off()
        self.stop_chrome()
        self.kill("wm", 2)
        self.kill("xvfb", 2)
        log("stopped")
        return 0


def with_close(port: int) -> None:
    """CDP Browser.close over the browser target (saves the session and cookies); best effort, 3 s."""
    import base64  # noqa: PLC0415
    import secrets  # noqa: PLC0415
    import socket  # noqa: PLC0415

    import browsersvc as bs  # noqa: PLC0415

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as r:
            ws = json.loads(r.read())["webSocketDebuggerUrl"]
        path = "/" + ws.split("/", 3)[3]
        s = socket.create_connection(("127.0.0.1", port), timeout=3)
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        s.sendall((f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                   f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())  # fmt: skip
        s.recv(4096)
        s.sendall(bs.frame(1, b'{"id":1,"method":"Browser.close"}', mask=True))
        s.settimeout(3)
        s.recv(4096)
        s.close()
    except (OSError, ValueError, KeyError, IndexError):
        pass


def ws_conns(logs: str) -> int:
    try:
        with open(os.path.join(logs, "novnc.log"), encoding="utf-8", errors="replace") as f:
            return sum(1 for line in f if "connecting to:" in line)
    except OSError:
        return 0


def control(r: Runner) -> ThreadingHTTPServer:
    tf = r.c["ctl"]["token_file"]

    class H(BaseHTTPRequestHandler):
        def _ok(self) -> bool:
            try:
                with open(tf, encoding="utf-8") as f:
                    want = f.read().strip()
            except OSError:
                want = ""
            got = self.headers.get("Authorization", "")
            if want and hmac.compare_digest(got.encode(), f"Bearer {want}".encode()):
                return True
            self._send(401, {"error": "unauthorized"})
            return False

        def _send(self, code: int, body: Any, ctype: str = "application/json") -> None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            if not self._ok():
                return
            if self.path == "/status":
                self._send(200, r.status())
            elif self.path.startswith("/audit"):
                import browsersvc as bs  # noqa: PLC0415

                q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                try:
                    hours = max(0.1, min(float((q.get("hours") or ["24"])[0]), 24.0 * 31))
                except ValueError:
                    return self._send(400, {"error": "hours"})
                self._send(200, bs.audit(hours))
            elif self.path == "/screen":
                try:
                    self._send(200, r.screen(), "image/png")
                except (OSError, subprocess.SubprocessError) as e:
                    self._send(500, {"error": "screen", "hint": type(e).__name__})
            else:
                self._send(404, {"error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802
            if not self._ok():
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
                a = json.loads(self.rfile.read(min(n, 4096)) or b"{}") if n else {}
            except ValueError:
                return self._send(400, {"error": "bad_json"})
            if self.path == "/view":
                if a.get("on"):
                    m = a.get("minutes", 30)
                    if not isinstance(m, int) or isinstance(m, bool) or not 1 <= m <= 240:
                        return self._send(400, {"error": "minutes: 1-240"})
                    try:
                        r.view_on(m)
                    except OSError as e:
                        return self._send(500, {"error": "view", "hint": type(e).__name__})
                else:
                    r.view_off()
                return self._send(200, r.view())
            if self.path == "/chrome/restart":
                with r.lock:
                    r.stop_chrome()
                    r.start_chrome()
                return self._send(200, {"chrome": "up" if r.cdp_up() else "down", "version": r.version})
            return self._send(404, {"error": "not_found"})

        def log_message(self, *a: Any) -> None:
            pass

    return ThreadingHTTPServer((r.c["ctl"]["bind"], int(r.c["ctl"]["port"])), H)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="browser/run.py")
    ap.add_argument("--config", required=True)
    ap.add_argument("--state", required=True)
    a = ap.parse_args(argv)
    with open(a.config, encoding="utf-8") as f:
        cfg = json.load(f)
    os.makedirs(a.state, mode=0o700, exist_ok=True)
    return Runner(cfg, a.state).run()


if __name__ == "__main__":
    sys.exit(main())
