"""container-watch (weekly container security check, hub schedule Wed 19:13): offline, the repo's script staged in
a bin dir whose lib/ holds the repo's core, botlib and profile modules (as deployed; nothing from the live ~/bin).

Never touches the network, `ssh unraid`, the real state/cache/inbox or Telegram:
- a module fixture points HOME at a temp sandbox and the synthetic profile at tests/legacy/profile/fixtures before
  the script is loaded, and restores the environment, sys.path and sys.modules after; every test gets its own
  state/cache/inbox dirs (tmp_path).
- GitHub releases/advisories, NVD, Ubiquiti, Docker Hub, image registries (registry.example.org, Docker Hub's
  registry with its token realm), nodejs.org and endoflife.date are served from fixtures/container-watch/up/<layer>/
  (raw API JSON, RSS, HTML; fictional data) by FakeNet, patched over urllib.request.urlopen. Unknown hosts are
  recorded and refused.
- `ssh`, `unraid-ssh`, `ha` and `tg` are fakes in a temp bin dir that is first on PATH; CONTAINER_WATCH_TG,
  CONTAINER_WATCH_HA and UNRAID_SSH_BIN point at them too. The fake tg only writes the message to a file; the fake
  ha prints fixtures/container-watch/ha-updates.json (CW_HA_JSON) or [].
- The Cron tests run the real scheduled arguments (the job's argv in the repo's schedule registry,
  core/schedules.toml) in a subprocess through container_watch_boot.py, which blocks every socket and fixes the clock.
"""

import contextlib
import datetime as dt
import email.message
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import warnings
from pathlib import Path
from unittest import mock

import pytest

REPO = Path(__file__).resolve().parents[2]
HERE = os.path.dirname(os.path.abspath(__file__))
FX = os.path.join(HERE, "fixtures", "container-watch")
INV = os.path.join(FX, "inventory.txt")
BOOT = os.path.join(HERE, "container_watch_boot.py")
PROFILE = REPO / "tests" / "legacy" / "profile" / "fixtures" / "profile.json"  # synthetic, never the real one
SCHEDULES = REPO / "core" / "schedules.toml"  # the registry of the tree under test
NOW = dt.datetime(2026, 10, 14, 17, 13, tzinfo=dt.UTC)  # Wednesday 19:13 Zurich

FAKES = {
    "tg": r"""#!/usr/bin/env bash
# fake tg (container-watch tests): records the message, never sends anything
d="${CW_TG_DIR:?fake tg: CW_TG_DIR unset}"; mkdir -p "$d"
printf '%s' "$1" > "$d/$(date +%s%N).txt"
[ -n "${CW_TG_SOFT_LOG:-}" ] && printf 'soft=%s\n' "${TG_SOFT:-}" >> "$CW_TG_SOFT_LOG"
exit "${CW_TG_EXIT:-0}"
""",
    "ssh": r"""#!/usr/bin/env bash
# fake ssh: `ssh -G host` (host IP lookup), `ssh ... host bash -s` (collector on stdin) and the UniFi OS VM's two
# version reads (CW_UNIFI_NET = dpkg version of the Network app, CW_UNIFI_OS = uosserver version; CW_UNIFI_FAIL = 255)
echo "ssh $*" >> "${CW_SSH_LOG:-/dev/null}"
if [ "$1" = "-G" ]; then echo "hostname 127.0.0.9"; echo "user root"; exit 0; fi
case "$*" in
  *" unifi-os cd /tmp && sudo -n -u uosserver podman exec uosserver dpkg-query -W unifi")
    [ -n "${CW_UNIFI_FAIL:-}" ] && { echo "ssh: connect to host unifi-os port 22: No route to host" >&2; exit 255; }
    [ -n "${CW_UNIFI_NET:-}" ] || exit 1
    printf 'unifi\t%s\n' "$CW_UNIFI_NET"; exit 0;;
  *" unifi-os uosserver version")
    [ -n "${CW_UNIFI_FAIL:-}" ] && exit 255
    [ -n "${CW_UNIFI_OS:-}" ] || exit 1
    echo "$CW_UNIFI_OS"; exit 0;;
esac
stdin=$(cat)
case "$stdin" in *"@@containers"*) ;; *) echo "fake ssh: no collector on stdin" >&2; exit 97;; esac
printf '%s' "$stdin" > "${CW_SSH_STDIN:-/dev/null}"
if [ -n "${CW_SSH_FAIL:-}" ]; then echo "ssh: connect to host unraid port 22: Connection refused" >&2; exit 255; fi
cat "${CW_INVENTORY:?}"
""",
    "unraid-ssh": r"""#!/usr/bin/env bash
# fake ~/bin/unraid-ssh: `mode` and `--host H run containers`
echo "unraid-ssh $*" >> "${CW_SSH_LOG:-/dev/null}"
if [ "$1" = "mode" ]; then echo "${CW_LOCKDOWN_MODE:-root}"; exit 0; fi
[ "$3 $4" = "run containers" ] || { echo "fake unraid-ssh: unexpected $*" >&2; exit 98; }
[ -n "${CW_SSH_FAIL:-}" ] && exit 255
cat "${CW_INVENTORY:?}"
""",
    "ha": r"""#!/usr/bin/env bash
# fake ~/bin/ha: only `ha list update --json` (HA's update entities); prints $CW_HA_JSON or []
echo "ha $*" >> "${CW_HA_LOG:-/dev/null}"
[ "$1 $2 $3" = "list update --json" ] || { echo "fake ha: unexpected $*" >&2; exit 98; }
[ -n "${CW_HA_FAIL:-}" ] && { echo "ha: connection refused" >&2; exit 1; }
if [ -n "${CW_HA_JSON:-}" ]; then cat "$CW_HA_JSON"; else echo '[]'; fi
""",
}

# set by the module fixture `cw_sandbox` (the script is loaded once per module, inside the sandbox)
CW = SANDBOX = FAKEBIN = FAKE_TG = BIN = None


def stage(d: Path) -> str:
    """bin/container-watch + lib/ (core, botlib, assistant_profile) from the repo: nothing from the live ~/bin."""
    b = d / "bin"
    (b / "lib").mkdir(parents=True)
    shutil.copy2(REPO / "scripts" / "container-watch", b / "container-watch")
    (b / "lib" / "core").symlink_to(REPO / "core")
    (b / "lib" / "botlib.py").symlink_to(REPO / "scripts" / "lib" / "botlib.py")
    (b / "lib" / "assistant_profile.py").symlink_to(REPO / "scripts" / "lib" / "legacy" / "assistant_profile.py")
    return str(b / "container-watch")


@pytest.fixture(scope="module", autouse=True)
def cw_sandbox(tmp_path_factory):
    """HOME, PATH (fakes first) and the profile set BEFORE the script loads; all restored after the module."""
    global CW, SANDBOX, FAKEBIN, FAKE_TG, BIN
    SANDBOX = str(tmp_path_factory.mktemp("cw"))
    FAKEBIN = os.path.join(SANDBOX, "fakebin")
    os.makedirs(FAKEBIN)
    for n, text in FAKES.items():
        with open(os.path.join(FAKEBIN, n), "w") as f:
            f.write(text)
        os.chmod(os.path.join(FAKEBIN, n), 0o755)
    FAKE_TG = os.path.join(FAKEBIN, "tg")
    BIN = stage(Path(SANDBOX))
    home = os.path.join(SANDBOX, "home")
    os.makedirs(home)
    env = dict(
        HOME=home,
        PATH=FAKEBIN + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"),
        ASSISTANT_PROFILE=str(PROFILE),
        CONTAINER_WATCH_TG=FAKE_TG,
        CONTAINER_WATCH_HA=os.path.join(FAKEBIN, "ha"),
        UNRAID_SSH_BIN=os.path.join(FAKEBIN, "unraid-ssh"),
        CONTAINER_WATCH_UNIFI_HOST="",  # the UniFi OS VM only in the tests that turn it on (TestUnifiVm)
    )
    path0, mods0 = list(sys.path), set(sys.modules)
    with mock.patch.dict(os.environ, env):
        os.environ.pop("CONTAINER_WATCH_HOST", None)
        ld = importlib.machinery.SourceFileLoader("cw", BIN)
        CW = importlib.util.module_from_spec(importlib.util.spec_from_loader("cw", ld))
        ld.exec_module(CW)
        assert CW.TG_CMD == FAKE_TG and shutil.which("tg") == FAKE_TG, "refusing to run: tg is not the fake"
        assert CW.HA_CMD == os.path.join(FAKEBIN, "ha") and shutil.which("ha").startswith(FAKEBIN), "ha not the fake"
        assert str(CW.HOME).startswith(SANDBOX), "refusing to run: HOME is not the sandbox"
        try:
            yield
        finally:
            sys.path[:] = path0
            for m in set(sys.modules) - mods0:
                sys.modules.pop(m, None)
            CW = None


# ---------------------------------------------------------------- fake network
def _hdrs(d):
    m = email.message.Message()
    for k, v in (d or {}).items():
        m[k] = v
    return m


class FakeResp:
    def __init__(self, body, headers):
        self.body, self.headers = body, _hdrs(headers)

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeNet:
    """urlopen stand-in. layers: fixture dirs under fixtures/up, first match wins. mode: None, 'down',
    'ratelimit' (GitHub 403 + X-RateLimit-Remaining: 0), '429', '304' (GitHub answers If-None-Match with 304),
    'nvd-down' (NVD 503), 'reg429' (registries answer 429). Registries (registry.example.org, Docker Hub) want a
    Bearer token: 401 + challenge without one, the realm hands out 'test-token'."""

    REALMS = {
        "registry.example.org": "https://registry.example.org/token",
        "registry-1.docker.io": "https://auth.docker.io/token",
    }

    def __init__(self, *layers, mode=None):
        self.layers = [os.path.join(FX, "up", layer) for layer in (layers or ("base",))]
        self.mode, self.calls, self.unexpected = mode, [], []

    def _raw(self, rel):
        for d in self.layers:
            p = os.path.join(d, rel)
            if os.path.exists(p):
                with open(p, "rb") as f:
                    return f.read()
        return None

    def _body(self, rel, default):
        b = self._raw(rel)
        return json.dumps(default).encode() if b is None else b

    def _registry(self, url, req, u):
        if u.path == "/token":
            return FakeResp(json.dumps({"token": "test-token"}).encode(), {})
        if self.mode == "reg429":
            raise urllib.error.HTTPError(url, 429, "Too Many Requests", _hdrs({}), io.BytesIO(b""))
        if req.get_header("Authorization") != "Bearer test-token":
            ch = f'Bearer realm="{self.REALMS[u.netloc]}",service="{u.netloc}",scope="repository:x:pull"'
            raise urllib.error.HTTPError(url, 401, "Unauthorized", _hdrs({"WWW-Authenticate": ch}), io.BytesIO(b""))
        repo, kind, ref = re.fullmatch(r"/v2/(.+)/(blobs|manifests)/([^/]+)", u.path).groups()
        body = self._raw(f"reg/{u.netloc}/{repo.replace('/', '__')}/{kind}/{ref.replace(':', '_')}.json")
        if body is None:
            raise urllib.error.HTTPError(url, 404, "Not Found", _hdrs({}), io.BytesIO(b""))
        return FakeResp(body, {})

    def host_calls(self, host):
        return [u for u, _ in self.calls if urllib.parse.urlparse(u).netloc == host]

    def __call__(self, req, timeout=None, context=None):
        url = req.full_url
        self.calls.append((url, req))
        u = urllib.parse.urlparse(url)
        if self.mode == "down":
            raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
        headers = {}
        if u.netloc == "api.github.com":
            if self.mode == "ratelimit":
                raise urllib.error.HTTPError(
                    url, 403, "rate limit exceeded", _hdrs({"X-RateLimit-Remaining": "0"}), io.BytesIO(b"{}")
                )
            if self.mode == "429":
                raise urllib.error.HTTPError(url, 429, "Too Many Requests", _hdrs({}), io.BytesIO(b""))
            if self.mode == "304" and req.has_header("If-none-match"):
                raise urllib.error.HTTPError(
                    url, 304, "Not Modified", _hdrs({"X-RateLimit-Remaining": "58"}), io.BytesIO(b"")
                )
            m = re.fullmatch(r"/repos/([^/]+)/([^/]+)/(releases|security-advisories)", u.path)
            kind = "releases" if m.group(3) == "releases" else "advisories"
            body = self._body(f"gh/{m.group(1)}__{m.group(2)}.{kind}.json", [])
            headers = {"ETag": '"' + hashlib.sha1(body).hexdigest()[:12] + '"', "X-RateLimit-Remaining": "59"}
        elif u.netloc == "services.nvd.nist.gov":
            if self.mode == "nvd-down":
                raise urllib.error.HTTPError(url, 503, "Service Unavailable", _hdrs({}), io.BytesIO(b""))
            cpe = urllib.parse.parse_qs(u.query)["virtualMatchString"][0].split(":")
            body = self._body(f"nvd/{cpe[3]}__{cpe[4]}.json", {"vulnerabilities": []})
        elif u.netloc == "www.cisa.gov":
            body = self._body("kev.json", {"vulnerabilities": []})
        elif u.netloc == "fw-update.ubnt.com":
            body = self._body("ubiquiti.json", {"_embedded": {"firmware": []}})
        elif u.netloc == "download.svc.ui.com" and u.path == "/v1/software-downloads":
            body = self._body("ui-downloads.json", {"downloads": []})
        elif u.netloc == "hub.docker.com":
            ns, repo = re.fullmatch(r"/v2/repositories/([^/]+)/([^/]+)/tags", u.path).groups()
            body = self._body(f"hub/{ns}__{repo}.json", {"results": []})
        elif u.netloc in ("registry.example.org", "registry-1.docker.io", "auth.docker.io"):
            return self._registry(url, req, u)
        elif u.netloc == "nodejs.org":
            if u.path == "/dist/index.json":
                body = self._body("node/index.json", [])
            elif u.path == "/en/feed/vulnerability.xml":
                body = self._raw("node/vulnerability.xml") or b"<rss></rss>"
            else:
                body = self._raw("node/" + u.path.rsplit("/", 1)[1] + ".html")
                if body is None:
                    raise urllib.error.HTTPError(url, 404, "Not Found", _hdrs({}), io.BytesIO(b""))
        elif u.netloc == "endoflife.date":
            body = self._body("eol/" + u.path.rsplit("/", 1)[1], [])
        elif u.netloc == "raw.githubusercontent.com":  # arr openapi.json: raw/<owner>__<repo>__<ref>__<path>
            body = self._raw("raw/" + u.path.strip("/").replace("/", "__"))
            if body is None:
                raise urllib.error.HTTPError(url, 404, "Not Found", _hdrs({}), io.BytesIO(b""))
        else:
            self.unexpected.append(url)
            raise urllib.error.URLError("unexpected host in test: " + u.netloc)
        return FakeResp(body, headers)


