"""App `usage` frames -> the product-analytics spool (~/bin/lib/usage.py; folded into usage.db by `usage tick`).

Each item becomes one spool line through the lib's own sanitizer (`record`: feature ids, kinds and short tokens only,
never text). Kinds map onto the store's: view on a notification = notify_sent (the app posted it), other views =
invoke (a screen visit is a use of that screen); open on a notification = notify_opened, else tap; dismiss = dismissed.
app.route.<reason> (the app's route switches: outcome home|tunnel|internet, ms = network change -> socket open) and
app.call.link (a call's connection dropped: outcome resumed|switched|lost, ms = the gap) are recorded with actor
system, so they never count as his use.
ext_id = pg:u:<frame id>:<n>, so a re-sent frame never counts twice. Writing is a short O_APPEND file write, run off
the event loop; any failure is logged and dropped (analytics never cost a frame or an ack).
"""
import importlib.util
import json
import logging
import os
import time

log = logging.getLogger("phonegw.usage")
SYSTEM_PREFIXES = ("app.route.", "app.call.link")      # the app's own route moves: the system acted, not him

LIB = os.environ.get("PHONEGW_USAGE_LIB") or os.path.expanduser("~/bin/lib/usage.py")
KINDS = {"invoke": "invoke", "tap": "tap", "dismiss": "dismissed", "error": "error"}


def _load():
    try:
        spec = importlib.util.spec_from_file_location("phonegw_usage_lib", LIB)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception as e:                       # no lib: the gateway runs on, analytics are off
        log.warning("usage lib not loaded (%s): app usage frames are dropped", type(e).__name__)
        return None


def store_kind(kind, surface):
    if kind == "view":
        return "notify_sent" if surface == "notify" else "invoke"
    if kind == "open":
        return "notify_opened" if surface == "notify" else "tap"
    return KINDS.get(kind)


class UsageForwarder:
    def __init__(self, state=None, enabled=True):
        self.lib = _load() if enabled else None
        self.state = state                       # None = the lib's own (USAGE_STATE or ~/.local/state/product)
        self.n = 0

    def spool_dir(self):
        return os.path.join(self.state, "spool") if self.state else self.lib.SPOOL

    def records(self, env_id, body):
        """-> spool records (dicts) for one usage frame; pure, for tests."""
        out, app = [], body.get("app")
        for i, it in enumerate(body.get("items") or []):
            surface = it.get("surface")
            kind = store_kind(it.get("kind"), surface)
            if not kind:
                continue
            attrs = {"surface": surface, "level": it.get("level"), "app": app}
            if it.get("kind") == "view" and surface != "notify":
                attrs["view"] = True
            # app.route.* = the app's own route switches (home/tunnel/internet): the system acted, not him
            system = str(it.get("feature") or "").startswith(SYSTEM_PREFIXES)
            rec = self.lib.record(it.get("feature"), kind, channel="app",
                                  actor="system" if system else "assistant" if kind == "notify_sent" else "owner",
                                  ms=it.get("ms"), outcome=it.get("outcome"), attrs=attrs, ts=it.get("at"),
                                  src="app", ext_id="pg:u:%s:%d" % (env_id, i))
            if rec:
                out.append(rec)
        dropped = body.get("dropped") or 0
        if dropped:
            rec = self.lib.record("app", "error", channel="app", actor="system", outcome="usage_capped",
                                  attrs={"n": int(dropped), "app": app}, src="app", ext_id="pg:u:%s:cap" % env_id)
            if rec:
                out.append(rec)
        return out

    def event(self, feature, kind="invoke", outcome=None, ms=None, actor="owner"):
        """One gateway-side event (e.g. phonegw.public connect/disconnect: no IPs, no text) -> the spool. Never raises."""
        if self.lib is None or self.lib.off():
            return 0
        try:
            rec = self.lib.record(feature, kind, channel="app", actor=actor, ms=ms, outcome=outcome)
            return self._append([rec])
        except Exception as e:
            log.warning("usage event %s not spooled (%s)", feature, type(e).__name__)
            return 0

    def _append(self, recs):
        d = self.spool_dir()
        os.makedirs(d, mode=0o700, exist_ok=True)
        path = os.path.join(d, "app-%s.jsonl" % time.strftime("%Y%m%d"))
        data = "".join(json.dumps(r, separators=(",", ":"), ensure_ascii=True) + "\n" for r in recs)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, data.encode())
        finally:
            os.close(fd)
        self.n += len(recs)
        return len(recs)

    def forward(self, env_id, body):
        """Append the frame's events to the spool; -> number written. Never raises."""
        if self.lib is None or self.lib.off():
            return 0
        try:
            recs = self.records(env_id, body)
            if not recs:
                return 0
            d = self.spool_dir()
            os.makedirs(d, mode=0o700, exist_ok=True)
            path = os.path.join(d, "app-%s.jsonl" % time.strftime("%Y%m%d"))
            data = "".join(json.dumps(r, separators=(",", ":"), ensure_ascii=True) + "\n" for r in recs)
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, data.encode())
            finally:
                os.close(fd)
            self.n += len(recs)
            return len(recs)
        except Exception as e:
            log.warning("usage frame %s not spooled (%s)", env_id, type(e).__name__)
            return 0
