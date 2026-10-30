"""Offline tests for ~/bin/security-watch (problems-only security watch; cron daily).
Never touches the network, the real HOME, state, inbox, crontab or nmap: everything runs in a temp HOME with fakes.
SW_BIN=<copy> tests a copy. Synthetic values only (example.org, documentation IPs)."""

import contextlib
import importlib.machinery
import importlib.util
import json
import os
import shutil
import socket
import ssl
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
BIN = os.environ.get("SW_BIN") or os.path.expanduser("~/bin/security-watch")  # before HOME moves to the sandbox
for _pf in ("profile", "profile-tests"):  # synthetic profile: repo layout tests/profile, live ~/opt/profile-tests
    _p = HERE.parent / _pf / "fixtures" / "profile.json"
    if _p.is_file():
        os.environ["ASSISTANT_PROFILE"] = str(_p)
        break
SANDBOX = tempfile.mkdtemp(prefix="swtest-")
os.environ["HOME"] = os.path.join(SANDBOX, "home")
os.environ["USAGE"] = "off"
os.makedirs(os.environ["HOME"])
FAKEBIN = os.path.join(SANDBOX, "bin")
os.makedirs(FAKEBIN)
NMAP_OUT = os.path.join(SANDBOX, "nmap.xml")
NMAP_LOG = os.path.join(SANDBOX, "nmap.log")
CRON_OUT = os.path.join(SANDBOX, "crontab.txt")
for name, body in (
    ("nmap", f'#!/bin/sh\necho "$@" >> {NMAP_LOG}\ncat {NMAP_OUT}\n'),
    ("crontab", f'#!/bin/sh\n[ "$1" = "-l" ] || exit 9\ncat {CRON_OUT}\n'),
):
    with open(os.path.join(FAKEBIN, name), "w") as f:
        f.write(body)
    os.chmod(os.path.join(FAKEBIN, name), 0o755)
os.environ["SECURITY_WATCH_NMAP"] = os.path.join(FAKEBIN, "nmap")
os.environ["SECURITY_WATCH_CRONTAB"] = os.path.join(FAKEBIN, "crontab")
# the hub's schedules: a fake `tool` (prints TOOL_OUT, or exits with TOOL_RC) + a registry fixture
TOOL_OUT, TOOL_RC, TOOL_LOG = (os.path.join(SANDBOX, n) for n in ("tool.json", "tool.rc", "tool.log"))
with open(os.path.join(FAKEBIN, "tool"), "w") as f:
    f.write(f'#!/bin/sh\necho "$@" >> {TOOL_LOG}\n[ -f {TOOL_RC} ] && exit "$(cat {TOOL_RC})"\ncat {TOOL_OUT}\n')
os.chmod(os.path.join(FAKEBIN, "tool"), 0o755)
os.environ["SECURITY_WATCH_TOOL"] = os.path.join(FAKEBIN, "tool")
REGISTRY = os.path.join(SANDBOX, "schedules.toml")
with open(REGISTRY, "w") as f:
    f.write('schema = 1\ntz = "Europe/Zurich"\n')
    for name, cls, queue, runs_on, cron, cmd in (
        ("brain-watch", "K", "", "cron", "* * * * *", "brain-watch --cron"),
        ("agent-watch", "X", "critical", "hub", "*/5 * * * *", "agent-watch run"),
        ("brain-auto", "M", "fast", "hub", "11 * * * *", "brain auto"),
        ("security-watch", "M", "fast", "cron", "23 6 * * *", "security-watch run --cron"),
    ):
        f.write(
            f'\n[jobs.{name}]\nclass = "{cls}"\nqueue = "{queue}"\nruns_on = "{runs_on}"\ncron = "{cron}"\n'
            f'argv = ["~/bin/{cmd.split()[0]}"]\ncron_line = "{cron} {cmd}"\n'
        )
os.environ["SECURITY_WATCH_SCHEDULES"] = REGISTRY


def hub_rows(**modes):
    """The fake hub's workflow.schedules answer: {job: (mode, status)}."""
    rows = [dict(name=f"job:{j}", job=j, mode=m, cron="* * * * *", status=s) for j, (m, s) in modes.items()]
    Path(TOOL_OUT).write_text(json.dumps(dict(ok=True, tool="workflow.schedules", data=dict(schedules=rows))))


PROC = os.path.join(SANDBOX, "proc")
os.makedirs(PROC)
os.environ["SECURITY_WATCH_PROC_NET"] = PROC

