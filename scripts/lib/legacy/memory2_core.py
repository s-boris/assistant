"""memory2_core: the fact store's schema, search and the per-turn recall hook (stdlib only, light imports: the
UserPromptSubmit hook imports this on every owner turn and must answer within ~300 ms).

Store: STATE/mem.db (SQLite WAL, 0600; STATE = $MEMORY2_STATE or ~/.local/state/memory2, dir 0700)
  facts   one row per fact: id, kind (fact | ledger | memory | decision | correction | preference), subject, text,
          source (citation: FACTS.md:LINE | memory/NAME.md | tg#ID / session:Lline), date (YYYY-MM-DD), confidence,
          supersedes, status (active | gone | superseded | promoted | rejected), origin (facts | ledger | memory |
          auto), path, line, verb + thing (ledger lines), hash (normalized text), vec (float32 EmbeddingGemma vector).
  ffts    FTS5 over subject + text (rowid = facts.rowid; porter, diacritics folded).
  review  the review queue: auto-accepted, pending and conflicting candidates (status open | accepted | rejected).
  cands   candidate states (the staging log itself is STATE/candidates.jsonl).
  meta    transcript offsets, last runs.
Search = BM25 over ffts (any-word, prefix) fused with cosine over the stored vectors when a query vector is given
(reciprocal rank); a relevance floor keeps unrelated facts out. The query vector comes from the shared
EmbeddingGemma server ($MEMORY2_EMBED_URL, default http://127.0.0.1:8786, `memory2 embedd`) when it answers in
time; otherwise text only.
"""
import array
import hashlib
import json
import os
import re
import socket
import sqlite3
import sys
import time

HOME = os.path.realpath(os.environ.get("MEMORY2_HOME") or os.path.expanduser("~"))
STATE = os.environ.get("MEMORY2_STATE") or os.path.join(HOME, ".local/state/memory2")
DB = os.path.join(STATE, "mem.db")
CONF = os.environ.get("MEMORY2_CONF") or os.path.join(HOME, ".config/memory2")
OFF_FILE = os.path.join(CONF, "hook-off")
HOOK_LOG = os.path.join(STATE, "hook.log")
EMBED_URL = os.environ.get("MEMORY2_EMBED_URL") or "http://127.0.0.1:8786"
WORKSPACE = os.path.join(HOME, "workspace")
SCHEMA = "1"

STOP = set("""a an the and or but if then else of to in on at by for with from into onto about over under as is are
was were be been being am do does did done doing have has had having i me my mine you your yours he him his she her we
us our they them their it its this that these those there here what when where which who whom whose why how can could
should would will shall may might must not no yes ok okay please pls just also too very really so than such only own
same any all some more most other again once out up down off now still even get got let lets make made want need
know think see say said tell told ask asked like one two its it's i'm i've i'll don't doesn't didn't can't won't
isn't aren't wasn't there's what's let's that's ever whether which much many few each every both either neither
der die das ein eine einen einem einer und oder aber wenn dann von zu im in am an auf mit für fur aus ist sind war
waren sein bin bist hat habe hast haben ich du er sie es wir ihr mein meine dein deine sich nicht kein keine ja nein
auch noch schon nur so wie was wer wo wann warum welche welcher welches dass ob bitte mal doch denn""".split())
WORD = re.compile(r"[0-9]+(?:[.,][0-9]+)?|[^\W\d_][\w'’-]*", re.U)


def terms_of(text, cap=14):
    """Content words of a query: lowercased, stopwords out, >= 3 letters (numbers always kept), first `cap`."""
    out, seen = [], set()
    for w in WORD.findall(text or ""):
        w = w.lower().strip("'’-")
        if not w or w in STOP or (len(w) < 3 and not w[0].isdigit()):
            continue
        if w not in seen:
            seen.add(w)
            out.append(w)
            if len(out) >= cap:
                break
    return out


