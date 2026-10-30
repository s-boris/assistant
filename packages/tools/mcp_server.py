"""The FastMCP server `hub`: every typed tool of the registry, run through the gateway (stdio; the hub serves the
same tools over streamable HTTP with its policy in front, tools.hubgw). Reserved arguments `_caller` (the hub's
PreToolUse stamp) and `_approval` (an approval nonce) are taken off before validation. Input and output schemas
come from the Pydantic models; the gateway
validates, so the CLI and MCP answer alike. Nothing here may print to stdout (the stdio transport owns it).
"""

from __future__ import annotations

from typing import Any

import anyio
from fastmcp import FastMCP
from fastmcp.tools.base import Tool, ToolResult
from mcp.types import ToolAnnotations
from pydantic import PrivateAttr

from tools import gateway
from tools.core import Registry, Spec, load

VERSION = "0.1.0"
INSTRUCTIONS = (
    "The assistant's typed tools (one gateway: validation, rate, approval, usage + trace per call). "
    "Errors come back as 'code: message (hint: …)'. Use format=detailed only when the short answer is not enough."
)


def request_meta() -> dict[str, Any]:
    """The current MCP request's _meta (traceparent lives there), {} outside a request."""
    try:
        from fastmcp.server.dependencies import fastmcp_request_ctx

        rc = fastmcp_request_ctx.get()
        m = getattr(rc, "meta", None)
        if m is None:
            return {}
        if isinstance(m, dict):
            return m
        return m.model_dump() if hasattr(m, "model_dump") else dict(m)
    except Exception:
        return {}


class RegistryTool(Tool):
    """A registry tool served over MCP."""

    _spec_name: str = PrivateAttr(default="")

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        meta = request_meta()
        name = self._spec_name
        args = dict(arguments or {})
        args.pop("_caller", None)  # the hub's PreToolUse stamp: only the hub gateway reads it
        nonce = args.pop("_approval", None)
        approval = nonce if isinstance(nonce, str) else None
        res = await anyio.to_thread.run_sync(
            lambda: gateway.call(name, args, surface="mcp", meta=meta, approval=approval)
        )
        if res.ok:
            return ToolResult(content=res.text or "ok", structured_content=res.data)
        return ToolResult(content=res.error_text(), is_error=True)


def slim(schema: Any) -> Any:
    """A JSON schema without Pydantic's cosmetic titles (they cost context on every turn)."""
    if isinstance(schema, list):
        return [slim(x) for x in schema]
    if not isinstance(schema, dict):
        return schema
    out = {}
    for k, v in schema.items():
        if k == "title":
            continue
        if k in ("properties", "$defs") and isinstance(v, dict):
            out[k] = {name: slim(sub) for name, sub in v.items()}  # a field may be called "title": keep the key
        else:
            out[k] = slim(v)
    return out


def to_mcp(spec: Spec, cls: type[RegistryTool] = RegistryTool) -> RegistryTool:
    t = cls(
        name=spec.mcp_name,
        title=spec.summary,
        description=spec.doc,
        parameters=slim(spec.input.model_json_schema()),
        output_schema=slim(spec.output.model_json_schema()),
        annotations=ToolAnnotations(
            title=spec.summary,
            read_only_hint=spec.effect == "read",
            destructive_hint=spec.destructive,
            idempotent_hint=spec.idempotent,
            open_world_hint=spec.effect in ("send_external", "spend"),
        ),
        timeout=spec.timeout_s + 15,
    )
    t._spec_name = spec.name
    return t


def build(reg: Registry | None = None) -> FastMCP:
    reg = reg or load()
    mcp = FastMCP("hub", instructions=INSTRUCTIONS, version=VERSION)
    switched = gateway.direct_domains()  # domains in direct (CLI) mode stay out of the server (direct switch)
    for spec in reg.typed.values():
        if not gateway.is_direct(spec.domain, switched):
            mcp.add_tool(to_mcp(spec))
    return mcp


def serve() -> int:
    gateway.drop_inherited_trace()  # long-lived: per-call trace context comes from each request's _meta only
    build().run(transport="stdio", show_banner=False)
    return 0
