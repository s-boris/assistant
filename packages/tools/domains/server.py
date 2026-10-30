"""Server: typed READ tools over the Unraid checks: `unraid-health` (a pure look: --dry-run, nothing saved or sent),
`unraid-capacity` (fill-up forecast), `container-watch` (updates + advisories, --dry-run) and the dispatcher's
read-only listing verbs through `unraid-ssh run` (containers, VMs, plugins). Every server read is one call of a
read-only dispatcher verb; the SSH key never leaves those scripts.

Boundaries (binding): no writes. unraid-do (container restart/update, VMs, User Scripts, appdata edits, plugin
updates, ISOs) stays a CLI with its `--owner-ok` for the big ones: not a tool in this phase. Env:
TOOLS_UNRAID_HEALTH_BIN, TOOLS_UNRAID_CAPACITY_BIN, TOOLS_CONTAINER_WATCH_BIN, TOOLS_UNRAID_SSH_BIN.
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, Field

from tools.clirun import bin_path, first_line, run, run_json
from tools.core import REGISTRY, Domain, Format, ToolError, ToolInput, ToolOutput, tool

NOTES = (
    "`unraid-health` / `unraid-capacity` / `container-watch` / `unraid-ssh` (CLIs, managed; skills "
    "server-health, unraid-capacity, container-watch): the crons (daily health, capacity record + alert, weekly "
    "container-watch --notify) and acks (`unraid-health ack add|rm`) stay there. Writes on the server: "
    "`unraid-do` (restart and update containers, VMs, User Scripts, appdata with backup, plugin updates, ISOs; "
    "the big ones need his OK as `--owner-ok`), never a tool here."
)

REGISTRY.add_domain(
    Domain(
        name="server",
        title="Server",
        summary="His Unraid server, read-only: health (disks, parity, temperatures, pools, containers, VMs),"
        " capacity forecast, container updates and advisories, and the container / VM / plugin lists.",
        feature="gateway.server",
        notes=NOTES,
    )
)


def _fail(rc: int, out: str, err: str, what: str) -> ToolError:
    msg = first_line(err, out, default=f"{what}: exit {rc}")
    if (rc == 2 and what in ("unraid-health", "container-watch")) or rc in (124, 255) or "ssh" in msg.lower():
        return ToolError("unavailable", msg, "the server or its SSH dispatcher is unreachable: retry in a minute")
    if rc == 2:
        return ToolError("invalid_args", msg)
    return ToolError("failed", msg, f"run `{what}` by hand to see why")


def _tsv(verb: str, cols: int) -> list[list[str]]:
    """Rows of a dispatcher listing verb (tab-separated), via `unraid-ssh run VERB`."""
    rc, out, err = run(bin_path("TOOLS_UNRAID_SSH_BIN", "unraid-ssh"), ["run", verb], timeout=60)
    if rc != 0:
        raise _fail(rc, out, err, f"unraid-ssh run {verb}")
    rows = []
    for ln in out.splitlines():
        if not ln.strip():
            continue
        parts = ln.split("\t")
        rows.append((parts + [""] * cols)[:cols])
    return rows


# ------------------------------------------------------------------ models


class EmptyIn(ToolInput):
    pass


class CapacityIn(ToolInput):
    live: bool = Field(default=False, description="take a fresh reading as 'now' (one SSH call, not saved)")


class ContainersIn(ToolInput):
    offline: bool = Field(default=False, description="upstream from the cache only (no GitHub/NVD fetches)")


class Finding(BaseModel):
    key: str
    level: str = Field(description="info | warn | crit")
    status: str | None = Field(default=None, description="new | ongoing | resolved | acked")
    msg: str


class HealthOut(ToolOutput):
    DETAIL: ClassVar[tuple[str, ...]] = ("info", "metrics")
    host: str | None = None
    new: int = Field(description="findings that are new since the last run")
    findings: list[Finding] = Field(description="alerts (new), ongoing, resolved and acked findings")
    gaps: list[Any] = Field(default_factory=list, description="what could not be read")
    summary: dict[str, Any] = Field(description="version, uptime_h, array %, free TB, CPU °C, pools, containers, VMs")
    info: list[Any] | None = None
    metrics: dict[str, Any] | None = None

    def render(self, fmt: Format = "concise") -> str:
        s = self.summary
        head = (
            f"{self.host}: Unraid {s.get('version')}, array {s.get('array_pct')} % used ({s.get('array_free_tb')} TB"
            f" free), CPU {s.get('cpu_c')} °C, containers {s.get('containers_running')}/{s.get('containers')} running,"
            f" VMs {s.get('vms_running')}/{s.get('vms')} running"
        )
        lines = [head]
        lines += [f"{f.level.upper()} [{f.status}] {f.msg}" for f in self.findings]
        lines += [f"gap: {g}" for g in self.gaps]
        return "\n".join(lines) if len(lines) > 1 else head + "\nno findings"


class CapacityOut(ToolOutput):
    DETAIL: ClassVar[tuple[str, ...]] = ("forecast",)
    as_of: str | None = None
    array: dict[str, Any] = Field(description="pct used, free / usable TB, eta date + days (projected full)")
    pools: dict[str, dict[str, Any]] = Field(
        description="per pool and docker.img: pct, free GB, eta (null = not filling)"
    )
    alerts: list[Any] = Field(default_factory=list)
    forecast: dict[str, Any] | None = Field(
        default=None, description="the full forecast (detailed): trends, disks, model"
    )

    def render(self, fmt: Format = "concise") -> str:
        a = self.array
        eta = a.get("eta") or {}
        out = [
            f"array {a.get('pct')} % used, {a.get('usable_tb')} TB usable free"
            + (f", full ~{eta.get('date')} ({eta.get('days')} d)" if eta.get("date") else ", not filling")
        ]
        for n, p in self.pools.items():
            e = p.get("eta") or {}
            out.append(
                f"{n}: {p.get('pct')} % used, {p.get('free_gb')} GB free"
                + (f", full ~{e.get('date')} ({e.get('days')} d)" if e.get("date") else "")
            )
        return "\n".join(out + [f"alert: {x.get('msg') if isinstance(x, dict) else x}" for x in self.alerts])


class ContainersOut(ToolOutput):
    DETAIL: ClassVar[tuple[str, ...]] = ("rows",)
    generated: str | None = None
    containers: int
    actionable: list[dict[str, Any]] = Field(
        description="container, app, installed, latest, kind (cve/unfixed/security-release/stuck), sev, ids, why"
    )
    kev: dict[str, Any] | None = Field(default=None, description="CISA KEV: catalogue size, affecting CVEs")
    errors: list[Any] = Field(default_factory=list)
    rows: list[dict[str, Any]] | None = Field(
        default=None, description="every container: image, tag, installed, latest ..."
    )

    def render(self, fmt: Format = "concise") -> str:
        if not self.actionable:
            head = f"{self.containers} containers: nothing actionable"
        else:
            head = f"{self.containers} containers, {len(self.actionable)} actionable:"
        lines = [head] + [
            f"  {x.get('container')} ({x.get('app')} {x.get('installed')} → {x.get('latest')}): {x.get('kind')}"
            f" {' '.join(x.get('ids') or [])} {str(x.get('why') or '')[:120]}"
            for x in self.actionable
        ]
        return "\n".join(lines + [f"error: {e}" for e in self.errors])


class DockerRow(BaseModel):
    name: str
    state: str
    access: str = Field(description="ok (unraid-do may restart/update it) | not-unraid-managed | off-limits")


class DockerOut(ToolOutput):
    count: int
    containers: list[DockerRow]

    def render(self, fmt: Format = "concise") -> str:
        return "\n".join(
            f"{c.name}: {c.state}" + ("" if c.access == "ok" else f" ({c.access})") for c in self.containers
        )


class VmRow(BaseModel):
    name: str
    state: str
    critical: bool = Field(description="a VM the dispatcher guards more strictly (its critical list)")
    uuid: str | None = None


class VmsOut(ToolOutput):
    count: int
    vms: list[VmRow]

    def data(self, fmt: Format = "concise") -> dict[str, Any]:
        ex = None if fmt == "detailed" else {"uuid"}
        return {"count": self.count, "vms": [v.model_dump(exclude_none=True, exclude=ex) for v in self.vms]}

    def render(self, fmt: Format = "concise") -> str:
        return "\n".join(f"{v.name}: {v.state}" + (" (critical)" if v.critical else "") for v in self.vms)


class PluginRow(BaseModel):
    name: str
    installed: str
    available: str | None = Field(default=None, description="a newer version Unraid has downloaded (null = none)")


class PluginsOut(ToolOutput):
    count: int
    updates: int
    plugins: list[PluginRow]

    def render(self, fmt: Format = "concise") -> str:
        rows = [p for p in self.plugins if p.available] if fmt == "concise" else self.plugins
        head = f"{self.count} plugins, {self.updates} with an update"
        return "\n".join(
            [head] + [f"  {p.name} {p.installed}" + (f" → {p.available}" if p.available else "") for p in rows]
        )


# ------------------------------------------------------------------ tools


def _tb(b: Any) -> float | None:
    return round(b / 1e12, 2) if isinstance(b, (int, float)) else None


def _summary(d: dict[str, Any]) -> dict[str, Any]:
    m = d.get("metrics") or {}
    sysm, at = m.get("system") or {}, m.get("array_total") or {}
    ctr = m.get("containers") or {}
    vms = m.get("vms") or {}
    up = sysm.get("uptime")
    return {
        "version": sysm.get("version"),
        "uptime_h": round(up / 3600, 1) if isinstance(up, (int, float)) else None,
        "array_pct": round(at["pct"], 1) if isinstance(at.get("pct"), (int, float)) else None,
        "array_free_tb": _tb(at.get("free")),
        "cpu_c": (m.get("sensors") or {}).get("cpu"),
        "pools": m.get("pool_status"),
        "containers": len(ctr),
        "containers_running": sum(1 for c in ctr.values() if isinstance(c, dict) and c.get("status") == "running"),
        "vms": len(vms),
        "vms_running": sum(1 for v in vms.values() if v == "running"),
    }


@tool("server.health", effect="read", rate="10/min", idempotent=True, timeout_s=120, examples=({},))
def health(a: EmptyIn) -> HealthOut:
    """A fresh read-only health check of the Unraid server: new, ongoing and acked findings plus a summary.
    One SSH call; nothing saved, nothing sent. Summary: array use, CPU, pools, containers, VMs. For "is the server
    ok", "any problems on Unraid", "is a disk failing", "is container X down"."""
    exe = bin_path("TOOLS_UNRAID_HEALTH_BIN", "unraid-health")
    rc, d = run_json(exe, ["--json", "--dry-run"], "unraid-health", ok=(0, 1), timeout=110, fail=_fail)
    findings = []
    for group, status in (("alerts", "new"), ("ongoing", "ongoing"), ("acked", "acked"), ("resolved", "resolved")):
        for f in d.get(group) or []:
            if isinstance(f, dict):
                findings.append(
                    Finding(
                        key=str(f.get("key")),
                        level=str(f.get("level") or "info"),
                        status=f.get("status") or status,
                        msg=str(f.get("msg") or f.get("title") or ""),
                    )
                )
    return HealthOut(
        host=d.get("host"),
        new=len(d.get("alerts") or []),
        findings=findings,
        gaps=d.get("gaps") or [],
        summary=_summary(d),
        info=d.get("info") or [],
        metrics=d.get("metrics") or {},
    )


