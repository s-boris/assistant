"""inbox.brainwatch: `brain-watch`, the Telegram inbound watchdog on events.db facts.

  brain-watch [--cron] [--json]    one check (cron, every minute), live: the acts are done (state brain-watch.json).
                                   Kill switch ~/.config/assistant/brain-watch-off = nothing at all
  brain-watch --dry-run [--json]   one check that only says what it would do: real decisions on
                                   live facts from the live state; every act is logged ("DRY-RUN would …"), none is
                                   done and no state is saved
  brain-watch --status [--json]    what it sees: mode, Telegram mode, session, bridge lease, rows, incidents

Facts: owner rows of receipts.RECEIPT_KINDS in events.db and their deliveries receipts, outbox replies
(reply_to_event), the main session's state (`claude agents --json` every run: agent view must stay on), its
transcript, the bridge lease (~/.local/state/tg-bridge/lease: the holder's pid, version and capabilities), and
start-claude's telegram-mode and bridge.log (~/.local/state/start-claude).
Watches:
  stall        an owner row with no receipt for 3 min (the transcript first: found there = a transcript receipt,
               written when live), session idle (or unknown): ask the bridge to redeliver; still none 3 min later: one
               notice, saying whether the bridge re-sent it or never took the request; asked again every 3 min, at
               most 3 times. Busy or on a dialog: one notice after 10 min. Not while the session is gone, starting
               or without its bridge (their own watches).
  api_error    a received owner message/tap with no reply whose turn died on a fatal API error (Claude Code's own
               error entries after its line in the transcript), session idle: redeliver once; the same failure after
               that: one notice
  session_down the main session missing from `claude agents` for 5 min
  bridge_down  the session up (past its first 3 min) with no live bridge lease for 5 min
  agents       `claude agents --json` failing for 15 min (brain-watch can't see the session; never "down")
  fallback     start-claude fell back to the official plugin (a bridge.log "fallback:" line with no "bridge start"
               after it, and its kill switch ~/.telegram-official there; telegram-mode says official only from the next
               start on): one notice per fallback, unless start-claude logged "fallback texted". The kill switch set
               by hand (no fallback line) is not news. Official mode or a fallback: the bridge watches stand down.
  In poll mode (the 0.2.0 poll path) channel-check watches: brain-watch stands down.
Acts (live):
  redeliver    meta 'bridge.redeliver' = {"rid", "ids", "why", "ts"} (one per run, every id wanted); a bridge whose
               lease says "redeliver" (telegram-plus 0.3.0) re-sends the rows still without a receipt (redelivered="1")
               and answers in meta 'bridge.redeliver.ack' = {"rid", "at", "sent", "skipped"}
  notice       to him without the session: the live adapter's outbox (an urgent row when a message of his waits: the
               adapter sends it at once), the app push (`phone push`) as the backup, the image's direct tg last. One per
               incident; "back to normal" only after one that went out (a notice still held by the adapter is withdrawn
               instead); 6 per 6 h; an incident back within 30 min of its end stays quiet; notices with no message of
               his waiting wait out quiet hours (0-8 on his clock) and are dropped if the incident ends meanwhile; a
               failed send is tried again on the next runs, 3 times in all.
  restart      only with ~/.config/assistant/brain-watch-restart (default off): `restart "auto-recover: …"` for a stall
               or a missing bridge in an idle session, or a session that is gone (1 per 15 min, 3 per 6 h, 2 per
               incident). Off: the decision is logged as "no restart: switch off".
Log: ~/.local/state/assistant/brain-watch.log (ids, kinds, ages, notice texts; never his text). One run at a time.
Hub mode: the owner rows, receipts and outbox replies are read from the hub (receipts.undelivered_hub, the
hub's received_unanswered read) and transcript receipts go there (receipts.mark_hub); a hub that does not answer
reads as "nothing pending" for that run. The redelivery request and the bridge's ack stay in the local SQLite meta.
Env (tests): BW_AGENTS_JSON (a file standing in for `claude agents --json`; required under ASSISTANT_TEST),
BW_WORKSPACE (the session's cwd), BW_BRIDGE_LEASE, BW_START_STATE (start-claude's state dir), BW_PHONE_BIN,
BW_TG_DIRECT, BW_RESTART_BIN, BW_QUIET ("0-8"; "" = none), BW_OUTBOX_WAIT (s), BW_DRY_STATE (a dry run's own state
file, read and saved, so a sequence of dry runs plays out as it would live), ASSISTANT_PROJECT_DIR, ASSISTANT_NOW,
ASSISTANT_TZ. Under ASSISTANT_TEST a sender without its seam is never run.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import zlib
from collections.abc import Callable
from datetime import datetime, tzinfo
from typing import Any

from core import clock, db, events, jsonstate, locks, paths, tg, todo

from . import receipts, sources

__all__ = ["ACK_KEY", "REQUEST_KEY", "Watch", "fatal_api_error", "main"]

STALL_S = 180
LONG_WAIT_S = 600
REPLAY_WAIT_S = 180
REPLAY_MAX = 3
STARTING_S = 180
DOWN_S = 300
AGENTS_BLIND_S = 900
FALLBACK_GRACE_S = 120
HORIZON_S = 6 * 3600
RESTART_GAP_S = 900
RESTART_WINDOW_S = 6 * 3600
RESTART_MAX = 3
RESTART_PER_INCIDENT = 2
NOTICE_WINDOW_S = 6 * 3600
NOTICE_MAX = 6
NOTICE_RETRIES = 3
REOPEN_QUIET_S = 1800
OUTBOX_WAIT_S = 15.0
PUSH_WAIT_S = 20
SENDER = "brain-watch"
PUSH_TITLE = "Telegram watchdog"
REQUEST_KEY = "bridge.redeliver"
ACK_KEY = "bridge.redeliver.ack"
TAIL = 8 << 20
ANSWERABLE = ("tg.message", "tg.button", "bridge.handoff")
FATAL_API = (
    (re.compile(r"safeguards flagged", re.I), "Opus safety filter false alarm", "#101715"),
    (
        re.compile(r"system content must contain at least one block", re.I),
        "broken request after a compaction",
        "#101181",
    ),
)
FATAL_PREFILTER = re.compile(rb'safeguards flagged|system content must contain|"stop_reason":"refusal"', re.I)
_LOGLINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (.*)$")


def fatal_api_error(o: Any) -> tuple[str, str] | None:
    """(cause, issue) for Claude Code's own record of a fatal API error (synthetic assistant message, system
    api_error entry, a stop_reason refusal); conversation text about the errors never matches."""
    if not isinstance(o, dict):
        return None
    t = o.get("type")
    msg = o.get("message")
    m: dict[str, Any] = msg if isinstance(msg, dict) else {}
    if t == "assistant" and m.get("stop_reason") == "refusal":
        return FATAL_API[0][1], FATAL_API[0][2]
    if t == "assistant" and (o.get("isApiErrorMessage") or m.get("model") == "<synthetic>"):
        text = json.dumps(m.get("content"))
    elif t == "system" and (o.get("subtype") == "api_error" or o.get("level") == "error"):
        text = json.dumps([o.get("content"), o.get("error")])
    else:
        return None
    for rx, cause, issue in FATAL_API:
        if rx.search(text):
            return cause, issue
    return None


def _tail(path: str | None, limit: int = TAIL) -> list[bytes]:
    if not path:
        return []
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - limit))
            data = f.read()
    except OSError:
        return []
    if size > limit:
        data = data.split(b"\n", 1)[-1]
    return data.split(b"\n")


def _bin(env: str, default: str) -> str | None:
    """A sender's executable: its seam, else the live default (never under ASSISTANT_TEST)."""
    v = os.environ.get(env, "")
    if v:
        return v
    return None if paths.test_mode() else default


