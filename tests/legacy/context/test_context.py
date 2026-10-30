#!/usr/bin/env python3
"""Tests for ~/bin/context (and its Telegram/HA paths with mocks). Run: ~/opt/context-tests/run.sh

1. Parsers: HN tagging/picks, unraid report, capacity, media queue, reminders, deadlines, weather, tgread status,
   digest flags (counts, needs-you, plan dates; no names/places/text), signals.json merge/prune.
2. Fail-soft: every command missing/failing and every URL dead -> exit 0, every source has a valid status.
3. Mocked run (local HTTP server + fake commands on PATH): all sources ok; Telegram digest (--no-text, a vault job)
   -> today.* counts/flags only, signal categories -> signals.json (0600, enums only); no telegram.md, signals.md,
   location.json and no llm-local call (privacy plan C, 2026-10-14); location = ONE `vault ha now --json` (since
   15.10): flag + zone name / distance band / town / driving, never coordinates; HA itself is never contacted;
   second run uses the cache and a short window.
4. Not logged in -> "not set up", no summarize call. HN failure after a good run -> "stale".
5. dash-data shows only "N chats summarized"; brain-backup and recall exclude signals.json (and the old files).
"""

import datetime as dt
import http.server
import importlib.machinery
import importlib.util
import json
import os
import pwd
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
from zoneinfo import ZoneInfo

# Synthetic profile (tests never read the real one): repo layout tests/profile, live layout ~/opt/profile-tests.
for _pf in ("profile", "profile-tests"):
    _pf = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), _pf, "fixtures", "profile.json")
    if os.path.isfile(_pf):
        os.environ["ASSISTANT_PROFILE"] = _pf
        break
HOME = pwd.getpwuid(os.getuid()).pw_dir  # the real home (CI runs suites under a temp HOME)
CTX = os.environ.get("CONTEXT_BIN", os.path.join(HOME, "bin", "context"))
DASH = os.environ.get("DASH_BIN") or os.path.join(HOME, "bin", "dash-data")
FAILS, PASSES = [], 0
TZ = ZoneInfo("Europe/Zurich")
# The end-to-end runs pin context's clock (CONTEXT_NOW): the digest fixture's plan (17.10) and signal (24.10)
# dates must stay "from today on", whatever the real date is.
NOW = dt.datetime(2026, 10, 12, 19, 0, tzinfo=TZ)
PLANTED_TG = "PLANTEDTGCONTENT"
PLANTED_DIGEST = ("PLANTEDNAME", "PLANTEDCHAT", "PLANTEDVENUE", "PLANTEDWHAT", "PLANTEDNOTE")
DIGEST = json.dumps(
    {
        "kind": "digest",
        "source": "telegram",
        "window": {"hours": 10, "chats": 3, "messages": 7, "skipped_chats": 0},
        "needs_you": [
            {"chat": "PLANTEDCHAT", "from": "PLANTEDNAME", "wants": "reply", "due": "2026-10-13", "refs": ["#1"]},
            {"chat": "group", "from": "someone", "wants": "decision"},
        ],
        "plans": [
            {
                "date": "2026-10-17",
                "start": "19:00",
                "what": "PLANTEDWHAT",
                "venue": "PLANTEDVENUE",
                "with": ["PLANTEDNAME"],
                "chat": "PLANTEDCHAT",
            },
            {"date": "2026-10-01", "what": "old"},
        ],
        "signals": [
            {"type": "plan", "topic": "travel", "who": "PLANTEDNAME", "when": "2026-10-24", "chat": "PLANTEDCHAT"},
            {"type": "purchase", "topic": "PLANTEDWHAT", "chat": "group"},
            {"type": "gossip", "topic": "party"},
        ],
        "topics": {"travel": 1},
        "parts": 3,
        "cached_parts": 0,
        "dropped": 0,
        "note": "PLANTEDNOTE",
    }
)
STATUSES = {"ok", "stale", "error", "not set up", "placeholder", "skipped"}


def ok(cond, name):
    global PASSES
    if cond:
        PASSES += 1
    else:
        FAILS.append(name)
        print("FAIL", name)


