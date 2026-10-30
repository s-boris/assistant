"""assistant_profile: the owner's shared personal settings, read from one private file.

Code that needs a personal value (the owner's name, home town, server name, LAN prefix, ...) reads it here
instead of hard-coding it. The file is ~/.config/assistant/profile.json (mode 0600, never exported or
published); $ASSISTANT_PROFILE points somewhere else (tests point it at a synthetic fixture profile).
Every key has a public placeholder default (DEFAULTS below), so the code runs without the file and a
published copy behaves exactly as it was reviewed. Keys in the file override the defaults one leaf at a time.

    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "lib"))
    import assistant_profile as profile
    profile.get("home.town")              # "Exampletown" unless the profile says otherwise
    profile.get("network.hosts.web_vm")   # dotted keys reach into sections
    profile.load()                        # the whole merged dict (a copy)
    profile.source()                      # path of the file in use, or None (defaults only)
    profile.tz()                          # the owner's ZoneInfo (locale.tz, else FALLBACK_TZ); never raises
    profile.service_url("radarr", url)    # services.radarr_url when the file sets it, else url (a fallback)
    profile.feature_on("media")           # the feature switch features.media (all on unless the file says no)
    profile.require_feature("media", "media")   # exit 3 with "media: feature media is off (...)" when off

A missing file means defaults. A file that exists but can't be read or parsed, or holds a value of the
wrong type, raises ProfileError (the message names the key, never the value): a broken profile must not
silently turn into placeholders. Callers that must keep running (hooks) catch it and use stricter
fallbacks. Shell scripts use bin/profile-get; for the zone: TZNAME=$(profile-get locale.tz) (for this key it prints tz_name() and never fails). The module is named assistant_profile, not profile, so it
never shadows Python's own `profile` module.
"""
import copy
import json
import os

FALLBACK_TZ = "Europe/Zurich"   # the one zone literal in code (owner-zone lint: profile-tests)

DEFAULTS = {
    "owner": {
        "first_name": "Alex",
        "last_name": "Doe",
        "email": "owner@example.com",
        "phone": "+41 00 000 00 00",
        "birth_year": 1990,
        "github": "example-user",
    },
    "home": {
        "street": "Examplestrasse 1",
        "postcode": "1234",
        "town": "Exampletown",
        "town_aliases": [],
        "canton": "XX",
        "municipality_bfs": 9001,
        "lat": 47.5,
        "lon": 8.5,
    },
    "work": {
        "town": "Worktown",
        "lat": 47.3769,
        "lon": 8.5417,
        "precision": "",                         # "town": only the town is known (drive-watch)
    },
    "locale": {
        "tz": FALLBACK_TZ,
        "lang": "en",
        "country": "CH",
    },
    "network": {
        "server_name": "Homesrv",
        "server_host": "198.51.100.5",
        "lan_prefix": "198.51.100.",
        "domain": "example.net",
        "hosts": {
            "web_vm": "user@198.51.100.3",
        },
    },
    "server": {                                  # host side of the server (claude-hub kit, hubkit.settings())
        "appdata": "/mnt/user/appdata",          # appdata share path on the host (hub data under <appdata>/claude-hub)
        "bot_home": "/mnt/user/appdata/claude-bot/home",  # this container's home as the host sees it
        "bot_network": "claude",                 # docker network the bot container sits on
    },
    "services": {
        "seerr_user": "Alex",
        "ha_user": "Alex",
        "ha_url": "http://198.51.100.7:8123",    # Home Assistant base URL (bin/ha, ha-audit, ha-apply, ...)
        "ha_phone": "phone",                     # HA device slug of the owner's current phone (entity prefix)
        "ha_keep_models": [],                    # phone model codes a cleanup must never touch (his, household)
        # media service base URLs (bin/media, emby, arr-unblock, arr-mismatch, container-watch, unraid-capacity);
        # their keys go by handle through core.egress. Read them with service_url(): a value still at its
        # placeholder counts as unset, so the key file's own url field (while it exists) wins over it.
        "radarr_url": "http://198.51.100.8:7878",
        "sonarr_url": "http://198.51.100.8:8989",
        "seerr_url": "http://198.51.100.8:5055",
        "emby_url": "http://198.51.100.8:8096",
        "bazarr_url": "http://198.51.100.8:6767",
        "sabnzbd_url": "http://198.51.100.8:8080",
    },
    "repos": {
        "assistant": "",                         # where assistant-publish pushes its snapshots (owner/name)
        "private_code": "example-user/assistant-export",   # codebase-sync's sanitized export (owner/name)
        "private_vault": "example-user/privacy-vault",     # vault-git-sync's repo for the vault project (owner/name)
    },
    "interests": {
        "topics": [],
        "events": [],
    },
    # Feature switches {name: bool} (names: FEATURES below). `assistant init` installs a feature's skills, agents and
    # hooks only while it is on, and its tools stop with "feature X is off" (require_feature). A switch the file
    # leaves out is on, so an install without a "features" section works as before; the example profile
    # (docs/profile.example.json) turns on telegram only. core is always on and not a switch.
    "features": {},
}

ENV = "ASSISTANT_PROFILE"
_cache = {}


class ProfileError(Exception):
    pass


def path():
    """The profile file to read: $ASSISTANT_PROFILE, else ~/.config/assistant/profile.json."""
    return os.environ.get(ENV) or os.path.join(os.path.expanduser("~"), ".config/assistant/profile.json")


def _kind(v):
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float)):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "list"
    if isinstance(v, dict):
        return "object"
    return "null" if v is None else type(v).__name__


