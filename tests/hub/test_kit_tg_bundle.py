"""The tg pieces of the hub kit: the compose override, the tg paste (rendered from the brain's live adapter config,
run in a sandbox with stub docker/install), the release's tg tree and ops/hub/tgloader.ts (bun, when present)."""

import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import time
from pathlib import Path

import pytest
from hubkit import HubError, settings
from hubkit import kit as K
from hubkit import release as R

REPO = Path(__file__).resolve().parents[2]
RID = "abcdef123456"
SHA = RID + "0" * 28
TOKEN = "123456:TEST-TOKEN-VALUE-never-in-the-kit"  # secretscan:allow (a fake token; the tests check it never leaks)
LOADER = REPO / "ops/hub/tgloader.ts"
BUN = shutil.which("bun") or str(Path.home() / ".bun/bin/bun")


def _yaml(p: Path):
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load(p.read_text())


def test_compose_override_policy():
    ov = _yaml(REPO / "ops/hub/compose.tg.yaml")
    assert set(ov["services"]) == {"hub", "tg"}
    for name, svc in ov["services"].items():
        assert "ports" not in svc and "network_mode" not in svc and "privileged" not in svc, name
        for v in svc.get("volumes", []):
            assert v["source"].startswith(("${HUB_ROOT", "${BOT_HOME")) and "docker.sock" not in v["source"], name
            if v["target"] in ("/srv/releases", "/opt/hub-loader", "/srv/tg/config"):
                assert v.get("read_only") is True, (name, v["target"])
    assert [v["target"] for v in ov["services"]["hub"]["volumes"]] == ["/srv/tgfiles"]
    tg = ov["services"]["tg"]
    assert tg["secrets"] == ["tg_live_token", "token_tg"] and set(ov["secrets"]) == {"tg_live_token", "token_tg"}
    assert tg["command"][:2] == ["bun", "/opt/hub-loader/tgloader.ts"]
    base = _yaml(REPO / "ops/hub/compose.yaml")
    assert base["services"]["tg"]["profiles"] == ["tg"], "tg starts only with the profile"
    assert "secrets" not in base["services"]["tg"], "no token in the base file"


def test_compose_override_merges(tmp_path):
    exe = (
        os.environ.get("HUB_COMPOSE_BIN")
        or shutil.which("docker-compose")
        or str(Path.home() / "opt/tools/compose/docker-compose")
    )
    if not Path(exe).is_file():
        pytest.skip("no docker-compose binary")
    env = tmp_path / "hub.env"
    env.write_text("HUB_ROOT=/tmp/hub\nBOT_HOME=/tmp/bot\nBRAIN_NETWORK=net\nTZ=UTC\n")
    cmd = [exe, "-f", str(REPO / "ops/hub/compose.yaml"), "-f", str(REPO / "ops/hub/compose.tg.yaml"), "--env-file",
           str(env), "--profile", "tg", "config", "--format", "json"]  # fmt: skip
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr[-800:]
    svc = json.loads(r.stdout)["services"]
    assert svc["tg"]["command"][1] == "/opt/hub-loader/tgloader.ts" and svc["tg"]["read_only"] is True
    assert {v["target"] for v in svc["tg"]["volumes"]} == {"/opt/hub-loader", "/srv/releases", "/srv/tg/config",
                                                           "/srv/tg/state", "/srv/tgfiles"}  # fmt: skip
    assert "/srv/tgfiles" in {v["target"] for v in svc["hub"]["volumes"]} and len(svc["hub"]["volumes"]) == 6
    assert {s["source"] for s in svc["tg"]["secrets"]} == {"tg_live_token", "token_tg"}
    assert "secrets" not in svc["hub"] or all(s["source"] != "tg_live_token" for s in svc["hub"]["secrets"])
    assert svc["tg"]["user"] == "10100:10100" and svc["tg"]["cap_drop"] == ["ALL"]


