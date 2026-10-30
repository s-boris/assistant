"""Cat supply tools against a stub cat-autopilot: forecast and stock parsing, the ledger writes' argv, free text after
`--`, typed errors; no tool orders, taps or touches HA buttons."""

from __future__ import annotations

from tools import gateway
from tools.core import load

FORECAST = {
    "today": "2026-10-22",
    "items": [
        {
            "key": "filter",
            "label": "Fountain filters",
            "unit": "filters",
            "stock": 0.0,
            "due": "2026-10-28",
            "runs_out": "2026-10-28",
            "days_left": 6,
            "lead_days": 4,
            "status": "soon",
            "confidence": "medium",
            "why": ["filter 33 %"],
            "icon": "💧",
        },
        {"key": "litter", "label": "Litter", "unit": "kg", "stock": None, "status": "ask"},
    ],
}
STOCK = {
    "fountain_filters": {"qty": 4.0, "exact": True, "pending": 0},
    "litter_kg": {"qty": 7.0, "exact": False, "pending": 5},
}


def call(name, **args):
    return gateway.call(name, args)


def test_forecast_and_stock(stubs):
    stubs.on("cat-autopilot", ["forecast"], FORECAST)
    stubs.on("cat-autopilot", ["stock", "show"], STOCK)
    f = call("cat.forecast")
    assert f.ok and f.data["items"][0]["runs_out"] == "2026-10-28" and "why" not in f.data["items"][0]
    assert f.text.splitlines() == [
        "Fountain filters: soon · stock 0 filters · runs out 2026-10-28 (6 d)",
        "Litter: ask · stock ? kg · runs out -",
    ]
    assert call("cat.forecast", format="detailed").data["items"][0]["why"] == ["filter 33 %"]
    s = call("cat.stock")
    assert s.text == "fountain_filters: 4\nlitter_kg: 7 (about) +5 ordered"
    assert stubs.calls("cat-autopilot") == [["forecast", "--json"], ["forecast", "--json"], ["stock", "show", "--json"]]


def test_writes(stubs):
    stubs.on("cat-autopilot", ["stock", "set"], STOCK)
    stubs.on("cat-autopilot", ["stock", "add"], STOCK)
    stubs.on("cat-autopilot", ["answer"], {"ok": True, "set": {"litter_kg": 12, "drawer_bags": "x"}})
    stubs.on("cat-autopilot", ["changed"], "filter: logged · 3 left\n")
    stubs.on("cat-autopilot", ["ordered"], "ordered at galaxus: counts as stock from Mon 27.10\n")
    assert call("cat.stock_set", item="fountain_filters", qty=4).ok
    assert call("cat.stock_set", item="litter_kg", qty=2.5, mode="add").ok
    a = call("cat.answer", text="- litter 12 kg, no liners")
    assert a.ok and a.text == "stock: litter_kg 12, drawer_bags not used"
    assert call("cat.changed", kind="filter").text == "filter: logged · 3 left"
    assert call("cat.changed", kind="desiccant", at="2026-10-21 18:30").ok
    assert call("cat.ordered", shop="galaxus", eta="2026-10-27").text.startswith("ordered at galaxus")
    assert stubs.calls("cat-autopilot") == [
        ["stock", "set", "fountain_filters", "4", "--json"],
        ["stock", "add", "litter_kg", "2.5", "--json"],
        ["answer", "--json", "--", "- litter 12 kg, no liners"],
        ["changed", "filter"],
        ["changed", "desiccant", "--at", "2026-10-21 18:30"],
        ["ordered", "galaxus", "--eta", "2026-10-27"],
    ]


def test_refusals_and_errors(stubs):
    for name, args in (
        ("cat.stock_set", {"item": "catnip", "qty": 1}),
        ("cat.stock_set", {"item": "litter_kg", "qty": -1}),
        ("cat.changed", {"kind": "fountain"}),
        ("cat.changed", {"kind": "filter", "at": "yesterday"}),
        ("cat.ordered", {"shop": "--apply"}),
        ("cat.answer", {"text": "   "}),
    ):
        assert call(name, **args).error["code"] == "invalid_args", name
    assert stubs.calls("cat-autopilot") == []
    stubs.on("cat-autopilot", ["forecast"], "", rc=1, err="cat-autopilot: HA unreachable (timeout)\n")
    stubs.on("cat-autopilot", ["answer"], "", rc=2, err="cat-autopilot: no stock found in the answer\n")
    assert call("cat.forecast").error["code"] == "unavailable"
    assert call("cat.answer", text="hello").error["code"] == "invalid_args"
    reg = load()
    assert {s.effect for s in reg.in_domain("cat")} == {"read", "home"}
    assert not [s.name for s in reg.in_domain("cat") if any(w in s.name for w in ("tap", "card", "order_", "reset"))]