_ld = importlib.machinery.SourceFileLoader("sw", BIN)
SW = importlib.util.module_from_spec(importlib.util.spec_from_loader("sw", _ld))
_ld.exec_module(SW)
assert str(SW.HOME).startswith(SANDBOX), "refusing to run: HOME is not the sandbox"
DOMAIN = "example.org"
IP = "203.0.113.7"
HOME = Path(os.environ["HOME"])

TCP_HEAD = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"


def nmap_xml(ports):
    rows = "".join(
        f'<port protocol="tcp" portid="{p}"><state state="open"/><service name="svc{p}"/></port>' for p in ports
    )
    return f'<?xml version="1.0"?><nmaprun><host><ports>{rows}</ports></host></nmaprun>'


class Net:
    """Fake http/get_json routing by host; records every host contacted."""

    def __init__(self):
        self.hosts, self.idb, self.crt, self.cs, self.http_codes = [], {"ports": [80, 443]}, [], [], {}
        self.crt_down = self.cs_down = False

    def http(self, url, timeout=20, accept=None, method="GET", follow=True):
        host = url.split("/")[2]
        self.hosts.append(host)
        if host == "internetdb.shodan.io":
            return 200, {}, json.dumps(dict(ip=IP, hostnames=[], **self.idb)).encode()
        if host == "crt.sh":
            return (502, {}, b"") if self.crt_down else (200, {}, json.dumps(self.crt).encode())
        if host == "api.certspotter.com":
            return (503, {}, b"") if self.cs_down else (200, {}, json.dumps(self.cs).encode())
        if host.endswith(DOMAIN):
            return self.http_codes.get(url, 401), {}, b""
        raise AssertionError("unexpected host " + host)


class Base(unittest.TestCase):
    def setUp(self):
        for d in (".local", ".config", "inbox", ".secrets", ".claude", ".cache", "opt"):
            shutil.rmtree(HOME / d, ignore_errors=True)
        (HOME / "inbox").mkdir()
        sec = HOME / ".secrets"
        sec.mkdir(mode=0o700)
        os.chmod(sec, 0o700)
        (sec / "token").write_text("x")
        os.chmod(sec / "token", 0o600)
        hooks = HOME / ".claude/hooks"
        hooks.mkdir(parents=True)
        for h in ("risk-check.sh", "vault-guard.sh"):
            (hooks / h).write_text("#!/bin/sh\n")
            os.chmod(hooks / h, 0o755)
        self.settings(
            dict(
                hooks=dict(
                    PreToolUse=[
                        dict(
                            matcher="Bash|Read",
                            hooks=[dict(type="command", command=str(hooks / "vault-guard.sh"), onFailure="block")],
                        ),
                        dict(
                            matcher="Bash",
                            hooks=[dict(type="command", command=str(hooks / "risk-check.sh"), onFailure="block")],
                        ),
                    ]
                )
            )
        )
        jobs = "* * * * * brain-watch --cron\n*/5 * * * * agent-watch\n11 * * * * brain auto\n23 6 * * * security-watch run --cron\n"  # noqa: E501
        (HOME / "crontab").write_text(jobs)
        Path(CRON_OUT).write_text(jobs)
        for p in (TOOL_RC, TOOL_LOG):
            if os.path.exists(p):
                os.remove(p)
        hub_rows(
            **{
                "agent-watch": ("live", "ACTIVE"),
                "brain-auto": ("live", "ACTIVE"),
                "security-watch": ("live", "ACTIVE"),
            }
        )
        for fn in ("tcp", "tcp6", "udp", "udp6"):
            Path(PROC, fn).write_text(TCP_HEAD)
        self.listen(["0100007F:1FF2", "00000000:17C0"])
        Path(NMAP_OUT).write_text(nmap_xml([80, 443]))
        if os.path.exists(NMAP_LOG):
            os.remove(NMAP_LOG)
        nginx = HOME / "opt/nginx-mirror/etc-nginx/sites-enabled"
        nginx.mkdir(parents=True)
        (nginx / "site.conf").write_text(
            f"server {{\n  server_name {DOMAIN} www.{DOMAIN} app.{DOMAIN} other.test *.{DOMAIN};\n}}\n"
            f"server {{\n    server_name _;\n}}\n"
        )
        self.net = Net()
        self.outside = {}
        self.outside_http_codes = []
        self.tls = {}
        self.egress, self.a = IP, [IP]
        p = [
            mock.patch.object(SW, "http", self.net.http),
            mock.patch.object(SW, "egress_ip", lambda: self.egress),
            mock.patch.object(SW, "resolve", lambda n, fam: list(self.a) if fam == socket.AF_INET else []),
            mock.patch.object(SW, "outside_tcp", lambda ip, ports: {p: self.outside.get(p, "closed") for p in ports}),
            mock.patch.object(SW, "outside_http", lambda url: list(self.outside_http_codes)),
            mock.patch.object(
                SW,
                "tls_probe",
                lambda ip, n: dict(self.tls.get(n) or dict(days=50.0, fp="f1", issuer="LE", error=None)),
            ),
            mock.patch.object(SW.time, "sleep", lambda s: None),
        ]
        for x in p:
            x.start()
            self.addCleanup(x.stop)

    def settings(self, d):
        (HOME / ".claude/settings.json").write_text(json.dumps(d))

    def listen(self, addrs):
        Path(PROC, "tcp").write_text(
            TCP_HEAD
            + "".join(
                f"   {i}: {a} 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 1\n"
                for i, a in enumerate(addrs)
            )
        )

    def config(self, **kw):
        (HOME / ".config/security-watch").mkdir(parents=True, exist_ok=True)
        (HOME / ".config/security-watch/config.json").write_text(json.dumps(kw))

    def run_sw(self, *argv):
        with open(os.devnull, "w") as null, contextlib.redirect_stdout(null):
            return SW.main(["run", *argv])

    def state(self):
        return json.loads((HOME / ".local/state/security-watch/state.json").read_text())

    def last(self):
        return json.loads((HOME / ".local/state/security-watch/last.json").read_text())

    def notes(self):
        return sorted((HOME / "inbox").glob("security-watch-*.md"))

    def keys(self):
        return sorted(f["key"] for f in self.last()["findings"])


