"""The egress injector: hubpolicy_egress (protected), its policy, hubd's wiring and the release switch. Synthetic
values only; the far side is a loopback fake; no hub, no real secret."""

from __future__ import annotations

import asyncio
import base64
import gzip
import importlib.util
import json
import os
import re
import secrets
import sys
import threading
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
HUB = REPO / "packages" / "hub"
POLICY = HUB / "policy" / "egress.toml"


def _mod(name: str, tag: str = "p312"):
    spec = importlib.util.spec_from_file_location(f"{name}_{tag}_{secrets.token_hex(4)}", HUB / f"{name}.py")
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m  # dataclasses look their module up while the class is built
    spec.loader.exec_module(m)
    return m


eg = _mod("hubpolicy_egress")
hr = _mod("hubredact")


def tok(n: int = 32) -> str:
    return secrets.token_urlsafe(n)[:n]


# ------------------------------------------------------------------------------------------------ a fake far side


class Far(BaseHTTPRequestHandler):
    seen: list[dict] = []
    value = ""

    def log_message(self, *a):  # quiet
        pass

    def _answer(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        Far.seen.append(
            {
                "method": self.command,
                "path": self.path,
                "body": body,
                "headers": {k.lower(): v for k, v in self.headers.items()},
            }
        )
        p = self.path.split("?")[0]
        if p == "/redirect":
            self.send_response(302)
            self.send_header("Location", f"http://elsewhere.invalid/x?k={Far.value}")
            self.end_headers()
            return
        if p == "/gzip":
            data = gzip.compress(json.dumps({"echo": Far.value}).encode())
            self.send_response(200)
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if p == "/br":
            self.send_response(200)
            self.send_header("Content-Encoding", "br")
            self.send_header("Content-Length", "3")
            self.end_headers()
            self.wfile.write(b"abc")
            return
        if p == "/api/v3/movie":  # a JSON array over max_response (4096), one item echoing the value
            items = [{"id": i, "title": f"t{i}", "pad": "p" * 40} for i in range(1, 201)]
            items[150]["echo"] = Far.value
            data = json.dumps(items, indent=2).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if p == "/api/v3/blob":  # big, not an array
            data = json.dumps({"x": "y" * 6000}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if p == "/big":
            data = b"x" * 5000
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if p == "/denied":
            self.send_response(401)
            self.send_header("X-Echo", Far.value)
            self.end_headers()
            self.wfile.write(b'{"message": "no"}')
            return
        data = json.dumps({"path": self.path, "auth": self.headers.get("Authorization", ""), "binary": "ÿ"}).encode()
        data += b"\x00\xff\xfe" + Far.value.encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("X-Echo", self.headers.get("X-Api-Key", ""))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = do_DELETE = do_PUT = _answer


@pytest.fixture
def far():
    Far.seen = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Far)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


POL = """
version = 1
[defaults]
mode = "audit"
timeout = 5
max_response = 4096

[bindings.ha]
secret = "ha_token"
endpoint = "@endpoints:ha"
inject = [{ in = "header", name = "Authorization", format = "Bearer {}" }]
rules = [{ methods = ["GET"], paths = ["/api/states", "/api/states/*", "/redirect", "/gzip", "/br", "/big",
          "/denied"] }]

[bindings.radarr]
secret = "media.json"
field = "radarr.key"
endpoint = "@field:radarr.url"
inject = [{ in = "header", name = "X-Api-Key" }]
mode = "enforce"
max_upstream = 65536
rules = [{ methods = ["GET"], paths = ["/api/v3/**"] }, { methods = ["DELETE"], paths = ["/api/v3/queue/*"] }]

[bindings.sab]
secret = "sab_key"
endpoint = "@endpoints:sab"
inject = [{ in = "query", name = "apikey" }]
mode = "enforce"
rules = [{ methods = ["GET"], paths = ["/api"], query = { mode = ["queue", "history"] } }]

[bindings.tg]
secret = "tg_token"
endpoint = "@endpoints:tg"
inject = [{ in = "path", format = "bot{}" }]
rules = [{ methods = ["POST"], paths = ["/*/getMe"] }]

[bindings.dash]
secret = "dash_auth"
endpoint = "@endpoints:dash"
inject = [{ in = "basic" }]
rules = [{ methods = ["GET"], paths = ["/live/health"] }]

[bindings.gone]
secret = "not_there"
endpoint = "http://claude-vault:8300"
inject = [{ in = "header", name = "Authorization", format = "Bearer {}" }]
"""


def _inj(tmp_path: Path, far_url: str, pol: str = POL, **extra_files: str):
    vals = {"ha": tok(40), "radarr": tok(32), "sab": tok(32), "tg": f"{secrets.randbelow(10**9)}:{tok(35)}",
            "dash": f"user:{tok(24)}"}  # fmt: skip
    files = {
        "ha_token": vals["ha"],
        "media.json": json.dumps({"radarr": {"url": far_url + "/", "key": vals["radarr"]}}),
        "sab_key": vals["sab"],
        "tg_token": vals["tg"],
        "dash_auth": vals["dash"],
        "egress.endpoints": f"# test\nha {far_url}\nsab {far_url}\ntg {far_url}\ndash {far_url}\n",
        **extra_files,
    }
    store = hr.Store.from_texts(files)
    p = tmp_path / "egress.toml"
    p.write_text(pol)
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    return eg.Injector.load(str(p), store, hr.Redactor(store), str(state)), vals


def _req(handle: str, url: str, method: str = "GET", headers=None, body: bytes | None = None) -> dict:
    a = {"handle": handle, "method": method, "url": url, "headers": headers or {}}
    if body is not None:
        a["body_b64"] = base64.b64encode(body).decode()
    return a


def _audit(tmp_path: Path) -> str:
    return (tmp_path / "state" / "egress" / "audit.jsonl").read_text()


# ------------------------------------------------------------------------------------------------ the policy


def test_repo_policy_parses_and_names_the_core_handles():
    b = eg.parse_policy(tomllib.loads(POLICY.read_text()))
    sys.path.insert(0, str(REPO))
    try:
        from core import egress as E
    finally:
        sys.path.remove(str(REPO))
    assert set(b) == set(E.HANDLES)
    assert all(x.mode == "audit" for x in b.values())  # audit first: enforce is a later protected change
    text = POLICY.read_text()
    assert not re.search(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", text)  # no address in the repo: endpoints come from his paste
    for n in ("radarr", "sonarr"):  # media deletions never in a rule
        assert not any("DELETE" in r.methods and any("movie" in p or "series" in p or "file" in p for p in r.paths)
                       for r in b[n].rules)  # fmt: skip


@pytest.mark.parametrize(
    ("handle", "method", "path"),
    [
        ("vault", "GET", "/events"),  # the vault-events cron (shadow would_deny, 23.10)
        ("vault", "GET", "/deploy/status"),
        ("vault", "GET", "/jobs/abc"),
        ("vault", "GET", "/alerts"),
        ("vault", "POST", "/raw/release"),
        ("vault", "POST", "/media/release"),
        ("gym", "POST", "/api/v2/user/routines"),  # gym plan push
        ("gym", "PATCH", "/api/v2/day_exercises/u_x"),
        ("gym", "DELETE", "/api/v2/days/u_x"),
    ],
)
def test_repo_policy_has_a_rule_for_the_tools_routes(handle, method, path):
    b = eg.parse_policy(tomllib.loads(POLICY.read_text()))[handle]
    assert any(method in r.methods and r.path_ok(path) for r in b.rules)


def test_repo_policy_keeps_gym_writes_to_the_plan_routes():
    b = eg.parse_policy(tomllib.loads(POLICY.read_text()))["gym"]
    for m, p in [("DELETE", "/api/v2/exercises/u_x"), ("POST", "/api/v2/user"), ("PUT", "/api/v2/routines/u_x")]:
        assert not any(m in r.methods and r.path_ok(p) for r in b.rules)


HEAD = 'version = 1\n[bindings.ha]\nsecret = "t"\n'
SLOT = 'inject = [{ in = "header", name = "A" }]\n'


@pytest.mark.parametrize(
    "bad",
    [
        HEAD + 'endpoint = "http://x"\n' + SLOT + 'mood = "x"\n',
        HEAD + 'endpoint = "http://x"\ninject = []\n',
        HEAD + 'endpoint = "http://x"\ninject = [{ in = "cookie", name = "A" }]\n',
        HEAD + 'endpoint = "http://x"\n' + SLOT + 'mode = "log"\n',
        HEAD + 'endpoint = "ftp://x"\n' + SLOT,
        HEAD + 'endpoint = "http://u@x"\n' + SLOT,
        HEAD + 'endpoint = "http://x"\ninject = [{ in = "header", name = "A", format = "x" }]\n',
        "version = 2\n",
        HEAD + 'endpoint = "http://x"\n' + SLOT + 'rules = [{ methods = ["FETCH"], paths = ["/"] }]\n',
        HEAD + 'endpoint = "http://x"\n' + SLOT + 'rules = [{ methods = ["GET"], paths = ["api"] }]\n',
        HEAD + 'endpoint = "http://x"\n' + SLOT + "max_response = 4096\nmax_upstream = 1024\n",
        HEAD + 'endpoint = "http://x"\n' + SLOT + "max_upstream = 134217728\n",
        HEAD + 'endpoint = "http://x"\n' + SLOT + "max_upstream = true\n",
    ],
)
def test_a_bad_policy_loads_no_binding_at_all(tmp_path, bad):
    with pytest.raises(eg.PolicyError):
        eg.parse_policy(tomllib.loads(bad))
    inj, _ = _inj(tmp_path, "http://127.0.0.1:9", pol=bad)
    assert inj.bindings == {} and inj.status()["policy"]["error"]
    assert inj.fetch(_req("ha", "http://127.0.0.1:9/api/states"))[0] == 404


def test_endpoints_resolve_from_the_secrets_folder_only(tmp_path):
    inj, _ = _inj(tmp_path, "http://127.0.0.1:9")
    st = inj.status()["bindings"]
    assert st["ha"]["ready"] and st["ha"]["endpoint"] == "http://127.0.0.1:9"
    assert st["radarr"]["ready"] and st["radarr"]["secret"] == "media.json:radarr.key"
    assert not st["gone"]["ready"] and st["gone"]["why"] == "no secret"
    code, x = inj.fetch(
        _req("gone", "http://claude-vault:8300/health", headers={"Authorization": "Bearer hub:secret:gone"})
    )
    assert code == 503 and x["error"] == "binding_unavailable"
    two = "ha http://127.0.0.1:9\nha http://127.0.0.1:10\n"
    inj2, _ = _inj(tmp_path, "http://127.0.0.1:9", **{"egress.endpoints": two})
    assert inj2.status()["bindings"]["ha"]["why"] == "endpoint listed twice"
    store = hr.Store.from_texts({"ha_token": tok(), "egress.endpoints": "ha http://127.0.0.1:9\n"})
    store.refused["egress.endpoints"] = "mode 0644"  # a file the owner's paste did not write right: fails closed
    (tmp_path / "p.toml").write_text(POL)
    inj3 = eg.Injector.load(str(tmp_path / "p.toml"), store, hr.Redactor(store), str(tmp_path / "state"))
    assert inj3.status()["bindings"]["ha"]["why"] == "endpoints file refused"


# ------------------------------------------------------------------------------------------------ decisions


def test_endpoint_binding_holds_in_every_mode(tmp_path, far):
    inj, _ = _inj(tmp_path, far)
    port = far.rsplit(":", 1)[1]
    h = {"Authorization": "Bearer hub:secret:ha"}
    for url in (f"http://127.0.0.2:{port}/api/states", f"https://127.0.0.1:{port}/api/states",
                f"http://127.0.0.1:{int(port) + 1}/api/states", "http://elsewhere.invalid/api/states",
                f"http://x@127.0.0.1:{port}/api/states"):  # fmt: skip
        code, x = inj.fetch(_req("ha", url, headers=h))
        assert code in (400, 403) and x["error"] in ("credential_endpoint_mismatch", "bad_request"), url
    assert Far.seen == []  # nothing left the hub
    for path in ("/api/states/../config", "/api/states/%2e%2e/x", "/api/states/a%2Fb", "/api//states"):
        code, x = inj.fetch(_req("ha", far + path, headers=h))
        assert code == 400 and x["error"] == "bad_path", path
    assert Far.seen == []


def test_placeholders_only_in_declared_slots(tmp_path, far):
    inj, v = _inj(tmp_path, far)
    ok = {"Authorization": "Bearer hub:secret:ha"}
    cases = [
        (_req("ha", far + "/api/states"), "no_placeholder"),
        (_req("ha", far + "/api/states", headers={"Authorization": "hub:secret:ha"}), "no_placeholder"),
        (_req("ha", far + "/api/states", headers={**ok, "X-Other": "hub:secret:ha"}), "placeholder_outside_slot"),
        (_req("ha", far + "/api/states?x=hub%3Asecret%3Aha", headers=ok), "placeholder_outside_slot"),
        (_req("ha", far + "/api/states", "POST", ok, b'{"t": "hub:secret:ha"}'), "placeholder_outside_slot"),
        (_req("ha", far + "/api/states", headers={**ok, "X-B": "hub:secret:radarr"}), "placeholder_mismatch"),
        (_req("ha", far + "/api/states", "POST", ok, b"hub%3Asecret%3Aradarr"), "placeholder_mismatch"),
    ]
    for a, want in cases:
        code, x = inj.fetch(a)
        assert code == 400 and x["error"] == want, (want, x)
    assert Far.seen == []
    assert v["ha"] not in _audit(tmp_path)


def test_audit_mode_logs_a_miss_and_sends_enforce_refuses_it(tmp_path, far):
    inj, v = _inj(tmp_path, far)
    code, x = inj.fetch(_req("ha", far + "/api/config", headers={"Authorization": "Bearer hub:secret:ha"}))
    assert code == 200 and x["decision"] == "would_deny" and x["status"] == 200
    assert Far.seen[-1]["headers"]["authorization"] == f"Bearer {v['ha']}"
    n = len(Far.seen)
    code, x = inj.fetch(_req("radarr", far + "/api/v3/movie/7", "DELETE", {"X-Api-Key": "hub:secret:radarr"}))
    assert code == 403 and x["error"] == "egress_denied" and len(Far.seen) == n  # enforce: never sent
    code, x = inj.fetch(_req("radarr", far + "/api/v3/queue/7", "DELETE", {"X-Api-Key": "hub:secret:radarr"}))
    assert code == 200 and x["decision"] == "allow" and Far.seen[-1]["headers"]["x-api-key"] == v["radarr"]
    code, x = inj.check(_req("radarr", far + "/api/v3/movie/7", "DELETE", {"X-Api-Key": "hub:secret:radarr"}))
    assert code == 200 and x == {"decision": "deny", "rule": None, "mode": "enforce"} and len(Far.seen) == n + 1
    st = inj.status()
    assert st["bindings"]["ha"]["count"]["fetch:would_deny"] == 1
    assert {"handle": "radarr", "method": "DELETE", "path": "/api/v3/movie/{n}"} in [
        {k: w[k] for k in ("handle", "method", "path")} for w in st["would_deny"]
    ]
    b = inj.brief()
    assert b["sent"] == 2 and b["would_deny"] == 1 and b["denied"] == 2
    log = _audit(tmp_path)
    assert v["ha"] not in log and v["radarr"] not in log and '"path": "/api/v3/movie/7"' in log


def test_query_and_path_slots_and_query_rules(tmp_path, far):
    inj, v = _inj(tmp_path, far)
    code, x = inj.fetch(_req("sab", far + "/api?mode=queue&apikey=hub%3Asecret%3Asab&output=json"))
    assert code == 200 and x["decision"] == "allow"
    assert Far.seen[-1]["path"] == f"/api?mode=queue&apikey={v['sab']}&output=json"
    code, x = inj.fetch(_req("sab", far + "/api?mode=delete&apikey=hub:secret:sab"))
    assert code == 403 and x["error"] == "egress_denied"
    code, x = inj.fetch(_req("tg", far + "/bothub:secret:tg/getMe", "POST", body=b"{}"))
    assert code == 200 and Far.seen[-1]["path"] == f"/bot{v['tg']}/getMe"
    assert v["tg"] not in base64.b64decode(x["body_b64"]).decode("latin-1")  # the echoed path is masked
    code, x = inj.fetch(_req("dash", far + "/live/health", headers={"Authorization": "Basic hub:secret:dash"}))
    assert (
        code == 200
        and Far.seen[-1]["headers"]["authorization"] == "Basic " + base64.b64encode(v["dash"].encode()).decode()
    )
    assert "Basic hub" not in json.dumps(x) and v["dash"] not in json.dumps(x)


def test_answers_are_masked_and_redirects_never_followed(tmp_path, far):
    inj, v = _inj(tmp_path, far)
    Far.value = v["ha"]
    h = {"Authorization": "Bearer hub:secret:ha"}
    code, x = inj.fetch(_req("ha", far + "/api/states", headers=h))
    body = base64.b64decode(x["body_b64"])
    assert code == 200 and v["ha"].encode() not in body and b"[redacted:ha_token]" in body
    assert b"\x00\xff\xfe" in body  # bytes that are no hit stay as they were
    assert x["redacted"] >= 2
    n = len(Far.seen)
    code, x = inj.fetch(_req("ha", far + "/redirect", headers=h))
    loc = dict((k.lower(), val) for k, val in x["headers"])["location"]
    assert code == 200 and x["status"] == 302 and v["ha"] not in loc and len(Far.seen) == n + 1
    code, x = inj.fetch(_req("ha", far + "/gzip", headers={**h, "Accept-Encoding": "gzip"}))
    assert code == 200 and json.loads(base64.b64decode(x["body_b64"])) == {"echo": "[redacted:ha_token]"}
    assert Far.seen[-1]["headers"].get("accept-encoding") == "identity"
    assert inj.fetch(_req("ha", far + "/br", headers=h))[1]["error"] == "encoded_response"
    assert inj.fetch(_req("ha", far + "/big", headers=h))[1]["error"] == "response_too_large"
    code, x = inj.fetch(_req("ha", far + "/denied", headers=h))
    assert code == 200 and x["status"] == 401 and v["ha"] not in json.dumps(x)
    Far.value = ""
    assert v["ha"] not in _audit(tmp_path) and v["ha"] not in json.dumps(inj.status())


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the folder mode")
def test_unreachable_far_side_and_audit_fail_closed(tmp_path):
    inj, v = _inj(tmp_path, "http://127.0.0.1:9")
    code, x = inj.fetch(_req("ha", "http://127.0.0.1:9/api/states", headers={"Authorization": "Bearer hub:secret:ha"}))
    assert code == 502 and x["error"] == "upstream_unreachable" and v["ha"] not in json.dumps(x)
    log = tmp_path / "state" / "egress" / "audit.jsonl"
    log.chmod(0o400)
    try:
        code, x = inj.fetch(
            _req("ha", "http://127.0.0.1:9/api/states", headers={"Authorization": "Bearer hub:secret:ha"})
        )
        assert code == 503 and x["error"] == "audit_unavailable"
    finally:
        log.chmod(0o600)


# ------------------------------------------------------------------------------------------------ hubd + release


def test_hubd_serves_egress_to_the_brain_only(tmp_path, monkeypatch, far):
    d = tmp_path / "secrets"
    d.mkdir(mode=0o700)
    v = tok(40)
    for n, t in {"token_brain": "B" * 40, "ha_token": v, "egress.endpoints": f"ha {far}\n"}.items():
        (d / n).write_text(t)
        (d / n).chmod(0o600)
    monkeypatch.setenv("HUB_SECRETS", str(d))
    monkeypatch.setenv("HUB_STATE", str(tmp_path / "state"))
    (tmp_path / "state").mkdir()
    hubd = _mod("hubd", "p312d")
    st = hubd.egress_state()
    assert st["state"] == "ok" and st["bindings"] >= 10 and st["ready"] == 1 and st["enforce"] == 0
    code, x = asyncio.run(hubd.egress_api("GET", "/egress/status", "tg", b""))
    assert code == 403
    code, x = asyncio.run(hubd.egress_api("GET", "/egress/status", "brain", b""))
    assert code == 200 and x["bindings"]["ha"]["ready"] and not x["bindings"]["vault"]["ready"]
    a = json.dumps(_req("ha", far + "/api/states", headers={"Authorization": "Bearer hub:secret:ha"})).encode()
    code, x = asyncio.run(hubd.egress_api("POST", "/egress/fetch", "brain", a))
    assert code == 200 and x["decision"] == "allow" and v not in json.dumps(x)
    code, x = asyncio.run(hubd.egress_api("POST", "/egress/check", "brain", a))
    assert code == 200 and x["decision"] == "allow"
    monkeypatch.setattr(hubd, "EGRESS", None)
    monkeypatch.setattr(hubd, "EGRESS_ERROR", "absent")
    assert asyncio.run(hubd.egress_api("GET", "/egress/status", "brain", b""))[0] == 503


def test_release_switch_keeps_the_running_digest(tmp_path):
    sys.path.insert(0, str(REPO / "packages"))
    try:
        from hubkit import release as R
    finally:
        sys.path.remove(str(REPO / "packages"))
    assert R.EGRESS_IN_RELEASE is True  # every release carries the egress files
    assert all((REPO / s).is_file() for s in R.EGRESS_FILES.values())
    rel = tmp_path / "rel"
    (rel / "app").mkdir(parents=True)
    for n in ("hubverify", "hubsecrets", "hubapprovals", "hubredact"):
        (rel / "app" / f"{n}.py").write_bytes((HUB / f"{n}.py").read_bytes())
    off = R.protected(rel)
    for dest, src in R.EGRESS_FILES.items():
        (rel / dest).parent.mkdir(parents=True, exist_ok=True)
        (rel / dest).write_bytes((REPO / src).read_bytes())
    on = R.protected(rel)
    assert set(R.EGRESS_FILES) <= set(on["files"]) and not set(R.EGRESS_FILES) & set(off["files"])
    assert on["digest"] != off["digest"]


# ---------------------------------------------------------------------------------------------------- paging


def test_json_page_cuts_whole_items_and_counts_them():
    body = json.dumps([{"a": i, "s": "ü" * 3} for i in range(10)], indent=3).encode()
    one = len(json.dumps({"a": 0, "s": "ü" * 3}, separators=(",", ":"), ensure_ascii=False).encode())
    got, start = [], 0
    while True:
        page, nxt, total = eg.json_page(body, start, 2 + 3 * one + 2)
        assert total == 10 and len(page) <= 2 + 3 * one + 2
        items = json.loads(page)
        assert len(items) == (nxt or total) - start
        got += items
        if nxt is None:
            break
        start = nxt
    assert got == json.loads(body)
    assert eg.json_page(b" [ ] ", 0, 10) == (b"[]", None, 0)
    assert eg.json_page(body, 10, 100) == (b"[]", None, 10)
    for bad, start, budget in [(b'{"a": 1}', 0, 99), (b"[1, 2", 0, 99), (b"[1] x", 0, 99), (body, 11, 9999),
                               (body, 0, one), (b"\xff[]", 0, 9)]:  # fmt: skip
        with pytest.raises(ValueError):
            eg.json_page(bad, start, budget)


def test_paged_get_reads_more_answers_pages_and_masks_each(tmp_path, far):
    inj, v = _inj(tmp_path, far)
    Far.value = v["radarr"]
    h = {"X-Api-Key": "hub:secret:radarr"}
    code, x = inj.fetch(_req("radarr", far + "/api/v3/movie", headers=h))
    assert code == 502 and x["error"] == "response_too_large"  # unpaged: the cap as before
    got, start, calls = [], 0, 0
    while True:
        a = dict(_req("radarr", far + "/api/v3/movie", headers=h), page={"from": start})
        code, x = inj.fetch(a)
        calls += 1
        body = base64.b64decode(x["body_b64"])
        assert code == 200 and x["status"] == 200 and len(body) <= 4096 and v["radarr"].encode() not in body
        assert x["page"]["from"] == start and x["page"]["total"] == 200
        got += json.loads(body)
        if x["page"]["next"] is None:
            break
        start = x["page"]["next"]
    assert [m["id"] for m in got] == list(range(1, 201)) and calls >= 3
    assert got[150]["echo"] == "[redacted:radarr_key]" or got[150]["echo"].startswith("[redacted:")
    aud = _audit(tmp_path)
    assert '"page_total": 200' in aud and v["radarr"] not in aud
    Far.value = ""


def test_paging_is_get_only_and_only_where_the_policy_allows_it(tmp_path, far):
    inj, v = _inj(tmp_path, far)
    h = {"X-Api-Key": "hub:secret:radarr"}
    for bad in ({"from": -1}, {"from": "0"}, {"from": True}, {"from": 0, "to": 9}, [0], 5):
        code, x = inj.fetch(dict(_req("radarr", far + "/api/v3/movie", headers=h), page=bad))
        assert code == 400 and x["error"] == "bad_request", bad
    code, x = inj.fetch(dict(_req("radarr", far + "/api/v3/queue/1", "DELETE", headers=h), page={"from": 0}))
    assert code == 400 and "GET only" in x["hint"]
    code, x = inj.fetch(dict(_req("radarr", far + "/api/v3/blob", headers=h), page={"from": 0}))
    assert code == 502 and x["error"] == "response_too_large" and "no JSON array" in x["hint"]
    code, x = inj.fetch(dict(_req("radarr", far + "/api/v3/movie/7", headers=h), page={"from": 0}))
    assert code == 200 and "page" not in x  # small and not an array: answered as it is
    ha = {"Authorization": "Bearer hub:secret:ha"}  # ha has no max_upstream: a page asks for nothing more
    code, x = inj.fetch(dict(_req("ha", far + "/big", headers=ha), page={"from": 0}))
    assert code == 502 and x["error"] == "response_too_large"


def test_repo_policy_pages_radarr_only_and_keeps_every_answer_under_the_cap():
    bs = eg.parse_policy(tomllib.loads(POLICY.read_text()))
    assert bs["radarr"].upstream >= 24 << 20 and bs["radarr"].max_response <= eg.MAX_RESPONSE
    assert [n for n, b in bs.items() if b.upstream > b.max_response] == ["radarr"]
