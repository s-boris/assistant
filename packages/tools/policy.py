"""The gateway's Cedar policy: schema from the registry, policy.cedar, decisions with reasons.

schema_text(reg)    the Cedar schema: namespace Hub; principal types Brain, Subagent, Binding, Reflex, Ci; one action
                    per typed tool (its registry name); resource Hub::Tool with domain, effect, labels, approval,
                    destructive (Tool in Hub::Domain); context approved / tainted / attributed
Engine(reg)         parses schema, policy and the tool entities once (cedarpy pre-parsed objects, ~35 µs a decision)
Engine.validate()   Cedar's validator over policy.cedar against the schema: [] = valid
Engine.decide(principal, spec, approved=, tainted=)   -> Decision(allowed, policies, reason, hint, approval_helps,
                    approval_needed): approval_helps = a token would turn this deny into an allow; approval_needed =
                    this allow holds only because a token came (the executor must consume it)
Engine.decide_api(principal, op)   -> Decision for one call of the hub's approval API: resource Hub::Api
                    "approvals", action "hubapi:approvals.<op>" (API_OPS). hubd evaluates it log-only next to its
                    route table, which keeps enforcing the callers: the brain requests, consumes and cancels; the
                    hub's Telegram adapter (Binding) records his tap (decide, card, expire); both read and note
                    which message carries a card (attach).
The policy never sees arguments or results: only who calls, which tool (effect, labels, approval) and the labels
already read in the caller's context. Stamps: stamp_mac() is the HMAC the PreToolUse hook (hooks/hub-stamp.py)
puts on mcp__hub__* calls so the hub can tell the main session from a subagent; keep both in step.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tools.core import EFFECTS, LABELS, Registry, Spec

NS = "Hub"
PRINCIPALS = ("brain", "subagent", "binding", "reflex", "ci", "voice", "phone")
ENTITY = {
    "brain": "Brain",
    "subagent": "Subagent",
    "binding": "Binding",
    "reflex": "Reflex",
    "ci": "Ci",
    "voice": "Voice",
    "phone": "Phone",
}
# One fixed entity per principal type: policies test the type only (`principal is Hub::Subagent`); which subagent
# called (agent id and type) goes to the decision log, never into the policy.
PRINCIPAL_ID = {
    "brain": "main",
    "subagent": "agent",
    "binding": "tg",
    "reflex": "rule",
    "ci": "ci",
    "voice": "relay",
    "phone": "gateway",
}
POLICY_FILE = Path(__file__).with_name("policy.cedar")
# The hub's approval API as Cedar actions: "hubapi:" keeps them apart from the typed tools' registry names.
API = "approvals"
API_OPS = ("request", "consume", "cancel", "decide", "card", "expire", "attach", "read")


def api_action(op: str) -> str:
    return f"hubapi:{API}.{op}"


STAMP_VERSION = "hub-stamp/1"
STAMP_TTL = 300  # seconds a stamp stays valid (the hook signs right before Claude Code sends the call)
ANNOT = re.compile(r'@(id|reason)\("((?:[^"\\]|\\.)*)"\)')
HINTS = {
    "owner-approval": "ask him first: `approvals new --action tool:{tool} --summary …`, then pass the nonce as "
    "_approval",
    "spend-irreversible": "ask him first: `approvals new --action tool:{tool} --summary …`, then pass the nonce as "
    "_approval",
    "label-flow": "this context read private data: ask him (`approvals new --action tool:{tool} …`) and pass the nonce "
    "as _approval, or do it from a context that did not read private data",
    "default": "a {principal} can't run {effect} tools: hand the step to the main session, or ask him",
}


@dataclass(frozen=True)
class Principal:
    kind: str  # one of PRINCIPALS
    agent_id: str = ""
    agent_type: str = ""
    session: str = ""
    attributed: bool = True

    @property
    def uid(self) -> str:
        return f'{NS}::{ENTITY[self.kind]}::"{PRINCIPAL_ID[self.kind]}"'

    @property
    def label(self) -> str:
        """For logs and reasons: brain, subagent:Explore, subagent:unattributed, binding ..."""
        if self.kind == "subagent":
            return f"subagent:{self.agent_type or ('unattributed' if not self.attributed else 'agent')}"
        return self.kind

    def taint_key(self) -> str:
        """Whose context a result lands in: the Claude Code session + the agent (main = the session's own)."""
        if self.kind in ("brain", "subagent") and self.session:
            return f"{self.session}:{self.agent_id or 'main'}"
        if self.kind == "subagent":
            return f"unattributed:{self.agent_id or '-'}"
        return self.kind


@dataclass(frozen=True)
class Decision:
    allowed: bool
    policies: tuple[str, ...]
    reason: str
    hint: str = ""
    approval_helps: bool = False
    approval_needed: bool = False
    errors: int = 0
    us: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "policies": list(self.policies),
            "reason": self.reason,
            "hint": self.hint,
            "approval_helps": self.approval_helps,
            "approval_needed": self.approval_needed,
            "errors": self.errors,
            "us": self.us,
        }


