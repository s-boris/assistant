"""Privacy classes, second layer behind the per-kind JSON Schemas (spec §1.1): C2 raw data (coordinates, SSIDs, BT
addresses, raw health samples) and C3 content (notification/message text) never enter the gateway. The schemas already
reject unknown keys; this layer also rejects raw keys at any depth and string values that smuggle raw data (coordinate
pairs, MAC addresses, URLs, long free text). Error messages name the path and rule only, never the value."""
import re

RAW_KEYS = {"lat", "lon", "lng", "latitude", "longitude", "coords", "coordinates", "location", "accuracy", "altitude",
            "ssid", "bssid", "mac", "address", "text", "title", "body", "message", "content", "sender", "contact",
            "phone", "number", "hr_samples", "samples", "spo2", "trail", "track", "notification", "notifications"}
SKIP_KEYS = {"q_id", "open_id"}                      # server-made ids (ULIDs may hold digit runs)
CHECKED = {"state", "event", "answer"}      # c2s types carrying C0/C1 data
MAX_STR = 64                                # longest C1 string (town / stop names)
COORD = re.compile(r"-?\d{1,3}\.\d{3,}\s*[,;/ ]\s*-?\d{1,3}\.\d{3,}")
MAC = re.compile(r"(?i)\b(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}\b")
URL = re.compile(r"(?i)\b(?:https?|geo|content|file)://|\bgeo:")
DIGITS = re.compile(r"\d{7,}")              # phone numbers, account ids


def problems(body, prefix="body"):
    """-> list of 'path: rule' strings (empty = clean)."""
    out = []

    def walk(v, path):
        if len(out) >= 10:
            return
        if isinstance(v, dict):
            for k, x in v.items():
                if k in SKIP_KEYS:
                    continue
                if str(k).lower() in RAW_KEYS:
                    out.append("%s: raw key %s (C2/C3 data stays on the phone)" % (path, str(k)[:24]))
                else:
                    walk(x, "%s.%s" % (path, str(k)[:24]))
        elif isinstance(v, list):
            for i, x in enumerate(v[:50]):
                walk(x, "%s.%d" % (path, i))
        elif isinstance(v, str):
            if len(v) > MAX_STR:
                out.append("%s: string longer than %d (free text is not accepted)" % (path, MAX_STR))
            elif COORD.search(v):
                out.append("%s: looks like coordinates" % path)
            elif MAC.search(v):
                out.append("%s: looks like a hardware address" % path)
            elif URL.search(v):
                out.append("%s: looks like a link" % path)
            elif DIGITS.search(v):
                out.append("%s: long digit run" % path)

    walk(body, prefix)
    return out


# Coarse place names (C1): place.town must read as a locality, place.stop as a public stop. Street addresses never
# pass: the public stop lookup also answers with the street address at the query point ("<street> <no>, <town>").
# These are blanked (not rejected) so the rest of the frame still lands; the caller logs the count, never the value.
STREET_END = re.compile(r"(?i)(?:strasse|straße|gasse|weg|allee|platz|quai)(?![^\W\d_])|\bstr\.")
STREET_START = re.compile(r"(?i)^(?:rue|chemin|ch\.|avenue|av\.|route|rte\.?|via|viale|piazza|corso|place)\s")
ANY_DIGIT = re.compile(r"\d")


def street_like(s):
    """A town that is not a plain locality: digits (house number, postcode), a comma or a street word."""
    s = str(s).strip()
    return bool(ANY_DIGIT.search(s) or "," in s or STREET_END.search(s) or STREET_START.search(s))


def address_like(s):
    """A stop name with a house number or postcode (public stop names may carry a street name, never a number)."""
    return bool(ANY_DIGIT.search(str(s)))


def scrub_places(body):
    """Blank street-like `town` (any depth) and address-like `stop` inside a `place` object, in place. -> count."""
    n = 0

    def walk(v, in_place=False):
        nonlocal n
        if isinstance(v, dict):
            for k in list(v):
                x = v[k]
                if k == "town" and isinstance(x, str) and street_like(x):
                    v[k] = None
                    n += 1
                elif k == "stop" and in_place and isinstance(x, str) and address_like(x):
                    v[k] = None
                    n += 1
                else:
                    walk(x, k == "place")
        elif isinstance(v, list):
            for x in v:
                walk(x, in_place)

    walk(body)
    return n