def connect(ro=False, path=None):
    path = path or DB
    if ro:
        con = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=0.2)
        con.execute("PRAGMA query_only=1")
        return con
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    os.chmod(os.path.dirname(path), 0o700)
    con = sqlite3.connect(path, timeout=30)
    os.chmod(path, 0o600)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript("""
        CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS facts(rowid INTEGER PRIMARY KEY, id TEXT UNIQUE, kind TEXT, subject TEXT,
            text TEXT, source TEXT, date TEXT, confidence REAL, supersedes TEXT, status TEXT, origin TEXT,
            path TEXT, line INTEGER, verb TEXT, thing TEXT, hash TEXT, vec BLOB, created TEXT, updated TEXT);
        CREATE INDEX IF NOT EXISTS facts_status ON facts(status);
        CREATE INDEX IF NOT EXISTS facts_hash ON facts(hash);
        CREATE VIRTUAL TABLE IF NOT EXISTS ffts USING fts5(subject, text,
            tokenize = 'porter unicode61 remove_diacritics 2');
        CREATE TABLE IF NOT EXISTS review(id TEXT PRIMARY KEY, cand_id TEXT, kind TEXT, fact_id TEXT,
            other_id TEXT, reason TEXT, score REAL, status TEXT, created TEXT, resolved TEXT);
        CREATE TABLE IF NOT EXISTS cands(id TEXT PRIMARY KEY, status TEXT, fact_id TEXT, data TEXT,
            created TEXT);
    """)
    con.execute("INSERT OR IGNORE INTO meta VALUES('schema', ?)", (SCHEMA,))
    con.commit()
    return con


# ------------------------------------------------------------------ vectors

def pack(vec):
    return array.array("f", vec).tobytes()


def unpack(blob):
    a = array.array("f")
    a.frombytes(blob)
    return a


def dot(a, b):
    return sum(map(float.__mul__, a, b))


def _post(url, path, obj, timeout):
    """Minimal HTTP/1.1 POST over a plain socket (urllib costs ~20 ms of imports on every hook run)."""
    m = re.match(r"http://([^/:]+):(\d+)", url)
    if not m:
        raise OSError("bad embed url")
    body = json.dumps(obj).encode()
    req = ("POST %s HTTP/1.1\r\nHost: %s\r\nContent-Type: application/json\r\nContent-Length: %d\r\n"
           "Connection: close\r\n\r\n" % (path, m.group(1), len(body))).encode() + body
    s = socket.create_connection((m.group(1), int(m.group(2))), timeout=timeout)
    try:
        s.settimeout(timeout)
        s.sendall(req)
        chunks = []
        while True:
            c = s.recv(65536)
            if not c:
                break
            chunks.append(c)
    finally:
        s.close()
    raw = b"".join(chunks)
    head, _, data = raw.partition(b"\r\n\r\n")
    if b" 200 " not in head.split(b"\r\n", 1)[0]:
        raise OSError("embed server status")
    if b"chunked" in head.lower():
        out, rest = b"", data
        while rest:
            size_s, _, rest = rest.partition(b"\r\n")
            size = int(size_s.split(b";")[0] or b"0", 16)
            if not size:
                break
            out, rest = out + rest[:size], rest[size + 2:]
        data = out
    return json.loads(data)


def doc_text(subject, text):
    return ("title: %s | text: %s" % (subject or "none", text))[:1500]


def query_text(q):
    return "task: search result | query: " + " ".join(re.sub(r'["*]', " ", q).split())[:600]


def embed(texts, timeout=10.0, url=None):
    """texts -> unit vectors (lists of floats) from the embedding server; raises OSError/ValueError."""
    d = _post(url or EMBED_URL, "/v1/embeddings", {"input": texts}, timeout)
    rows = sorted(d.get("data") or [], key=lambda r: r.get("index", 0))
    if len(rows) != len(texts):
        raise ValueError("embedding count")
    out = []
    for r in rows:
        v = r.get("embedding") or []
        n = sum(x * x for x in v) ** 0.5 or 1.0
        out.append([x / n for x in v])
    return out


# ------------------------------------------------------------------ search

LEX_W, SEM_W, RRF_K = 1.0, float(os.environ.get("MEMORY2_SEM_W") or 1.0), 6
SEM_MIN = float(os.environ.get("MEMORY2_SEM_MIN") or 0.40)  # cosine floor for a semantic candidate (EmbeddingGemma query vs document prompt)
FLOOR_COS = 0.45      # a hit is kept if its cosine reaches this ...
FLOOR_COVER = 0.34    # ... or it covers this share of the query's content words (and >= 2 of them, or the only one)
FLOOR_REL = float(os.environ.get("MEMORY2_FLOOR_REL") or 0.8)  # ... and its fused score is at least this share of the best hit's
# Core mode (CLAUDE.md imports FACTS-core.md instead of FACTS.md, v2 H16): the pack carries the facts on file that the
# loaded core leaves out, so it takes more hits, a looser relative floor, wider clauses and a bigger budget (chars;
# ~2.1 chars per token on these files, so 5200 chars stay far below the 4k-token cap per turn).
CORE_FILE = "FACTS-core.md"
CORE_K = int(os.environ.get("MEMORY2_CORE_K") or 8)
CORE_FLOOR_REL = float(os.environ.get("MEMORY2_CORE_FLOOR_REL") or 0.4)
CORE_BUDGET = int(os.environ.get("MEMORY2_CORE_BUDGET") or 5200)
CORE_WIDTH = int(os.environ.get("MEMORY2_CORE_WIDTH") or 420)
IMPORT_LINE = re.compile(r"^@(\S+)\s*$", re.M)
KIND_TAG = {"fact": "fact", "ledger": "ledger", "memory": "memory"}