class IpAndScanGuard(Base):
    def test_all_good_is_silent(self):
        self.assertEqual(self.run_sw(), 0)
        self.assertEqual(self.keys(), [])
        self.assertEqual(self.notes(), [])
        self.assertEqual(self.last()["errors"], {})
        line = (HOME / ".local/state/security-watch/posture.txt").read_text()
        self.assertIn("🛡 Security:", line)
        self.assertIn("open findings 0", line)
        self.assertNotIn(IP, line)

    def test_mismatch_refuses_scan_and_needs_two_runs(self):
        self.a = ["198.51.100.9"]
        self.run_sw("--only", "ip,ports")
        self.assertFalse(os.path.exists(NMAP_LOG), "nmap must not run when the A record is not his egress IP")
        self.assertIn("ports", self.last()["errors"])
        self.assertEqual(self.notes(), [], "confirm=2: no note on the first sighting")
        self.run_sw("--only", "ip")
        self.assertEqual(len(self.notes()), 1)
        self.assertIn("ip:dns-mismatch", self.notes()[0].read_text())
        self.assertNotIn("198.51.100.9", self.notes()[0].read_text())

    def test_usage_outcome_findings_are_ok_errors_are_error(self):
        # new findings are a healthy run (outcome ok, the state rides in attrs); any other exec outcome counts as a
        # failed run + an error in the usage store, so only a failing check logs outcome error
        got = []

        class Rec:
            @staticmethod
            def log(feature, kind="invoke", **kw):
                got.append((kind, kw.get("outcome"), (kw.get("attrs") or {}).get("state")))

        old, SW.usage = SW.usage, Rec
        try:
            self.a = ["198.51.100.9"]
            self.run_sw("--only", "ip,ports")  # ports refused: a check error, no note yet
            self.run_sw("--only", "ip")  # second sighting: the note with a new finding
        finally:
            SW.usage = old
        self.assertEqual([g for g in got if g[0] == "exec"], [("exec", "error", None), ("exec", "ok", "findings")])

    def test_no_egress_ip_refuses_scan(self):
        self.egress = None
        self.run_sw("--only", "ports")
        self.assertFalse(os.path.exists(NMAP_LOG))

    def test_scan_args_are_polite_and_target_only_his_ip(self):
        self.run_sw("--only", "ports")
        calls = Path(NMAP_LOG).read_text().splitlines()
        self.assertEqual(len(calls), 2)  # top ports + extra_ports
        for c in calls:
            self.assertTrue(c.endswith(" " + IP), c)
            self.assertIn("-sT", c)
            self.assertIn("--max-rate 100", c)


