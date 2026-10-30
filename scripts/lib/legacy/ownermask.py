#!/usr/bin/env python3
"""ownermask: the owner's personal data masked out of browser output at all times, not only while a secure fill is
live. Used by abx (everything it prints: text, snapshot, eval, html, values, errors) and by the browser half of
securefill (securefill-cdp.mjs maskText runs `ownermask.py spans`, so `securefill shot` / `securefill mask` black out
the same page text before a screenshot).

Values: the private profile (assistant_profile: owner.first_name / last_name / email / phone, home.street / postcode /
town / town_aliases) turned into the word-token fingerprints a secure fill makes (securefill-cdp.mjs textItems, hashed
in memory with a per-process key: nothing is stored or printed) and matched by securefill's own matcher
(securefill.find_hits: normalized tokens, umlaut variants, %XX / \\uXXXX / HTML escapes; JSON escapes like \\n are
read as spaces). Masked: his name words, the e-mail and its local-part words, the street (also as "...str." and
"... Strasse") with its house number, the postcode, and the town next to the postcode (the town alone stays: coarse,
needed for trains). Near his name (the next 250 characters) address-shaped text is masked too: a delivery address that
is not his home. Always by pattern: card numbers (Luhn) and IBANs (mod 97).
Fail closed: no profile, an unreadable one, or a key still at its placeholder -> that kind is masked by pattern
(street + house number, CH/DE/AT postcode + town, phone numbers, e-mail addresses). A crash while masking is the
caller's to fail closed (abx withholds the output; the browser half takes no screenshot).
Screenshots (keep_town): the postcode is masked but the town words next to it stay, so the approval card shows him
his own delivery town; name, street + number, e-mail, phone, IBAN and card stay masked.
Images (mask_image, the PostToolUse hook in securefill.py for browser screenshots): OCR word boxes (tesseract TSV) ->
the same spans (keep_town) -> those boxes blacked out -> OCR again until nothing is found (at most ROUNDS); OCR or
imaging unavailable, the time budget spent or still readable after the last round -> "withhold".

  ownermask.py spans  < {"texts": [...], "keep_town": false}  -> {"ok": true, "marks": [[i, start, end], ...]}
                                           (UTF-16 offsets, for JS)
  ownermask.py image IN.png OUT.png        -> {"status": "clean|masked|withhold", "boxes": n} (OUT written when masked)
  ownermask.py status                      -> which kinds are matched by value and which by pattern (never a value)
Marker: [personal]. Tests: ~/opt/agent-browser/tests/test_ownermask.py (synthetic profiles) and test_abx.py F_OwnerMask.
"""
import bisect
import io
import json
import os
import re
import secrets
import subprocess
import sys
import time

LIB = os.path.dirname(os.path.realpath(__file__))
if LIB not in sys.path:
    sys.path.insert(0, LIB)
import securefill as SF  # noqa: E402  (one matcher for fill fingerprints and the profile)

MARK = "[personal]"
WINDOW = 250                    # characters after a hit on his name searched for address-shaped text
KEYS = {"given": ("owner", "first_name"), "surname": ("owner", "last_name"), "email": ("owner", "email"),
        "phone": ("owner", "phone"), "street": ("home", "street"), "postcode": ("home", "postcode"),
        "town": ("home", "town")}
FALLBACK = ("street", "postcode", "email", "phone")      # masked by pattern when the profile has no value for them
# the same rules as securefill-cdp.mjs textItems (STOP, MAIL_STOP, COMMON_MAIL, strongTok)
STOP = frozenset((
    "am an auf bei der die das den dem des de di da do du del della dello dei degli delle la le les lo los las el il "
    "van von vom zum zur im in on of the and und et e y sur sous san santa sankt saint st ste rue via viale route "
    "chemin avenue av weg gasse strasse str platz street road rd lane c o mr mrs ms dr herr frau").split())
MAIL_STOP = frozenset("info mail email contact kontakt hello office admin post service noreply".split())
COMMON_MAIL = re.compile(r"^(gmail|googlemail|outlook|hotmail|live|msn|yahoo|ymail|icloud|me|mac|gmx|web|bluewin|"
                         r"sunrise|protonmail|proton|pm|hispeed|swissonline|aol|mail|t-online|freenet|yandex|zoho)\.")
CALLING = ("423", "41", "49", "43", "33", "39", "44", "31", "32", "34", "1")
JSON_ESC = re.compile(r'\\[nrtbf"/\\]')                   # same length as two spaces: offsets stay put