def _live_config(home: Path) -> None:
    (home / ".secrets").mkdir(mode=0o700, exist_ok=True)
    (home / ".secrets" / "tg-live").write_text(TOKEN + "\n")
    acc = home / ".claude/channels/telegram/access.json"
    acc.parent.mkdir(parents=True)
    acc.write_text('{"allowFrom": ["1"]}\n')
    cfg = home / ".config/tg-adapter/live.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"mode": "live", "bot": "test_h6_bot", "token_file": "~/.secrets/tg-live",
                               "allow_from_file": str(acc), "send": True, "instant_commands": False,
                               "compat_journal": "~/x.jsonl", "compat_buttons_file": "~/b.json"}))  # fmt: skip


def _render(src: Path) -> dict:
    R.build(src, RID, SHA)
    R.write_current(RID)
    return K.render(src, SHA)


STUBS = {
    "docker": '#!/bin/sh\necho "docker $*" >> "$STUB_LOG"\n[ "$1" = top ] && cat "$STUB_TOP" 2>/dev/null\nexit 0\n',
    "install": '#!/bin/bash\na=(); while [ $# -gt 0 ]; do case "$1" in -o|-g) shift 2;; *) a+=("$1"); shift;; esac;'
    ' done\nexec /usr/bin/install "${a[@]}"\n',
}


def _inner(text: str) -> str:
    return text.split("<<'HUB'\n", 1)[1].split("\nHUB\n", 1)[0] + "\n"


def test_tg_paste_render_and_sandbox(hubhome, srctree, tmp_path):
    _live_config(hubhome)
    info = _render(srctree)
    assert info["tg"]["state"] == "rendered" and info["tg"]["paste_lines"] <= K.PASTE_MAX
    kd = hubhome / ".local/share/claude-hub/kit"
    for f in kd.rglob("*"):
        if f.is_file():
            assert TOKEN not in f.read_text(errors="replace"), f"token value in {f}"
    cfg = json.loads((kd / "tg/live.json").read_text())
    assert cfg["bot"] == "test_h6_bot" and cfg["token_file"] == "/run/secrets/tg_live_token" and cfg["send"] is True
    assert cfg["hub"] == {
        "url": "http://claude-hub:8700",
        "token_file": "/run/secrets/token_tg",
        "files_dir": "/srv/tgfiles",
    }
    assert cfg["instant_commands"] is False and not any(k.startswith("compat") for k in cfg)
    paste = (kd / "tg-install.sh").read_text()
    blob = b"".join((kd / n).read_bytes() for n in ("compose.tg.yaml", "loader/tgloader.ts", "tg/live.json"))
    assert f"= '{hashlib.sha256(blob).hexdigest()[:16]}'" in paste
    assert "'.secrets/tg-live'" not in paste and '"$B/.secrets/tg-live"' in paste
    assert K.info()["kit"] == info["kit"], "the tg files are not part of the hub kit id"
    hub = Path(settings()["hub_root"])
    (hub / "secrets").mkdir(parents=True)
    (hub / "compose.yaml").write_text("x")
    (hub / "loader").mkdir()
    (hub / "secrets/token_tg").write_text("t" * 64)
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for n, body in STUBS.items():
        (stubs / n).write_text(body)
        (stubs / n).chmod(0o755)
    script = tmp_path / "tg.sh"
    script.write_text(_inner(paste))
    log, top = tmp_path / "stub.log", tmp_path / "top.txt"
    env = {"PATH": f"{stubs}:/usr/bin:/bin", "STUB_LOG": str(log), "STUB_TOP": str(top), "HOME": str(hubhome)}

    def run():
        return subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True, cwd=tmp_path)

    r = run()
    assert r.returncode == 1 and "still switched on" in r.stderr and not (hub / "secrets/tg_live_token").exists()
    (hubhome / ".config/tg-adapter/live.off").write_text("")
    top.write_text("ARGS\nbun src/cli.ts run --instance live\n")
    r = run()
    assert r.returncode == 1 and "still running" in r.stderr and not (hub / "secrets/tg_live_token").exists()
    top.write_text("ARGS\nbun src/cli.ts run --instance test\n")
    r = run()
    assert r.returncode == 0, r.stderr
    assert f"claude-hub tg {info['tg']['kit']}: OK" in r.stdout
    tok = hub / "secrets/tg_live_token"
    assert tok.read_text() == TOKEN + "\n" and stat.S_IMODE(tok.stat().st_mode) == 0o600
    assert (hub / "tg/config/live.json").read_bytes() == (kd / "tg/live.json").read_bytes()
    assert (
        (hub / "tg/config/access.json").is_file() and (hub / "tgfiles/in").is_dir() and (hub / "tgfiles/out").is_dir()
    )
    assert (hub / "loader/tgloader.ts").read_bytes() == (REPO / "ops/hub/tgloader.ts").read_bytes()
    calls = log.read_text()
    assert "compose -f compose.yaml -f compose.tg.yaml up -d --wait --wait-timeout 180 hub" in calls
    assert "compose -f compose.yaml -f compose.tg.yaml --profile tg up -d --wait --wait-timeout 150 tg" in calls
    assert TOKEN not in r.stdout + r.stderr + calls
    with (kd / "tg/live.json").open("a") as f:
        f.write(" ")
    r = run()
    assert r.returncode == 1 and "the kit changed" in r.stderr
    rb = _inner((kd / "tg-rollback.sh").read_text())
    (tmp_path / "rb.sh").write_text(rb)
    r = subprocess.run(["bash", str(tmp_path / "rb.sh")], env=env, capture_output=True, text=True, cwd=tmp_path)
    assert r.returncode == 0 and "stopped and removed" in r.stdout
    assert "--profile tg stop -t 20 tg" in log.read_text()


