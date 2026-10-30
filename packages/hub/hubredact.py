#!/usr/bin/env python3
"""hubredact: the hub's credential store, exact-value redaction and HMAC fingerprints. Stdlib only,
one file. PROTECTED (hubverify: app/hubredact*): a release that changes it loads only after the owner's ✅ on its
digest.

The secrets folder (HUB_SECRETS: the read-only mount of HUB_ROOT/secrets, one flat file per credential, 0600, the
hub's uid; filled by the owner's console pastes) is read ONCE at start into this process's memory. Nothing re-reads it
per call and nothing here writes it.

  Store.load(dir)          every regular file of the folder (flat; dotfiles, *.tmp, *.new skipped; ≤ 1 MiB): its
                           stripped text plus the fields secretscan sees (JSON leaves under secret-ish keys, KEY=VALUE
                           lines, bare token lines, 13–19 digit card numbers). A file with group/other bits, another
                           owner or a symlink is REFUSED for use (still redacted).
  store.value(file)        the file's stripped text, for domain code in this process (KeyError: no such file,
                           PermissionError: refused)
  store.field(file, key)   one parsed field: "radarr.key" of media.json, "UOS_PASS" of an env file (same errors)
  store.state()            {files, values, refused: [names], loaded}: names and counts only (for /health)
  Redactor(store)          every value (refused files too) as needles: plain, JSON-escaped, base64 (standard + URL
                           alphabet, padded or not, plus the 3 offset cores a value has inside a longer base64 run such
                           as Basic auth), hex (lower, upper), URL-encoded (quote and quote_plus, %XX or %xx); needles
                           shorter than MIN_NEEDLE are dropped; one trie regex (longest needle wins)
  redactor.text(s)         -> (s', [names]): every hit becomes "[redacted:<name>]"
  redactor.obj(o)          -> (o', [names]) for JSON-like data: str values, dict keys, lists and tuples (copies)
  fp_key(state_dir)        the 32-byte HMAC key, STATE/secrets-fp.key (0600, made once, never in a release or log)
  store.fingerprints(key)  the export for the brain's secretscan: {v, alg, key, w, alphabet, made, items: [{name,
                           kind, n, a, fp}], weak}. Per value: its length, a 16-bit CRC anchor of its first W chars and
                           HMAC-SHA256(key, value); never a value. The key travels with the export because the brain
                           has to hash its own text: a fingerprint is a keyed hash, not a vault, so values with little
                           entropy (passwords) are named in "weak" and should be rotated to long random ones;
                           "refused" names the files held back from use (mode, owner, link).
Values never leave this process: no logging, no API route returns them, errors and states carry file names only.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import stat
import time
import urllib.parse
import zlib
from collections.abc import Iterable

MAX_FILE = 1 << 20
MIN_KEYED = 10  # a value under a secret-ish key (secretscan's MIN_KEYED)
MIN_UNKEYED = 20  # an unkeyed value that still looks like a token (secretscan's MIN_UNKEYED)
MIN_LINE = 16  # a bare line of a multi-line file (secretscan's rule)
MIN_NEEDLE = 10  # shortest needle the redactor searches for (shorter ones would hit ordinary text)
ANCHOR_W = 8  # the fingerprint anchor's width (chars)
WEAK_BITS = 72
FP_KEY_FILE = "secrets-fp.key"
SKIP_SUFFIX = (".tmp", ".new", ".part", ".bak")
FILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SECRET_KEY_RE = re.compile(r"key|token|secret|pass|pwd|auth|cookie|session|credential|bearer|private", re.I)
KV_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_.-]*)\s*[=:]\s*(.*?)\s*$")
DIGIT_RUN_RE = re.compile(r"\d(?:[ -]?\d)+")
CARD_KEYS = ("number", "pan", "card")
MARK = "[redacted:{}]"


# ------------------------------------------------------------------------------------------------ parsing


def _digits(v: str) -> str | None:
    s = re.sub(r"[ -]", "", v)
    return s if s.isdigit() else None


def _tokenish(v: str) -> bool:
    if len(v) < MIN_UNKEYED or re.search(r"\s", v):
        return False
    if (
        re.match(r"[A-Za-z][A-Za-z0-9+.-]*://", v)
        or v.startswith(("/", "~"))
        or re.fullmatch(r"[^@\s]+@[^@\s]+\.\w+", v)
    ):
        return False
    return sum(bool(re.search(p, v)) for p in (r"[a-z]", r"[A-Z]", r"\d")) >= 2


def _add(out: list[tuple[str, str, str]], name: str, value: object, keyed: bool) -> None:
    if not isinstance(value, str):
        return
    v = value.strip().strip("'\"")
    d = _digits(v)
    if d and 13 <= len(d) <= 19:
        out.append(("digits", name, d))
    elif d:
        return  # short numbers (PINs, ports) would match everywhere
    elif (keyed and len(v) >= MIN_KEYED) or _tokenish(v):
        out.append(("str", name, v))


def _walk(obj: object, prefix: str, out: list[tuple[str, str, str]], keyed: bool = False) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():  # the leaf's own key decides
            _walk(v, f"{prefix}.{k}" if prefix else str(k), out,
                  bool(SECRET_KEY_RE.search(str(k))) or str(k).lower() in CARD_KEYS)  # fmt: skip
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _walk(v, f"{prefix}[{i}]", out, keyed)
    elif isinstance(obj, str) and keyed:
        _add(out, prefix, obj, keyed)


def parse(label: str, text: str) -> list[tuple[str, str, str]]:
    """[(kind, name, value)] of one secrets file: kind "str" or "digits"; names "<label>", "<label>:<field>" or
    "<label>:line<n>". The whole stripped file counts when it is one line without spaces (a dedicated token file)."""
    out: list[tuple[str, str, str]] = []
    body = text.strip()
    single = bool(body) and "\n" not in body
    if single and not re.search(r"\s", body):
        _add(out, label, body, True)
    try:
        obj = json.loads(text)
    except ValueError:
        obj = None
    if isinstance(obj, (dict, list)):
        mine: list[tuple[str, str, str]] = []
        _walk(obj, "", mine)
        out.extend((k, f"{label}:{n}", v) for k, n, v in mine)
    elif single:
        m = KV_RE.match(body)  # one KEY=VALUE line: its value too (the file is a dedicated secret)
        if m and m.group(2):
            _add(out, f"{label}:{m.group(1)}", m.group(2), True)
    elif body:
        for i, line in enumerate(text.splitlines(), 1):
            s = line.strip()
            if not s or s.startswith("#") or "-----" in s:
                continue
            m = KV_RE.match(s)
            if m:
                k, v = m.groups()
                _add(out, f"{label}:{k}", v, bool(SECRET_KEY_RE.search(k)) or k.lower() in CARD_KEYS)
                continue
            for run in DIGIT_RUN_RE.findall(s):
                d = _digits(run)
                if d and 13 <= len(d) <= 19:
                    out.append(("digits", f"{label}:line{i}", d))
            if not re.search(r"\s", s) and len(s) >= MIN_LINE:
                out.append(("str", f"{label}:line{i}", s))
    seen: set[tuple[str, str]] = set()
    uniq = []
    for kind, name, val in out:
        if (kind, val) not in seen:
            seen.add((kind, val))
            uniq.append((kind, name, val))
    return uniq


# ------------------------------------------------------------------------------------------------ the store


class Store:
    """The hub's credentials in memory. Build with Store.load(dir) (or Store.from_texts in tests)."""

    def __init__(self) -> None:
        self._text: dict[str, str] = {}
        self._fields: dict[str, dict[str, str]] = {}
        self._values: list[tuple[str, str, str]] = []
        self.refused: dict[str, str] = {}  # file -> why (mode, owner, link, size, unreadable)
        self.loaded = 0.0

    @classmethod
    def load(cls, d: str, uid: int | None = None) -> Store:
        st = cls()
        me = os.getuid() if uid is None else uid
        try:
            names = sorted(os.listdir(d))
        except OSError:
            names = []
        for n in names:
            if n.startswith(".") or n.endswith(SKIP_SUFFIX) or not FILE_NAME.match(n):
                continue
            p = os.path.join(d, n)
            try:
                ls = os.lstat(p)
            except OSError:
                continue
            if stat.S_ISDIR(ls.st_mode):
                continue
            why = ""
            if stat.S_ISLNK(ls.st_mode):
                why = "symlink"
            elif not stat.S_ISREG(ls.st_mode):
                continue
            elif ls.st_size > MAX_FILE:
                why = "too big"
            elif ls.st_mode & 0o077:
                why = f"mode {stat.S_IMODE(ls.st_mode):04o}"
            elif ls.st_uid != me:
                why = "owner"
            if why in ("symlink", "too big"):
                st.refused[n] = why
                continue
            try:
                fd = os.open(p, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                with os.fdopen(fd, "rb") as f:
                    raw = f.read(MAX_FILE + 1)
            except OSError:
                st.refused[n] = "unreadable"
                continue
            st._add(n, raw.decode("utf-8", "replace"))
            if why:
                st.refused[n] = why
        st.loaded = time.time()
        return st

    @classmethod
    def from_texts(cls, files: dict[str, str]) -> Store:
        st = cls()
        for n, t in files.items():
            st._add(n, t)
        st.loaded = time.time()
        return st

    def _add(self, name: str, text: str) -> None:
        self._text[name] = text.strip()
        got = parse(name, text)
        self._fields[name] = {k[len(name) + 1 :]: v for _, k, v in got if k.startswith(name + ":")}
        known = {(k, v) for k, _, v in self._values}
        self._values.extend(x for x in got if (x[0], x[2]) not in known)

    def names(self) -> list[str]:
        return sorted(self._text)

    def _usable(self, name: str) -> None:
        if name not in self._text:
            raise KeyError(name)
        if name in self.refused:
            raise PermissionError(f"{name}: refused ({self.refused[name]})")

    def value(self, name: str) -> str:
        self._usable(name)
        return self._text[name]

    def field(self, name: str, key: str) -> str:
        self._usable(name)
        f = self._fields.get(name) or {}
        if key not in f:
            raise KeyError(f"{name}:{key}")
        return f[key]

    def values(self) -> list[tuple[str, str, str]]:
        """[(kind, name, value)]: for the redactor and the fingerprints only."""
        return list(self._values)

    def state(self) -> dict:
        return {
            "files": len(self._text),
            "values": len(self._values),
            "refused": sorted(self.refused),
            "loaded": round(self.loaded),
        }

    def fingerprints(self, key: bytes) -> dict:
        items, weak, alpha = [], [], set()
        for kind, name, v in self._values:
            it = {"name": f"hub:{name}", "kind": kind, "n": len(v), "fp": fingerprint(key, v)}
            if kind == "str":
                it["a"] = anchor(v)
                alpha.update(v)
                if entropy_bits(v) < WEAK_BITS:
                    weak.append(f"hub:{name}")
            items.append(it)
        return {
            "v": 1,
            "alg": "hmac-sha256",
            "key": key.hex(),
            "w": ANCHOR_W,
            "alphabet": "".join(sorted(alpha)),
            "made": round(time.time()),
            "items": items,
            "weak": sorted(weak),
            "refused": sorted(self.refused),
        }


def fingerprint(key: bytes, value: str) -> str:
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()


def anchor(value: str) -> int:
    """16-bit CRC32 anchor of the value's first ANCHOR_W chars (UTF-8): a cheap filter, 16 bits of the prefix."""
    return zlib.crc32(value[:ANCHOR_W].encode("utf-8")) & 0xFFFF


def entropy_bits(v: str) -> float:
    pool = 0
    pool += 26 if re.search(r"[a-z]", v) else 0
    pool += 26 if re.search(r"[A-Z]", v) else 0
    pool += 10 if re.search(r"\d", v) else 0
    pool += 32 if re.search(r"[^A-Za-z0-9]", v) else 0
    return len(v) * math.log2(max(pool, 2))


def fp_key(state_dir: str) -> bytes:
    """The fingerprint HMAC key: STATE/secrets-fp.key, 32 random bytes, 0600, created once (O_EXCL)."""
    p = os.path.join(state_dir, FP_KEY_FILE)
    for _ in range(3):
        try:
            with open(p, "rb") as f:
                k = f.read(64)
            if len(k) == 32:
                return k
            raise OSError("bad key file")
        except FileNotFoundError:
            pass
        try:
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "wb") as f:
            f.write(secrets.token_bytes(32))
            f.flush()
            os.fsync(f.fileno())
    raise OSError("no fingerprint key")


