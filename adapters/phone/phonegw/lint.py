"""Lock-screen lint, the same rules as ~/bin/push lint() (parity via protocol/fixtures/lint/lockscreen.json):
masks from the voice relay's answer filter (built-in fallback), a 5+ digit run, title > 40 or body > 80 chars."""
import importlib.machinery
import importlib.util
import os
import re

HOME = os.environ.get("HOME") or os.path.expanduser("~")
FILTER_SRC = os.environ.get("PHONEGW_FILTER") or os.path.join(HOME, "opt/assistant/adapters/voice/voice_relay/answer_filter.py")
DIGIT_RUN = re.compile(r"(?<!\d)\d(?:[ '’]?\d){4,}(?!\d)")
TITLE_MAX, BODY_MAX = 40, 80
_FALLBACK = [
    (re.compile(r"(?i)\b(?:https?://|www\.)\S+"), "link"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "email"),
    (re.compile(r"(?i)\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){3,7}\s?[A-Z0-9]{1,4}\b"), "iban"),
    (re.compile(r"(?<![\w])\+?\d[\d ()/-]{8,}\d"), "phone"),
    (re.compile(r"(?i)\b(?:chf|fr\.|eur|usd|€|\$)\s?\d[\d'’ ]*(?:[.,]\d{1,2})?"), "amount"),
]
_mod = None


def _filter():
    global _mod
    if _mod is None:
        try:
            loader = importlib.machinery.SourceFileLoader("phonegw_answer_filter", FILTER_SRC)
            spec = importlib.util.spec_from_loader("phonegw_answer_filter", loader)
            mod = importlib.util.module_from_spec(spec)
            loader.exec_module(mod)
            mod.mask_identifiers  # noqa: B018
            _mod = mod
        except Exception:
            _mod = False
    return _mod


def mask(text):
    mod = _filter()
    if mod:
        return mod.mask_identifiers(text, mask_amounts=True)
    counts = {}
    for rx, key in _FALLBACK:
        text, n = rx.subn("[%s]" % key, text)
        if n:
            counts[key] = counts.get(key, 0) + n
    return text, counts


def lint(title, body, lockscreen):
    """-> (title, body, problems, masked_counts). lockscreen=True (urgent): any problem blocks the push."""
    problems, counts, out = [], {}, []
    for part in (title, body):
        m, c = mask(part)
        for k, v in c.items():
            counts[k] = counts.get(k, 0) + v
        out.append(re.sub(r"\s+", " ", m).strip())
    if lockscreen:
        if counts:
            problems.append("would mask " + ", ".join(f"{k} x{v}" for k, v in sorted(counts.items())))
        runs = sum(len(DIGIT_RUN.findall(x)) for x in out)
        if runs:
            problems.append(f"digit run x{runs}")
        if len(title) > TITLE_MAX:
            problems.append(f"title {len(title)} > {TITLE_MAX} chars")
        if len(body) > BODY_MAX:
            problems.append(f"body {len(body)} > {BODY_MAX} chars")
    return out[0], out[1], problems, counts
