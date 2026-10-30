"""Small helpers: ULIDs, pairing codes, time, private file writes, log masking (copied from the relay's logmask)."""
import ipaddress
import json
import logging
import os
import re
import secrets
import sys
import tempfile
import time

CROCK = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


def now_ms():
    return int(time.time() * 1000)


def now_s():
    return int(time.time())


_last = [0, 0]


def ulid(ms=None):
    """Monotonic within this process: same millisecond -> random part + 1, so ids sort in creation order."""
    ms = now_ms() if ms is None else int(ms)
    if ms <= _last[0]:
        ms, r = _last[0], (_last[1] + 1) & ((1 << 80) - 1)
    else:
        r = secrets.randbits(79)
    _last[0], _last[1] = ms, r
    n = (ms << 80) | r
    out = []
    for _ in range(26):
        out.append(CROCK[n & 31])
        n >>= 5
    return "".join(reversed(out))


def gen_code():
    """8 Crockford base32 characters (40 bits), shown as XXXX-XXXX."""
    s = "".join(secrets.choice(CROCK) for _ in range(8))
    return s[:4] + "-" + s[4:]


def normalize_code(s):
    """Accept lower case, a dash or spaces, and Crockford's look-alikes (I/L -> 1, O -> 0). None if malformed."""
    if not isinstance(s, str):
        return None
    t = re.sub(r"[\s-]", "", s).upper().translate(str.maketrans("ILO", "110"))
    if len(t) != 8 or any(c not in CROCK for c in t):
        return None
    return t


def short_id(n=6):
    return "".join(secrets.choice(CROCK) for _ in range(n))


def write_private(path, data, mode=0o600):
    """Atomic write with the given mode (the file never exists with wider permissions)."""
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb" if isinstance(data, bytes) else "w") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path, default=None):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def ip_of(s):
    try:
        ip = ipaddress.ip_address(str(s).split("%", 1)[0])
    except ValueError:
        return None
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip


# ---------------------------------------------------------------- log masking (relay logmask.py, trimmed)
IP_MASK, TOKEN_MASK = "<ip>", "***"
_V4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?!\w|\.\d)")
_V6 = re.compile(r"(?<![\w:.])\[?((?:[0-9A-Fa-f]{0,4}:){2,7}(?:[0-9A-Fa-f]{0,4}|(?:\d{1,3}\.){3}\d{1,3})(?:%[\w.-]+)?)\]?")
_TOKEN = re.compile(r"(?<![\w+=-])[A-Za-z0-9_\-+=]{32,}")


def _is_ip(s):
    return ip_of(s) is not None


def mask(text, secret_values=()):
    text = str(text)
    for v in sorted({str(s) for s in secret_values if s and len(str(s)) >= 4}, key=len, reverse=True):
        text = text.replace(v, TOKEN_MASK)
    text = _V6.sub(lambda m: IP_MASK if _is_ip(m.group(1)) and m.group(1).count(":") >= 2 else m.group(0), text)
    text = _V4.sub(lambda m: IP_MASK if _is_ip(m.group(0)) else m.group(0), text)
    return _TOKEN.sub(lambda m: TOKEN_MASK if re.search(r"\d", m.group(0)) and re.search(r"[A-Za-z]", m.group(0))
                      else m.group(0), text)


class MaskingFormatter(logging.Formatter):
    def __init__(self, secret_values=()):
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
        self.secret_values = tuple(secret_values)

    def format(self, record):
        return mask(super().format(record), self.secret_values)


def setup_logging(secret_values=(), level=logging.INFO, stream=None):
    h = logging.StreamHandler(stream or sys.stderr)
    h.setFormatter(MaskingFormatter(secret_values))
    root = logging.getLogger()
    root.handlers[:] = [h]
    root.setLevel(logging.WARNING)
    logging.getLogger("phonegw").setLevel(level)
    return h
