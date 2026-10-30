"""Cat supplies: typed tools over `cat-autopilot` (~/bin/cat-autopilot: read-only on HA, never orders): the forecast
(when litter, liners, fountain filters, feeder desiccant, batteries and food run out), the stock at home, and his
stock answers / changes / orders as ledger writes (his own state: effect home).

Boundaries: no tool orders, adds to a cart or presses an HA button (the vendor reset buttons stay off ha's allowlist:
he presses them). Baskets, the ask message and cat:… taps stay Telegram flows (skill cat-supplies); an order goes
through skill shopping's ✅-per-order flow, then cat.ordered records it. Env: TOOLS_CAT_AUTOPILOT_BIN.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from tools.clirun import bin_path, first_line, run, run_json
from tools.core import REGISTRY, Domain, Format, ToolError, ToolInput, ToolOutput, tool

NOTES = (
    "`cat-autopilot` (CLI, managed; skill cat-supplies): plus the Telegram side that is not a tool: `card` (the "
    "basket card for the cat-autopilot note), `ask` (stock questions with buttons), `tap DATA` (cat:… buttons; "
    "`--apply` puts a basket on the shopping list), `--cron` (forecast refresh + one note when a basket is due). "
    "Orders: skill shopping (his ✅ per order), then `cat_ordered`."
)

REGISTRY.add_domain(
    Domain(
        name="cat",
        title="Cat supplies",
        summary="When the cat's supplies run out (forecast per item), the stock at home, and his stock answers,"
        " changes and orders. Never orders anything.",
        feature="gateway.cat",
        notes=NOTES,
    )
)

StockKey = Literal[
    "litter_kg", "dry_food_kg", "drawer_bags", "fountain_filters", "desiccant_packs", "feeder_batteries", "wet_portions"
]
Changed = Literal["filter", "desiccant", "litter", "batteries"]
WHEN_RE = r"^\d{4}-\d\d-\d\d \d\d:\d\d$"


def _not_blank(v: str) -> str:
    if not v.strip():
        raise ValueError("empty")
    return v


FreeText = Annotated[str, AfterValidator(_not_blank)]  # passed after `--`: may start with '-'


def _bin() -> str:
    return bin_path("TOOLS_CAT_AUTOPILOT_BIN", "cat-autopilot")


def _fail(rc: int, out: str, err: str, what: str) -> ToolError:
    msg = first_line(err, out, default=f"{what}: exit {rc}").removeprefix("cat-autopilot: ")
    if rc == 1 and "HA unreachable" in msg:
        return ToolError("unavailable", msg, "Home Assistant is unreachable: retry later")
    if rc == 2:
        return ToolError("invalid_args", msg, "`tool show` lists the fields")
    return ToolError("failed", msg, f"run `{what}` by hand to see why")


def cat_json(argv: list[str], what: str, timeout: float = 60.0) -> Any:
    return run_json(_bin(), argv, what, timeout=timeout, fail=_fail)[1]


def cat_text(argv: list[str], what: str, timeout: float = 60.0) -> str:
    rc, out, err = run(_bin(), argv, timeout=timeout)
    if rc != 0:
        raise _fail(rc, out, err, what)
    return out


# ------------------------------------------------------------------ models


class EmptyIn(ToolInput):
    pass


class StockSetIn(ToolInput):
    item: StockKey
    qty: float = Field(ge=0, le=1000, description="the amount in the item's unit (kg, bags, filters, packs ...)")
    mode: Literal["set", "add"] = Field("set", description="set = he has exactly this now; add = this many arrived")


class AnswerIn(ToolInput):
    text: FreeText = Field(
        min_length=1, max_length=300, description="his words, e.g. 'litter 12 kg, 4 filters, no liners'"
    )


class ChangedIn(ToolInput):
    kind: Changed
    at: str | None = Field(default=None, pattern=WHEN_RE, description="YYYY-MM-DD HH:MM his time (default now)")


class OrderedIn(ToolInput):
    shop: str = Field(pattern=r"^[a-z][a-z0-9_]{1,30}$", description="the shop key of the basket (galaxus, brack ...)")
    eta: str | None = Field(
        default=None, pattern=r"^\d{4}-\d\d-\d\d$", description="delivery date (default: shop's days)"
    )


class Item(BaseModel):
    model_config = ConfigDict(extra="ignore")

    key: str
    label: str | None = None
    unit: str | None = None
    stock: float | None = None
    pending: float | None = None
    due: str | None = Field(default=None, description="when the device needs it")
    runs_out: str | None = Field(default=None, description="when the stock at home runs out")
    days_left: float | None = None
    lead_days: float | None = None
    status: str | None = Field(default=None, description="now | order | soon | ok | ask (stock unknown)")
    confidence: str | None = None
    why: list[str] | None = None
    rate: str | None = None
    note: str | None = None


class ForecastOut(ToolOutput):
    today: str | None = None
    items: list[Item]

    def data(self, fmt: Format = "concise") -> dict[str, Any]:
        keep = None if fmt == "detailed" else {"why", "rate", "note", "lead_days", "confidence"}
        items = [i.model_dump(exclude_none=True, exclude=keep) for i in self.items]
        return {"today": self.today, "items": items}

    def render(self, fmt: Format = "concise") -> str:
        out = []
        for i in self.items:
            stock = "?" if i.stock is None else f"{i.stock:g}"
            out.append(
                f"{i.label or i.key}: {i.status} · stock {stock} {i.unit or ''} · runs out {i.runs_out or '-'}"
                + (f" ({i.days_left:g} d)" if i.days_left is not None else "")
            )
        return "\n".join(out) or "no items"


class StockOut(ToolOutput):
    stock: dict[str, dict[str, Any]] = Field(description="key -> qty, exact (false = about), pending (ordered)")

    def render(self, fmt: Format = "concise") -> str:
        if not self.stock:
            return "no stock answers yet"
        return "\n".join(
            f"{k}: {v.get('qty', 0):g}"
            + ("" if v.get("exact", True) else " (about)")
            + (f" +{v['pending']:g} ordered" if v.get("pending") else "")
            for k, v in sorted(self.stock.items())
        )


class AnswerOut(ToolOutput):
    set: dict[str, Any] = Field(description="what was stored per stock key ('x' = not used)")

    def render(self, fmt: Format = "concise") -> str:
        return "stock: " + ", ".join(f"{k} {'not used' if v == 'x' else v}" for k, v in self.set.items())


class DoneOut(ToolOutput):
    result: str = Field(description="the CLI's one-line answer")

    def render(self, fmt: Format = "concise") -> str:
        return self.result


# ------------------------------------------------------------------ tools


@tool("cat.forecast", effect="read", rate="30/min", idempotent=True, timeout_s=60, examples=({},))
def forecast(a: EmptyIn) -> ForecastOut:
    """When each cat supply runs out: device due date, stock run-out date, days left and status.
    Status now / order / soon / ok / ask (= stock unknown). For "how much litter is left", "when do I need new
    filters", "cat supplies"."""
    d = cat_json(["forecast", "--json"], "cat-autopilot forecast")
    items = [Item.model_validate(i) for i in (d.get("items") or []) if isinstance(i, dict)]
    return ForecastOut(today=d.get("today"), items=items)


