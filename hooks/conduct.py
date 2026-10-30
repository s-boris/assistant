#!/usr/local/bin/python3.13 -IS
"""conduct: token-free conduct checks at the moment of the act.

  conduct.py pre      PreToolUse (stdin = the hook JSON):
                        ackfirst       main session, while he waits for a first reply (an owner Telegram/app prompt and
                                       nothing sent yet): slow foreground work (vault ask, vault
                                       job wait, assistant land/ci/deploy, test-all, run.sh, pytest, bun test, hub
                                       deploy, sleep >= 20, a wait loop under timeout >= 60; hub vault_ask/vault_job
                                       wait_s > 20, deploy_run plan=false)
                        ackslow        main session, same open owner turn, nothing sent ACK_AFTER_S (12 s; a number
                                       in ~/.config/conduct/ack-after-s overrides, floor 5) after his prompt: the
                                       next Bash or hub wait call is held once per turn (serial lookups were 43 of 67
                                       first replies > 20 s, 29.10); `ackslow-shadow` logs only
                        inbox          any caller, a Bash or Monitor command: rm/unlink/shred/trash/mv/find
                                       -delete/xargs rm/os.remove of a top-level ~/inbox note (~/inbox/files/ is
                                       not a note). Monitor gets only this check: it starts a background watch and
                                       returns at once, so ackfirst/ackslow and the send detection stay Bash-only
                        sendlint       Telegram reply/send_poll (pitch-check.sh calls this first): a declined,
                                       skipped, scratched, removed or off ledger item (`memory ledger --negative`),
                                       > 1 question on one keyboard, > 12 text lines
                        status         C1, Telegram reply/send_poll/edit_message and phone_chat, on with
                                       ~/.config/conduct/status-on, SHADOW unless `status-enforce` exists: a clause
                                       claiming not done (pending, waiting on/for, not yet, not live/landed/done,
                                       still open/running, queued, unlanded, blocked on, needs your ✅) or done
                                       (live, landed, deployed, installed, done, merged, fixed) about an item the
                                       setup records the other way (reality.json: lands + hub releases in
                                       deploys.jsonl, a git check of the exact branch, board.json open items and
                                       probe states, PROACTIVE.md closes, plan ids logged live in PROACTIVE.md /
                                       CHANGELOG.md, 14 days). Questions, will/once/after/if/would..., quoted lines
                                       and code spans are no claims. No evidence = an "unverified" row
                      Any send (reply, send_poll, react, draft, edit_message, phone_chat, voice_reply,
                      voice_drive_update, Bash voice-reply/drive-update/phone chat) closes the open turn.
  conduct.py prompt   UserPromptSubmit: records the main session's turn (owner, beat, loop, other); prints nothing.
  conduct.py stop     Stop (idle): a beat or loop turn ending with startable work (the agenda's Startable counts:
                      goals, ideas, night), 0 running agents/tasks/lands and no `wake next` hold -> block once.
  conduct.py holds [--days N] [--json]   counts per check: held, resent unchanged (override), shadow, warn, unverified,
                      error.

Deny-once: a held call's input hash is kept 15 min; the identical retry passes and is logged as an override. The
checks never decide for me: I read the one-line reason, then fix or resend (his "LLM at the core" decision).
FAILS OPEN: any error or a bad payload = allow, one line in the log. Switches in ~/.config/conduct/: `off` (all),
`<check>-off` (ackfirst, sendlint, inbox, idle), `<rule>-shadow` (ledger, keyboard, length, ackfirst, inbox, idle:
log would-hold only), `status-on` (C1 on, shadow), `status-enforce` (C1 holds). State in ~/.local/state/conduct/:
holds.jsonl (every hold, override, warn, shadow, unverified; ids, rule names and counts only, never message text),
held.json, turn.json (no prompt text: hashed keywords only), ledger.json and reality.json (caches), log. Tests:
tests/hooks/test_conduct.py. Seams: CONDUCT_STATE_DIR, CONDUCT_CONFIG_DIR, CONDUCT_MEMORY_BIN, CONDUCT_FACTS,
CONDUCT_ACCESS, CONDUCT_MAIN_CWD, CONDUCT_AGENT_WATCH, CONDUCT_TASKS_BIN, CONDUCT_WAKE_BIN, CONDUCT_PROC_DIR,
CONDUCT_LIB, CONDUCT_TODO, CONDUCT_GOALS, CONDUCT_NOW, CONDUCT_DEPLOYS, CONDUCT_BOARD_FEED, CONDUCT_PROACTIVE,
CONDUCT_CHANGELOG, CONDUCT_REPO, CONDUCT_GIT ("off" = no git checks).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from collections.abc import Iterator
from typing import Any

ENV = os.environ.get
HELD_TTL = 900  # s: an identical resend within 15 min goes through
TURN_MAX_AGE = 1800  # s: an owner turn older than this is not "waiting" any more
ACK_AFTER_S = 12.0  # s: ackslow: an owner turn with no send this long holds its next tool call once (exit E8)
LEDGER_MAX_AGE = 6 * 3600  # s: the ledger cache is refreshed when FACTS.md changes, else at most this old
LOG_MAX = 1024 * 1024
MAX_LINES = 12
TP = "mcp__plugin_telegram-plus_telegram__"
LINT_TOOLS = {TP + "reply", TP + "send_poll"}
SEND_TOOLS = {
    TP + "react",
    TP + "draft",
    TP + "edit_message",
    "mcp__hub__phone_chat",
    "mcp__hub__voice_reply",
    "mcp__hub__voice_drive_update",
}
HUB_WAIT = {"mcp__hub__vault_ask", "mcp__hub__vault_job", "mcp__hub__deploy_run"}
RULE_CHECK = {"ledger": "sendlint", "keyboard": "sendlint", "length": "sendlint", "status": "status"}
NEGATIVE = ("declined", "skipped", "scratched", "removed", "off")
ACK = {"removed": r"\b(?:removed|gone|deleted|retired)\b", "off": r"\b(?:off|disabled|stopped|paused)\b"}


def now() -> float:
    v = ENV("CONDUCT_NOW")
    return float(v) if v else time.time()


def home() -> str:
    return os.path.expanduser("~")


def state_dir() -> str:
    return ENV("CONDUCT_STATE_DIR") or os.path.join(home(), ".local", "state", "conduct")


def config_dir() -> str:
    return ENV("CONDUCT_CONFIG_DIR") or os.path.join(home(), ".config", "conduct")


def sp(name: str) -> str:
    return os.path.join(state_dir(), name)


def flag(name: str) -> bool:
    return os.path.exists(os.path.join(config_dir(), name))


def enabled(check: str) -> bool:
    if flag("off"):
        return False
    if check == "status":
        return flag("status-on")
    return not flag(f"{check}-off")


def shadow(rule: str) -> bool:
    if rule == "status":  # C1 is shadow-only until `status-enforce` exists (after the 3-day review of its rows)
        return flag("status-shadow") or not flag("status-enforce")
    return flag(f"{rule}-shadow")


# ------------------------------------------------------------------------------------------------ state + log
def _mkdir() -> None:
    os.makedirs(state_dir(), mode=0o700, exist_ok=True)


def use_lib() -> None:
    """~/bin/lib (core/) on sys.path: core.locks for the state lock, core.todo for the idle check."""
    lib = ENV("CONDUCT_LIB") or os.path.join(home(), "bin", "lib")
    if lib not in sys.path:
        sys.path.insert(0, lib)


@contextlib.contextmanager
def locked() -> Iterator[None]:
    _mkdir()
    use_lib()
    from core import locks  # type: ignore[import-not-found]  # the repo's flock helper (no flock outside core/)

    with locks.hold(sp("lock"), wait=1.0, mode=0o600):  # busy past 1 s: go on unlocked (best effort)
        yield


def read_json(path: str, default: Any) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path: str, data: Any) -> None:
    _mkdir()
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def _append(name: str, line: str) -> None:
    try:
        _mkdir()
        p = sp(name)
        with open(p, "a", encoding="utf-8") as f:
            f.write(line.rstrip("\n") + "\n")
        if os.path.getsize(p) > LOG_MAX:
            with open(p, encoding="utf-8") as f:
                keep = f.readlines()[-5000:]
            with open(p + ".tmp", "w", encoding="utf-8") as f:
                f.writelines(keep)
            os.replace(p + ".tmp", p)
    except OSError:
        pass


def record(check: str, rule: str, act: str, tool: str = "", **extra: Any) -> None:
    """One row per hold, override, shadow hit, warn or error in holds.jsonl: ids and names only, never text."""
    row = {"ts": int(now()), "check": check, "rule": rule, "act": act, "tool": tool.replace(TP, "tg:")}
    row.update({k: v for k, v in extra.items() if v not in (None, "")})
    _append("holds.jsonl", json.dumps(row, ensure_ascii=False))


def log(msg: str) -> None:
    _append("log", f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}")


# ------------------------------------------------------------------------------------------------ session
def main_cwd() -> str:
    return ENV("CONDUCT_MAIN_CWD") or os.path.join(home(), "workspace")


def is_main(ev: dict[str, Any]) -> bool:
    """The main session's own call: no agent_id (a subagent's call carries one), and cwd = the workspace or a
    transcript in the workspace's project folder (the cwd follows every `cd`)."""
    if ev.get("agent_id"):
        return False
    want = os.path.realpath(main_cwd())
    cwd = ev.get("cwd")
    if isinstance(cwd, str) and cwd and os.path.realpath(cwd) == want:
        return True
    tp = ev.get("transcript_path")
    if isinstance(tp, str) and tp:
        return os.path.basename(os.path.dirname(tp)) == re.sub(r"[^A-Za-z0-9]", "-", want)
    return False


