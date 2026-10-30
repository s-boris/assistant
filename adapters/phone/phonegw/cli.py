"""phonegw init|serve|front|public|ping|retention|status. The `phone` CLI (~/bin/phone) is the user-facing door."""
import argparse
import asyncio
import ipaddress
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile

from . import __version__, pki
from . import config as C
from .util import read_json, setup_logging, write_private


# generic defaults, built at run time (the publish scan flags private-range literals): Unraid's WireGuard tunnel
# subnet and Docker's default bridge pool
UNRAID_WG = ".".join(("10", str(250 + 3), "0", "0")) + "/24"
DOCKER_NETS = ".".join(("172", str(8 * 2), "0", "0")) + "/12"

def profile(key):
    try:
        out = subprocess.run(["profile-get", key], capture_output=True, text=True, timeout=10)
        return out.stdout.strip() if out.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def cmd_init(a, paths):
    """Private CA, server cert, internal token and the private config. Prints no secret, only what it did."""
    paths.ensure_dirs()
    cfg = C.load(paths)
    fresh = not os.path.exists(paths.config)
    if fresh or a.server_ip or a.lan_cidr or a.wg_cidr or a.wg_ip:
        lan_prefix = profile("network.lan_prefix")
        server_ip = a.server_ip or (cfg["server_ips"][0] if cfg["server_ips"] else profile("network.server_host"))
        lan = a.lan_cidr or cfg["allow"].get("lan") or (lan_prefix + "0/24" if lan_prefix else None)
        wg = a.wg_cidr or cfg["allow"].get("wg") or profile("network.wg_cidr") or UNRAID_WG   # Unraid default (assumed)
        wg_ip = a.wg_ip or (cfg["server_ips"][1] if len(cfg["server_ips"]) > 1 else str(next(ipaddress.ip_network(wg).hosts())))
        allow = {"loopback": "127.0.0.0/8", "docker": DOCKER_NETS}
        if lan:
            allow["lan"] = lan
        allow["wg"] = wg
        cfg["allow"] = allow
        cfg["server_ips"] = [i for i in (server_ip, wg_ip) if i]
        cfg["urls"] = ["https://%s:%d" % (i, cfg["front_port"]) for i in cfg["server_ips"]]
        cfg["tz"] = profile("locale.tz") or cfg["tz"]
        C.save(paths, cfg)
    did = []
    if pki.init_ca(paths):
        did.append("CA created")
    if pki.issue_server(paths, cfg["server_ips"], cfg["server_dns"], force=a.renew_server):
        did.append("server cert issued (%d IP SANs + %d DNS)" % (len(cfg["server_ips"]) + 1, len(cfg["server_dns"])))
    if not os.path.exists(paths.token):
        write_private(paths.token, secrets.token_urlsafe(32) + "\n")
        did.append("internal token created")
    for f in (paths.ca_key, paths.ca_pem, paths.server_key, paths.server_pem, paths.token, paths.config):
        if os.path.exists(f):
            os.chmod(f, 0o600)
    print("phonegw init: %s. CA sha256 %s…; config %s (0600)." % ("; ".join(did) or "nothing to do",
                                                                    pki.ca_fingerprint(paths)[:16], paths.config))
    return 0


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def cmd_serve(a, paths):
    from .server import serve
    if a.test:
        # Temp state + certs on free loopback ports, for the app's JVM contract test. Prints one JSON line.
        d = a.dir or tempfile.mkdtemp(prefix="phonegw-test-")
        env = {"PHONEGW_STATE": os.path.join(d, "state"), "PHONEGW_SECRETS": os.path.join(d, "secrets"),
               "PHONEGW_CONFIG": os.path.join(d, "config.json"), "PHONEGW_INBOX": os.path.join(d, "inbox"),
               "PHONEGW_PROTOCOL": paths.protocol}
        paths = C.Paths(env)
        paths.ensure_dirs()
        cfg = C.load(paths)
        cfg.update({"tls_port": a.tls_port or 0, "internal_port": 0, "front_port": free_port(), "front": False,
                    "server_ips": ["127.0.0.1"], "urls": ["https://127.0.0.1:0"], "allow": {"loopback": "127.0.0.0/8"},
                    "tz": "Europe/Zurich", "keepalive_s": a.keepalive or 240})
        C.save(paths, cfg)
        pki.init_ca(paths)
        pki.issue_server(paths, ["127.0.0.1"], ["localhost"])
        write_private(paths.token, secrets.token_urlsafe(32) + "\n")
        setup_logging(level=20)

        def ready(gw):
            code = a.pair_code
            import hashlib
            import time as _t
            from .util import gen_code, normalize_code
            code = code or gen_code()
            gw.db.x("INSERT OR REPLACE INTO pair_codes(code_sha256, expires, attempts, used, name) VALUES(?,?,0,0,?)",
                    hashlib.sha256(normalize_code(code).encode()).hexdigest(), int(_t.time()) + 3600, "test")
            print(json.dumps({"tls": "https://127.0.0.1:%d" % gw.ports["tls"], "internal": "http://127.0.0.1:%d" % gw.ports["internal"],
                              "ca_pem": paths.ca_pem, "token_file": paths.token, "pair_code": code, "dir": d,
                              "inbox": paths.inbox}), flush=True)

        asyncio.run(serve(paths, front=False, ready=ready))
        return 0
    if not os.path.exists(paths.token) or not os.path.exists(paths.server_pem):
        print("phonegw: not initialised; run `phonegw init`", file=sys.stderr)
        return 1
    with open(paths.token) as fh:
        tok = fh.read().strip()
    setup_logging([tok], level=20)
    asyncio.run(serve(paths, front=not a.no_front))
    return 0