class Ports(Base):
    def test_router_only_ports_are_not_findings(self):
        Path(NMAP_OUT).write_text(nmap_xml([80, 443, 8291]))
        self.outside = {80: "open", 443: "open", 8291: "closed"}
        self.run_sw("--only", "ports")
        self.assertEqual(self.keys(), [])
        self.assertEqual(self.last()["metrics"]["ports"]["lan_only"], [8291])

    def test_port_open_outside_alerts_once_and_rearms(self):
        Path(NMAP_OUT).write_text(nmap_xml([80, 443, 8291]))
        self.outside = {80: "open", 443: "open", 8291: "open"}
        self.run_sw("--only", "ports")
        self.assertEqual(self.keys(), ["port:8291"])
        self.assertEqual(len(self.notes()), 1)
        self.assertIn("HIGH: Port 8291", self.notes()[0].read_text())
        self.run_sw("--only", "ports")
        self.assertEqual(len(self.notes()), 1, "same finding: no second note")
        self.outside[8291] = "closed"
        self.run_sw("--only", "ports")
        self.assertTrue(self.state()["findings"]["port:8291"]["resolved"])
        self.outside[8291] = "open"
        time.sleep(1.1)  # distinct note file name
        self.run_sw("--only", "ports")
        self.assertEqual(len(self.notes()), 2, "re-armed after it was resolved")

    def test_allowed_ports_never_alert(self):
        self.config(public_allow=[80, 443, 51413])
        Path(NMAP_OUT).write_text(nmap_xml([80, 443, 51413]))
        self.outside = {80: "open", 443: "open", 51413: "open"}
        self.run_sw("--only", "ports")
        self.assertEqual(self.keys(), [])

    def test_cron_runs_scan_weekly_only(self):
        self.run_sw("--cron")
        self.run_sw("--cron")
        self.assertEqual(len(Path(NMAP_LOG).read_text().splitlines()), 2, "second cron run must skip the scan")
        self.assertNotIn("ports", self.last()["ran"])
        self.assertIn("box", self.last()["ran"])


class InternetDB(Base):
    def plant_kev(self, ids):
        import hashlib

        p = HOME / ".cache/container-watch" / (hashlib.sha1(SW.KEV_URL.encode()).hexdigest()[:20] + ".json")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(dict(data={i: dict(added="2026-10-01") for i in ids})))

    def test_extra_port_kev_and_tags(self):
        self.plant_kev(["CVE-2026-0001"])
        self.net.idb = dict(
            ports=[22, 80, 443, 3389], vulns=["CVE-2026-0001", "CVE-2026-0002"], tags=["compromised", "eol-product"]
        )
        self.outside = {22: "open", 3389: "closed"}
        self.run_sw("--only", "internetdb")
        self.assertEqual(
            self.keys(), ["idb-kev:CVE-2026-0001", "idb-port:22", "idb-tag:compromised", "idb-tag:eol-product"]
        )
        self.assertTrue(any("3389" in i and "stale" in i for i in self.last()["info"]))
        m = self.last()["metrics"]["internetdb"]
        self.assertEqual((m["vulns"], m["kev"]), (2, 1))


class Tls(Base):
    def test_names_from_nginx_only_his_domain(self):
        c = SW.Ctx(SW.load_config(), {}, DOMAIN)
        self.assertEqual(SW.nginx_names(c), [f"app.{DOMAIN}", DOMAIN, f"www.{DOMAIN}"])

    def test_expiry_and_invalid(self):
        self.tls = {
            DOMAIN: dict(days=10.0, fp="a", issuer="LE", error=None),
            f"www.{DOMAIN}": dict(days=10.0, fp="a", issuer="LE", error=None),
            f"app.{DOMAIN}": dict(days=3.0, fp="b", issuer="LE", error="hostname mismatch"),
        }
        self.run_sw("--only", "tls")
        f = {x["key"]: x["sev"] for x in self.last()["findings"]}
        self.assertEqual(f, {"tls-exp:a": 2, "tls-exp:b": 3, f"tls-invalid:app.{DOMAIN}": 3})
        self.assertEqual(self.last()["metrics"]["tls"]["certs"], 2)