def owner_ids() -> set[str] | None:
    p = ENV("CONDUCT_ACCESS") or os.path.join(home(), ".claude", "channels", "telegram", "access.json")
    d = read_json(p, None)
    if not isinstance(d, dict) or not isinstance(d.get("allowFrom"), list):
        return None
    return {str(x).strip() for x in d["allowFrom"]}


CHANNEL = re.compile(r"<channel\b([^>]*)>")
ATTR = re.compile(r'(\w+)="([^"]*)"')


def classify(prompt: str) -> tuple[str, dict[str, str]]:
    """(kind, channel attrs): owner = a Telegram message/button/poll/approval from an allowlisted user or a phone-app
    prompt (he waits for a reply); beat; loop (a /loop tick); other (task notifications, reactions, the rest)."""
    head = (prompt or "")[:4000]
    if "[heartbeat]" in head[:300] or "The heartbeat plugin" in head[:300]:
        return "beat", {}
    if re.match(r"\s*#\s*Autonomous loop", head, re.I) or "<command-name>/loop" in head[:600]:
        return "loop", {}
    if head.lstrip().startswith("<task-notification"):
        return "other", {}
    m = CHANNEL.search(head)
    if m:
        attrs = dict(ATTR.findall(m.group(1)))
        if "telegram" not in attrs.get("source", ""):
            return "other", attrs
        ev = attrs.get("event_type", "message")
        if attrs.get("handled") or ev not in ("message", "button", "poll_answer", "approval"):
            return "other", attrs
        ids = owner_ids()
        if ids is not None and attrs.get("user_id", "") not in ids and attrs.get("user", "") not in ids:
            return "other", attrs
        return "owner", attrs
    if head.lstrip().startswith("\U0001f4f1"):  # phone app prompt
        return "owner", {"source": "app"}
    return "other", {}


def kw_hash(w: str) -> str:
    return hashlib.sha1(("conduct:" + w).encode()).hexdigest()[:10]


def load_turn() -> dict[str, Any]:
    t = read_json(sp("turn.json"), {})
    return t if isinstance(t, dict) else {}


def turn_open(turn: dict[str, Any], ev: dict[str, Any]) -> bool:
    if turn.get("kind") != "owner" or turn.get("sent") or turn.get("ended"):
        return False
    if turn.get("sid") and ev.get("session_id") and turn["sid"] != ev["session_id"]:
        return False
    return now() - float(turn.get("ts") or 0) <= TURN_MAX_AGE


def update_turn(**kv: Any) -> None:
    with locked():
        t = load_turn()
        if t:
            t.update(kv)
            write_json(sp("turn.json"), t)


# ------------------------------------------------------------------------------------------------ words (board-lint's)
STOP = set(
    """a an the and or of for to in on at by with from into via per as is are was were be been it its this that
these those his her him he she they them their our my me i you your we not no yes all any some more most less only also
then than so but if when while after before once still now new next old up down out over under about again just very
can could would should will may might must do does did done get got has have had make made use used using need needs
own same each every both few many much other such what which who whom whose why how where there here one two three
etc incl e g ie eg vs pm am h min x y n ok via re ask asked asks owner owners item items step steps todo goals note
notes pitch pitched check checked waiting wait waits open plan planned tonight today tomorrow morgen heute""".split()
)
DONE_WORDS = {
    "deployed",
    "added",
    "removed",
    "deleted",
    "installed",
    "live",
    "done",
    "skipped",
    "declined",
    "booked",
    "answered",
    "confirmed",
    "ordered",
    "placed",
    "paid",
    "bought",
    "merged",
    "shipped",
    "fixed",
    "applied",
    "enabled",
    "cancelled",
    "canceled",
    "resolved",
    "solved",
    "finished",
    "completed",
    "scratched",
    "excluded",
    "decided",
    "kept",
    "set",
    "off",
}
TIME_RE = re.compile(r"(?<![\d:.])([01]?\d|2[0-3]):([0-5]\d)(?![\d:])")
DATE_DM = re.compile(r"(?<![\w.])(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?(?![\w]|\.\d)")
DATE_ISO = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
VERSION = re.compile(r"\b\d+(?:\.\d+){2,}\b")
EMOJI = re.compile("[←-⯿☀-➿\U0001f000-\U0001faff️‍]")
WORD = re.compile(r"[a-z0-9äöüéèà]+")


def stem(w: str) -> str:
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 4 and w.endswith(("ches", "shes", "sses", "xes", "zes")):
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
        return w[:-1]
    return w


def line_words(line: str, drop_done: bool = False) -> list[str]:
    """Significant words of one line in order (with repeats): lowercase, dates/times out, versions whole, plurals
    stemmed: the normalization of board-lint's keyword_list."""
    t = EMOJI.sub(" ", line)
    found: list[tuple[int, str]] = [(m.start(), m.group(0)) for m in VERSION.finditer(t)]
    t = VERSION.sub(lambda m: " " * len(m.group(0)), t)
    for rx in (DATE_ISO, DATE_DM, TIME_RE):
        t = rx.sub(lambda m: " " * len(m.group(0)), t)
    for m in WORD.finditer(t.lower()):
        w = m.group(0)
        if len(w) < 2 or w in STOP:
            continue
        w = stem(w)
        if w in STOP or (drop_done and w in DONE_WORDS):
            continue
        found.append((m.start(), w))
    return [w for _, w in sorted(found)]


def thing_words(thing: str) -> list[str]:
    seen: list[str] = []
    for w in line_words(thing, drop_done=True):
        if w not in seen:
            seen.append(w)
    return seen


def text_hashes(text: str) -> list[str]:
    return sorted({kw_hash(w) for line in text.splitlines() for w in line_words(line)})[:400]


# ------------------------------------------------------------------------------------------------ ledger
def facts_path() -> str:
    return ENV("CONDUCT_FACTS") or os.path.join(home(), "workspace", "FACTS.md")


LEDGER_LINE = re.compile(r"^- (\d{1,2})\.(\d{1,2})\.?\s+([a-z]+):\s*(.+)$")


def ledger_from_facts(text: str) -> list[dict[str, Any]]:
    """Fallback when `memory` fails: the negative ledger lines of FACTS.md's ledger section."""
    out, on = [], False
    for line in text.splitlines():
        if line.startswith("## "):
            on = "ledger" in line.lower()
            continue
        m = LEDGER_LINE.match(line.strip()) if on else None
        if m and m.group(3) in NEGATIVE:
            head = re.split(r" \(| — |; ", m.group(4), maxsplit=1)[0]
            out.append({"id": "", "day": int(m.group(1)), "month": int(m.group(2)), "verb": m.group(3), "thing": head})
    return out


def ledger_items() -> list[dict[str, Any]]:
    """The negative ledger as match items {id, verb, dm, thing, kw}, cached in ledger.json per FACTS.md version."""
    fp = facts_path()
    try:
        st = os.stat(fp)
        sig = [int(st.st_mtime), st.st_size]
    except OSError:
        sig = [0, 0]
    cache = read_json(sp("ledger.json"), {})
    if isinstance(cache, dict) and cache.get("sig") == sig and now() - float(cache.get("ts") or 0) < LEDGER_MAX_AGE:
        return cache.get("items") or []
    rows: list[dict[str, Any]] | None = None
    mem = ENV("CONDUCT_MEMORY_BIN") or os.path.join(home(), "bin", "memory")
    try:
        r = subprocess.run([mem, "ledger", "--negative", "--json"], capture_output=True, text=True, timeout=2)
        d = json.loads(r.stdout) if r.returncode == 0 else None
        if isinstance(d, list):
            rows = d
    except (OSError, ValueError, subprocess.SubprocessError):
        rows = None
    src = "memory"
    if rows is None:
        src = "facts"
        try:
            with open(fp, encoding="utf-8") as f:
                rows = ledger_from_facts(f.read())
        except OSError:
            rows = []
    items = []
    for r in rows:
        verb = str(r.get("verb", ""))
        if verb not in NEGATIVE:
            continue
        head = re.split(r" \(| — |; ", str(r.get("thing", "")), maxsplit=1)[0]
        for part in (p.strip() for p in head.split(", ")):
            kw = thing_words(part)
            if kw:
                dm = f"{int(r.get('day') or 0):02d}.{int(r.get('month') or 0):02d}"
                items.append({"id": str(r.get("id", "")), "verb": verb, "dm": dm, "thing": part, "kw": kw})
    try:
        write_json(sp("ledger.json"), {"sig": sig, "ts": now(), "src": src, "items": items})
    except OSError:
        pass
    return items


