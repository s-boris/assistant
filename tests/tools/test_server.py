"""Server tools against stub unraid-health / unraid-capacity / container-watch / unraid-ssh: always the pure-look
flags (--dry-run), only the dispatcher's read-only listing verbs, parsing, and unreachable = unavailable. No tool runs
unraid-do."""

from __future__ import annotations

from tools import gateway
from tools.core import load
from tools.domains import server

HEALTH = {
    "host": "tower",
    "alerts": [{"key": "disk:disk4", "level": "warn", "msg": "disk4: 44 UDMA CRC errors", "status": "new"}],
    "ongoing": [{"key": "btrfs:docker", "level": "warn", "msg": "docker.img error counters", "status": "ongoing"}],
    "acked": [],
    "resolved": [],
    "gaps": [],
    "info": ["x"] * 50,
    "metrics": {
        "system": {"version": "7.3.3", "uptime": 7200},
        "array_total": {"free": 18_069_867_900_928, "pct": 86.31},
        "sensors": {"cpu": 44.0},
        "pool_status": {"cache": "ONLINE"},
        "containers": {"a": {"status": "running"}, "b": {"status": "exited"}},
        "vms": {"vm1": "running", "vm2": "shut off"},
    },
}
CAP = {
    "as_of_local": "2026-10-22 03:17",
    "array": {
        "pct": 86.3,
        "free": 18e12,
        "usable": 16.2e12,
        "n_disks": 12,
        "eta": {"days": 81.6, "date": "2027-01-11"},
    },
    "pools": {
        "cache": {"pct": 89.7, "free": 101.5e9, "eta": {"days": 12.9, "date": "2026-11-03"}},
        "docker.img": {"pct": 49.1, "free": 25e9, "eta": None},
    },
    "alerts": [{"key": "pool:cache", "msg": "cache pool projected full in ~13 days"}],
    "disks": {},
}


def call(name, **args):
    return gateway.call(name, args)


def test_health_capacity_containers(stubs):
    stubs.on("unraid-health", ["--json", "--dry-run"], HEALTH, rc=1)  # 1 = new issues: still an answer
    stubs.on("unraid-capacity", ["forecast"], CAP)
    stubs.on(
        "container-watch",
        ["--json", "--dry-run"],
        {
            "generated": "2026-10-22T02:52:15+00:00",
            "rows": [{"container": "a"}, {"container": "b"}],
            "actionable": [
                {
                    "container": "a",
                    "app": "A",
                    "installed": "1.0",
                    "latest": "1.0",
                    "kind": "unfixed",
                    "ids": ["GHSA-x"],
                    "why": "path traversal",
                }
            ],
            "errors": [],
            "kev": {"affecting": []},
        },
        rc=1,
    )
    h = call("server.health")
    assert h.ok and h.data["new"] == 1 and len(h.data["findings"]) == 2 and "info" not in h.data
    assert h.data["summary"] == {
        "version": "7.3.3",
        "uptime_h": 2.0,
        "array_pct": 86.3,
        "array_free_tb": 18.07,
        "cpu_c": 44.0,
        "pools": {"cache": "ONLINE"},
        "containers": 2,
        "containers_running": 1,
        "vms": 2,
        "vms_running": 1,
    }
    assert "WARN [new] disk4: 44 UDMA CRC errors" in h.text
    assert len(call("server.health", format="detailed").data["info"]) == 50
    c = call("server.capacity", live=True)
    assert c.data["array"]["eta"]["date"] == "2027-01-11" and c.data["pools"]["cache"]["free_gb"] == 102
    assert "forecast" not in c.data and c.text.splitlines()[-1] == "alert: cache pool projected full in ~13 days"
    w = call("server.containers", offline=True)
    assert w.data["containers"] == 2 and "rows" not in w.data and "a (A 1.0 → 1.0): unfixed GHSA-x" in w.text
    assert ["--json", "--dry-run"] == stubs.calls("unraid-health")[0]
    assert ["forecast", "--live"] in stubs.calls("unraid-capacity")
    assert ["--json", "--dry-run", "--offline"] in stubs.calls("container-watch")


def test_listings(stubs):
    stubs.on("unraid-ssh", ["run", "docker-list"], "radarr\trunning\tok\nclaude-bot\trunning\toff-limits\n")
    stubs.on("unraid-ssh", ["run", "vm-list"], "vm1\trunning\tuuid-1\tcritical\nvm2\tshut off\tuuid-2\t\n")
    stubs.on(
        "unraid-ssh", ["run", "plugin-list"], "p.one\t2026.01.01\t-\np.two\t2026.01.01\t2026.02.01\np.three\t1\t1\n"
    )
    d = call("server.docker")
    assert d.data["containers"][1] == {"name": "claude-bot", "state": "running", "access": "off-limits"}
    assert d.text == "radarr: running\nclaude-bot: running (off-limits)"
    v = call("server.vms")
    assert v.data["vms"][0] == {"name": "vm1", "state": "running", "critical": True}  # concise: no uuid
    assert call("server.vms", format="detailed").data["vms"][1]["uuid"] == "uuid-2"
    p = call("server.plugins")
    assert p.data["updates"] == 1 and p.text == "3 plugins, 1 with an update\n  p.two 2026.01.01 → 2026.02.01"
    assert {tuple(c) for c in stubs.calls("unraid-ssh")} == {
        ("run", "docker-list"),
        ("run", "vm-list"),
        ("run", "plugin-list"),
    }


def test_unreachable_and_read_only(stubs):
    stubs.on("unraid-health", [], "", rc=2, err="unraid-health: ssh: connect to host unraid port 22: timed out\n")
    stubs.on("unraid-ssh", ["run"], "", rc=255, err="ssh: connect to host unraid: No route to host\n")
    stubs.on("container-watch", [], "", rc=2, err="container-watch: could not collect\n")
    assert call("server.health").error["code"] == "unavailable"
    assert call("server.docker").error["code"] == "unavailable"
    assert call("server.containers").error["code"] == "unavailable"
    reg = load()
    specs = reg.in_domain("server")
    assert specs and {s.effect for s in specs} == {"read"}
    assert {s.name for s in specs} == {  # no unraid-do tool: adding one is a deliberate change (his --owner-ok)
        "server.health",
        "server.capacity",
        "server.containers",
        "server.docker",
        "server.vms",
        "server.plugins",
    }
    assert "TOOLS_UNRAID_DO_BIN" not in open(server.__file__).read()
