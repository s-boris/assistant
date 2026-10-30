"""Paths and the private config (~/.config/phonegw/config.json, 0600). Every personal value lives in that file."""
import copy
import ipaddress
import json
import os

from .util import read_json, write_private

HOME = os.environ.get("HOME") or os.path.expanduser("~")

DEFAULTS = {
    "front": True,
    "front_bind": "0.0.0.0",
    "front_port": 6080,
    "tls_bind": "127.0.0.1",
    "tls_port": 8797,
    "internal_port": 8796,
    "internal_bind": "127.0.0.1",               # claude-hub-phone: 0.0.0.0 (the brain's `phone` reaches it on its network)
    "novnc_port": 6081,
    "novnc_host": "127.0.0.1",                  # claude-hub-phone: the brain container (browser view lives there)
    "allow": {"loopback": "127.0.0.0/8"},     # init adds lan, wg and docker from the profile
    "server_ips": [],
    "server_dns": ["phonegw.home.arpa"],
    "urls": [],
    "tz": "UTC",
    "keepalive_s": 240,
    "quiet": ["00:00", "08:00"],
    "state_every_s": 900,
    "geofence_resp_s": 120,
    "activity": True,
    "trip": "off",
    "config_rev": 1,
    "front_limits": {"max_conn": 64, "per_ip": 20, "window_s": 10, "sniff_timeout_s": 10,
                     "idle_tls_s": 7200, "idle_http_s": 3600},
    "pair": {"code_ttl_s": 600, "max_attempts": 5, "per_ip_per_min": 10},
    "device_cert_days": 730,
    "ttl_s": {"urgent": 1800, "timed": 3600, "decision": 86400, "info": 86400, "chat": 604800, "probe": 120,
              "cancel": 86400, "widget": 604800, "card": 172800, "geofences": 604800, "alarms": 172800},
    "retention_days": {"events": 30, "chat": 30, "push_log": 90, "daily": 90, "inbound": 7, "outbox": 7},
    "chat_plugin": False,                       # true once telegram-plus accepts source:"app" on its inbox
    "chat_plugin_url": "http://127.0.0.1:8792/voice",
    "alarms": "auto",                           # local reminder alarms: auto | off | backup | on (m2.alarms_mode)
    "bridge": True,                             # his taps -> push ack / remind act / talk-button (bridge.py)
    "action_hook": None,                        # extra argv list; gets one JSON action on stdin
    "hook_timeout_s": 20,
    # zones and routines
    "zone_names": {},                           # app zone -> the vault's zone name (defaults: home Home, office Work, ...)
    "routines": {"remind_place": False, "sleepin_release": False, "routine_leaving": True, "talk_button": True,
                 "sleepin_sleep_end": False, "cat_mark": True, "crate_now": True, "card_deck": True},
    # health and NFC
    "health": False,                            # Health Connect summaries accepted (he hasn't agreed yet: off)
    "nfc": {"tags": {}},                        # tag id -> action (m4.NFC_ACTIONS); `phone nfc map` adds to the db
    "flags": {},                                # thresholds for flags.py (S), e.g. ready_min, buds_window_min
    # Guards (guards.py)
    "buds_guard": True,                         # Buds left-behind push (flags.buds_left); no-op until a phone pairs
    "battery_budget": {"reconnects_d": 48, "wakeups_h": 30, "loc_d": 350, "cpu_s_d": 600, "days": 2,
                       "check_at": "07:30", "on": True},   # spec §1.2 self-report; one inbox note per over-budget streak
    "places": {},                               # gym/station/airport: {lat, lon, r_m} or {query}; venue_r_m; walk_to_stop_min
    "widgets_auto": True,                       # refresh board/leg/trip widgets while a device is paired
    "widget_every_s": 300,
    "offline_note_h": 2,
    "remind_db": None,
    "night": "dnd",                             # urgent pushes in quiet hours: dnd (DND decides) | ring | hold
    "share_max_bytes": 2 << 30,
    "share_min_free": 20 << 30,
    # Internet path (public.py): nginx stream passthrough by SNI + PROXY v1 from proxy_ips; off until `phonegw public on`
    "public": {"enabled": False, "host": None, "port": 8443, "proxy_ips": [], "tls_port": 8798, "proxy_timeout_s": 5,
               "limits": {"new_per_ip": 12, "window_s": 60, "conc_per_ip": 6, "conc_total": 16, "handshake_s": 10},
               "ban": {"fails": 5, "window_s": 600, "ban_s": 3600, "trust_s": 86400},
               "deny_paths": ["/v1/pair"], "spike": {"refused_d": 200, "bans_d": 20},
               # usage: one session per stay on the route; a new socket within this many seconds of the last one
               # closing (handover, drop and retry) continues it (server.public_open)
               "session_grace_s": 120},
}