def _q(s: str) -> str:
    return json.dumps(s)


def schema_text(reg: Registry) -> str:
    """The Cedar schema (human format) for every typed tool of REG."""
    kinds = ", ".join(ENTITY[k] for k in PRINCIPALS)
    lines = [
        f"namespace {NS} {{",
        "  type CallContext = { approved: Bool, tainted: Set<String>, attributed: Bool };",
        *(f"  entity {ENTITY[k]};" for k in PRINCIPALS),
        "  entity Domain;",
        "  entity Tool in [Domain] = { domain: String, effect: String, labels: Set<String>, approval: String,"
        " destructive: Bool };",
        "  entity Api;",
    ]
    for op in API_OPS:
        lines.append(
            f"  action {_q(api_action(op))} appliesTo"
            f" {{ principal: [{kinds}], resource: [Api], context: CallContext }};"
        )
    for name in sorted(reg.typed):
        lines.append(
            f"  action {_q(name)} appliesTo {{ principal: [{kinds}], resource: [Tool], context: CallContext }};"
        )
    lines.append("}")
    return "\n".join(lines) + "\n"


def entities(reg: Registry) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [
        {"uid": {"type": f"{NS}::{ENTITY[k]}", "id": PRINCIPAL_ID[k]}, "attrs": {}, "parents": []} for k in PRINCIPALS
    ]
    out.append({"uid": {"type": f"{NS}::Api", "id": API}, "attrs": {}, "parents": []})
    for d in sorted({s.domain for s in reg.typed.values()}):
        out.append({"uid": {"type": f"{NS}::Domain", "id": d}, "attrs": {}, "parents": []})
    for s in reg.typed.values():
        out.append(
            {
                "uid": {"type": f"{NS}::Tool", "id": s.name},
                "attrs": {
                    "domain": s.domain,
                    "effect": s.effect,
                    "labels": list(s.labels_out),
                    "approval": s.approval,
                    "destructive": s.destructive,
                },
                "parents": [{"type": f"{NS}::Domain", "id": s.domain}],
            }
        )
    return out


def policy_text(path: Path | None = None) -> str:
    return (path or POLICY_FILE).read_text(encoding="utf-8")


def policy_reasons(text: str) -> dict[str, str]:
    """{@id: @reason} for every policy in TEXT (the reason a deny by that policy shows)."""
    out: dict[str, str] = {}
    cur_id, cur_reason = None, ""
    for line in text.splitlines():
        s = line.strip()
        for kind, val in ANNOT.findall(s):
            if kind == "id":
                cur_id = val
            else:
                cur_reason = val
        if s.startswith(("permit", "forbid")):
            if cur_id:
                out[cur_id] = cur_reason
            cur_id, cur_reason = None, ""
    return out


def policy_effects(text: str) -> dict[str, str]:
    """{@id: "permit" | "forbid"}."""
    out: dict[str, str] = {}
    cur = None
    for line in text.splitlines():
        s = line.strip()
        m = re.match(r'@id\("([^"]+)"\)', s)
        if m:
            cur = m[1]
        elif s.startswith(("permit", "forbid")) and cur:
            out[cur] = s.split()[0].split("(")[0]
            cur = None
    return out