def _run(exe: str | None, args: list[str], timeout: float) -> int:
    """Exit code (127 none or missing, 124 timeout); output discarded."""
    if not exe:
        return 127
    try:
        return subprocess.run(
            [exe, *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        ).returncode
    except subprocess.TimeoutExpired:
        return 124
    except OSError:
        return 127


def _num(env: str, default: float) -> float:
    try:
        return float(os.environ.get(env, "") or default)
    except ValueError:
        return default


def _his_zone(now: float, home: tzinfo) -> tzinfo:
    """The zone he is in (~/bin/lib/tripclock: a trip's zone while away), else his home zone."""
    lib = os.fspath(paths.bin_dir("lib"))
    if not os.path.isfile(os.path.join(lib, "tripclock.py")):
        return home
    try:
        if lib not in sys.path:
            sys.path.append(lib)
        import tripclock  # type: ignore[import-not-found]

        z = tripclock.zone(now, home)
        return z if isinstance(z, tzinfo) else home
    except Exception:  # noqa: BLE001 - his home zone is the safe answer
        return home


class Facts:
    def __init__(self, con: sqlite3.Connection | None, now: float) -> None:
        self.con, self.now = con, now
        self.workspace = os.environ.get("BW_WORKSPACE") or os.fspath(paths.home() / "workspace")
        self._session: tuple[str, dict[str, Any] | None] | None = None
        self._lines: list[bytes] | None = None
        self._pending: list[dict[str, Any]] | None = None

    def pending(self) -> list[dict[str, Any]]:
        """Owner rows with no receipt for STALL_S (within HORIZON_S), oldest first."""
        if self._pending is None:
            if receipts._hub_tg():
                try:
                    rows = receipts.undelivered_hub(self.now, STALL_S)
                except Exception:  # noqa: BLE001 - the hub down: nothing to judge this run
                    rows = []
            else:
                rows = receipts.undelivered(self.con, self.now, STALL_S) if self.con is not None else []
            self._pending = [r for r in rows if r["ts"] > self.now - HORIZON_S]
        return self._pending

    def received_unanswered(self) -> list[dict[str, Any]]:
        if receipts._hub_tg():
            from core import hub

            try:
                rows = hub.tg_q(
                    "received_unanswered",
                    kinds=list(ANSWERABLE),
                    after_ts=self.now - HORIZON_S,
                    before_ts=self.now - STALL_S,
                    timeout=3.0,
                )
            except hub.HubError:
                return []
            return [{"id": int(r["id"]), "kind": r["kind"], "ts": float(r["ts"])} for r in rows]
        if self.con is None or not receipts.has_deliveries(self.con):
            return []
        q = ",".join("?" * len(ANSWERABLE))
        try:
            rows = self.con.execute(
                f"SELECT e.id, e.kind, e.ts FROM events e JOIN deliveries d ON d.event_id=e.id"
                f" WHERE e.kind IN ({q}) AND d.receipt_ts IS NOT NULL AND e.ts > ? AND e.ts <= ?"
                f" AND NOT EXISTS (SELECT 1 FROM outbox o WHERE o.reply_to_event=e.id) ORDER BY e.ts",
                (*ANSWERABLE, self.now - HORIZON_S, self.now - STALL_S),
            ).fetchall()
        except sqlite3.OperationalError:  # outbox without reply_to_event (schema 1, read-only)
            return []
        return [dict(r) for r in rows]

    def agents(self) -> list[dict[str, Any]] | None:
        """`claude agents --json` (or BW_AGENTS_JSON); None when it fails: unknown, never "absent"."""
        try:
            f = os.environ.get("BW_AGENTS_JSON")
            if f:
                with open(f, encoding="utf-8") as fh:
                    raw = fh.read()
            elif paths.test_mode():
                return None
            else:
                exe = shutil.which("claude") or os.fspath(paths.home() / ".local" / "bin" / "claude")
                p = subprocess.run([exe, "agents", "--json"], capture_output=True, text=True, timeout=30, check=False)
                if p.returncode != 0:
                    return None
                raw = p.stdout
            data = json.loads(raw)
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
        return [a for a in data if isinstance(a, dict)] if isinstance(data, list) else None

    def session(self) -> tuple[str, dict[str, Any] | None]:
        """idle | busy | waiting | starting | absent | unknown (`claude agents` failed)."""
        if self._session is None:
            agents = self.agents()
            if agents is None:
                self._session = ("unknown", None)
                return self._session
            cands = [a for a in agents if a.get("cwd") == self.workspace]
            s = next((a for a in cands if a.get("kind") == "interactive"), None) or (cands[0] if cands else None)
            if not s:
                self._session = ("absent", None)
            elif (s.get("startedAt") or 0) / 1000 > self.now - STARTING_S:
                self._session = ("starting", s)
            elif s.get("status") in ("waiting", "busy"):
                self._session = (str(s["status"]), s)
            else:
                self._session = ("idle", s)
        return self._session

    def transcript(self) -> str | None:
        pd = os.fspath(sources.project_dir())
        _, s = self.session()
        if s and s.get("sessionId"):
            p = os.path.join(pd, f"{s['sessionId']}.jsonl")
            if os.path.exists(p):
                return p
        try:
            files = [os.path.join(pd, f) for f in os.listdir(pd) if f.endswith(".jsonl")]
        except OSError:
            return None
        return max(files, key=os.path.getmtime) if files else None

    def lines(self) -> list[bytes]:
        if self._lines is None:
            self._lines = _tail(self.transcript())
        return self._lines

    def seen_at(self, ids: list[int]) -> dict[int, int]:
        """{event id: index of its newest user/attachment line carrying event_id="N"} in the transcript tail."""
        lines = self.lines()
        out: dict[int, int] = {}
        for eid in ids:
            key = f'event_id=\\"{eid}\\"'.encode()
            for i in range(len(lines) - 1, -1, -1):
                if key in lines[i]:
                    try:
                        if json.loads(lines[i]).get("type") in ("user", "attachment"):
                            out[eid] = i
                            break
                    except ValueError:
                        pass
        return out

    def api_errors(self, at: dict[int, int]) -> dict[int, tuple[str, str, str]]:
        """{event id: (cause, issue, key)} for the last fatal API error entry after the event's newest line."""
        if not at:
            return {}
        lines = self.lines()
        errs = []
        for i in range(min(at.values()) + 1, len(lines)):
            if FATAL_PREFILTER.search(lines[i]):
                try:
                    o = json.loads(lines[i])
                except ValueError:
                    continue
                hit = fatal_api_error(o)
                if hit:
                    errs.append((i, hit, str(o.get("uuid") or o.get("timestamp") or zlib.crc32(lines[i]))))
        out = {}
        for eid, idx in at.items():
            after = [(hit, key) for i, hit, key in errs if i > idx]
            if after:
                out[eid] = (*after[-1][0], after[-1][1])
        return out

    def lease(self) -> dict[str, Any] | None:
        """The bridge lease record while its holder runs (the file outlives the holder: the pid decides)."""
        p = os.environ.get("BW_BRIDGE_LEASE") or os.fspath(paths.home() / ".local" / "state" / "tg-bridge" / "lease")
        try:
            with open(p, encoding="utf-8") as f:
                d = json.loads(f.read(4096) or "{}")
            pid = int(d.get("pid") or 0)
        except (OSError, ValueError, TypeError, AttributeError):
            return None
        if pid <= 1:
            return None
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return None
        except PermissionError:
            pass
        return d

    def _start_state(self, name: str) -> str:
        base = os.environ.get("BW_START_STATE") or os.fspath(paths.home() / ".local" / "state" / "start-claude")
        return os.path.join(base, name)

    def tg_mode(self) -> str:
        """start-claude's record of this start: bridge | official | poll ("" = none yet)."""
        try:
            with open(self._start_state("telegram-mode"), encoding="utf-8") as f:
                return f.read(64).strip()
        except OSError:
            return ""

    def fallback(self) -> dict[str, Any] | None:
        """start-claude's last fallback in bridge.log, {"ts", "why", "texted"}; None after a later bridge start."""
        fb: dict[str, Any] | None = None
        for raw in _tail(self._start_state("bridge.log"), 64 << 10):
            m = _LOGLINE.match(raw.decode("utf-8", "replace"))
            if not m:
                continue
            try:
                ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp()  # local time, as `date` wrote it
            except ValueError:
                continue
            rest = m.group(2)
            if rest.startswith("fallback texted"):
                if fb:
                    fb["texted"] = True
            elif rest.startswith("fallback: "):
                fb = {"ts": ts, "why": rest[len("fallback: ") :][:120], "texted": False}
            elif rest.startswith("bridge start"):
                fb = None
        return fb

    def official(self) -> bool:
        """start-claude's kill switch: the official plugin from the next start on."""
        return (paths.home() / ".telegram-official").exists()

    def ack(self) -> dict[str, Any]:
        if self.con is None:
            return {}
        try:
            v = db.meta_get(self.con, ACK_KEY, {})
        except sqlite3.Error:
            return {}
        return v if isinstance(v, dict) else {}


def _load(path: str) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = {}
    st = st if isinstance(st, dict) else {}
    for k, v in (
        ("incidents", {}),
        ("replays", {}),
        ("asks", {}),
        ("restarts", []),
        ("sent", []),
        ("api", {}),
        ("resolved", {}),
        ("noted", {}),
        ("fallback", {}),
        ("request", {}),
    ):
        if not isinstance(st.get(k), type(v)):
            st[k] = v
    return st


class Watch:
    def __init__(self, facts: Facts, st: dict[str, Any], live: bool = False) -> None:
        self.f, self.st, self.live, self.now = facts, st, live, facts.now
        self.acts: list[dict[str, Any]] = []
        self.tz = todo.owner_tz()
        self.want: dict[int, str] = {}
        self._quiet: bool | None = None
        old = self.now - 2 * HORIZON_S
        for k in ("replays", "api", "asks", "noted", "resolved"):
            self.st[k] = {
                i: v
                for i, v in self.st[k].items()
                if k == "asks" or (v if isinstance(v, (int, float)) else (v or {}).get("at", 0)) > old
            }
        self.st["asks"] = {i: n for i, n in self.st["asks"].items() if i in self.st["replays"]}

    # -- helpers
    def act(self, what: str, **kw: Any) -> None:
        self.acts.append({"act": what, **kw})

    def hm(self, ts: float) -> str:
        return datetime.fromtimestamp(ts, self.tz).strftime("%H:%M")

    def quiet(self) -> bool:
        """Inside quiet hours on his clock (BW_QUIET, default 0-8; his trip zone while away)."""
        if self._quiet is None:
            m = re.fullmatch(r"\s*(\d{1,2})-(\d{1,2})\s*", os.environ.get("BW_QUIET", "0-8") or "")
            if not m:
                self._quiet = False
            else:
                h = datetime.fromtimestamp(self.now, _his_zone(self.now, self.tz)).hour
                a, b = int(m.group(1)), int(m.group(2))
                self._quiet = a <= h < b if a <= b else (h >= a or h < b)
        return self._quiet

    def write(self, fn: Callable[[sqlite3.Connection], Any]) -> bool:
        """One short write transaction on the store (live only; never creates the store)."""
        if self.f.con is None:
            return False
        try:
            con = db.connect(timeout=2)
        except (OSError, sqlite3.Error):
            return False
        try:
            with db.tx(con):
                fn(con)
            return True
        except sqlite3.Error:
            return False
        finally:
            con.close()

    # -- notices
    def notify(self, text: str, push: str, *, urgent: bool, tag: str) -> tuple[bool, dict[str, Any]]:
        sent = [t for t in self.st["sent"] if t > self.now - NOTICE_WINDOW_S]
        self.st["sent"] = sent
        if len(sent) >= NOTICE_MAX:
            self.act("suppressed", tag=tag, why=f"cap {NOTICE_MAX} per 6 h")
            return False, {"capped": True}
        if not self.live:
            sent.append(self.now)
            self.act("notify", tag=tag, urgent=urgent, text=text)
            return True, {}
        via, info = self.deliver(text, push, urgent=urgent, tag=tag)
        if via:
            sent.append(self.now)
        self.act("notify", tag=tag, urgent=urgent, via=via or "none", **info, text=text)
        return bool(via), {**info, "via": via}

    def deliver(self, text: str, push: str, *, urgent: bool, tag: str) -> tuple[str | None, dict[str, Any]]:
        """The live adapter's outbox, else the app push, else the image's direct tg: (path or None, info)."""
        tried: list[str] = []
        try:
            out = tg.send(
                SENDER,
                "send",
                {"text": text},
                urgent=urgent,
                idem=f"{SENDER}:{tag}:{int(self.now)}",
                wait=_num("BW_OUTBOX_WAIT", OUTBOX_WAIT_S),
            )
        except Exception as e:  # noqa: BLE001 - a sender fault must never stop the watchdog
            out = tg.Outcome("direct", error=type(e).__name__)
        if out.status in ("sent", "skipped"):
            return "outbox", {"row": out.row_id}
        if out.status == "queued":
            return "outbox-held", {"row": out.row_id}
        tried.append(f"outbox:{out.status}")
        phone = _bin("BW_PHONE_BIN", os.fspath(paths.bin_dir("phone")))
        level = "urgent" if urgent else "info"
        args = ["push", "--level", level, "--title", PUSH_TITLE, "--body", push[:80], "--tag", f"bw-{tag}"]
        rc = _run(phone, [*args, "--wait", str(PUSH_WAIT_S)], PUSH_WAIT_S + 20)
        if rc == 0:
            return "push", {"tried": ",".join(tried)}
        tried.append(f"push:{rc}")
        rc = _run(_bin("BW_TG_DIRECT", "/usr/local/bin/tg"), [text], 30)
        if rc == 0:
            return "direct", {"tried": ",".join(tried)}
        tried.append(f"direct:{rc}")
        return None, {"tried": ",".join(tried)}

    def open(self, kind: str, notice: str, push: str, *, owner: bool, **kw: Any) -> dict[str, Any]:
        inc = self.st["incidents"].get(kind)
        if inc:
            inc.update(kw)
            if owner and not inc.get("owner"):  # a message of his now waits on it: tell him, quiet hours or not
                inc["owner"] = True
                inc["notice"], inc["push"] = notice, push
                if not inc.get("notified"):
                    inc.pop("hush", None)
                    self.tell(kind, inc)
            return inc
        inc = {"opened": self.now, "notified": False, "restarts": 0, "owner": owner, "notice": notice, "push": push}
        inc.update(kw)
        self.st["incidents"][kind] = inc
        self.act("open", incident=kind, owner=owner)
        if not owner and self.now - self.st["resolved"].get(kind, 0) < REOPEN_QUIET_S:
            inc["hush"] = True
            self.act("hush", incident=kind, why="back within 30 min of its end: no new notice")
            return inc
        self.tell(kind, inc)
        return inc

    def tell(self, kind: str, inc: dict[str, Any]) -> None:
        if not inc.get("owner") and self.quiet():
            if not inc.get("deferred"):
                inc["deferred"] = True
                self.act("defer", incident=kind, why="quiet hours")
            return
        inc.pop("deferred", None)
        ok, info = self.notify(inc["notice"], inc["push"], urgent=bool(inc.get("owner")), tag=kind)
        inc["notified"] = ok
        if info.get("via") == "outbox-held" and info.get("row") is not None:
            inc["held_row"] = info["row"]
        if ok or info.get("capped"):
            inc.pop("retry", None)
        else:
            inc["tries"] = inc.get("tries", 0) + 1
            inc["retry"] = inc["tries"] < NOTICE_RETRIES

    def due_notices(self) -> None:
        """Deferred notices whose quiet hours are over, and failed ones that get another try."""
        for kind, inc in list(self.st["incidents"].items()):
            if (inc.get("deferred") and (inc.get("owner") or not self.quiet())) or inc.get("retry"):
                self.tell(kind, inc)

    def resolve(self, kind: str, what: str, *, quiet: bool = False) -> None:
        inc = self.st["incidents"].pop(kind, None)
        if not inc:
            return
        self.st["resolved"][kind] = self.now
        self.act("resolve", incident=kind, after_min=int((self.now - inc["opened"]) // 60))
        if not inc.get("notified") or quiet:
            return
        row = inc.get("held_row")
        if self.live and row is not None:
            try:
                gone = tg.withdraw(int(row), f"{SENDER}: {kind} resolved before it went out")
            except Exception:  # noqa: BLE001 - then it went out: say it's over
                gone = False
            if gone:
                self.act("withdraw", incident=kind, row=row)
                return
        self.notify(
            f"✅ (auto) Back to normal: {what}",
            f"Back to normal: {what}",
            urgent=bool(inc.get("owner")),
            tag=f"{kind}-ok",
        )

    # -- acts on the session and the bridge
    def try_restart(self, kind: str, why: str) -> str:
        state, _ = self.f.session()
        if state not in ("idle", "absent"):
            return f"no restart: session {state}"
        if not paths.config("brain-watch-restart").exists():
            return "no restart: switch off"
        inc = self.st["incidents"].get(kind) or {}
        recent = [t for t in self.st["restarts"] if t > self.now - RESTART_WINDOW_S]
        self.st["restarts"] = recent
        if inc.get("restarts", 0) >= RESTART_PER_INCIDENT or len(recent) >= RESTART_MAX:
            if self.st.get("limit_notified", 0) <= self.now - RESTART_WINDOW_S:
                self.st["limit_notified"] = self.now
                self.notify(
                    f"⚠️ (auto) Telegram is still broken after automatic restarts ({why}). It needs you.",
                    "Telegram still broken after automatic restarts: it needs you.",
                    urgent=True,
                    tag="restart-limit",
                )
            return "no restart: limit reached"
        if recent and self.now - recent[-1] < RESTART_GAP_S:
            return "no restart: the last one was under 15 min ago"
        if self.live:
            rc = _run(_bin("BW_RESTART_BIN", os.fspath(paths.bin_dir("restart"))), [f"auto-recover: {why}"], 90)
            self.act("restart", why=f"auto-recover: {why}", rc=rc)
        else:
            self.act("restart", why=f"auto-recover: {why}")
        recent.append(self.now)
        if inc:
            inc["restarts"] = inc.get("restarts", 0) + 1
        return "restarted"

    def decision(self, kind: str, result: str) -> None:
        inc = self.st["incidents"].get(kind)
        if inc is not None and result != inc.get("last_action"):
            inc["last_action"] = result
            self.act("decision", incident=kind, result=result)

    def redeliver(self, ids: list[int], why: str) -> None:
        for i in ids:
            self.st["replays"][str(i)] = self.now
            self.st["asks"][str(i)] = int(self.st["asks"].get(str(i), 0)) + 1
            self.want.setdefault(i, why)

    def flush(self) -> None:
        """One redelivery request per run, with every id this run wants."""
        if not self.want:
            return
        ids = sorted(self.want)
        why = "; ".join(sorted(set(self.want.values())))[:200]
        rid = f"bw-{int(self.now)}-{zlib.crc32(','.join(map(str, ids)).encode()):08x}"
        req = {"rid": rid, "ids": ids, "why": why, "ts": self.now}
        self.st["request"] = req
        if self.live:
            ok = self.write(lambda con: db.meta_set(con, REQUEST_KEY, req))
            self.act("redeliver", ids=ids, why=why, rid=rid, ok=ok)
        else:
            self.act("redeliver", ids=ids, why=why)

    def resend_result(self) -> str:
        lease = self.f.lease() or {}
        if not lease.get("redeliver"):
            return "this bridge can't re-send"
        if not self.live:
            return "re-sent (dry run: assumed)"
        req, ack = self.st.get("request") or {}, self.f.ack()
        if not req or ack.get("rid") != req.get("rid"):
            return "the bridge didn't take the re-send request"
        return "re-sent, still no receipt" if ack.get("sent") else "the bridge found nothing to re-send"

    # -- checks
    def run(self) -> list[dict[str, Any]]:
        mode = self.f.tg_mode()
        if mode == "poll":
            if not self.st.get("stood_down"):
                self.st["stood_down"] = self.now
                self.act("stand_down", why="poll mode: channel-check watches the poll path")
            return self.acts
        self.st.pop("stood_down", None)
        self.due_notices()
        state, _ = self.f.session()
        self.watch_agents(state)
        self.watch_session(state)
        fb = self.f.fallback() if self.f.official() else None
        if mode == "official" or fb:
            for k in ("stall", "long_wait", "bridge_down"):
                self.resolve(k, "", quiet=True)
            if fb:
                self.watch_fallback(fb)
            return self.acts
        self.watch_bridge(state)
        self.watch_inbound(state)
        self.flush()
        return self.acts

    def watch_agents(self, state: str) -> None:
        if state != "unknown":
            self.st.pop("blind_since", None)
            self.resolve("agents", "brain-watch sees my session again.")
            return
        since = self.st.setdefault("blind_since", self.now)
        if self.now - since >= AGENTS_BLIND_S:
            hm = self.hm(since)
            self.open(
                "agents",
                f"⚠️ (auto) brain-watch can't see my session since {hm}: `claude agents --json` fails (agent view"
                " off?). Telegram inbound isn't watched until it works again.",
                f"Watchdog blind since {hm}: claude agents fails.",
                owner=False,
            )

    def watch_session(self, state: str) -> None:
        if state == "unknown":
            return
        if state != "absent":
            self.st.pop("absent_since", None)
            self.resolve("session_down", "my main session is back.")
            return
        since = self.st.setdefault("absent_since", self.now)
        if self.now - since < DOWN_S:
            return
        n, hm = len(self.f.pending()), self.hm(since)
        waits = f" {n} message(s) of yours wait for it." if n else " Messages wait for it."
        self.open(
            "session_down",
            f"⚠️ (auto) My main session has been down since {hm}.{waits}",
            f"My session is down since {hm}. Messages wait for it.",
            owner=bool(n),
        )
        self.decision("session_down", self.try_restart("session_down", f"the main session is gone since {hm}"))

    def watch_bridge(self, state: str) -> None:
        if state not in ("idle", "busy", "waiting"):  # starting, absent, unknown: not yet, or another watch's
            self.st.pop("nolease_since", None)
            return
        if self.f.lease():
            self.st.pop("nolease_since", None)
            self.resolve("bridge_down", "the Telegram bridge is back in my session.")
            return
        since = self.st.setdefault("nolease_since", self.now)
        if self.now - since < DOWN_S:
            return
        n, hm = len(self.f.pending()), self.hm(since)
        self.open(
            "bridge_down",
            f"⚠️ (auto) My session has run without its Telegram bridge since {hm}: your messages can't reach it"
            + (f" ({n} waiting)." if n else "."),
            f"Telegram bridge missing since {hm}: messages can't reach me.",
            owner=bool(n),
        )
        self.decision("bridge_down", self.try_restart("bridge_down", "no Telegram bridge in the session"))

    def watch_fallback(self, fb: dict[str, Any]) -> None:
        key = f"{fb['ts']:.0f}"
        told: dict[str, Any] = self.st["fallback"] if self.st["fallback"].get("key") == key else {"key": key}
        if told.get("done") or told.get("tries", 0) >= NOTICE_RETRIES:
            return
        if fb["texted"]:
            self.st["fallback"] = {"key": key, "done": True, "by": "start-claude"}
            self.act("fallback", told="start-claude texted him")
            return
        if self.now - fb["ts"] < FALLBACK_GRACE_S:
            return
        if self.quiet():
            if not told.get("deferred"):
                self.st["fallback"] = {**told, "deferred": True}
                self.act("defer", incident="fallback", why="quiet hours")
            return
        hm = self.hm(fb["ts"])
        ok, _ = self.notify(
            f"⚠️ (auto) Telegram fell back to the official plugin at {hm} ({fb['why']}). It works, but without the"
            ' bridge and its watchdog. Back: rm ~/.telegram-official && restart "bridge".',
            f"Telegram fell back to the official plugin at {hm}.",
            urgent=False,
            tag="fallback",
        )
        self.st["fallback"] = {"key": key, "done": ok, "tries": told.get("tries", 0) + 1, "at": self.now}

    def watch_inbound(self, state: str) -> None:
        pending = self.f.pending()
        unanswered = self.f.received_unanswered()
        if not pending and not unanswered:
            self.resolve_inbound()
            return
        seen = self.f.seen_at([r["id"] for r in pending]) if pending else {}
        if seen:
            self.transcript_receipts(sorted(seen))
        unseen = [r for r in pending if r["id"] not in seen]
        if state == "idle" and unanswered:
            self.api_failures(unanswered)
        if not unseen:
            self.resolve_inbound()
            return
        if state in ("starting", "absent") or "bridge_down" in self.st["incidents"]:
            return  # a fresh session gets a moment; a gone session or bridge has its own watch
        first = min(r["ts"] for r in unseen)
        ids = [r["id"] for r in unseen]
        hm = self.hm(first)
        if state in ("busy", "waiting"):  # never a restart for a slow answer
            if self.now - first >= LONG_WAIT_S:
                if state == "busy":
                    notice = (
                        f"⚠️ (auto) Your message from {hm} is still waiting: I'm busy with something that hasn't let"
                        " me read it yet. I'll answer as soon as it finishes; Remote Control if it's urgent."
                    )
                    push = f"Your {hm} message waits: I'm busy. The answer comes when I'm done."
                else:
                    notice = (
                        f"⚠️ (auto) Your message from {hm} is waiting: my session is stuck on a dialog. Remote"
                        " Control can clear it."
                    )
                    push = f"Your {hm} message waits: my session is stuck on a dialog."
                self.open("long_wait", notice, push, owner=True, ids=ids)
            return
        asked, asks = self.st["replays"], self.st["asks"]
        fresh = [i for i in ids if str(i) not in asked]
        if fresh and "stall" not in self.st["incidents"]:
            self.redeliver(fresh, "stalled")
            return
        if any(self.now - asked.get(str(i), 0) < REPLAY_WAIT_S for i in ids):
            return
        restart = paths.config("brain-watch-restart").exists() and state == "idle"
        tail = "I'm restarting my session to recover it." if restart else "Remote Control works if it's urgent."
        self.open(
            "stall",
            f"⚠️ (auto) Your message from {hm} hasn't reached my session ({self.resend_result()}). {tail}",
            f"Your {hm} message hasn't reached my session. Remote Control works.",
            owner=True,
            ids=ids,
        )
        again = [i for i in ids if int(asks.get(str(i), 0)) < REPLAY_MAX]
        if again:
            self.redeliver(again, "stalled, again")
        self.decision("stall", self.try_restart("stall", f"owner event from {hm} never reached the session"))

    def resolve_inbound(self) -> None:
        for k in ("stall", "long_wait"):
            self.resolve(k, "your messages reach me again.")

    def transcript_receipts(self, ids: list[int]) -> None:
        new = [i for i in ids if str(i) not in self.st["noted"]]
        if not new:
            return
        for i in new:
            self.st["noted"][str(i)] = self.now
        if self.live and receipts._hub_tg():
            got = []
            for i in new:
                try:
                    got.append(receipts.mark_hub(i, self.now, how="transcript"))
                except Exception:  # noqa: BLE001 - a refused receipt never stops the watchdog
                    got.append("error")
            self.act("receipt", ids=new, how="transcript", ok=all(g in ("ok", "spooled") for g in got))
        elif self.live:
            ok = self.write(lambda con: [receipts.mark(con, i, self.now, how="transcript") for i in new])
            self.act("receipt", ids=new, how="transcript", ok=ok)
        else:
            self.act("receipt", ids=new, how="transcript")

    def api_failures(self, rows: list[dict[str, Any]]) -> None:
        book = self.st["api"]
        at = self.f.seen_at([r["id"] for r in rows])
        hits = self.f.api_errors(at)
        for r in rows:
            hit, b = hits.get(r["id"]), book.get(str(r["id"]))
            if not hit or (b and (b.get("told") or hit[2] == b.get("key"))):
                continue
            cause, issue, key = hit
            if not b:
                book[str(r["id"])] = {"at": self.now, "key": key, "issue": issue}
                self.redeliver([r["id"]], f"API error {issue}")
                continue
            b["told"], b["key"] = self.now, key
            self.notify(
                f"⚠️ (auto) Your message from {self.hm(r['ts'])} failed twice on an API error ({cause}). Please rephrase"
                " it or use Remote Control.",
                f"Your {self.hm(r['ts'])} message failed twice on an API error.",
                urgent=True,
                tag="api_error",
            )


def _line(a: dict[str, Any], live: bool) -> str:
    rest = " ".join(f"{k}={v}" for k, v in a.items() if k not in ("act", "text"))
    head = a["act"] if live else f"DRY-RUN would {a['act']}"
    return head + (f" {rest}" if rest else "") + (f" · notice: {a['text']}" if "text" in a else "")


def _status(now: float, mode: str, as_json: bool) -> int:
    try:
        con: sqlite3.Connection | None = db.connect(ro=True, timeout=2)
    except FileNotFoundError:
        con = None
    st = _load(os.fspath(paths.state("brain-watch.json")))
    try:
        f = Facts(con, now)
        state, _ = f.session()
        lease = f.lease()
        out = {
            "mode": mode,
            "restart_switch": paths.config("brain-watch-restart").exists(),
            "telegram_mode": f.tg_mode() or "none",
            "session": state,
            "bridge": {k: lease.get(k) for k in ("pid", "version", "redeliver")} if lease else None,
            "store": ("hub" if receipts._hub_tg() else "ok") if con or receipts._hub_tg() else "missing",
            "pending": len(f.pending()),
            "unanswered": len(f.received_unanswered()),
            "incidents": {
                k: {"opened": v.get("opened"), "notified": v.get("notified")} for k, v in st["incidents"].items()
            },
            "notices_6h": len([t for t in st["sent"] if t > now - NOTICE_WINDOW_S]),
            "restarts_6h": len([t for t in st["restarts"] if t > now - RESTART_WINDOW_S]),
            "last_request": (st.get("request") or {}).get("rid"),
        }
    finally:
        if con is not None:
            con.close()
    if as_json:
        print(json.dumps(out, ensure_ascii=False))
        return 0
    b = out["bridge"]
    print(
        f"mode: {mode} (switches in ~/.config/assistant: brain-watch-off / -restart) · restarts:"
        f" {'on' if out['restart_switch'] else 'off'} · store: {out['store']}"
    )
    print(
        f"telegram mode: {out['telegram_mode']} · session: {state} · bridge: "
        + (f"pid {b['pid']} v{b['version']}{' (redeliver)' if b.get('redeliver') else ''}" if b else "no lease holder")
    )
    print(f"owner rows without a receipt ≥ 3 min: {out['pending']} · received, unanswered: {out['unanswered']}")
    print(f"incidents: {json.dumps(out['incidents']) if out['incidents'] else 'none'}")
    print(f"notices (6 h): {out['notices_6h']} · restarts (6 h): {out['restarts_6h']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "-h" in argv or "--help" in argv:
        print((__doc__ or "").strip())
        return 0
    off = paths.config("brain-watch-off").exists()
    live = "--dry-run" not in argv and "--shadow" not in argv
    mode = "off" if off else ("live" if live else "dry-run")
    now = clock.now()
    if "--status" in argv:
        return _status(now, "off" if off else "live", "--json" in argv)
    if off:
        return 0
    with locks.hold(paths.state("brain-watch.lock"), wait=0) as held:
        if held is None:
            return 0  # the last run is still sending
        try:
            con: sqlite3.Connection | None = db.connect(ro=True, timeout=2)
        except FileNotFoundError:
            con = None
        dry_state = None if live else os.environ.get("BW_DRY_STATE")
        spath = dry_state or os.fspath(paths.state("brain-watch.json"))
        st = _load(spath)
        try:
            acts = Watch(Facts(con, now), st, live=live).run()
        finally:
            if con is not None:
                con.close()
        if live or dry_state:  # a plain dry run saves nothing
            jsonstate.save(spath, st, mode=0o600, dir_mode=0o700)
    if acts:
        stamp = datetime.fromtimestamp(now, todo.owner_tz()).strftime("%Y-%m-%d %H:%M:%S")
        logp = paths.state("brain-watch.log")
        for a in acts:
            events.append_private(logp, f"{stamp} {_line(a, live)}")
    if "--json" in argv:
        print(json.dumps({"now": now, "mode": mode, "acts": acts}, ensure_ascii=False))
    else:
        for a in acts:
            print(_line(a, live))
    return 0
