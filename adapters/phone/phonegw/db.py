"""SQLite store (~/.local/state/phonegw/phonegw.db, 0600, WAL). Outbox times: created/sent/acked in epoch ms,
expires in epoch s. Bodies are set to NULL once acked or expired, so no pushed text is kept."""
import json
import os
import sqlite3
import time

from .util import now_ms, now_s

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices(id TEXT PRIMARY KEY, name TEXT, model TEXT, cert_sha256 TEXT UNIQUE, bio_pub TEXT,
                     created INT, last_seen INT, app_code INT, status TEXT CHECK(status IN ('active','revoked')));
CREATE TABLE IF NOT EXISTS pair_codes(code_sha256 TEXT PRIMARY KEY, expires INT, attempts INT DEFAULT 0, used INT DEFAULT 0,
                     name TEXT);
CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY, device_id TEXT, t TEXT, key TEXT, body TEXT, created INT, expires INT,
                    sent INT, acked INT, ack INT DEFAULT 1);
CREATE INDEX IF NOT EXISTS outbox_pending ON outbox(device_id, acked, expires);
CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, device_id TEXT, kind TEXT, at INT, body TEXT, received INT);
CREATE TABLE IF NOT EXISTS state(device_id TEXT, k TEXT, v TEXT, at INT, PRIMARY KEY(device_id, k));
CREATE TABLE IF NOT EXISTS daily(device_id TEXT, day TEXT, metric TEXT, v TEXT, PRIMARY KEY(device_id, day, metric));
CREATE TABLE IF NOT EXISTS push_log(id TEXT PRIMARY KEY, at INT, level TEXT, tag TEXT, delivered_ms INT, acked_ms INT, verb TEXT,
                      device_id TEXT, nonce TEXT, verbs TEXT, arg TEXT, action_at INT);