class CT(Base):
    def test_baseline_then_new_name_and_issuer(self):
        self.net.crt = [dict(name_value=f"{DOMAIN}\n*.{DOMAIN}", issuer_name="C=US, O=Let's Encrypt, CN=R11")]
        self.run_sw("--only", "ct")
        self.assertEqual(self.keys(), [])
        self.net.crt.append(dict(name_value=f"vpn.{DOMAIN}", issuer_name="C=XX, O=Shady CA, CN=X1"))
        self.run_sw("--only", "ct")
        self.assertEqual(self.keys(), ["ct-issuer:shady ca", f"ct-name:vpn.{DOMAIN}"])

    def test_certspotter_fallback_same_issuer_names(self):
        self.net.crt = [dict(name_value=DOMAIN, issuer_name="C=US, O=Let's Encrypt, CN=R11")]
        self.run_sw("--only", "ct")
        self.net.crt_down = True
        self.net.cs = [
            dict(
                dns_names=[DOMAIN], issuer=dict(name="C=US, O=Let's Encrypt, CN=R12", friendly_name="Let's Encrypt R12")
            )
        ]
        self.run_sw("--only", "ct")
        self.assertEqual(self.keys(), [], "switching source must not look like a new CA")
        self.assertEqual(self.last()["metrics"]["ct"]["source"], "certspotter")

    def test_both_down_three_times_is_one_finding(self):
        self.net.crt_down = self.net.cs_down = True
        for _ in range(3):
            self.run_sw("--only", "ct")
        self.assertEqual(self.keys(), ["check-failing:ct"])
        self.assertEqual(len(self.notes()), 1)
        self.net.crt_down = False
        self.run_sw("--only", "ct")
        self.assertTrue(self.state()["findings"]["check-failing:ct"]["resolved"])


class Auth(Base):
    def setUp(self):
        super().setUp()
        self.config(auth_probes={"app": ["/api/status"], "@": ["/admin"]})

    def test_gated_is_quiet(self):
        self.run_sw("--only", "auth")
        self.assertEqual(self.keys(), [])
        self.assertEqual(self.last()["metrics"]["auth"]["gated"], 2)

    def test_open_from_outside_is_high(self):
        self.net.http_codes[f"https://app.{DOMAIN}/api/status"] = 200
        self.outside_http_codes = [200, 200]
        self.run_sw("--only", "auth")
        self.assertEqual(
            {f["key"]: f["sev"] for f in self.last()["findings"]}, {f"auth-open:app.{DOMAIN}/api/status": 3}
        )

    def test_lan_only_open_after_gated_is_medium(self):
        self.run_sw("--only", "auth")
        self.net.http_codes[f"https://app.{DOMAIN}/api/status"] = 200
        self.outside_http_codes = [401]
        self.run_sw("--only", "auth")
        self.assertEqual(
            {f["key"]: f["sev"] for f in self.last()["findings"]}, {f"auth-lan:app.{DOMAIN}/api/status": 2}
        )

    def test_unraid_login_notifications(self):
        p = HOME / ".local/state/unraid-health/last-collect.txt"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            "=====UH meta\nx=1\n=====UH notify_unread\n"
            '@file=a.notify\nevent="Unraid Disk 1 message"\nsubject="Notice [SRV] - Disk 1 returned to normal temperature"\nimportance="normal"\n'  # noqa: E501
            '@file=b.notify\nevent="Unraid Status"\nsubject="Warning [SRV] - 12 failed login attempts"\nimportance="warning"\n'  # noqa: E501
            "=====UH syslog_errors\nfailed login in syslog is not a notification\n"
        )
        self.run_sw("--only", "auth")
        f = self.last()["findings"]
        self.assertEqual(len(f), 1)
        self.assertEqual((f[0]["sev"], f[0]["check"]), (3, "auth"))
        self.assertIn("failed login attempts", f[0]["title"])