@tool("server.capacity", effect="read", rate="10/min", idempotent=True, timeout_s=90, examples=({}, {"live": True}))
def capacity(a: CapacityIn) -> CapacityOut:
    """Space left and the fill-up forecast for the array, the pools and docker.img.
    The array's projected full date comes from the 30-day trend. For "how much space is left", "when will the
    server be full"."""
    exe = bin_path("TOOLS_UNRAID_CAPACITY_BIN", "unraid-capacity")
    _, d = run_json(
        exe, ["forecast"] + (["--live"] if a.live else []), "unraid-capacity forecast", timeout=80, fail=_fail
    )
    arr = d.get("array") or {}
    pools = {}
    for name, p in (d.get("pools") or {}).items():
        if isinstance(p, dict):
            pools[name] = {
                "pct": round(p["pct"], 1) if isinstance(p.get("pct"), (int, float)) else None,
                "free_gb": round(p["free"] / 1e9) if isinstance(p.get("free"), (int, float)) else None,
                "eta": p.get("eta"),
            }
    return CapacityOut(
        as_of=d.get("as_of_local"),
        array={
            "pct": round(arr["pct"], 1) if isinstance(arr.get("pct"), (int, float)) else None,
            "free_tb": _tb(arr.get("free")),
            "usable_tb": _tb(arr.get("usable")),
            "eta": arr.get("eta"),
            "disks": arr.get("n_disks"),
        },
        pools=pools,
        alerts=d.get("alerts") or [],
        forecast=d,
    )