def test_tg_paste_absent_without_live_config(hubhome, srctree, capsys):
    from hubkit import cli

    info = _render(srctree)
    assert info["tg"]["state"] == "not rendered" and "live adapter config" in info["tg"]["why"]
    assert cli.main(["kit", "paste", "tg-install"]) == 3
    assert "no tg-install.sh" in capsys.readouterr().err
    assert cli.main(["kit", "paste", "install"]) == 0


FAKE_TG_CLI = """import fake from 'fakepkg'
import inner from '@scope/inner'
import ddl from './schema.sql' with { type: 'text' }
console.log(JSON.stringify({ fake, inner, ddl: ddl.trim() }))
"""


def _tg_tree(src: Path, nm: Path, version: str = "1.2.3") -> None:
    (src / "adapters/tg/src").mkdir(parents=True)
    (src / "adapters/tg/src/cli.ts").write_text(FAKE_TG_CLI)
    (src / "adapters/tg/src/db.ts").write_text("export const x = 1\n")
    (src / "adapters/tg/src/schema.sql").write_text("CREATE TABLE t(x);\n")
    (src / "adapters/tg/package.json").write_text('{"name": "tg-adapter"}\n')
    (src / "adapters/tg/bun.lock").write_text(
        '{\n  "lockfileVersion": 2,\n  "packages": {\n    "fakepkg": ["fakepkg@1.2.3", "", {}, "sha512-x"],\n'
        '    "@scope/inner": ["@scope/inner@0.1.0", "", {}, "sha512-y"],\n  },\n}\n'
    )
    (src / "schemas").mkdir(exist_ok=True)
    (src / "schemas/tools.toml").write_text("# tools\n")
    for name, ver, val in (("fakepkg", version, "fake-" + version), ("@scope/inner", "0.1.0", "inner")):
        (nm / name).mkdir(parents=True, exist_ok=True)
        (nm / name / "package.json").write_text(json.dumps({"name": name, "version": ver, "main": "index.js"}))
        (nm / name / "index.js").write_text(f"module.exports = {json.dumps(val)}\n")
    (nm / ".bin").mkdir(exist_ok=True)
    os.symlink("../fakepkg/index.js", nm / ".bin" / "fake")