L = r"[^\W\d_]"
_SUF = (r"(?:strasse|straße|str\.?|gasse|weg|platz|allee|ring|rain|halde|matte|steig|stieg|ufer|damm|graben|quai|hof|"
        r"park|berg|feld|acker|wiese|egg|bühl|buehl|rank|hang|pfad|promenade|zeile|chaussee|markt|street|road|lane)")
STREET = (
    re.compile(rf"(?<!{L})(?:{L}+[ \-])?{L}*{_SUF}\s?\d{{1,4}}(?:\s?[a-z](?!{L}))?(?!\d)", re.I),
    re.compile(rf"(?<!{L})(?:rue|route|chemin|avenue|boulevard|place|via|viale|piazza|corso|vicolo)\s+"
               rf"(?:{L}+['’\- ]+){{0,4}}{L}+,?\s+\d{{1,4}}(?:[a-z](?!{L}))?(?!\d)", re.I),
    re.compile(rf"(?<!\w)\d{{1,4}}[a-z]?,?\s+(?:rue|route|chemin|avenue|boulevard|place)(?:\s+{L}+['’\-]?){{1,5}}", re.I))
_UP = r"[A-ZÀ-ÖØ-Ý]"
POSTCODE = (                                          # group pc: the code alone (keep_town masks only that)
    re.compile(rf"(?<![\w\-./])(?P<pc>(?:(?:CH|D|DE|A|AT|FL|LI)[\- ]?)?\d{{4,5}})[ \t]+{_UP}{L}*(?:[\-.'’]{L}+)*"
               rf"(?:[ \t](?:am|an|im|bei|ob|sur|sous|di|{_UP}{L}*)(?:[\-.'’]{L}+)*)*"),
    re.compile(r"(?<![\w\-])(?:CH|D|DE|A|AT|FL|LI)-\d{4,5}(?!\d)"))
PHONE = (
    re.compile(r"(?<![\w+])(?:\+|00)\d{1,3}(?:[ \-/]?\(0\))?(?:[ \-/]?\d{1,5}){2,6}(?!\d)"),
    re.compile(r"(?<![\w+.,])\(?0\d{1,4}\)?(?:[ \-/]?\d{2,5}){1,5}(?!\d)"),
    re.compile(r"(?<![\w+])7[5-9] \d{3} \d{2} \d{2}(?!\d)"))
# not inside a word, path or file name (abx prints screenshot-<13-digit epoch ms>.png: 1 in 10 of those passes Luhn)
CARD = re.compile(r"(?<![\w.,\-/])(?:\d{4}[ \-]?){3}\d{1,7}(?!\d|\.\w)"
                  r"|(?<![\w.,\-/])\d{4}[ \-]?\d{6}[ \-]?\d{4,5}(?!\d|\.\w)")
IBAN = re.compile(r"(?<![A-Za-z0-9])[A-Z]{2}\d{2}(?: ?[A-Z0-9]){11,30}")


def _words(s):
    return SF.WORD.findall(str(s or ""))


def _strong(t):
    nd = sum(c.isdigit() for c in t)
    return len(t) >= 3 and t not in STOP and (nd == 0 or nd >= 4)


def _values():
    """{kind: value} from the private profile; a missing, placeholder or unreadable value is left out ({} = none)."""
    try:
        import assistant_profile as P
        if not P.source():
            return {}
        d = P.load()
    except Exception:
        return {}
    out = {}
    for kind, (sec, k) in KEYS.items():
        v = (d.get(sec) or {}).get(k)
        if isinstance(v, (str, int)) and not isinstance(v, bool) and str(v).strip() \
                and v != (P.DEFAULTS.get(sec) or {}).get(k):
            out[kind] = str(v).strip()
    al = (d.get("home") or {}).get("town_aliases")
    if isinstance(al, list):
        out["towns"] = [a.strip() for a in al if isinstance(a, str) and a.strip()]
    return out


