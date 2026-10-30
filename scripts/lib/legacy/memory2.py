"""memory2: auto-learned facts with a review queue on top of FACTS.md + auto-memory (token-free; local models only).

  sync      mirror FACTS.md (facts + done/declined ledger) and the auto-memory files into the fact store
            (hash-keyed; lines that vanish become status gone, never deleted); embeds new rows when the
            embedding server is up (`embedd up` starts it).
  extract   read new owner turns from the main-session transcripts (recall's parser; owner text, button taps and
            reactions only, never tool results), propose candidates by cue (decision / correction / preference /
            fact) into STATE/candidates.jsonl, normalize them with a small LOCAL model (Qwen3.5-4B, llama-server
            started per batch, niced) when --llm, check each against the store (duplicate / contradiction:
            embedding similarity + ledger polarity + numbers + negation, local-model judgement with --llm) and
            auto-accept only high-confidence owner decisions/corrections (into the store, origin auto; FACTS.md
            is never edited automatically). Everything else lands in the review queue.
  review / accept / reject / promote / digest   work the queue; `promote` appends one line to FACTS.md (a section
            fact or a ledger line) after checking that every existing line survives.
Privacy: owner messages only; sensitive topics (health, money, intimate, identifiers) and pasted/forwarded text
are dropped before staging; everything passes secretscan.redact(); store 0600; nothing leaves the box.
Env (tests): MEMORY2_HOME / _STATE / _CONF / _FACTS / _MEMDIR / _PROJECT / _EMBED_URL / _LLM_MODEL / _NOW,
MEMORY2_LLM=off, MEMORY2_EMBED=off.
"""
import fcntl
import glob
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import re
import socket
import subprocess
import sys
import time
from datetime import date as Date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import memory2_core as core  # noqa: E402
from memory2_core import HOME, STATE, CONF  # noqa: E402

FACTS = os.environ.get("MEMORY2_FACTS") or os.path.join(HOME, "workspace", "FACTS.md")
MEMDIR = os.environ.get("MEMORY2_MEMDIR") or os.path.join(HOME, ".claude/projects/-home-node-workspace/memory")
PROJECT = os.environ.get("MEMORY2_PROJECT") or os.path.join(HOME, ".claude/projects/-home-node-workspace")
CANDS = os.path.join(STATE, "candidates.jsonl")
INBOX = os.path.join(HOME, "inbox")
RECALL_BIN = os.environ.get("MEMORY2_RECALL") or os.path.join(os.path.expanduser("~"), "bin", "recall")
LLAMA = os.path.expanduser(os.environ.get("MEMORY2_LLAMA") or "~/opt/llama.cpp/build/bin/llama-server")
EMBED_MODEL = os.path.expanduser(os.environ.get("MEMORY2_EMBED_MODEL") or "~/opt/llm-models/embeddinggemma-300M-Q8_0.gguf")
LLM_MODEL = os.path.expanduser(os.environ.get("MEMORY2_LLM_MODEL") or "~/opt/llm-models/Qwen3.5-4B-MTP-Q4_K_M.gguf")
EMBED_OFF = os.environ.get("MEMORY2_EMBED", "").lower() in ("off", "0", "no")
LLM_OFF = os.environ.get("MEMORY2_LLM", "").lower() in ("off", "0", "no")
LEDGER_VERBS = ("declined", "skipped", "scratched", "removed", "off", "done", "decided", "kept")
NEG_VERBS = ("declined", "skipped", "scratched", "removed", "off")
AUTO_KINDS = ("decision", "correction")
AUTO_MIN = 0.80
REVIEW_MIN = 0.55
LLM_BATCH = int(os.environ.get("MEMORY2_LLM_BATCH") or 12)


def now():
    return float(os.environ.get("MEMORY2_NOW") or time.time())


def today():
    return datetime.fromtimestamp(now()).date()


def iso_now():
    return datetime.fromtimestamp(now()).strftime("%Y-%m-%d %H:%M")


def sha(s, n=10):
    return hashlib.sha1(s.encode("utf-8", "replace")).hexdigest()[:n]


def norm(s):
    return " ".join(re.sub(r"[^\w%./:-]+", " ", (s or "").lower()).split())


def meta(con, k, default=None):
    r = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r[0] if r else default


def set_meta(con, k, v):
    con.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, str(v)))


_RECALL = None


def recall_mod():
    """~/bin/recall as a module (its transcript parser, clean() and secretscan redact()): extend, don't fork."""
    global _RECALL
    if _RECALL is None:
        loader = importlib.machinery.SourceFileLoader("recall_mod", RECALL_BIN)
        spec = importlib.util.spec_from_loader("recall_mod", loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        _RECALL = mod
    return _RECALL


def redact(text):
    m = recall_mod()
    if m.secretscan is None:
        raise RuntimeError("secretscan unavailable: refusing to store text (fail-closed)")
    return m.redact(text)


# ------------------------------------------------------------------ dates

DM = re.compile(r"(?<![\d.])(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?(?!\d)")


def dm_date(text):
    """Last DD.MM[.YYYY] in text -> YYYY-MM-DD (year: this one, unless that is > 60 days ahead)."""
    best = None
    for m in DM.finditer(text or ""):
        d, mo, y = int(m.group(1)), int(m.group(2)), m.group(3)
        if not (1 <= d <= 31 and 1 <= mo <= 12):
            continue
        t = today()
        yy = int(y) if y else t.year
        try:
            dd = Date(yy, mo, d)
        except ValueError:
            continue
        if not y and dd > t + timedelta(days=60):
            dd = Date(yy - 1, mo, d)
        best = dd.isoformat()
    return best


# ------------------------------------------------------------------ sync (FACTS.md + memory -> store)

LEDGER_RE = re.compile(r"^- (\d{1,2})\.(\d{1,2})(?:\.(\d{4}))? (%s): (.+)$" % "|".join(LEDGER_VERBS))


def parse_facts(path=None):
    path = path or FACTS
    items, section = [], ""
    rel = os.path.relpath(path, os.path.join(HOME, "workspace"))
    with open(path, encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f, 1):
            line = line.rstrip("\n")
            if line.startswith("## "):
                section = line[3:].strip()
                continue
            if not line.startswith("- "):
                continue
            body = line[2:].strip()
            if section.lower().startswith("done / declined"):
                m = LEDGER_RE.match(line)
                if not m:
                    continue
                rest = m.group(5)
                thing = re.split(r"\s+\(|\s+—\s+", rest, 1)[0].strip()
                items.append(dict(origin="ledger", kind="ledger", subject=thing, text=body, verb=m.group(4),
                                  thing=thing, date=dm_date("%s.%s.%s" % (m.group(1), m.group(2), m.group(3) or ""))
                                  if m.group(3) else dm_date("%s.%s." % (m.group(1), m.group(2))),
                                  path=path, line=i, source="%s:%d" % (rel, i)))
            else:
                tail = body.rsplit(" — ", 1)[1] if " — " in body else ""
                items.append(dict(origin="facts", kind="fact", subject=section, text=body, verb=None, thing=None,
                                  date=dm_date(tail), path=path, line=i,
                                  source="%s:%d" % (rel, i)))
    return items


FRONT = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.S)