def _cover(terms, text):
    low = text.lower()
    hit = 0
    for t in terms:
        stem = t[:max(4, len(t) - 2)] if len(t) > 5 else t
        if stem in low:
            hit += 1
    return hit


def search(con, query, k=5, qvec=None, status=("active",), floor=True, kinds=None, floor_rel=None, skip=None):
    """[(score, row dict)] best facts for `query`, best first. row: id kind subject text source date origin
    confidence verb thing cos cover."""
    terms = terms_of(query)
    marks = ",".join("?" * len(status))
    cond = "f.status IN (%s)" % marks
    params = list(status)
    if kinds:
        cond += " AND f.kind IN (%s)" % ",".join("?" * len(kinds))
        params += list(kinds)
    lex = []
    if terms:
        match = " OR ".join('"%s"*' % t.replace('"', "") for t in terms)
        try:
            lex = [r[0] for r in con.execute(
                "SELECT f.rowid FROM ffts JOIN facts f ON f.rowid = ffts.rowid WHERE ffts MATCH ? AND %s "
                "ORDER BY bm25(ffts, 1.5, 1.0) LIMIT 40" % cond, [match] + params)]
        except sqlite3.OperationalError:
            lex = []
    cos = {}
    if qvec is not None:
        q = array.array("f", qvec)
        for rid, blob in con.execute("SELECT f.rowid, f.vec FROM facts f WHERE f.vec IS NOT NULL AND " + cond,
                                     params):
            v = unpack(blob)
            if len(v) == len(q):
                c = dot(q, v)
                if c >= SEM_MIN:
                    cos[rid] = c
    sem = sorted(cos, key=lambda r: -cos[r])[:40]
    score = {}
    for rank, rid in enumerate(lex):
        score[rid] = score.get(rid, 0.0) + LEX_W / (RRF_K + rank + 1)
    for rank, rid in enumerate(sem):
        score[rid] = score.get(rid, 0.0) + SEM_W / (RRF_K + rank + 1)
    if not score:
        return []
    rows = {}
    q = "SELECT rowid, id, kind, subject, text, source, date, origin, confidence, verb, thing FROM facts WHERE rowid IN (%s)"
    ids = list(score)
    for r in con.execute(q % ",".join("?" * len(ids)), ids):
        rows[r[0]] = dict(zip(("rowid", "id", "kind", "subject", "text", "source", "date", "origin", "confidence",
                               "verb", "thing"), r))
    out = []
    for rid, s in score.items():
        r = rows.get(rid)
        if not r:
            continue
        r["cos"] = cos.get(rid, 0.0)
        r["cover"] = _cover(terms, (r["subject"] or "") + " " + (r["text"] or "")) if terms else 0
        need = 1 if len(terms) <= 1 else 2
        frac = r["cover"] / len(terms) if terms else 0.0
        if floor and not (r["cos"] >= FLOOR_COS or (r["cover"] >= need and frac >= FLOOR_COVER)):
            continue
        s += 0.02 * frac + (0.05 * max(0.0, r["cos"] - FLOOR_COS))
        out.append((s, r))
    if skip:
        out = [x for x in out if x[1]["text"] not in skip]   # already in context: the best other hit sets the floor
    out.sort(key=lambda x: -x[0])
    if floor and out:
        top = out[0][0]
        out = [x for x in out if x[0] >= (FLOOR_REL if floor_rel is None else floor_rel) * top]
    return out[:k]


def focus(text, terms, width=220):
    """The part of a long fact line that matches the query best (clauses split on ; or ' — ')."""
    text = " ".join((text or "").split())
    if len(text) <= width:
        return text
    parts = [p.strip() for p in re.split(r";\s+|\s+—\s+|\.\s+", text) if p.strip()] or [text]
    cov = [_cover(terms, p) if terms else 0 for p in parts]
    order = sorted(range(len(parts)), key=lambda i: (-cov[i], i))
    cand = [i for i in order if cov[i] > 0] or order[:1]
    chosen, used = [], 0
    if len(parts[0]) <= width // 3:           # the leading clause names the thing
        chosen, used = [0], len(parts[0])
    for i in cand:
        if i not in chosen and used + len(parts[i]) + 3 <= width:
            chosen.append(i)
            used += len(parts[i]) + 3
    if cand[0] not in chosen:                 # the best clause did not fit: cut it
        room = max(40, width - used - 3)
        clause = parts[cand[0]] if len(parts[cand[0]]) <= room else parts[cand[0]][:room - 1].rstrip() + "…"
        return " … ".join([parts[i] for i in sorted(chosen)] + [clause])
    return " … ".join(parts[i] for i in sorted(chosen))