class Box(Base):
    def test_secret_modes_never_read(self):
        os.chmod(HOME / ".secrets/token", 0o644)
        os.chmod(HOME / ".secrets", 0o755)
        os.symlink("/etc/hostname", HOME / ".secrets/link")
        with mock.patch("builtins.open", wraps=open) as o:
            self.run_sw("--only", "box")
            self.assertFalse(
                any(".secrets" in str(c.args[0]) for c in o.call_args_list if c.args),
                "must never open files in ~/.secrets",
            )
        self.assertEqual(self.keys(), ["secrets:dir-mode", "secrets:link:link", "secrets:mode:token"])

    def test_listener_baseline_then_new(self):
        self.run_sw("--only", "box")
        self.listen(["0100007F:1FF2", "00000000:17C0", "00000000:1F90", "0B00007F:98A7"])
        self.run_sw("--only", "box")
        self.assertEqual(self.keys(), ["listen:tcp/8080"])
        SW.main(["baseline", "listen"])
        self.run_sw("--only", "box")
        self.assertEqual(self.keys(), [])

    def test_hooks(self):
        os.remove(HOME / ".claude/hooks/risk-check.sh")
        self.settings(
            dict(
                hooks=dict(
                    PreToolUse=[dict(matcher="Read", hooks=[dict(command=str(HOME / ".claude/hooks/vault-guard.sh"))])]
                )
            )
        )
        self.run_sw("--only", "box")
        self.assertEqual(self.keys(), ["hook:failopen:vault-guard.sh", "hook:missing:risk-check.sh"])

    def test_crontab_empty_drift_missing(self):
        hub_rows()  # no hub schedules: missing from cron = missing
        Path(CRON_OUT).write_text("# nothing\n")
        self.run_sw("--only", "box")
        self.assertEqual(self.keys(), ["cron:empty"])
        Path(CRON_OUT).write_text("* * * * * brain-watch --cron\n11 * * * * brain auto\n")
        self.run_sw("--only", "box")
        self.assertEqual(self.keys(), ["cron:drift", "cron:missing:agent-watch", "cron:missing:security-watch"])
        self.assertNotIn("cron:drift", self.notes()[-1].read_text(), "drift needs two runs")

    def _cron_without(self, *gone):
        jobs = "* * * * * brain-watch --cron\n*/5 * * * * agent-watch\n11 * * * * brain auto\n23 6 * * * security-watch run --cron\n"  # noqa: E501
        jobs = "".join(l + "\n" for l in jobs.splitlines() if not any(g in l for g in gone))  # noqa: E741
        (HOME / "crontab").write_text(jobs)
        Path(CRON_OUT).write_text(jobs)

    def test_required_job_live_on_the_hub_is_present(self):
        self._cron_without("agent-watch", "brain auto")
        self.run_sw("--only", "box")
        self.assertEqual(self.keys(), [])
        self.assertTrue(any("'agent-watch'" in i and "job:agent-watch" in i for i in self.last()["info"]))
        self.assertEqual(Path(TOOL_LOG).read_text().split(), ["call", "workflow.schedules", "{}", "--json"])

    def test_registry_hub_but_schedule_not_live_is_missing(self):
        self._cron_without("agent-watch", "brain auto")
        hub_rows(**{"agent-watch": ("shadow", "ACTIVE"), "brain-auto": ("live", "PAUSED")})
        self.run_sw("--only", "box")
        self.assertEqual(self.keys(), ["cron:missing:agent-watch", "cron:missing:brain auto"])
        self.assertIn("no live hub schedule", self.last()["findings"][0]["detail"])

    def test_live_on_hub_but_registry_says_cron_is_missing(self):
        self._cron_without("security-watch")
        self.run_sw("--only", "box")
        self.assertEqual(self.keys(), ["cron:missing:security-watch"])
        self.assertFalse(os.path.exists(TOOL_LOG), "no hub read when the registry says cron")

    def test_hub_unreadable_fails_safe(self):
        self._cron_without("agent-watch")
        for broken in ("rc", "garbage", "not ok"):
            with self.subTest(broken=broken):
                if broken == "rc":
                    Path(TOOL_RC).write_text("3")
                else:
                    os.remove(TOOL_RC) if os.path.exists(TOOL_RC) else None
                    Path(TOOL_OUT).write_text("{" if broken == "garbage" else json.dumps(dict(ok=False, error={})))
                self.run_sw("--only", "box")
                self.assertEqual(self.keys(), ["cron:missing:agent-watch"])
                self.assertIn("could not be read", self.last()["findings"][0]["detail"])

    def test_registry_unreadable_fails_safe(self):
        self._cron_without("agent-watch")
        with mock.patch.object(SW, "SCHEDULES", os.path.join(SANDBOX, "no-such-registry.toml")):
            self.run_sw("--only", "box")
        self.assertEqual(self.keys(), ["cron:missing:agent-watch"])
        self.assertFalse(os.path.exists(TOOL_LOG), "no hub read without the registry")

    def test_all_in_cron_never_asks_the_hub(self):
        self.run_sw("--only", "box")
        self.assertEqual(self.keys(), [])
        self.assertFalse(os.path.exists(TOOL_LOG))

    def test_loopback(self):
        for a, want in (
            ("0100007F", True),
            ("0B00007F", True),
            ("00000000", False),
            ("0258A8C0", False),
            ("00000000000000000000000001000000", True),
            ("0000000000000000FFFF00000100007F", True),
            ("00000000000000000000000000000000", False),
        ):
            self.assertEqual(SW.is_loopback(a), want, a)


