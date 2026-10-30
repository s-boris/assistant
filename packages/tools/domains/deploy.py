"""Deploy: typed tools over `assistant` for the brain's own code: the managed ~/bin files and the claude-hub release.
Lands stay `assistant land BRANCH` (CI, the hub runner's signed record, the eval subset).

Boundaries (binding): a hub release whose protected paths changed (secret loader, approvals, policy, redaction, the
release verifier) loads only after his ✅ on its digest: the tool then returns approval_required with the approval
(the hub mints it, `assistant deploy` sends him its card; the brain can't decide it) and changes nothing. `plan`
(the default) only says what would happen. A hub deploy waits up to CALL_WAIT_MIN minutes for a live voice call to
end (`--wait-calls`, so the whole run fits the 580 s budget); still live = conflict, CURRENT unchanged. No --force
here: dropping his call is the CLI's, by hand.
Env: TOOLS_ASSISTANT_BIN.
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import Field

from tools.clirun import bin_path, first_line, run, run_json
from tools.core import REGISTRY, Domain, Format, ToolError, ToolInput, ToolOutput, tool

NOTES = (
    "`assistant` (CLI; tests in the repo): `assistant land BRANCH` (CI + the hub runner's signed record + the eval"
    " subset; `ci-gate status|shadow|enforce|off`), `assistant rollback [--target hub]`, `assistant cron`. Deploys"
    " here are the managed ~/bin files of main (target bin) and the hub release (target hub, loads through the"
    " loader; protected paths wait for his ✅)."
)

REGISTRY.add_domain(
    Domain(
        name="deploy",
        title="Deploy",
        summary="My own code: what is deployed (managed ~/bin files, the hub release, the CI gate) and deploying main"
        " to ~/bin or to the hub.",
        feature="gateway.deploy",
        notes=NOTES,
    )
)


CALL_WAIT_MIN = 5  # hub deploys: minutes a live call may hold the switch (5 min + build + 90 s /health < 580 s)


def _exe() -> str:
    return bin_path("TOOLS_ASSISTANT_BIN", "assistant")


def _fail(rc: int, out: str, err: str, what: str) -> ToolError:
    msg = first_line(err, out, default=f"{what}: exit {rc}")
    if rc == 8:
        return ToolError(
            "approval_required", msg, "his ✅ on the card the deploy sent (or his console line), then deploy again"
        )
    if rc == 7:
        return ToolError("conflict", msg, "another land or deploy holds the lock: retry in a few minutes")
    if rc == 9:
        return ToolError("conflict", msg, "a voice call is live: deploy again after it ends")
    if rc == 2:
        return ToolError("invalid_args", msg)
    if rc == 4:
        return ToolError("unavailable", msg, "the hub did not answer or did not run the release: `hub status`")
    return ToolError("failed", msg, f"run `{what}` by hand to see why")


class StatusIn(ToolInput):
    pass


class StatusOut(ToolOutput):
    release: str | None = Field(default=None, description="the ~/bin release (main's commit, 12 hex)")
    drift: int = Field(default=0, description="managed files whose live bytes differ")
    gate: str = Field(default="shadow", description="the land gate: shadow | enforce | off")
    signed_green_lands: int = 0
    recent: list[dict[str, Any]] = Field(default_factory=list)
    DETAIL: ClassVar[tuple[str, ...]] = ("recent",)

    def render(self, fmt: Format = "concise") -> str:
        head = (
            f"~/bin release {self.release or '-'}, drift {self.drift} · land gate {self.gate}, "
            f"{self.signed_green_lands} signed green lands"
        )
        lines = [head]
        if fmt == "detailed":
            for r in self.recent:
                rr = r.get("runner") or {}
                lines.append(
                    f"  {r.get('release')} {r.get('branch')}: runner {rr.get('state') or '-'}"
                    f" {rr.get('secs') or '-'} s, local CI {r.get('ci_local_secs') or '-'} s"
                )
        return "\n".join(lines)


class RunIn(ToolInput):
    target: Literal["bin", "hub"] = Field(default="bin", description="bin = managed ~/bin files, hub = hub release")
    rev: str | None = Field(default=None, pattern=r"^[0-9a-f]{7,40}$", description="a main commit (default: main)")
    plan: bool = Field(default=True, description="only say what would happen; false = deploy")


class RunOut(ToolOutput):
    target: str
    done: bool = Field(description="false = plan only")
    lines: list[str] = Field(default_factory=list, description="what `assistant` said (last lines)")

    def render(self, fmt: Format = "concise") -> str:
        return f"{self.target}: {'deployed' if self.done else 'plan'}\n" + "\n".join(self.lines[-12:])


@tool("deploy.status", effect="read", rate="30/min", idempotent=True, timeout_s=40, examples=({},))
def status(a: StatusIn) -> StatusOut:
    """What is deployed: the ~/bin release and drift, the land gate's mode and signed green lands, recent lands
    with the hub runner's and local CI's seconds. For "what's deployed", "is the CI gate on", "did the last land
    get a signed record"."""
    exe = _exe()
    _, st = run_json(exe, ["status", "--json"], "assistant status", ok=(0, 3), timeout=30, fail=_fail)
    _, gate = run_json(exe, ["ci-gate", "status", "--json"], "assistant ci-gate status", timeout=30, fail=_fail)
    drift = st.get("drift")
    return StatusOut(
        release=st.get("release"),
        drift=len(drift) if isinstance(drift, list) else int(drift or 0),
        gate=str(gate.get("mode") or "shadow"),
        signed_green_lands=int(gate.get("signed_green_lands") or 0),
        recent=list(gate.get("recent") or []),
    )


@tool(
    "deploy.run",
    effect="home",
    rate="6/h",
    timeout_s=600,
    examples=({"target": "hub"}, {"target": "bin", "plan": False}),
)
def deploy_run(a: RunIn) -> RunOut:
    """Deploy main (or a main commit) to ~/bin (target bin) or as the hub release (target hub). plan=true (default)
    changes nothing: hub = the plan (release, kit, CURRENT), bin = drift and the release that would go live. A hub
    release with changed protected paths returns approval_required (his ✅, then again). Code reaches main only
    through `assistant land`."""
    exe = _exe()
    if a.plan and a.target == "bin":
        rc, out, err = run(exe, ["drift"], timeout=30)
        if rc not in (0, 3):
            raise _fail(rc, out, err, "assistant drift")
        return RunOut(target="bin", done=False, lines=(out.strip() or "no drift").splitlines())
    args = ["deploy"] + (["--target", "hub"] if a.target == "hub" else []) + (["--rev", a.rev] if a.rev else [])
    if a.plan:
        args.append("--plan")
    if a.target == "hub":
        args += ["--wait-calls", str(CALL_WAIT_MIN)]
    rc, out, err = run(exe, args, timeout=580)
    if rc != 0:
        raise _fail(rc, out, err, "assistant " + " ".join(args))
    return RunOut(target=a.target, done=not a.plan, lines=(out + err).strip().splitlines()[-20:])