def _rel_path_rule():
    import importlib.util

    spec = importlib.util.spec_from_file_location("hub_loader_rule", REPO / "packages/hub/loader.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.REL_PATH


def test_release_tg_bundle(hubhome, srctree, tmp_path, monkeypatch):
    if not Path(BUN).is_file():
        pytest.skip("no bun")
    nm = tmp_path / "node_modules"
    _tg_tree(srctree, nm)
    monkeypatch.setenv("HUB_TG_NODE_MODULES", str(nm))
    monkeypatch.setenv("HUB_TG_BUN", BUN)
    assert R.TG_IN_RELEASE is True
    d, built = R.build(srctree, RID, SHA)
    meta = json.loads((d / "release.json").read_text())
    assert built and meta["entry"]["tg"] == "tg/adapter.js" and R.verify(RID) is None
    tg = sorted(k for k in meta["files"] if k.startswith("tg/"))
    assert tg == ["tg/adapter.js", "tg/tools.toml"], "one bundle + the registry: no node_modules, no sources"
    rule = _rel_path_rule()
    assert all(rule.match(k) for k in meta["files"]), [k for k in meta["files"] if not rule.match(k)]
    assert not any("node_modules" in k or "@" in k for k in meta["files"])
    lock = srctree / "adapters/tg/bun.lock"
    assert meta["tg"]["packages"] == {"@scope/inner": "0.1.0", "fakepkg": "1.2.3"}
    assert meta["tg"]["lock_sha256"] == hashlib.sha256(lock.read_bytes()).hexdigest() and meta["tg"]["bun"]
    js = (d / "tg/adapter.js").read_text()
    assert str(tmp_path) not in js and "hub-tg-" not in js and "/../" not in js, "the bundle names no build path"
    r = subprocess.run([BUN, str(d / "tg/adapter.js")], capture_output=True, text=True, timeout=60, cwd=tmp_path)
    assert r.returncode == 0 and json.loads(r.stdout) == {"fake": "fake-1.2.3", "inner": "inner",
                                                           "ddl": "CREATE TABLE t(x);"}, r.stderr  # fmt: skip
    again, _ = R.build(srctree, "abcdef999999", "c" * 40, root=tmp_path / "other")
    assert (again / "tg/adapter.js").read_bytes() == (d / "tg/adapter.js").read_bytes(), "same inputs, same bundle"
    r = subprocess.run([BUN, str(LOADER), "--releases", str(d.parent), "--check-release", RID],
                       capture_output=True, text=True, timeout=60)  # fmt: skip
    assert (r.returncode, r.stdout.strip()) == (0, "ok"), r.stderr
    (d / "tg/adapter.js").write_text(js + "\n// changed\n")
    r = subprocess.run([BUN, str(LOADER), "--releases", str(d.parent), "--check-release", RID],
                       capture_output=True, text=True, timeout=60)  # fmt: skip
    assert r.returncode == 1 and "sha256 mismatch" in r.stdout
    (nm / "fakepkg/package.json").write_text('{"version": "9.9.9"}')
    with pytest.raises(HubError, match="bun.lock wants 1.2.3"):
        R.build(srctree, "abcdef654321", "b" * 40)
    assert not list((d.parent).glob(".stage-*")) and not list(Path(os.environ.get("TMPDIR", "/tmp")).glob("hub-tg-*"))


def test_release_tg_bundle_needs_bun(hubhome, srctree, tmp_path, monkeypatch):
    nm = tmp_path / "node_modules"
    _tg_tree(srctree, nm)
    monkeypatch.setenv("HUB_TG_NODE_MODULES", str(nm))
    monkeypatch.setenv("HUB_TG_BUN", str(tmp_path / "no-bun-here"))
    with pytest.raises(HubError, match="bun build failed"):
        R.build(srctree, RID, SHA)
    assert R.verify(RID) is not None, "no release without its adapter"


FAKE_CLI = """import { appendFileSync, readFileSync } from 'fs'
const mode = readFileSync(import.meta.dir + '/mode.txt', 'utf8').trim()
appendFileSync(process.env.TG_ADAPTER_STATE_DIR + '/starts.log', JSON.stringify({ rel: process.env.HUB_RELEASE,
  argv: process.argv.slice(2), cfg: process.env.TG_ADAPTER_CONFIG, tools: process.env.TG_ADAPTER_TOOLS,
  db: process.env.ASSISTANT_EVENTS_DB, off: process.env.TG_ADAPTER_OFF_FILE }) + '\\n')
if (mode === 'exit3') process.exit(3)
process.on('SIGTERM', () => process.exit(0))
setInterval(() => {}, 1000)
"""


def _fake_release(root: Path, rid: str, mode: str, tamper: bool = False) -> None:
    d = root / rid
    files = {"tg/adapter.js": FAKE_CLI, "tg/mode.txt": mode + "\n", "tg/tools.toml": "# t\n"}
    meta = {"format": 1, "rid": rid, "entry": {"hub": "app/hubd.py", "tg": "tg/adapter.js"}, "files": {}}
    for rel, text in files.items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text(text)
        meta["files"][rel] = {"sha256": hashlib.sha256(text.encode()).hexdigest()}
    if tamper:
        (d / "tg/mode.txt").write_text("run-but-changed\n")
    (d / "release.json").write_text(json.dumps(meta))


def _until(fn, secs: float = 20.0):
    end = time.monotonic() + secs
    while time.monotonic() < end:
        got = fn()
        if got:
            return got
        time.sleep(0.1)
    raise AssertionError("timed out")


def test_tgloader_runs_switches_and_stays_down_on_409(tmp_path):
    if not Path(BUN).is_file():
        pytest.skip("no bun")
    rel, state, cfg = tmp_path / "releases", tmp_path / "state", tmp_path / "config/live.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("{}")
    _fake_release(rel, "aaaaaaa1", "run")
    _fake_release(rel, "bbbbbbb2", "exit3")
    _fake_release(rel, "ccccccc3", "run", tamper=True)
    _fake_release(rel, "ddddddd4", "run")
    (rel / "CURRENT").write_text("aaaaaaa1 t1\n")
    p = subprocess.Popen([BUN, str(REPO / "ops/hub/tgloader.ts"), "--releases", str(rel), "--state", str(state),
                          "--config", str(cfg), "--poll", "0.2", "--healthy-after", "0.6"],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)  # fmt: skip

    def status() -> dict:
        f = state / "tgloader-status.json"
        return json.loads(f.read_text()) if f.exists() else {}

    def starts() -> list:
        f = state / "adapter/starts.log"
        return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []

    try:
        _until(lambda: status().get("state") == "running" and status().get("release") == "aaaaaaa1")
        st = json.loads((state / "tgloader.json").read_text())
        assert st["last_good"] == "aaaaaaa1"
        s0 = starts()[0]
        assert s0["argv"] == ["run", "--instance", "live"] and s0["cfg"] == str(cfg)
        assert s0["tools"].endswith("aaaaaaa1/tg/tools.toml") and s0["db"] == str(state / "adapter/journal.db")
        assert s0["off"] == str(cfg.parent / "live.off")
        (rel / "CURRENT").write_text("bbbbbbb2 t2\n")
        _until(lambda: status().get("state") == "conflict")
        time.sleep(1.5)
        assert [s["rel"] for s in starts()] == ["aaaaaaa1", "bbbbbbb2"], "a 409 is not retried"
        assert "409" in status()["why"]
        (rel / "CURRENT").write_text("ccccccc3 t3\n")
        _until(lambda: "ccccccc3 t3" in json.loads((state / "tgloader.json").read_text()).get("bad", {}))
        _until(lambda: status().get("state") == "running" and status().get("release") == "aaaaaaa1")
        (rel / "CURRENT").write_text("ddddddd4 t4\n")
        _until(lambda: status().get("release") == "ddddddd4" and status().get("state") == "running")
        _until(lambda: json.loads((state / "tgloader.json").read_text())["last_good"] == "ddddddd4")
    finally:
        p.send_signal(signal.SIGTERM)
        try:
            p.wait(15)
        except subprocess.TimeoutExpired:
            p.kill()
    assert p.returncode == 0
    out = p.stdout.read() if p.stdout else ""
    assert '"event":"loader_stop"' in out and TOKEN not in out


def test_tgloader_runs_the_real_bundle_on_a_mock_bot(hubhome, srctree, tmp_path, monkeypatch):
    """The repo's own adapter as the release's tg/adapter.js, run by tgloader: getMe, a poll, the row in its journal."""
    nm = REPO / "adapters/tg/node_modules"
    if not Path(BUN).is_file() or not nm.is_dir():
        pytest.skip("no bun or no adapters/tg/node_modules")
    import sqlite3

    a = srctree / "adapters/tg"
    shutil.copytree(REPO / "adapters/tg/src", a / "src")
    for n in ("package.json", "bun.lock"):
        shutil.copyfile(REPO / "adapters/tg" / n, a / n)
    (srctree / "schemas").mkdir(exist_ok=True)
    shutil.copyfile(REPO / "schemas/tools.toml", srctree / "schemas/tools.toml")
    monkeypatch.setenv("HUB_TG_NODE_MODULES", str(nm))
    monkeypatch.setenv("HUB_TG_BUN", BUN)
    d, _ = R.build(srctree, RID, SHA)
    (d.parent / "CURRENT").write_text(f"{RID} t1\n")
    sock, calls, home = tmp_path / "bot.sock", tmp_path / "calls.json", tmp_path / "adapter-home"
    home.mkdir()
    tok = tmp_path / "tok.env"
    tok.write_text("TELEGRAM_BOT_TOKEN=4242:TEST-not-a-real-token\n")  # secretscan:allow (fake token)
    tok.chmod(0o600)
    # the test instance: the adapter refuses a mock Bot API in live mode (tgloader passes no test env through)
    cfg, state = tmp_path / "config/test.json", tmp_path / "state"
    cfg.parent.mkdir()
    cfg.write_text(json.dumps({"mode": "test", "bot": "testadapter_bot", "token_file": str(tok), "allow_from": ["111"],
                               "api_root": "http://mock.invalid", "api_unix_socket": str(sock), "send": False,
                               "poll_timeout_s": 1, "quiet_hours": "off", "sleepin": False, "typing": False,
                               "transcribe": False, "scan_private": False}))  # fmt: skip
    mock = subprocess.Popen([BUN, "test/mockserve.ts", str(sock), str(calls), "ping"], cwd=REPO / "adapters/tg")
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home), "TZ": "UTC"}
    p = None
    try:
        _until(sock.exists, 15)
        p = subprocess.Popen([BUN, str(LOADER), "--releases", str(d.parent), "--state", str(state), "--config",
                              str(cfg), "--instance", "test", "--poll", "0.2", "--healthy-after", "1"], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)  # fmt: skip

        def status() -> dict:
            f = state / "tgloader-status.json"
            try:
                return json.loads(f.read_text())
            except (OSError, ValueError):
                return {}

        def journal() -> list:
            db = state / "adapter/test.db"  # the test instance keeps its own DB in the state dir
            if not db.exists():
                return []
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                return con.execute("SELECT kind, source FROM events ORDER BY id").fetchall()
            except sqlite3.Error:
                return []
            finally:
                con.close()

        try:
            _until(lambda: status().get("state") == "running" and status().get("release") == RID, 30)
            _until(lambda: journal() == [("tg.message", "tg-adapter")], 30)
        except AssertionError:
            p.send_signal(signal.SIGTERM)
            out = p.communicate(timeout=20)[0] or ""
            p = None
            raise AssertionError(f"status {status()} journal {journal()} loader {out[-1500:]}") from None
        methods = [c["method"] for c in json.loads(calls.read_text())]
        assert "getMe" in methods and "sendMessage" not in methods, methods
        _until(lambda: json.loads((state / "tgloader.json").read_text()).get("last_good") == RID, 15)
    finally:
        if p is not None:
            p.send_signal(signal.SIGTERM)
            try:
                p.wait(20)
            except subprocess.TimeoutExpired:
                p.kill()
        mock.send_signal(signal.SIGTERM)
        mock.wait(10)
    out = p.stdout.read() if p and p.stdout else ""
    assert p is not None and p.returncode == 0, out[-2000:]
    assert "TEST-not-a-real-token" not in out
