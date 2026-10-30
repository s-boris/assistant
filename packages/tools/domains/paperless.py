"""Paperless: typed tools over `vault paperless` (~/bin/paperless execs it): cards, tier-1 fields, search, recent.

Fields only. Document content, tier-2 fields (IBAN, references, numbers, addresses, dates in the text) and
downloads need his ✅ per document in the vault bot: not tools (skill paperless / vault). Uploads and metadata
writes stay on the CLI (skill paperless). The vault domain's guard() checks every argv. Env: TOOLS_VAULT_BIN.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from tools.core import REGISTRY, Domain, Format, ToolInput, ToolOutput, tool
from tools.domains.vault import Text, vault_json

NOTES = (
    "`paperless` = `vault paperless` (skill paperless; managed): upload, set, rules by hand. Not tools on "
    "purpose: content (`get … ` with the content flag), tier-2 fields with a reason, download: each needs his ✅ "
    "per document in the vault bot. Bills use the tier-1 fields only; never ask him for IBAN or reference."
)

REGISTRY.add_domain(
    Domain(
        name="paperless",
        title="Paperless",
        summary="His paperless-ngx documents through the vault: cards (metadata, amount, due date), tier-1 bill"
        " fields, search and recent. Never content.",
        feature="gateway.paperless",
        notes=NOTES,
    )
)

# the vault's free tier (sources.TIER1_FIELDS); tier 2 is not offered here at all
Tier1 = Literal[
    "title",
    "document_date",
    "correspondent",
    "document_type",
    "amount",
    "currency",
    "due_date",
    "invoice_date",
    "qr_bill",
    "payee",
    "has_payment_ref",
    "period_years",
]


class IdIn(ToolInput):
    id: int = Field(ge=1, le=10_000_000, description="the paperless document id (#N)")


class FieldsIn(IdIn):
    fields: list[Tier1] | None = Field(
        None, min_length=1, max_length=12, description="tier-1 fields (default: the bill set)"
    )


class SearchIn(ToolInput):
    query: Text = Field(min_length=1, max_length=200, description="paperless full-text query")
    n: int = Field(5, ge=1, le=50)


class RecentIn(ToolInput):
    n: int = Field(5, ge=1, le=50)


class CardOut(ToolOutput):
    card: dict[str, Any] = Field(description="metadata, pages, amount, currency, due date: never content")

    def render(self, fmt: Format = "concise") -> str:
        c = self.card
        bits = [f"#{c.get('id')}", str(c.get("created") or ""), str(c.get("title") or "")]
        bits += [f"corr: {c.get('correspondent') or '-'}", f"type: {c.get('document_type') or '-'}"]
        if c.get("amount") is not None:
            bits.append(f"{c.get('amount')} {c.get('currency') or ''}".strip())
        if c.get("due_date"):
            bits.append(f"due {c['due_date']}")
        return " | ".join(b for b in bits if b)


class FieldsOut(ToolOutput):
    fields: dict[str, Any] = Field(description="tier-1 values; null = not found")
    withheld: list[str] = Field(default_factory=list, description="tier-2 names the vault held back")

    def render(self, fmt: Format = "concise") -> str:
        return ", ".join(f"{k}={v}" for k, v in self.fields.items())


class DocsOut(ToolOutput):
    count: int
    results: list[dict[str, Any]] = Field(description="id, created, title, correspondent, document_type, tags")

    def render(self, fmt: Format = "concise") -> str:
        lines = [f"{self.count} document(s)"]
        for d in self.results:
            lines.append(
                f"#{d.get('id')}  {d.get('created') or ''}  {d.get('title')}  | corr: {d.get('correspondent') or '-'}"
                f" | type: {d.get('document_type') or '-'}"
            )
        return "\n".join(lines)


@tool("paperless.get", effect="read", labels_out=("private",), rate="120/min", idempotent=True, examples=({"id": 42},))
def get(a: IdIn) -> CardOut:
    """A document's card: metadata, pages, amount, currency, due date. Never its content."""
    d = vault_json(["paperless", "get", str(a.id), "--json"], "paperless get", timeout=120)
    return CardOut(card=d if isinstance(d, dict) else {"value": d})


@tool(
    "paperless.fields",
    effect="read",
    labels_out=("private",),
    rate="120/min",
    idempotent=True,
    examples=({"id": 42}, {"id": 42, "fields": ["amount", "currency", "due_date"]}),
)
def fields(a: FieldsIn) -> FieldsOut:
    """Tier-1 fields of a document (title, dates, type, correspondent, amount, currency, due date, payee, QR bill).
    Tier 2 is never offered here: it needs his ✅ per document in the vault bot."""
    argv = ["paperless", "fields", str(a.id)] + (["--fields", ",".join(a.fields)] if a.fields else [])
    d = vault_json(argv, "paperless fields", timeout=120)
    if not isinstance(d, dict):
        d = {"value": d}
    withheld = [str(x) for x in d.pop("_withheld", []) or []]
    return FieldsOut(fields=d, withheld=withheld)


def _docs(d: Any) -> DocsOut:
    rows = d.get("results") if isinstance(d, dict) else d
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    n = d.get("count") if isinstance(d, dict) else None
    return DocsOut(count=n if isinstance(n, int) else len(rows), results=rows)


@tool("paperless.search", effect="read", labels_out=("private",), rate="60/min", examples=({"query": "Lohnausweis"},))
def search(a: SearchIn) -> DocsOut:
    """Find documents (metadata only): ids for paperless_get / paperless_fields."""
    return _docs(vault_json(["paperless", "search", a.query, "-n", str(a.n), "--json"], "paperless search"))


@tool("paperless.recent", effect="read", labels_out=("private",), rate="60/min", examples=({"n": 10},))
def recent(a: RecentIn) -> DocsOut:
    """The newest documents (metadata only)."""
    return _docs(vault_json(["paperless", "recent", "-n", str(a.n), "--json"], "paperless recent"))