class FakeUp:
    """Upstream stand-in for evaluate() unit tests (already-trimmed data)."""

    def __init__(
        self,
        releases=None,
        advisories=None,
        nvd=None,
        ubi=(None, None),
        hub=None,
        kev=None,
        configs=None,
        node=None,
        posts=None,
        post=None,
        eol=None,
        ui=None,
    ):
        self.r, self.a, self.n, self.ubi, self.h = releases or {}, advisories or {}, nvd or {}, ubi, hub or {}
        self.k, self.cfg, self.nd, self.ps, self.p, self.e = kev, configs or {}, node, posts, post or {}, eol or {}
        self.ui, self.ui_calls = ui, 0
        self.cfg_calls = []

    def ui_downloads(self):
        self.ui_calls += 1
        return self.ui

    def kev(self):
        return self.k

    def image_config(self, image, digest=None, alt=(), by_tag=False):
        self.cfg_calls.append((image, digest, by_tag))
        return self.cfg.get(image)

    def node_releases(self):
        return self.nd

    def node_posts(self):
        return self.ps

    def node_post(self, url):
        return self.p.get(url)

    def eol(self, product):
        return self.e.get(product)

    def releases(self, repo):
        return self.r.get(repo)

    def advisories(self, repo):
        return self.a.get(repo, [])

    def nvd(self, vp, version):
        return self.n.get(vp, [])

    def ubiquiti_latest(self):
        return self.ubi

    def hub_tags(self, repo):
        return self.h.get(repo, [])


# ---------------------------------------------------------------- helpers
def rel(tag, pub, body="", pre=False):
    return dict(
        tag=tag,
        name=tag,
        published=pub,
        pre=pre,
        draft=False,
        body=body,
        url=f"https://github.com/x/y/releases/tag/{tag}",
    )


def adv(gid, sev, rng, patched, pub="2026-09-01T00:00:00Z", cve=None, summary="Some bug", withdrawn=None):
    return dict(
        id=gid,
        cve=cve,
        summary=summary,
        severity=sev,
        score=None,
        published=pub,
        withdrawn=withdrawn,
        state="published",
        url=f"https://github.com/x/y/security/advisories/{gid}",
        vulns=[dict(pkg="y", range=rng, patched=patched)],
    )


def mini_inv(
    image="lscr.io/linuxserver/radarr:latest", version="5.8.3.8933-ls230", name="radarr", status=None, env=None
):
    c = dict(
        name="/" + name,
        image=image,
        imageId="sha256:x",
        state="running",
        startedAt="",
        created="",
        labels={},
        net="bridge",
        ports={},
        ips="",
    )
    i = dict(
        id="sha256:x",
        tags=[image],
        digests=[],
        created="2026-09-01T00:00:00Z",
        labels={"org.opencontainers.image.version": version} if version else {},
    )
    st = {CW.unraid_status_key(image): {"status": status}} if status else {}
    lines = ["@@meta", "@@containers", json.dumps(c), "@@env"]
    lines += [f"/{name}\t{k}={v}" for k, v in (env or {}).items()]
    lines += [
        "@@images",
        json.dumps(i),
        "@@history",
        "@@unraid-status",
        json.dumps(st),
        "@@autoupdate",
        "{}",
        "@@mdns",
        "@@end",
    ]
    return CW.build_inventory("\n".join(lines))


RADARR_RELS = [
    rel("v5.8.3.8933", "2026-08-30T10:00:00Z"),
    rel("v5.9.1.9070", "2026-09-20T10:00:00Z"),
    rel("v5.10.0.9100", "2026-10-13T10:00:00Z"),
]


def evaluate(advs=(), rels=None, grace=3, state=None, **inv_kw):
    up = FakeUp(
        releases={"Radarr/Radarr": RADARR_RELS if rels is None else rels}, advisories={"Radarr/Radarr": list(advs)}
    )
    st = {} if state is None else state
    rows, items = CW.evaluate(mini_inv(**inv_kw), CW.Local(False), up, NOW, grace, st)
    return rows[0], items, st


class Base:
    @pytest.fixture(autouse=True)
    def _base(self, tmp_path):
        warnings.simplefilter("ignore", ResourceWarning)  # main() leaves its lock file to the GC
        self.d = str(tmp_path)
        self.state, self.cache, self.inbox, self.tgdir = (
            os.path.join(self.d, x) for x in ("state", "cache", "inbox", "tg")
        )
        self.sshlog = os.path.join(self.d, "ssh.log")
        self.env = dict(
            CW_TG_DIR=self.tgdir,
            CW_TG_EXIT="0",
            CW_SSH_LOG=self.sshlog,
            CW_INVENTORY=INV,
            CW_SSH_STDIN=os.path.join(self.d, "ssh-stdin.txt"),
        )
        with mock.patch.dict(os.environ, self.env):  # also undoes a test's own os.environ writes
            assert CW.TG_CMD == FAKE_TG
            yield

    def run_main(self, *args, net=None, input_file=INV, now=NOW, state=None, cache=None, inbox=None):
        argv = (
            [
                "container-watch",
                "--state-dir",
                state or self.state,
                "--cache-dir",
                cache or self.cache,
                "--inbox-dir",
                inbox or self.inbox,
            ]
            + (["--input", input_file] if input_file else [])
            + list(args)
        )
        self.net = net or FakeNet("base")
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch("urllib.request.urlopen", self.net),
            mock.patch.object(CW, "now_utc", return_value=now),
            mock.patch("time.sleep") as self.sleep,
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            rc = CW.main()
        assert self.net.unexpected == [], "contacted an unexpected host"
        return rc, out.getvalue(), err.getvalue()

    def sent(self):
        if not os.path.isdir(self.tgdir):
            return []
        out = []
        for n in sorted(os.listdir(self.tgdir)):
            with open(os.path.join(self.tgdir, n)) as f:
                out.append(f.read())
        return out

    def _inbox(self):
        return sorted(os.listdir(self.inbox)) if os.path.isdir(self.inbox) else []

    def notes(self):
        return [n for n in self._inbox() if n.startswith("container-watch-") and "errors" not in n]

    def error_notes(self):
        return [n for n in self._inbox() if n.startswith("container-watch-errors-")]

    def statej(self):
        with open(os.path.join(self.state, "state.json")) as f:
            return json.load(f)


# ================================================================ version parsing and comparison
class TestVersions:
    def test_vparse_vclean(self):
        assert CW.vparse("v4.0.20.3014-ls326") == (4, 0, 20, 3014)
        assert CW.vclean("v4.0.20.3014-ls326") == "4.0.20.3014"
        assert CW.vclean("5.21.1.9799-ls123") == "5.21.1.9799"
        assert CW.vparse("version-5.21.1.9799") == (5, 21, 1, 9799)
        assert CW.vclean("5.1.3-1-01") == "5.1.3"  # binhex RELEASETAG
        assert CW.vclean("9.0.114-unraid") == "9.0.114"
        assert CW.vparse("2026.9.4") == (2026, 9, 4)
        assert CW.vparse("latest") is None
        assert CW.vparse(None) is None
        assert CW.vclean("latest") == "latest"

    def test_vcmp_numeric_not_lexical(self):
        v = lambda a, b: CW.vcmp(CW.vparse(a), CW.vparse(b))  # noqa: E731
        assert v("1.10.0", "1.9.0") == 1
        assert v("4.0.20.3014", "4.0.20") == 1
        assert v("1.0", "1.0.0") == 0
        assert v("v5.10.0.9100", "5.8.3.8933-ls230") == 1

    def test_date_tags(self):
        v = lambda a, b: CW.vcmp(CW.vparse(a), CW.vparse(b))  # noqa: E731
        assert v("2026.10.0", "2026.9.4") == 1  # Home Assistant
        assert v("2025.12.3", "2026.1.0") == -1
        assert v("26.10.1", "26.09.2") == 1  # jlesage vYY.MM.N
        assert CW.is_pre("2026.10.0b3")
        assert not CW.is_pre("2026.10.0")

    def test_vkey_prerelease_and_build_suffixes(self):
        k = CW.vkey
        assert CW.kcmp(k("1.6.2-beta.3"), k("1.6.2")) == -1
        assert CW.kcmp(k("1.6.2"), k("1.6.3")) == -1
        assert CW.kcmp(k("1.6.2-rc1"), k("1.6.2")) == -1
        assert CW.kcmp(k("1.6.2-beta.2"), k("1.6.2-beta.10")) == -1
        for build in ("5.1.3-ls326", "5.1.3-r0", "5.1.3-1-01", "v5.1.3", "5.1.3-alpine", "5.1.3-unraid"):
            assert CW.kcmp(k(build), k("5.1.3")) == 0, build
            assert not CW.is_pre(build), build
        assert k("latest") is None

    def test_stable_releases_filter(self):
        rels = [
            rel("v2.0.0", "2026-01-01"),
            rel("v2.1.0-beta.1", "2026-01-02"),
            rel("v2.1.0-rc.2", "2026-01-03"),
            rel("4.3.3RC1", "2026-01-04"),
            rel("2026.10.0b3", "2026-01-05"),
            rel("nightly-2026", "2026-01-06"),
            rel("v2.0.1", "2026-01-07", pre=True),
            dict(rel("v2.0.2", "2026-01-08"), draft=True),
            rel("2026082801", "2026-01-09"),
            rel("v2.0.3-ls12", "2026-01-10"),
        ]
        assert [r["tag"] for r in CW.stable_releases(rels)] == ["v2.0.0", "v2.0.3-ls12"]

    def test_image_repo_tag_and_unraid_key(self):
        assert CW.image_repo_tag("lscr.io/linuxserver/radarr:latest") == ("lscr.io", "linuxserver/radarr", "latest")
        assert CW.image_repo_tag("redis:7.2.4") == ("", "redis", "7.2.4")
        assert CW.image_repo_tag("docker.io/binhex/arch-sabnzbd") == ("", "binhex/arch-sabnzbd", "latest")
        assert CW.image_repo_tag("localhost:5000/foo:1.0") == ("localhost:5000", "foo", "1.0")
        assert CW.image_repo_tag("ghcr.io/x/y:1.2@sha256:abc") == ("ghcr.io", "x/y", "1.2")
        assert CW.unraid_status_key("redis:7.2.4") == "library/redis:7.2.4"
        assert CW.unraid_status_key("linuxserver/sonarr") == "linuxserver/sonarr:latest"
        assert CW.unraid_status_key("lscr.io/linuxserver/radarr") == "lscr.io/linuxserver/radarr:latest"

    def test_parse_iso(self):
        assert CW.parse_iso("2026-10-01T02:00:34.88731859Z") == dt.datetime(
            2026, 10, 1, 2, 0, 34, 887318, tzinfo=dt.UTC
        )
        assert CW.parse_iso("2026-10-01T04:00:00+0200") == dt.datetime(2026, 10, 1, 2, tzinfo=dt.UTC)
        assert CW.parse_iso(0) == dt.datetime(1970, 1, 1, tzinfo=dt.UTC)
        assert CW.parse_iso("soon") is None
        assert CW.parse_iso("") is None

    def test_installed_version_sources(self):
        inv = CW.build_inventory(open(INV).read())
        by = {c["name"]: c for c in inv["containers"]}
        local = CW.Local(False)
        app = lambda key: next(a for a in CW.APPS if a["key"] == key)  # noqa: E731
        assert CW.installed_version(app("radarr"), by["radarr"], inv, local) == ("5.8.3.8933", "label")
        assert CW.installed_version(app("sabnzbd"), by["binhex-sabnzbd"], inv, local) == ("4.3.2", "history")
        assert CW.installed_version(app("homeassistant"), by["HomeAssistant_inabox"], inv, local) == (
            "2026.9.4",
            "mdns:ha",
        )
        assert CW.installed_version(app("redis"), by["paperless_redis"], inv, local) == ("7.2.4", "env:REDIS_VERSION")
        assert CW.installed_version(app("emby"), by["EmbyServer"], inv, local) == (None, None)
        # HA's vpat rejects a non-calendar version even if a label has one
        c = dict(by["HomeAssistant_inabox"], img={"labels": {"org.opencontainers.image.version": "1.2.3"}})
        assert CW.installed_version(app("homeassistant"), c, dict(inv, mdns=[]), local) == (None, None)
        assert (
            CW.label_version({"labels": {"build_version": "Linuxserver.io version:- 1.6.2-ls99 Build-date:- x"}})
            == "1.6.2-ls99"
        )

    def test_inventory_parsing(self):
        inv = CW.build_inventory(open(INV).read())
        names = [c["name"] for c in inv["containers"]]
        assert "claude-bot" in names
        assert inv["mdns"][0]["version"] == "2026.9.4"
        assert inv["mdns"][0]["ip"] == "198.51.100.50"
        assert inv["unraid_status"]["lscr.io/linuxserver/radarr:latest"]["status"] == "false"
        assert inv["autoupdate"]["containers"]["radarr"]["update"]
        assert inv["meta"]["now"] == "2026-10-14T17:13:00Z"
        assert next(c for c in inv["containers"] if c["name"] == "unifi-controller")["env"] == {
            "APP_VERSION": "9.0.114"
        }