@tool("cat.stock", effect="read", rate="30/min", idempotent=True, examples=({},))
def stock(a: EmptyIn) -> StockOut:
    """The stock at home per item (his answers minus what HA saw used, plus ordered packs after their ETA)."""
    d = cat_json(["stock", "show", "--json"], "cat-autopilot stock")
    return StockOut(stock={k: v for k, v in (d or {}).items() if isinstance(v, dict)})


@tool(
    "cat.stock_set",
    effect="home",
    rate="30/h",
    idempotent=False,
    examples=({"item": "fountain_filters", "qty": 4}, {"item": "litter_kg", "qty": 10, "mode": "add"}),
)
def stock_set(a: StockSetIn) -> StockOut:
    """Record his stock for one item: set = he has exactly this now ("I have 4 filters"); add = this many arrived."""
    d = cat_json(["stock", a.mode, a.item, f"{a.qty:g}", "--json"], f"cat-autopilot stock {a.mode}")
    return StockOut(stock={k: v for k, v in (d or {}).items() if isinstance(v, dict)})


@tool(
    "cat.answer",
    effect="home",
    rate="30/h",
    examples=({"text": "litter 12 kg, 4 filters, no liners"},),
)
def answer(a: AnswerIn) -> AnswerOut:
    """Store his free-text stock answer ("litter 12 kg, 4 filters, no liners"); invalid_args = no stock in it."""
    d = cat_json(["answer", "--json", "--", a.text], "cat-autopilot answer")
    return AnswerOut(set=d.get("set") or {})


@tool(
    "cat.changed",
    effect="home",
    rate="20/h",
    examples=({"kind": "filter"}, {"kind": "desiccant", "at": "2026-10-21 18:30"}),
)
def changed(a: ChangedIn) -> DoneOut:
    """He changed the fountain filter, feeder desiccant, litter or feeder batteries: stock −1.
    A change HA also saw within 48 h counts once. Remind him to reset the counter in the vendor app (not an HA
    tool)."""
    argv = ["changed", a.kind] + (["--at", a.at] if a.at else [])
    return DoneOut(result=cat_text(argv, "cat-autopilot changed"))


@tool(
    "cat.ordered",
    effect="home",
    rate="10/h",
    examples=({"shop": "galaxus"}, {"shop": "brack", "eta": "2026-10-27"}),
)
def ordered(a: OrderedIn) -> DoneOut:
    """After an order went through (skill shopping, his ✅): that shop's basket packs count as stock from the ETA."""
    argv = ["ordered", a.shop] + (["--eta", a.eta] if a.eta else [])
    return DoneOut(result=cat_text(argv, "cat-autopilot ordered"))