class Engine:
    def __init__(self, reg: Registry, policy: str | None = None) -> None:
        import cedarpy  # pyright: ignore[reportMissingImports]  (vendored in the hub release; dev dep here)

        self.cedar: Any = cedarpy
        self.reg = reg
        self.policy = policy if policy is not None else policy_text()
        self.schema = schema_text(reg)
        self.reasons = policy_reasons(self.policy)
        self.kinds = policy_effects(self.policy)
        self.ps = cedarpy.PolicySet.from_str(self.policy)
        self.sc = cedarpy.Schema.from_str(self.schema)
        self.ents = cedarpy.Entities.from_json_str(json.dumps(entities(reg)), self.sc)
        self.sha = hashlib.sha256(self.policy.encode()).hexdigest()[:12]

    def validate(self) -> list[str]:
        r = self.cedar.validate_policies(self.policy, self.schema)
        errs = [str(getattr(e, "error", e)) for e in (r.errors or [])]
        missing = [i for i in self.reasons if i not in self.kinds]
        return errs + [f"policy {i}: no permit/forbid follows its @id" for i in missing]

    def _eval(self, p: Principal, spec: Spec, approved: bool, tainted: frozenset[str]) -> tuple[bool, list[str], int]:
        return self._ask(p, f"{NS}::Action::{_q(spec.name)}", f"{NS}::Tool::{_q(spec.name)}", approved, tainted)

    def _ask(
        self, p: Principal, action: str, resource: str, approved: bool, tainted: frozenset[str]
    ) -> tuple[bool, list[str], int]:
        req = {
            "principal": p.uid,
            "action": action,
            "resource": resource,
            "context": {"approved": approved, "tainted": sorted(tainted), "attributed": p.attributed},
        }
        r = self.cedar.is_authorized(req, self.ps, self.ents, schema=self.sc)
        d = r.diagnostics
        by = d.id_annotations_by_reason or {}
        ids = sorted({by.get(x, x) for x in (d.reasons or [])})
        return bool(r.allowed), ids, len(d.errors or [])

    def decide(
        self, p: Principal, spec: Spec, *, approved: bool = False, tainted: frozenset[str] | set[str] = frozenset()
    ) -> Decision:
        t0 = time.perf_counter()
        taint = frozenset(x for x in tainted if x in LABELS)
        if p.kind not in PRINCIPALS or spec.effect not in EFFECTS:
            return Decision(False, (), "unknown principal or effect", errors=1)
        ok, ids, errs = self._eval(p, spec, approved, taint)
        helps = needed = False
        if ok and approved:
            needed = not self._eval(p, spec, False, taint)[0]
        elif not ok and not approved:
            helps = self._eval(p, spec, True, taint)[0]
        us = int((time.perf_counter() - t0) * 1e6)
        if ok:
            return Decision(True, tuple(ids), "", approval_needed=needed, errors=errs, us=us)
        forbids = [i for i in ids if self.kinds.get(i) == "forbid"]
        key = forbids[0] if forbids else "default"
        if forbids:
            reason = self.reasons.get(key) or key
        else:
            reason = f"no policy lets a {p.kind} run {spec.effect} tools"
        if errs and not forbids:
            reason += f" ({errs} policy error{'s' if errs > 1 else ''})"
        hint = HINTS.get(key, HINTS["default"]).format(tool=spec.name, principal=p.kind, effect=spec.effect)
        return Decision(False, tuple(forbids), reason, hint, approval_helps=helps, errors=errs, us=us)

    def decide_api(self, p: Principal, op: str) -> Decision:
        """One call of the hub's approval API: who may do OP. No token, no taint: the API never runs a tool."""
        t0 = time.perf_counter()
        if p.kind not in PRINCIPALS or op not in API_OPS:
            return Decision(False, (), "unknown principal or approval action", errors=1)
        ok, ids, errs = self._ask(p, f"{NS}::Action::{_q(api_action(op))}", f"{NS}::Api::{_q(API)}", False, frozenset())
        us = int((time.perf_counter() - t0) * 1e6)
        if ok:
            return Decision(True, tuple(ids), "", errors=errs, us=us)
        forbids = [i for i in ids if self.kinds.get(i) == "forbid"]
        if forbids:
            reason = self.reasons.get(forbids[0]) or forbids[0]
        else:
            reason = f"no policy lets a {p.kind} {op} approvals"
        if errs and not forbids:
            reason += f" ({errs} policy error{'s' if errs > 1 else ''})"
        return Decision(False, tuple(forbids), reason, errors=errs, us=us)


def stamp_mac(key: bytes, p: str, agent: str, agent_type: str, session: str, ts: int, tool: str) -> str:
    """The stamp's HMAC (hex, 32 chars) over the principal, agent, session, time and MCP tool name."""
    msg = "|".join((STAMP_VERSION, p, agent, agent_type, session, str(ts), tool)).encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()[:32]


def verify_stamp(stamp: Any, tool: str, key: bytes | None, now: float | None = None) -> Principal | None:
    """The principal a valid stamp names (brain, subagent or reflex), else None (missing, forged, stale, other tool)."""
    if not key or not isinstance(stamp, dict):
        return None
    p, a, t, s, ts, m = (stamp.get(k) for k in ("p", "a", "t", "s", "ts", "m"))
    a, t, s = a or "", t or "", s or ""
    if p not in ("brain", "subagent", "reflex") or not isinstance(m, str) or isinstance(ts, bool):
        return None
    if not isinstance(ts, int) or not all(isinstance(x, str) and len(x) <= 128 for x in (a, t, s)):
        return None
    if abs((now if now is not None else time.time()) - ts) > STAMP_TTL:
        return None
    if not hmac.compare_digest(stamp_mac(key, p, a, t, s, ts, tool), m):
        return None
    if p == "subagent" and not a:
        return None
    return Principal(p, agent_id=a, agent_type=t, session=s)


__all__ = [
    "API_OPS",
    "ENTITY",
    "Decision",
    "Engine",
    "PRINCIPALS",
    "Principal",
    "api_action",
    "entities",
    "policy_text",
    "schema_text",
    "stamp_mac",
    "verify_stamp",
]