CREATE TABLE IF NOT EXISTS chat(id TEXT PRIMARY KEY, at INT, dir TEXT, text TEXT, reply_to TEXT);
CREATE TABLE IF NOT EXISTS cards(id TEXT PRIMARY KEY, kind TEXT, body TEXT, created INT, expires INT, answer TEXT, answered INT);
CREATE TABLE IF NOT EXISTS shares(id TEXT PRIMARY KEY, at INT, kind TEXT, path TEXT, bytes INT, mime TEXT);
CREATE TABLE IF NOT EXISTS held(id TEXT PRIMARY KEY, device_id TEXT, key TEXT, body TEXT, ttl INT, created INT, release INT);
CREATE TABLE IF NOT EXISTS inbound(id TEXT PRIMARY KEY, device_id TEXT, t TEXT, at INT);
"""


class DB:
    def __init__(self, path):
        new = not os.path.exists(path)
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        if new:
            os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
        self.c = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=10)
        self.c.row_factory = sqlite3.Row
        self.c.execute("PRAGMA journal_mode=WAL")
        self.c.execute("PRAGMA synchronous=NORMAL")
        self.c.executescript(SCHEMA)
        for side in ("-wal", "-shm"):
            if os.path.exists(path + side):
                os.chmod(path + side, 0o600)
        os.chmod(path, 0o600)

    def x(self, sql, *args):
        return self.c.execute(sql, args)

    def one(self, sql, *args):
        return self.c.execute(sql, args).fetchone()

    def all(self, sql, *args):
        return self.c.execute(sql, args).fetchall()

    # ------------------------------------------------------------ devices
    def device_by_fp(self, fp):
        return self.one("SELECT * FROM devices WHERE cert_sha256=?", fp)

    def device(self, dev_id):
        return self.one("SELECT * FROM devices WHERE id=?", dev_id)

    def active_devices(self):
        return self.all("SELECT * FROM devices WHERE status='active' ORDER BY created")

    def devices(self):
        return self.all("SELECT * FROM devices ORDER BY created")

    # ------------------------------------------------------------ outbox
    def enqueue(self, dev_id, msg_id, t, body, ttl, key=None, ack=True, created=None):
        created = created or now_ms()
        if key:  # coalescing: a newer message with the same key replaces the older unacked one
            self.x("DELETE FROM outbox WHERE device_id=? AND key=? AND acked IS NULL", dev_id, key)
        self.x("INSERT INTO outbox(id, device_id, t, key, body, created, expires, sent, acked, ack) VALUES(?,?,?,?,?,?,?,?,?,?)",
               msg_id, dev_id, t, key, json.dumps(body, ensure_ascii=False), created, created // 1000 + int(ttl), None, None,
               1 if ack else 0)
        return msg_id

    def pending(self, dev_id, after=None, limit=200):
        now = now_s()
        if after:
            row = self.one("SELECT rowid FROM outbox WHERE id=? AND device_id=?", after, dev_id)
            if row:
                return self.all("SELECT * FROM outbox WHERE device_id=? AND acked IS NULL AND expires>? AND body IS NOT NULL "
                                "AND rowid>? ORDER BY rowid LIMIT ?", dev_id, now, row[0], limit)
        return self.all("SELECT * FROM outbox WHERE device_id=? AND acked IS NULL AND expires>? AND body IS NOT NULL "
                        "ORDER BY rowid LIMIT ?", dev_id, now, limit)

    def pending_count(self, dev_id):
        return self.one("SELECT COUNT(*) n FROM outbox WHERE device_id=? AND acked IS NULL AND expires>? AND body IS NOT NULL",
                        dev_id, now_s())["n"]

    def mark_sent(self, msg_id, at=None):
        self.x("UPDATE outbox SET sent=COALESCE(sent, ?) WHERE id=?", at or now_ms(), msg_id)

    def ack(self, dev_id, ids, at=None):
        """-> rows newly acked (id, t, created, sent)."""
        at = at or now_ms()
        out = []
        for i in ids:
            r = self.one("SELECT id, t, key, created, sent FROM outbox WHERE id=? AND device_id=? AND acked IS NULL", i, dev_id)
            if r:
                self.x("UPDATE outbox SET acked=?, body=NULL WHERE id=?", at, i)
                out.append(r)
            elif not self.one("SELECT 1 FROM outbox WHERE id=?", i):
                continue
        return out

    def drop_unsent_tag(self, key):
        """push.cancel: queued pushes with that key that the phone never got are dropped."""
        return self.x("UPDATE outbox SET acked=-1, body=NULL WHERE key=? AND acked IS NULL AND sent IS NULL", key).rowcount

    def expire(self):
        return self.x("UPDATE outbox SET body=NULL WHERE acked IS NULL AND expires<=? AND body IS NOT NULL", now_s()).rowcount

    # ------------------------------------------------------------ state
    def put_state(self, dev_id, k, v, at=None):
        self.x("INSERT INTO state(device_id, k, v, at) VALUES(?,?,?,?) ON CONFLICT(device_id, k) DO UPDATE SET v=excluded.v, at=excluded.at",
               dev_id, k, json.dumps(v, ensure_ascii=False), at or now_s())

    def get_state(self, dev_id, k, default=None):
        r = self.one("SELECT v, at FROM state WHERE device_id=? AND k=?", dev_id, k)
        return (json.loads(r["v"]), r["at"]) if r else (default, None)

    def seen_inbound(self, msg_id, dev_id, t):
        """True if this inbound id was already processed (de-duplication, 7 d)."""
        try:
            self.x("INSERT INTO inbound(id, device_id, t, at) VALUES(?,?,?,?)", msg_id, dev_id, t, now_s())
            return False
        except sqlite3.IntegrityError:
            return True

    # ------------------------------------------------------------ retention
    def retention(self, days):
        now = now_s()
        d = lambda k: now - int(days.get(k, 30)) * 86400  # noqa: E731
        n = {}
        n["events"] = self.x("DELETE FROM events WHERE received<?", d("events")).rowcount
        n["chat"] = self.x("DELETE FROM chat WHERE at<?", d("chat")).rowcount
        n["push_log"] = self.x("DELETE FROM push_log WHERE at<?", d("push_log") * 1000).rowcount
        n["daily"] = self.x("DELETE FROM daily WHERE day<?", time.strftime("%Y-%m-%d", time.gmtime(d("daily")))).rowcount
        n["inbound"] = self.x("DELETE FROM inbound WHERE at<?", d("inbound")).rowcount
        n["outbox"] = self.x("DELETE FROM outbox WHERE body IS NULL AND created<?", d("outbox") * 1000).rowcount
        n["pair_codes"] = self.x("DELETE FROM pair_codes WHERE expires<?", now - 86400).rowcount
        self.expire()
        self.x("PRAGMA wal_checkpoint(TRUNCATE)")
        return n
