"""llm_prices: Anthropic first-party list prices (USD per million tokens) and a cost function for transcript usage.

Source: platform.claude.com/docs/en/about-claude/pricing (checked against the docs when the table was last edited;
`PRICES_CHECKED` says when). Cache writes are multiples of the base input price: 5-minute TTL 1.25x, 1-hour TTL 2x.
Cache reads have their own column (0.05x on Opus/Sonnet 5.5, 0.1x on most others).
Modifiers: fast mode (usage.speed == "fast") = 2x every category on the models that offer it; US-only inference
(usage.inference_geo == "us") = 1.1x; Haiku 5.5 prompts over 100k tokens use its higher tier.
cost() never raises; an unknown model is priced as DEFAULT_MODEL and flagged (known=False).
"""

PRICES_CHECKED = "2026-10-21"
DEFAULT_MODEL = "claude-opus-5-5"

# model prefix -> (input, output, cache_read[, long-prompt tier (threshold, input, output, cache_read)])
PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-opus-5": (5.00, 25.00, 0.50),
    "claude-opus-4-8": (5.00, 25.00, 0.50),
    "claude-opus-4-7": (5.00, 25.00, 0.50),
    "claude-opus-4-6": (5.00, 25.00, 0.50),
    "claude-opus-4-5": (5.00, 25.00, 0.50),
    "claude-opus-4-1": (15.00, 75.00, 1.50),
    "claude-opus-4": (15.00, 75.00, 1.50),
    "claude-sonnet-5-5": (2.00, 10.00, 0.10),
    "claude-sonnet-5": (2.00, 10.00, 0.20),
    "claude-sonnet-4-6": (3.00, 15.00, 0.30),
    "claude-sonnet-4-5": (3.00, 15.00, 0.30),
    "claude-sonnet-4": (3.00, 15.00, 0.30),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, (100_000, 0.50, 2.50, 0.05)),
    "claude-haiku-4-5": (1.00, 5.00, 0.10),
    "claude-fable-5-1": (10.00, 50.00, 0.25),
    "claude-fable-5": (10.00, 50.00, 1.00),
    "claude-mythos-5-1": (10.00, 50.00, 0.25),
    "claude-mythos-5": (10.00, 50.00, 1.00),
}
FAST_MULT = 2.0      # fast mode: Opus 5.5 $8/$40, Opus 5 and 4.8 $10/$50 = 2x the standard rates
US_GEO_MULT = 1.1
WRITE_5M, WRITE_1H = 1.25, 2.0


def lookup(model):
    """(row, known) for a model id; date-suffixed ids (claude-haiku-4-5-20251001) match their prefix."""
    m = (model or "").lower()
    if m in PRICES:
        return PRICES[m], True
    for k in sorted(PRICES, key=len, reverse=True):
        if m.startswith(k + "-") or m.startswith(k + "@") or m == k:
            return PRICES[k], True
    return PRICES[DEFAULT_MODEL], False


def cost(model, inp=0, out=0, cache_read=0, cache_w5=0, cache_w1h=0, speed=None, geo=None):
    """USD for one API call's usage (API-equivalent list price). Never raises."""
    try:
        row, _ = lookup(model)
        pin, pout, pcr = row[0], row[1], row[2]
        if len(row) > 3:
            thr, lin, lout, lcr = row[3]
            if (inp or 0) + (cache_read or 0) + (cache_w5 or 0) + (cache_w1h or 0) > thr:
                pin, pout, pcr = lin, lout, lcr
        usd = ((inp or 0) * pin + (out or 0) * pout + (cache_read or 0) * pcr
               + (cache_w5 or 0) * pin * WRITE_5M + (cache_w1h or 0) * pin * WRITE_1H) / 1e6
        if (speed or "") == "fast":
            usd *= FAST_MULT
        if (geo or "").lower() in ("us", "us-only", "us_only"):
            usd *= US_GEO_MULT
        return usd
    except Exception:
        return 0.0


def usage_cost(model, usage):
    """(usd, tokens dict) from a Messages API usage object as Claude Code writes it into transcripts."""
    u = usage if isinstance(usage, dict) else {}

    def n(k):
        try:
            return int(u.get(k) or 0)
        except (TypeError, ValueError):
            return 0
    cw = n("cache_creation_input_tokens")
    cc = u.get("cache_creation") if isinstance(u.get("cache_creation"), dict) else {}
    w1h = int(cc.get("ephemeral_1h_input_tokens") or 0) if cc else 0
    w5 = int(cc.get("ephemeral_5m_input_tokens") or 0) if cc else cw
    if w1h + w5 != cw:
        w5 = max(0, cw - w1h)
    t = {"input": n("input_tokens"), "output": n("output_tokens"), "cache_read": n("cache_read_input_tokens"),
         "cache_w5": w5, "cache_w1h": w1h}
    return cost(model, t["input"], t["output"], t["cache_read"], w5, w1h, u.get("speed"), u.get("inference_geo")), t