def need(n: int) -> int:
    return n if n <= 2 else max(2, -(-3 * n // 4))


def covers(ws: list[str], kw: list[str]) -> int:
    """board-lint's keyword cover on one line's words: kw[0] plus >= 3/4 of kw (all when <= 2) within n+2 words of
    it (a 2-word thing in order, at most 1 word between). The index of the kw[0] that covers, else -1."""
    n, k = len(kw), need(len(kw))
    for i, w in enumerate(ws):
        if w != kw[0]:
            continue
        if n == 1:
            return i
        if n == 2:
            if kw[1] in ws[i + 1 : i + 3]:
                return i
        elif len(set(ws[max(0, i - 1) : i + n + 1]) & set(kw)) >= k:
            return i
    return -1


def ledger_hits(text: str, items: list[dict[str, Any]], owner_kwh: set[str] | None = None) -> list[dict[str, Any]]:
    """Ledger items the text names: the item's naming word plus >= 3/4 of its words (all when <= 2) on one line
    within n+2 words of it (a 2-word item in order, at most 1 word between). Not a hit: an item his own prompt covers
    (he brought it up), a removed/off item on a line that says so. Declined/skipped/scratched stay silent: no
    exemption for "(declined)" or "left out"."""
    lines = text.splitlines()
    words = [line_words(ln) for ln in lines]
    hits, seen = [], set()
    for it in items:
        kw = it["kw"]
        k = need(len(kw))
        if owner_kwh is not None and sum(1 for w in kw if kw_hash(w) in owner_kwh) >= k and kw_hash(kw[0]) in owner_kwh:
            continue
        for li, ws in enumerate(words):
            hit = covers(ws, kw) >= 0
            if hit and it["verb"] in ACK and re.search(ACK[it["verb"]], lines[li], re.I):
                hit = False
            if hit and it["thing"] not in seen:
                seen.add(it["thing"])
                hits.append(dict(it, line=li + 1))
                break
    return hits


# ------------------------------------------------------------------------------------------------ send lint
FENCE = re.compile(r"```.*?(?:```|$)", re.S)
INLINE = re.compile(r"`[^`\n]*`")
URL = re.compile(r"https?://\S+|\bwww\.\S+")
KEYCAP = re.compile(r"^\s*([0-9])️?⃣|^\s*Q?([0-9])[.):]\s", re.I)


def questions(text: str) -> int:
    """Question sentences in the visible text: code, URLs and quoted lines don't count."""
    t = FENCE.sub(" ", text or "")
    t = INLINE.sub(" ", t)
    t = URL.sub(" ", t)
    t = "\n".join(ln for ln in t.splitlines() if not ln.lstrip().startswith(">"))
    return sum(1 for s in re.findall(r"[^.!?\n]*\?+", t) if re.search(r"[^\W\d_]", s))


def _label(b: Any) -> tuple[str, bool]:
    if isinstance(b, str):
        return b, True
    if isinstance(b, dict):
        return str(b.get("text", "")), not b.get("url")
    return "", False


def keyboard_rows(inp: dict[str, Any]) -> list[list[str]]:
    rows = []
    for key in ("buttons", "keyboard"):
        v = inp.get(key)
        if isinstance(v, list):
            for row in v:
                labels = [lab for lab, cb in (_label(b) for b in (row if isinstance(row, list) else [row])) if cb]
                if labels:
                    rows.append(labels)
    if isinstance(inp.get("approval"), dict):
        rows.append(["approve", "deny"])
    return rows


def numbered_per_question(rows: list[list[str]]) -> bool:
    """Rows numbered per question (1️⃣ Yes / 1️⃣ No / 2️⃣ ...): >= 2 numbers, one of them on >= 2 buttons."""
    nums: dict[str, int] = {}
    for row in rows:
        for lab in row:
            m = KEYCAP.match(lab)
            if m:
                d = m.group(1) or m.group(2)
                nums[d] = nums.get(d, 0) + 1
    return len(nums) >= 2 and max(nums.values()) >= 2


def text_lines(text: str) -> tuple[int, float]:
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return 0, 0.0
    return len(lines), sum(1 for ln in lines if ln.lstrip().startswith("|")) / len(lines)


def send_parts(tool: str, inp: dict[str, Any]) -> tuple[str, str]:
    """(main text, everything the owner can read) of a reply or poll."""
    if tool.endswith("send_poll"):
        opts = [str(o) for o in inp.get("options") or [] if isinstance(o, str | int | float)]
        q = str(inp.get("question") or "")
        return q, "\n".join([q, *opts])
    text = str(inp.get("text") or "")
    labels = [lab for row in keyboard_rows(inp) for lab in row if lab not in ("approve", "deny")]
    full = "\n".join([text, str(inp.get("details") or ""), *labels])
    return text, full


def sendlint(
    tool: str, inp: dict[str, Any], owner_kwh: set[str] | None, items: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    text, full = send_parts(tool, inp)
    out: list[dict[str, Any]] = []
    for h in ledger_hits(full, ledger_items() if items is None else items, owner_kwh):
        out.append(
            {
                "check": "sendlint",
                "rule": "ledger",
                "item": h["id"],
                "msg": f"names {h['verb']} item “{h['thing']}” (ledger {h['dm']}): leave it out"
                " unless he asked about it",
            }
        )
    if tool.endswith("reply"):
        rows = keyboard_rows(inp)
        q = questions(text)
        if rows and (q >= 2 or numbered_per_question(rows)):
            out.append(
                {
                    "check": "sendlint",
                    "rule": "keyboard",
                    "n": q,
                    "msg": f"{q} questions on one keyboard: one question per keyboard"
                    " (combinable options -> send_poll multiple)",
                }
            )
        n, table = text_lines(text)
        if n > MAX_LINES and table < 0.5:
            out.append(
                {
                    "check": "sendlint",
                    "rule": "length",
                    "n": n,
                    "msg": f"text has {n} lines: keep ~10 short lines, the rest into `details`",
                }
            )
    return out


# ------------------------------------------------------------------------------------------------ status (C1)
# A status claim ("pending", "not live yet" / "live", "landed", "done") and a named item in one clause of an outgoing
# message, checked against what the setup itself records: lands and hub releases (deploys.jsonl + a git check of the
# exact branch), the board's open items and their probe states (board.json, the board-eval feed), items the board
# closed and plan ids logged live (PROACTIVE.md, CHANGELOG.md, last LOG_DAYS days). Built into reality.json (items
# {key, aliases, kind, state, since, evidence}), rebuilt when a source changes or after REALITY_TTL. Contradiction ->
# a finding (rule "status"); an item with no evidence -> one "unverified" row. Shadow unless `status-enforce`.
STATUS_TOOLS = LINT_TOOLS | {TP + "edit_message", "mcp__hub__phone_chat"}
REALITY_TTL = 900  # s: the reality cache is rebuilt when a source changes, else at most this old
BOARD_STALE = 3600  # s: an older board feed says nothing about its items (board-eval runs every 5 min)
LOG_DAYS = 14  # PROACTIVE.md / CHANGELOG.md lines this recent are evidence
GIT_BUDGET = 0.8  # s: all git checks of one send together
GIT_TIMEOUT = 0.5  # s: one git call
DONE_STATES = ("landed", "live", "done")
OPEN_STATES = ("unlanded", "pending", "failed")
_NEG = r"(?:not|never|(?:is|are|was|were|has|have|had)n['’]t)"
NOT_DONE_RE = re.compile(
    r"\bpending\b|\bwait(?:s|ing)?\s+(?:on|for)\b|\bawaiting\b|\bnot\s+yet\b|\bqueued\b|\bunlanded\b"
    rf"|\b{_NEG}\s+(?:been\s+)?(?:yet\s+)?(?:live|landed|deployed|installed|done|merged|fixed)\b"
    r"|\bstill\s+(?:open|running|pending|waiting|queued|blocked|unlanded|red|in\s+progress)\b|\bblocked\s+(?:on|by)\b"
    r"|\bneeds?\s+(?:your|his)\s+(?:✅|ok\b|okay\b|approval\b|go\b|tap\b)",
    re.I,
)
DONE_RE = re.compile(r"(?<![\w/-])(?:live|landed|deployed|installed|done|merged|fixed)(?![\w/-])", re.I)
NOT_A_CLAIM = {  # word -> (text before it, text after it) that make it no claim
    "live": (
        re.compile(r"\b(?:go|goes|going|to)\s+$", re.I),
        re.compile(
            r"^\s+(?:calls?|reads?|data|state|cop(?:y|ies)|files?|feeds?|views?|streams?|pages?|logs?|checks?|probes?"
            r"|sessions?|versions?|cache|tests?|runs?|chats?|look|settings?|values?|sites?|tiles?|preview|time|status"
            r"|events?|traffic|edits?|sources?)\b",
            re.I,
        ),
    ),
    "fixed": (
        None,
        re.compile(
            r"^\s+(?:times?|prices?|costs?|dates?|rates?|ip|width|size|schedule|amounts?|fees?|points?|intervals?"
            r"|order|terms?|lines?|slots?)\b",
            re.I,
        ),
    ),
    "done": (None, re.compile(r"^\s+when\b", re.I)),
}
COND_RE = re.compile(
    r"\b(?:will|won['’]t|once|after|as\s+soon\s+as|if|unless|going\s+to|would|should|could|might)\b|\w['’]ll\b", re.I
)
SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
CLAUSE_SPLIT = re.compile(r"\s*[;·•|]\s*|\s+[—–-]\s+")
PLAN_RE = re.compile(r"(?<![\w-])[A-Z]{1,3}\d{1,2}(?:-\d{1,2})?(?![\w-])")
TOKEN_RE = re.compile(r"(?<![\w./-])[A-Za-z0-9][\w./-]*[A-Za-z0-9](?![\w/-])")
SHA_RE = re.compile(r"(?<!\w)[0-9a-f]{7,40}(?!\w)")
ROLLBACK_RE = re.compile(r"\b(?:rolled\s+back|reverted|disabled|paused|switched\s+off|turned\s+off)\b", re.I)
LOG_LINE = re.compile(
    r"^- (?:(\d{4})-(\d{2})-(\d{2})|(\d{1,2})\.(\d{1,2})\.?)\s+(?:(\d{1,2}):(\d)[\dx]\s+)?(?:—\s+)?(.+)$"
)


def _blank(s: str) -> str:
    return re.sub(r"[^\n]", " ", s)


def prose_lines(text: str) -> list[tuple[int, str, str]]:
    """(line number, item view, claim view) of the visible lines. Both views keep every character position: code
    fences and URLs blanked in both, inline code blanked in the claim view (its words are no claim) and unwrapped in
    the item view (a `branch-name` still names the branch); quoted lines (">") are skipped."""
    t = FENCE.sub(lambda m: _blank(m.group(0)), text or "")
    t = URL.sub(lambda m: _blank(m.group(0)), t)
    iv = INLINE.sub(lambda m: " " + m.group(0)[1:-1] + " ", t)
    cv = INLINE.sub(lambda m: _blank(m.group(0)), t)
    out = []
    for n, (li, lc) in enumerate(zip(iv.split("\n"), cv.split("\n"), strict=True), 1):
        if lc.strip() and not lc.lstrip().startswith(">"):
            if lc.lstrip().startswith("|"):  # a table row is one clause: its cells pair up
                li, lc = li.replace("|", ","), lc.replace("|", ",")
            out.append((n, li, lc))
    return out


def _spans(text: str, sep: re.Pattern[str], base: int = 0) -> list[tuple[int, int]]:
    out, pos = [], 0
    for m in sep.finditer(text):
        out.append((base + pos, base + m.start()))
        pos = m.end()
    out.append((base + pos, base + len(text)))
    return [(s, e) for s, e in out if e > s]


def clauses(cv: str) -> list[tuple[int, int]]:
    """Spans of the clauses that can carry a claim: not in a question sentence, no future/conditional wording."""
    out = []
    for s0, e0 in _spans(cv, SENTENCE_END):
        if cv[s0:e0].rstrip().endswith("?"):
            continue
        for s, e in _spans(cv[s0:e0], CLAUSE_SPLIT, s0):
            if cv[s:e].strip() and not COND_RE.search(cv[s:e]):
                out.append((s, e))
    return out


def claims(cv: str) -> list[tuple[int, int, str]]:
    """(start, end, "notdone" | "done") claims in a clause's claim view; a negated done word is a "notdone"."""
    out = [(m.start(), m.end(), "notdone") for m in NOT_DONE_RE.finditer(cv)]
    masked = cv
    for s, e, _ in out:
        masked = masked[:s] + " " * (e - s) + masked[e:]
    for m in DONE_RE.finditer(masked):
        before, after = NOT_A_CLAIM.get(m.group(0).lower(), (None, None))
        if (before and before.search(masked[: m.start()])) or (after and after.search(masked[m.end() :])):
            continue
        out.append((m.start(), m.end(), "done"))
    return sorted(out)


def nearest(cl: list[tuple[int, int, str]], s: int, e: int, cv: str = "") -> str:
    """The claim nearest to an item span in its clause: fewest commas between them, then the smallest gap (a tie
    goes to the claim after the item). "Rules review: done, H6 not live yet" pairs H6 with "not live"."""

    def cost(c: tuple[int, int, str]) -> tuple[int, int, bool]:
        lo, hi = (e, c[0]) if c[0] >= e else (c[1], s)
        return cv[lo:hi].count(","), max(0, hi - lo), c[0] < e

    return min(cl, key=cost)[2]


def _ts(y: int, mo: int, d: int, h: int = 0, mi: int = 0) -> float:
    return time.mktime((y, mo, d, h, mi, 0, 0, 0, -1))


def hm(ts: float | None) -> str:
    return time.strftime("%d.%m %H:%M", time.localtime(ts)) if ts else "?"


def reality_paths() -> dict[str, str]:
    st, ws = os.path.join(home(), ".local", "state"), os.path.join(home(), "workspace")
    return {
        "deploys": ENV("CONDUCT_DEPLOYS") or os.path.join(st, "assistant", "deploys.jsonl"),
        "board": ENV("CONDUCT_BOARD_FEED") or os.path.join(st, "board", "board.json"),
        "proactive": ENV("CONDUCT_PROACTIVE") or os.path.join(ws, "PROACTIVE.md"),
        "changelog": ENV("CONDUCT_CHANGELOG") or os.path.join(ws, "CHANGELOG.md"),
    }


def repo_dir() -> str:
    return ENV("CONDUCT_REPO") or os.path.join(home(), "opt", "assistant")


def git(args: list[str], timeout: float) -> subprocess.CompletedProcess[str] | None:
    g = ENV("CONDUCT_GIT") or "git"
    if g == "off" or not os.path.isdir(repo_dir()):
        return None
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0")
    try:
        return subprocess.run(
            [g, "-C", repo_dir(), *args], capture_output=True, text=True, timeout=timeout, env=env, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None


def branch_name_ok(b: str) -> bool:
    """Branch names a message can name unambiguously: 4+ characters with a "-", "/" or digit (not "fixes")."""
    return len(b) >= 4 and b != "main" and bool(re.search(r"[-/\d]", b)) and bool(re.search(r"[a-z]", b))


def title_head(title: str) -> str:
    return re.split(r" \(| — |; ", re.sub(r"\*\*", "", title), maxsplit=1)[0].strip()


def _lands(path: str, items: dict[str, dict[str, Any]]) -> None:
    try:
        with open(path, encoding="utf-8") as f:
            lines = [ln for ln in f if '"land"' in ln or '"deploy"' in ln]
    except OSError:
        return  # no land log: no lands
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if not isinstance(r, dict):
            continue
        ts = float(r.get("end") or r.get("ts") or 0)
        sha = str(r.get("sha") or r.get("release") or "")
        if r.get("op") == "land" and isinstance(r.get("branch"), str):
            b = r["branch"]
            it = items.setdefault(f"branch:{b}", {"key": b, "kind": "branch", "state": "unknown"})
            if r.get("ok"):
                it.update(
                    state="landed", since=ts, ok=True, evidence=f"landed {hm(ts)}" + (f" ({sha[:7]})" * bool(sha))
                )
                if re.fullmatch(r"[0-9a-f]{7,40}", sha):
                    items[f"sha:{sha[:7]}"] = {
                        "key": sha[:12],
                        "aliases": [b],
                        "kind": "release",
                        "state": "landed",
                        "since": ts,
                        "evidence": f"landed {hm(ts)} (branch {b})",
                    }
            elif not it.get("ok"):
                why = str(r.get("why") or "")[:20]
                it.update(
                    state="failed", since=ts, evidence=f"its last land failed {hm(ts)}" + (f" ({why})" * bool(why))
                )
        elif r.get("op") == "deploy" and r.get("ok") and re.fullmatch(r"[0-9a-f]{7,40}", sha):
            items[f"sha:{sha[:7]}"] = {
                "key": sha[:12],
                "kind": "release",
                "state": "live",
                "since": ts,
                "evidence": f"{r.get('target') or 'hub'} release deployed {hm(ts)}",
            }


def _refs(items: dict[str, dict[str, Any]]) -> None:
    r = git(["for-each-ref", "--format=%(refname:short)", "refs/heads"], 1.0)
    if r is None or r.returncode != 0:
        return
    for b in r.stdout.split():
        if branch_name_ok(b):
            items.setdefault(f"branch:{b}", {"key": b, "kind": "branch", "state": "unknown"})["ref"] = True


def _board(path: str, items: dict[str, dict[str, Any]]) -> None:
    d = read_json(path, None)
    if not isinstance(d, dict) or not isinstance(d.get("items"), list):
        return
    fresh = now() - float(d.get("ts") or 0) <= BOARD_STALE
    for row in d["items"]:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        head = title_head(str(row.get("title") or ""))
        kw = thing_words(head)
        if len(kw) < 2:
            continue
        probes = [p for p in row.get("probes") or [] if isinstance(p, dict)]
        fails = [str(p.get("spec") or "") for p in probes if p.get("state") == "fail"]
        state, ev = "unknown", "no probe (unverified)"
        if fresh and fails:
            state, ev = "pending", "its board probe fails: " + fails[0][:60]
        elif fresh and probes and all(p.get("state") == "pass" for p in probes):
            state, ev = "done", "its board probes all pass"
        items[f"todo:{row['id']}"] = {
            "key": str(row["id"]),
            "aliases": [head],
            "kind": "todo",
            "state": state,
            "since": None,
            "evidence": ev,
            "kw": kw,
        }


def _logs(paths: dict[str, str], items: dict[str, dict[str, Any]]) -> None:
    """Closed board items ("- DD.MM HH:MM Title: done", PROACTIVE.md) and plan ids (P3-2, H6, WP10) on a logged
    line with a done claim. Logged "not done" wording is narration (a sub-part, a quoted mistake) and changes nothing;
    a later rolled back / reverted / disabled / paused / switched off line makes the id unknown (no verdict)."""
    t0, year = now(), time.localtime(now()).tm_year
    events: list[tuple[float, int, str, str, str]] = []  # (ts, order, plan id, polarity, file)
    for name in ("proactive", "changelog"):
        try:
            with open(paths[name], encoding="utf-8") as f:
                lines = f.read().splitlines()
        except OSError:
            continue
        label = "PROACTIVE.md" if name == "proactive" else "CHANGELOG.md"
        for order, line in enumerate(lines):
            m = LOG_LINE.match(line)
            if not m:
                continue
            g = m.groups()
            h, mi = (int(g[5]), int(g[6]) * 10) if g[5] else (0, 0)
            try:
                if g[0]:
                    ts = _ts(int(g[0]), int(g[1]), int(g[2]), h, mi)
                else:
                    ts = _ts(year, int(g[4]), int(g[3]), h, mi)
                    if ts > t0 + 2 * 86400:
                        ts = _ts(year - 1, int(g[4]), int(g[3]), h, mi)
            except (ValueError, OverflowError):
                continue
            if t0 - ts > LOG_DAYS * 86400:
                continue
            rest = g[7]
            dm = re.match(r"(.+?): done\b", rest)
            if name == "proactive" and dm:
                head = title_head(dm.group(1))
                kw = thing_words(head)
                if len(kw) >= 2:
                    key = "p:" + hashlib.sha1(head.encode()).hexdigest()[:8]
                    items[f"todo:{key}"] = {
                        "key": key,
                        "aliases": [head],
                        "kind": "todo",
                        "state": "done",
                        "since": ts,
                        "evidence": f"closed {hm(ts)} (PROACTIVE.md)",
                        "kw": kw,
                    }
            if not PLAN_RE.search(rest):
                continue
            for _, iv, cv in prose_lines(rest):
                for s, e in clauses(cv):
                    back = ROLLBACK_RE.search(cv[s:e])
                    cl = claims(cv[s:e])
                    if not cl and not back:
                        continue
                    for pm in PLAN_RE.finditer(iv[s:e]):
                        pol = "back" if back else nearest(cl, pm.start(), pm.end(), cv[s:e])
                        events.append((ts, order, pm.group(0), pol, label))
    for ts, _, pid, pol, label in sorted(events):
        it = items.setdefault(f"plan:{pid}", {"key": pid, "kind": "plan", "state": "unknown"})
        if pol == "done":
            it.update(state="live", since=ts, evidence=f"logged live/done {hm(ts)} ({label})")
        elif pol == "back":
            it.update(state="unknown", evidence=f"logged rolled back/off {hm(ts)} ({label})")


def reality() -> list[dict[str, Any]]:
    """The reality cache (reality.json): rebuilt when a source file changes or it is older than REALITY_TTL."""
    paths = reality_paths()
    sig = []
    for k in sorted(paths):
        try:
            st = os.stat(paths[k])
            sig.append([st.st_mtime_ns, st.st_size])
        except OSError:
            sig.append([0, 0])
    cache = read_json(sp("reality.json"), {})
    if isinstance(cache, dict) and cache.get("sig") == sig and 0 <= now() - float(cache.get("ts") or 0) < REALITY_TTL:
        return cache.get("items") or []
    items: dict[str, dict[str, Any]] = {}
    _lands(paths["deploys"], items)
    _refs(items)
    _board(paths["board"], items)
    _logs(paths, items)
    out = [it for it in items.values() if it["kind"] != "plan" or it.get("since")]
    try:
        write_json(sp("reality.json"), {"sig": sig, "ts": now(), "items": out})
    except OSError:
        pass
    return out


class Reality:
    def __init__(self, items: list[dict[str, Any]]):
        self.keys: dict[tuple[str, str], dict[str, Any]] = {}
        self.titles: list[dict[str, Any]] = []
        for it in items:
            if it["kind"] == "todo":
                self.titles.append(it)
            elif it["kind"] == "release":
                self.keys[("sha", it["key"][:7])] = it
            else:
                self.keys[(it["kind"], it["key"])] = it
        self.git_left = GIT_BUDGET

    def names(self, iv: str) -> list[tuple[int, int, dict[str, Any]]]:
        """(start, end, item) for each reality item the clause names: plan ids, branch names and commit ids exactly,
        board titles by keyword cover. Titles of different states in one clause tell nothing (all unknown)."""
        out, seen = [], set()
        for m in PLAN_RE.finditer(iv):
            it = self.keys.get(("plan", m.group(0)))
            if it and id(it) not in seen:
                seen.add(id(it))
                out.append((m.start(), m.end(), it))
        for m in TOKEN_RE.finditer(iv):
            it = self.keys.get(("branch", m.group(0)))
            if it and id(it) not in seen:
                seen.add(id(it))
                out.append((m.start(), m.end(), it))
        for m in SHA_RE.finditer(iv):
            s = m.group(0)
            it = self.keys.get(("sha", s[:7]))
            if it and re.search(r"\d", s) and re.search(r"[a-f]", s) and it["key"].startswith(s[:12]):
                if id(it) not in seen:
                    seen.add(id(it))
                    out.append((m.start(), m.end(), it))
        ws, low, tit = line_words(iv), iv.lower(), []
        for it in self.titles:
            if covers(ws, it["kw"]) >= 0:
                at = max(0, low.find(it["kw"][0]))
                tit.append((at, at + len(it["kw"][0]), it))
        if len({t[2]["state"] for t in tit}) > 1:
            tit = [(s, e, dict(it, state="unknown", evidence="titles disagree")) for s, e, it in tit]
        return out + tit

    def state(self, it: dict[str, Any]) -> tuple[str, str]:
        """(state, evidence); a branch with a ref gets the live git check: on main or not."""
        st, ev = it.get("state", "unknown"), it.get("evidence", "")
        if it["kind"] != "branch" or not it.get("ref") or self.git_left <= 0:
            return st, ev
        t0 = time.monotonic()
        r = git(["merge-base", "--is-ancestor", f"refs/heads/{it['key']}", "refs/heads/main"], GIT_TIMEOUT)
        self.git_left -= time.monotonic() - t0
        if r is None or r.returncode not in (0, 1):
            return st, ev
        if r.returncode == 0:
            return ("landed", ev) if it.get("ok") else ("unknown", "on main, no land logged")
        if it.get("ok"):  # a land rebases: the old tip is no ancestor, but its commits are older than the land
            t1 = time.monotonic()
            c = git(["log", "-1", "--format=%ct", f"refs/heads/{it['key']}"], GIT_TIMEOUT)
            self.git_left -= time.monotonic() - t1
            if c is None or c.returncode != 0 or not c.stdout.strip().isdigit():
                return "unknown", "git log failed"
            if int(c.stdout.strip()) <= float(it.get("since") or 0) + 60:
                return "landed", ev
            return "unlanded", f"the branch has commits after its land {hm(it.get('since'))}, not on main"
        if st == "failed":
            return st, ev
        return "unlanded", "the branch is not on main and no land is logged"


def status_texts(tool: str, inp: dict[str, Any]) -> list[tuple[str, str]]:
    if tool.endswith("send_poll"):
        return [("", str(inp.get("question") or ""))]
    out = [("", str(inp.get("text") or ""))]
    if inp.get("details") and not tool.startswith("mcp__hub__"):
        out.append(("details ", str(inp["details"])))
    return out


def status_findings(tool: str, inp: dict[str, Any]) -> list[dict[str, Any]]:
    """C1, the pre-send status check: a "not done" claim on an item the setup records as landed/live/done, or a
    "done" claim on one it records as unlanded/pending/failed -> a finding (rule "status"). An item without
    evidence -> an "unverified" row. Fails open on its own (any error: no finding, one log line)."""
    try:
        return _status(tool, inp)
    except Exception as e:  # noqa: BLE001 - fail open, C1 alone: the other checks still run
        log(f"status: {type(e).__name__}: {str(e)[:120]}")
        record("status", "status", "error", tool, err=type(e).__name__)
        return []


def _status(tool: str, inp: dict[str, Any]) -> list[dict[str, Any]]:
    cands = []
    for label, text in status_texts(tool, inp):
        for n, iv, cv in prose_lines(text):
            for s, e in clauses(cv):
                cl = claims(cv[s:e])
                if cl:
                    cands.append((f"{label}line {n}", iv[s:e], cv[s:e], cl))
    if not cands:
        return []
    real = Reality(reality())
    out: list[dict[str, Any]] = []
    unverified = 0
    seen: set[tuple[str, str]] = set()
    for where, iv, cv, cl in cands:
        for s, e, it in real.names(iv):
            pol = nearest(cl, s, e, cv)
            if (it["key"], pol) in seen:
                continue
            seen.add((it["key"], pol))
            st, ev = real.state(it)
            name = (it.get("aliases") or [it["key"]])[0] if it["kind"] == "todo" else it["key"]
            meta = {"kind": it["kind"], "claim": pol, "state": st, "line": where}
            if (pol == "notdone" and st in DONE_STATES) or (pol == "done" and st in OPEN_STATES):
                said = "is not done yet" if pol == "notdone" else "is done/live"
                out.append(
                    {
                        "check": "status",
                        "rule": "status",
                        "item": it["key"],
                        "log": meta,
                        "msg": f"{where} says “{name}” {said}, but {st}: {ev}",
                    }
                )
            elif st == "unknown" and unverified < 5:
                unverified += 1
                record("status", "status", "unverified", tool, item=it["key"], **meta)
    return out


# ------------------------------------------------------------------------------------------------ shell parsing
HEREDOC = re.compile(r"(?<!<)<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
INTERP = ("python", "python3", "perl", "node", "ruby", "sh", "bash")


def strip_heredocs(cmd: str) -> tuple[str, list[str]]:
    """(command without heredoc bodies, bodies fed to an interpreter): a doc written with cat <<EOF is not a command."""
    lines, out, scripts, i = cmd.split("\n"), [], [], 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        for m in HEREDOC.finditer(line):
            body = []
            while i < len(lines) and lines[i].strip() != m.group(2):
                body.append(lines[i])
                i += 1
            i += 1
            first = os.path.basename(line.strip().split(" ", 1)[0]) if line.strip() else ""
            if first.startswith(INTERP):
                scripts.append("\n".join(body))
    return "\n".join(out), scripts


def split_cmd(cmd: str) -> list[list[str]]:
    """Pipelines (split at ; && || & newline and parentheses) of stages (split at |), outside quotes."""
    pipes: list[list[str]] = [[]]
    cur: list[str] = []
    q = ""
    i = 0

    def cut(new_pipe: bool) -> None:
        s = "".join(cur).strip()
        cur.clear()
        if s:
            pipes[-1].append(s)
        if new_pipe and pipes[-1]:
            pipes.append([])

    while i < len(cmd):
        c = cmd[i]
        if q:
            if c == "\\" and q == '"' and i + 1 < len(cmd):
                cur.append(cmd[i : i + 2])
                i += 2
                continue
            if c == q:
                q = ""
            cur.append(c)
        elif c == "\\" and i + 1 < len(cmd):
            cur.append(cmd[i : i + 2])
            i += 2
            continue
        elif c in "'\"":
            q = c
            cur.append(c)
        elif cmd.startswith(("&&", "||"), i):
            cut(True)
            i += 2
            continue
        elif c == "&" and ((cur and cur[-1].endswith((">", "<"))) or cmd[i + 1 : i + 2] == ">"):
            cur.append(c)  # 2>&1, &>file: a redirection, not a separator
        elif c in ";\n&()`":
            cut(True)
        elif c == "|":
            cut(False)
        else:
            cur.append(c)
        i += 1
    cut(True)
    return [p for p in pipes if p]


WRAPPERS = {"nice", "time", "env", "command", "exec", "sudo", "nohup", "stdbuf", "ionice", "builtin"}


def words(stage: str) -> tuple[list[str], int]:
    """(the stage's command words without env assignments and wrappers, the largest `timeout` it runs under)."""
    try:
        w = shlex.split(stage, comments=True, posix=True)
    except ValueError:
        w = stage.split()
    i, tmo = 0, 0
    while i < len(w):
        t = w[i]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t):
            i += 1
        elif t == "timeout":
            i += 1
            while i < len(w) and w[i].startswith("-"):
                i += 2 if w[i] in ("-k", "-s", "--kill-after", "--signal") else 1
            if i < len(w):
                tmo = max(tmo, duration(w[i]))
                i += 1
        elif t in WRAPPERS:
            i += 1
            while i < len(w) and w[i].startswith("-"):
                i += 1
        else:
            break
    return w[i:], tmo


def duration(s: str) -> int:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([smhd]?)", s.strip())
    if not m:
        return 0
    return int(float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)])


def name_of(w: list[str]) -> str:
    return os.path.basename(w[0]) if w else ""


# ------------------------------------------------------------------------------------------------ ack-first
def slow_words(w: list[str], tmo: int, depth: int = 0) -> str:
    n = name_of(w)
    a = w[1:]
    if n in ("sh", "bash") and "-c" in a and depth < 2:
        inner = a[a.index("-c") + 1] if a.index("-c") + 1 < len(a) else ""
        if tmo >= 60 and re.search(r"\b(?:until|while)\b", inner):
            return f"a wait loop under timeout {tmo} s"
        return slow_cmd(inner, depth + 1)
    if n == "vault" and a[:1] == ["ask"]:
        return "vault ask"
    if n == "vault" and a[:1] == ["job"] and "wait" in a[1:3]:
        return "vault job wait"
    if n == "assistant" and a[:1] and a[0] in ("land", "ci", "deploy"):
        return f"assistant {a[0]}"
    if n in ("test-all", "pytest", "run.sh"):
        return n
    if n.startswith("python") and a[:2] == ["-m", "pytest"]:
        return "pytest"
    if n == "uv" and "pytest" in a[:3]:
        return "pytest"
    if n == "bun" and a[:1] == ["test"]:
        return "bun test"
    if n == "hub" and a[:1] == ["deploy"]:
        return "hub deploy"
    if n == "sleep" and a and duration(a[0]) >= 20:
        return f"sleep {a[0]}"
    return ""


def slow_cmd(cmd: str, depth: int = 0) -> str:
    body, _ = strip_heredocs(cmd or "")
    for pipe in split_cmd(body):
        for stage in pipe:
            w, tmo = words(stage)
            s = slow_words(w, tmo, depth)
            if s:
                return s
    return ""


def slow_bash(inp: dict[str, Any]) -> list[dict[str, Any]]:
    if inp.get("run_in_background") is True:
        return []
    what = slow_cmd(str(inp.get("command") or ""))  # a big Bash `timeout` alone is no signal: 7 of 8 ran < 10 s
    return [ack_finding(what)] if what else []


def slow_hub(tool: str, inp: dict[str, Any]) -> list[dict[str, Any]]:
    if tool.endswith("deploy_run"):
        return [ack_finding("deploy_run")] if inp.get("plan") is False else []
    w = inp.get("wait_s", 20)
    return [ack_finding(f"{tool.rsplit('__', 1)[-1]} wait_s {w}")] if isinstance(w, int | float) and w > 20 else []


def ack_after() -> float:
    try:
        with open(os.path.join(config_dir(), "ack-after-s"), encoding="utf-8") as f:
            return max(5.0, float(f.read().strip()))
    except (OSError, ValueError):
        return ACK_AFTER_S


def slow_turn(turn: dict[str, Any]) -> list[dict[str, Any]]:
    """ackslow: he has waited ack_after() s and nothing is sent; once per turn (turn.json "ackslow")."""
    age = now() - float(turn.get("ts") or 0)
    if turn.get("ackslow") or age < ack_after():
        return []
    return [
        {
            "check": "ackfirst",
            "rule": "ackslow",
            "what": "elapsed",
            "n": int(age),
            "msg": f"he has waited {int(age)} s and nothing is sent yet: reply 1 line now (the answer so far, or"
            " what you're doing + ETA; a react alone doesn't show him a reply), then go on",
        }
    ]


def ack_finding(what: str) -> dict[str, Any]:
    return {
        "check": "ackfirst",
        "rule": "ackfirst",
        "what": what.split(" ")[0],
        "msg": f"he's waiting and nothing is sent yet ({what}): react ✍ or send 1 line + ETA first,"
        " then run it with run_in_background",
    }


def is_send_cmd(cmd: str) -> bool:
    for pipe in split_cmd(strip_heredocs(cmd or "")[0]):
        for stage in pipe:
            w, _ = words(stage)
            n = name_of(w)
            if n in ("voice-reply", "drive-update") or (n == "phone" and w[1:2] == ["chat"]):
                return True
    return False


# ------------------------------------------------------------------------------------------------ inbox
RM_CMDS = {"rm", "unlink", "shred", "trash", "trash-put", "srm"}
PY_RM = re.compile(r"os\.(?:remove|unlink)\s*\(|\.unlink\s*\(|shutil\.rmtree\s*\(|\bunlink\s*\(|\bunlinkSync\s*\(")


def inbox_rel(path: str) -> str | None:
    """The part after the inbox dir for a path into ~/inbox ("" = the dir itself), else None."""
    p = path.strip().strip("'\"")
    h = home()
    for pre in ("~/", "$HOME/", "${HOME}/", h.rstrip("/") + "/"):
        if p.startswith(pre):
            rest = p[len(pre) :]
            if rest == "inbox" or rest == "inbox/":
                return ""
            if rest.startswith("inbox/"):
                return rest[len("inbox/") :]
    return None


def is_note(rel: str | None) -> bool:
    if rel is None:
        return False
    if rel in ("", "*", ".", "./"):
        return True
    if "/" in rel.rstrip("/"):
        return False  # a subfolder (files/ ...): not a note
    return rel.endswith(".md") or any(c in rel for c in "*?[")


def local_note(arg: str) -> bool:
    a = arg.strip("'\"")
    return "/" not in a and (a.endswith(".md") or a in ("*", ".") or any(c in a for c in "*?["))


def mentions_notes(text: str) -> bool:
    return any(is_note(inbox_rel(t)) for t in re.findall(r"[^\s'\"()=,;]+", text or ""))


def inbox_hit(cmd: str, depth: int = 0) -> str:
    body, scripts = strip_heredocs(cmd or "")
    for s in scripts:
        if PY_RM.search(s) and mentions_notes(s):
            return "script"
    in_inbox = False
    for pipe in split_cmd(body):
        earlier_lists = False
        for stage in pipe:
            w, _ = words(stage)
            n, args = name_of(w), w[1:]
            plain = [a for a in args if not a.startswith("-")]
            if n == "cd":
                in_inbox = bool(plain) and inbox_rel(plain[0]) == ""
            elif n in RM_CMDS or (n == "gio" and args[:1] == ["trash"]):
                if any(is_note(inbox_rel(a)) or (in_inbox and local_note(a)) for a in plain):
                    return n
            elif n == "mv":
                if "-t" in args:
                    i = args.index("-t")
                    srcs = [a for j, a in enumerate(args) if not a.startswith("-") and j != i + 1]
                else:
                    srcs = plain[:-1]
                if any(is_note(inbox_rel(a)) or (in_inbox and local_note(a)) for a in srcs):
                    return "mv"
            elif n == "find":
                starts = []
                for a in args:
                    if a.startswith(("-", "(", "!")):
                        break
                    starts.append(a)
                tail = " ".join(args[len(starts) :])
                deletes = "-delete" in args or re.search(r"-exec(?:dir)?\s+(?:/\S*/)?(?:rm|unlink|shred|mv)\b", tail)
                if deletes and any(inbox_rel(s) == "" or (in_inbox and s in (".", "./")) for s in starts):
                    return "find -delete"
            elif n == "xargs":
                rest = [a for a in args if not a.startswith("-")]
                if rest and os.path.basename(rest[0]) in RM_CMDS | {"mv"} and earlier_lists:
                    return "xargs " + os.path.basename(rest[0])
            elif n in ("sh", "bash") and "-c" in args and depth < 2:
                i = args.index("-c")
                hit = inbox_hit(args[i + 1] if i + 1 < len(args) else "", depth + 1)
                if hit:
                    return hit
            elif n.startswith(INTERP) and PY_RM.search(stage) and (mentions_notes(stage) or in_inbox):
                return "script"
            if any(is_note(inbox_rel(a)) for a in args) or (in_inbox and n in ("ls", "find", "printf", "echo")):
                earlier_lists = True
    return ""


def inbox_findings(cmd: str) -> list[dict[str, Any]]:
    how = inbox_hit(cmd)
    if not how:
        return []
    return [
        {
            "check": "inbox",
            "rule": "inbox",
            "what": how,
            "msg": f"{how} on an ~/inbox note: notes go via `events ack ID` (it deletes the note; `events list`);"
            " by hand only when the events CLI is down",
        }
    ]


# ------------------------------------------------------------------------------------------------ deny-once
KEY_FIELDS = {
    "Bash": ("command",),
    "Monitor": ("command",),
    TP + "reply": ("text", "details", "buttons", "keyboard", "approval"),
    TP + "send_poll": ("question", "options", "multiple"),
}


def input_key(tool: str, inp: Any) -> str:
    """The resend identity: what the check judged (a Bash call's description or a reply's chat_id may change)."""
    if isinstance(inp, dict):
        keep = KEY_FIELDS.get(tool)
        inp = {k: v for k, v in inp.items() if k != "_caller" and (keep is None or k in keep)}
    raw = tool + "\0" + json.dumps(inp, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


def decide(tool: str, inp: Any, findings: list[dict[str, Any]], main: bool) -> str | None:
    """The deny reason, or None (allow). Shadowed rules are only logged; a held identical input passes once more."""
    active = []
    for f in findings:
        if shadow(f["rule"]):
            record(
                f["check"],
                f["rule"],
                "shadow",
                tool,
                item=f.get("item"),
                what=f.get("what"),
                n=f.get("n"),
                **(f.get("log") or {}),
            )
        else:
            active.append(f)
    if not active:
        return None
    key = input_key(tool, inp)
    t = now()
    with locked():
        held = read_json(sp("held.json"), {})
        held = (
            {k: v for k, v in held.items() if isinstance(v, int | float) and t - v <= HELD_TTL}
            if isinstance(held, dict)
            else {}
        )
        again = key in held
        if again:
            held.pop(key)
        else:
            held[key] = t
        write_json(sp("held.json"), held)
    for f in active:
        record(
            f["check"],
            f["rule"],
            "override" if again else "hold",
            tool,
            h=key[:8],
            main=main,
            item=f.get("item"),
            what=f.get("what"),
            n=f.get("n"),
            **(f.get("log") or {}),
        )
    if again:
        return None
    checks = sorted({f["check"] for f in active})
    return (
        f"[conduct:{','.join(checks)}] Held once: "
        + " · ".join(f["msg"] for f in active)
        + ". Fix it, or resend unchanged if you checked and it's right (the identical retry goes through)."
    )


def deny(reason: str) -> str:
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        },
        ensure_ascii=False,
    )


# ------------------------------------------------------------------------------------------------ hook entry points
def on_prompt(ev: dict[str, Any]) -> str:
    if not is_main(ev):
        return ""
    prompt = str(ev.get("prompt") or "")
    kind, attrs = classify(prompt)
    turn: dict[str, Any] = {
        "sid": ev.get("session_id") or "",
        "ts": now(),
        "kind": kind,
        "ev": attrs.get("event_type", ""),
        "sent": False,
    }
    if kind == "owner":
        body = CHANNEL.sub(" ", prompt[:8000])
        turn["kwh"] = text_hashes(re.sub(r"</channel>", " ", body))
    with locked():
        write_json(sp("turn.json"), turn)
    return ""


def on_pre(ev: dict[str, Any]) -> str:
    tool = str(ev.get("tool_name") or "")
    inp = ev.get("tool_input")
    if not isinstance(inp, dict) or flag("off"):
        return ""
    main = is_main(ev)
    turn = load_turn() if main else {}
    waiting = main and turn_open(turn, ev)
    findings: list[dict[str, Any]] = []
    sent = False
    if tool == "Bash":
        cmd = str(inp.get("command") or "")
        if enabled("inbox"):
            findings += inbox_findings(cmd)
        if main and is_send_cmd(cmd):
            sent = True
        elif waiting and enabled("ackfirst"):
            findings += slow_bash(inp) or slow_turn(turn)
    elif tool == "Monitor":  # its command is a shell script too; a background start, so no ackfirst/ackslow
        if enabled("inbox"):
            findings += inbox_findings(str(inp.get("command") or ""))
    elif tool in HUB_WAIT:
        if waiting and enabled("ackfirst"):
            findings += slow_hub(tool, inp) or slow_turn(turn)
    elif tool in LINT_TOOLS:
        sent = main
        if enabled("sendlint"):
            fresh = turn.get("kind") == "owner" and now() - float(turn.get("ts") or 0) <= TURN_MAX_AGE
            kwh = set(turn.get("kwh") or []) if fresh else None
            findings += sendlint(tool, inp, kwh)
    elif tool in SEND_TOOLS:
        sent = main
    if tool in STATUS_TOOLS and enabled("status"):
        findings += status_findings(tool, inp)
    reason = decide(tool, inp, findings, main) if findings else None
    if waiting and any(f["rule"] == "ackslow" for f in findings):
        update_turn(ackslow=True)  # once per turn, held or shadowed
    if reason:
        return deny(reason)
    if sent and waiting:
        update_turn(sent=True, sent_ts=now())
    return ""


def running_work() -> int | None:
    """Running or stalled background agents (agent-watch) + running tasks; None when agent-watch can't say."""
    aw = ENV("CONDUCT_AGENT_WATCH") or os.path.join(home(), "bin", "agent-watch")
    try:
        r = subprocess.run([aw, "status", "--hours", "6", "--json"], capture_output=True, text=True, timeout=1.5)
        d = json.loads(r.stdout)
        items = d.get("agents", []) if isinstance(d, dict) else d
        live = ("running", "stalled")
        n = sum(1 for a in items if isinstance(a, dict) and (a.get("status") or a.get("state")) in live)
    except (OSError, ValueError, subprocess.SubprocessError, AttributeError):
        return None
    tb = ENV("CONDUCT_TASKS_BIN") or os.path.join(home(), "bin", "tasks")
    try:
        r = subprocess.run([tb, "list", "--json"], capture_output=True, text=True, timeout=1.5)
        rows = json.loads(r.stdout or "[]")
        n += sum(1 for t in rows if isinstance(t, dict) and t.get("state") == "running")
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return n


def heavy_procs() -> int:
    """Lands, CI runs and full suites running on this host (a background `assistant land` is work in progress)."""
    n, proc = 0, ENV("CONDUCT_PROC_DIR") or "/proc"
    try:
        pids = os.listdir(proc)
    except OSError:
        return 0
    for pid in pids:
        if not pid.isdigit():
            continue
        try:
            with open(f"{proc}/{pid}/cmdline", "rb") as f:
                cl = f.read(600).replace(b"\0", b" ")
        except OSError:
            continue
        if re.search(rb"\bassistant (?:land|ci)\b|/test-all\b|^test-all\b", cl):
            n += 1
    return n


def wake_hold() -> bool:
    """An active `wake next` hold: I parked idle beats on purpose, with a reason."""
    wb = ENV("CONDUCT_WAKE_BIN") or os.path.join(home(), "bin", "wake")
    try:
        r = subprocess.run([wb, "list", "--json"], capture_output=True, text=True, timeout=1.5)
        d = json.loads(r.stdout)
        return any(isinstance(x, dict) and x.get("klass") == "hold" for x in d.get("rules") or [])
    except (OSError, ValueError, subprocess.SubprocessError, AttributeError):
        return False


def startable() -> dict[str, int] | None:
    """The agenda's Startable counts (core.todo: open GOALS.md steps, TODO Ideas, Night backlog)."""
    use_lib()
    try:
        from core import todo  # type: ignore[import-not-found]

        b = todo.read(ENV("CONDUCT_TODO") or None, ENV("CONDUCT_GOALS") or None)
        return dict(b.startable) if b.ok else None
    except Exception as e:  # noqa: BLE001 - fail open
        log(f"stop: startable failed: {type(e).__name__}")
        return None


def on_stop(ev: dict[str, Any]) -> str:
    if not is_main(ev):
        return ""
    turn = load_turn()
    kind = turn.get("kind")
    if turn and not turn.get("ended"):
        update_turn(ended=True)
    if ev.get("stop_hook_active") or kind not in ("beat", "loop") or turn.get("warned") or not enabled("idle"):
        return ""
    if turn.get("sid") and ev.get("session_id") and turn["sid"] != ev["session_id"]:
        return ""
    s = startable()
    if not s or sum(s.values()) == 0:
        return ""
    busy = running_work()
    if busy is None or busy > 0 or heavy_procs() > 0 or wake_hold():
        return ""
    update_turn(warned=True)
    if shadow("idle"):
        record("idle", "idle", "shadow", "Stop", kind=kind)
        return ""
    record("idle", "idle", "warn", "Stop", kind=kind, n=sum(s.values()))
    reason = (
        f"[conduct:idle] This {kind} ends with 0 agents running and startable work (goals {s.get('goals', 0)},"
        f" ideas {s.get('ideas', 0)}, night {s.get('night', 0)}): launch 2-3 background builds now (Night"
        ' backlog, GOALS.md), or park on purpose: `wake next +2h "why"` or the wait reason in TODO.md.'
    )
    return json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False)