def parse_memory(d=None):
    d = d or MEMDIR
    out = []
    for p in sorted(glob.glob(os.path.join(d, "*.md"))):
        if os.path.basename(p) == "MEMORY.md":
            continue
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                raw = f.read(20000)
        except OSError:
            continue
        m = FRONT.match(raw)
        fm, body = (m.group(1), m.group(2)) if m else ("", raw)
        name = re.search(r"^name:\s*(.+)$", fm, re.M)
        desc = re.search(r"^description:\s*(.+)$", fm, re.M)
        mod = re.search(r"modified:\s*(\d{4}-\d\d-\d\d)", fm)
        name = (name.group(1).strip().strip("\"'") if name else os.path.basename(p)[:-3])
        desc = desc.group(1).strip().strip("\"'") if desc else ""
        para = next((x.strip() for x in re.split(r"\n\s*\n", body) if x.strip() and not x.lstrip().startswith("#")), "")
        text = (desc + (" — " if desc and para else "") + " ".join(para.split()))[:700]
        if not text:
            continue
        line = (raw[:m.start(2)].count("\n") + 1) if m else 1
        date = mod.group(1) if mod else datetime.fromtimestamp(os.path.getmtime(p)).strftime("%Y-%m-%d")
        out.append(dict(origin="memory", kind="memory", subject=name, text=text, verb=None, thing=None, date=date,
                        path=p, line=line, source="memory/%s:%d" % (os.path.basename(p), line)))
    return out


def upsert(con, it, status="active", confidence=1.0, extra_id=None):
    fid = extra_id or ("f" + sha(it["origin"] + "|" + norm(it["text"])))
    h = sha(norm(it["text"]), 16)
    row = con.execute("SELECT rowid FROM facts WHERE id=?", (fid,)).fetchone()
    ts = iso_now()
    if row:
        con.execute("UPDATE facts SET subject=?, source=?, date=?, path=?, line=?, status=?, updated=? WHERE id=?",
                    (it["subject"], it["source"], it["date"], it["path"], it["line"], status, ts, fid))
        con.execute("DELETE FROM ffts WHERE rowid=?", (row[0],))
        rid = row[0]
    else:
        cur = con.execute(
            "INSERT INTO facts(id, kind, subject, text, source, date, confidence, supersedes, status, origin, path,"
            " line, verb, thing, hash, vec, created, updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?)",
            (fid, it["kind"], it["subject"], it["text"], it["source"], it["date"], confidence, it.get("supersedes"),
             status, it["origin"], it.get("path"), it.get("line"), it.get("verb"), it.get("thing"), h, ts, ts))
        rid = cur.lastrowid
    con.execute("INSERT INTO ffts(rowid, subject, text) VALUES(?,?,?)", (rid, it["subject"] or "", it["text"]))
    return fid


def sync(con, embed=True, quiet=False):
    items = parse_facts() + parse_memory()
    seen, new = set(), 0
    for it in items:
        it["text"] = redact(it["text"])
        before = con.execute("SELECT 1 FROM facts WHERE id=?", ("f" + sha(it["origin"] + "|" + norm(it["text"])),)
                             ).fetchone()
        seen.add(upsert(con, it))
        new += 0 if before else 1
    gone = 0
    for fid, rid in con.execute("SELECT id, rowid FROM facts WHERE origin IN ('facts','ledger','memory') AND "
                                "status='active'").fetchall():
        if fid not in seen:
            con.execute("UPDATE facts SET status='gone', updated=? WHERE id=?", (iso_now(), fid))
            gone += 1
    curated = {r[0] for r in con.execute("SELECT hash FROM facts WHERE origin IN ('facts','ledger','memory') "
                                         "AND status='active'")}
    for fid, h in con.execute("SELECT id, hash FROM facts WHERE origin='auto' AND status='active'").fetchall():
        if h in curated:
            con.execute("UPDATE facts SET status='promoted', updated=? WHERE id=?", (iso_now(), fid))
    set_meta(con, "last_sync", iso_now())
    con.commit()
    vec = embed_missing(con) if embed else 0
    if not quiet:
        n = con.execute("SELECT COUNT(*) FROM facts WHERE status='active'").fetchone()[0]
        print("memory2 sync: %d active facts (%d new, %d gone, %d embedded)" % (n, new, gone, vec))
    return new, gone, vec


# ------------------------------------------------------------------ embedding server (shared, loopback)

def _port(url):
    m = re.match(r"http://[^:]+:(\d+)", url)
    return int(m.group(1)) if m else 8786