@tool(
    "server.containers",
    effect="read",
    rate="6/min",
    idempotent=True,
    timeout_s=240,
    examples=({}, {"offline": True}),
)
def containers(a: ContainersIn) -> ContainersOut:
    """Container updates and security advisories against what runs on the server; nothing is pulled or updated.
    Sources: GitHub releases + advisories, NVD, CISA KEV. Actionable = an affecting advisory or CVE, a security
    release left un-updated, or long-stuck updates. For "are my containers up to date", "is X vulnerable"."""
    exe = bin_path("TOOLS_CONTAINER_WATCH_BIN", "container-watch")
    argv = ["--json", "--dry-run"] + (["--offline"] if a.offline else [])
    _, d = run_json(exe, argv, "container-watch", ok=(0, 1), timeout=230, fail=_fail)
    rows = [r for r in d.get("rows") or [] if isinstance(r, dict)]
    return ContainersOut(
        generated=d.get("generated"),
        containers=len(rows),
        actionable=[x for x in d.get("actionable") or [] if isinstance(x, dict)],
        kev=d.get("kev"),
        errors=d.get("errors") or [],
        rows=rows,
    )


@tool("server.docker", effect="read", rate="20/min", idempotent=True, timeout_s=70, examples=({},))
def docker(a: EmptyIn) -> DockerOut:
    """Every container on the server with its state (running, exited ...) and whether unraid-do may act on it."""
    rows = [DockerRow(name=n, state=s, access=w or "ok") for n, s, w in _tsv("docker-list", 3)]
    return DockerOut(count=len(rows), containers=rows)


@tool("server.vms", effect="read", rate="20/min", idempotent=True, timeout_s=70, examples=({},))
def vms(a: EmptyIn) -> VmsOut:
    """The server's VMs and whether each is running (critical = on the dispatcher's guarded list)."""
    rows = [VmRow(name=n, state=s, uuid=u or None, critical=f == "critical") for n, s, u, f in _tsv("vm-list", 4)]
    return VmsOut(count=len(rows), vms=rows)


@tool("server.plugins", effect="read", rate="20/min", idempotent=True, timeout_s=70, examples=({},))
def plugins(a: EmptyIn) -> PluginsOut:
    """Unraid plugins with their installed version and a newer one Unraid already knows of."""
    rows = [
        PluginRow(name=n, installed=i, available=None if v in ("", "-", i) else v) for n, i, v in _tsv("plugin-list", 3)
    ]
    return PluginsOut(count=len(rows), updates=sum(1 for r in rows if r.available), plugins=rows)