def holds(argv: list[str]) -> int:
    days = 7.0
    if "--days" in argv:
        days = float(argv[argv.index("--days") + 1])
    since = now() - days * 86400
    counts: dict[str, dict[str, int]] = {}
    try:
        with open(sp("holds.jsonl"), encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if float(r.get("ts") or 0) < since:
                    continue
                k = r.get("check", "?") if r.get("rule") == r.get("check") else f"{r.get('check')}/{r.get('rule')}"
                counts.setdefault(k, {})
                counts[k][r.get("act", "?")] = counts[k].get(r.get("act", "?"), 0) + 1
    except OSError:
        pass
    sw = sorted(os.listdir(config_dir())) if os.path.isdir(config_dir()) else []
    if "--json" in argv:
        print(json.dumps({"days": days, "counts": counts, "switches": sw}))
        return 0
    print(f"conduct, last {days:g} days" + (f" (switches: {', '.join(sw)})" if sw else " (all checks on)"))
    for k in sorted(counts):
        c = counts[k]
        acts = ("hold", "override", "warn", "shadow", "unverified", "error")
        print(f"  {k}: " + ", ".join(f"{c[a]} {a}" for a in acts if a in c))
    if not counts:
        print("  nothing held")
    return 0


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "holds":
        return holds(argv[2:])
    if cmd not in ("pre", "prompt", "stop"):
        print(__doc__)
        return 0 if cmd in ("-h", "--help", "help") else 2
    t0 = time.monotonic()
    try:
        ev = json.loads(sys.stdin.read() or "{}")
        if not isinstance(ev, dict):
            return 0
        out = {"pre": on_pre, "prompt": on_prompt, "stop": on_stop}[cmd](ev)
        if out:
            sys.stdout.write(out + "\n")
    except Exception as e:  # noqa: BLE001 - fail open: allow, one log line
        log(f"{cmd}: {type(e).__name__}: {str(e)[:120]}")
        with contextlib.suppress(Exception):
            record(cmd, cmd, "error", err=type(e).__name__)
    ms = int((time.monotonic() - t0) * 1000)
    if ms > 800:
        log(f"{cmd}: slow run {ms} ms")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