def _items(v, key, keep_town=False):
    """Fingerprint items (tok / seq / email / digits; hashes only) of the profile values, like a fill's. keep_town:
    no postcode + town sequences (the postcode alone is still a token)."""
    items, seen = [], set()

    def H(s):
        return SF._h(key, "tok", s)

    def tok(n):
        h = H(n)
        if h not in seen:
            seen.add(h)
            items.append({"kind": "tok", "h": h})

    def ent(w, strong=None):
        n = SF._tnorm(w)
        return n, (_strong(n[0]) if strong is None else strong)

    def seq(es):
        if len(es) >= 2:
            items.append({"kind": "seq", "p": [{"s": 1 if s else 0, "h": [H(x) for x in n]} for n, s in es]})

    def value(ws):                                     # strong words on their own + the whole run
        for w in ws:
            n, s = ent(w)
            if s:
                for x in n:
                    tok(x)
        seq([ent(w) for w in ws])

    given, sur = _words(v.get("given")), _words(v.get("surname"))
    value(given)
    value(sur)
    if given and sur:
        seq([ent(w) for w in given + sur])
        seq([ent(w) for w in sur + given])
    street = _words(v.get("street"))
    value(street)
    for i, w in enumerate(street):                     # Xstrasse 5 also as "Xstr. 5", "X-Strasse 5", "X Str. 5"
        rest = [ent(x) for x in street[i + 1:]]
        for n in SF._tnorm(w):
            if n.endswith("strasse") and len(n) > 9:
                stem = n[:-7]
                tok(stem + "str")
                seq([((stem + "str",), True)] + rest)
                seq([((stem,), True), (("strasse",), False)] + rest)
                seq([((stem,), True), (("str",), False)] + rest)
    pc = _words(v.get("postcode"))
    value(pc)
    for town in ([] if keep_town else [v.get("town")] + list(v.get("towns") or [])):
        tw = [ent(w, False) for w in _words(town)]    # town words never strong: the town alone stays
        if pc and tw:
            p = [ent(w) for w in pc]
            seq(p + tw)
            seq(tw + p)
    em = str(v.get("email") or "").strip().lower()
    if "@" in em:
        items.append({"kind": "email", "h": SF._h(key, "email", em)})
        local, domain = em.rsplit("@", 1)
        lw = _words(local)
        for w in lw:
            n = SF._tnorm(w)
            if _strong(n[0]) and len(n[0]) >= 4 and n[0] not in MAIL_STOP:
                for x in n:
                    tok(x)
        seq([ent(w) for w in lw])
        if not COMMON_MAIL.match(domain):
            seq([ent(w) for w in _words(domain)])
    raw = str(v.get("phone") or "").strip()
    d = re.sub(r"\D", "", raw)
    if d:
        if raw.startswith("00"):
            d = d[2:]
        if raw.startswith(("+", "00")):
            d = next((d[len(c):] for c in CALLING if d.startswith(c)), d)
        d = d.lstrip("0")
        if len(d) >= 6:
            items.append({"kind": "digits", "n": len(d), "h": SF._h(key, "digits", d)})
    return items


def _index(items):
    """Items -> the matcher's fingerprint dict (the shape of securefill.live())."""
    out, seqs = {}, set()
    for it in items:
        if it["kind"] == "seq":
            seqs.add(tuple((bool(x["s"]), frozenset(x["h"])) for x in it["p"]))
        else:
            out.setdefault(it["kind"], {})[it["h"]] = it.get("n")
    if seqs:
        out["seq"] = sorted(seqs, key=len)
        idx = {}
        for si, sq in enumerate(out["seq"]):
            for pos, (_, hs) in enumerate(sq):
                for h in hs:
                    idx.setdefault(h, []).append((si, pos))
        out["_seqidx"] = idx
    return out


def _luhn(d):
    s = 0
    for i, c in enumerate(reversed(d)):
        n = int(c) * (2 if i % 2 else 1)
        s += n - 9 if n > 9 else n
    return s % 10 == 0


def _iban_ok(s):
    t = s[4:] + s[:4]
    try:
        return int("".join(str(int(c, 36)) for c in t)) % 97 == 1
    except ValueError:
        return False


def _phone_ok(s):
    intl = s.startswith(("+", "00"))
    d = re.sub(r"\D", "", s.replace("(0)", ""))
    if s.startswith("00"):
        d = d[2:]
    if intl:
        return 9 <= len(d) <= 15
    return s[0] == "7" or 9 <= len(d) <= 12


def _pc_spans(v, keep_town=False):
    out = []
    for rx in POSTCODE:
        g = "pc" if keep_town and "pc" in rx.groupindex else 0
        out += [m.span(g) for m in rx.finditer(v)]
    return out


def _pattern_spans(kind, v, keep_town=False):
    out = []
    if kind == "street":
        for rx in STREET:
            out += [m.span() for m in rx.finditer(v)]
    elif kind == "postcode":
        out += _pc_spans(v, keep_town)
    elif kind == "email":
        out += [m.span() for m in SF.EMAIL.finditer(v)]
    elif kind == "phone":
        for rx in PHONE:
            out += [m.span() for m in rx.finditer(v) if _phone_ok(m.group())]
    elif kind == "card":
        for m in CARD.finditer(v):
            d = re.sub(r"\D", "", m.group())
            if 13 <= len(d) <= 19 and _luhn(d):
                out.append(m.span())
    elif kind == "iban":
        for m in IBAN.finditer(v):
            pos = [m.start() + i for i, c in enumerate(m.group()) if c != " "]
            s = "".join(v[p] for p in pos)
            for n in range(min(34, len(s)), 14, -1):
                if s[2:4].isdigit() and _iban_ok(s[:n]):
                    out.append((m.start(), pos[n - 1] + 1))
                    break
    return out


