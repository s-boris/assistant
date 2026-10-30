#!/usr/local/bin/python3.13 -I
# ruff: noqa: E501
"""Command stub for this environment: records each call ($HOME/.eval/stubs.jsonl) and answers from canned rules
($HOME/.eval/case.json "stubs": [{"cmd", "match", "after", "out", "exit"}]) or from defaults; `--help` shows the real
script's own usage header ($HOME/.eval/help/NAME.txt). remind/shop/lists keep their items in $HOME/.eval/state.json.
No network, nothing outside $HOME."""

import datetime as dt
import json
import os
import re
import select
import shlex
import sys
import time

HOME = os.environ.get("HOME", "")
EV = os.path.join(HOME, ".eval")
NAME = os.path.basename(sys.argv[0])
ARGS = sys.argv[1:]
LINE = " ".join([NAME] + [shlex.quote(a) for a in ARGS])


def load_case():
    try:
        with open(os.path.join(EV, "case.json")) as f:
            return json.load(f)
    except Exception:
        return {}


CASE = load_case()


def clock():
    c = CASE.get("clock")
    if c:
        try:
            return dt.datetime.fromisoformat(c)
        except ValueError:
            pass
    return dt.datetime.now()


def stdin_head():
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return ""
        r, _, _ = select.select([sys.stdin], [], [], 0.2)
        return sys.stdin.read(4000) if r else ""
    except Exception:
        return ""