def serve_pid(paths):
    try:
        pid = int(open(paths.pid).read().strip())
        os.kill(pid, 0)
        with open("/proc/%d/cmdline" % pid, "rb") as fh:
            if b"phonegw" not in fh.read():
                return None
        return pid
    except (OSError, ValueError):
        return None


def listening(port, host="127.0.0.1"):
    try:
        with socket.create_connection((host, int(port)), timeout=1):
            return True
    except OSError:
        return False


def cmd_ping(a, paths):
    """exit 0 when serve runs and its ports answer (front too when wanted or --front)."""
    cfg = C.load(paths)
    pid = serve_pid(paths)
    ok = bool(pid) and listening(cfg["internal_port"]) and listening(cfg["tls_port"])
    want_front = a.front or (cfg["front"] and not os.path.exists(paths.front_off))
    front_ok = listening(cfg["front_port"]) if want_front else None
    if not a.quiet:
        print("serve %s · internal %s · tls %s · front %s" % (
            "pid %d" % pid if pid else "down", "ok" if listening(cfg["internal_port"]) else "down",
            "ok" if listening(cfg["tls_port"]) else "down",
            {None: "off", True: "ok", False: "down"}[front_ok]))
    return 0 if ok and front_ok is not False else 1


def cmd_front(a, paths):
    paths.ensure_dirs()
    if a.state == "off":
        write_private(paths.front_off, "off\n", 0o644)
    elif a.state == "on":
        try:
            os.unlink(paths.front_off)
        except FileNotFoundError:
            pass
    else:
        cfg = C.load(paths)
        on = cfg["front"] and not os.path.exists(paths.front_off)
        print("front: %s (switch), port %s %s" % ("on" if on else "off", cfg["front_port"],
                                                  "listening" if listening(cfg["front_port"]) else "not listening"))
        return 0
    pid = serve_pid(paths)
    if pid:
        os.kill(pid, signal.SIGHUP)
    print("front %s%s" % (a.state, "" if pid else " (serve not running; takes effect at start)"))
    return 0


