"""browser (the CLI of his headed Chrome in claude-hub-browser) and browser-hub, offline.

browser hands every command (all but --help) to browser-hub beside its own file. The first tests run a copy of
browser beside a fake browser-hub (arguments, exit codes, a symlinked browser, help, a missing browser-hub). The rest
run it beside a copy of the repo's browser-hub against a fake claude-hub-browser on 127.0.0.1 (a ThreadingHTTPServer
in a thread): one server playing both the control API (BROWSER_HUB_CTL: status, restart, screen, view, audit; bearer
token from BROWSER_HUB_CTL_TOKEN) and the CDP gate (BROWSER_HUB_CDP: /json/*), plus the noVNC page `browser link`
checks. Every request is logged, so a command that went elsewhere fails its check.

Isolated: HOME is tmp_path/home, the token, the state dir (BROWSER_HUB_STATE) and the MCP config (CLAUDE_JSON) are
temp files, usage and profile-get beside the copy are fakes; the copies are staged from the repo (bin/lib/core and
bin/lib/browsersvc are the repo's packages), nothing from the live ~/bin."""

import json
import os
import secrets
import shutil
import socket
import stat
import subprocess
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
BROWSER = REPO / "scripts" / "browser"
HUB = REPO / "scripts" / "browser-hub"
MANAGED = "# managed: ~/opt/assistant — edit there, deploy with assistant land"
VNC = "/vnc.html?autoconnect=1&resize=scale&reconnect=1"


def flavours(d: Path) -> tuple[Path, Path]:
    """browser as in the repo (no managed header line) and as deployed (with it)."""
    lines = [ln for ln in BROWSER.read_text().splitlines(keepends=True) if not ln.startswith("# managed: ")]
    plain, managed = d / "browser.plain", d / "browser.managed"
    plain.write_text("".join(lines))
    managed.write_text(lines[0] + MANAGED + "\n" + "".join(lines[1:]))
    return plain, managed


