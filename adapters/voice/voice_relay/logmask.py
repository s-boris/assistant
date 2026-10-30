"""Relay log setup: timestamped stderr lines with IP addresses and tokens masked.

ntgcalls forwards its native connection log to the Python loggers "ntgcalls" and "webrtc" (and sets both to CRITICAL
when imported). The relay shows ntgcalls at INFO (connection state) and keeps webrtc at WARNING (its INFO is noisy and
carries ICE candidates with IP addresses). Every formatted line, tracebacks included, passes through mask().
"""
import ipaddress
import logging
import re
import sys

IP_MASK = "<ip>"
TOKEN_MASK = "***"

_V4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?!\w|\.\d)")
# IPv6 candidates (also IPv4-mapped and %zone); validated with ipaddress, so clock times like 12:34:56 stay.
_V6 = re.compile(r"(?<![\w:.])\[?((?:[0-9A-Fa-f]{0,4}:){2,7}(?:[0-9A-Fa-f]{0,4}|(?:\d{1,3}\.){3}\d{1,3})(?:%[\w.-]+)?)\]?")
# Long opaque runs (bearer tokens, session strings, api hashes); short ids and words are left alone.
_TOKEN = re.compile(r"(?<![\w+=-])[A-Za-z0-9_\-+=]{32,}")


def _is_ip(s):
    try:
        ipaddress.ip_address(s.split("%", 1)[0])
        return True
    except ValueError:
        return False


def mask(text, secrets=()):
    """Mask known secret values, IPv4/IPv6 addresses and long token-like strings."""
    text = str(text)
    for v in sorted({str(s) for s in secrets if s and len(str(s)) >= 4}, key=len, reverse=True):
        text = text.replace(v, TOKEN_MASK)
    text = _V6.sub(lambda m: IP_MASK if _is_ip(m.group(1)) and m.group(1).count(":") >= 2 else m.group(0), text)
    text = _V4.sub(lambda m: IP_MASK if _is_ip(m.group(0)) else m.group(0), text)
    return _TOKEN.sub(lambda m: TOKEN_MASK if re.search(r"\d", m.group(0)) and re.search(r"[A-Za-z]", m.group(0))
                      else m.group(0), text)


class MaskingFormatter(logging.Formatter):
    def __init__(self, secrets=(), fmt="%(asctime)s %(levelname)s %(name)s: %(message)s"):
        super().__init__(fmt, datefmt="%Y-%m-%d %H:%M:%S")
        self.secrets = tuple(secrets)

    def format(self, record):
        return mask(super().format(record), self.secrets)


def setup_serve_logging(secrets=(), stream=None):
    """Root handler -> stderr, masked and timestamped; ntgcalls INFO, webrtc WARNING."""
    try:
        import ntgcalls  # noqa: F401  (its import resets both loggers to CRITICAL: import first, then set)
    except Exception:
        pass
    h = logging.StreamHandler(stream or sys.stderr)
    h.setFormatter(MaskingFormatter(secrets))
    root = logging.getLogger()
    root.handlers[:] = [h]
    root.setLevel(logging.WARNING)
    logging.getLogger("ntgcalls").setLevel(logging.INFO)
    logging.getLogger("webrtc").setLevel(logging.WARNING)
    logging.getLogger("voice_relay").setLevel(logging.INFO)   # e.g. accept -> connected time of outgoing calls
    return h