class AckDryRunPosture(Base):
    def test_ack_silences_and_expires(self):
        os.chmod(HOME / ".secrets/token", 0o644)
        SW.main(["ack", "secrets:mode:token", "--label", "test", "--until", "never"])
        self.run_sw("--only", "box")
        self.assertEqual(self.notes(), [])
        self.assertIn("open findings 0", (HOME / ".local/state/security-watch/posture.txt").read_text())
        SW.main(["ack", "rm", "secrets:mode:token"])
        SW.main(["ack", "secrets:mode:token", "--until", "2000-01-01"])
        self.run_sw("--only", "box")
        self.assertEqual(len(self.notes()), 1)

    def test_dry_run_writes_nothing(self):
        os.chmod(HOME / ".secrets/token", 0o644)
        self.run_sw("--dry-run", "--only", "box")
        self.assertFalse((HOME / ".local/state/security-watch/state.json").exists())
        self.assertEqual(self.notes(), [])

    def test_unknown_check_is_usage_error(self):
        self.assertEqual(self.run_sw("--only", "nope"), 2)

    def test_posture_counts_container_kev(self):
        p = HOME / ".local/state/container-watch/last.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(dict(kev=dict(available=True, affecting=["CVE-2026-1"]))))
        self.run_sw("--only", "box")
        self.assertIn("container KEV 1", (HOME / ".local/state/security-watch/posture.txt").read_text())

    def test_only_allowed_hosts_contacted(self):
        self.config(auth_probes={"app": ["/x"]})
        self.run_sw()
        allowed = {"internetdb.shodan.io", "crt.sh", "api.certspotter.com"}
        self.assertTrue(all(h in allowed or h.endswith(DOMAIN) for h in self.net.hosts), self.net.hosts)


class Mtls(Base):
    """mtls_probes: the app's internet path (device certificate required); the probe itself is faked here, the real
    socket code is exercised in MtlsProbeReal against a local TLS server."""

    GOOD = dict(reachable=True, cert_ok=True, days=700.0, nocert_answered=False, wrong_sni_answered=False, error=None)
    DOWN = dict(
        reachable=False,
        cert_ok=False,
        days=None,
        nocert_answered=False,
        wrong_sni_answered=False,
        error="ConnectionRefusedError",
    )

    def setUp(self):
        super().setUp()
        self.config(mtls_probes=[dict(name=f"app.{DOMAIN}", port=8443, ca_file="~/ca.pem")])
        self.res = dict(self.DOWN)
        self.calls = []
        x = mock.patch.object(
            SW, "mtls_probe", lambda ip, n, port, ca: self.calls.append((ip, n, port, ca)) or dict(self.res)
        )
        x.start()
        self.addCleanup(x.stop)

    def test_pending_until_first_seen_then_down_is_low(self):
        self.run_sw("--only", "auth")
        self.assertEqual(self.keys(), [])
        self.assertEqual(self.last()["metrics"]["auth"]["mtls"][0]["state"], "pending")
        self.assertEqual(self.calls[0][:3], (IP, f"app.{DOMAIN}", 8443))
        self.assertTrue(self.calls[0][3].endswith("/ca.pem") and not self.calls[0][3].startswith("~"))
        self.res, self.outside = dict(self.GOOD), {8443: "open"}
        self.run_sw("--only", "auth")
        self.assertEqual(self.keys(), [])
        self.assertEqual(
            self.last()["metrics"]["auth"]["mtls"][0],
            dict(name=f"app.{DOMAIN}:8443", state="ok", days=700.0, outside="open"),
        )
        self.res = dict(self.DOWN)
        self.run_sw("--only", "auth")
        self.assertEqual({f["key"]: f["sev"] for f in self.last()["findings"]}, {f"mtls-down:app.{DOMAIN}:8443": 1})

    def test_answer_without_cert_is_high_wrong_sni_medium_expiry(self):
        self.res = dict(self.GOOD, nocert_answered=True, wrong_sni_answered=True, days=20.0)
        self.run_sw("--only", "auth")
        self.assertEqual(
            {f["key"]: f["sev"] for f in self.last()["findings"]},
            {f"mtls-open:app.{DOMAIN}:8443": 3, f"mtls-sni:app.{DOMAIN}:8443": 2, f"mtls-exp:app.{DOMAIN}:8443": 2},
        )

    def test_wrong_ca_is_medium(self):
        self.res = dict(self.GOOD, cert_ok=False, days=None, wrong_sni_answered=True, error="verify: self-signed")
        self.run_sw("--only", "auth")
        self.assertIn(f"mtls-cert:app.{DOMAIN}:8443", self.keys())