def install(src: Path, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    dest.chmod(0o755)
    return dest


def script(path: Path, *lines: str) -> None:
    path.write_text("\n".join(("#!/bin/bash", *lines)) + "\n")
    path.chmod(0o755)


def run(argv, env, timeout=60):
    p = subprocess.run([str(a) for a in argv], env=env, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout.rstrip("\n"), p.stderr.rstrip("\n")


def closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# --- part 1: browser beside a fake browser-hub ---------------------------------------------------------------------


@pytest.fixture
def wrap(tmp_path):
    plain, managed = flavours(tmp_path)
    fake = tmp_path / "fake"
    fake.mkdir()
    b = install(managed, tmp_path / "wrap" / "browser")
    script(
        tmp_path / "wrap" / "browser-hub",
        'printf "%s\\n" "$@" > "$FAKE/hub.args"; echo "hub out"; echo "hub err" >&2; exit "${FAKE_RC:-0}"',
    )
    env = dict(os.environ, FAKE=str(fake), HOME=str(tmp_path / "home"))
    env.pop("FAKE_RC", None)
    return {"t": tmp_path, "b": b, "fake": fake, "env": env, "plain": plain, "args": fake / "hub.args"}


def test_hands_every_command_to_browser_hub(wrap):
    b, env, args = wrap["b"], wrap["env"], wrap["args"]
    rc, out, err = run([b, "open", "https://example.org/x?a=1&b=two words"], env)
    assert rc == 0
    assert args.read_text() == "open\nhttps://example.org/x?a=1&b=two words\n"  # verbatim, one each
    assert (out, err) == ("hub out", "hub err")  # stdout and stderr pass through
    for c in ("status", "ensure", "start", "restart", "stop", "screenshot", "url", "view on 5", "link", "update",
              "audit", "prep", "mcp status", "bogus"):  # fmt: skip
        args.unlink(missing_ok=True)
        run([b, *c.split()], env)
        assert args.read_text().split("\n")[:-1] == c.split(), c
    for want in (1, 2, 3):
        assert run([b, "status"], dict(env, FAKE_RC=str(want)))[0] == want


def test_symlinked_and_missing_hub(wrap):
    t, env, args = wrap["t"], wrap["env"], wrap["args"]
    (t / "link").mkdir()
    (t / "link" / "browser").symlink_to(wrap["b"])
    args.unlink(missing_ok=True)
    rc, _, _ = run([t / "link" / "browser", "url"], env)
    assert (rc, args.read_text()) == (0, "url\n")  # browser-hub beside the real file
    lone = install(wrap["plain"], t / "lone" / "browser")
    rc, out, err = run([lone, "status"], env)
    assert rc == 1
    assert f"browser: browser-hub is missing ({os.path.realpath(t / 'lone')}/browser-hub)" in out + err


def test_help(wrap):
    b, env, args = wrap["b"], wrap["env"], wrap["args"]
    args.unlink(missing_ok=True)
    rc, help_, _ = run([b, "--help"], env)
    assert rc == 0
    assert not args.exists()  # browser-hub not run
    assert run([b, "-h"], env)[1] == help_ == run([b, "help"], env)[1]
    assert "browser: his real, headed Google Chrome" in help_.splitlines()[0]
    assert "managed:" not in help_
    assert "browser-hub --help" in help_.splitlines()[-1]  # the whole block, to its last line
    assert run(["bash", wrap["plain"], "--help"], env)[1] == help_  # with or without the managed header
    assert "# " not in help_
    assert "browser view on [MIN]|off|status" in help_
    assert len(help_cmds(help_)) == 11


def help_cmds(help_: str) -> list[str]:
    """The commands its help names (`  browser a|b ...` lines), as browser-hub must know them."""
    out = []
    for ln in help_.splitlines():
        if ln.startswith("  browser "):
            word = ln[len("  browser ") :].split(" ", 1)[0]
            if word and all(c.isalpha() and c.islower() or c == "|" for c in word):
                out += word.split("|")
    return out


# --- part 2: browser + browser-hub against a fake claude-hub-browser -----------------------------------------------


class FakeHub:
    """The container's control API and CDP gate on one loopback port; every request logged (method, path, auth)."""

    def __init__(self, fake: Path, token: str):
        self.fake, self.token, self.reqs, self.lock = fake, token, [], threading.Lock()
        self.view = {"on": False, "until": None, "conns": 4242}
        hub = self

        class H(BaseHTTPRequestHandler):
            def send(self, body, ctype="application/json", code=200):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def one(self, method):
                hub.handle(self, method)

            def do_GET(self):  # noqa: N802
                self.one("GET")

            def do_POST(self):  # noqa: N802
                self.one("POST")

            def do_PUT(self):  # noqa: N802
                self.one("PUT")

            def log_message(self, *a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    def flag(self, name):
        return (self.fake / name).exists()

    def handle(self, h, method):
        auth = h.headers.get("Authorization")
        who = "token" if auth == f"Bearer {self.token}" else "none" if auth is None else "bad"
        n = int(h.headers.get("Content-Length") or 0)
        body = h.rfile.read(n).decode() if n else ""
        with self.lock:
            self.reqs.append(f"{method} {h.path} {who}" + (f" {body}" if body else ""))
        path, _, query = h.path.partition("?")
        if path == "/vnc.html" and method == "GET":
            return h.send(b"<html>noVNC</html>", "text/html", 404 if self.flag("novnc-broken") else 200)
        if path.startswith("/json/"):  # the CDP gate (no token: the gate checks commits itself)
            if path == "/json/version":
                return h.send({"Browser": "Chrome/999.0.0.0", "webSocketDebuggerUrl": "ws://127.0.0.1/x"})
            if path == "/json/list":
                return h.send(
                    [
                        {"type": "page", "title": "Example Domain", "url": "https://example.org/"},
                        {"type": "service_worker", "title": "SW", "url": "https://example.org/sw.js"},
                        {"type": "page", "title": "", "url": "about:blank"},
                    ]
                )
            if path == "/json/new" and method == "PUT":
                return h.send({"id": "T1", "url": urllib.parse.unquote(query)})
            return h.send({"error": "no"}, code=404)
        if who != "token":
            return h.send({"error": "unauthorized"}, code=401)
        chrome = "down" if self.flag("chrome-down") else "up"
        view = self.view
        if path == "/status" and method == "GET":
            view["conns"] = 4242 + (1 if self.flag("view-opened") else 0)
            return h.send(
                {"chrome": chrome, "version": "Chrome/999.0.0.0", "svc": {"up": True, "mode": "enforce"}, "view": view}
            )
        if path == "/chrome/restart" and method == "POST":
            return h.send({"chrome": chrome, "version": "Chrome/999.0.0.0"})
        if path == "/screen" and method == "GET":
            return h.send(b"\x89PNG fake\n", "image/png")
        if path == "/view" and method == "POST":
            a = json.loads(body or "{}")
            on = bool(a.get("on"))
            view.update(on=on, until=time.time() + 60 * a.get("minutes", 0) if on else None)
            return h.send(view)
        if path == "/audit" and method == "GET":
            hours = urllib.parse.parse_qs(query).get("hours", ["?"])[0]
            unguarded = 2 if self.flag("unguarded") else 0
            return h.send({"hours": hours, "commits_token": 3, "denied": 1, "unguarded": unguarded})
        return h.send({"error": "no route"}, code=404)

    def since(self, n: int) -> list[str]:
        with self.lock:
            return list(self.reqs[n:])

    def count(self) -> int:
        with self.lock:
            return len(self.reqs)

    def last(self, prefix: str = "") -> str:
        with self.lock:
            return [r for r in self.reqs if r.startswith(prefix)][-1]

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class Box:
    """A temp HOME, the staged bin (browser, browser-hub, lib/core, lib/browsersvc, fake usage and profile-get) and
    the fake container; br() runs the staged browser."""

    def __init__(self, t: Path):
        self.t, self.home, self.fake = t, t / "home", t / "fake"
        self.home.mkdir()
        self.fake.mkdir()
        self.tok = secrets.token_hex(16)
        self.hub = FakeHub(self.fake, self.tok)
        _, managed = flavours(t)
        b = t / "bin"
        self.browser = install(managed, b / "browser")
        install(HUB, b / "browser-hub")
        (b / "lib").mkdir()
        (b / "lib" / "core").symlink_to(REPO / "core")
        (b / "lib" / "browsersvc").symlink_to(REPO / "packages" / "browsersvc")
        script(b / "usage", 'printf "%s\\n" "$*" >> "$FAKE/usage.log"')
        script(
            b / "profile-get",
            '[ "$1" = --feature ] && { echo "${FAKE_FEATURE:-on}"; [ "${FAKE_FEATURE:-on}" = on ]; exit; }',
            'echo "$*" >> "$FAKE/profile.args"; [ "$1" = network.server_host ] && cat "$FAKE/host" 2>/dev/null',
        )
        self.url = f"http://127.0.0.1:{self.hub.port}"
        self.token_file = self.home / ".secrets" / "browser-ctl-token"
        self.st = t / "state"
        self.claude_json = self.home / ".claude.json"
        env = dict(os.environ)
        for k in ("BROWSER_LINK_HOST", "ASSISTANT_LIB", "FAKE_FEATURE"):
            env.pop(k, None)
        env.update(
            HOME=str(self.home),
            FAKE=str(self.fake),
            BROWSER_HUB_CTL=self.url,
            BROWSER_HUB_CDP=self.url,
            BROWSER_HUB_CTL_TOKEN=str(self.token_file),
            BROWSER_HUB_STATE=str(self.st),
            BROWSER_VIEW_PORT=str(self.hub.port),
            CLAUDE_JSON=str(self.claude_json),
            no_proxy="127.0.0.1,localhost",
            NO_PROXY="127.0.0.1,localhost",
        )
        self.env = env

    def install_token(self):
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        self.token_file.write_text(self.tok + "\n")
        self.token_file.chmod(0o600)

    def br(self, *args, **extra):
        return run([self.browser, *args], dict(self.env, **extra))

    def read(self, name: str) -> str:
        p = self.fake / name
        return p.read_text() if p.exists() else ""

    def lines(self, name: str) -> int:
        return len(self.read(name).splitlines())

    def set_host(self, host: str):
        (self.fake / "host").write_text(host + "\n" if host else "")

    def check_isolation(self):
        """Every request carried this run's token or none; the token never reached a log, an argument or state."""
        assert [r for r in self.hub.since(0) if r.split(" ")[2] not in ("token", "none")] == []
        dumped = "".join(p.read_text(errors="replace") for p in self.fake.glob("*.log"))
        dumped += "".join(p.read_text(errors="replace") for p in self.fake.glob("*.args"))
        if self.st.is_dir():
            dumped += "".join(p.read_text(errors="replace") for p in self.st.iterdir() if p.is_file())
        assert self.tok not in dumped
        # the default state dir under HOME stays unused (BROWSER_HUB_STATE points elsewhere)
        assert not (self.home / ".local" / "state" / "browser").exists()


@pytest.fixture
def bare(tmp_path):
    box = Box(tmp_path)
    yield box
    box.hub.close()
    box.check_isolation()


@pytest.fixture
def box(bare):
    bare.install_token()
    return bare


def test_no_control_token_then_prep(bare):
    b = bare
    rc, out, _ = b.br("status")
    assert rc == 3
    assert "claude-hub-browser: no control token (browser-hub prep)" in out
    assert f"clients -> {b.url} (hub)" in out  # the clients' CDP route on the gate
    rc, out, err = b.br("ensure")
    assert (rc, (out + err).strip()) == (3, "browser-hub: no control token (browser-hub prep)")
    assert [r for r in b.hub.since(0) if not r.startswith("GET /json/version ")] == []  # nothing to the control API
    rc, out, _ = b.br("prep")
    assert rc == 0
    assert "control token made" in out
    made = b.token_file.read_text()
    assert stat.S_IMODE(b.token_file.stat().st_mode) == 0o600
    assert len(made.replace("\n", "")) == 64
    assert "control token kept" in b.br("prep")[1]
    assert b.token_file.read_text() == made


def test_status_ensure_start_stop(box):
    b, closed = box, closed_port()
    n = b.hub.count()
    rc, out, _ = b.br("status")
    assert rc == 0
    assert out == (
        f"clients -> {b.url} (hub)\nclaude-hub-browser: chrome up (Chrome/999.0.0.0) · service up (enforce) · view off"
    )
    assert b.hub.since(n) == ["GET /status token", "GET /json/version none"]
    rc, out, _ = b.br("status", "--json")
    d = json.loads(out)
    assert (rc, d["route"]["cdp"], d["route"]["ok"], d["container"]["chrome"], d["container"]["svc"]["mode"]) == (
        0,
        b.url,
        True,
        "up",
        "enforce",
    )
    out = b.br("status", BROWSER_HUB_CDP=f"http://127.0.0.1:{closed}")[1]
    assert f"clients -> http://127.0.0.1:{closed} (hub (down))" in out  # gate down
    rc, out, _ = b.br("status", BROWSER_HUB_CTL=f"http://127.0.0.1:{closed}")
    assert rc == 3
    assert "claude-hub-browser: claude-hub-browser unreachable" in out
    assert b.br("ensure")[0] == 0
    (b.fake / "chrome-down").touch()
    assert b.br("ensure")[0] == 1
    rc, out, _ = b.br("status")
    assert rc == 1
    assert "chrome down (Chrome/999.0.0.0)" in out
    assert b.br("start")[0] == 1  # Chrome stays down
    (b.fake / "chrome-down").unlink()
    assert b.br("ensure", BROWSER_HUB_CTL=f"http://127.0.0.1:{closed}")[0] == 3
    n = b.hub.count()
    rc, out, _ = b.br("start")
    assert (rc, out) == (0, "chrome up (Chrome/999.0.0.0) in claude-hub-browser")
    assert b.br("restart")[0] == 0
    assert b.hub.since(n) == ["POST /chrome/restart token {}", "POST /chrome/restart token {}"]
    n = b.hub.count()
    rc, out, err = b.br("stop")
    assert rc == 2
    assert "refused: Chrome runs in claude-hub-browser and stays up there" in out + err
    assert b.hub.count() == n  # nothing sent


def test_url_open_screenshot(box):
    b, closed = box, closed_port()
    n = b.hub.count()
    rc, out, _ = b.br("url")
    assert (rc, out) == (0, "Example Domain | https://example.org/\n | about:blank")  # pages only
    assert b.hub.since(n) == ["GET /json/list none"]
    assert b.br("tabs")[1] == out
    rc, out, err = b.br("url", BROWSER_HUB_CDP=f"http://127.0.0.1:{closed}")
    assert rc == 3
    assert "the service does not answer" in out
    n = b.hub.count()
    rc, out, _ = b.br("open", "https://example.org/x?a=1&b=2")
    assert (rc, out) == (0, "opened https://example.org/x?a=1&b=2")
    assert b.hub.since(n) == ["PUT /json/new?https://example.org/x?a=1&b=2 none"]
    n = b.hub.count()
    rc, out, err = b.br("open")
    assert rc == 2
    assert "usage: browser-hub open URL" in out + err
    assert b.hub.count() == n
    assert b.br("open", "https://example.org/", BROWSER_HUB_CDP=f"http://127.0.0.1:{closed}")[0] == 1
    shots = b.t / "shots"
    rc, out, _ = b.br("screenshot", shots / "a.png")
    assert (rc, out) == (0, str(shots / "a.png"))
    assert (shots / "a.png").read_bytes() == b"\x89PNG fake\n"  # the container's PNG
    assert b.hub.last() == "GET /screen token"
    out = b.br("screenshot")[1]
    assert out.startswith(f"{b.home}/reports/browser/screen-")  # the default path
    assert Path(out).stat().st_size > 0
    assert b.br("shot", shots / "b.png")[1] == str(shots / "b.png")
    rc, _, _ = b.br("screenshot", shots / "c.png", BROWSER_HUB_CTL=f"http://127.0.0.1:{closed}")
    assert rc == 3
    assert not (shots / "c.png").exists()


def test_owner_view(box):
    b = box
    n = b.hub.count()
    rc, out, _ = b.br("view", "on", "2")
    assert rc == 0
    assert "(claude-hub-browser; noVNC asks for the VNC password)" in out
    assert b.hub.since(n) == ['POST /view token {"on": true, "minutes": 2}']
    assert "view: on until " in b.br("view", "status")[1]
    assert "view: on until " in b.br("view")[1]  # no subcommand: status
    assert b.br("view", "off")[:2] == (0, "view: off")
    assert b.hub.last("POST /view") == 'POST /view token {"on": false}'
    assert b.br("view", "status")[1] == "view: off"
    b.br("view", "on")
    assert b.hub.last("POST /view") == 'POST /view token {"on": true, "minutes": 60}'  # 60 min by default
    b.br("view", "off")
    n = b.hub.count()
    for args in (("view", "on", "abc"), ("view", "bogus")):
        rc, out, err = b.br(*args)
        assert rc == 2, args
        assert "usage: browser-hub view on [MIN]|off|status" in out + err
    assert b.hub.count() == n  # usage errors: nothing sent


def test_feature_switch(box):
    rc, out, err = box.br("status", FAKE_FEATURE="off")
    assert (rc, (out + err).strip()) == (3, "browser: feature browser is off (features.browser in the profile)")


def test_link(box):
    b, port = box, box.hub.port
    b.set_host("127.0.0.1")
    rc, out, _ = b.br("link", "--json")
    assert rc == 0
    d = json.loads(out)
    assert (d["url"], d["verified"], d["http"], d["view_minutes_left"], d["where"]) == (
        f"http://127.0.0.1:{port}{VNC}",
        True,
        "200",
        30,
        "claude-hub-browser",
    )
    assert '"password": "noVNC asks for the VNC password (never in the URL)"' in out
    assert b.read("profile.args") == "network.server_host\n"  # host from profile-get
    assert b.hub.last("POST /view") == 'POST /view token {"on": true, "minutes": 30}'
    assert sum(r == "GET /vnc.html none" for r in b.hub.since(0)) == 1  # the noVNC page checked
    assert json.loads((b.st / "hub-link.json").read_text())["conns"] == 4242  # the view's connections marked
    usage = b.read("usage.log")
    assert "log tg.browser.open exec --channel telegram --ms " in usage
    assert "--outcome ok --attr http=200" in usage
    n = b.hub.count()
    assert b.br("link")[:2] == (0, f"http://127.0.0.1:{port}{VNC}")  # 30 min left
    assert not any(r.startswith("POST /view") for r in b.hub.since(n))  # >= 10 min left: not turned on again
    (b.fake / "view-opened").touch()
    b.br("view", "off")
    last = b.read("usage.log").splitlines()[-1]
    assert "log tg.browser.open notify_opened --channel telegram --outcome opened --attr conns=1" in last
    assert not (b.st / "hub-link.json").exists()  # the link mark removed
    (b.fake / "view-opened").unlink()
    b.br("link")
    b.br("view", "off")
    last = b.read("usage.log").splitlines()[-1]
    assert "log tg.browser.open ignored --channel telegram --outcome unopened --attr conns=0" in last
    n = b.lines("profile.args")
    assert b.br("link", "--min", "5", BROWSER_LINK_HOST="127.0.0.1")[0] == 0
    assert b.hub.last("POST /view") == 'POST /view token {"on": true, "minutes": 5}'
    assert b.lines("profile.args") == n  # BROWSER_LINK_HOST: profile-get not asked
    b.br("view", "off")
    n = b.hub.count()
    for bad in ("0", "241", "abc"):
        rc, out, err = b.br("link", "--min", bad)
        assert (rc, (out + err).strip()) == (2, "browser-hub: --min must be 1-240"), bad
    assert b.hub.count() == n  # nothing sent
    b.set_host("")
    rc, out, err = b.br("link")
    assert rc == 1
    assert "no host (profile network.server_host)" in out + err
    b.br("view", "off")
    b.set_host("127.0.0.1")
    (b.fake / "novnc-broken").touch()
    rc, out, _ = b.br("link", "--json")
    d = json.loads(out)
    assert (rc, d["verified"], str(d["http"])) == (1, False, "404")  # the noVNC page not loading
    (b.fake / "novnc-broken").unlink()
    b.br("view", "off")


def test_audit_update_mcp_unknown(box):
    b = box
    assert b.br("audit")[:2] == (0, "hours 24 · commits_token 3 · denied 1 · unguarded 0")
    assert b.hub.last() == "GET /audit?hours=24 token"
    b.br("audit", "--hours", "2", "--json")
    assert b.hub.last() == "GET /audit?hours=2 token"
    (b.fake / "unguarded").touch()
    assert b.br("audit")[0] == 1
    (b.fake / "unguarded").unlink()
    assert b.br("audit", "--hours", "x")[0] == 2
    n = b.hub.count()
    rc, out, _ = b.br("update")
    assert rc == 0
    assert "Chrome is pinned in claude-hub-browser's image (ops/hub/browser-pins.toml" in out
    assert b.hub.count() == n  # nothing sent
    b.claude_json.write_text(
        '{"mcpServers": {"browser": {"args": ["cli.js", "--cdp-endpoint", "http://127.0.0.1:9222"]}}, "other": 1}\n'
    )
    b.claude_json.chmod(0o600)
    assert b.br("mcp", "status")[1] == "browser MCP --cdp-endpoint http://127.0.0.1:9222"
    assert b.br("mcp", "hub")[0] == 0
    d = json.loads(b.claude_json.read_text())
    assert (d["mcpServers"]["browser"]["args"][2], d["other"]) == (b.url, 1)  # the gate, the rest kept
    assert stat.S_IMODE(b.claude_json.stat().st_mode) == 0o600  # mode kept
    assert "(unchanged)" in b.br("mcp", "hub")[1]
    assert b.br("mcp", "local")[0] == 2
    rc, out, err = b.br("bogus")
    assert rc == 2
    assert "browser-hub: unknown command bogus" in out + err


def test_help_commands_known_to_browser_hub(box):
    b = box
    b.set_host("127.0.0.1")
    cmds = help_cmds(b.br("--help")[1])
    assert len(cmds) == 11
    extra = {"open": ["https://example.org/"], "view": ["status"], "screenshot": [b.t / "shots" / "h.png"]}
    for c in cmds:
        err = b.br(c, *extra.get(c, []))[2]
        assert "unknown command" not in err, c
    b.br("view", "off")