def label(r):
    if r["origin"] == "ledger":
        d = r["date"] or ""
        return "ledger %s.%s %s" % (d[8:10], d[5:7], r["verb"] or "") if len(d) == 10 else "ledger"
    if r["origin"] == "memory":
        return "memory"
    if r["origin"] == "auto":
        return "auto %s, unreviewed" % (r["kind"] or "fact")
    return "fact"


def block(hits, terms, budget=950, width=220, core=False):
    lines = ["memory2: settled facts that may bear on this message (FACTS.md wins; check before asking or "
             "pitching):" if not core else
             "memory2: facts on file that may bear on this message (not in the loaded FACTS core; FACTS.md wins; "
             "check before asking or pitching):"]
    used = len(lines[0])
    for _, r in hits:
        t = focus(r["text"], terms, width=width)
        if r["origin"] == "ledger" and r["thing"] and r["thing"].lower() not in t.lower():
            t = r["thing"] + ": " + t
        line = "- [%s] %s (%s)" % (label(r), t, r["source"] or "?")
        if used + len(line) > budget:
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines) if len(lines) > 1 else ""


# ------------------------------------------------------------------ the UserPromptSubmit hook

CHAN = re.compile(r'<channel((?:\s+[\w-]+="[^"]*")*)\s*>(.*?)(?:</channel>|$)', re.S)
ATTR = re.compile(r'([\w-]+)="([^"]*)"')
SYSREM = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
PREFIX = re.compile(r"^\s*(?:🎙️|🗣️|📱)\s*\([^)]*\)\s*")
SKIP = ("[heartbeat]", "<task-notification", "[self-scheduled", "<local-command", "The heartbeat plugin",
        "Another Claude session", "[board-lint]", "Caveat:", "<command-name>")


def prompt_text(prompt):
    """The owner's words in a prompt ('' for heartbeats, notifications, reactions and button taps)."""
    s = SYSREM.sub("", prompt or "").strip()
    if not s or s.startswith(SKIP):
        return ""
    if s.startswith("<channel"):
        parts = []
        for m in CHAN.finditer(s):
            a = dict(ATTR.findall(m.group(1)))
            if a.get("event_type"):
                continue        # button, reaction, poll answer: an answer to my question, not a new topic
            parts.append(PREFIX.sub("", m.group(2).strip()))
        s = " ".join(parts)
    return s[:600]


def context_mode():
    """core when the workspace CLAUDE.md imports FACTS-core.md and not FACTS.md, else full (MEMORY2_HOOK_MODE wins)."""
    m = (os.environ.get("MEMORY2_HOOK_MODE") or "").lower()
    if m in ("core", "full"):
        return m
    try:
        with open(os.path.join(WORKSPACE, "CLAUDE.md"), encoding="utf-8", errors="replace") as f:
            names = {os.path.basename(x) for x in IMPORT_LINE.findall(f.read())}
    except OSError:
        return "full"
    return "core" if CORE_FILE in names and "FACTS.md" not in names else "full"


def loaded_lines():
    """The bullet texts the session already holds (the FACTS core it imports): the pack skips those."""
    try:
        with open(os.path.join(WORKSPACE, CORE_FILE), encoding="utf-8", errors="replace") as f:
            return {ln[2:].rstrip("\n") for ln in f if ln.startswith("- ")}
    except OSError:
        return set()


def main_session(data):
    """cwd = the workspace, or a transcript in the workspace's project folder (the same session after a `cd`)."""
    want = os.path.realpath(os.environ.get("MEMORY2_HOOK_CWD") or WORKSPACE)
    if os.path.realpath(data.get("cwd") or "") == want:
        return True
    tp = data.get("transcript_path")
    if not isinstance(tp, str) or not tp:
        return False
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")
    proj = os.path.join(base, "projects", re.sub(r"[^A-Za-z0-9]", "-", want))
    return os.path.realpath(os.path.dirname(tp)) == os.path.realpath(proj)