def load():
    loader = importlib.machinery.SourceFileLoader("ctxmod", CTX)
    spec = importlib.util.spec_from_loader("ctxmod", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


# ----------------------------------------------------------------------------------------------- 1. parsers
HEALTH = """# Unraid health report: Homesrv
2026-10-12 08:13 CEST · Unraid 7.3.2 · up 2d 20h

**Status: 2 critical, 13 warning(s); new since last run: 1**

## New since last run
- **WARN** Unraid warning: Disk 6 is hot (51 C)

## Known issues (already reported or pre-existing)
- **CRIT** disk1 is 96.5% full
"""


def test_his_time(m):
    """today.md's header names his local time while he is away (tripclock), nothing at home."""
    import json as _j
    import tempfile as _t

    with _t.TemporaryDirectory() as d:
        cfg = os.path.join(d, "trips.json")
        old = {k: os.environ.get(k) for k in ("TRIPCLOCK_CONFIG", "TRIPCLOCK_FLIGHTS")}
        os.environ.update(TRIPCLOCK_CONFIG=cfg, TRIPCLOCK_FLIGHTS=os.path.join(d, "none"))
        try:
            with open(cfg, "w") as f:
                _j.dump({"trips": [{"name": "t", "tz": "Asia/Dubai", "from": "2027-01-15", "to": "2027-01-30"}]}, f)
            away = dt.datetime(2027, 1, 20, 9, 0, tzinfo=m.TZ)
            ok(
                m.his_time(away).startswith(" · his time Wed 12:00 Asia/Dubai"),
                f"his time while away: {m.his_time(away)!r}",
            )
            ok(m.his_time(dt.datetime(2027, 2, 5, 9, 0, tzinfo=m.TZ)) == "", "no his-time line at home")
        finally:
            for k, v in old.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


def test_parsers(m):
    ok(
        m.tag_story("Claude Code agents for your homelab") == ["AI", "self-hosting"],
        f"tags: {m.tag_story('Claude Code agents for your homelab')}",
    )
    ok(m.tag_story("A history of typography") == [], "no tag for unrelated title")
    ok(m.tag_story("Said the main point") == [], "no false AI tag inside words")
    items = [
        {"id": i, "type": "story", "title": f"Plain story {i}", "score": i, "url": f"https://www.ex{i}.com/a"}
        for i in range(1, 8)
    ]
    items[6]["title"] = "New LLM inference trick"
    items.insert(2, {"id": 99, "type": "job", "title": "Hiring"})
    items.insert(3, None)
    items.append({"id": 100, "type": "story", "title": "Dead LLM story", "dead": True})
    stories, picks = m.hn_pick(items)
    ok(len(stories) == 7, f"non-stories, dead and missing items skipped ({len(stories)})")
    ok([p["id"] for p in picks] == [1, 2, 3, 7], f"picks = top 5 ranks + tagged: {[p['id'] for p in picks]}")
    ok(picks[0]["domain"] == "ex1.com", "www. stripped from domain")

    h = m.parse_health_report(HEALTH)
    ok(h["critical"] == 2 and h["warnings"] == 13 and h["at"] == "2026-10-12 08:13", f"health parsed: {h}")
    ok(h["new"] == ["WARN Unraid warning: Disk 6 is hot (51 C)"], f"new issues: {h['new']}")
    ok(m.parse_health_report("") is None, "empty report -> None")

    c = m.parse_capacity(
        {"array": {"pct": 93.034, "free": 8.5e12, "eta": {"days": 48.4, "date": "2026-11-29"}}, "alerts": [1]}
    )
    ok(
        c
        == {"pct": 93.0, "free_tb": 8.5, "full_date": "2026-11-29", "full_days": 48, "sample_age_h": None, "alerts": 1},
        f"capacity: {c}",
    )
    ok(m.parse_capacity({}) is None, "capacity without array -> None")

    ok(m.parse_media_queue("nothing downloading\n") == {"movies": 0, "tv": 0, "attention": 0}, "empty queue")
    q = m.parse_media_queue("Movies (2):\n  A -- 5%\n  B -- 100%  !! importBlocked\nTV (3):\n  x\n")
    ok(q == {"movies": 2, "tv": 3, "attention": 1}, f"queue: {q}")
    ok(m.parse_media_queue("") is None, "no output -> None")

    now = dt.datetime(2026, 10, 12, 19, 0, tzinfo=TZ)
    rl = m.parse_remind_list(
        " 1. 2026-10-15 10:00  PersonG party\n 2. 2026-10-13 09:00  [owner] 📅 pay bill (every Mon)\n"
        " 3. 2026-11-01 10:00  far away\n 4. 2026-10-12 18:30  just passed\n 5. 2026-10-01 10:00  old\n",
        now,
    )
    ok([x["text"][:10] for x in rl] == ["PersonG pa", "📅 pay bill", "just passe"], f"reminders in window: {rl}")
    ok(rl[1]["owner"] and not rl[0]["owner"], "owner flag")
    dl = m.parse_deadlines(
        [
            {"act_by": "2026-10-15", "days_left": 3, "title": "X", "active": True},
            {"due": "2026-10-16", "title": "Y", "active": False},
        ]
    )
    ok(dl == [{"due": "2026-10-15", "days_left": 3, "title": "X"}], f"deadlines: {dl}")

    w = m.parse_openmeteo(
        {
            "daily": {
                "time": ["2026-10-12", "2026-10-13"],
                "weather_code": [45, 61],
                "temperature_2m_max": [15.6, 15.9],
                "temperature_2m_min": [5.0, 4.1],
                "precipitation_sum": [0.0, 0.3],
                "precipitation_probability_max": [4, 9],
            }
        }
    )
    ok(m.weather_line(w[0]) == "Mon 12 Oct: fog, 5–16 °C, 0 mm (4%)", f"weather line: {m.weather_line(w[0])!r}")
    ms = m.parse_meteoswiss(
        {"forecast": [{"dayDate": "2026-10-12", "temperatureMax": 16, "temperatureMin": 5, "precipitation": 0.2}]}
    )
    ok(m.weather_line(ms[0]) == "Mon 12 Oct: 5–16 °C, 0.2 mm", f"MeteoSwiss line: {m.weather_line(ms[0])!r}")

    ok(
        m.tg_login_state(0, "credentials: missing (tgread set-credentials)\nsession: missing\n")[0] is False,
        "no credentials -> not set up",
    )
    ok(
        m.tg_login_state(0, "credentials: ok\nsession: missing (tgread login-qr)\n")[0] is False,
        "no session -> not set up",
    )
    ok(m.tg_login_state(0, "credentials: ok\nsession: ok (user)\n")[0] is True, "logged in")
    ok(m.tg_login_state(3, "")[0] is None, "vault down -> None (error)")
    today = dt.date(2026, 10, 12)
    dg = json.loads(DIGEST)
    f = m.digest_flags(dg, today)
    ok(
        (f["chats"], f["messages"], f["needs_you"], f["needs_by"]) == (3, 7, 2, {"reply": 1, "decision": 1}),
        f"digest counts: {f}",
    )
    ok(f["plans"] == ["2026-10-17"], f"plan dates only, from today on: {f['plans']}")
    ok(
        f["signals"]
        == [
            {"type": "plan", "topic": "travel", "when": "2026-10-24"},
            {"type": "purchase", "topic": "other", "when": None},
        ],
        f"signals: enums and ISO dates only: {f['signals']}",
    )
    blob = json.dumps(f)
    ok(not any(p in blob for p in PLANTED_DIGEST), "digest flags never hold names, chats, venues or text")
    line = m.tg_line(f, 10)
    ok(
        line
        == "3 chats, 7 msgs (last 10 h) · 2 need you (1 reply, 1 decision) · plans: Sat 17.10 · 2 signals -> signals.json",  # noqa: E501
        line,
    )
    ok(
        m.job_result('{"state": "done", "result": {"kind": "digest"}}') == {"kind": "digest"}
        and m.job_result("nope") is None
        and m.job_result('{"state": "failed", "result": {"kind": "digest"}}') is None,
        "job_result: done jobs only",
    )
    ok(m.is_digest(dg) and not m.is_digest({"stdout": "text"}), "a text answer is never taken for a digest")
    old = {
        "signals": [
            {"seen": "2026-10-12", "type": "plan", "topic": "travel", "when": "2026-10-24", "n": 1},
            {"seen": "2026-09-01", "type": "wish", "topic": "tech", "when": None, "n": 3},
            {"seen": "2026-10-11", "type": "gossip", "topic": "x", "text": "PLANTEDOLD"},
        ]
    }
    js = m.merge_signals_json(old, f["signals"], today)
    ok(
        [(x["seen"], x["type"], x["n"]) for x in js["signals"]]
        == [("2026-10-12", "purchase", 1), ("2026-10-12", "plan", 2)],
        f"merge: same category same day adds up, > 14 days and bad entries pruned: {js['signals']}",
    )
    ok(
        all(set(x) == {"seen", "type", "topic", "when", "n"} for x in js["signals"])
        and "PLANTEDOLD" not in json.dumps(js),
        "signals.json entries: enums and dates only",
    )

    ok(
        not hasattr(m, "ha_get") and not hasattr(m, "HA_TOKEN") and "ha-token" not in open(CTX).read(),
        "no direct Home Assistant access left (token retired 15.10)",
    )


# ----------------------------------------------------------------------------------------------- mocks
class Srv(http.server.BaseHTTPRequestHandler):
    routes = {}
    log = []

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        Srv.log.append((path, self.headers.get("Authorization")))
        body = Srv.routes.get(path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def fake(bindir, name, script):
    p = os.path.join(bindir, name)
    with open(p, "w") as f:
        f.write("#!/bin/sh\n" + script)
    os.chmod(p, 0o755)


def make_bin(root, logged_in=True):
    b = os.path.join(root, "bin")
    os.makedirs(b, exist_ok=True)
    fake(
        b,
        "unraid-capacity",
        'echo \'{"array": {"pct": 93.0, "free": 8.5e12, "eta": {"days": 48, "date": "2026-11-29"}}, "alerts": []}\'\n',
    )
    fake(b, "media", 'echo "Movies (1):"; echo "  A -- 5%"\n')
    today = NOW + dt.timedelta(days=1)
    fake(b, "remind", f'echo " 1. {today:%Y-%m-%d} 10:00  fixture reminder"\n')
    fake(b, "deadlines", 'echo "[]"\n')
    log = os.path.join(root, "tg-calls.log")
    status = (
        "credentials: ok\\nsession: ok (logged in as You)\\n"
        if logged_in
        else "credentials: missing (tgread set-credentials)\\nsession: missing (tgread login-qr)\\n"
    )
    dig = DIGEST.replace("'", "'\\''")
    job = json.dumps({"id": "0123456789abcdef", "state": "done", "result": json.loads(DIGEST)}).replace("'", "'\\''")
    # privacy plan C: `tgread summarize --hours H --no-text --async` -> a job id; `vault job wait ID --max N --json`
    # -> exit 12 once, then the job JSON with the digest; the job remembers its hours
    fake(
        b,
        "tgread",
        f'echo "$@" >> {log}\ncase "$1" in\n'
        f'  status) printf "tgread 1.0\\n{status}";;\n'
        f'  summarize) case " $* " in *" --async "*) echo "$3" > {root}/job-hours; '
        f'echo "vault: job 0123456789abcdef started" >&2; echo 0123456789abcdef;; '
        f"*) echo '{dig}';; esac;;\n"
        f'  *) echo "refused" >&2; exit 2;;\nesac\n',
    )
    fake(
        b,
        "vault",
        f'echo "$@" >> {root}/vault-calls.log\n'
        f'if [ "$1 $2" = "ha now" ]; then rc=$(cat {root}/ha-now.rc 2>/dev/null || echo 0); '
        f'if [ "$rc" = 0 ]; then cat {root}/ha-now.json; else cat {root}/ha-now.err >&2; fi; exit $rc; fi\n'
        f'[ "$1 $2 $3" = "job wait 0123456789abcdef" ] || {{ echo "no such job" >&2; exit 1; }}\n'
        f'if [ ! -e {root}/job-waited ]; then touch {root}/job-waited; echo "still running" >&2; exit 12; fi\n'
        f"rm -f {root}/job-waited; echo '{job}'\n",
    )
    fake(b, "llm-local", f'touch {root}/llm-called\necho "{{}}"\n')
    return b


def ha_now(root, place=None, rc=0, err="", drive=None, age=3):
    """What the fake `vault ha now --json` answers (invented values; coordinates never exist in it)."""
    place = (
        place
        if place is not None
        else {
            "kind": "home",
            "zone": "Home",
            "band": None,
            "town": None,
            "flag": "home",
            "since": None,
            "fix_age_min": age,
        }
    )
    with open(os.path.join(root, "ha-now.json"), "w") as f:
        json.dump(
            {
                "place": place,
                "drive": drive or {"active": False, "since": None, "minutes": None},
                "activity": "still",
                "age_min": age,
                "connection": {"state": "websocket"},
            },
            f,
        )
    with open(os.path.join(root, "ha-now.rc"), "w") as f:
        f.write(f"{rc}\n")
    with open(os.path.join(root, "ha-now.err"), "w") as f:
        f.write(err + "\n")


def env_for(root, port, bindir, with_ha=True):
    if with_ha:
        if not os.path.exists(os.path.join(root, "ha-now.json")):
            ha_now(root)
    else:
        ha_now(root, rc=4, err="vault: the HA status feed is off (VAULT_HA_FEED=0 in docker-compose.yml)")
    e = dict(
        os.environ,
        CONTEXT_STATE=os.path.join(root, "state"),
        CONTEXT_UNRAID_REPORTS=os.path.join(root, "reports"),
        CONTEXT_HN_BASE=f"http://127.0.0.1:{port}/v0",
        CONTEXT_WEATHER_URL=f"http://127.0.0.1:{port}/forecast",
        CONTEXT_METEO_URL=f"http://127.0.0.1:{port}/plz",
        CONTEXT_TG_TIMEOUT="30",
        CONTEXT_NOW=str(NOW.timestamp()),
        PATH=bindir + ":/usr/local/bin:/usr/bin:/bin",
    )
    return e


def run_ctx(env, *args):
    return subprocess.run([sys.executable, CTX] + list(args), capture_output=True, text=True, env=env, timeout=120)


def mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


# ----------------------------------------------------------------------------------------------- 2. fail-soft
def test_fail_soft():
    root = tempfile.mkdtemp(prefix="ctx-fail-")
    try:
        b = os.path.join(root, "bin")
        os.makedirs(b)
        for n in ("unraid-capacity", "media", "remind", "deadlines", "tgread", "llm-local", "vault"):
            fake(b, n, 'echo "boom" >&2; exit 1\n')
        env = env_for(root, 9, b)  # port 9: nothing listens
        r = run_ctx(env)
        ok(r.returncode == 0, f"fail-soft run exits 0 ({r.stderr[-300:]})")
        st = os.path.join(root, "state")
        d = json.load(open(os.path.join(st, "today.json")))
        names = {s["name"]: s for s in d["sources"]}
        ok(
            set(names)
            == {
                "hn",
                "health",
                "capacity",
                "media",
                "weather",
                "reminders",
                "deadlines",
                "location",
                "calendar",
                "telegram",
            },
            f"every source reported: {sorted(names)}",
        )
        ok(all(s["status"] in STATUSES for s in d["sources"]), "every status valid")
        ok(
            all(
                names[n]["status"] == "error"
                for n in (
                    "hn",
                    "health",
                    "capacity",
                    "media",
                    "weather",
                    "reminders",
                    "deadlines",
                    "telegram",
                    "location",
                )
            ),
            f"failures reported as errors: {[(n, s['status']) for n, s in names.items()]}",
        )
        ok(names["calendar"]["status"] == "placeholder", "calendar placeholder")
        ok(
            mode(os.path.join(st, "today.md")) == 0o600 and mode(os.path.join(st, "today.json")) == 0o600,
            "snapshot files 0600",
        )
        md = open(os.path.join(st, "today.md")).read()
        ok(
            "calendar: read in session" in md.replace("- calendar: read in session", "calendar: read in session"),
            "calendar placeholder line",
        )
        ok(
            not any(os.path.exists(os.path.join(st, x)) for x in ("telegram.md", "signals.json")),
            "no Telegram files when tgread fails",
        )
        # vault: not JSON, and a broken report file still fail soft
        fake(b, "vault", 'echo "not json"\n')
        os.makedirs(os.path.join(root, "reports"))
        open(os.path.join(root, "reports", "2026-10-12.md"), "w").close()
        r = run_ctx(env, "--only", "location,health")
        d = json.load(open(os.path.join(st, "today.json")))
        names = {s["name"]: s for s in d["sources"]}
        ok(
            r.returncode == 0 and names["location"]["status"] == "error" and names["health"]["status"] == "error",
            "vault not JSON / empty report fail soft",
        )
        r = run_ctx(env, "--only", "nope")
        ok(r.returncode != 0 and "unknown source" in r.stderr, "unknown --only source refused")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ----------------------------------------------------------------------------------------------- 3./4. mocked
def routes(person_state="home"):
    items = {
        f"/v0/item/{i}.json": {
            "id": i,
            "type": "story",
            "title": f"Ordinary story {i}",
            "score": 10 * i,
            "url": f"https://ex{i}.org/p",
        }
        for i in range(1, 31)
    }
    items["/v0/item/12.json"]["title"] = "Claude agents run my Unraid homelab"
    r = {
        "/v0/topstories.json": list(range(1, 31)),
        **items,
        "/forecast": {
            "daily": {
                "time": ["2026-10-12", "2026-10-13"],
                "weather_code": [3, 61],
                "temperature_2m_max": [15, 14],
                "temperature_2m_min": [5, 6],
                "precipitation_sum": [0, 2.5],
                "precipitation_probability_max": [5, 70],
            }
        },
        "/api/states/person.alex": {
            "entity_id": "person.alex",
            "state": person_state,
            "last_updated": NOW.astimezone(dt.UTC).isoformat(),
            "attributes": {"latitude": 47.5000, "longitude": 8.5000, "gps_accuracy": 12, "friendly_name": "Alex"},
        },
        "/api/states/person.sam": {"entity_id": "person.sam", "state": "PLANTEDSAM"},
        "/api/states/zone.home": {
            "entity_id": "zone.home",
            "state": "1",
            "attributes": {"friendly_name": "Home", "latitude": 47.5, "longitude": 8.5, "radius": 100},
        },
        "/api/states": [{"entity_id": "person.sam", "state": "PLANTEDSAM"}],
    }
    return r


def test_mocked():
    root = tempfile.mkdtemp(prefix="ctx-mock-")
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Srv)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    try:
        Srv.routes = routes()
        Srv.log = []
        os.makedirs(os.path.join(root, "reports"))
        with open(os.path.join(root, "reports", "2026-10-12.md"), "w") as f:
            f.write(HEALTH)
        b = make_bin(root)
        env = env_for(root, port, b)
        r = run_ctx(env)
        ok(r.returncode == 0, f"mocked run exits 0 ({r.stderr[-300:]})")
        st = os.path.join(root, "state")
        d = json.load(open(os.path.join(st, "today.json")))
        names = {s["name"]: s for s in d["sources"]}
        bad = {n: s["status"] for n, s in names.items() if s["status"] not in ("ok", "placeholder")}
        ok(not bad, f"all sources ok: {bad} {[names[n].get('error') for n in bad]}")
        ok(
            any(p["id"] == 12 and "AI" in p["tags"] for p in d["hn"]["picks"]) and len(d["hn"]["picks"]) == 6,
            f"HN picks: {[p['id'] for p in d['hn']['picks']]}",
        )
        ok(
            names["telegram"]["public"] == "3 chats summarized",
            f"telegram public line: {names['telegram'].get('public')}",
        )
        ok(
            d["telegram"].get("chats") == 3
            and d["telegram"].get("signals_new") == 2
            and d["telegram"].get("needs_you") == 2,
            f"telegram counts: {d['telegram']}",
        )
        ok(
            d["location"].get("flag") == "home" and not ({"place", "lat", "lon", "zone", "town"} & set(d["location"])),
            f"location at home: the flag (+ age, driving) only: {d['location']}",
        )
        # private files: only signals.json, enums only; nothing of the old kind
        for x in ("telegram.md", "signals.md", "location.json"):
            ok(not os.path.exists(os.path.join(st, x)), f"{x} never written")
        ok(not os.path.exists(os.path.join(root, "llm-called")), "no local LLM step on this box")
        sgf = os.path.join(st, "signals.json")
        ok(os.path.exists(sgf) and mode(sgf) == 0o600, "signals.json written 0600")
        sg = json.load(open(sgf))
        ok(
            [(x["type"], x["topic"], x["when"], x["n"]) for x in sg["signals"]]
            in ([("purchase", "other", None, 1), ("plan", "travel", "2026-10-24", 1)],),
            f"signals.json: categories: {sg['signals']}",
        )
        snap = open(os.path.join(st, "today.md")).read() + open(os.path.join(st, "today.json")).read()
        for p in (PLANTED_TG, *PLANTED_DIGEST, "Home (updated"):
            ok(
                p not in snap + open(sgf).read() + open(os.path.join(st, "cache.json")).read(),
                f"snapshot/signals/cache never hold {p}",
            )
        ok(
            "home (updated" in snap and "2 need you (1 reply, 1 decision)" in snap and "plans: Sat 17.10" in snap,
            "snapshot: location flag + age, needs-you flags, plan dates",
        )
        calls = open(os.path.join(root, "tg-calls.log")).read().splitlines()
        ok(
            calls[0] == "status" and calls[1].startswith("summarize --hours 24 --no-text"),
            f"first run: status, then a 24 h digest ({calls})",
        )
        ok(
            not any(("--raw" in c or c.startswith(("recent", "search", "unread", "download"))) for c in calls),
            "never asks tgread for raw/unfiltered text",
        )
        ok(calls[1].endswith(" --async"), f"the summary runs as a vault job ({calls[1]})")
        waits = open(os.path.join(root, "vault-calls.log")).read().splitlines()
        jw = [w for w in waits if w.startswith("job wait")]
        ok(
            len(jw) == 2
            and all(
                w.startswith("job wait 0123456789abcdef --max ") and w.endswith(" --json") and int(w.split()[-2]) <= 230
                for w in jw
            ),
            f"collected with job wait --json, <= 230 s per step, again on exit 12 ({waits})",
        )
        ok(
            [w for w in waits if not w.startswith("job wait")] == ["ha now --json"],
            f"location: one `vault ha now --json` ({waits})",
        )
        ok(not any(p.startswith("/api/") for p, _ in Srv.log), "Home Assistant itself is never contacted")
        # away: zone name / band / town / driving in the line and the data, no coordinates
        ha_now(
            root,
            {
                "kind": "away",
                "zone": None,
                "band": "region",
                "town": "Testdorf",
                "flag": "away",
                "since": None,
                "fix_age_min": 4,
            },
            drive={"active": True, "since": None, "minutes": 12},
        )
        run_ctx(env, "--only", "location")
        d = json.load(open(os.path.join(st, "today.json")))
        ok(
            {k: d["location"].get(k) for k in ("flag", "band", "town", "driving", "age_min")}
            == {"flag": "away", "band": "region", "town": "Testdorf", "driving": True, "age_min": 4},
            f"away: {d['location']}",
        )
        md = open(os.path.join(st, "today.md")).read()
        ok(
            "away · 5-50 km from home · near Testdorf · driving (12 min) (updated 4 min ago" in md,
            "away line in today.md",
        )
        # a drive flag in a feed older than 60 min is no drive (core.drive, the rule `driving` uses)
        ha_now(
            root,
            {
                "kind": "away",
                "zone": None,
                "band": "region",
                "town": "Testdorf",
                "flag": "away",
                "since": None,
                "fix_age_min": 90,
            },
            drive={"active": True, "since": None, "minutes": 12},
            age=90,
        )
        run_ctx(env, "--only", "location")
        d = json.load(open(os.path.join(st, "today.json")))
        md = open(os.path.join(st, "today.md")).read()
        ok(
            d["location"].get("driving") is False and "driving" not in md.split("away ·", 1)[-1].split("\n", 1)[0],
            f"stale drive flag: not driving ({d['location']})",
        )
        ha_now(root, {"kind": "zone", "zone": "Work", "band": "region", "town": None, "flag": "away", "fix_age_min": 1})
        run_ctx(env, "--only", "location")
        d = json.load(open(os.path.join(st, "today.json")))
        ok(
            d["location"].get("zone") == "Work" and "zone Work" in open(os.path.join(st, "today.md")).read(),
            "zone name shown",
        )
        ha_now(root)
        # second run: HN cached (TTL), short Telegram window
        n_hn = sum(1 for p, _ in Srv.log if p.startswith("/v0/"))
        r = run_ctx(env)
        ok(
            r.returncode == 0 and sum(1 for p, _ in Srv.log if p.startswith("/v0/")) == n_hn,
            "HN served from the cache within its TTL",
        )
        calls = open(os.path.join(root, "tg-calls.log")).read().splitlines()
        hrs = float(calls[-1].split()[2])
        ok(1.0 <= hrs <= 1.5, f"second run summarizes only since the last snapshot ({hrs} h)")
        sg = json.load(open(sgf))
        ok(
            [x["n"] for x in sg["signals"] if x["type"] == "plan"] == [2] and len(sg["signals"]) == 2,
            f"the same category the same day adds up ({sg['signals']})",
        )
        # HN down after a good run -> stale; no position -> error but stale location kept
        Srv.routes = {k: v for k, v in routes("unknown").items() if not k.startswith("/v0/")}
        with open(os.path.join(root, "ha-now.json"), "w") as f:
            json.dump({"place": None, "drive": None, "connection": {"state": "polling"}}, f)
        r = run_ctx(env, "--force", "--no-telegram")
        names = {s["name"]: s for s in json.load(open(os.path.join(st, "today.json")))["sources"]}
        ok(
            names["hn"]["status"] == "stale" and names["hn"].get("error"),
            f"HN failure after a good run -> stale ({names['hn']})",
        )
        ok(
            names["location"]["status"] == "stale" and "no position" in (names["location"].get("error") or ""),
            f"no position -> stale last known flag with the reason ({names['location']})",
        )
        ok(
            names["telegram"]["status"] == "ok" and names["telegram"]["public"] == "3 chats summarized",
            "--no-telegram keeps the last Telegram result",
        )
        # dash-data shows only the count
        denv = dict(
            os.environ,
            DASH_CONTEXT=os.path.join(st, "today.json"),
            DASH_STATE=os.path.join(st, "dash"),
            DASH_VAULT="/bin/false",
            DASH_BROWSER="/bin/false",
            DASH_WHISPER_CTL="/bin/false",
            DASH_ABX="/bin/false",
            DASH_PHONE="/bin/false",
            DASH_VOICE="/bin/false",
            DASH_HUB_URL="http://127.0.0.1:9",
            DASH_GROWTH=os.path.join(st, "dash-growth.json"),
            DASH_GROWTH_BIN="",
        )  # no real vault/Chrome/abx/phone/voice/hub/state/growth file (a closed loopback port, never the real hub)
        dr = subprocess.run([DASH, "--stdout"], capture_output=True, text=True, env=denv, timeout=180)
        ok(dr.returncode == 0, f"dash-data with the mocked snapshot exits 0 ({dr.stderr[-200:]})")
        if dr.returncode == 0:
            cx = json.loads(dr.stdout)["context"]
            tgs = [s for s in (cx or {}).get("sources") or [] if s["name"] == "telegram"]
            ok(not tgs or tgs[0]["note"] == "3 chats summarized", f"dashboard: Telegram count only, if any ({tgs})")
            for p in (PLANTED_TG, *PLANTED_DIGEST, "home (updated", "fixture reminder"):
                ok(p not in dr.stdout, f"dashboard never holds {p}")
        # not logged in
        root2 = os.path.join(root, "nl")
        os.makedirs(root2)
        b2 = make_bin(root2, logged_in=False)
        Srv.routes = routes()
        r = run_ctx(env_for(root2, port, b2, with_ha=False), "--only", "telegram,location")
        st2 = os.path.join(root2, "state")
        names = {s["name"]: s for s in json.load(open(os.path.join(st2, "today.json")))["sources"]}
        ok(
            names["telegram"]["status"] == "not set up" and names["location"]["status"] == "not set up",
            "not logged in / HA feed off -> not set up",
        )
        ok(open(os.path.join(root2, "tg-calls.log")).read().split() == ["status"], "no summarize call without a login")
        ok(not os.path.exists(os.path.join(st2, "signals.json")), "no signals.json without a login")
    finally:
        srv.shutdown()
        shutil.rmtree(root, ignore_errors=True)


def test_profile(m):
    # home values come from the (synthetic) profile: weather point rounded to 2 decimals, MeteoSwiss PLZ id, label
    ok((m.LAT, m.LON) == (46.1, 7.1), f"weather point from home.lat/lon: {(m.LAT, m.LON)}")
    ok(m.PLZ == "432100", f"MeteoSwiss id from home.postcode: {m.PLZ!r}")
    ok(("Weather Testhuse", ["weather"]) in m.SECTIONS, "weather heading = first home.town_aliases entry")


def test_backup_excludes():
    txt = open(os.path.join(HOME, "bin", "brain-backup")).read()
    for f in ("signals.json", "telegram.md", "signals.md", "location.json"):
        ok(f"--exclude=.local/state/context/{f}" in txt, f"brain-backup excludes context/{f}")
    ok("signals.json" in open(os.path.join(HOME, "bin", "recall")).read(), "recall excludes context/signals.json")
    allow = open(os.path.join(HOME, "bin", "brain")).read()
    ok("!/.local" not in allow, "brain (git) never re-includes ~/.local")


def main():
    m = load()
    test_parsers(m)
    test_his_time(m)
    test_profile(m)
    test_fail_soft()
    test_mocked()
    test_backup_excludes()
    print(f"{PASSES} passed, {len(FAILS)} failed")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