def _merge(base, over, prefix=""):
    """Deep-merge `over` into a copy of `base`; a leaf must keep the default's type."""
    out = copy.deepcopy(base)
    for k, v in over.items():
        key = prefix + k
        if k.startswith("_"):                       # "_comment" and friends
            continue
        if k in out and isinstance(out[k], dict):
            if not isinstance(v, dict):
                raise ProfileError("profile key %s: expected an object, got %s" % (key, _kind(v)))
            out[k] = _merge(out[k], v, key + ".")
        elif k in out and _kind(out[k]) != _kind(v):
            raise ProfileError("profile key %s: expected %s, got %s" % (key, _kind(out[k]), _kind(v)))
        else:
            out[k] = copy.deepcopy(v)
    return out


def _read():
    p = path()
    try:
        st = os.stat(p)
    except FileNotFoundError:
        return None, None, copy.deepcopy(DEFAULTS)
    except OSError as e:
        raise ProfileError("profile %s unreadable (%s)" % (p, e.__class__.__name__))
    key = (p, st.st_mtime_ns, st.st_size)
    if _cache.get("key") == key:
        return p, key, _cache["data"]
    try:
        with open(p, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError) as e:
        raise ProfileError("profile %s unreadable or not JSON (%s)" % (p, e.__class__.__name__))
    if not isinstance(raw, dict):
        raise ProfileError("profile %s: top level must be an object" % p)
    data = _merge(DEFAULTS, raw)
    _cache.update(key=key, data=data)
    return p, key, data


def load():
    """The merged profile (defaults + file) as a new dict."""
    return copy.deepcopy(_read()[2])


def source():
    """Path of the profile file in use, or None when only the defaults apply."""
    return _read()[0]


_MISSING = object()


def get(key, default=_MISSING):
    """Value at a dotted key. Unknown key: `default` if given, else KeyError."""
    cur = _read()[2]
    for part in key.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif default is not _MISSING:
            return default
        else:
            raise KeyError(key)
    return copy.deepcopy(cur)


def service_url(name, fallback=None):
    """Base URL of service `name` (key services.<name>_url) when the profile file sets one, else `fallback` (e.g. a
    key file's own url field while it still exists), else None. A value still at its placeholder default counts
    as unset. A broken profile raises ProfileError, as get() does."""
    v = get("services.%s_url" % name, None)
    if isinstance(v, str) and v.strip() and v != DEFAULTS["services"].get(name + "_url"):
        return v.strip()
    return fallback or None


FEATURES = ("core", "telegram", "hub", "vault", "home_assistant", "media", "unraid", "phone", "voice", "browser",
            "calendar", "swiss")
FEATURE_OFF_EXIT = 3


def feature_on(name):
    """True while features.<name> is on: core always, a switch the profile leaves out, a name that is no switch
    (nothing to gate). A switch that is not true/false, or a broken profile, raises ProfileError."""
    if name == "core" or name not in FEATURES:
        return True
    v = get("features." + name, True)
    if not isinstance(v, bool):
        raise ProfileError("profile key features.%s: expected bool, got %s" % (name, _kind(v)))
    return v


def features_on():
    """The names of the features that are on, in FEATURES order."""
    return [f for f in FEATURES if feature_on(f)]


def feature_off_message(name, tool=None):
    return "%sfeature %s is off (features.%s in %s)" % (tool + ": " if tool else "", name, name,
                                                        "the profile" if source() is None else source())


def require_feature(name, tool):
    """Stop the calling tool with exit 3 and one clear line on stderr when feature `name` is off. A broken
    profile does not stop it here (the tool's own profile reads report that)."""
    try:
        on = feature_on(name)
    except ProfileError:
        return
    if not on:
        import sys
        print(feature_off_message(name, tool), file=sys.stderr)
        raise SystemExit(FEATURE_OFF_EXIT)


def as_text(v):
    """How profile-get prints a value: strings as they are, everything else as compact JSON."""
    if isinstance(v, str):
        return v
    return json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def keys(d=None, prefix=""):
    """Every leaf key (dotted), sorted."""
    d = _read()[2] if d is None else d
    out = []
    for k, v in d.items():
        if isinstance(v, dict) and v:
            out += keys(v, prefix + k + ".")
        else:
            out.append(prefix + k)
    return sorted(out)


def unknown_keys():
    """Leaf keys present in the file but not in DEFAULTS (allowed, but often a typo)."""
    known = set(keys(DEFAULTS)) | {"features." + f for f in FEATURES if f != "core"}
    known_objs = {k for k in _obj_keys(DEFAULTS)}
    return [k for k in keys() if k not in known and not any(k.startswith(o + ".") for o in known_objs)]


def _obj_keys(d, prefix=""):
    """Dotted keys whose default is an object that may hold free-form entries (e.g. network.hosts)."""
    out = []
    for k, v in d.items():
        if isinstance(v, dict):
            if k == "hosts":
                out.append(prefix + k)
            out += _obj_keys(v, prefix + k + ".")
    return out


def tz_name():
    """The owner's IANA time zone name: locale.tz when it names a zone, else FALLBACK_TZ. Never raises
    (a broken profile or an unknown zone falls back), so clocks, quiet hours and timestamps keep working."""
    try:
        name = get("locale.tz", None)
        if isinstance(name, str) and name:
            from zoneinfo import ZoneInfo
            ZoneInfo(name)
            return name
    except Exception:
        pass
    return FALLBACK_TZ


def tz():
    """The owner's time zone as a ZoneInfo (see tz_name)."""
    from zoneinfo import ZoneInfo
    return ZoneInfo(tz_name())