class MtlsProbeReal(unittest.TestCase):
    """SW.mtls_probe against a local TLS 1.3 server (openssl-made CA + server cert for app.example.org)."""

    @classmethod
    def setUpClass(cls):
        import subprocess

        d = cls.d = Path(tempfile.mkdtemp(prefix="mtls-", dir=SANDBOX))

        def run(*a):
            subprocess.run(a, check=True, capture_output=True, cwd=d)

        for name in ("ca", "other"):  # the server's CA and an unrelated one (the wrong CA)
            run(
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "ec",
                "-pkeyopt",
                "ec_paramgen_curve:P-256",
                "-nodes",
                "-keyout",
                f"{name}.key",
                "-out",
                f"{name}.pem",
                "-days",
                "30",
                "-subj",
                f"/CN=test {name}",
                # a CA as Python 3.13's strict X.509 check wants it (ssl.VERIFY_X509_STRICT is on by default there)
                "-addext",
                "basicConstraints=critical,CA:TRUE",
                "-addext",
                "keyUsage=critical,keyCertSign,cRLSign",
            )
        run(
            "openssl",
            "req",
            "-newkey",
            "ec",
            "-pkeyopt",
            "ec_paramgen_curve:P-256",
            "-nodes",
            "-keyout",
            "srv.key",
            "-out",
            "srv.csr",
            "-subj",
            "/CN=app.example.org",
        )
        (d / "ext").write_text(
            "subjectAltName=DNS:app.example.org\nauthorityKeyIdentifier=keyid\nkeyUsage=critical,digitalSignature\n"
            "extendedKeyUsage=serverAuth\n"
        )
        run(
            "openssl",
            "x509",
            "-req",
            "-in",
            "srv.csr",
            "-CA",
            "ca.pem",
            "-CAkey",
            "ca.key",
            "-CAcreateserial",
            "-out",
            "srv.pem",
            "-days",
            "20",
            "-extfile",
            "ext",
        )

    def serve(self, require):
        import threading

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_3
        ctx.load_cert_chain(str(self.d / "srv.pem"), str(self.d / "srv.key"))
        ctx.load_verify_locations(str(self.d / "ca.pem"))
        ctx.verify_mode = ssl.CERT_REQUIRED if require else ssl.CERT_OPTIONAL
        ls = socket.socket()
        ls.bind(("127.0.0.1", 0))
        ls.listen(8)

        def loop():
            while True:
                try:
                    c, _ = ls.accept()
                except OSError:
                    return
                try:
                    with ctx.wrap_socket(c, server_side=True) as s:
                        if s.recv(100):
                            s.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
                except (ssl.SSLError, OSError):
                    pass

        threading.Thread(target=loop, daemon=True).start()
        self.addCleanup(ls.close)
        return ls.getsockname()[1]

    def test_required_cert_gives_no_answer(self):
        port = self.serve(require=True)
        r = SW.mtls_probe("127.0.0.1", "app.example.org", port, str(self.d / "ca.pem"), timeout=5)
        self.assertTrue(r["reachable"] and r["cert_ok"], r)
        self.assertFalse(r["nocert_answered"])
        self.assertTrue(r["wrong_sni_answered"])  # this toy server answers any SNI (nginx would close it)
        self.assertTrue(18 < r["days"] < 21)

    def test_optional_cert_is_caught(self):
        port = self.serve(require=False)
        r = SW.mtls_probe("127.0.0.1", "app.example.org", port, str(self.d / "ca.pem"), timeout=5)
        self.assertTrue(r["nocert_answered"], r)

    def test_wrong_ca_and_closed_port(self):
        port = self.serve(require=True)
        r = SW.mtls_probe("127.0.0.1", "app.example.org", port, str(self.d / "other.pem"), timeout=5)
        self.assertFalse(r["cert_ok"])
        self.assertTrue(r["error"].startswith("verify"), r)
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
        s.close()
        r = SW.mtls_probe("127.0.0.1", "app.example.org", free, str(self.d / "ca.pem"), timeout=2)
        self.assertEqual((r["reachable"], r["cert_ok"]), (False, False))


if __name__ == "__main__":
    try:
        unittest.main(verbosity=1)
    finally:
        shutil.rmtree(SANDBOX, ignore_errors=True)