def cmd_public(a, paths):
    """public on|off|status: the internet path (public.py). on = enabled + the public URL last in `urls` (config_rev
    bump: connected phones get the new list at once, the others on their next connect); off = refuse PROXY connections
    and close live public sockets (the URL stays unless --drop-url)."""
    paths.ensure_dirs()
    cfg = C.load(paths)
    pc = cfg["public"]
    url = C.public_url(cfg)
    if a.state == "status":
        info = pki.public_info(paths)
        print("public: %s · host %s · port %s · proxies %d · site 127.0.0.1:%s %s · url %s · cert %s" % (
            "on" if pc.get("enabled") else "off", pc.get("host") or "-", pc.get("port"), len(pc.get("proxy_ips") or []),
            pc.get("tls_port"), "listening" if listening(pc.get("tls_port")) else "not listening",
            "in urls" if url and url in cfg["urls"] else "not in urls",
            ("%s, %d days left%s" % (",".join(info[0]), info[2], ", IP SANs!" if info[1] else "")) if info else "none"))
        return 0
    disk = read_json(paths.config, {}) or {}            # write on top of the file, so hand edits are kept
    dpc = dict(disk.get("public") or {})
    changed_urls = False
    if a.state == "on":
        if not pc.get("host") or not pc.get("proxy_ips"):
            print("phonegw public: set public.host and public.proxy_ips in %s first" % paths.config, file=sys.stderr)
            return 2
        dpc["enabled"] = True
        urls = list(cfg["urls"])
        if url not in urls:
            urls.append(url)
            changed_urls = True
    else:
        dpc["enabled"] = False
        urls = list(cfg["urls"])
        if a.drop_url and url in urls:
            urls.remove(url)
            changed_urls = True
    disk["public"] = dpc
    if changed_urls:
        disk["urls"] = urls
        disk["config_rev"] = int(cfg.get("config_rev") or 1) + 1
    full = C._merge(C.DEFAULTS, disk)
    C.save(paths, full)
    pid = serve_pid(paths)
    if pid:
        os.kill(pid, signal.SIGHUP)
    print("public %s%s%s" % (a.state, "; urls now %d (config_rev %d)" % (len(urls), full["config_rev"]) if changed_urls else "",
                              "" if pid else " (serve not running; takes effect at start)"))
    return 0


def cmd_retention(a, paths):
    from .db import DB
    cfg = C.load(paths)
    n = DB(paths.db).retention(cfg["retention_days"])
    print("retention: " + ", ".join("%s %d" % kv for kv in n.items() if kv[1]) or "retention: nothing to delete")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="phonegw", description="Phone gateway for the owner's app (version %s)" % __version__)
    sub = p.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("init", help="CA, server cert, internal token, private config (idempotent)")
    i.add_argument("--server-ip")
    i.add_argument("--lan-cidr")
    i.add_argument("--wg-cidr")
    i.add_argument("--wg-ip")
    i.add_argument("--renew-server", action="store_true")
    s = sub.add_parser("serve", help="run the gateway")
    s.add_argument("--no-front", action="store_true")
    s.add_argument("--test", action="store_true", help="temp state/certs on free ports; prints one JSON line")
    s.add_argument("--dir")
    s.add_argument("--pair-code")
    s.add_argument("--tls-port", type=int)
    s.add_argument("--keepalive", type=int)
    f = sub.add_parser("front", help="front on|off|status (off = browser view binds 6080 itself)")
    f.add_argument("state", nargs="?", choices=["on", "off", "status"], default="status")
    u = sub.add_parser("public", help="public on|off|status: the internet path (nginx passthrough + device cert)")
    u.add_argument("state", nargs="?", choices=["on", "off", "status"], default="status")
    u.add_argument("--drop-url", action="store_true", help="off: also remove the public URL from urls (config_rev bump)")
    g = sub.add_parser("ping", help="exit 0 if serve and its ports answer")
    g.add_argument("--front", action="store_true")
    g.add_argument("-q", "--quiet", action="store_true")
    sub.add_parser("retention", help="delete rows past retention (nightly, from the keeper)")
    a = p.parse_args(argv)
    paths = C.Paths()
    return {"init": cmd_init, "serve": cmd_serve, "front": cmd_front, "public": cmd_public, "ping": cmd_ping, "retention": cmd_retention}[a.cmd](a, paths)


if __name__ == "__main__":
    sys.exit(main())