# ================================================================ advisory matching
class TestRanges:
    def test_parse_range(self):
        k = CW.vkey
        assert CW.parse_range("< 1.2.3") == [[("<", k("1.2.3"))]]
        assert CW.parse_range(">= 1.0.0, < 1.2.3") == [[(">=", k("1.0.0")), ("<", k("1.2.3"))]]
        assert CW.parse_range(">=1.0.0 <2.0.0") == [[(">=", k("1.0.0")), ("<", k("2.0.0"))]]
        assert CW.parse_range("1.0.0 - 2.0.0") == [[(">=", k("1.0.0")), ("<=", k("2.0.0"))]]
        assert len(CW.parse_range("< 2.0 || >= 3.0, < 3.1.2")) == 2
        assert CW.parse_range("= 1.4.0") == [[("=", k("1.4.0"))]]
        for bad in ("All", "TBD", "before 2024-01-01", "", None):
            assert CW.parse_range(bad) is None, bad

    def test_in_range_bounds(self):
        r = CW.parse_range(">= 1.0.0, < 1.2.3")
        for v, want in (("0.9.9", False), ("1.0.0", True), ("1.2.2", True), ("1.2.3", False), ("1.2.3-beta.1", True)):
            assert CW.in_range(CW.vkey(v), r) == want, v
        r = CW.parse_range("<= 1.2.3")
        assert CW.in_range(CW.vkey("1.2.3-ls12"), r)
        assert not CW.in_range(CW.vkey("1.2.4"), r)
        assert CW.upper_bounded(r)
        assert not CW.upper_bounded(CW.parse_range(">= 7.0.0"))

    def test_is_patched_per_branch(self):
        p = ["6.2.16", "7.2.6", "7.4.1"]
        for v, want in (("7.2.7", True), ("7.2.5", False), ("6.2.16", True), ("7.3.0", False), ("8.0.0", True)):
            assert CW.is_patched(CW.vkey(v), p) == want, v
        assert not CW.is_patched(CW.vkey("1.0"), [])

    def g(self, installed, rng, patched, **kw):
        return CW.ghsa_affects(
            installed,
            dict(vulns=[dict(range=rng, patched=patched)], published=kw.pop("pub", "2026-09-01T00:00:00Z")),
            **kw,
        )

    def test_affected_with_fix(self):
        assert self.g("1.2.0", "< 1.2.3", "1.2.3") == (True, "1.2.3", "")
        assert self.g("5.8.3.8933", "< 5.10.0.9100", "5.10.0.9100")[:2] == (True, "5.10.0.9100")
        assert self.g("7.2.4", ">= 7.0.0, < 7.2.6", "6.2.16, 7.2.6, 7.4.1")[:2] == (True, "7.2.6")
        assert self.g("6.2.10", "< 6.2.16 || >= 7.0.0, < 7.2.6", "6.2.16, 7.2.6")[:2] == (True, "6.2.16")

    def test_fixed_and_unaffected(self):
        assert not self.g("1.2.3", "< 1.2.3", "1.2.3")[0]  # exactly the fix
        assert not self.g("1.2.3-ls40", "< 1.2.3", "1.2.3")[0]  # linuxserver build of the fix
        assert not self.g("0.9.0", ">= 1.0.0, < 1.2.3", "1.2.3")[0]  # below the range
        assert not self.g("7.2.7", ">= 7.0.0", "7.2.6, 7.4.1")[0]  # open range, own-branch fix
        assert not self.g("2.0.0", "All", "1.9.9")[0]  # unparseable range, patched

    def test_unparseable_or_open_ended(self):
        assert self.g("1.0.0", "All", "1.9.9")[0]
        assert self.g("1.0.0", "All", "")[0] is None
        assert self.g("1.0.0", ">= 0.5.0", "", installed_date="2026-08-01T00:00:00Z")[0] is None
        hit, fix, note = self.g("1.0.0", ">= 0.5.0", "", installed_date="2026-10-01T00:00:00Z")
        assert hit is False
        assert "assumed fixed" in note
        assert CW.ghsa_affects("1.0.0", dict(vulns=[], published=None))[0] is None

    def test_fix_only_in_prerelease(self):
        hit, fix, note = self.g("1.9.0", "< 2.0.0-beta.3", "2.0.0-beta.3", stable_versions=["1.9.0", "2.0.0", "2.0.1"])
        assert (hit, fix) == (True, "2.0.0")  # first stable after the beta
        hit, fix, note = self.g("1.9.0", "< 2.0.0-beta.3", "2.0.0-beta.3", stable_versions=["1.9.0"])
        assert (hit, fix) == (True, None)
        assert "fix only in pre-release 2.0.0-beta.3" in note

    def test_nvd_affects(self):
        cpe = "cpe:2.3:a:ui:unifi_network_application:*:*:*:*:*:*:*:*"
        vp = "ui:unifi_network_application"
        n = lambda v, **m: CW.nvd_affects(v, dict(matches=[dict(criteria=m.pop("criteria", cpe), **m)]), vp)  # noqa: E731
        assert n("9.0.114", versionEndExcluding="9.0.118") == (True, "9.0.118")
        assert n("9.0.118", versionEndExcluding="9.0.118") == (False, None)
        assert n("9.0.118", versionEndIncluding="9.0.118") == (True, None)
        assert n("7.9.0", versionStartIncluding="8.0.0", versionEndExcluding="9.0.118") == (False, None)
        assert n("8.0.0", versionStartExcluding="8.0.0") == (False, None)
        assert n("9.0.114", criteria="cpe:2.3:a:ui:unifi_network_application:9.0.114:*:*:*:*:*:*:*") == (True, None)
        assert n("9.0.115", criteria="cpe:2.3:a:ui:unifi_network_application:9.0.114:*:*:*:*:*:*:*") == (False, None)
        assert n("1.0.0", criteria="cpe:2.3:a:ui:unifi_protect:*:*:*:*:*:*:*:*") == (False, None)
        assert (
            CW.nvd_short("An attacker could exploit a remote code execution vulnerability.") == "remote code execution"
        )

    def test_sec_snippet(self):
        assert "CVE-2026-1" in CW.sec_snippet("- New: x\n- Fixed: **auth** bypass (CVE-2026-1234)")
        assert CW.sec_snippet("Security: fix an issue in the API key check") is not None
        assert CW.sec_snippet("- Added SECURITY.md\n- security-opt docs") is None
        assert CW.sec_snippet("- New: calendar view") is None