# ------------------------------------------------------------------------------------------------ redaction


def _b64_forms(b: bytes) -> list[str]:
    out = []
    for k in (0, 1, 2):
        enc = base64.b64encode(b"\0" * k + b).decode("ascii")
        core = enc[-(-4 * k // 3) : (4 * (k + len(b))) // 3]  # the chars that depend on b only
        out.append(core)
        if k == 0:
            out += [enc, enc.rstrip("=")]
    return out + [x.translate(str.maketrans("+/", "-_")) for x in out]


def _url_forms(v: str) -> list[str]:
    out = []
    for q in (urllib.parse.quote(v, safe=""), urllib.parse.quote_plus(v, safe="")):
        out += [q, re.sub(r"%[0-9A-F]{2}", lambda m: m.group().lower(), q)]
    return out


def needles(kind: str, v: str) -> list[str]:
    """Every form of one value the redactor searches for (≥ MIN_NEEDLE chars, deduplicated, plain first)."""
    if kind == "digits":
        forms = [v]
        if len(v) == 16:
            forms += [sep.join(v[i : i + 4] for i in range(0, 16, 4)) for sep in (" ", "-")]
    else:
        b = v.encode("utf-8")
        forms = [v, json.dumps(v)[1:-1], json.dumps(v, ensure_ascii=False)[1:-1]]
        forms += _b64_forms(b) + [binascii.hexlify(b).decode(), binascii.hexlify(b).decode().upper()]
        forms += _url_forms(v)
    seen, out = set(), []
    for f in forms:
        if len(f) >= MIN_NEEDLE and f not in seen:
            seen.add(f)
            out.append(f)
    return out


def _trie_regex(words: Iterable[str]) -> re.Pattern[str] | None:
    """One regex over all words, built from their prefix tree (single-child chains stay literal runs): at most one
    attempt per branch point at each position; the greedy optional tails make the longest word win."""
    trie: dict = {}
    for w in words:
        node = trie
        for ch in w:
            node = node.setdefault(ch, {})
        node[""] = {}
    if not trie:
        return None

    def emit(node: dict) -> str:
        run = []
        while True:
            keys = [k for k in node if k != ""]
            if len(keys) == 1 and "" not in node:
                run.append(re.escape(keys[0]))
                node = node[keys[0]]
                continue
            break
        keys = sorted(k for k in node if k != "")
        alts = [re.escape(k) + emit(node[k]) for k in keys]
        tail = ""
        if alts:
            tail = alts[0] if len(alts) == 1 else "(?:" + "|".join(alts) + ")"
            if "" in node:
                tail = "(?:" + tail + ")?"
        return "".join(run) + tail

    return re.compile(emit(trie))


class Redactor:
    def __init__(self, store: Store | None = None, values: Iterable[tuple[str, str, str]] = ()) -> None:
        self.names: dict[str, str] = {}
        for kind, name, v in [*(store.values() if store else []), *values]:
            for n in needles(kind, v):
                self.names.setdefault(n, name)
        self.rx = _trie_regex(sorted(self.names))

    def __len__(self) -> int:
        return len(self.names)

    def text(self, s: str) -> tuple[str, list[str]]:
        if self.rx is None or not s:
            return s, []
        hits: list[str] = []

        def sub(m: re.Match[str]) -> str:
            name = self.names.get(m.group(), "secret")
            hits.append(name)
            return MARK.format(name)

        return self.rx.sub(sub, s), hits

    def obj(self, o: object) -> tuple[object, list[str]]:
        hits: list[str] = []

        def walk(x: object, depth: int = 0) -> object:
            if depth > 200:
                raise ValueError("too deep to redact")
            if isinstance(x, str):
                y, h = self.text(x)
                hits.extend(h)
                return y
            if isinstance(x, dict):
                return {walk(k, depth + 1) if isinstance(k, str) else k: walk(v, depth + 1) for k, v in x.items()}
            if isinstance(x, (list, tuple)):
                return [walk(v, depth + 1) for v in x]
            if isinstance(x, (bytes, bytearray)):
                return walk(bytes(x).decode("utf-8", "replace"), depth + 1)
            return x

        return walk(o), hits