def record(code, out, inp):
    try:
        os.makedirs(EV, exist_ok=True)
        with open(os.path.join(EV, "stubs.jsonl"), "a") as f:
            f.write(
                json.dumps(
                    {
                        "ts": time.time(),
                        "cmd": NAME,
                        "args": ARGS,
                        "line": LINE,
                        "stdin": inp[:2000],
                        "exit": code,
                        "out": out[:300],
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    except Exception:
        pass


def fmt_when(args):
    for a in args:
        if re.match(r"^\d{4}-\d{2}-\d{2}", a):
            return a
    return "the requested time"


def state(update=None):
    p = os.path.join(EV, "state.json")
    try:
        st = json.load(open(p))
    except Exception:
        st = json.loads(json.dumps(CASE.get("state") or {}))  # seeded by the case
    if update:
        update(st)
        with open(p + ".tmp", "w") as f:
            json.dump(st, f)
        os.replace(p + ".tmp", p)
    return st


def hex_id(seed, n=8):
    import hashlib

    return hashlib.sha1(seed.encode()).hexdigest()[:n]


ITEM_RE = re.compile(r"^\s*(\S+)\s*·\s*(.*?)\s*·\s*(.*)$")
VALUE_FLAGS = (
    "--every",
    "--until",
    "--tag",
    "--on",
    "--via",
    "--match",
    "--max",
    "--for",
    "--section",
    "--src",
    "--date",
    "--details",
    "--scope",
    "--after",
    "--source",
    "--grep",
    "--supersedes",
)


def positional(args):
    """Non-option arguments (values of the known value options skipped)."""
    out, skip = [], False
    for a in args:
        if skip:
            skip = False
            continue
        if a in VALUE_FLAGS:
            skip = True
            continue
        if not a.startswith("--"):
            out.append(a)
    return out


def opt(args, flag):
    for i, a in enumerate(args):
        if a == flag and i + 1 < len(args):
            return args[i + 1]
        if a.startswith(flag + "="):
            return a.split("=", 1)[1]
    return None


def remind_items():
    """The case's 'remind' state ("ID · WHEN · TEXT") in the shape of `remind list --json` items."""
    out = []
    for s in state().get("remind", []):
        m = ITEM_RE.match(s)
        rid, when, text = (m[1], m[2], m[3]) if m else (s.split()[0], "", s)
        at = re.match(r"(\d{4}-\d\d-\d\d)[ T](\d\d:\d\d)", when)
        item = {"id": rid, "text": text, "owner": True}
        if at:
            item.update(kind="timed", at=f"{at[1]} {at[2]}")
        elif re.match(r"(?i)(📍 ?)?(home|leave|zone:)", when):
            item.update(kind="place", on=when.removeprefix("📍").strip().split()[0])
        elif re.match(r"(?i)every\b", when):
            item.update(kind="series", every=when[6:].strip() or "weekly")
        else:
            item.update(kind="timed")
        out.append(item)
    return out


def remind_cmd(sub, args):
    """remind add / list [--json] / cancel|rm …: the real CLI's output lines (8-hex IDs, "added …", "cancelled: …")."""
    rest = args[1:]
    if sub == "add":
        pos = positional(rest)
        rid = hex_id(LINE + str(len(state().get("remind", []))))
        on = opt(rest, "--on")
        if on:
            text = " ".join(pos)
            state(lambda st: st.setdefault("remind", []).append(f"{rid} · 📍 {on} · {text}"))
            return f"added location reminder {rid}: 📍 {on} — {text}", 0
        text = pos[-1] if len(pos) > 1 else " ".join(pos)
        when = " ".join(pos[:-1]) if len(pos) > 1 else ""
        m = re.search(r"(\d{4}-\d\d-\d\d)[ T]?(\d{1,2}:\d\d)?", when)
        if m:
            hhmm = (m[2] or "09:00").zfill(5)
            d = dt.datetime.fromisoformat(f"{m[1]} {hhmm}")
            stamp = d.strftime("%a %Y-%m-%d %H:%M")
            wtxt = f"{m[1]} {hhmm}"
        else:
            stamp, wtxt = when or "the requested time", when or "the requested time"
        every = opt(rest, "--every")
        if every:
            wtxt = f"every {every} {wtxt}".strip()
        state(lambda st: st.setdefault("remind", []).append(f"{rid} · {wtxt} · {text}"))
        own = "owner " if "--owner" in rest else ""
        return f"added {own}reminder for {stamp}" + (f", every {every}" if every else ""), 0
    if sub in ("list", "ls", "show", "card", ""):
        if "--json" in args:
            return json.dumps(
                {"now": clock().strftime("%Y-%m-%d %H:%M"), "items": remind_items(), "recent": []}, ensure_ascii=False
            ), 0
        got = state().get("remind", [])
        return ("\n".join(got) if got else "No reminders."), 0
    if sub in ("rm", "del", "delete", "cancel", "remove", "skip", "done"):
        ids = positional(rest)
        match = (opt(rest, "--match") or "").lower()
        hit = [
            x
            for x in state().get("remind", [])
            if (ids and any(x.split()[0].startswith(i) or i.startswith(x.split()[0]) for i in ids))
            or (match and match in x.lower())
        ]
        if not hit:
            return f"remind: no reminder {' '.join(ids) or match}", 1
        state(lambda st: st.__setitem__("remind", [x for x in st.get("remind", []) if x not in hit]))
        return "\n".join("cancelled: " + ("  ".join(m.group(2, 3)) if (m := ITEM_RE.match(x)) else x) for x in hit), 0
    if sub == "when":
        return json.dumps({"ok": False, "error": "no clock parser in the eval sandbox: pass YYYY-MM-DD HH:MM"}), 1
    return "OK", 0


STOP = {
    "what",
    "which",
    "when",
    "where",
    "with",
    "from",
    "that",
    "this",
    "again",
    "have",
    "does",
    "about",
    "there",
    "they",
}


def facts_rows():
    """(id, section, text) per fact line of the sandbox's FACTS.md: `memory show` / `kb ask` answer from it."""
    rows, sec = [], ""
    try:
        lines = open(os.path.join(HOME, "workspace", "FACTS.md"), encoding="utf-8").read().splitlines()
    except OSError:
        return rows
    for ln in lines:
        if ln.startswith("## "):
            sec = ln[3:].strip()[:16]
        elif ln.startswith("- "):
            rows.append(("r" + hex_id(ln, 10), sec, ln[2:]))
    return rows


VERBS = ("declined", "skipped", "scratched", "removed", "off", "done", "decided", "kept")
DECIDE_EXAMPLE = (
    'e.g. memory decide declined "Ryanair flights" --details "never again"  or one ledger line: '
    'memory decide "21.10 declined: Ryanair flights (never again)"'
)
LEDGER_ARG = re.compile(r"^(?:-\s+)?(?:(\d{1,2}\.\d{1,2}(?:\.\d{4})?)\.?\s+)?([A-Za-z]+):\s*(.*)$")
THING_SPLIT = re.compile(r" \(| — |; ")
DM_IN = re.compile(r"(?<![\d.])\d{1,2}\.\d{1,2}(?!\d)")


def ledger_parts(rest):
    """(thing, details) of a ledger line's text after "verb: ", split the way the real importer splits it."""
    head = THING_SPLIT.split(rest, maxsplit=1)[0]
    det = re.sub(r"^(?:;|—)\s*", "", rest[len(head) :].strip())
    if det.startswith("(") and det.endswith(")"):
        det = det[1:-1]
    return head.strip(), det or None


def short_ledger(line):
    """A ledger line as the FACTS core shortens a long one: "- DD.MM verb: thing"."""
    m = re.match(r"^- (\S+) ([a-z]+): (.+)$", line)
    return f"- {m[1]} {m[2]}: {ledger_parts(m[3])[0]}" if m else line


def row_id(line):
    return "r" + hex_id(line, 10)


def mem_files():
    return [os.path.join(HOME, "workspace", f) for f in ("FACTS.md", "FACTS-core.md")]


def mem_read(path):
    try:
        return open(path, encoding="utf-8").read().split("\n")
    except OSError:
        return None


def mem_save(path, lines):
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def line_of(path, line):
    lines = mem_read(path) or []
    return lines.index(line) + 1 if line in lines else None


def mem_history(update=None):
    """Superseded / removed rows the sandbox has seen: {id: {"line", "status", "by"}} (for `memory show OLD_ID`)."""
    if update:
        state(lambda st: update(st.setdefault("_memory_rows", {})))
    return state().get("_memory_rows", {})


def mem_report(op, rid, line, old_line=None, old_id=None, section="", dry=False, removed=False):
    """The real CLI's write report: ids, the FACTS.md line number and text, the FACTS-core.md place, the diff."""
    facts, core = mem_files()
    sec = f" ({section})" if section else ""
    what = {
        "set": f"new row {rid}",
        "decide": f"new ledger row {rid}" + (f", supersedes {old_id} (kept as history)" if old_id else ""),
        "supersede": f"new row {rid} replaces {old_id} (the old row stays as superseded history)",
        "remove": f"row {rid} retired (status removed, kept as history)",
    }[op]
    head = f"memory {op}: " + (
        f"dry run, nothing written · would be: {what}" if dry else f"written (rows mode) · {what}"
    )
    out = [head]
    if removed:
        out.append(f"  FACTS.md: line {line_of(facts, old_line) or '?'}{sec} {'would be ' if dry else ''}deleted")
    else:
        n = line_of(facts, line) if not dry else None
        out.append(f"  FACTS.md line {n or '?'}{sec} {'would read' if dry else 'now reads'}: {line}")
    if not dry:
        if removed:
            out.append("  FACTS-core.md re-rendered: the line is gone from it too")
        elif line_of(core, line):
            out.append(f"  FACTS-core.md line {line_of(core, line)} (re-rendered): the same line")
        elif line_of(core, short_ledger(line)):
            out.append(
                f"  FACTS-core.md line {line_of(core, short_ledger(line))} (re-rendered): {short_ledger(line)} "
                "(shortened to the thing)"
            )
        else:
            out.append(
                "  FACTS-core.md re-rendered: this row stays on file only (rule long; `memory core explain` lists why)"
            )
    if old_line:
        out.append(f"    -{old_line}")
    if not removed:
        out.append(f"    +{line}")
    if not dry:
        out.append(
            f"  FACTS.md and FACTS-core.md already re-rendered: no read-back needed (`memory show {rid}` = the row "
            "and its history)"
        )
    return "\n".join(out)


def memory_write(sub, rest):
    """memory set|decide|supersede on the sandbox's FACTS.md (and FACTS-core.md), as the rows would render, with the
    real CLI's report (row ids, FACTS.md / FACTS-core.md line numbers, the diff) and its usage errors."""
    dry = "--dry-run" in rest
    pos = positional(rest)
    files = mem_files()
    date = opt(rest, "--date") or clock().strftime("%d.%m")

    def edit(path, fn):
        lines = mem_read(path)
        if lines is None:
            return False
        new = fn(lines)
        if new is None:
            return False
        if not dry:
            mem_save(path, new)
        return True

    if sub == "decide":
        verb, thing, det = (pos + [None, None])[:2] + [opt(rest, "--details")]
        if verb and thing is None:  # one ledger-shaped string
            m = LEDGER_ARG.match(" ".join(verb.split()))
            if not m or m[2].lower() not in VERBS or not m[3].strip():
                return (
                    f'memory decide: not a ledger line: {verb!r}. Give VERB "THING" or one string '
                    f'"[DD.MM ]verb: thing (details)" with verb {"|".join(VERBS)}; {DECIDE_EXAMPLE}'
                ), 2
            verb, (thing, d2) = m[2].lower(), ledger_parts(m[3].strip())
            det = det or d2
            date = opt(rest, "--date") or (m[1] or date).rstrip(".")
        if not verb:
            return (
                f"usage: memory decide VERB [THING] ...\nmemory decide: error: the following arguments are required: verb; {DECIDE_EXAMPLE}",
                2,
            )
        verb = verb.lower().rstrip(":")
        if verb not in VERBS:
            return f"memory decide: verb {verb!r}: one of {'|'.join(VERBS)}; {DECIDE_EXAMPLE}", 3
        thing = " ".join(thing.split())
        if not thing or THING_SPLIT.search(thing):
            return (
                "memory decide: the thing is the 2–4 words naming it: no '(', ' — ' or '; ' (details go in --details, "
                f"or pass the whole line as one string); {DECIDE_EXAMPLE}"
            ), 3
        line = f"- {date} {verb}: {thing}" + (f" ({det.strip()})" if det and det.strip() else "")
        old_id = opt(rest, "--supersedes")
        old_line = None
        if old_id:
            old_line = next(("- " + t for i, _, t in facts_rows() if i == old_id), None)
            if old_line is None:
                return f"memory decide: no active row {old_id} (memory show --grep WORD finds the id)", 6

        def ins(lines):
            if old_line:
                lines = [x for x in lines if x not in (old_line, short_ledger(old_line))]
            h = next((i for i, x in enumerate(lines) if x.startswith("## ") and "ledger" in x.lower()), None)
            if h is None:
                return lines + [line]
            j = next(
                (i for i in range(h + 1, len(lines)) if lines[i].startswith("- ") or lines[i].startswith("## ")),
                len(lines),
            )
            return lines[:j] + [line] + lines[j:]

        for f in files:
            edit(f, ins)
        if old_line and not dry:
            mem_history(lambda h: h.__setitem__(old_id, {"line": old_line, "status": "superseded", "by": row_id(line)}))
        return mem_report("decide", row_id(line), line, old_line, old_id, "Done / declined ledger", dry), 0
    if sub == "set":
        sec = (opt(rest, "--section") or "").lower()
        src = opt(rest, "--src")
        if not sec:
            return "memory set: set needs --section S (or --after ID)", 3
        if not pos:
            return 'usage: memory set --section S "CLAIM" --src SRC [--date DD.MM]', 2
        if "--raw" in rest:
            line = "- " + pos[0].removeprefix("- ")
        elif not src:
            return "memory set: a fact names its source: --src 'owner' / 'live' / '<agent>, DD.MM' (or --raw)", 3
        else:
            line = f"- {pos[0].strip()} — {src.strip()}" + ("" if DM_IN.search(src) else f", {date}")
        found = {}

        def ins(lines):
            h = next((i for i, x in enumerate(lines) if x.startswith("## ") and sec in x.lower()), None)
            if h is None:
                return None
            found["sec"] = lines[h][3:].strip()
            j = next((i for i in range(h + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
            while j > h + 1 and not lines[j - 1].strip():
                j -= 1
            return lines[:j] + [line] + lines[j:]

        if not edit(files[0], ins):
            return f"memory set: no section {sec!r} (memory show lists the rows per section)", 6
        return mem_report("set", row_id(line), line, section=found.get("sec", ""), dry=dry), 0
    rid = pos[0] if pos else ""
    hit = next(((s, t) for i, s, t in facts_rows() if i == rid), None)
    if hit is None:
        h = mem_history().get(rid)
        if h:
            return f"memory supersede: {rid} is {h['status']} (superseded by {h.get('by') or '-'})", 3
        return f"memory supersede: no row {rid} (memory show --grep WORD finds the id)", 6
    section, old = hit
    remove = "--remove" in rest
    text = None if remove else (pos[1] if len(pos) > 1 else None)
    if text is None and not remove:
        return "memory supersede: supersede needs the new text (or --remove)", 3
    if text is not None:
        text = text.strip().removeprefix("- ")
        src = opt(rest, "--src")
        if src and "--raw" not in rest:
            text = f"{text} — {src.strip()}" + ("" if DM_IN.search(src) else f", {date}")
    new_line = None if text is None else "- " + text

    def rep(lines):
        out = []
        for x in lines:
            if x == "- " + old:
                if new_line:
                    out.append(new_line)
            elif new_line and x == short_ledger("- " + old) and x != "- " + old:
                out.append(short_ledger(new_line))
            else:
                out.append(x)
        return out

    for f in files:
        edit(f, rep)
    if not dry:
        mem_history(
            lambda h: h.__setitem__(
                rid,
                {
                    "line": "- " + old,
                    "status": "removed" if remove else "superseded",
                    "by": new_line and row_id(new_line),
                },
            )
        )
    if remove:
        return mem_report("remove", rid, "- " + old, "- " + old, section=section, dry=dry, removed=True), 0
    return mem_report("supersede", row_id(new_line), new_line, "- " + old, rid, section, dry), 0


def memory_show(args):
    """memory show [ID…] [--grep TEXT]: the real CLI's row lines from the sandbox's FACTS.md; an old id shows its
    history (superseded / removed) and the row that replaced it."""
    pat = (opt(args, "--grep") or "").lower()
    ids = [a for a in positional(args[1:]) if re.fullmatch(r"r[0-9a-f]{10}", a)]
    if re.fullmatch(r"r[0-9a-f]{10}", pat):
        ids.append(pat)
    rows = [
        f"{rid}  f  {sec:<16}  -           live   {t}"
        for rid, sec, t in facts_rows()
        if (pat and pat in t.lower()) or rid in ids
    ]
    hist = mem_history()
    for rid in ids:
        h = hist.get(rid)
        if h and not any(r.startswith(rid) for r in rows):
            rows.append(f"{rid}  f  {'':<16}  -           live   [{h['status']}] {h['line'][2:]}")
            nxt = next(
                (f"{i}  f  {s:<16}  -           live   {t}" for i, s, t in facts_rows() if i == h.get("by")), None
            )
            if nxt:
                rows.append(nxt)
    if not rows:
        return "memory show: no active rows match (--status any adds superseded and removed ones)", 0
    return "\n".join(rows[:12]), 0


def items_cmd(name, sub, args):
    key = (
        name
        if name != "lists"
        else "lists:" + (args[1] if len(args) > 1 and sub in ("add", "list", "done", "rm") else "todo")
    )
    if sub == "add":
        text = " ".join(a for a in args[1:] if not a.startswith("--"))
        rid = f"{'r' if name == 'remind' else 'i'}{abs(hash(LINE)) % 9000 + 1000}"
        when = fmt_when(args[1:]) if name == "remind" else ""
        state(lambda st: st.setdefault(key, []).append(f"{rid} · {when + ' · ' if when else ''}{text}"))
        return (f"OK reminder {rid} set for {when}" if name == "remind" else f"added: {text} ({rid})"), 0
    if sub in ("list", "ls", "show", "card", ""):
        got = state().get(key, [])
        return ("\n".join(got) if got else ("No reminders." if name == "remind" else "The list is empty.")), 0
    if sub in ("rm", "del", "delete", "cancel", "remove", "done"):
        ids = [a for a in args[1:] if not a.startswith("--")]
        state(lambda st: st.__setitem__(key, [x for x in st.get(key, []) if not any(x.startswith(i) for i in ids)]))
        return f"removed {' '.join(ids)}".strip(), 0
    return "OK", 0


def default(name, args):
    """Generic answers: (stdout, exit). Real data never appears here; cases add their own via case.json."""
    sub = args[0] if args else ""
    now = clock()
    if "--help" in args or "-h" in args or sub == "help":
        try:
            return open(os.path.join(EV, "help", name + ".txt")).read().rstrip(), 0
        except OSError:
            return f"usage: {name} <command> [options]", 0
    if name == "tripclock":
        return now.strftime("%Y-%m-%d %H:%M %a") + f" ({CASE.get('tz_name', 'home time')})", 0
    if name == "date":
        fmt = next((a[1:] for a in args if a.startswith("+")), None)
        if fmt is not None:
            return now.strftime(fmt), 0
        if not args:
            return now.strftime("%a %b %d %H:%M:%S %Z %Y").replace("  ", " "), 0
        os.execv("/bin/date", ["/bin/date"] + args)
    if name == "driving":
        return ("driving" if CASE.get("driving") else "not driving"), (0 if CASE.get("driving") else 1)
    if name == "sleepin":
        return ("sleep-in active" if CASE.get("sleepin") else "no sleep-in"), (0 if CASE.get("sleepin") else 1)
    if name == "secretscan":
        return (stdin_cache if "--redact" in args else ""), 0
    if name == "recall":
        return "No matches.", 0
    if name == "remind":
        return remind_cmd(sub, args)
    if name in ("shop", "lists"):
        return items_cmd(name, sub, args)
    if name == "vault" and sub == "job":
        jid = next((a for a in args[1:] if re.fullmatch(r"[0-9a-f]{16}", a)), "")
        j = state().get("_jobs", {}).get(jid)
        if not j:
            return json.dumps(
                {"id": jid, "state": "expired", "error": {"error": "unknown job", "code": "result_dropped"}}
            ), 1
        if j["exit"] == 10:
            res = {"text_release": True, "request_id": "q1"}
        elif j["exit"]:
            return json.dumps({"id": jid, "state": "failed", "error": {"error": j["out"][:200]}}), 1
        else:
            res = {"answer": j["out"]}
        return json.dumps({"id": jid, "state": "done", "elapsed_s": 4, "result": res}, ensure_ascii=False), 0
    if name in ("vault", "tgread"):
        if "--text" in args or "--content" in args or sub == "raw":
            return "Approval requested from the owner in the vault bot (request q1). Exit 10 until he decides.", 10
        if "ask" in args:
            return "The archive has nothing clear on that.", 0
        if args[:2] == ["ha", "now"]:
            return "zone: home · driving: no · activity: still", 0
        return "OK (no data)", 0
    if name == "whereabouts":
        return "zone: home (source: vault)", 0
    if name == "ha":
        if "--json" in args and sub in ("state", "list", "find", "history", "logbook", "trace", "areas"):
            return "[]", 0
        if sub in ("state", "list", "areas", "history", "logbook", "trace", "todo"):
            return "(no matching entities)", 0
        return "OK (state verified)", 0
    if name == "unraid-health":
        return "Server OK: array started, parity valid, no disk errors, temperatures normal.", 0
    if name == "unraid-capacity":
        return "Array 86% used, 18 TB free; at the current rate full in about 7 months.", 0
    if name == "unraid-do":
        return "OK: done (dispatcher)", 0
    if name in ("tg", "tg-status", "tg-react", "tg-soft", "tg-buttons", "tg-keyboards", "tg-voice", "push"):
        return "sent", 0
    if name == "voice-reply":
        return "spoken", 0
    if name == "voice-relay":
        return ("answered; call ended" if sub == "call" else "relay up"), 0
    if name == "voice-say":
        return "voice note sent", 0
    if name == "phone":
        if "--json" in args:
            if sub in ("push", "link", "open", "chat"):
                return json.dumps({"status": "acked", "id": "p" + hex_id(LINE, 6), "tag": opt(args, "--tag")}), 0
            if sub == "pushes":
                return json.dumps({"items": []}), 0
            return json.dumps({"ok": True}), 0
        return "delivered", 0
    if name == "kb":
        words = {w for w in re.findall(r"[a-zà-ÿ0-9]{4,}", " ".join(args[1:]).lower()) if w not in STOP}
        hits = sorted(((sum(w in t.lower() for w in words), t) for _, _, t in facts_rows()), reverse=True)
        hits = [t for n, t in hits if n >= 2][:3]
        return (
            "\n".join(f"- {t[:300]} [FACTS.md]" for t in hits)
            if hits
            else "No settled fact or note matches that question."
        ), 0
    if name == "memory":
        if sub in ("set", "decide", "supersede"):
            return memory_write(sub, args[1:])
        if sub == "show":
            return memory_show(args)
        if sub == "status":
            n = len(facts_rows())
            return f"memory: mode rows · {n} rows active · file matches rows: yes · last import just now", 0
        if sub == "import":
            return "memory import: 0 kept, 0 moved, 0 edited, 0 added, 0 restored, 0 removed, 0 layout lines", 0
        return "OK", 0
    if name == "calcache" and sub == "show":
        days = int(opt(args, "--days") or 7)
        t0 = clock().date()
        evs = []
        for e in CASE.get("calendar") or []:
            st = str(e.get("start", ""))
            try:
                d = dt.date.fromisoformat(st[:10])
            except ValueError:
                continue
            if t0 <= d < t0 + dt.timedelta(days=days):
                evs.append(
                    {
                        "id": e.get("id"),
                        "start": st,
                        "end": e.get("end"),
                        "all_day": len(st) == 10,
                        "title": e.get("summary", ""),
                        "town": e.get("location"),
                        "free": e.get("transparency") == "transparent",
                    }
                )
        if "--json" in args:
            return json.dumps(evs, ensure_ascii=False), 0
        return "\n".join(f"{e['start']}  {e['title']}" for e in evs) or "No events.", 0
    if name == "approvals":
        n = hex_id(LINE, 8)
        if sub == "new":
            if "--json" in args:
                return json.dumps(
                    {
                        "nonce": n,
                        "expires": (clock() + dt.timedelta(minutes=15)).isoformat(timespec="minutes"),
                        "summary": opt(args, "--summary"),
                        "ref": opt(args, "--ref"),
                        "buttons": [[{"text": "✅ Yes", "data": f"ap:{n}:y"}, {"text": "❌ No", "data": f"ap:{n}:n"}]],
                    },
                    ensure_ascii=False,
                ), 0
            return n, 0
        if sub in ("status", "wait"):
            return "pending", 6 if sub == "wait" else 0
        if sub == "consume":
            return "approvals: not decided (no approval from him for this)", 5
        return "OK", 0
    if name == "context":
        return json.dumps({"place": "home", "driving": False, "quiet": False, "needs_you": 0}), 0
    if name == "profile-get":
        return "", 0
    if name in ("crontab", "sudo", "docker", "ssh", "scp", "restart", "brain-commit", "codebase-sync", "gh"):
        return f"{name}: not available in the eval sandbox", 1
    return f"OK ({name} {sub}: no data in the eval sandbox)".replace(" :", ":"), 0


stdin_cache = stdin_head()


def called_before(pat):
    """True when a call recorded earlier in this sandbox matches pat (its full line, e.g. `webagent resume w12`)."""
    try:
        with open(os.path.join(EV, "stubs.jsonl")) as f:
            return any(re.search(pat, json.loads(ln).get("line", "")) for ln in f if ln.strip())
    except (OSError, ValueError):
        return False


def answer(name, args):
    """(stdout, exit): the case's first matching rule, else the defaults. A rule with "after" (a regex on an earlier
    call's full line) applies only once such a call ran, so a status can change after the step that changes it."""
    for rule in CASE.get("stubs", []):
        if rule.get("cmd") not in (name, "*"):
            continue
        pat = rule.get("match")
        if pat and not re.search(pat, " ".join(args)):
            continue
        if rule.get("after") and not called_before(rule["after"]):
            continue
        return str(rule.get("out", "")).rstrip("\n"), int(rule.get("exit", 0))
    return default(name, args)


def main():
    if NAME == "vault" and ARGS[:1] == ["--async"] and len(ARGS) > 1:
        inner, icode = answer(NAME, ARGS[1:])
        out, code = hex_id(LINE + str(time.time()), 16), 0
        state(lambda st: st.setdefault("_jobs", {}).__setitem__(out, {"out": inner, "exit": icode}))
    else:
        out, code = answer(NAME, ARGS)
    record(code, out, stdin_cache)
    if out:
        sys.stdout.write(out + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