def cached_qvec(q):
    """A precomputed query vector (MEMORY2_QVEC_CACHE: JSON {sha256(query text): vector}; the eval sandbox has no
    loopback, so the harness embeds its prompts beforehand). None when unset or missing."""
    path = os.environ.get("MEMORY2_QVEC_CACHE")
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            v = json.load(f).get(hashlib.sha256(q.encode("utf-8")).hexdigest())
        return [float(x) for x in v] if v else None
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def hook_log(ms, n, sem, why="", mode="", chars=0):
    if os.environ.get("MEMORY2_HOOK_NOLOG"):
        return
    try:
        os.makedirs(STATE, mode=0o700, exist_ok=True)
        if os.path.exists(HOOK_LOG) and os.path.getsize(HOOK_LOG) > 512 * 1024:
            with open(HOOK_LOG, encoding="utf-8", errors="replace") as f:
                keep = f.readlines()[-2000:]
            with open(HOOK_LOG, "w") as f:
                f.writelines(keep)
        fd = os.open(HOOK_LOG, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, ("%d %d %d %d %s %s %d\n" % (time.time(), ms, n, 1 if sem else 0, why or "ok", mode or "-",
                                                    chars)).encode())
        finally:
            os.close(fd)
    except OSError:
        pass
    if os.environ.get("USAGE", "") == "off" or os.environ.get("MEMORY2_STATE"):
        return
    try:   # one usage-store spool line (format of ~/bin/lib/usage.py record(); numbers and tokens only)
        spool = os.path.join(os.environ.get("USAGE_STATE") or os.path.join(HOME, ".local/state/product"), "spool")
        if os.path.isdir(spool):
            rec = {"ts": round(time.time(), 3), "f": "hook.memory2", "k": "run", "src": "hook", "ch": "session",
                   "ms": int(ms), "o": "ok" if why in ("ok", "skip") else "error",
                   "at": {"hits": int(n), "sem": 1 if sem else 0}}
            fd = os.open(os.path.join(spool, "hook-%s.jsonl" % time.strftime("%Y%m%d")),
                         os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, (json.dumps(rec, separators=(",", ":")) + "\n").encode())
            finally:
                os.close(fd)
    except OSError:
        pass


def hook_main(stdin=None, stdout=None, budget_s=0.30):
    """Reads the hook JSON, prints {"hookSpecificOutput": {...additionalContext}} or nothing. Never raises."""
    t0 = time.time()
    out = stdout or sys.stdout
    why, n, sem, mode, chars = "ok", 0, False, "", 0
    try:
        if os.environ.get("MEMORY2_HOOK", "").lower() in ("off", "0", "false", "no") or os.path.exists(OFF_FILE):
            why = "skip"
            return 0
        data = json.loads((stdin or sys.stdin).read() or "{}")
        if not main_session(data):
            why = "skip"        # the main session only (claude -p workers, tests and other folders get nothing)
            return 0
        text = prompt_text(data.get("prompt") or "")
        terms = terms_of(text)
        if len(terms) < 2:
            why = "skip"
            return 0
        if not os.path.exists(DB):
            why = "nodb"
            return 0
        mode = context_mode()
        core = mode == "core"
        con = connect(ro=True)
        qvec = cached_qvec(query_text(text))
        sem = qvec is not None
        left = budget_s - (time.time() - t0) - 0.05
        if qvec is None and left > 0.06 and os.environ.get("MEMORY2_HOOK_SEM", "on") != "off":
            try:
                qvec = embed([query_text(text)], timeout=min(left, 0.25))[0]
                sem = True
            except (OSError, ValueError, KeyError):
                qvec = None
        k = int(os.environ.get("MEMORY2_HOOK_K") or (CORE_K if core else 4))
        if core:
            hits = search(con, text, k=k, qvec=qvec, floor_rel=CORE_FLOOR_REL, skip=loaded_lines())
        else:
            hits = search(con, text, k=k, qvec=qvec)
        con.close()
        n = len(hits)
        ctx = block(hits, terms, budget=CORE_BUDGET if core else 950, width=CORE_WIDTH if core else 220, core=core)
        chars = len(ctx)
        if ctx:
            out.write(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                                          "additionalContext": ctx}}, ensure_ascii=False))
            out.flush()
        return 0
    except Exception as e:  # fail-open: never block or break a turn
        why = "err:" + type(e).__name__
        return 0
    finally:
        ms = int((time.time() - t0) * 1000)
        if why != "skip" or os.environ.get("MEMORY2_HOOK_LOG_SKIPS"):
            hook_log(ms, n, sem, why, mode, chars)


if __name__ == "__main__":
    sys.exit(hook_main())