class Paths:
    def __init__(self, env=None):
        env = os.environ if env is None else env
        self.state = env.get("PHONEGW_STATE") or os.path.join(HOME, ".local/state/phonegw")
        self.secrets = env.get("PHONEGW_SECRETS") or os.path.join(HOME, ".secrets/phonegw")
        self.config = env.get("PHONEGW_CONFIG") or os.path.join(HOME, ".config/phonegw/config.json")
        self.inbox = env.get("PHONEGW_INBOX") or os.path.join(HOME, "inbox")
        self.protocol = env.get("PHONEGW_PROTOCOL") or os.path.join(HOME, "opt/phone-app/claude/protocol")
        self.db = os.path.join(self.state, "phonegw.db")
        self.log = os.path.join(self.state, "gw.log")
        self.pid = os.path.join(self.state, "serve.pid")
        self.front_off = os.path.join(self.state, "front.off")
        self.ca_key = os.path.join(self.secrets, "ca.key")
        self.ca_pem = os.path.join(self.secrets, "ca.pem")
        self.server_key = os.path.join(self.secrets, "server.key")
        self.server_pem = os.path.join(self.secrets, "server.pem")
        self.public_key = os.path.join(self.secrets, "public.key")
        self.public_pem = os.path.join(self.secrets, "public.pem")
        self.token = os.path.join(self.secrets, "internal.token")

    def ensure_dirs(self):
        for d in (self.state, self.secrets):
            os.makedirs(d, mode=0o700, exist_ok=True)
            os.chmod(d, 0o700)
        os.makedirs(os.path.dirname(self.config), mode=0o700, exist_ok=True)


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k != "allow":
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load(paths):
    return _merge(DEFAULTS, read_json(paths.config, {}) or {})


def save(paths, cfg):
    """Write only the keys that differ from the defaults (keeps the private file short)."""
    out = {k: v for k, v in cfg.items() if DEFAULTS.get(k) != v}
    write_private(paths.config, json.dumps(out, indent=2, ensure_ascii=False) + "\n")


def allow_networks(cfg):
    nets = []
    for name, cidr in (cfg.get("allow") or {}).items():
        for c in ([cidr] if isinstance(cidr, str) else cidr):
            nets.append((name, ipaddress.ip_network(c, strict=False)))
    return nets


def public_url(cfg):
    """The internet URL the app gets last in `urls` (None while no public host is configured)."""
    pc = cfg.get("public") or {}
    return "https://%s:%d" % (pc["host"], int(pc.get("port") or 443)) if pc.get("host") else None


def app_config(cfg):
    """The `config` body the app gets (s2c.config.json)."""
    return {"rev": int(cfg["config_rev"]), "keepalive_s": int(cfg["keepalive_s"]), "quiet": list(cfg["quiet"]),
            "state_every_s": int(cfg["state_every_s"]), "geofence_resp_s": int(cfg["geofence_resp_s"]),
            "activity": bool(cfg["activity"]), "trip": cfg["trip"], "night": cfg.get("night", "dnd"), "health": bool(cfg.get("health")), "urls": list(cfg["urls"]) or ["https://127.0.0.1:%d" % cfg["front_port"]]}
