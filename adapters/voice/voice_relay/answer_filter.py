"""Spoken-answer filter. Everything sent back to the voice provider passes through here: identifiers are masked
with the same regex pass the vault/llm-local use (~/bin/llm-local regex_pass), plus street addresses and long
number runs, markdown is stripped and the answer is cut to a short spoken length."""
import importlib.machinery
import importlib.util
import os
import re
from dataclasses import dataclass, field

MAX_CHARS = 450
_SOURCE = os.environ.get("VOICE_FILTER_SOURCE", os.path.expanduser("~/bin/llm-local"))
_regex_pass = None

# Fallback (a subset of llm-local's rules) if llm-local can't be loaded.
_FALLBACK = [
    (re.compile(r"[\w.+%-]+@[\w-]+(?:\.[\w-]+)+"), "[email]", "email"),
    (re.compile(r"(?i)\b(?:https?://|www\.)[^\s<>\"')\]]+"), "[link]", "link"),
    (re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]){11,30}\b"), "[IBAN]", "iban"),
    (re.compile(r"(?<!\d)756[.\s]?\d{4}[.\s]?\d{4}[.\s]?\d{2}(?!\d)"), "[AHV number]", "ahv"),
    (re.compile(r"(?<![\w+])(?:\+|00)[1-9]\d{0,2}[ .\-/]?\d{1,4}(?:[ .\-/]?\d{2,4}){2,4}(?!\d)"), "[phone]", "phone"),
    (re.compile(r"(?<![\w.:/])0\d{2}[ .\-/]?\d{3}[ .\-/]?\d{2}[ .\-/]?\d{2}(?![\d:])"), "[phone]", "phone"),
    (re.compile(r"(?<![\d.])\d(?:[ -]?\d){12,18}(?!\d)"), "[card number]", "card"),
    (re.compile(r"(?i)\b(password|passwort|pin|code|token)\s*(?:[:=]|is|ist)?\s*\S+"), "[secret]", "secret"),
]
_URL = re.compile(r"(?i)\b(?:https?://|www\.)[^\s<>\"')\]]+")
_STREET = re.compile(r"\b[A-ZÄÖÜ][\wäöüß.-]*(?:strasse|straße|str\.|gasse|weg|platz|allee|ring|quai)\s+\d{1,4}[a-z]?\b")
_LONG_NUMBER = re.compile(r"(?<![\w.,:/])\d(?:[ '’]?\d){8,}(?![\w])")
_AMOUNT = re.compile(r"(?i)\b(?:chf|fr\.|eur|usd|€|\$)\s?\d[\d'’ ]*(?:[.,]\d{1,2}|\.[-–])?|\b\d[\d'’]*(?:[.,]\d{1,2})?\s?(?:chf|franken|fr\.|eur|euro|usd|dollars?)\b")
_FENCE = re.compile(r"```.*?```", re.S)
_MD = re.compile(r"[*_`#>|~]+")
_LINK_PH = re.compile(r"\[link(?::[^\]\n]*)?\]")
_PH = re.compile(r"@?\[(?:private:\s*)?([^\]\n]{1,40})\]")
_WS = re.compile(r"\s+")


def _load():
    global _regex_pass
    if _regex_pass is None:
        try:
            loader = importlib.machinery.SourceFileLoader("voice_llm_local", _SOURCE)
            spec = importlib.util.spec_from_loader("voice_llm_local", loader)
            mod = importlib.util.module_from_spec(spec)
            loader.exec_module(mod)
            _regex_pass = mod.regex_pass
        except Exception:  # missing or broken: the fallback rules still apply
            _regex_pass = False
    return _regex_pass


def engine():
    return "llm-local" if _load() else "fallback"


@dataclass
class Filtered:
    text: str
    masked: dict = field(default_factory=dict)
    truncated: bool = False


def _bump(counts, key, n=1):
    if n:
        counts[key] = counts.get(key, 0) + n


def mask_identifiers(text, mask_amounts=True):
    counts = {}
    text, n = _URL.subn("[link]", text)          # links are never spoken (llm-local keeps some)
    _bump(counts, "link", n)
    rp = _load()
    if rp:
        text, c = rp(text, money=True, all_amounts=False)
        for k, v in c.items():
            _bump(counts, k, v)
    else:
        for rx, rep, key in _FALLBACK:
            text, n = rx.subn(rep, text)
            _bump(counts, key, n)
    text, n = _STREET.subn("[address]", text)
    _bump(counts, "address", n)
    text, n = _LONG_NUMBER.subn("[number]", text)
    _bump(counts, "number", n)
    if mask_amounts:
        text, n = _AMOUNT.subn("[amount]", text)
        _bump(counts, "amount", n)
    return text, counts


def spoken(text, max_chars=MAX_CHARS):
    text = _FENCE.sub(" ", text)
    text = _LINK_PH.sub("(link on Telegram)", text)
    text = _PH.sub(lambda m: "(%s withheld)" % m.group(1).strip(), text)
    text = _MD.sub("", text)
    text = _WS.sub(" ", text).strip()
    if len(text) <= max_chars:
        return text, False
    cut = text[:max_chars]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    cut = cut[:end + 1] if end > max_chars // 3 else cut[:cut.rfind(" ")] + "…"
    return cut + " More on Telegram.", True


def filter_answer(text, mask_amounts=True, max_chars=MAX_CHARS):
    """Mask identifiers, then shape for speech. Returns Filtered(text, masked counts, truncated)."""
    masked, counts = mask_identifiers(str(text or ""), mask_amounts)
    out, cut = spoken(masked, max_chars)
    return Filtered(out or "Done.", counts, cut)