def _merge(spans):
    merged = []
    for a, b in sorted(spans):
        if b <= a:
            continue
        if merged and a < merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return merged


class Masker:
    def __init__(self, keep_town=False):
        self.key = secrets.token_bytes(32)             # per process, in memory only
        self.cache = {}
        self.keep_town = bool(keep_town)
        vals = _values()
        self.kinds = sorted(k for k in KEYS if vals.get(k))
        self.fps = _index(_items(vals, self.key, self.keep_town))
        names = {k: vals[k] for k in ("given", "surname") if vals.get(k)}
        self.name_fps = _index(_items(names, self.key)) if names else {}
        self.generic = [k for k in FALLBACK if not vals.get(k)] + ["card", "iban"]

    def status(self):
        return {"profile_values": self.kinds, "by_pattern": self.generic, "near_name_addresses": bool(self.name_fps)}

    def value_hits(self, text):
        return SF.find_hits(text, self.fps, self.key, self.cache) if self.fps else []

    def spans(self, text):
        if not text:
            return []
        v = JSON_ESC.sub("  ", text) if "\\" in text else text
        out = self.value_hits(v)
        if self.name_fps:
            for _, b in SF.find_hits(v, self.name_fps, self.key, self.cache):
                w = v[b:b + WINDOW]
                for rx in STREET:
                    out += [(b + m.start(), b + m.end()) for m in rx.finditer(w)]
                out += [(b + s, b + e) for s, e in _pc_spans(w, self.keep_town)]
        for kind in self.generic:
            out += _pattern_spans(kind, v, self.keep_town)
        return out

    def mask(self, text):
        merged = _merge(self.spans(text))
        if not merged:
            return text, 0
        parts, last = [], 0
        for a, b in merged:
            parts += [text[last:a], MARK]
            last = b
        parts.append(text[last:])
        return "".join(parts), len(merged)


_M = {}


def masker(keep_town=False):
    k = bool(keep_town)
    if k not in _M:
        _M[k] = Masker(k)
    return _M[k]


def mask(text):
    """text with his personal data replaced by [personal]; raises on a matcher error (callers fail closed)."""
    return masker().mask(text)[0] if text else text


# ---------------------------------------------------------------- images (screenshots): OCR word boxes blacked out
OCR_LANGS = ("eng+deu", "eng")                         # deu reads umlauts; eng alone when deu is not installed
ROUNDS = 3                                             # blackout -> OCR again, at most this often
PAD = 3                                                # px around each word box