# ================================================================ evaluate(): which findings become alerts
class TestEvaluate:
    def test_severity_and_grace(self):
        _, items, _ = evaluate([adv("GHSA-low", "low", "< 5.9.1.9070", "5.9.1.9070")])
        assert items == []  # LOW never alerts
        row, items, _ = evaluate([adv("GHSA-med", "moderate", "< 5.9.1.9070", "5.9.1.9070", cve="CVE-2026-1")])
        assert [(i["kind"], i["key"], i["fixed"], i["ids"]) for i in items] == [
            ("advisory", "adv:radarr:GHSA-med", "5.9.1.9070", ["CVE-2026-1"])
        ]
        assert row["installed"] == "5.8.3.8933"
        fresh = adv("GHSA-new", "high", "< 5.10.0.9100", "5.10.0.9100")  # fix released 1 day ago
        assert evaluate([fresh])[1] == []
        assert len(evaluate([fresh], grace=0)[1]) == 1

    def test_unfixed_only_high(self):
        hi = adv("GHSA-hi", "critical", "<= 5.10.0.9100", "")
        assert [i["kind"] for i in evaluate([hi])[1]] == ["unfixed"]
        assert evaluate([adv("GHSA-md", "medium", "<= 5.10.0.9100", "")])[1] == []

    def test_unknown_withdrawn_unaffected_stay_silent(self):
        advs = [
            adv("GHSA-open", "critical", ">= 5.0.0", "", pub="2026-10-10T00:00:00Z"),
            adv("GHSA-gone", "critical", "< 9.0.0", "9.0.0", withdrawn="2026-10-01T00:00:00Z"),
            adv("GHSA-old", "high", "< 5.0.0", "5.0.0"),
            adv("GHSA-same", "high", ">= 5.0.0", "", pub="2026-08-01T00:00:00Z"),
        ]
        row, items, _ = evaluate(advs)
        assert items == []
        assert row["unknown_range"] == ["GHSA-open"]
        assert row["assumed_fixed"] == ["GHSA-same"]
        assert row["advisories_total"] == 3

    def test_security_release(self):
        rels = RADARR_RELS[:1] + [
            rel("v5.9.1.9070", "2026-09-20T10:00:00Z", "- Fixed: XSS (CVE-2026-1234)"),
            rel("v5.9.2.9080", "2026-10-05T10:00:00Z", "## Security\n- hardening"),
            rel("v5.10.0.9100", "2026-10-13T10:00:00Z", "security fix"),
        ]
        row, items, _ = evaluate(rels=rels)
        assert len(items) == 1
        i = items[0]
        assert (i["kind"], i["key"], i["count"], i["fixed"]) == (
            "security-release",
            "rel:radarr:5.9.2.9080",
            2,
            "5.9.2.9080",
        )
        assert "2 newer releases mention security fixes (5.9.1.9070 … 5.9.2.9080)" in i["why"]
        assert len(row["newer_security"]) == 3  # the 1-day-old one waits
        quiet = RADARR_RELS[:1] + [rel("v5.9.1.9070", "2026-09-20T10:00:00Z", "- Added SECURITY.md")]
        assert evaluate(rels=quiet)[1] == []

    def test_stale_update(self):
        key = "lscr.io/linuxserver/radarr:latest"
        rels = RADARR_RELS[:1] + [rel(f"v5.9.{n}.9000", f"2026-09-0{n}T00:00:00Z") for n in (1, 2, 3)]
        st = {"update_avail_since": {key: "2026-09-01T00:00:00+00:00"}}
        _, items, _ = evaluate(rels=rels, status="false", state=st)
        assert [(i["kind"], i["key"]) for i in items] == [("stale", "stale:radarr:2026-09-01")]
        st = {"update_avail_since": {key: "2026-09-20T00:00:00+00:00"}}
        assert evaluate(rels=rels, status="false", state=st)[1] == []  # < 30 days
        _, _, st = evaluate(rels=rels, status=None, state={})
        assert st["update_avail_since"] == {}
        _, _, st = evaluate(rels=rels, status="false", state={})
        assert st["update_avail_since"] == {key: NOW.isoformat(timespec="seconds")}
        _, _, st = evaluate(rels=rels, status="true", state=st)
        assert st["update_avail_since"] == {}

    def test_self_and_unknown_containers_not_judged(self):
        inv = CW.build_inventory(open(INV).read())
        up = FakeUp(releases={}, advisories={})
        rows, items = CW.evaluate(inv, CW.Local(False), up, NOW, 3, {})
        names = [r["container"] for r in rows]
        assert "claude-bot" not in names
        assert next(r for r in rows if r["container"] == "tdarr")["app"] is None
        assert "installed version unknown" in next(r for r in rows if r["container"] == "EmbyServer")["notes"]

    def test_ghsa_and_nvd_same_cve_counted_once(self):
        up = FakeUp(
            releases={"transmission/transmission": [rel("4.0.5", "2026-01-01T00:00:00Z")]},
            advisories={"transmission/transmission": [adv("GHSA-t1", "high", "< 4.0.6", "4.0.6", cve="CVE-2026-7")]},
            nvd={
                "transmissionbt:transmission": [
                    dict(
                        id="CVE-2026-7",
                        published="2026-09-01T00:00:00",
                        status="Analyzed",
                        severity="high",
                        score=8.0,
                        desc="x",
                        matches=[
                            dict(
                                criteria="cpe:2.3:a:transmissionbt:transmission:*:*:*:*:*:*:*:*",
                                versionEndExcluding="4.0.6",
                            )
                        ],
                    )
                ]
            },
        )
        inv = mini_inv(image="linuxserver/transmission", version="4.0.5-r0-ls200", name="transmission")
        rows, items = CW.evaluate(inv, CW.Local(False), up, NOW, 3, {})
        assert len(rows[0]["affecting"]) == 1
        assert len(items) == 1

    def test_kev_listed_cve_is_actionable_at_once(self):
        """A CVE in CISA's KEV catalogue: actionable even inside the grace days and at low severity, CRITICAL,
        own kev: key (so it re-alerts after the plain advisory was sent), marked in the message line."""
        fresh = (NOW - dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        up = FakeUp(
            releases={"Radarr/Radarr": [rel("5.8.3.8933", "2026-01-01T00:00:00Z"), rel("5.8.4.9000", fresh)]},
            advisories={
                "Radarr/Radarr": [adv("GHSA-k1", "low", "< 5.8.4.9000", "5.8.4.9000", cve="CVE-2026-5555", pub=fresh)]
            },
            kev={"CVE-2026-5555": dict(added="2026-10-01", due="2026-10-22", ransomware=True, name="x")},
        )
        rows, items = CW.evaluate(mini_inv(), CW.Local(False), up, NOW, 3, {})
        assert rows[0]["affecting"][0]["kev"] == "2026-10-01"
        assert rows[0]["affecting"][0]["kev_ransomware"]
        assert [(i["key"], i["sev"]) for i in items] == [("kev:radarr:CVE-2026-5555", 4)]
        assert "KEV" in CW.container_line("radarr", items)
        assert "KEV CRITICAL" in CW.print_actionable(items)
        # same advisory without KEV: low + fixed 1 day ago -> not actionable
        up.k = {}
        assert CW.evaluate(mini_inv(), CW.Local(False), up, NOW, 3, {})[1] == []

    def test_kev_unavailable_changes_nothing(self):
        up = FakeUp(
            releases={"Radarr/Radarr": [rel("5.8.3.8933", "2026-01-01T00:00:00Z")]},
            advisories={"Radarr/Radarr": [adv("GHSA-k2", "high", "< 5.9", "5.9", cve="CVE-2026-6")]},
            kev=None,
        )
        rows, items = CW.evaluate(mini_inv(), CW.Local(False), up, NOW, 3, {})
        assert "kev" not in rows[0]["affecting"][0]
        assert [i["key"] for i in items] == ["adv:radarr:GHSA-k2"]

    def test_kev_trim(self):
        up = CW.Upstream.__new__(CW.Upstream)
        captured = {}
        up.get = lambda url, kind, trim: captured.setdefault(
            "d",
            trim(
                {
                    "vulnerabilities": [
                        {
                            "cveID": "CVE-2026-1234",
                            "dateAdded": "2026-10-01",
                            "dueDate": "2026-10-22",
                            "knownRansomwareCampaignUse": "Known",
                            "vulnerabilityName": "Foo RCE",
                        },
                        {"cveID": "not-a-cve"},
                    ]
                }
            ),
        )
        d = up.kev()
        assert list(d) == ["CVE-2026-1234"]
        assert d["CVE-2026-1234"]["ransomware"]
        assert up.kev_loaded
        assert up.kev_count == 1

    def test_pinned_hub_tag_same_flavour(self):
        up = FakeUp(
            releases={"redis/redis": []},
            hub={"redis": [{"name": n} for n in ("latest", "8.0.0", "7.4.1-alpine", "7.4.2", "7.2.6-alpine")]},
        )
        inv = mini_inv(image="redis:7.2.4-alpine", version=None, name="redis", env={"REDIS_VERSION": "7.2.4"})
        rows, _ = CW.evaluate(inv, CW.Local(False), up, NOW, 3, {})
        assert rows[0]["pinned"]
        assert rows[0]["newest_tag"] == "7.4.1-alpine"


# ================================================================ bugs found while writing these tests
class TestBugs:
    def test_prerelease_kinds_are_ordered(self):
        """BUG 1 (fixed 2026-10-14): vkey ranked every pre-release by its number only, so 2.0.0-alpha.5 sorted
        above 2.0.0-beta.1 and an installed 2.0.0-beta.3 looked newer than 2.0.0-rc.1, so it was 'not affected'
        by an advisory '< 2.0.0-rc.1'."""
        order = ["2.0.0.dev1", "2.0.0-alpha.5", "2.0.0-beta.1", "2.0.0-rc.1", "2.0.0"]
        for a, b in zip(order, order[1:], strict=False):
            assert CW.kcmp(CW.vkey(a), CW.vkey(b)) == -1, f"{a} < {b}"
        assert CW.kcmp(CW.vkey("2.0.0b2"), CW.vkey("2.0.0rc1")) == -1
        assert CW.kcmp(CW.vkey("2.0.0a9"), CW.vkey("2.0.0b1")) == -1
        hit = CW.ghsa_affects(
            "2.0.0-beta.3",
            dict(vulns=[dict(range="< 2.0.0-rc.1", patched="2.0.0-rc.1")], published="2026-09-01T00:00:00Z"),
        )
        assert hit[0] is True

    def test_pinned_linuxserver_tag_finds_newer_build(self):
        """BUG 2 (fixed 2026-10-14): for a pinned linuxserver tag like 5.8.3.8933-ls230 the Docker Hub check
        only accepted tags ending in the very same -ls230 build, so it never reported a newer tag."""
        up = FakeUp(
            releases={"Radarr/Radarr": RADARR_RELS},
            hub={
                "linuxserver/radarr": [
                    {"name": n}
                    for n in ("latest", "5.10.0.9100-ls236", "5.10.0.9100-ls235", "5.8.3.8933-ls230", "develop")
                ]
            },
        )
        inv = mini_inv(image="linuxserver/radarr:5.8.3.8933-ls230")
        rows, _ = CW.evaluate(inv, CW.Local(False), up, NOW, 3, {})
        assert rows[0]["newest_tag"] == "5.10.0.9100-ls236"


# ================================================================ upstream fetching, cache, error paths
class TestUpstream(Base):
    def fetch(
        self, net, max_age=0, offline=False, url="https://api.github.com/repos/Radarr/Radarr/releases?per_page=100"
    ):
        up = CW.Upstream(self.cache, max_age, offline)
        with mock.patch("urllib.request.urlopen", net):
            return up, up.get(url, "gh", CW.Upstream._t_releases)

    def test_trims(self):
        with open(os.path.join(FX, "up/affected/nvd/ui__unifi_network_application.json")) as f:
            nvd = CW.Upstream._t_nvd(json.load(f))
        assert (nvd[0]["id"], nvd[0]["severity"], nvd[0]["score"]) == ("CVE-2026-50001", "critical", 10.0)
        assert nvd[0]["matches"][0]["versionEndExcluding"] == "9.0.118"
        with open(os.path.join(FX, "up/affected/gh/Radarr__Radarr.advisories.json")) as f:
            a = CW.Upstream._t_advisories(json.load(f))[0]
        assert (a["id"], a["cve"], a["severity"], a["score"]) == ("GHSA-r4d1-2x3c-9v8b", "CVE-2026-41001", "high", 8.1)
        assert a["vulns"][0]["range"] == "< 5.10.0.9100"
        assert CW.Upstream._t_releases({"message": "Not Found"}) is None

    def test_cache_hit_and_etag_revalidation(self):
        net = FakeNet("base", mode="304")
        up, data = self.fetch(net)
        assert data[0]["tag"] == "v5.11.0.9300"
        assert not net.calls[0][1].has_header("If-none-match")
        up2, again = self.fetch(net, max_age=3600)  # fresh: no request at all
        assert (again, up2.net_calls, len(net.calls)) == (data, 0, 1)
        up3, again = self.fetch(net, max_age=0)  # stale: If-None-Match -> 304
        assert again == data
        assert net.calls[1][1].has_header("If-none-match")
        assert up3.errors == []

    def test_404_cached_as_none(self):
        net = FakeNet("base")
        url = "https://api.github.com/repos/nobody/nothing/releases?per_page=100"
        e404 = lambda req, **k: (_ for _ in ()).throw(urllib.error.HTTPError(req.full_url, 404, "nf", _hdrs({}), None))  # noqa: E731
        up, data = self.fetch(e404, url=url)
        assert data is None
        assert up.errors == []
        up, data = self.fetch(net, max_age=3600, url=url)
        assert len(net.calls) == 0

    def test_api_down_without_and_with_cache(self):
        up, data = self.fetch(FakeNet(mode="down"))
        assert data is None
        assert up.errors == ["api.github.com/repos/Radarr/Radarr/releases: URLError"]
        _, good = self.fetch(FakeNet("base"))
        up, data = self.fetch(FakeNet(mode="down"))
        assert data == good  # stale cache beats nothing

    def test_offline(self):
        up, data = self.fetch(FakeNet("base"), offline=True)
        assert data is None
        assert "no cached data for" in up.errors[0]

    def test_nvd_throttle(self):
        up = CW.Upstream(self.cache, 0, False)
        with mock.patch("urllib.request.urlopen", FakeNet("affected", "base")), mock.patch("time.sleep") as sl:
            a = up.nvd("ui:unifi_network_application", "9.0.114")
            up.nvd("ui:unifi_network_application", "9.0.118")
        assert a[0]["id"] == "CVE-2026-50001"
        assert sl.called and 0 < sl.call_args[0][0] <= 6.5


# ================================================================ main(): alerting, dedup, silence, errors
ARR_SRC = {
    "media": """#!/usr/bin/env python3
\"\"\"media: prose like GET /api/v3/wanted/missing in a docstring is not a call.\"\"\"
def queue(svc):
    if svc == "radarr":
        return api("radarr", "GET", "/api/v3/queue?page=1&pageSize=200&includeMovie=true")["records"]
    return api("sonarr", "GET", "/api/v3/queue?page=1&pageSize=200&includeSeries=true&includeEpisode=true")
def health(svc):
    return get(svc, "/api/v3/health")
def fix(d, svc, it, sid, ids, body):
    m = get("radarr", f"/api/v3/movie/{int(it['arr_id'])}")
    d.act("sonarr", "PUT", f"/api/v3/series/{sid}", body)
    d.act(svc, "DELETE", f"/api/v3/queue/{ids}?removeFromClient=true&blocklist=true&skipRedownload=false")
    return (get("sonarr", f"/api/v3/series?tvdbId={int(it['tvdb'])}") or [None])[0], m
""",
    "media-remux": """#!/usr/bin/env python3
def grab(c, body):
    rels = radarr(f'/api/v3/release?movieId={c["id"]}', timeout=1)
    M.api('radarr', 'POST', '/api/v3/release', body)
    return rels
""",
    "arr-unblock": """#!/usr/bin/env python3
def imp(svc, name, files):
    cache = {name: api(svc, "GET", f"/api/v3/parse?title={q(name)}")}
    return cache, api(svc, "POST", "/api/v3/command", {"name": "ManualImport", "files": files})
""",
    "arr-mismatch": """#!/usr/bin/env python3
def lib():
    return get("radarr", "GET", "/api/v3/movie") or []
""",
}


class TestArrApi(Base):
    """--arr-api: his arr scripts' /api/v3 calls vs the apps' openapi.json (develop + tags)."""

    @pytest.fixture(autouse=True)
    def _arr(self, _base):
        self.src = os.path.join(self.d, "src")
        os.makedirs(self.src)
        for name, text in ARR_SRC.items():
            with open(os.path.join(self.src, name), "w") as f:
                f.write(text)
        with mock.patch.dict(os.environ, {"CONTAINER_WATCH_ARR_SRC": self.src}):
            yield

    def test_extracts_calls_per_app(self):
        calls, unread = CW.arr_calls(self.src)
        assert unread == []
        got = {k: (sorted(v["params"]), v["where"]) for k, v in calls.items()}
        assert sorted(k for k in got if k[0] == "radarr") == [
            ("radarr", "DELETE", "/api/v3/queue/{}"),
            ("radarr", "GET", "/api/v3/health"),
            ("radarr", "GET", "/api/v3/movie"),
            ("radarr", "GET", "/api/v3/movie/{}"),
            ("radarr", "GET", "/api/v3/parse"),
            ("radarr", "GET", "/api/v3/queue"),
            ("radarr", "GET", "/api/v3/release"),
            ("radarr", "POST", "/api/v3/command"),
            ("radarr", "POST", "/api/v3/release"),
        ]
        assert sorted(k for k in got if k[0] == "sonarr") == [
            ("sonarr", "DELETE", "/api/v3/queue/{}"),
            ("sonarr", "GET", "/api/v3/health"),
            ("sonarr", "GET", "/api/v3/parse"),
            ("sonarr", "GET", "/api/v3/queue"),
            ("sonarr", "GET", "/api/v3/series"),
            ("sonarr", "POST", "/api/v3/command"),
            ("sonarr", "PUT", "/api/v3/series/{}"),
        ]
        assert got["radarr", "GET", "/api/v3/queue"] == (["includeMovie", "page", "pageSize"], ["media:5"])
        assert got["sonarr", "GET", "/api/v3/queue"][0] == ["includeEpisode", "includeSeries", "page", "pageSize"]
        assert got["sonarr", "DELETE", "/api/v3/queue/{}"][0] == ["blocklist", "removeFromClient", "skipRedownload"]
        assert got["radarr", "GET", "/api/v3/release"] == (["movieId"], ["media-remux:3"])  # script default
        assert "/api/v3/wanted/missing" not in {k[2] for k in got}  # docstring prose

    def test_develop_ok_and_tag_regressions(self):
        rc, out, err = self.run_main("--arr-api", "--tag", "sonarr=v5.0.0.3100", input_file=None)
        assert rc == 1, out + err
        assert "16 /api/v3 calls" in out
        assert "Sonarr @develop: all 7 calls present" in out
        assert "Radarr @develop: all 9 calls present" in out
        assert "Sonarr @v5.0.0.3100: 3 of 7 calls missing/changed" in out
        assert re.search("MISSING +GET +/api/v3/health +path gone +\\(media:8\\)", out)
        assert re.search("CHANGED +PUT +/api/v3/series/\\{\\} +PUT gone \\(spec has GET\\)", out)
        assert re.search("CHANGED +GET +/api/v3/queue +query parameter\\(s\\) gone: includeEpisode", out)
        assert not os.path.exists(self.sshlog)  # GitHub only: no server collection
        assert {urllib.parse.urlparse(u).netloc for u, _ in self.net.calls} == {"raw.githubusercontent.com"}
        rc, out, _ = self.run_main("--arr-api", input_file=None)
        assert (rc, len(self.net.calls)) == (0, 0)  # develop cached (12h)

    def test_tag_without_spec_and_json(self):
        rc, out, _ = self.run_main("--arr-api", "--tag", "radarr=v7.0.0.1", "--json", input_file=None)
        assert rc == 1
        res = json.loads(out)
        r = [r for r in res["results"] if r["ref"] == "v7.0.0.1"][0]
        assert (r["app"], r["status"], len(r["findings"])) == ("radarr", "nospec", 9)

    def test_fetch_error_and_unreadable_scripts(self):
        rc, out, _ = self.run_main("--arr-api", input_file=None, net=FakeNet(mode="down"))
        assert rc == 2
        assert "could not fetch the spec" in out
        for name in ARR_SRC:
            os.unlink(os.path.join(self.src, name))
        rc, out, _ = self.run_main("--arr-api", input_file=None)
        assert rc == 2
        assert "could not read: media, media-remux, arr-unblock, arr-mismatch" in out

    def test_bad_tag(self):
        bad = (["--arr-api", "--tag", "lidarr=v1"], ["--arr-api", "--tag", "sonarr=../x"], ["--tag", "sonarr=v5"])
        for args in bad:
            with pytest.raises(SystemExit) as cm:
                self.run_main(*args, input_file=None)
            assert cm.value.code == 2


class TestNotify(Base):
    def test_alerts_once(self):
        net = FakeNet("affected", "base")
        rc, out, err = self.run_main("--notify", "--max-age", "1d", net=net)
        assert (rc, err) == (1, "")
        msgs = self.sent()
        assert len(msgs) == 1, msgs
        m = msgs[0]
        assert len(m) <= CW.TG_MAX
        assert m.startswith("🛡️ Container security check (Testsrv)"), m
        assert "unifi-controller 9.0.114 → 9.0.118" in m  # critical first
        assert m.index("unifi-controller") < m.index("radarr")
        assert m.index("radarr") < m.index("paperless_redis")
        for s in ("CVE-2026-50001", "CVE-2026-41001", "radarr 5.8.3.8933 → 5.10.0.9100"):
            assert s in m
        for quiet in (
            "sonarr",
            "paperless-ngx",
            "HomeAssistant",
            "sabnzbd",
            "claude-bot",
            "claude-vault",
            "Emby",
            "tdarr",
        ):
            assert quiet not in m
        notes = self.notes()
        assert len(notes) == 1
        note = open(os.path.join(self.inbox, notes[0])).read()
        assert "don't resend" in note
        assert "CVE-2026-31000" in note  # full details incl. redis
        assert "pinned tag 7.2.4" in note
        assert self.error_notes() == []
        st = self.statej()
        keys = {k for k, v in st["items"].items() if v["alerted"]}
        assert keys == {
            "adv:radarr:GHSA-r4d1-2x3c-9v8b",
            "rel:radarr:5.10.0.9100",
            "adv:paperless_redis:GHSA-r3d1-2x3c-9v8b",
            "adv:unifi-controller:CVE-2026-50001",
        }
        assert "sent=True" in open(os.path.join(self.state, "runs.log")).read()
        # next week: same findings -> nothing sent, nothing new in the inbox, but still listed on stdout
        rc, out, err = self.run_main("--notify", "--max-age", "1d", net=FakeNet("affected", "base"))
        assert rc == 1
        assert len(self.sent()) == 1
        assert len(self.notes()) == 1
        assert "[already alerted]" in out
        assert "[new]" not in out
        # a later week brings one new advisory: only that container is texted
        rc, out, err = self.run_main("--notify", net=FakeNet("sonarr-new", "affected", "base"), cache=self.cache + "2")
        msgs = self.sent()
        assert len(msgs) == 2
        assert "sonarr 4.0.20.3014 → 4.0.21.3100" in msgs[1]
        assert "CRITICAL" in msgs[1]
        for old in ("radarr", "unifi", "paperless_redis"):
            assert old not in msgs[1]

    def test_digest_is_soft_kev_on_exposed_is_urgent(self):
        """A plain advisory digest goes `tg --soft` (TG_SOFT=1); only an exploited (KEV) CVE on an internet-facing
        container (or exposure unknown) is urgent."""
        log = os.path.join(self.d, "soft.log")
        with mock.patch.dict(os.environ, CW_TG_SOFT_LOG=log):
            rc, out, err = self.run_main("--notify", "--max-age", "1d", net=FakeNet("affected", "base"))
        assert len(self.sent()) == 1
        assert open(log).read() == "soft=1\n"
        assert "TG_SOFT" not in os.environ
        sites = os.path.join(self.d, "sites")
        os.makedirs(sites)
        with open(os.path.join(sites, "example.conf"), "w") as f:
            f.write(
                "server { server_name media.example.org; location / { proxy_pass http://192.0.2.10:7878; } }\n"
                "server { server_name emby.example.org; location / { proxy_pass http://192.0.2.10:8096; } }\n"
            )
        routes = CW.public_routes(sites)
        assert routes == ({7878, 8096}, {"media", "emby"})
        inv = dict(
            containers=[
                dict(name="radarr", ports={"7878/tcp": [{"HostPort": "7878"}]}),
                dict(name="EmbyServer", net="host", ports={}),
                dict(name="bazarr", ports={"6767/tcp": [{"HostPort": "6767"}]}),
            ]
        )
        kev = lambda n: dict(container=n, kev="2026-10-01")  # noqa: E731
        assert CW.urgent_items([kev("radarr"), kev("EmbyServer"), kev("bazarr")], inv, routes) == [
            kev("radarr"),
            kev("EmbyServer"),
        ]
        assert CW.urgent_items([dict(container="radarr")], inv, routes) == []  # not KEV: soft
        assert CW.urgent_items([kev("bazarr")], inv, ()) == [kev("bazarr")]  # exposure unknown
        assert CW.public_routes(os.path.join(self.d, "missing")) is None

    def test_silent_when_nothing_affected(self):
        rc, out, err = self.run_main("--notify", "--max-age", "1d", net=FakeNet("base"))
        assert (rc, out, err) == (0, "", "")
        assert self.sent() == []
        assert (os.listdir(self.inbox) if os.path.isdir(self.inbox) else []) == []
        st = self.statej()
        assert st["items"] == {}
        last = json.load(open(os.path.join(self.state, "last.json")))
        rows = {r["container"]: r for r in last["rows"]}
        assert last["actionable"] == []
        assert rows["radarr"]["latest"] == "5.10.0.9100"  # nightly pre-release ignored
        assert rows["HomeAssistant_inabox"]["behind"] == 2
        assert rows["paperless-ngx"]["newer_security"][0]["tag"] == "2.19.0"  # inside the 3-day grace
        assert rows["unifi-controller"]["newest_tag"] == "9.0.118-unraid"
        assert rows["paperless_redis"]["newest_tag"] == "7.4.1"
        assert rows["sonarr"]["assumed_fixed"] == ["GHSA-s0n2-2x3c-9v8b"]

    def test_grace_period_then_alert(self):
        later = NOW + dt.timedelta(days=3)  # paperless 2.19.0 + sabnzbd 4.3.3 now due
        rc, out, err = self.run_main("--notify", net=FakeNet("base"), now=later)
        assert rc == 1
        m = self.sent()[0]
        assert "paperless-ngx 2.18.4 → 2.19.0" in m
        assert "binhex-sabnzbd 4.3.2 → 4.3.3" in m

    def test_tg_failure_retries_next_run(self):
        os.environ["CW_TG_EXIT"] = "1"
        rc, out, err = self.run_main("--notify", net=FakeNet("affected", "base"))
        assert "tg send failed; will retry next run" in err
        assert self.notes() == []
        assert not any(v["alerted"] for v in self.statej()["items"].values())
        os.environ["CW_TG_EXIT"] = "0"
        self.run_main("--notify", net=FakeNet("affected", "base"))
        assert len(self.notes()) == 1
        assert len(self.sent()) == 2  # the failed try + the retry

    def test_ack_then_notify_sends_nothing(self):
        rc, out, err = self.run_main("--ack", net=FakeNet("affected", "base"))
        assert "marked 4 item(s) as already alerted" in err
        assert all("(ack)" in v["alerted"] for v in self.statej()["items"].values())
        self.run_main("--notify", net=FakeNet("affected", "base"))
        assert self.sent() == []

    def test_dry_run_changes_nothing(self):
        rc, out, err = self.run_main("--notify", "--dry-run", net=FakeNet("affected", "base"))
        assert rc == 1
        assert "[new]" in out
        assert self.sent() == []
        assert not os.path.exists(os.path.join(self.state, "state.json"))
        assert not os.path.exists(self.inbox)

    def test_without_notify_never_sends(self):
        rc, out, err = self.run_main(net=FakeNet("affected", "base"))
        assert rc == 1
        assert self.sent() == []
        assert "[new]" in out

    def test_report_and_json(self):
        rc, out, err = self.run_main("--report", net=FakeNet("affected", "base"))
        assert rc == 0
        for name in ("radarr", "sonarr", "unifi-controller", "tdarr"):
            assert name in out
        rc, out, err = self.run_main("--json", net=FakeNet("affected", "base"))
        assert rc == 0
        assert len(json.loads(out)["actionable"]) == 4
        assert self.sent() == []

    def test_message_budget(self):
        items = []
        for n in range(12):
            items.append(
                dict(
                    container=f"container-{n:02d}",
                    app="X",
                    installed="1.0.0",
                    latest="1.0.9",
                    pinned=False,
                    tag="latest",
                    newest_tag=None,
                    kind="advisory",
                    key=f"k{n}",
                    sev=3 + n % 2,
                    ids=[f"CVE-2026-{1000 + n}"],
                    why="A fairly long explanation of the vulnerability " * 3,
                    url=f"https://github.com/x/y/security/advisories/GHSA-{n:04d}-aaaa-bbbb",
                    fixed="1.0.9",
                )
            )
        msg = CW.compose_message(CW.group_items(items))
        assert len(msg) <= CW.TG_MAX
        assert "more:" in msg
        assert msg.endswith("tell me if you want an update plan.")
        assert "container-01" in msg.split("\n")[1]  # a CRITICAL one leads


class TestErrorPaths(Base):
    def test_api_down_cold_cache_is_silent(self):
        rc, out, err = self.run_main("--notify", net=FakeNet(mode="down"))
        assert (rc, out, err) == (0, "", "")
        assert self.sent() == []
        assert self.notes() == []
        notes = self.error_notes()
        assert len(notes) == 1  # inbox only, never Telegram
        assert "several upstream sources failed" in open(os.path.join(self.inbox, notes[0])).read()
        rows = {r["container"]: r for r in json.load(open(os.path.join(self.state, "last.json")))["rows"]}
        assert "upstream releases unavailable" in rows["radarr"]["notes"]
        assert "NVD unavailable" in rows["unifi-controller"]["notes"]

    def test_api_down_warm_cache_no_repeat(self):
        self.run_main("--notify", net=FakeNet("affected", "base"))
        assert len(self.sent()) == 1
        rc, out, err = self.run_main("--notify", "--max-age", "0", net=FakeNet(mode="down"))
        assert rc == 1
        assert len(self.sent()) == 1
        assert "[already alerted]" in out
        last = json.load(open(os.path.join(self.state, "last.json")))
        assert len(last["actionable"]) == 4  # judged from the cache
        assert any("URLError" in e for e in last["errors"])

    @pytest.mark.parametrize("mode", ["ratelimit", "429"])
    def test_github_rate_limit(self, mode):
        net = FakeNet("base", mode=mode)
        rc, out, err = self.run_main("--notify", net=net)
        assert len(net.host_calls("api.github.com")) == 1  # stops asking after the first
        assert (rc, out) == (0, "")
        assert self.sent() == []
        last = json.load(open(os.path.join(self.state, "last.json")))
        assert "GitHub rate limit reached: using cached data" in last["errors"]

    def test_rate_limit_with_cache_keeps_judging(self):
        self.run_main("--notify", net=FakeNet("affected", "base"))
        net = FakeNet("base", mode="ratelimit")
        rc, out, err = self.run_main("--notify", "--max-age", "0", net=net)
        assert rc == 1
        assert len(net.host_calls("api.github.com")) == 1
        assert len(self.sent()) == 1

    def test_nvd_down(self):
        rc, out, err = self.run_main("--notify", net=FakeNet("affected", "base", mode="nvd-down"))
        m = self.sent()[0]
        assert "unifi" not in m
        assert "radarr" in m
        last = json.load(open(os.path.join(self.state, "last.json")))
        assert any("HTTP 503" in e for e in last["errors"])


# ================================================================ collection over (fake) ssh
class TestCollect(Base):
    def test_root_mode_runs_collector_over_ssh(self):
        rc, out, err = self.run_main("--notify", "--max-age", "1d", input_file=None, net=FakeNet("base"))
        assert (rc, out, err) == (0, "", "")
        log = open(self.sshlog).read()
        assert "unraid-ssh mode" in log
        assert "ssh -o BatchMode=yes -o ConnectTimeout=10 unraid bash -s" in log
        assert "ssh -G unraid" in log  # Local's host-IP lookup
        assert open(self.env["CW_SSH_STDIN"]).read().strip() == CW.COLLECTOR.strip()
        # the collector output, plus the HA add-on section read locally (fake ha: none) for --input replays
        assert (
            open(os.path.join(self.state, "last-collect.txt")).read()
            == open(INV).read().rstrip("\n") + "\n@@ha-addons\n"
        )

    def test_lockdown_verbs_mode(self):
        os.environ["CW_LOCKDOWN_MODE"] = "verbs"
        rc, out, err = self.run_main(input_file=None, net=FakeNet("base"))
        assert rc == 0
        log = open(self.sshlog).read()
        assert "unraid-ssh --host unraid run containers" in log
        assert "bash -s" not in log

    def test_ssh_failure(self):
        os.environ["CW_SSH_FAIL"] = "1"
        rc, out, err = self.run_main("--notify", input_file=None, net=FakeNet("affected", "base"))
        assert (rc, out) == (2, "")
        assert "could not collect from unraid" in err
        assert "ssh exit 255" in err
        assert "collect-failed RuntimeError" in open(os.path.join(self.state, "runs.log")).read()
        assert self.sent() == []
        assert not os.path.exists(os.path.join(self.state, "state.json"))
        assert self.net.calls == []

    def test_missing_unraid_ssh_falls_back_to_root(self):
        with mock.patch.object(CW, "UNRAID_SSH", os.path.join(self.d, "does-not-exist")):
            assert CW.lockdown_mode() == "root"

    def test_empty_inventory(self):
        p = os.path.join(self.d, "empty.txt")
        open(p, "w").write("@@meta\n@@containers\n@@end\n")
        rc, out, err = self.run_main("--notify", input_file=p)
        assert rc == 2
        assert "no containers" in err
        assert self.sent() == []

    def test_concurrent_run_refused(self):
        from core import locks  # the script's lib dir (with core) is on sys.path once it is loaded

        with locks.hold(os.path.join(self.state, "lock"), wait=0) as fd:
            assert fd is not None
            rc, out, err = self.run_main("--notify", net=FakeNet("affected", "base"))
        assert rc == 2
        assert "another run is in progress" in err
        assert self.sent() == []

    def test_collector_is_read_only(self):
        rc, out, err = self.run_main("--show-collector")
        assert out.strip() == CW.COLLECTOR.strip()
        assert re.search("docker\\s+(pull|run|exec|rm|rmi|stop|start|restart|update|kill|create|cp)\\b", out) is None
        assert "claude-vault" in out  # vault env/history excluded
        assert not re.search("(?m)^\\s*(rm|mv|cp|tee|dd)\\s", out)


# ================================================================ the UniFi OS Server VM (Network app + OS Server)
VM_ON = dict(CW_UNIFI_NET="10.6.106-36011-1", CW_UNIFI_OS="5.1.42")
VM_NET, VM_OS = "UniFi Network (UniFi OS VM)", "UniFi OS Server (VM)"
VM_LINE = (
    "• UniFi Network (UniFi OS VM) 10.6.106 → 10.6.109: update available since 2026-10-13; nothing updates the VM"
    " by itself https://community.ui.com/releases/r/network/10.6.109"
)
VM_SSH = [
    "ssh -o BatchMode=yes -o ConnectTimeout=10 unifi-os cd /tmp && sudo -n -u uosserver podman exec uosserver"
    " dpkg-query -W unifi",
    "ssh -o BatchMode=yes -o ConnectTimeout=10 unifi-os uosserver version",
]
NOTES = "https://community.ui.com/releases/r/network/"


def vm_input(d, net="10.6.106-36011-1", os_ver="5.1.42"):
    """INV plus the sections a live run appends ('@@ha-addons', '@@unifi-vm'), for --input replays."""
    rows = [dict(item="network", installed=net), dict(item="uosserver", installed=os_ver)]
    p = os.path.join(d, "vm-collect.txt")
    with open(INV) as f, open(p, "w") as o:
        o.write(f.read().rstrip("\n") + "\n@@ha-addons\n@@unifi-vm\n" + "\n".join(json.dumps(r) for r in rows) + "\n")
    return p


def ui_row(v, date, name="UniFi Network Application {} for Debian/Ubuntu", enabled=True):
    return dict(name=name.format(v), version=v, published=date, notes=NOTES + v, enabled=enabled)


UI_ROWS = [
    ui_row("10.6.109", "2026-10-13"),
    ui_row("10.6.106", "2026-09-15"),
    ui_row("10.6.101", "2026-08-27"),
    ui_row("5.1.42", "2026-09-10", name="UniFi OS Server {} for Linux (x64)"),
]


class TestUnifiVm(Base):
    @pytest.fixture(autouse=True)
    def _vm(self, _base):
        with mock.patch.object(CW, "UNIFI_HOST", "unifi-os"):
            yield

    def vm_rows(self):
        with open(os.path.join(self.state, "last.json")) as f:
            j = json.load(f)
        return {r["container"]: r for r in j["rows"] if r["container"] in (VM_NET, VM_OS)}, j

    def vm_ssh(self):
        if not os.path.exists(self.sshlog):
            return []
        return [ln for ln in open(self.sshlog).read().splitlines() if " unifi-os " in ln]

    def test_commands_and_default_host(self):
        """Exactly these two reads run on the VM (any change must change this test); default ssh alias unifi-os."""
        assert [v["cmd"] for v in CW.UNIFI_VM] == [
            "cd /tmp && sudo -n -u uosserver podman exec uosserver dpkg-query -W unifi",
            "uosserver version",
        ]
        src = open(BIN).read()
        assert 'UNIFI_HOST = os.environ.get("CONTAINER_WATCH_UNIFI_HOST", "unifi-os")' in src
        assert CW.UI_DOWNLOADS == "https://download.svc.ui.com/v1/software-downloads"

    def test_live_run_reads_the_vm_and_alerts_once(self):
        with mock.patch.dict(os.environ, VM_ON):
            rc, out, err = self.run_main("--notify", input_file=None, net=FakeNet("unifi-vm", "base"))
        assert (rc, err) == (1, "")
        assert "[new]" in out
        assert self.vm_ssh() == VM_SSH
        coll = open(os.path.join(self.state, "last-collect.txt")).read()
        assert coll.endswith(
            '@@ha-addons\n@@unifi-vm\n{"item": "network", "installed": "10.6.106-36011-1"}\n'
            '{"item": "uosserver", "installed": "5.1.42"}\n'
        ), coll[-200:]
        assert len(self.sent()) == 1
        assert VM_LINE in self.sent()[0]
        assert "UniFi OS Server" not in self.sent()[0]  # current: no news
        rows, j = self.vm_rows()
        net, uos = rows[VM_NET], rows[VM_OS]
        assert (net["installed"], net["source"], net["latest"], net["latest_date"], net["behind"]) == (
            "10.6.106",
            "ssh",
            "10.6.109",
            "2026-10-13",
            1,
        )
        assert (net["affecting"], net["advisories_total"]) == ([], 2)  # NVD by CPE, none affecting
        assert (uos["installed"], uos["latest"], uos["behind"]) == ("5.1.42", "5.1.42", 0)
        assert self.net.host_calls("download.svc.ui.com") == [CW.UI_DOWNLOADS]  # one fetch for both
        assert j["errors"] == []
        assert "vm:network:10.6.109" in self.statej()["items"]
        assert len(self.notes()) == 1
        note = open(os.path.join(self.inbox, self.notes()[0])).read()
        assert "    - UPDATE update available since 2026-10-13; nothing updates the VM by itself → 10.6.109" in note
        # a week later, same versions: already alerted, nothing new sent
        with mock.patch.dict(os.environ, VM_ON):
            rc, out, err = self.run_main("--notify", input_file=None, net=FakeNet("unifi-vm", "base"))
        assert rc == 1
        assert "[already alerted]" in out
        assert (len(self.sent()), len(self.notes())) == (1, 1)

    def test_current_vm_is_quiet(self):
        """Disabled, early-access, beta, Windows and arm64 entries with higher versions never count."""
        with mock.patch.dict(os.environ, VM_ON):
            rc, out, err = self.run_main("--notify", input_file=None, net=FakeNet("base"))
        assert (rc, out, err, self.sent(), self.notes()) == (0, "", "", [], [])
        rows, _ = self.vm_rows()
        assert (rows[VM_NET]["latest"], rows[VM_NET]["behind"]) == ("10.6.106", 0)
        assert (rows[VM_OS]["latest"], rows[VM_OS]["behind"]) == ("5.1.42", 0)
        rc, out, err = self.run_main("--report", input_file=vm_input(self.d), net=FakeNet("base"))
        assert f"| {VM_NET} | UniFi Network | 10.6.106 (ssh) | 10.6.106 (2026-09-15) | 0 |" in out
        assert f"| {VM_OS} | UniFi OS Server | 5.1.42 (ssh) | 5.1.42 (2026-09-10) | 0 |" in out
        assert "runs on the UniFi OS Server VM: updated there, not by Unraid" in out

    def test_unreachable_vm_is_quiet(self):
        """unifi-health watches the VM itself: here an unreadable version is a report line, never an alert."""
        with mock.patch.dict(os.environ, dict(VM_ON, CW_UNIFI_FAIL="1")):
            rc, out, err = self.run_main("--notify", input_file=None, net=FakeNet("unifi-vm", "base"))
        assert (rc, out, err, self.sent(), self.notes()) == (0, "", "", [], [])
        assert self.vm_ssh() == VM_SSH[:1]  # tried once, not once per item
        rows, j = self.vm_rows()
        assert (rows[VM_NET]["installed"], rows[VM_OS]["installed"]) == (None, None)
        assert "installed version unknown" in rows[VM_NET]["notes"]
        assert f"{VM_NET}: unifi-os unreachable (ssh exit 255)" in j["errors"]
        assert self.net.host_calls("download.svc.ui.com") == []
        assert self.error_notes() == []

    def test_replay_needs_no_ssh(self):
        rc, out, err = self.run_main(input_file=vm_input(self.d), net=FakeNet("unifi-vm", "base"))
        assert rc == 1
        assert VM_LINE + "  [new]" in out
        assert "    - UPDATE update available since 2026-10-13" in out
        assert self.vm_ssh() == []

    def test_vm_off_without_host(self):
        with mock.patch.object(CW, "UNIFI_HOST", ""), mock.patch.dict(os.environ, VM_ON):
            assert CW.unifi_vm_section() == ""
            rc, out, err = self.run_main("--notify", input_file=None, net=FakeNet("unifi-vm", "base"))
        assert (rc, self.vm_ssh()) == (0, [])
        assert self.vm_rows()[0] == {}

    def test_download_list_trim_and_pick(self):
        up = CW.Upstream(self.cache, 0, False)
        with mock.patch("urllib.request.urlopen", FakeNet("base")):
            rows = up.ui_downloads()
        assert {tuple(sorted(r)) for r in rows} == {("enabled", "name", "notes", "published", "version")}
        assert not any("Protect" in r["name"] for r in rows)

        def pick(key):
            dl = next(v for v in CW.UNIFI_VM if v["key"] == key)["dl"]
            return [r["version"] for r in CW.ui_releases(rows, dl)]

        assert pick("network") == ["10.6.106", "10.6.101"]
        assert pick("uosserver") == ["5.1.42", "5.1.40"]

    def test_rules_unit(self):
        """Two releases behind; an NVD CVE in CISA KEV on the VM alerts at once with its own key and, the VM not
        being a container behind the proxy, as urgent; no '@@unifi-vm' section = no rows and no fetch."""
        cve = dict(
            id="CVE-2026-60001",
            published="2026-09-20T00:00:00",
            status="Analyzed",
            severity="high",
            score=8.8,
            desc="An attacker could exploit a command injection vulnerability in the Network app.",
            matches=[
                dict(criteria="cpe:2.3:a:ui:unifi_network_application:*:*:*:*:*:*:*:*", versionEndExcluding="10.6.106")
            ],
        )
        up = FakeUp(
            ui=UI_ROWS, nvd={"ui:unifi_network_application": [cve]}, kev={"CVE-2026-60001": {"added": "2026-10-01"}}
        )
        inv = CW.build_inventory(open(vm_input(self.d, net="10.6.101-1")).read())
        rows, items = CW.evaluate_unifi_vm(inv, CW.Local(False), up, NOW, 3, up.kev())
        assert up.ui_calls == 1
        by = {i["key"]: i for i in items}
        assert set(by) == {"vm:network:10.6.109", "kev:unifi-vm-network:CVE-2026-60001"}
        upd = by["vm:network:10.6.109"]
        assert (upd["kind"], upd["sev"], upd["fixed"], upd["count"]) == ("vm-update", 1, "10.6.109", 2)
        assert upd["why"] == "update available since 2026-10-13, 2 releases behind; nothing updates the VM by itself"
        k = by["kev:unifi-vm-network:CVE-2026-60001"]
        assert (k["kind"], k["sev"], k["fixed"], k["container"]) == ("advisory", 4, "10.6.106", VM_NET)
        assert rows[0]["affecting"][0]["fixed_date"] == "2026-09-15"
        assert CW.urgent_items(items, inv, routes=({443}, {"emby"})) == [k]
        assert (
            CW.container_line(VM_NET, items, compact=True)
            == f"• {VM_NET} 10.6.101 → 10.6.109: KEV: exploited in the wild; CRITICAL: command injection "
            "(CVE-2026-60001); update available since 2026-10-13, 2 releases behind https://nvd.nist.gov/vuln/detail/CVE-2026-60001"
        )
        # no section (an older --input file): no VM rows, no download fetch
        up2 = FakeUp(ui=UI_ROWS)
        rows2, items2 = CW.evaluate(mini_inv(), CW.Local(False), up2, NOW, 3, {})
        assert (up2.ui_calls, [r for r in rows2 if r["container"] in (VM_NET, VM_OS)]) == (0, [])


# ================================================================ bundled runtimes (Node/Bun/Deno/Python… in images)
RT_INV = os.path.join(FX, "inventory-runtimes.txt")
HA_UPDATES = os.path.join(FX, "ha-updates.json")
POST = "https://nodejs.org/en/blog/vulnerability/october-2026-security-releases"
NODE_REL = [
    dict(v="24.22.1", date="2026-10-08", security=True),
    dict(v="24.22.0", date="2026-10-01", security=False),
    dict(v="24.18.1", date="2026-07-28", security=True),
    dict(v="22.23.4", date="2026-10-08", security=True),
    dict(v="26.0.0-rc.1", date="2026-04-01", security=True),
]
NODE_POSTS = [dict(url=POST, date="2026-10-08T22:00:00+00:00")]
NODE_CVES = {
    POST: [
        dict(ids=["CVE-2026-90002"], title="DNS name truncation", severity="medium", majors=[24]),
        dict(
            ids=["CVE-2026-90001"],
            title="Brotli overflow permits remote code execution",
            severity="high",
            majors=[26, 24, 22],
        ),
        dict(ids=["CVE-2026-90003"], title="Permission model bypass", severity="low", majors=[22]),
    ]
}
EOL = {
    "nodejs": [
        dict(cycle="24", eol="2028-04-30", support="2026-10-20", latest="24.22.1"),
        dict(cycle="20", eol="2026-04-30", support="2024-10-22", latest="20.20.0"),
    ],
    "python": [
        dict(cycle="3.14", eol="2030-10-31", support="2027-10-01", latest="3.14.9", latestReleaseDate="2026-10-08"),
        dict(cycle="3.11", eol="2027-10-31", support="2024-04-01", latest="3.11.17", latestReleaseDate="2026-10-01"),
        dict(cycle="3.9", eol="2025-10-31", support="2022-05-17", latest="3.9.25"),
    ],
    "bun": [dict(cycle="1", eol=False, latest="1.4.4")],
}
BUN_RELS = [rel("bun-v1.4.4", "2026-10-08T10:00:00Z"), rel("bun-v1.4.2", "2026-09-05T10:00:00Z")]
BUN_ADV = [
    adv(
        "GHSA-b0n1-2x3c-9v8b",
        "high",
        ">= 1.0.21, < 1.4.4",
        "1.4.4",
        pub="2026-10-09T09:00:00Z",
        cve="CVE-2026-90001",
        summary="Bundled brotli decoder has an out-of-bounds write",
    )
]


def rt_eval(
    image="registry.example.org/acme/app:latest",
    env=None,
    name="app",
    now=NOW,
    kev=None,
    node=NODE_REL,
    configs=None,
    version=None,
    posts=NODE_POSTS,
):
    up = FakeUp(
        node=node,
        posts=posts,
        post=NODE_CVES,
        eol=EOL,
        kev=kev,
        configs=configs,
        releases={"oven-sh/bun": BUN_RELS},
        advisories={"oven-sh/bun": BUN_ADV},
    )
    rows, items = CW.evaluate(
        mini_inv(image=image, version=version, name=name, env=env), CW.Local(False), up, now, 3, {}
    )
    return rows[0], items, up


class TestRuntimes:
    def test_rt_version(self):
        assert CW.rt_version("node", "v24.22.0") == "24.22.0"
        assert CW.rt_version("java", "jdk-21.0.5+11") == "21.0.5"
        assert CW.rt_version("java", "jdk8u432-b06") == "8.0.432"
        assert CW.rt_version("python", "3.11.16-slim-bookworm", tag=True) == "3.11.16"
        assert CW.rt_version("deno", "alpine-2.1.4", tag=True) == "2.1.4"
        for floating in ("24-alpine3.22", "lts", "3.11-slim", "latest"):
            assert CW.rt_version("node", floating, tag=True) is None, floating

    def test_sources_in_order(self):
        cfg = dict(
            env={"NODE_VERSION": "24.19.0", "PYTHON_VERSION": "3.14.6"},
            args={"DENO_VERSION": "2.1.4"},
            present=["bun", "node"],
        )
        found, unknown = CW.runtimes_of(
            {"NODE_VERSION": "24.22.0", "YARN_VERSION": "1.22.22"}, cfg, "oven/bun:1.4.4-slim"
        )
        assert found == {
            "node": ("24.22.0", "env"),
            "python": ("3.14.6", "registry"),
            "deno": ("2.1.4", "history"),
            "bun": ("1.4.4", "tag"),
        }
        assert unknown == []
        found, unknown = CW.runtimes_of({}, dict(present=["bun"]), "registry.example.org/acme/app")
        assert (found, unknown) == ({}, ["bun"])

    def test_config_facts_keep_only_runtime_facts(self):
        j = dict(
            created="2026-09-30T00:00:00Z",
            config=dict(
                Env=["PATH=/usr/bin", "DATABASE_URL=postgres://u:secret@db.example.org/x", "NODE_VERSION=24.19.0"]
            ),
            history=[
                dict(created_by="ENV NODE_VERSION=24.19.0"),
                dict(created_by="RUN |2 DENO_VERSION=2.0.0 TOKEN=abc /bin/sh -c echo"),
                dict(created_by="RUN |1 DENO_VERSION=2.1.4 /bin/sh -c curl https://example.org/d.zip"),
                dict(created_by="COPY /usr/local/bin/bun /usr/local/bin/bun # buildkit"),
            ],
        )
        f = CW.config_facts(j)
        assert f == dict(
            created="2026-09-30T00:00:00Z",
            env={"NODE_VERSION": "24.19.0"},
            args={"NODE_VERSION": "24.19.0", "DENO_VERSION": "2.1.4"},
            present=["bun"],
        )
        assert "secret" not in json.dumps(f)
        assert "TOKEN" not in json.dumps(f)
        assert CW.config_facts(None) is None

    def test_node_post_parser(self):
        page = open(os.path.join(FX, "up/runtimes/node/october-2026-security-releases.html")).read()
        cves = CW.node_post_cves(page)
        assert [(c["ids"], c["severity"], c["majors"]) for c in cves] == [
            (["CVE-2026-90002"], "high", [24]),
            (["CVE-2026-90001"], "high", [22, 24, 26]),
            (["CVE-2026-90003"], "low", [22]),
        ]
        assert cves[1]["title"] == "Brotli decoder out-of-bounds write permits remote code execution"
        assert CW.node_post_cves("<h2>Summary</h2><p>nothing</p>") is None

    def test_node_misses_security_release(self):
        row, items, _ = rt_eval(env={"NODE_VERSION": "24.22.0"})
        assert len(items) == 1
        i = items[0]
        assert (i["key"], i["kind"], i["sev"], i["fixed"]) == ("rt:app:node:24.22.1", "runtime", 3, "24.22.1")
        assert i["ids"] == ["CVE-2026-90001", "CVE-2026-90002"]  # worst (RCE) first; 22.x-only one dropped
        assert i["url"] == POST
        assert "misses security release 24.22.1" in row["runtimes"][0]["status"]
        line = CW.container_line("app", items)
        assert "bundled Node 24.22.0 → 24.22.1: 2 CVEs (worst HIGH): Brotli overflow permits remote code" in line

    def test_node_current_prerelease_and_older_line(self):
        row, items, _ = rt_eval(env={"NODE_VERSION": "24.22.1"})
        assert (items, row["runtimes"][0]["status"]) == ([], "current")
        row, items, _ = rt_eval(env={"NODE_VERSION": "26.0.0"})  # 26.0.0-rc.1 is no fix for 26.0.0
        assert items == []
        row, items, _ = rt_eval(env={"NODE_VERSION": "24.17.0"})  # two missed waves: worst + newest fix
        assert (items[0]["fixed"], items[0]["key"]) == ("24.22.1", "rt:app:node:24.22.1")

    def test_node_grace_unknown_cves_and_no_index(self):
        fresh = [dict(v="24.22.1", date="2026-10-13", security=True)]  # 1 day before NOW: inside the grace
        row, items, _ = rt_eval(env={"NODE_VERSION": "24.22.0"}, node=fresh)
        assert items == []
        assert "misses security release" in row["runtimes"][0]["status"]
        row, items, _ = rt_eval(env={"NODE_VERSION": "24.22.0"}, now=NOW + dt.timedelta(days=3), node=fresh)
        assert len(items) == 1
        assert (items[0]["sev"], items[0]["ids"]) == (2, [])  # no post that week: MEDIUM, no CVEs
        assert items[0]["url"] == "https://nodejs.org/en/blog/release/v24.22.1"
        row, items, _ = rt_eval(env={"NODE_VERSION": "24.22.0"}, node=None)
        assert (items, row["runtimes"][0]["status"]) == ([], "Node release index unavailable")

    def test_kev_listed_runtime_cve_alerts_at_once(self):
        fresh = [dict(v="24.22.1", date="2026-10-14", security=True)]  # out today: inside the grace
        row, items, _ = rt_eval(
            env={"NODE_VERSION": "24.22.0"},
            node=fresh,
            posts=[dict(url=POST, date="2026-10-14T06:00:00+00:00")],
            kev={"CVE-2026-90002": dict(added="2026-10-14")},
        )
        assert len(items) == 1
        assert (items[0]["key"], items[0]["sev"], items[0]["kev"]) == ("kev:app:CVE-2026-90002", 4, "2026-10-14")
        assert "KEV: exploited in the wild" in CW.container_line("app", items)
        assert CW.urgent_items(items, dict(containers=[]), ()) == items  # exposure unknown: urgent

    def test_bun_advisories_like_apps(self):
        row, items, _ = rt_eval(image="oven/bun:1.4.2-slim")
        assert [(i["key"], i["sev"], i["fixed"]) for i in items] == [("rt:app:bun:GHSA-b0n1-2x3c-9v8b", 3, "1.4.4")]
        assert row["runtimes"][0]["source"] == "tag"
        row, items, _ = rt_eval(image="oven/bun:1.4.4-slim")
        assert items == []
        assert row["runtimes"][0]["status"] == "current"

    def test_eol_and_security_only_branches(self):
        row, items, _ = rt_eval(env={"PYTHON_VERSION": "3.9.18"})
        assert [(i["key"], i["kind"], i["sev"]) for i in items] == [("rteol:app:python3.9", "runtime-eol", 2)]
        assert "Python 3.9 is end-of-life since 2025-10-31" in CW.container_line("app", items)
        row, items, _ = rt_eval(env={"PYTHON_VERSION": "3.11.16"})  # security-only branch: report only
        assert items == []
        assert "3.11.17 (2026-10-01) is a security-only release of 3.11" in row["runtimes"][0]["status"]
        row, items, _ = rt_eval(env={"PYTHON_VERSION": "3.14.6"})
        assert items == []
        assert row["runtimes"][0]["status"] == "behind 3.14.9 (bugfix branch, not judged)"
        row, items, _ = rt_eval(env={"NODE_VERSION": "20.19.0"})  # EOL line: no more security releases
        assert [i["key"] for i in items] == ["rteol:app:node20"]

    def test_app_and_runtime_in_one_line(self):
        up = FakeUp(
            releases={"Radarr/Radarr": RADARR_RELS},
            advisories={
                "Radarr/Radarr": [
                    adv(
                        "GHSA-aaaa-bbbb-cccc",
                        "high",
                        "< 5.9.1.9070",
                        "5.9.1.9070",
                        cve="CVE-2026-41001",
                        summary="Auth bypass",
                    )
                ]
            },
            node=NODE_REL,
            posts=NODE_POSTS,
            post=NODE_CVES,
            eol=EOL,
        )
        rows, items = CW.evaluate(mini_inv(env={"NODE_VERSION": "24.22.0"}), CW.Local(False), up, NOW, 3, {})
        line = CW.container_line("radarr", items)
        assert line.startswith("• radarr 5.8.3.8933 → 5.9.1.9070 (latest 5.10.0.9100): HIGH: Auth bypass"), line
        assert "; bundled Node 24.22.0 → 24.22.1" in line
        assert "→ 24.22.1 (" not in line.split(":")[0]  # runtime fix never the app's target

    def test_self_vault_and_registry_calls(self):
        for name in ("claude-bot", "claude-vault"):
            up = FakeUp(node=NODE_REL, eol=EOL)
            rows, items = CW.evaluate(
                mini_inv(name=name, image="registry.example.org/acme/x", version=None, env={"NODE_VERSION": "24.22.0"}),
                CW.Local(False),
                up,
                NOW,
                3,
                {},
            )
            assert (items, up.cfg_calls) == ([], []), name  # never judged, never read
        row, items, up = rt_eval(env={"NODE_VERSION": "24.22.0"}, name="claude-hub-x")
        assert items and all(i["own"] for i in items)  # own stack: judged, marked own
        assert up.cfg_calls == [("registry.example.org/acme/app:latest", "sha256:x", False)]


class TestRuntimeRuns(Base):
    def rt_input(self):
        """The runtime inventory plus the add-on section the fake ha yields (as last-collect.txt would hold it)."""
        p = os.path.join(self.d, "rt-input.txt")
        with mock.patch.dict(os.environ, CW_HA_JSON=HA_UPDATES):
            sec = CW.ha_addons_section()
        with open(p, "w") as f:
            f.write(open(RT_INV).read() + sec + "\n")
        return p

    def test_ha_addons_section(self):
        with mock.patch.dict(os.environ, CW_HA_JSON=HA_UPDATES):
            sec = CW.ha_addons_section().splitlines()
        assert sec[0] == "@@ha-addons"
        assert [json.loads(ln)["title"] for ln in sec[1:]] == ["Matter Server", "Mystery Add-on"]
        with mock.patch.dict(os.environ, CW_HA_FAIL="1"):
            sec = CW.ha_addons_section()
        inv = CW.build_inventory(open(RT_INV).read() + sec)
        assert inv["addons"] == []
        assert "HA add-ons unreadable" in inv["addons_error"]

    def test_notify_runtimes_once_own_to_inbox(self):
        inp = self.rt_input()
        rc, out, err = self.run_main("--notify", input_file=inp, net=FakeNet("runtimes"))
        assert (rc, err) == (1, "")
        msgs = self.sent()
        assert len(msgs) == 1
        m = msgs[0]
        assert len(m) <= CW.TG_MAX
        for s in (
            "Matter Server (HA add-on) 9.2.0: bundled Node 24.19.0 → 24.22.1",
            "bunapp",
            "nodeapp",
            "histapp",
            "oldpy",
        ):
            assert s in m
        for quiet in ("pyapp", "regapp", "claude-hub-x", "claude-bot", "claude-vault", "Mystery"):
            assert quiet not in m
        notes = self.notes()
        assert len(notes) == 2
        own = [n for n in notes if n.startswith("container-watch-own-")]
        assert len(own) == 1
        txt = open(os.path.join(self.inbox, own[0])).read()
        assert "claude-hub-x: bundled Node 24.22.0 → 24.22.1" in txt
        assert "Not sent to the owner" in txt
        st = self.statej()["items"]
        assert {k for k, v in st.items() if v["alerted"]} == {
            "rt:nodeapp:node:24.22.1",
            "rt:Matter Server (HA add-on):node:24.22.1",
            "rt:bunapp:bun:GHSA-b0n1-2x3c-9v8b",
            "rteol:histapp:deno2.1",
            "rteol:oldpy:python3.9",
            "rt:claude-hub-x:node:24.22.1",
        }
        assert "(own)" in st["rt:claude-hub-x:node:24.22.1"]["alerted"]
        rc, out, err = self.run_main("--notify", input_file=inp, net=FakeNet("runtimes"))
        assert (rc, len(self.sent()), len(self.notes())) == (1, 1, 2)  # next week: nothing new
        assert "[new]" not in out

    def test_report_and_json(self):
        inp = self.rt_input()
        rc, out, err = self.run_main("--report", input_file=inp, net=FakeNet("runtimes"))
        sec = out[out.index("## Bundled runtimes") :]
        for s in (
            "- regapp: Node 22.23.4 (registry): current",
            "- histapp: Deno 2.1.4 (history): Deno 2.1 is end-of-life since 2025-04-30",
            "- histapp: Bun ? (history): installed without a version variable",
            "- pyapp: Python 3.11.16 (env): 3.11.17 (2026-10-01) is a security-only release of 3.11",
            "- bunapp: Bun 1.4.2 (tag): 1 advisory affects it, fixed in 1.4.4 [CVE-2026-90001]",
            "Not checked (no image to read): Mystery Add-on (HA add-on)",
        ):
            assert s in sec
        assert "claude-vault" not in sec
        assert "| Matter Server (HA add-on) | HA add-on | 9.2.0 (ha) |" in out
        rc, out, err = self.run_main("--json", input_file=inp, net=FakeNet("runtimes"))
        rows = {r["container"]: r for r in json.loads(out)["rows"]}
        assert rows["nodeapp"]["runtimes"][0]["cves"] == ["CVE-2026-90001", "CVE-2026-90002"]
        assert rows["Matter Server (HA add-on)"]["image"] == "homeassistant/amd64-addon-matter-server:9.2.0"

    def test_registry_token_digest_cache_and_no_tag_reads(self):
        inp = self.rt_input()
        self.run_main("--json", input_file=inp, net=FakeNet("runtimes"))
        calls = [u for u, _ in self.net.calls]
        assert "https://registry.example.org/token?service=registry.example.org&scope=repository%3Ax%3Apull" in calls
        authed = [u for u, r in self.net.calls if r.get_header("Authorization") == "Bearer test-token"]
        assert any("/acme/histapp/blobs/sha256:" in u for u in authed)
        assert not [u for u in calls if "registry.example.org/v2/" in u and "/manifests/" in u]  # IDs only
        assert [u for u in calls if u.endswith("/homeassistant/amd64-addon-matter-server/manifests/9.2.0")]
        self.run_main("--json", "--max-age", "0", input_file=inp, net=FakeNet("runtimes"))
        again = [u for u, _ in self.net.calls]
        assert not [u for u in again if "/acme/histapp/" in u or "/acme/regapp/" in u]  # digests: cached
        assert [u for u in again if "/acme/nodeapp/" in u]  # a miss is retried after --max-age

    def test_registry_rate_limit_and_offline(self):
        inp = self.rt_input()
        net = FakeNet("runtimes", mode="reg429")
        rc, out, err = self.run_main("--json", input_file=inp, net=net)
        j = json.loads(out)
        assert len(net.host_calls("registry.example.org")) == 1  # stops asking after the first 429
        assert any("registry rate limit" in e for e in j["errors"])
        keys = {i["key"] for i in j["actionable"]}
        assert "rt:nodeapp:node:24.22.1" in keys  # env-based findings still judged
        assert "rteol:histapp:deno2.1" not in keys  # needs the registry config
        self.run_main("--json", input_file=inp, net=FakeNet("runtimes"), cache=self.cache + "-ok")
        rc, out, err = self.run_main(
            "--json", "--offline", input_file=inp, net=FakeNet(mode="down"), cache=self.cache + "-ok"
        )
        assert self.net.calls == []
        assert "rteol:histapp:deno2.1" in {i["key"] for i in json.loads(out)["actionable"]}


# ================================================================ the cron entrypoint (subprocess, sockets blocked)
def registry_job():
    """The job's [jobs.container-watch] entry in the schedule registry (core/schedules.toml): the hub schedule
    (runs_on = "hub") or the crontab line (runs_on = "cron") runs its argv."""
    import tomllib

    with open(SCHEDULES, "rb") as f:
        job = tomllib.load(f).get("jobs", {}).get("container-watch")
    if not job:
        raise AssertionError(f"no [jobs.container-watch] in {SCHEDULES}")
    return job


def cron_args():
    av = list(registry_job().get("argv") or [])
    while av[:1] == ["timeout"]:
        av = av[2:]
    if not av or not av[0].endswith("bin/container-watch"):
        raise AssertionError(f"container-watch argv does not run ~/bin/container-watch: {av}")
    return av[1:]


class TestCron(Base):
    @pytest.fixture(autouse=True)
    def _cron(self, _base):
        self.home = os.path.join(self.d, "home")
        self.netlog = os.path.join(self.d, "net.log")
        os.makedirs(self.home)

    def warm_cache(self, *layers):
        """The cron run must not need the network: fill $HOME/.cache/container-watch from the fixtures."""
        self.run_main(
            "--dry-run",
            "--max-age",
            "0",
            net=FakeNet(*layers),
            cache=os.path.join(self.home, ".cache/container-watch"),
            state=os.path.join(self.d, "warm"),
        )

    def cron(self):
        env = dict(os.environ, HOME=self.home, CW_BIN=BIN, CW_NET_LOG=self.netlog, CW_TEST_NOW=NOW.isoformat())
        r = subprocess.run(
            [sys.executable, "-I", BOOT] + cron_args(), env=env, capture_output=True, text=True, timeout=120
        )
        assert not os.path.exists(self.netlog), "cron run tried the network: " + (
            open(self.netlog).read() if os.path.exists(self.netlog) else ""
        )
        return r

    def home_inbox(self):
        p = os.path.join(self.home, "inbox")
        return sorted(os.listdir(p)) if os.path.isdir(p) else []

    def test_cron_line(self):
        a = cron_args()
        assert "--notify" in a
        assert "--dry-run" not in a
        assert "--input" not in a
        job = registry_job()
        assert job.get("cron") == "13 19 * * 3"  # Wed 19:13 Zurich
        assert (job.get("out"), job.get("err")) == ("container-watch/cron.log", "out")  # like >> log 2>&1
        assert job.get("runs_on") in ("hub", "cron")

    def test_cron_quiet_when_nothing_affected(self):
        self.warm_cache("base")
        r = self.cron()
        assert (r.returncode, r.stdout, r.stderr) == (0, "", "")
        assert self.sent() == []
        assert self.home_inbox() == []
        runs = open(os.path.join(self.home, ".local/state/container-watch/runs.log")).read()
        assert "actionable=0 new=0 sent=None net=0" in runs

    def test_cron_alerts_once(self):
        self.warm_cache("affected", "base")
        r = self.cron()
        assert r.returncode == 1, r.stderr
        assert r.stderr == ""
        assert "[new]" in r.stdout  # goes to cron.log only
        assert len(self.sent()) == 1
        assert len(self.home_inbox()) == 1
        r = self.cron()
        assert r.returncode == 1, r.stderr
        assert len(self.sent()) == 1
        assert len(self.home_inbox()) == 1

    def test_cron_runtimes_and_addons_alert_once(self):
        """The scheduled run reads the HA add-ons (fake ha), judges bundled runtimes from the warm cache only."""
        with mock.patch.dict(os.environ, CW_INVENTORY=RT_INV, CW_HA_JSON=HA_UPDATES):
            inp = TestRuntimeRuns.rt_input(self)
            self.run_main(
                "--dry-run",
                "--max-age",
                "0",
                input_file=inp,
                net=FakeNet("runtimes"),
                cache=os.path.join(self.home, ".cache/container-watch"),
                state=os.path.join(self.d, "warm"),
            )
            r = self.cron()
            assert (r.returncode, r.stderr) == (1, "")
            assert len(self.sent()) == 1
            assert "Matter Server (HA add-on) 9.2.0: bundled Node 24.19.0 → 24.22.1" in self.sent()[0]
            assert len(self.home_inbox()) == 2  # alert + own-stack note
            r = self.cron()
            assert (r.returncode, len(self.sent()), len(self.home_inbox())) == (1, 1, 2)

    def test_cron_unifi_vm_alerts_once(self):
        """The scheduled run reads the UniFi OS VM over (fake) ssh and judges it from the warm cache only."""
        self.run_main(
            "--dry-run",
            "--max-age",
            "0",
            input_file=vm_input(self.d),
            net=FakeNet("unifi-vm", "base"),
            cache=os.path.join(self.home, ".cache/container-watch"),
            state=os.path.join(self.d, "warm"),
        )
        with mock.patch.dict(os.environ, dict(VM_ON, CONTAINER_WATCH_UNIFI_HOST="unifi-os")):
            r = self.cron()
            assert (r.returncode, r.stderr) == (1, "")
            assert len(self.sent()) == 1
            assert VM_LINE in self.sent()[0]
            assert len(self.home_inbox()) == 1
            r = self.cron()
            assert (r.returncode, len(self.sent()), len(self.home_inbox())) == (1, 1, 1)
        assert [ln for ln in open(self.sshlog).read().splitlines() if " unifi-os " in ln] == VM_SSH * 2

    def test_cron_ssh_down_is_quiet(self):
        self.warm_cache("affected", "base")
        with mock.patch.dict(os.environ, {"CW_SSH_FAIL": "1"}):
            r = self.cron()
        assert r.returncode == 2
        assert r.stdout == ""
        assert "could not collect" in r.stderr  # one line in cron.log
        assert self.sent() == []
        assert self.home_inbox() == []