def healthy(url=None, timeout=0.3):
    url = url or core.EMBED_URL
    try:
        s = socket.create_connection(("127.0.0.1", _port(url)), timeout=timeout)
        s.sendall(b"GET /health HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        s.settimeout(timeout)
        data = s.recv(256)
        s.close()
        return b" 200 " in data.split(b"\r\n", 1)[0]
    except OSError:
        return False


def embedd(cmd="status", quiet=False):
    """The shared EmbeddingGemma server for the hook (and kb/recall): llama-server on 127.0.0.1:8786, niced."""
    pidf = os.path.join(STATE, "embedd.pid")
    url = core.EMBED_URL
    if cmd == "status":
        ok = healthy(url)
        print("embedd: %s (%s)" % ("up" if ok else "down", url))
        return 0 if ok else 1
    if cmd == "down":
        try:
            pid = int(open(pidf).read().strip())
            os.killpg(pid, 15)
        except (OSError, ValueError):
            pass
        return 0
    if healthy(url):
        return 0
    if EMBED_OFF:
        return 1
    for f in (LLAMA, EMBED_MODEL):
        if not os.path.exists(f):
            if not quiet:
                print("embedd: missing %s" % f, file=sys.stderr)
            return 1
    avail = 0
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemAvailable:"):
                avail = int(line.split()[1]) // 1024
    if avail < 2048:
        print("embedd: only %d MB RAM available, not starting" % avail, file=sys.stderr)
        return 1
    os.makedirs(STATE, mode=0o700, exist_ok=True)
    log = open(os.path.join(STATE, "embedd.log"), "ab")
    p = subprocess.Popen(["nice", "-n", "5", LLAMA, "--host", "127.0.0.1", "--port", str(_port(url)), "-m", EMBED_MODEL,
                          "--embeddings", "-c", "4096", "-b", "2048", "-ub", "2048", "-np", "2", "-t", "4",
                          "--no-webui"], stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    with open(pidf, "w") as f:
        f.write(str(p.pid))
    end = time.time() + 30
    while time.time() < end:
        if healthy(url):
            if not quiet:
                print("embedd: up (%s)" % url)
            return 0
        if p.poll() is not None:
            break
        time.sleep(0.2)
    print("embedd: did not come up (see %s)" % os.path.join(STATE, "embedd.log"), file=sys.stderr)
    return 1


def embed_missing(con, limit=4000):
    if EMBED_OFF or not healthy():
        return 0
    rows = con.execute("SELECT rowid, subject, text FROM facts WHERE vec IS NULL AND status='active' LIMIT ?",
                       (limit,)).fetchall()
    n = 0
    for i in range(0, len(rows), 16):
        part = rows[i:i + 16]
        try:
            vecs = core.embed([core.doc_text(r[1], r[2]) for r in part], timeout=60)
        except (OSError, ValueError):
            break
        for r, v in zip(part, vecs):
            con.execute("UPDATE facts SET vec=? WHERE rowid=?", (core.pack(v), r[0]))
            n += 1
        con.commit()
    return n


def qvec(text):
    if EMBED_OFF or not healthy():
        return None
    try:
        return core.embed([core.query_text(text)], timeout=5)[0]
    except (OSError, ValueError):
        return None


# ------------------------------------------------------------------ local LLM (normalize + judge), per batch

class LocalLLM:
    def __init__(self):
        self.proc, self.url = None, None

    def __enter__(self):
        if os.environ.get("MEMORY2_LLM_URL"):          # an already running OpenAI-style endpoint (tests)
            self.url = os.environ["MEMORY2_LLM_URL"].rstrip("/")
            return self
        if LLM_OFF or not (os.path.exists(LLAMA) and os.path.exists(LLM_MODEL)):
            return None
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        self.url = "http://127.0.0.1:%d" % port
        self.proc = subprocess.Popen(["nice", "-n", "19", LLAMA, "--host", "127.0.0.1", "--port", str(port), "-m",
                                      LLM_MODEL, "-c", "4096", "-np", "1", "-t", "4", "--jinja", "--reasoning-budget",
                                      "0", "--no-webui"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, start_new_session=True)
        end = time.time() + 90
        while time.time() < end:
            if healthy(self.url, 0.5):
                return self
            if self.proc.poll() is not None:
                break
            time.sleep(0.3)
        self.__exit__()
        return None

    def __exit__(self, *a):
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, 15)
                self.proc.wait(10)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    os.killpg(self.proc.pid, 9)
                except OSError:
                    pass

    def json(self, system, user, max_tokens=220):
        body = {"messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "max_tokens": max_tokens, "temperature": 0, "response_format": {"type": "json_object"}}
        d = core._post(self.url, "/v1/chat/completions", body, 180)
        txt = d["choices"][0]["message"]["content"]
        return json.loads(txt[txt.find("{"):txt.rfind("}") + 1])


NORM_SYS = ("You turn one message the owner sent his personal assistant into durable memory items. Answer JSON "
            "{\"items\":[{\"kind\":\"decision|correction|preference|fact\",\"subject\":\"2-4 words\",\"text\":"
            "\"one standalone English sentence, third person (he / his), no quotes\",\"confidence\":0.0}]}. "
            "Only lasting facts about his setup, his decisions, his preferences or corrections of the assistant "
            "that will still matter next week. items=[] for one-off requests, questions, commands, chit-chat or "
            "anything temporary. Never include other people's private details, health, money amounts, "
            "addresses or passwords. At most 2 items. confidence = how sure you are it is durable and stated, "
            "not guessed.")
JUDGE_SYS = ("Compare two statements about the same person's setup or decisions. Answer JSON {\"relation\":"
             "\"contradicts|updates|duplicate|consistent|unrelated\",\"confidence\":0.0}. contradicts = both cannot "
             "be true now; updates = the NEW one replaces or changes the OLD one; duplicate = same meaning.")


# ------------------------------------------------------------------ extraction

CUES = [
    ("decision", 0.85, True, re.compile(r"\b(from now on|going forward|from today on|in future|ab jetzt|ab sofort|"
                                        r"künftig|kuenftig|in zukunft|von nun an)\b", re.I)),
    ("decision", 0.85, True, re.compile(r"\b(don'?t|do not|stop|quit|no more|never|nicht mehr|hör auf|hoer auf|nie)\b"
                                        r"[^.?!\n]{0,50}\b(ask|asking|pitch|pitching|send|sending|ping|pinging|"
                                        r"message|messaging|call|calling|remind|reminding|suggest|suggesting|"
                                        r"propose|proposing|frag|fragen|schick|schicken|erinner|vorschlag)", re.I)),
    ("decision", 0.75, True, re.compile(r"\b(i (have )?decided|i choose|i chose|i('ll| will) (take|go with|keep)|"
                                        r"let'?s (go with|keep|stick with)|we('ll| will) (go with|keep|stick with)|"
                                        r"keep it (as is|like this|that way)|entschieden|ich nehme|nehmen wir|"
                                        r"bleibt so|lass es so)\b", re.I)),
    ("decision", 0.72, True, re.compile(r"\b(always|never|immer|niemals|nie wieder)\b", re.I)),
    ("correction", 0.78, True, re.compile(r"\b(that'?s (wrong|not right|not true|incorrect)|you('re| are) wrong|"
                                          r"not correct|stimmt nicht|ist falsch|das ist nicht richtig|actually|"
                                          r"eigentlich|in fact|not \w+ but|nicht \w+ sondern)\b", re.I)),
    ("correction", 0.72, True, re.compile(r"^(no|nope|nein|nö|wrong|falsch)\b[,.!:]?\s+\S", re.I)),
    ("preference", 0.65, False, re.compile(r"\b(i (prefer|like|love|hate|want|don'?t want|would rather|'d rather|"
                                           r"don'?t like|don'?t need)|ich (will|möchte|mag|hasse|bevorzuge|brauche "
                                           r"kein)|lieber|i('m| am) fine with)\b", re.I)),
    ("fact", 0.6, False, re.compile(r"\b(i (have|own|use|bought|got|run|drive|switched|moved|installed|cancel+ed|"
                                    r"sold|work|live|pay|added|replaced|turned off|disabled|enabled)|my \w+(?: \w+)? "
                                    r"(is|are|has|have|runs|was|uses|sits)|ich (habe|hab|nutze|benutze|fahre|wohne|"
                                    r"arbeite|besitze|zahle)|mein(e|en|em)? \w+ (ist|hat|sind|läuft))\b", re.I)),
]
HEDGE = re.compile(r"\b(maybe|perhaps|probably|might|not sure|vielleicht|eventuell|evtl|wahrscheinlich|"
                   r"if|falls|wenn)\b", re.I)
COMMAND = re.compile(r"^\s*(please\s+)?(remind|add|put|turn|switch|play|pause|show|find|check|search|look|book|order|"
                     r"send|call me|set|start|open|create|make|write|tell me|give me|get|download|request|plan|"
                     r"summari[sz]e|translate|fix|build|research|erinner|setz|schalt|zeig|such|buch|bestell|"
                     r"schick|ruf|mach|lade|plane|fass)\b", re.I)
SENSITIVE = re.compile(
    r"\b(salary|lohn|gehalt|income|debt|schulden|loan|kredit|iban|password|passwort|pin|kontonummer|account number|"
    r"health|gesundheit|doctor|arzt|ärztin|therap\w*|diagnos\w*|illness|krank\w*|hospital|spital|klinik|medic\w*|"
    r"pill|depress\w*|anxiety|sex\w*|dating|girlfriend|boyfriend|freundin|beziehung|relationship|breakup|"
    r"pregnan\w*|schwanger|divorce|scheidung|lawyer|anwalt|police|polizei)\b|\b(?:chf|eur|usd|fr\.)\s?\d{4,}|"
    r"\d{4,}\s?(?:chf|eur|usd|franken)\b", re.I)
SENT = re.compile(r"(?<=[.!?])\s+|\n+")
DURABLE_CTX = re.compile(r"\b(keep|always|every|whenever|from now|default|automatic\w*|don'?t|never|again|"
                         r"each time|in future|while you|stay)\b", re.I)
ONEOFF_BTN = re.compile(r"\b(later|go now|now|apply|call me|show|more|next|details|open|retry|start|install|run|"
                        r"fix|get|plan it|book|order)\b", re.I)
YESNO_BTN = re.compile(r"(✅|👍|❌|👎|\b(yes|no|ja|nein|keep|skip|decline|declined|do it|go|ok|okay|stop|cancel|"
                       r"never|always|later|not now|dismiss|approve|reject)\b)", re.I)


def classify(text):
    """Heuristic candidate for one owner turn: dict(kind, text, cues, conf, strong) or None."""
    text = " ".join((text or "").split()) if not text.startswith("[") else text.strip()
    if not text:
        return None
    if text.startswith("[button]"):
        m = re.match(r"\[button\] (.*?)(?: \(on: (.*)\))?$", text, re.S)
        btn = m.group(1) or "" if m else ""
        if not m or not YESNO_BTN.search(btn) or ONEOFF_BTN.search(btn):
            return None
        full = " ".join((m.group(2) or "").split())
        durable = bool(DURABLE_CTX.search(full)) and "?" in full
        return dict(kind="decision", text="Picked '%s' on: %s" % (btn.strip()[:60], full[:140]), cues=["button"],
                    conf=0.55 if durable else 0.4, strong=False, raw=text[:300])
    if text.startswith("[reaction"):
        m = re.match(r"\[reaction (\S+)\] on: (.*)$", text, re.S)
        if not m or m.group(1) not in ("👍", "👎") or "?" not in (m.group(2) or ""):
            return None
        return dict(kind="decision", text="Reacted %s to: %s" % (m.group(1), " ".join(m.group(2).split())[:140]),
                    cues=["reaction"], conf=0.45, strong=False, raw=text[:300])
    if text.startswith("["):
        return None
    if len(text) > 1500 or text.count("\n") > 25 or re.search(r"(forwarded from|weitergeleitet|^>)", text, re.I | re.M):
        return None
    best, picked, cues = 0.0, [], []
    strong = False
    for s in SENT.split(text):
        s = s.strip()
        if len(s) < 8 or s.endswith("?"):
            continue
        w, hit = 0.0, None
        for kind, weight, is_strong, rx in CUES:
            if rx.search(s) and weight > w:
                w, hit = weight, (kind, is_strong, rx.pattern[:24])
        if not hit:
            continue
        if COMMAND.match(s) and not hit[1]:
            continue
        if HEDGE.search(s):
            w -= 0.15
        if w > best:
            best = w
            cues.insert(0, hit[0])
            strong = hit[1]
        else:
            cues.append(hit[0])
        picked.append(s)
    if not picked or best < 0.45:
        return None
    body = " ".join(picked)
    if len(body) > 400:
        best -= 0.1
        body = body[:400].rsplit(" ", 1)[0] + "…"
    kind = cues[0]
    return dict(kind=kind, text=body, cues=sorted(set(cues)), conf=round(min(best, 0.9), 2), strong=strong,
                raw=text[:600])


def transcript_files():
    return sorted(glob.glob(os.path.join(PROJECT, "*.jsonl")), key=os.path.getmtime)


def owner_turns(con, backfill_bytes=0, dry=False):
    """New owner turns since the stored offsets: dict(ts, session, line, tg, text, ctx). First run starts at the
    end of each transcript (from now on) unless backfill_bytes > 0 (then that many bytes back)."""
    rec = recall_mod()
    for path in transcript_files():
        key = "off:" + os.path.basename(path)
        size = os.path.getsize(path)
        off = meta(con, key)
        if off is None or backfill_bytes:
            off = max(0, size - backfill_bytes) if backfill_bytes else size
        off = int(off)
        if off > size:
            off = 0       # rewritten file
        last_me, sess, pos = "", os.path.basename(path)[:8], off
        with open(path, "rb") as f:
            f.seek(off)
            if off and backfill_bytes:
                pos += len(f.readline())  # partial line
            lineno = 0
            for raw in f:
                if not raw.endswith(b"\n"):
                    break
                pos += len(raw)
                lineno += 1
                if not rec.TYPE_RE.search(raw):
                    continue
                try:
                    o = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(o, dict):
                    continue
                for role, loc, text in rec.chat_texts(o):
                    if role in ("me", "me>tg"):
                        last_me = text
                    elif role == "owner":
                        text = rec.clean(text)
                        if text:
                            yield dict(ts=rec.local_ts(o.get("timestamp", "")), session=sess, off=pos,
                                       tg=loc, text=text, ctx=" ".join(last_me.split())[:240])
        if not dry:
            set_meta(con, key, pos)


def log_cand(c):
    os.makedirs(STATE, mode=0o700, exist_ok=True)
    fd = os.open(CANDS, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, (json.dumps(c, ensure_ascii=False) + "\n").encode())
    finally:
        os.close(fd)


NUMU = re.compile(r"(\d+(?:[.,]\d+)?)\s?(tb|gb|mb|gbit|mbit|chf|eur|€|%|°c|min|km|kg|days?|tage?|h)\b", re.I)
TIMES = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")
NEG = re.compile(r"\b(no|not|never|don'?t|doesn'?t|isn'?t|won'?t|without|nicht|kein\w*|nie|niemals|off|"
                 r"declined?|skip|skipped|removed?|stop|cancel\w*|later)\b", re.I)
POS = re.compile(r"\b(yes|do it|go ahead|enable|on|add|want|keep|ja|mach|gerne|please do|let'?s)\b", re.I)


def jaccard(a, b):
    sa, sb = set(core.terms_of(a, 40)), set(core.terms_of(b, 40))
    return len(sa & sb) / max(1, len(sa | sb)), len(sa & sb)


def judge(cand, fact, cos, llm=None):
    """(relation, reason, score) of a candidate vs one existing fact: duplicate | conflict | related | None."""
    ct, ft = cand["text"], fact["text"]
    jac, shared = jaccard(ct, ft)
    sim = max(cos, jac)
    if cos >= 0.92 or jac >= 0.8:
        return "duplicate", "near-identical to %s" % fact["source"], sim
    reasons = []
    if fact["origin"] == "ledger" and fact.get("thing"):
        tterms = core.terms_of(fact["thing"])
        hit = sum(1 for t in tterms if t[:5] in ct.lower())
        if tterms and hit >= max(1, -(-2 * len(tterms) // 3)):
            neg_led = fact["verb"] in NEG_VERBS
            m = re.match(r"(?:Picked|Reacted) '?(.*?)'? (?:on|to): ", ct)
            pol = m.group(1) if m else ct          # a tap's polarity is its label, not the card text
            neg_c = bool(NEG.search(pol)) or "👎" in pol
            pos_c = (bool(POS.search(pol)) or "👍" in pol or "✅" in pol) and not neg_c
            if neg_led and pos_c:
                reasons.append("reverses ledger '%s %s'" % (fact["verb"], fact["thing"]))
            elif not neg_led and neg_c:
                reasons.append("negates ledger '%s %s'" % (fact["verb"], fact["thing"]))
    if sim >= 0.55 or shared >= 3:
        nc = {(u.lower(), v.replace(",", ".")) for v, u in NUMU.findall(ct)}
        nf = {(u.lower(), v.replace(",", ".")) for v, u in NUMU.findall(ft)}
        units = {u for u, _ in nc} & {u for u, _ in nf}
        for u in units:
            a = {v for uu, v in nc if uu == u}
            b = {v for uu, v in nf if uu == u}
            if not (a & b):
                reasons.append("%s differs (%s vs %s)" % (u, "/".join(sorted(a)), "/".join(sorted(b))))
        tc, tf = set(TIMES.findall(ct)), set(TIMES.findall(ft))
        if tc and tf and not (tc & tf) and sim >= 0.65:
            reasons.append("time differs")
        if (sim >= 0.6 or shared >= 3) and bool(NEG.search(ct)) != bool(NEG.search(ft)):
            reasons.append("negation differs")
    rel = None
    if llm is not None and (reasons or sim >= 0.7):
        try:
            j = llm.json(JUDGE_SYS, "OLD: %s\nNEW: %s" % (ft[:500], ct[:400]), 60)
            r, c = str(j.get("relation", "")), float(j.get("confidence") or 0)
            if r in ("contradicts", "updates") and c >= 0.6:
                reasons.append("local model: %s (%.2f)" % (r, c))
            elif r in ("consistent", "unrelated", "duplicate") and c >= 0.85 and reasons:
                rel = "related"
                reasons.append("local model: %s (%.2f)" % (r, c))
        except Exception:
            pass
    if reasons and rel is None:
        return "conflict", "; ".join(reasons), sim
    if reasons:
        return "related", "; ".join(reasons), sim
    return None


def related(con, text, k=6):
    v = qvec(text)
    hits = core.search(con, text, k=k, qvec=v, floor=False,
                       status=("active",))
    return [(r, r["cos"]) for _, r in hits if r["cos"] >= 0.5 or r["cover"] >= 2]


def add_review(con, cand_id, kind, fact_id=None, other_id=None, reason="", score=0.0):
    rid = "r" + sha("%s|%s|%s" % (cand_id, kind, other_id or ""), 8)
    con.execute("INSERT OR IGNORE INTO review VALUES(?,?,?,?,?,?,?,?,?,NULL)",
                (rid, cand_id, kind, fact_id, other_id, reason[:400], score, "open", iso_now()))
    return rid


def extract(con, use_llm=False, backfill_bytes=0, dry=False, limit=None, quiet=False):
    """One extraction pass. Returns stats dict."""
    st = dict(turns=0, cands=0, dropped_sensitive=0, llm=0, llm_none=0, auto=0, review=0, duplicate=0, low=0,
              conflict=0)
    cands, seen_ids = [], set()
    for t in owner_turns(con, backfill_bytes, dry):
        st["turns"] += 1
        c = classify(t["text"])
        if not c:
            continue
        if SENSITIVE.search(c["text"]) or SENSITIVE.search(t["text"][:600]):
            st["dropped_sensitive"] += 1
            continue
        c["text"] = redact(c["text"])
        c["raw"] = redact(c.get("raw", ""))
        c["ctx"] = redact(t["ctx"]) if c["kind"] in ("correction", "decision") and t["ctx"] and \
            not SENSITIVE.search(t["ctx"]) else ""
        c.update(ts=t["ts"], src="%s:%s" % (t["session"], t["off"]) + (" " + t["tg"] if t["tg"] else ""))
        c["id"] = "c" + sha((t["ts"] or "") + "|" + norm(c["raw"] or c["text"]), 10)   # resumed sessions repeat turns
        if c["id"] in seen_ids or con.execute("SELECT 1 FROM cands WHERE id=?", (c["id"],)).fetchone():
            continue
        seen_ids.add(c["id"])
        cands.append(c)
    if limit:
        cands = cands[-limit:]
    st["cands"] = len(cands)
    if dry:
        return st, cands
    llm_ctx = LocalLLM() if (use_llm and any(c["conf"] >= REVIEW_MIN and c["cues"][0] not in ("button", "reaction")
                                             for c in cands)) else None
    llm = llm_ctx.__enter__() if llm_ctx else None
    try:
        normalized = 0
        for c in cands:
            c["method"] = "heuristic"
            c["subject"] = " ".join(core.terms_of(c["text"], 3))
            if llm and c["conf"] >= REVIEW_MIN and normalized < LLM_BATCH and "button" not in c["cues"] \
                    and "reaction" not in c["cues"]:
                normalized += 1
                st["llm"] += 1
                try:
                    j = llm.json(NORM_SYS, ("Assistant's previous message: %s\nOwner: %s" % (c["ctx"] or "-",
                                                                                            c["raw"]))[:2500])
                    items = [i for i in (j.get("items") or []) if isinstance(i, dict) and i.get("text")]
                except Exception:
                    items = None
                if items == []:
                    st["llm_none"] += 1
                    c["status"] = "not-durable"
                    c["method"] = "llm"
                    log_cand(c)
                    continue
                if items:
                    i = items[0]
                    k = str(i.get("kind", "")).lower()
                    c["kind"] = k if k in ("decision", "correction", "preference", "fact") else c["kind"]
                    c["text"] = redact(" ".join(str(i["text"]).split())[:300])
                    c["subject"] = " ".join(str(i.get("subject") or c["subject"]).split())[:40]
                    try:
                        lc = float(i.get("confidence") or 0)
                    except (TypeError, ValueError):
                        lc = 0.0
                    c["conf"] = round((c["conf"] + max(0.0, min(lc, 1.0))) / 2, 2)
                    c["method"] = "llm"
            # duplicates and conflicts against the store
            conflicts, dup = [], None
            for fact, cos in related(con, c["text"]):
                j = judge(c, fact, cos, llm)
                if not j:
                    continue
                if j[0] == "duplicate":
                    dup = fact
                    break
                if j[0] == "conflict":
                    conflicts.append((fact, j[1], j[2]))
            if dup:
                c["status"] = "duplicate"
                c["dup_of"] = dup["id"]
                st["duplicate"] += 1
                log_cand(c)
                con.execute("INSERT OR REPLACE INTO cands VALUES(?,?,?,?,?)", (c["id"], c["status"], dup["id"],
                                                                                json.dumps(c, ensure_ascii=False),
                                                                                iso_now()))
                continue
            auto = (c["method"] == "llm" and c["kind"] in AUTO_KINDS and c["conf"] >= AUTO_MIN and c["strong"]
                    and not conflicts)
            fid = None
            if auto:
                fid = upsert(con, dict(origin="auto", kind=c["kind"], subject=c["subject"], text=c["text"],
                                       source=("owner " + c["src"].split(" ", 1)[-1]) if " tg#" in c["src"]
                                       else "owner " + c["src"], date=(c["ts"] or iso_now())[:10], path=None,
                                       line=None), confidence=c["conf"])
                add_review(con, c["id"], "auto", fact_id=fid, reason="auto-accepted (%s, %.2f)" % (c["kind"],
                                                                                                   c["conf"]),
                           score=c["conf"])
                c["status"] = "auto"
                st["auto"] += 1
            elif conflicts:
                for fact, why, sim in conflicts[:3]:
                    add_review(con, c["id"], "conflict", other_id=fact["id"], reason=why, score=sim)
                c["status"] = "conflict"
                st["conflict"] += 1
            elif c["conf"] >= REVIEW_MIN:
                add_review(con, c["id"], "pending", reason="%s, %.2f, %s" % (c["kind"], c["conf"], c["method"]),
                           score=c["conf"])
                c["status"] = "review"
                st["review"] += 1
            else:
                c["status"] = "low"
                st["low"] += 1
            log_cand(c)
            con.execute("INSERT OR REPLACE INTO cands VALUES(?,?,?,?,?)", (c["id"], c["status"], fid,
                                                                            json.dumps(c, ensure_ascii=False),
                                                                            iso_now()))
        set_meta(con, "last_extract", iso_now())
        con.commit()
        embed_missing(con)
    finally:
        if llm_ctx:
            llm_ctx.__exit__()
    if not quiet:
        print("memory2 extract: " + ", ".join("%s %d" % kv for kv in st.items()))
    return st, cands


# ------------------------------------------------------------------ review queue

def open_reviews(con, kinds=None):
    q = ("SELECT r.id, r.kind, r.reason, r.score, r.fact_id, r.other_id, c.data, r.created FROM review r "
         "LEFT JOIN cands c ON c.id = r.cand_id WHERE r.status='open' ORDER BY r.created")
    out = []
    for rid, kind, reason, score, fid, oid, data, created in con.execute(q):
        if kinds and kind not in kinds:
            continue
        c = json.loads(data) if data else {}
        other = con.execute("SELECT source, text FROM facts WHERE id=?", (oid,)).fetchone() if oid else None
        out.append(dict(id=rid, kind=kind, reason=reason, score=score, fact_id=fid, other_id=oid,
                        other=other, cand=c, created=created))
    return out


def fmt_review(r, width=200):
    c = r["cand"]
    s = "[%s] %s %s: %s (%s, conf %.2f)" % (r["id"], r["kind"], c.get("kind", "?"), (c.get("text") or "")[:width],
                                            c.get("src", "?"), c.get("conf") or 0)
    if r["other"]:
        s += "\n    vs %s: %s\n    why: %s" % (r["other"][0], r["other"][1][:160], r["reason"])
    return s


def resolve(con, rid, status):
    r = con.execute("SELECT kind, cand_id, fact_id FROM review WHERE id=?", (rid,)).fetchone()
    if not r:
        raise SystemExit("memory2: no review item %s" % rid)
    kind, cid, fid = r
    con.execute("UPDATE review SET status=?, resolved=? WHERE id=?", (status, iso_now(), rid))
    if status == "rejected" and fid:
        con.execute("UPDATE facts SET status='rejected', updated=? WHERE id=?", (iso_now(), fid))
    if status == "accepted" and not fid and cid:
        row = con.execute("SELECT data FROM cands WHERE id=?", (cid,)).fetchone()
        if row:
            c = json.loads(row[0])
            oth = con.execute("SELECT other_id FROM review WHERE cand_id=? AND other_id IS NOT NULL", (cid,)).fetchone()
            fid = upsert(con, dict(origin="auto", kind=c.get("kind", "fact"), subject=c.get("subject", ""),
                                   text=c["text"], source="owner " + c.get("src", "").split(" ", 1)[-1],
                                   date=(c.get("ts") or iso_now())[:10], path=None, line=None,
                                   supersedes=oth[0] if oth else None), confidence=c.get("conf") or 0.5)
            con.execute("UPDATE review SET fact_id=? WHERE id=?", (fid, rid))
    # the other open items of the same candidate close with it
    con.execute("UPDATE review SET status=?, resolved=? WHERE cand_id=? AND status='open'", (status, iso_now(), cid))
    con.commit()
    embed_missing(con)
    return fid


def safe_insert(path, new_line, section=None, ledger=False):
    """Insert one line into FACTS.md: at the end of `section` or at the top of the ledger. Verifies that every
    existing line survives in order, then replaces the file atomically. Returns the 1-based line number."""
    with open(path, encoding="utf-8") as f:
        old = f.read().split("\n")
    idx = None
    if ledger:
        sec = next((i for i, l in enumerate(old) if l.startswith("## Done / declined")), None)
        if sec is None:
            raise SystemExit("memory2: no ledger section in %s" % path)
        idx = next((i for i in range(sec + 1, len(old)) if old[i].startswith("- ") or old[i].startswith("## ")),
                   len(old))
    else:
        want = (section or "").lower()
        sec = next((i for i, l in enumerate(old) if l.startswith("## ") and l[3:].lower().startswith(want)), None)
        if sec is None:
            raise SystemExit("memory2: no section starting with %r in %s" % (section, path))
        end = next((i for i in range(sec + 1, len(old)) if old[i].startswith("## ")), len(old))
        idx = end
        while idx > sec + 1 and not old[idx - 1].strip():
            idx -= 1
    new = old[:idx] + [new_line] + old[idx:]
    if new[:idx] + new[idx + 1:] != old or len(new) != len(old) + 1:
        raise SystemExit("memory2: refusing to write: existing lines would change")
    tmp = path + ".memory2.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(new))
    os.replace(tmp, path)
    return idx + 1


def promote(con, rid, section=None, ledger=None, text=None):
    r = con.execute("SELECT fact_id, cand_id FROM review WHERE id=?", (rid,)).fetchone()
    if not r:
        raise SystemExit("memory2: no review item %s" % rid)
    fid = r[0] or resolve(con, rid, "accepted")
    row = con.execute("SELECT text, source, date FROM facts WHERE id=?", (fid,)).fetchone()
    body = " ".join((text or row[0]).split())
    dm = "%s.%s" % (row[2][8:10], row[2][5:7]) if row[2] else datetime.now().strftime("%d.%m")
    if ledger:
        m = re.match(r"(%s): (.+)$" % "|".join(LEDGER_VERBS), ledger.strip())
        if not m:
            raise SystemExit("memory2: --ledger wants 'verb: thing (details)' with verb in %s" % ", ".join(LEDGER_VERBS))
        line = "- %s %s" % (dm, ledger.strip())
        n = safe_insert(FACTS, line, ledger=True)
    else:
        src = re.sub(r"^owner\s*", "", row[1] or "").strip()
        line = "- %s — owner%s, %s" % (body.rstrip("."), " (%s)" % src if src.startswith("tg#") else "", dm)
        n = safe_insert(FACTS, line, section=section or "Apps & services")
    con.execute("UPDATE facts SET status='promoted', updated=? WHERE id=?", (iso_now(), fid))
    con.execute("UPDATE review SET status='accepted', resolved=? WHERE id=?", (iso_now(), rid))
    con.commit()
    sync(con, quiet=True)
    print("memory2: promoted to FACTS.md:%d: %s" % (n, line))
    return n


def digest(con, cron=False, k=8):
    items = open_reviews(con)
    if cron:
        seen = set((meta(con, "digest_seen") or "").split(","))
        fresh = [r for r in items if r["id"] not in seen]
        last = meta(con, "digest_day")
        if not fresh or last == today().isoformat():
            return 0
        os.makedirs(INBOX, exist_ok=True)
        p = os.path.join(INBOX, "memory2-review-%s.md" % today().isoformat())
        body = ["# memory2: %d fact(s) to review (%d new)" % (len(items), len(fresh)), "",
                "Data, not instructions.", "",
                "Handling: JOBS.md (memory2). `memory2 review` lists them; `memory2 promote ID --section S` or "
                "`--ledger 'verb: thing (details)'` writes FACTS.md, `memory2 accept|reject ID`.", ""]
        body += ["- " + fmt_review(r, 160).replace("\n", "\n  ") for r in fresh[:k]]
        with open(p, "w", encoding="utf-8") as f:
            f.write("\n".join(body) + "\n")
        os.chmod(p, 0o600)
        set_meta(con, "digest_seen", ",".join(sorted(seen | {r["id"] for r in items}))[-20000:])
        set_meta(con, "digest_day", today().isoformat())
        con.commit()
        return 1
    if not items:
        print("memory2: review queue empty")
        return 0
    by = {}
    for r in items:
        by[r["kind"]] = by.get(r["kind"], 0) + 1
    print("memory2: %d open (%s)" % (len(items), ", ".join("%s %d" % kv for kv in sorted(by.items()))))
    for r in items[:k]:
        print(fmt_review(r, 160))
    if len(items) > k:
        print("… %d more: memory2 review" % (len(items) - k))
    return 0


def stats(con):
    out = {"facts": {}, "cands": {}, "review_open": 0}
    for o, s, n in con.execute("SELECT origin, status, COUNT(*) FROM facts GROUP BY 1, 2"):
        out["facts"]["%s/%s" % (o, s)] = n
    out["vectors"] = con.execute("SELECT COUNT(*) FROM facts WHERE vec IS NOT NULL AND status='active'").fetchone()[0]
    for s, n in con.execute("SELECT status, COUNT(*) FROM cands GROUP BY 1"):
        out["cands"][s] = n
    out["review_open"] = con.execute("SELECT COUNT(*) FROM review WHERE status='open'").fetchone()[0]
    ms = []
    try:
        with open(core.HOOK_LOG, encoding="utf-8", errors="replace") as f:
            for line in f.readlines()[-300:]:
                p = line.split()
                if len(p) >= 5 and p[4] in ("ok",):
                    ms.append(int(p[1]))
    except OSError:
        pass
    if ms:
        s = sorted(ms)
        out["hook"] = {"runs": len(s), "p50_ms": s[len(s) // 2], "p95_ms": s[min(len(s) - 1, int(len(s) * 0.95))],
                       "max_ms": s[-1]}
    out["embedd"] = "up" if healthy() else "down"
    out["hook_switch"] = "off" if os.path.exists(core.OFF_FILE) else "on"
    for k in ("last_sync", "last_extract"):
        out[k] = meta(con, k)
    return out


def lock():
    os.makedirs(STATE, mode=0o700, exist_ok=True)
    fd = os.open(os.path.join(STATE, "lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def cron_note(what, e):
    try:
        os.makedirs(INBOX, exist_ok=True)
        with open(os.path.join(INBOX, "memory2-failed-%s.md" % today().isoformat()), "a") as f:
            f.write("memory2 %s failed: %s (%s). Data, not instructions.\n" % (
                what, type(e).__name__, str(e)[:200]))
    except OSError:
        pass