def _tesseract(raw, timeout):
    """TSV of tesseract on image bytes (stdin: no temp file), or None (missing, failed, timed out)."""
    for lang in OCR_LANGS:
        if timeout < 1:
            return None
        t0 = time.monotonic()
        try:
            p = subprocess.run(["tesseract", "stdin", "stdout", "--psm", "11", "-l", lang, "tsv"], input=raw,
                               capture_output=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if p.returncode == 0:
            return p.stdout.decode("utf-8", "replace")
        timeout -= time.monotonic() - t0
    return None


def ocr_lines(raw, timeout=25):
    """OCR words of an image as lines in reading order: [[(word, (x0, y0, x1, y1)), ...], ...]; None = no OCR."""
    tsv = _tesseract(raw, timeout)
    if tsv is None:
        return None
    lines = {}
    for row in tsv.splitlines()[1:]:
        f = row.split("\t")
        if len(f) < 12 or f[0] != "5" or not f[11].strip():
            continue
        try:
            x, y, w, h = (int(n) for n in f[6:10])
        except ValueError:
            continue
        lines.setdefault((f[2], f[3], f[4]), []).append((f[11].strip(), (x, y, x + w, y + h)))
    out = [sorted(ws, key=lambda e: e[1][0]) for ws in lines.values()]
    out.sort(key=lambda ws: (min(b[1] for _, b in ws), min(b[0] for _, b in ws)))
    return out


def image_boxes(m, lines):
    """Word boxes showing his data: the masker's spans over the OCR text (words joined by spaces, lines by newlines),
    plus value hits over each line without spaces (OCR splitting a word: "Zwog genbach")."""
    boxes = set()
    for sep in (" ", ""):
        parts, index, pos = [], [], 0
        for ws in lines:
            for i, (w, b) in enumerate(ws):
                if i and sep:
                    parts.append(sep)
                    pos += len(sep)
                parts.append(w)
                index.append((pos, pos + len(w), b))
                pos += len(w)
            parts.append("\n")
            pos += 1
        text = "".join(parts)
        spans = m.spans(text) if sep else m.value_hits(text)
        if not spans:
            continue
        starts = [s for s, _, _ in index]
        for a, e in _merge(spans):
            i = max(0, bisect.bisect_right(starts, a) - 1)
            while i < len(index) and index[i][0] < e:
                if index[i][1] > a:
                    boxes.add(index[i][2])
                i += 1
    return boxes


def mask_image(raw, keep_town=True, budget=25.0):
    """His personal data blacked out of an image: {"status": "clean"} (nothing found), {"status": "masked", "data":
    bytes, "mime": ..., "boxes": n} or {"status": "withhold", "why": ...}. Fails closed: an unreadable image, OCR or
    Pillow missing, the budget spent, or his data still found after ROUNDS blackouts -> withhold."""
    end = time.monotonic() + budget
    m = masker(keep_town)
    try:
        from PIL import Image, ImageDraw
        im = Image.open(io.BytesIO(raw))
        im.load()
    except Exception:
        return {"status": "withhold", "why": "image or Pillow unavailable"}
    fmt = "JPEG" if im.format == "JPEG" else "PNG"
    data, total = raw, 0
    for rnd in range(ROUNDS + 1):
        lines = ocr_lines(data, end - time.monotonic())
        if lines is None:
            return {"status": "withhold", "why": "OCR failed or out of time"}
        boxes = image_boxes(m, lines)
        if not boxes:
            if not total:
                return {"status": "clean"}
            return {"status": "masked", "data": data, "mime": "image/jpeg" if fmt == "JPEG" else "image/png",
                    "boxes": total}
        if rnd >= ROUNDS:
            break
        if im.mode not in ("RGB", "RGBA", "L"):
            im = im.convert("RGBA" if fmt == "PNG" else "RGB")
        d = ImageDraw.Draw(im)
        fill = 0 if im.mode == "L" else (0, 0, 0) if im.mode == "RGB" else (0, 0, 0, 255)
        for x0, y0, x1, y1 in boxes:
            d.rectangle((x0 - PAD, y0 - PAD, x1 + PAD, y1 + PAD), fill=fill)
        total += len(boxes)
        buf = io.BytesIO()
        im.save(buf, fmt, **({"quality": 90} if fmt == "JPEG" else {}))
        data = buf.getvalue()
    return {"status": "withhold", "why": "still readable after the blackout"}


def _u16(t, k):
    return k + sum(1 for c in t[:k] if ord(c) > 0xFFFF)


def marks(texts, m=None):
    """[[i, start, end]] over a list of page text nodes (JS UTF-16 offsets); joined with newlines first, so the window
    after his name reaches the next nodes (seqs never cross a newline)."""
    m = m or masker()
    parts = [t if isinstance(t, str) else "" for t in texts]
    starts, pos = [], 0
    for t in parts:
        starts.append(pos)
        pos += len(t) + 1
    out = []
    for a, b in _merge(m.spans("\n".join(parts))):
        i = max(0, bisect.bisect_right(starts, a) - 1)
        while i < len(parts) and starts[i] < b:
            na, nb = max(a, starts[i]) - starts[i], min(b, starts[i] + len(parts[i])) - starts[i]
            if nb > na:
                out.append([i, _u16(parts[i], na), _u16(parts[i], nb)])
            i += 1
    return out


def main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    try:
        if cmd == "spans":
            d = json.load(sys.stdin)
            texts = d.get("texts") if isinstance(d, dict) else None
            if not isinstance(texts, list):
                raise ValueError("texts")
            print(json.dumps({"ok": True, "marks": marks(texts, masker(bool(d.get("keep_town"))))}))
            return 0
        if cmd == "image" and len(argv) == 4:
            with open(argv[2], "rb") as f:
                r = mask_image(f.read())
            if r["status"] == "masked":
                fd = os.open(argv[3], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "wb") as f:
                    f.write(r["data"])
            print(json.dumps({k: v for k, v in r.items() if k in ("status", "boxes", "why")}))
            return 0
        if cmd == "status":
            print(json.dumps(masker().status()))
            return 0
    except Exception as e:
        print(json.dumps({"ok": False, "error": type(e).__name__}))
        return 1
    print(__doc__.strip(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
