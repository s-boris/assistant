#!/usr/bin/env python3
"""Offline tests for ~/bin/lib/usage.py + ~/bin/lib/product.py (+ the usage/product CLIs, cron-fail's spool line,
tg-cmd's /usage and exec record, the PostToolUse hook). Fake fixtures only, built in a temp dir; no network.
Env: USAGE_LIB (lib dir to test, default ~/bin/lib), CRON_FAIL_BIN, TGCMD_BIN, USAGE_HOOK (copies to test)."""

import json
import os
import pwd
import sqlite3
import subprocess
import sys
import tempfile
import time

T = tempfile.mkdtemp(prefix="usage-tests-")
REAL_HOME = pwd.getpwuid(os.getuid()).pw_dir  # CI runs suites under a temp HOME
LIB = os.environ.get("USAGE_LIB") or (
    os.path.join(os.path.dirname(os.environ["USAGE_CLI"]), "lib")
    if os.environ.get("USAGE_CLI")
    else os.path.join(REAL_HOME, "bin/lib")
)
NOW = time.time()
SECRETS = [
    "secret text",
    "Secret meeting",
    "Somewhere 1",
    "no that's wrong",
    "Please check the queue",
    "Fake Person",
    "47.1234",
    "so I can watch",
    "what about tomorrow",
    "hello there",
]
P = os.path.join
for d in (
    "bin",
    "state",
    "tg",
    "skills/media-requests",
    "skills/reminders",
    "conf",
    "reports",
    "inbox",
    "transcripts",
    "ws",
):
    os.makedirs(P(T, d), exist_ok=True)
for name in ("media", "remind", "tg-cmd", "lastcall", "cron-fail", "talk-button"):
    with open(P(T, "bin", name), "w") as f:
        f.write(
            f'#!/bin/sh\n# fake {name}: test script for the usage tests\ntg "x"\n'
            if name == "lastcall"
            else f"#!/bin/sh\n# fake {name}: test script\n"
        )
    os.chmod(P(T, "bin", name), 0o755)
with open(P(T, "bin", "tg-cmd"), "a") as f:  # the command table + HIDDEN as the catalogue reads them (text only)
    f.write(
        'COMMANDS = {\n    "home": ("Home panel", cmd_home),\n    "cat": ("Cat week", cmd_cat),\n}\nHIDDEN = {"home"}\n'
    )
ENV = {
    "USAGE_STATE": P(T, "state"),
    "USAGE_BIN": P(T, "bin"),
    "USAGE_CATALOGUE": P(T, "conf/features.json"),
    "USAGE_JOURNAL": P(T, "tg/inbound-journal.jsonl"),
    "USAGE_TRANSCRIPTS": P(T, "transcripts"),
    "USAGE_CALLS_CMD": P(T, "fake-voice-hub"),
    "USAGE_PHONEGW_DB": P(T, "phonegw.db"),
    "USAGE_TALK_LOG": P(T, "talk.log"),
    "USAGE_MENU_COUNTS": P(T, "counts.json"),
    "USAGE_CALCACHE": P(T, "calcache.json"),
    "USAGE_PLACE_STATE": P(T, "place-state.json"),
    "ASSISTANT_PROFILE": P(T, "profile.json"),
    "USAGE_REMIND_FIRED": P(T, "fired.log"),
    "USAGE_TG_DIR": P(T, "tg"),
    "USAGE_SKILLS": P(T, "skills"),
    "USAGE_CRONTAB": P(T, "crontab"),
    "USAGE_WORKSPACE": P(T, "ws"),
    "USAGE_REPORTS": P(T, "reports"),
    "USAGE_INBOX": P(T, "inbox"),
    "USAGE_BRAIN_GIT": P(T, "nobrain"),
    "USAGE_NO_SUBPROC": "1",
    "TRIPCLOCK_OFF": "1",
    "USAGE_SECURITY_POSTURE": P(T, "posture.txt"),
    "USAGE_EVENTS_DB": P(T, "events.db"),
    "USAGE_TOOLS": P(T, "tools.toml"),
    "USAGE_SCHEDULES": P(T, "schedules.toml"),
}
os.environ.update(ENV)
os.environ.pop("USAGE", None)
for _k in ("USAGE_TEST", "TG_CMD_TEST", "PYTEST_CURRENT_TEST"):  # CI sets USAGE_TEST; the actor cases need it unset
    os.environ.pop(_k, None)
sys.path.insert(0, LIB)
import product  # noqa: E402
import usage  # noqa: E402

FAILS = []


def ok(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t))


def wj(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f)


# ---------------------------------------------------------------- fixtures
wj(P(T, "profile.json"), {"work": {"town": "Testwork"}, "locale": {"tz": "Europe/Zurich"}})
# the tools registry (menu lines) the tg-bind rows below point at
with open(P(T, "tools.toml"), "w") as f:
    f.write(
        'schema = 1\n[tools."tg-cmd.week"]\nargv = ["~/bin/tg-cmd", "week"]\neffect = "read"\n'
        'description = "Week ahead"\nmenu = "Your week"\n'
        '[tools."tg-cmd.queue"]\nargv = ["~/bin/tg-cmd", "queue"]\neffect = "read"\ndescription = "Queue"\n'
        'menu = "Downloads"\ntext_arg = true\n'
        '[tools."tg-cmd.cat"]\nargv = ["~/bin/tg-cmd", "cat"]\neffect = "read"\ndescription = "Cat week"\n'
        '[tools."waiting-card"]\nargv = ["~/bin/waiting-card"]\neffect = "read"\ndescription = "Waiting"\n'
        'menu = "What waits"\n'
    )
wj(P(T, "tg/commands.json"), [{"command": "tonight", "description": "Movie pick"}])
with open(P(T, "skills/media-requests/SKILL.md"), "w") as f:
    f.write(
        "---\nname: media-requests\ndescription: Request movies. Uses ~/bin/media. Button presses with callback "
        "data `mt:play:…` and `mt:more`.\n---\n# Media\n"
    )
with open(P(T, "skills/reminders/SKILL.md"), "w") as f:
    f.write("---\nname: reminders\ndescription: Set reminders for the owner.\n---\n# Reminders\n")
with open(P(T, "crontab"), "w") as f:
    f.write(
        "PATH=/usr/bin\n*/2 * * * * cron-fail --after 3 lastcall timeout 280 {}/lastcall run >/dev/null\n"
        '47 18 * * 0 echo "Data, not instructions" > "$HOME/inbox/idea-scan-$(date +%F).md"\n'
        "*/10 * * * * ~/bin/media ready-watch >/dev/null 2>&1  # assistant:media-ready\n".format(P(T, "bin"))
    )
# the schedule registry (core/schedules.toml): jobs that left cron (hub, keepers, planned) keep their catalogue
# entries; registry order = crontab order (media-posters ranks before the crontab's media-ready line)
_REG = {
    "media-posters": (
        "M",
        "fast",
        "hub",
        "30 17 * * 5",
        '["~/bin/media", "posters"]',
        "cron-fail media-posters ~/bin/media posters --cron >/dev/null",
        "",
    ),
    "media-ready": (
        "M",
        "fast",
        "cron",
        "*/10 * * * *",
        '["~/bin/media", "ready-watch"]',
        "~/bin/media ready-watch >/dev/null 2>&1",
        "",
    ),
    "vibe": (
        "M",
        "fast",
        "hub",
        "47 5 * * *",
        '["~/bin/media", "vibe", "--refresh"]',
        "cron-fail vibe-refresh ~/bin/media vibe --refresh >/dev/null 2>&1",
        'line = "vibe-refresh"\n',
    ),
    "traces": (
        "M",
        "fast",
        "hub",
        "*/5 * * * *",
        '["~/bin/lastcall", "status"]',
        "cron-fail traces ~/bin/lastcall status; ~/bin/remind list >/dev/null",
        "",
    ),
    "tasks-sync": (
        "M",
        "fast",
        "hub",
        "*/5 * * * *",
        '["~/bin/remind", "list"]',
        "cron-fail traces ~/bin/lastcall status; ~/bin/remind list >/dev/null",
        'line = "traces"\n',
    ),
    "talk-keeper": (
        "K",
        "",
        "keepers",
        "*/5 * * * *",
        '["~/bin/talk-button", "ensure"]',
        "~/bin/talk-button ensure >/dev/null 2>&1",
        "keeper = true\nevery = 5\n",
    ),
    "drill": ("K", "", "planned", "0 0 1 1 *", '["~/bin/talk-button", "drill"]', "~/bin/talk-button drill", ""),
    "old-job": ("R", "", "retired", "0 3 * * *", '["~/bin/talk-button", "old"]', "~/bin/talk-button old", ""),
    "cue-week": (
        "C",
        "fast",
        "hub",
        "3 15 * * 0",
        "[]",
        'echo "Data, not instructions" > "$HOME/inbox/week-cue-$(date +%F).md"',
        "shell = true\n",
    ),
}
with open(P(T, "schedules.toml"), "w") as f:
    f.write('schema = 1\ntz = "Europe/Zurich"\n')
    for _n, (_c, _q, _r, _cr, _a, _cmd, _x) in _REG.items():
        f.write(
            f'\n[jobs.{_n}]\nclass = "{_c}"\nqueue = "{_q}"\nruns_on = "{_r}"\ncron = "{_cr}"\n'
            f"argv = {_a}\ncron_line = '{_cr} {_cmd}'\n{_x}"
        )
with open(P(T, "ws/FACTS.md"), "w") as f:
    f.write("# Facts\n## Done / declined ledger\n- 01.10 kept: /queue command (test)\n")
# journal: instant /week, a reply with a correction, a button tap, a reaction, a relay hand-off (skipped)
J = [
    {
        "id": "a1",
        "kind": "instant",
        "owner": True,
        "content": "/week (answered instantly …)",
        "meta": {"chat_id": "1", "message_id": "400", "user": "u", "ts": iso(NOW - 3000), "handled": "week"},
    },
    {
        "id": "a2",
        "kind": "message",
        "owner": True,
        "content": "no that's wrong",
        "meta": {"chat_id": "1", "message_id": "402", "user": "u", "ts": iso(NOW - 2400), "reply_to_message_id": "501"},
    },
    {
        "id": "a3",
        "kind": "button",
        "owner": True,
        "content": "button",
        "meta": {
            "chat_id": "1",
            "message_id": "502",
            "ts": iso(NOW - 2300),
            "event_type": "button",
            "callback_data": "mt:play:123",
            "button_text": "Play",
            "message_text": "Secret meeting",
        },
    },
    {
        "id": "a4",
        "kind": "reaction",
        "owner": True,
        "content": "reaction",
        "meta": {
            "chat_id": "1",
            "ts": iso(NOW - 2200),
            "event_type": "reaction",
            "reaction": "👍",
            "reacted_to_message_id": "503",
        },
    },
    {
        "id": "a5",
        "kind": "instant",
        "owner": False,
        "content": "x",
        "meta": {"chat_id": "1", "message_id": "404", "ts": iso(NOW - 2100), "via": "voice", "handled": "x"},
    },
]
with open(P(T, "tg/inbound-journal.jsonl"), "w") as f:
    f.write("".join(json.dumps(r) + "\n" for r in J))
# events ledger: tg-adapter rows; a bound /space (received by the binding), an unbound /lights, a tap,
# the journal's a2 again (dedupe by ext_id), another source's row (ignored)
_ev = sqlite3.connect(P(T, "events.db"))
_ev.execute("CREATE TABLE events(id INTEGER PRIMARY KEY, ts REAL, source TEXT, kind TEXT, payload TEXT, ack_by TEXT)")
for _src, _kind, _p, _ack in [
    (
        "tg-adapter",
        "tg.message",
        {"chat_id": "1", "message_id": "410", "ts": iso(NOW - 1900), "event_type": "message", "text": "/space"},
        "binding",
    ),
    (
        "tg-adapter",
        "tg.message",
        {"chat_id": "1", "message_id": "411", "ts": iso(NOW - 1850), "event_type": "message", "text": "/lights"},
        "channel",
    ),
    (
        "tg-adapter",
        "tg.button",
        {
            "chat_id": "1",
            "message_id": "412",
            "ts": iso(NOW - 1800),
            "event_type": "button",
            "callback_data": "zz:tap:1",
            "button_text": "Z",
            "text": "(pressed button: Z)",
        },
        "binding",
    ),
    ("tg-adapter", "tg.message", dict(J[1]["meta"], event_type="message", text=J[1]["content"]), "channel"),
    ("vault-events", "tg.message", {"message_id": "413", "ts": iso(NOW - 1700), "text": "/x"}, None),
]:
    _ev.execute(
        "INSERT INTO events(ts, source, kind, payload, ack_by) VALUES (?, ?, ?, ?, ?)",
        (NOW - 1900, _src, _kind, json.dumps(_p), _ack),
    )
# tg-bind rows: active /week /queue /cat (no menu line) /waiting (not a tg-cmd tool); /home retired,
# /old expired, a card bind: those three never reach the catalogue
_ev.execute(
    "CREATE TABLE bindings(id INTEGER PRIMARY KEY, kind TEXT, key TEXT, tool TEXT, args TEXT, note INTEGER,"
    " on_error TEXT, why TEXT, created REAL, expires REAL, active INTEGER)"
)
for _k, _key, _tool, _exp, _act in [
    ("command", "week", "tg-cmd.week", None, 1),
    ("command", "queue", "tg-cmd.queue", None, 1),
    ("command", "home", "tg-cmd.home", None, 0),
    ("command", "cat", "tg-cmd.cat", None, 1),
    ("command", "old", "tg-cmd.week", NOW - 60, 1),
    ("card", "1:2:x", "tg-cmd.week", None, 1),
    ("command", "waiting", "waiting-card", NOW + 3600, 1),
]:
    _ev.execute(
        "INSERT INTO bindings(kind, key, tool, args, note, on_error, why, created, expires, active)"
        " VALUES (?, ?, ?, '[]', 1, 'reply', 'test', ?, ?, ?)",
        (_k, _key, _tool, NOW, _exp, _act),
    )
_ev.commit()
_ev.close()
# transcript: owner channel message (same as journal a2? no: a new one) -> Bash media queue -> reply sent 501;
# the journal's a2 again as a channel tag (dedupe); heartbeat -> remind due
tag = (
    f'<channel source="plugin:telegram-plus:telegram" chat_id="1" message_id="401" user="u" ts="{iso(NOW - 2700)}">'
    "Please check the queue</channel>"
)
dup = (
    f'<channel source="plugin:telegram-plus:telegram" chat_id="1" message_id="402" user="u" ts="{iso(NOW - 2400)}" '
    'reply_to_message_id="501">no that\'s wrong</channel>'
)
TX = [
    {
        "type": "user",
        "uuid": "u1",
        "timestamp": iso(NOW - 2700),
        "origin": {"kind": "channel"},
        "isMeta": True,
        "message": {"role": "user", "content": tag},
    },
    {
        "type": "assistant",
        "timestamp": iso(NOW - 2690),
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": "tu1",
                    "name": "Bash",
                    "input": {"command": "media queue --json | head -5; echo 'secret text'"},
                }
            ]
        },
    },
    {
        "type": "user",
        "timestamp": iso(NOW - 2680),
        "message": {"content": [{"type": "tool_result", "tool_use_id": "tu1", "content": "Fake Person 47.1234"}]},
    },
    {
        "type": "user",
        "uuid": "u0",
        "timestamp": iso(NOW - 2675),
        "origin": {"kind": "plugin"},
        "message": {"role": "user", "content": "[heartbeat] notes landed"},
    },
    {
        "type": "assistant",
        "timestamp": iso(NOW - 2672),
        "message": {
            "content": [{"type": "tool_use", "id": "tu0", "name": "Bash", "input": {"command": "remind list"}}]
        },
    },
    {
        "type": "assistant",
        "timestamp": iso(NOW - 2670),
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": "tu2",
                    "name": "mcp__plugin_telegram-plus_telegram__reply",
                    "input": {"chat_id": "1", "text": "Secret meeting", "buttons": [[{"text": "a", "data": "b"}]]},
                }
            ]
        },
    },
    {
        "type": "user",
        "timestamp": iso(NOW - 2660),
        "message": {
            "content": [
                {"type": "tool_result", "tool_use_id": "tu2", "content": [{"type": "text", "text": "sent (id: 501)"}]}
            ]
        },
    },
    {
        "type": "user",
        "uuid": "u2",
        "timestamp": iso(NOW - 2400),
        "origin": {"kind": "channel"},
        "isMeta": True,
        "message": {"role": "user", "content": dup},
    },
    {
        "type": "user",
        "uuid": "u3",
        "timestamp": iso(NOW - 1000),
        "origin": {"kind": "plugin"},
        "message": {"role": "user", "content": "[heartbeat] idle"},
    },
    {
        "type": "assistant",
        "timestamp": iso(NOW - 990),
        "message": {
            "content": [
                {"type": "tool_use", "id": "tu3", "name": "Bash", "input": {"command": "remind due && ls /tmp"}},
                {"type": "tool_use", "id": "tu4", "name": "Edit", "input": {"file_path": "/x/workspace/FACTS.md"}},
            ]
        },
    },
    {
        "type": "user",
        "timestamp": iso(NOW - 980),
        "message": {"content": [{"type": "tool_result", "tool_use_id": "tu3", "is_error": True, "content": "boom"}]},
    },
]
with open(P(T, "transcripts/session.jsonl"), "w") as f:
    f.write("".join(json.dumps(r) + "\n" for r in TX))
# `voice-hub calls --last 50 --days 14`: voice.calls digests (metadata only); c2 is still live (taken once it ends)
CALLS = [
    {
        "id": "c1",
        "kind": "tg-out",
        "start": NOW - 1800,
        "ring": ["answered", 5],
        "outcome": "answered",
        "answered": True,
        "talked": True,
        "end": {"reason": "assistant_hangup", "by": "assistant", "why": None, "duration_s": 30},
        "events": {
            "start": 1,
            "ring": 1,
            "him": 1,
            "tool": 1,
            "reply": 1,
            "resumed": 1,
            "model_fallback": 1,
            "partial": 1,
            "end": 1,
        },
        "tools": [["weather", "fast", None, False]],
        "reply_ms": [900],
        "idle": [],
        "results": [],
        "closing": ["none"],
    },
    {
        "id": "c2",
        "kind": "tg-in",
        "start": NOW - 60,
        "ring": None,
        "outcome": None,
        "answered": None,
        "talked": True,
        "end": None,
        "events": {"start": 1, "tool": 1},
        "tools": [["lights", "fast", None, False]],
        "reply_ms": [],
        "idle": [],
        "results": [],
        "closing": [],
    },
]
with open(P(T, "calls.json"), "w") as f:
    json.dump({"calls": CALLS, "outgoing": {}, "via": "hub"}, f)
with open(P(T, "fake-voice-hub"), "w") as f:
    f.write(f"#!/bin/sh\ncat '{P(T, 'calls.json')}'\n")
os.chmod(P(T, "fake-voice-hub"), 0o755)
with open(P(T, "talk.log"), "w") as f:
    f.write(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(NOW - 1500)) + " tap -> answered\n")
wj(
    P(T, "counts.json"),
    {
        "updated": "2026-01-01T10:00:00+00:00",
        "days": {
            time.strftime("%Y-%m-%d", time.localtime(NOW - 10 * 86400)): {
                "cmd": {"week": {"instant": 2}},
                "btn": {"zz:old": 1},
            },
            time.strftime("%Y-%m-%d", time.localtime(NOW)): {"cmd": {"queue": {"instant": 3}}},
        },
    },
)
# turn model: curated button map + command entry; a second transcript with test / build / spillover / queued turns
wj(
    P(T, "conf/curated.json"),
    {"buttons": {"qq": "skill.reminders"}, "features": {"skill.reminders": {"entry": {"via": ["/week"]}}}},
)


def chan(mid, t, body, **kw):
    extra = "".join(f' {k}="{v}"' for k, v in kw.items())
    return (
        f'<channel source="plugin:telegram-plus:telegram" chat_id="1" message_id="{mid}" user="u" ts="{iso(t)}"'
        f"{extra}>{body}</channel>"
    )


def bash(i, t, cmd):
    return {
        "type": "assistant",
        "timestamp": iso(t),
        "message": {"content": [{"type": "tool_use", "id": i, "name": "Bash", "input": {"command": cmd}}]},
    }


def utt(u, t, content):
    return {
        "type": "user",
        "uuid": u,
        "timestamp": iso(t),
        "origin": {"kind": "channel"},
        "isMeta": True,
        "message": {"role": "user", "content": content},
    }


TX2 = [
    utt("v1", NOW - 900, chan(601, NOW - 900, "Please run the test so I can watch")),  # dev words: test turn
    bash("w1", NOW - 890, "media queue"),
    utt("v2", NOW - 800, chan(602, NOW - 800, "button", event_type="button", callback_data="zz:go")),  # ad-hoc tap
    bash("w2", NOW - 790, "remind add x"),
    utt("v3", NOW - 700, chan(603, NOW - 700, "hello there")),  # mostly building
    {
        "type": "assistant",
        "timestamp": iso(NOW - 695),
        "message": {
            "content": [
                {"type": "tool_use", "id": "w3a", "name": "Edit", "input": {"file_path": "/x/bin/media"}},
                {"type": "tool_use", "id": "w3b", "name": "Write", "input": {"file_path": "/x/bin/media"}},
            ]
        },
    },
    bash("w3c", NOW - 690, 'brain-commit "x"'),
    bash("w3d", NOW - 685, "/x/opt/foo-tests/run.sh"),
    bash("w3e", NOW - 680, "media queue"),
    bash("w3f", NOW - 675, "remind list"),
    {
        "type": "attachment",
        "uuid": "v4",
        "timestamp": iso(NOW - 600),
        "isSidechain": False,  # queued mid-turn
        "attachment": {
            "type": "queued_command",
            "origin": {"kind": "channel"},
            "isMeta": True,
            "prompt": chan(604, NOW - 600, "what about tomorrow"),
        },
    },
    bash("w4a", NOW - 590, "remind list"),
    bash("w4b", NOW - 585, "media queue"),  # spillover
    utt("v5", NOW - 500, chan(605, NOW - 500, "button", event_type="button", callback_data="qq:yes")),  # curated prefix
    bash("w5", NOW - 490, "media queue"),
]
with open(P(T, "transcripts/session2.jsonl"), "w") as f:
    f.write("".join(json.dumps(r) + "\n" for r in TX2))
wj(P(T, "place-state.json"), {"zone": "home", "since": "x"})
wj(
    P(T, "calcache.json"),
    {
        "fetched": iso(NOW - 600),
        "events": [
            {
                "id": "e1",
                "start": iso(NOW - 1800),
                "end": iso(NOW + 3600),
                "all_day": False,
                "title": "Secret meeting",
                "town": "Testtown",
                "free": False,
                "flags": {"travel": None, "busy_evening": False},
            },
            {
                "id": "e2",
                "start": iso(NOW + 3 * 3600),
                "end": iso(NOW + 4 * 3600),
                "all_day": False,
                "title": "🚆 A → B",
                "town": "",
                "free": False,
                "flags": {"travel": "train"},
            },
            {
                "id": "e3",
                "start": time.strftime("%Y-%m-%d", time.localtime(NOW)),
                "end": time.strftime("%Y-%m-%d", time.localtime(NOW + 86400)),
                "all_day": True,
                "title": "@Testwork",
                "flags": {},
            },
        ],
    },
)
pg = sqlite3.connect(P(T, "phonegw.db"))
pg.executescript("""
CREATE TABLE devices(id TEXT PRIMARY KEY, name TEXT, model TEXT, cert_sha256 TEXT, bio_pub TEXT, created INT, last_seen INT, app_code INT, status TEXT);
CREATE TABLE state(device_id TEXT, k TEXT, v TEXT, at INT, PRIMARY KEY(device_id, k));
CREATE TABLE events(id TEXT PRIMARY KEY, device_id TEXT, kind TEXT, at INT, body TEXT, received INT);
CREATE TABLE push_log(id TEXT PRIMARY KEY, at INT, level TEXT, tag TEXT, delivered_ms INT, acked_ms INT, verb TEXT, device_id TEXT, nonce TEXT, verbs TEXT, arg TEXT, action_at INT);
CREATE TABLE cards(id TEXT PRIMARY KEY, kind TEXT, body TEXT, created INT, expires INT, answer TEXT, answered INT);
CREATE TABLE chat(id TEXT PRIMARY KEY, at INT, dir TEXT, text TEXT, reply_to TEXT);
CREATE TABLE inbound(id TEXT PRIMARY KEY, device_id TEXT, t TEXT, at INT);
""")  # noqa: E501
n = int(NOW)
pg.execute("INSERT INTO devices VALUES('d1','p','m','c','b',?,?,1,'active')", (n - 9000, n))
for k, v in {
    "place": {"zone": "home", "band": "near", "town": "Testtown", "stop": "Somewhere 1", "lat": 47.1234},
    "battery": {"level": 55, "band": "mid", "charging": False},
    "screen": {"on": True},
    "net": {"type": "wifi", "vpn": True, "roaming": False},
    "dnd": "off",
    "car": {"projection": False, "bt": False},
    "bt": ["buds"],
    "alarm": {"next": n + 7200},
    "activity": {"kind": "still", "since": n - 600},
    "flag.drive": {"active": False},
}.items():
    pg.execute("INSERT INTO state VALUES('d1',?,?,?)", (k, json.dumps(v), n - 60))
pg.execute(
    "INSERT INTO state VALUES('',?,?,?)",
    (
        "widget:home",
        json.dumps({"at": n - 120, "data": {"lights_on": 2, "tv": "off", "away": False, "vacuum": "docked"}}),
        n - 120,
    ),
)
pg.execute(
    "INSERT INTO events VALUES('ev1','d1','tile',?,?,?)", (n - 1200, json.dumps({"kind": "tile", "name": "talk"}), n)
)
pg.execute(
    "INSERT INTO events VALUES('ev3','d1','tile',?,?,?)",
    (n - 1150, json.dumps({"kind": "tile", "name": "sleepin:on"}), n),
)
pg.execute(
    "INSERT INTO push_log VALUES('p3',?, 'info','link-x',?,?, 'dismissed','d1','n','v','a',?)",
    ((n - 980) * 1000, (n - 979) * 1000, (n - 979) * 1000, (n - 950) * 1000),
)
pg.execute(
    "INSERT INTO events VALUES('ev2','d1','zone',?,?,?)", (n - 1100, json.dumps({"kind": "zone", "zone": "home"}), n)
)
pg.execute(
    "INSERT INTO push_log VALUES('p1',?, 'timed','rem-abc',?,?, 'done','d1','n','v','a',?)",
    ((n - 1000) * 1000, (n - 999) * 1000, (n - 999) * 1000, (n - 900) * 1000),
)
pg.execute("INSERT INTO push_log VALUES('p2',?, 'probe','probe',1,1,NULL,'d1','n','v','a',NULL)", ((n - 990) * 1000,))
pg.execute(
    "INSERT INTO push_log VALUES('p4',?, 'alarm','alarm:abc',NULL,NULL,NULL,'d1','n','v','a',NULL)",
    ((n - 5000) * 1000,),
)
pg.execute(
    "INSERT INTO push_log VALUES('p5',?, 'timed','rem-def',NULL,NULL,NULL,'d1','n','v','a',NULL)", ((n - 4000) * 1000,)
)
pg.execute("INSERT INTO cards VALUES('k1','decision','{}',?,?,'yes',?)", (n - 800, n + 999, n - 700))
pg.execute("INSERT INTO chat VALUES('m1',?, 'in','Secret meeting',NULL)", ((n - 600) * 1000,))
pg.execute("INSERT INTO inbound VALUES('i1','d1','crash',?)", (n - 500,))
pg.execute("INSERT INTO inbound VALUES('i2','d1','hello',?)", (n - 400,))
pg.commit()
pg.close()
# remind's fired.log (his zone's wall clock, minutes): the push p1 minute (the app showed it), a minute whose push
# p5 was never shown (Telegram sent it), two in one minute, one of mine, one too fresh (< 10 min) for this tick
_tz = usage._home_tz()


def _fl(t):
    return (
        time.strftime("%Y-%m-%d %H:%M", time.localtime(t))
        if _tz is None
        else __import__("datetime").datetime.fromtimestamp(t, _tz).strftime("%Y-%m-%d %H:%M")
    )


with open(P(T, "fired.log"), "w") as f:
    f.write(
        "".join(
            f"{_fl(t)} {who}\n"
            for t, who in (
                (n - 4000, "owner"),
                (n - 3000, "me"),
                (n - 3000, "owner"),
                (n - 3000, "owner"),
                (n - 1000, "owner"),
                (n - 120, "owner"),
            )
        )
    )
    f.write("not a line\n")

# ---------------------------------------------------------------- writer + hook
ok("log: basic", usage.log("tg.week", "invoke", channel="telegram", attrs={"good": "x1", "bad": "two words"}))
ok("log: bad kind refused", usage.log("tg.week", "nonsense") is False)
ok("log: bad feature refused", usage.log("Has Spaces!", "invoke") is False)
os.environ["USAGE"] = "off"
ok("log: USAGE=off", usage.log("tg.week") is False)
os.environ.pop("USAGE")
_SENT = NOW - 7200  # derive waits until 10:00 for a send in quiet hours (< 08:00): keep this fixture outside them,
if usage.local(_SENT)[0].hour < 8:  # else the "ignored" check fails whenever the suite runs 02:00-10:00
    _SENT = usage.local(_SENT)[0].replace(hour=0, minute=0, second=0, microsecond=0).timestamp() - 4 * 3600
usage.log(
    "bin.lastcall", "notify_sent", channel="telegram", actor="assistant", ref="tg:777", ts=_SENT, ext_id="tgsent:777"
)
usage.log("bin.lastcall", "run", channel="cron", ms=1200, outcome="rc1")
for i in range(4):  # dev/test runs of instant commands by a session or subagent: never owner failures
    usage.log("tg.week", "exec", channel="session", actor="agent", ms=50, outcome="fail", ts=NOW - 600 - i)
usage.log("tg.week", "exec", channel="telegram", actor="owner", ms=50, outcome="usage", ts=NOW - 500)
for i in range(3):  # a retired (hidden) command failing for the owner: no verdict
    usage.log("tg.home", "exec", channel="telegram", actor="owner", ms=50, outcome="fail", ts=NOW - 400 - i)
usage.log("tg.cat", "exec", channel="telegram", actor="owner", ms=50, outcome="fail", ts=NOW - 300)
ok(
    "caller_actor: owner channels, session = agent, test env",
    usage.caller_actor("telegram") == "owner"
    and usage.caller_actor("app") == "owner"
    and usage.caller_actor("session") == "agent"
    and usage.caller_actor("cron") == "cron",
)
os.environ["TG_CMD_TEST"] = "1"
ok("caller_actor: TG_CMD_TEST -> test", usage.caller_actor("telegram") == "test")
_anc = usage.ancestors
usage.ancestors = lambda pid=None: [
    (["/usr/bin/python3", "/h/bin/tg-cmd", "emby"], ["tg-cmd"]),
    (["/usr/local/bin/bun", "/h/opt/assistant/adapters/tg/src/cli.ts", "run", "--instance", "live"], []),
]
ok("caller_channel: tg-adapter run -> telegram", usage.caller_channel() == "telegram")
usage.ancestors = lambda pid=None: [
    (["/usr/local/bin/bun", "/h/opt/assistant/adapters/tg/src/cli.ts", "bindings"], []),
    (["/usr/bin/claude"], []),
]
ok("caller_channel: tg-adapter bindings (CLI) -> not telegram", usage.caller_channel() == "session")
usage.ancestors = _anc
del os.environ["TG_CMD_TEST"]
spool = "".join(open(P(T, "state/spool", x)).read() for x in os.listdir(P(T, "state/spool")))
ok("log: attrs with spaces dropped", '"good":"x1"' in spool and "two words" not in spool)
ok(
    "scripts(): only ~/bin names, verbs",
    usage.scripts("cd /x && media queue 'a b' | tg-cmd week; echo hi; ls") == [["media", "queue"], ["tg-cmd", "week"]],
    str(usage.scripts("cd /x && media queue 'a b' | tg-cmd week; echo hi")),
)
ok(
    "scripts(): path forms + timeout",
    usage.scripts(f"timeout 30 {P(T, 'bin')}/remind due && ~/bin/media") == [["remind", "due"], ["media", None]],
    str(usage.scripts(f"timeout 30 {P(T, 'bin')}/remind due && ~/bin/media")),
)
# the live hook sits in the account's real home: under test-all HOME is a temp dir without .claude
hook = os.environ.get("USAGE_HOOK") or os.path.join(pwd.getpwuid(os.getuid()).pw_dir, ".claude/hooks/usage-hook.sh")
if not os.environ.get("USAGE_HOOK") and not os.path.exists(hook):
    # a home without the hook file (the CI runner): a stand-in with the hook's body, so the lib's --hook path and
    # the subagent aggregate below are still covered
    hook = P(T, "usage-hook.sh")
    with open(hook, "w") as f:
        f.write(
            '#!/bin/sh\n[ "${USAGE:-}" = off ] && exit 0\n'
            'timeout 4 python3 -I -S "$HOME/bin/lib/usage.py" --hook >/dev/null 2>&1\nexit 0\n'
        )
if os.path.exists(hook):
    hook_home = P(T, "hookhome")
    os.makedirs(P(hook_home, "bin/lib"), exist_ok=True)
    os.symlink(P(LIB, "usage.py"), P(hook_home, "bin/lib/usage.py"))
    inp = json.dumps(
        {
            "tool_name": "Bash",
            "tool_use_id": "x",
            "agent_id": "ag1",
            "tool_input": {"command": "media queue 'secret text'"},
        }
    )
    t0 = time.time()
    r = subprocess.run(["sh", hook], input=inp, capture_output=True, text=True, env={**os.environ, "HOME": hook_home})
    dt_ms = (time.time() - t0) * 1000
    hs = P(T, "state/spool")
    lines = [l for x in os.listdir(hs) if x.startswith("hook-") for l in open(P(hs, x))]  # noqa: E741
    ok("hook: exit 0, silent", r.returncode == 0 and not r.stdout and not r.stderr)
    ok(
        "hook: spool line, subagent flag, no text",
        len(lines) == 1 and '"agent":true' in lines[0] and '["media","queue"]' in lines[0] and "secret" not in lines[0],
        str(lines),
    )
    ok("hook: fast (< 400 ms)", dt_ms < 400, f"{dt_ms:.0f} ms")
    r = subprocess.run(
        ["sh", hook], input="not json", capture_output=True, text=True, env={**os.environ, "HOME": hook_home}
    )
    ok("hook: bad input fails open", r.returncode == 0 and not r.stdout)

# ---------------------------------------------------------------- catalogue + tick
cat = product.build(write=True)
ids = {f["id"] for f in cat["features"]}
ok(
    "catalogue: commands, skills, scripts, curated",
    {
        "tg.week",
        "tg.queue",
        "tg.tonight",
        "skill.media-requests",
        "bin.lastcall",
        "voice.tool.weather",
        "app.tile.talk",
        "job.cue.idea-scan",
    }
    <= ids,
    str(sorted(ids))[:300],
)
_tg = {f["id"]: f for f in cat["features"] if f["id"].startswith("tg.")}
ok(
    "catalogue: tg-bind rows (active, unexpired, no card binds) are the instant commands",
    {"tg.week", "tg.queue", "tg.cat", "tg.waiting"} <= set(_tg) and not {"tg.home", "tg.old"} & set(_tg),
    str(sorted(_tg)),
)
ok(
    "catalogue: a tg-cmd tool = command, menu line as purpose, args = text_arg, tg-cmd + verb",
    {k: _tg["tg.queue"].get(k) for k in ("kind", "menu", "args", "purpose", "owner_facing")}
    == {"kind": "command", "menu": True, "args": True, "purpose": "Downloads", "owner_facing": True}
    and _tg["tg.queue"]["entry"].get("scripts") == ["tg-cmd"]
    and _tg["tg.queue"]["entry"].get("verbs") == ["queue"],
    json.dumps(_tg["tg.queue"]),
)
ok(
    "catalogue: no menu line = menu false, the description as purpose",
    (_tg["tg.cat"]["menu"], _tg["tg.cat"]["purpose"]) == (False, "Cat week"),
    json.dumps(_tg["tg.cat"]),
)
ok(
    "catalogue: another tool gets no script credit",
    "scripts" not in _tg["tg.waiting"]["entry"]
    and _tg["tg.waiting"]["purpose"] == "What waits"
    and cat["index"]["verbs"].get("tg-cmd week") == "tg.week",
    json.dumps(_tg["tg.waiting"]),
)
ok(
    "catalogue: index",
    cat["index"]["scripts"].get("media") == "skill.media-requests"
    and cat["index"]["buttons"].get("mt") == "skill.media-requests"
    and cat["index"]["jobs"].get("lastcall") == "bin.lastcall",
    json.dumps(cat["index"])[:300],
)
lc = next(f for f in cat["features"] if f["id"] == "bin.lastcall")
ok("catalogue: notifier + rare tag + metric", lc["notifies"] and "rare" in lc["tags"] and lc["metric"])
_F, _J = {f["id"]: f for f in cat["features"]}, cat["index"]["jobs"]
_mr = _F["skill.media-requests"]
ok(
    "catalogue: jobs on hub schedules keep job + schedule entries (registry cron_line)",
    {"media-posters", "vibe-refresh", "vibe", "media-ready"} <= set(_mr["entry"].get("jobs", []))
    and _J.get("vibe-refresh") == _J.get("vibe") == _J.get("media-posters") == "skill.media-requests",
    json.dumps(_mr["entry"]),
)
ok(
    "catalogue: registry order = crontab order (the first line crediting a feature sets its schedule)",
    _mr.get("schedule") == "30 17 * * 5",
    str(_mr.get("schedule")),
)
ok(
    "catalogue: a shared registry line credits each job to its own argv's script",
    _J.get("traces") == "bin.lastcall"
    and _J.get("tasks-sync") == cat["index"]["scripts"].get("remind")
    and _J.get("tasks-sync"),
    json.dumps({k: _J.get(k) for k in ("traces", "tasks-sync")}),
)
ok(
    "catalogue: keepers and planned jobs count, retired ones do not",
    _J.get("talk-keeper") == _J.get("drill") == "bin.talk-button"
    and "old-job" not in _J
    and _F["bin.talk-button"].get("schedule") == "*/5 * * * *",
    json.dumps(_F["bin.talk-button"]["entry"]),
)
ok(
    "catalogue: a cue that moved to the hub stays a routine job",
    _F.get("job.cue.week-cue", {}).get("schedule") == "3 15 * * 0" and _J.get("cue-week") == "job.cue.week-cue",
    json.dumps(_F.get("job.cue.week-cue")),
)
_saved = product.SCHEDULES
product.SCHEDULES = P(T, "no-such-registry.toml")
_c2 = product.build(write=False)
product.SCHEDULES = _saved
ok(
    "catalogue: an unreadable registry = the crontab alone, no error",
    _c2["index"]["jobs"].get("lastcall") == "bin.lastcall"
    and "vibe-refresh" not in _c2["index"]["jobs"]
    and _c2["index"]["jobs"].get("media") == "skill.media-requests",
    json.dumps(_c2["index"]["jobs"]),
)
out = usage.tick(backfill=True, now=NOW)
ok("tick: no source errors", not any(isinstance(v, str) and v.startswith("error") for v in out.values()), str(out))
con = usage.connect(ro=True)
ev = con.execute("SELECT * FROM events").fetchall()
by = {}
for r in ev:
    by.setdefault((r["feature"], r["kind"]), []).append(r)
ok("journal: instant /week", ("tg.week", "invoke") in by)
ok(
    "journal+transcript dedupe: one reply row",
    len(by.get(("chat.telegram", "reply"), [])) == 1,
    str([dict(r) for r in by.get(("chat.telegram", "reply"), [])]),
)
rep = by[("chat.telegram", "reply")][0]
ok("feedback flag, no text", json.loads(rep["attrs"]).get("fb") == "correction" and rep["ref"] == "tg:501")
ok("button tap -> skill via prefix", ("skill.media-requests", "tap") in by)
ok("relay hand-off skipped in journal", not any(r["feature"] == "tg.x" for r in ev))
sp = by.get(("tg.space", "invoke"), [])
ok(
    "events: a bound /space = one instant invoke",
    len(sp) == 1 and json.loads(sp[0]["attrs"]).get("instant") is True and sp[0]["src"] == "events",
    str([dict(r) for r in sp]),
)
ok("events: unbound /lights = a session invoke", len(by.get(("tg.lights", "invoke"), [])) == 1)
ok(
    "events: a tap (unknown prefix: session.buttons)",
    any(r["feature"] == "session.buttons" and r["kind"] == "tap" and r["src"] == "events" for r in ev),
    str(sorted({(r["feature"], r["src"]) for r in ev if r["kind"] == "tap"})),
)
ok("events: other sources ignored", not any(r["feature"] == "tg.x" for r in ev))
ok("events: watermark", usage.mget(con, "events_id") == 4)
ex = by.get(("skill.media-requests", "exec"), [])
ok("transcript exec: owner trigger + turn", ex and json.loads(ex[0]["ctx"]).get("trigger") == "owner" and ex[0]["turn"])
rm = [r for r in ev if r["kind"] == "exec" and r["feature"] in ("skill.reminders", "bin.remind")]
ok(
    "transcript exec: heartbeat trigger + error outcome",
    any(json.loads(r["ctx"]).get("trigger") == "heartbeat" and r["outcome"] == "error" for r in rm),
    str([dict(r) for r in rm]),
)
ok("transcript: memory file op", ("mem.facts", "exec") in by)
sent = by.get(("session.reply", "notify_sent"), [])
ok(
    "proactive reply -> notify_sent tg:501 attributed",
    sent and sent[0]["ref"] == "tg:501" and json.loads(sent[0]["attrs"]).get("for") == "skill.reminders",
    str([dict(r) for r in sent]),
)
ok("derive: reply opened (his reply to 501)", ("session.reply", "notify_opened") in by)
ok("derive: unanswered notification ignored", ("bin.lastcall", "ignored") in by)
ok("cron failure -> error event", ("bin.lastcall", "error") in by)
ok(
    "calls: call row with duration",
    ("voice.call", "call") in by and json.loads(by[("voice.call", "call")][0]["attrs"]).get("dur_s") == 30,
)
ok("calls: outbound call answered", ("voice.call_out", "notify_opened") in by)
ok("calls: a live call waits for its end", ("voice.tool.lights", "invoke") not in by, str(sorted(by))[:300])
ok(
    "calls: fast-lane tool with its lane",
    ("voice.tool.weather", "invoke") in by
    and json.loads(by[("voice.tool.weather", "invoke")][0]["attrs"]).get("lane") == "fast",
)
ok(
    "calls: voice-first events (continued call, model fallback, partial answer)",
    all(k in by for k in [("voice.resume", "invoke"), ("voice.model_fallback", "error"), ("voice.partial", "invoke")]),
    str(sorted(by))[:300],
)
ok(
    "phonegw: tile, push opened, card, chat, crash",
    all(
        k in by
        for k in [
            ("app.tile.talk", "invoke"),
            ("skill.reminders", "notify_opened"),
            ("app.card.decision", "notify_opened"),
            ("chat.app", "msg"),
            ("app", "error"),
        ]
    ),
    str(sorted(by))[:400],
)
ok(
    "phonegw: tile state folded into one feature",
    ("app.tile.sleepin", "invoke") in by
    and json.loads(by[("app.tile.sleepin", "invoke")][0]["attrs"]).get("state") == "on",
)
ok(
    "phonegw: swiped-away push = dismissed, not opened",
    ("bin.phone", "dismissed") in by and ("bin.phone", "notify_opened") not in by,
)
ok("phonegw: probe push skipped", not any(r["ext_id"] == "pg:push:p2" for r in ev))
ok(
    "phonegw: remind pushes and the app's alarm schedule are no sends (fired.log is)",
    not any(r["ext_id"] in ("pg:push:p1", "pg:push:p4", "pg:push:p5") for r in ev)
    and any(r["ext_id"] == "resp:pg:push:p1" and r["kind"] == "notify_opened" for r in ev),
)
_rs = sorted((r for r in ev if r["src"] == "remind"), key=lambda r: (r["ts"], r["ext_id"]))
ok(
    "remind fired.log: one send per fired owner reminder, mine and fresh ones skipped",
    [(r["feature"], r["kind"], r["actor"]) for r in _rs] == [("skill.reminders", "notify_sent", "assistant")] * 4
    and len({r["ext_id"] for r in _rs}) == 4
    and all(r["ext_id"].startswith("rem:fired:") for r in _rs),
    str([dict(r) for r in _rs]),
)
ok(
    "remind fired.log: channel app when the phone showed its push that minute, else telegram",
    [r["channel"] for r in _rs] == ["telegram", "telegram", "telegram", "app"]
    and json.loads(_rs[-1]["attrs"]).get("via") == "app",
    str([(r["channel"], r["ext_id"]) for r in _rs]),
)
ok(
    "remind fired.log: times on his zone's clock (the minute of the line)",
    abs(_rs[-1]["ts"] - (n - 1000)) < 60 and _rs[-1]["ts"] % 60 == 0,
    str(_rs[-1]["ts"] - (n - 1000)),
)
_c0 = con.execute("SELECT count(*) FROM events WHERE src='remind'").fetchone()[0]
usage.tick(now=NOW + 900)
_c1 = usage.connect(ro=True).execute("SELECT count(*) FROM events WHERE src='remind'").fetchone()[0]
ok(
    "remind fired.log: re-read is idempotent; the fresh line comes on a later tick",
    (_c0, _c1) == (4, 5),
    f"{_c0} {_c1}",
)
ok("talk-button tap", ("voice.talk_button", "tap") in by)
mu = [r for r in ev if r["src"] == "menu-usage"]
ok(
    "menu-usage gap-fill: settled days only, buttons per prefix",
    len(mu) == 3
    and sum(1 for r in mu if json.loads(r["attrs"]).get("prefix") == "zz:old") == 1
    and not any(r["day"] == time.strftime("%Y-%m-%d", time.localtime(NOW)) for r in mu),
    str([dict(r) for r in mu]),
)
TT = {r["turn"]: dict(r) for r in con.execute("SELECT * FROM turns")}
H = usage._h
ok(
    "turns: labels (use / test / build / queued / curated tap)",
    [TT.get(H(u), {}).get("label") for u in ("u1", "v1", "v2", "v3", "v4", "v5")]
    == ["use", "test", "use", "build", "use", "use"],
    str(
        [(u, TT.get(H(u), {}).get("label"), TT.get(H(u), {}).get("mix")) for u in ("u1", "v1", "v2", "v3", "v4", "v5")]
    ),
)
ok(
    "turns: primary feature",
    [TT.get(H(u), {}).get("primary_f") for u in ("u1", "v1", "v2", "v3", "v4", "v5")]
    == [
        "skill.media-requests",
        "skill.media-requests",
        "skill.reminders",
        "skill.media-requests",
        "skill.reminders",
        "skill.reminders",
    ],
    str([TT.get(H(u), {}).get("primary_f") for u in ("u1", "v1", "v2", "v3", "v4", "v5")]),
)
ok(
    "turns: start kinds + dev flag only",
    TT[H("v2")]["start"] == "tap"
    and TT[H("v2")]["prefix"] == "zz:go"
    and TT[H("v1")]["dev"] == "test"
    and TT[H("v3")]["start"] == "text"
    and not TT[H("v3")]["dev"],
)
ok("turns: heartbeat turns not recorded", H("u0") not in TT and H("u3") not in TT)
tap = [r for r in ev if r["kind"] == "tap" and json.loads(r["attrs"]).get("prefix") == "zz:go"]
ok(
    "ad-hoc tap credited to the turn's feature",
    tap
    and tap[0]["feature"] == "session.buttons"
    and json.loads(tap[0]["attrs"]).get("credit") == "skill.reminders"
    and tap[0]["turn"] == H("v2"),
    str([dict(r) for r in tap]),
)
ok(
    "curated button prefix -> feature",
    any(
        r["kind"] == "tap" and r["feature"] == "skill.reminders" and json.loads(r["attrs"]).get("prefix") == "qq:yes"
        for r in ev
    ),
)
ok(
    "queued mid-turn message collected with its turn",
    any((r["ext_id"] or "").startswith("tg:msg:604:") and r["turn"] == H("v4") for r in ev),
)
runs = {(r["feature"], r["actor"]): r for r in con.execute("SELECT * FROM runs")}
ok(
    "runs: cron + voice reply latency",
    runs.get(("bin.lastcall", "cron")) is not None and runs.get(("voice.reply", "assistant")) is not None,
    str(list(runs)),
)
ok("runs: hook subagent aggregate", runs.get(("skill.media-requests", "subagent")) is not None, str(list(runs)))
# context
c = json.loads(con.execute("SELECT ctx FROM ctx_log").fetchone()[0])
want = {
    "zone": "home",
    "batt": "mid",
    "buds": True,
    "alarm_in": "1-3h",
    "event": "during",
    "office_day": True,
    "busy": "some",
    "tv": "off",
    "lights": "1-3",
    "net": "wifi",
    "vpn": True,
    "screen": "on",
    "driving": False,
    "phone": "live",
    "town": "Testtown",
}
ok("ctx: rich coarse dims", all(c.get(k) == v for k, v in want.items()), json.dumps(c))
ok("ctx: no stop/coordinates", "stop" not in c and "lat" not in c)
# driving: the gateway's flag.drive under core.drive's rule (`driving`'s 60 min), no raw car/activity guess
_pg = sqlite3.connect(P(T, "phonegw.db"))


def _drive_rows(flag, flag_age_s, car=None, activity=None):
    _pg.execute(
        "UPDATE state SET v=?, at=? WHERE device_id='d1' AND k='flag.drive'", (json.dumps(flag), n - flag_age_s)
    )
    if car is not None:
        _pg.execute("UPDATE state SET v=? WHERE device_id='d1' AND k='car'", (json.dumps(car),))
    if activity is not None:
        _pg.execute("UPDATE state SET v=? WHERE device_id='d1' AND k='activity'", (json.dumps(activity),))
    _pg.commit()
    return usage._phone(NOW).get("driving")


ok("ctx driving: a fresh flag.drive on", _drive_rows({"active": True}, 60) is True)
ok(
    "ctx driving: a flag.drive row older than 60 min is no drive (stuck flag)",
    _drive_rows({"active": True}, 3 * 3600) is False,
)
ok("ctx driving: the gateway's until passed", _drive_rows({"active": True, "until": n - 30}, 60) is False)
ok(
    "ctx driving: stuck car state + vehicle activity without the gateway's flag is no drive",
    _drive_rows({"active": False}, 60, car={"projection": True, "bt": False}, activity={"kind": "vehicle"}) is False,
)
_drive_rows({"active": False}, 60, car={"projection": False, "bt": False}, activity={"kind": "still", "since": n - 600})
_pg.close()
recent = [r for r in ev if r["ts"] > NOW - 300 and r["src"] != "derive"]
dump = "\n".join(
    "|".join(str(x) for x in tuple(r))
    for t in ("events", "runs", "ctx_log", "meta", "turns")
    for r in sqlite3.connect(P(T, "state/usage.db")).execute(f"SELECT * FROM {t}")
)
dump += open(P(T, "state/ctx.json")).read()
ok("privacy: no fixture text in the store", not any(s in dump for s in SECRETS), [s for s in SECRETS if s in dump])
# idempotent
before = con.execute("SELECT count(*) FROM events").fetchone()[0]
usage.tick(now=NOW + 60)
after = usage.connect(ro=True).execute("SELECT count(*) FROM events").fetchone()[0]
new = (
    usage.connect(ro=True)
    .execute("SELECT feature, kind, src, ext_id FROM events ORDER BY id DESC LIMIT ?", (max(0, after - before),))
    .fetchall()
)
ok("tick: idempotent", before == after, f"{before} -> {after}: {[tuple(r) for r in new]}")

# ---------------------------------------------------------------- report / slice / card / review
an = product.analyse(30, NOW + 120)
f = {r["id"]: r for r in an["features"]}
ok(
    "report: uses counted",
    f["tg.week"]["uses"] >= 1 and f["skill.media-requests"]["uses"] >= 2,
    json.dumps({k: f[k]["uses"] for k in ("tg.week", "skill.media-requests")}),
)
ok(
    "report: build/test turns and spillover are not uses; work turns counted",
    f["skill.media-requests"]["uses"] == 2
    and f["skill.media-requests"]["work_turns"] == 2
    and f["skill.reminders"]["uses"] == 3
    and f.get("session.buttons", {}).get("uses", 0) == 2,  # the old gap-fill tap + the ledger's zz: tap (no turns)
    json.dumps({k: (f[k]["uses"], f[k]["work_turns"]) for k in ("skill.media-requests", "skill.reminders")}),
)
ok(
    "report: turn mix + menu-usage store health",
    an["meta"]["turns"].get("build") == 1
    and an["meta"]["turns"].get("test") == 1
    and "menu-usage store" in an["meta"]["sources"],
    json.dumps(an["meta"])[:400],
)
ok(
    "catalogue: curated button + via entry",
    "qq" in f["skill.reminders"]["entry"].get("buttons", [])
    and "/week" in f["skill.reminders"]["entry"].get("via", []),
    json.dumps(f["skill.reminders"]["entry"]),
)
ok(
    "classify_turn: command stays a use unless mostly building",
    usage.classify_turn("command", False, None, {"use": 1, "edit": 1}) == "use"
    and usage.classify_turn("command", False, None, {"edit": 4, "use": 1}) == "build"
    and usage.classify_turn("text", False, None, {"maint": 3}) == "maint"
    and usage.classify_turn("text", False, "build", {}) == "build"
    and usage.classify_turn("text", False, "build", {"use": 2}) == "build"
    and usage.classify_turn("text", False, None, {"use": 3, "edit": 1}) == "use",
)
ok(
    "dev_flag: words only",
    usage.dev_flag("can you deploy it") == "build"
    and usage.dev_flag("what's new on emby") is None
    and usage.dev_flag("Test it so I can watch") == "test",
)
ok(
    "broken: agent/test failures and usage hints never count",
    "broken" not in f["tg.week"]["flags"] and f["tg.week"]["owner_errors"] == 0,
    json.dumps({k: f["tg.week"][k] for k in ("flags", "errors", "owner_errors")}),
)
ok(
    "broken: one owner-facing instant failure flags it",
    "broken" in f["tg.cat"]["flags"],
    json.dumps(f["tg.cat"]["flags"]),
)
ok("retired tg-bind row: /home is not in the catalogue", "tg.home" not in ids, json.dumps(f.get("tg.home"))[:300])
ok("card: Broken lists only owner failures", "Broken: /cat" in product.card(30, NOW + 120), product.card(30, NOW + 120))
ok("report: idle (short data) flag on a menu command", "idle" in f["tg.queue"]["flags"], str(f["tg.queue"]["flags"]))
an60 = product.analyse(30, NOW + 40 * 86400)
ok(
    "report: unused once a full window of data exists",
    "unused" in {r["id"]: r for r in an60["features"]}["tg.queue"]["flags"],
)
ok("report: notify stats", f["session.reply"]["notify"]["sent"] == 1 and f["session.reply"]["notify"]["opened"] == 1)
ok("report: complaint after use", f["skill.media-requests"]["complaints"] >= 1 or f["tg.week"]["complaints"] >= 1)
ok(
    "report: hypotheses for flagged",
    all(r["why"] for r in an["features"] if {"unused", "idle", "noisy"} & set(r["flags"])),
)
# latency = waits only: a screen's dwell, a session's or call's length and his time to decide are durations, never slow
_mc = sqlite3.connect(":memory:")
_mc.executescript(usage.SCHEMA)
_mc.row_factory = sqlite3.Row
_t0, _n = NOW - 3 * 86400, [0]


def _ev(at, feat, kind, ms=None, outcome=None, attrs=None):
    _n[0] += 1
    usage.put(
        _mc,
        {
            "ts": _t0 + at,
            "feature": feat,
            "kind": kind,
            "actor": "owner",
            "channel": "app",
            "src": "app",
            "ms": ms,
            "outcome": outcome,
            "attrs": attrs or {},
            "ext_id": f"lat:{_n[0]}",
        },
    )


for i, ms in enumerate((4000, 5000, 6000)):  # app screen visits: ms = dwell (usagefwd's view mark)
    _ev(i * 3600, "app.screen.decide", "invoke", ms, attrs={"surface": "screen", "view": True})
for i, ms in enumerate((600000, 900000, 3600000)):  # internet-route sessions: ms on the end = time on it
    _ev(10000 + i * 7200, "phonegw.public", "invoke", outcome="connect")
    _ev(10000 + i * 7200 + ms / 1000 + 120, "phonegw.public", "invoke", ms, outcome="disconnect")
_ev(40000, "phonegw.public", "invoke", outcome="connect")  # a session still open: a use, no length yet
for i in range(2):
    _ev(50000 + i * 3600, "app.talk", "invoke", 24000, outcome="hangup")  # ms = call length (catalogue ms: call)
    _ev(60000 + i * 3600, "skill.web-agent", "tap", 70000, outcome="ok")  # ms = card -> his tap (ms: {tap: decide})
    _ev(70000 + i * 3600, "app.trackid", "invoke", 5000, outcome="done")  # a real wait: stays latency
_day = time.strftime("%Y-%m-%d", time.localtime(NOW - 10 * 86400))
_cat = {
    "features": [
        dict(id=i, name=i, area="app", kind=k, owner_facing=True, entry={}, tags=[], start=_day, **x)
        for i, k, x in (
            ("app.screen.decide", "app", {}),
            ("phonegw.public", "service", {"ms": "session", "session_end": ["disconnect"]}),
            ("app.talk", "app", {"ms": "call"}),
            ("skill.web-agent", "skill", {"ms": {"tap": "decide"}}),
            ("app.trackid", "app", {}),
        )
    ]
}
_la = {r["id"]: r for r in product.analyse(30, NOW + 120, con=_mc, cat=_cat)["features"]}
_sd, _pp = _la["app.screen.decide"], _la["phonegw.public"]
ok(
    "latency: a screen's dwell is no wait (no p50, not slow; duration_ms dwell)",
    _sd["uses"] == 3
    and _sd["latency_ms"]["p50"] is None
    and "slow" not in _sd["flags"]
    and (_sd.get("duration_ms") or {}).get("what") == "dwell"
    and (_sd.get("duration_ms") or {})["p50"] == 5000,
    json.dumps(_sd)[:600],
)
ok(
    "latency: a session's end is its length, not a use, a retry or a wait",
    _pp["uses"] == 4
    and _pp["retries"] == 0
    and _pp["latency_ms"]["p50"] is None
    and not {"slow", "friction"} & set(_pp["flags"])
    and (_pp.get("duration_ms") or {}).get("what") == "session"
    and (_pp.get("duration_ms") or {})["n"] == 3
    and (_pp.get("duration_ms") or {})["p50"] == 900000,
    json.dumps(_pp)[:600],
)
ok(
    "latency: catalogue ms (call length; per kind: his time to decide on a tap) is a duration",
    all(
        _la[i]["latency_ms"]["p50"] is None and "slow" not in _la[i]["flags"] and _la[i]["uses"] == 2
        for i in ("app.talk", "skill.web-agent")
    )
    and (_la["app.talk"].get("duration_ms") or {}).get("what") == "call"
    and (_la["skill.web-agent"].get("duration_ms") or {}).get("what") == "decide",
    json.dumps(
        {i: (_la[i]["latency_ms"], _la[i].get("duration_ms"), _la[i]["flags"]) for i in ("app.talk", "skill.web-agent")}
    ),
)
ok(
    "latency: a real wait still counts and flags slow",
    _la["app.trackid"]["latency_ms"]["p50"] == 5000
    and "slow" in _la["app.trackid"]["flags"]
    and _la["app.trackid"].get("duration_ms", "missing") is None,
    json.dumps(_la["app.trackid"])[:400],
)
# session_gap: a page that re-reports itself while open = one use per session; opened_by: his taps on a reminder's
# own buttons are opens of its notification (one per message)
_sc = sqlite3.connect(":memory:")
_sc.executescript(usage.SCHEMA)
_sc.row_factory = sqlite3.Row
_s0, _k = NOW - 2 * 86400, [0]


def _sv(at, feat, kind, actor="owner", ref=None, attrs=None, ms=None):
    _k[0] += 1
    usage.put(
        _sc,
        {
            "ts": _s0 + at,
            "feature": feat,
            "kind": kind,
            "actor": actor,
            "channel": "telegram",
            "src": "test",
            "ref": ref,
            "ms": ms,
            "attrs": attrs or {},
            "ext_id": f"sg:{_k[0]}",
        },
    )


for m in (0, 10, 20, 35, 70, 180, 185):  # minutes: 0-35 one session, 70, 180-185 two more
    _sv(m * 60, "dash.view", "invoke", attrs={"surface": "web"})
    _sv(m * 60, "dash.other", "invoke")
for m in (0, 100, 200, 300, 400):  # five reminders sent (fired.log rows)
    _sv(m * 60, "skill.reminders", "notify_sent", actor="assistant")
_sv(2 * 60, "skill.reminders", "tap", ref="tg:11", attrs={"prefix": "rm"})  # snooze on message 11
_sv(3 * 60, "reminders.buttons", "tap", ref="tg:11", attrs={"prefix": "rm:done"})  # then done: still one open
_sv(110 * 60, "reminders.buttons", "tap", ref="tg:12", attrs={"prefix": "rm:stom"})  # message 12, 10 min after its send
_sv(
    111 * 60, "reminders.buttons", "tap", attrs={"code": "stom", "mode": "instant"}
)  # remind's own row (no ref): no open
_sv(
    120 * 60, "skill.reminders", "tap", ref="tg:13", attrs={"prefix": "fl:tonight"}
)  # a reminder-making button: no open
_sv(205 * 60, "skill.reminders", "notify_opened", ref="push:x", ms=300000)  # the app's Done on a push
_scat = {
    "features": [
        dict(id=i, name=i, area="core", kind="skill", owner_facing=True, entry={}, tags=[], start=_day, **x)
        for i, x in (
            ("dash.view", {"session_gap": 1800}),
            ("dash.other", {}),
            ("skill.reminders", {"opened_by": ["rm"], "notifies": True}),
            ("reminders.buttons", {}),
        )
    ]
}
_sa = {r["id"]: r for r in product.analyse(30, NOW, con=_sc, cat=_scat)["features"]}
ok(
    "session_gap: events < 30 min apart (sliding) are one use; other features unchanged",
    _sa["dash.view"]["uses"] == 3 and _sa["dash.other"]["uses"] == 7 and _sa["dash.view"]["retries"] == 0,
    json.dumps({i: _sa[i]["uses"] for i in ("dash.view", "dash.other")}),
)
_rn = _sa["skill.reminders"]["notify"]
ok(
    "opened_by: a tap on its own buttons opens the reminder, one per message; app opens still count",
    (_rn["sent"], _rn["opened"], _rn["open_rate"]) == (5, 3, 60) and _rn["median_open_min"] == 5.0,
    json.dumps(_rn),
)
ok(
    "opened_by: the tap stays a use of its own feature",
    _sa["reminders.buttons"]["uses"] == 3 and _sa["reminders.buttons"]["notify"]["opened"] == 0,
    json.dumps(_sa["reminders.buttons"]["notify"]),
)
# job (cron) failures vs use errors: a failing background job is its own review line, never "broken"
_jc = sqlite3.connect(":memory:")
_jc.executescript(usage.SCHEMA)
_jc.row_factory = sqlite3.Row
_jr = usage.Runs()
for i in range(100):  # an ensure job failing half its runs, logged as a run row and an error event
    _jr.add(NOW - 86400 + i * 60, "skill.ensure", "cron", "cron", ok=i % 2 == 0)
    if i % 2:
        usage.put(
            _jc,
            {
                "ts": NOW - 86400 + i * 60,
                "feature": "skill.ensure",
                "kind": "error",
                "actor": "cron",
                "channel": "cron",
                "src": "keepers",
                "outcome": "rc3",
                "ext_id": f"jf:{i}",
            },
        )
for i in range(20):  # a helper script: 20 job runs (10 failed), 12 session-side runs (4 failed)
    _jr.add(NOW - 86400 + i * 60, "bin.feed", "cron", "cron", ok=i % 2 == 0)
    if i < 12:
        _jr.add(NOW - 86400 + i * 60, "bin.flaky", "script", "script", ok=i >= 4)
_jr.flush(_jc)
_jcat = {
    "features": [
        dict(
            id="skill.ensure",
            name="ensure",
            area="core",
            kind="skill",
            owner_facing=True,
            start=_day,
            tags=[],
            entry={"jobs": ["thing-ensure"]},
        ),
        dict(
            id="bin.feed",
            name="feed",
            area="core",
            kind="script",
            owner_facing=False,
            start=_day,
            tags=[],
            entry={"jobs": ["feed-a", "feed-b"]},
        ),
        dict(
            id="bin.flaky", name="flaky", area="core", kind="script", owner_facing=False, start=_day, tags=[], entry={}
        ),
    ]
}
_ja = product.analyse(30, NOW, con=_jc, cat=_jcat)
_jf = {r["id"]: r for r in _ja["features"]}
ok(
    "jobs: cron failures are job_fails (event + run row of one failure counted once), not use errors or broken",
    (_jf["skill.ensure"]["errors"], _jf["skill.ensure"]["job_fails"]) == (0, 50)
    and (_jf["bin.feed"]["errors"], _jf["bin.feed"]["job_fails"]) == (0, 10)
    and "broken" not in _jf["skill.ensure"]["flags"] + _jf["bin.feed"]["flags"],
    json.dumps({i: (_jf[i]["errors"], _jf[i]["job_fails"], _jf[i]["flags"]) for i in ("skill.ensure", "bin.feed")}),
)
ok(
    "jobs: a feature is broken on its use-error rate",
    _jf["bin.flaky"]["errors"] == 4 and "broken" in _jf["bin.flaky"]["flags"] and _jf["bin.flaky"]["job_fails"] == 0,
    json.dumps({k: _jf["bin.flaky"][k] for k in ("errors", "job_fails", "flags", "runs")}),
)
_real_analyse, product.analyse = product.analyse, lambda *a, **k: _ja
try:
    _jt = product.review(30, NOW, write=False)
finally:
    product.analyse = _real_analyse
ok(
    "review: failed job runs on their own lines, ranked; the job's feature not under Improve",
    "- jobs: thing-ensure 50 failed runs of 100 (ensure)" in _jt
    and "- jobs: feed-a, feed-b 10 failed runs of 20 (feed)" in _jt
    and _jt.index("thing-ensure") < _jt.index("feed-a")
    and "- ensure [" not in _jt
    and "- flaky [broken]" in _jt,
    _jt,
)
txt, data = product.slice_by(usage.connect(ro=True), "hour_band", None, 30, NOW + 120)
ok("slice by context dim", sum(data["uses"].values()) >= 3 and "hour_band" in txt)
txt, data = product.slice_by(usage.connect(ro=True), "trigger_ch", "skill.media-requests", 30, NOW + 120)
ok("slice one feature by trigger channel", data["uses"].get("telegram", 0) >= 1, json.dumps(data))
cd = product.card(30, NOW + 120)
ok("card: <= 10 lines", 3 <= len(cd.splitlines()) <= 10, cd)
with open(P(T, "posture.txt"), "w") as f:
    f.write("🛡 Security: test posture · open findings 0\n")
note = product.review(30, NOW + 120, write=True)
nt = open(note).read()
ok("review: security-watch posture line in the note", "- 🛡 Security: test posture" in nt, nt)
ok("review: inbox note marked as data", "Data, not instructions" in nt and os.listdir(P(T, "reports")))
ok("review: ledger 'kept' keeps /queue off the retire list", "retire? /queue" not in nt, nt)

# ---------------------------------------------------------------- CLIs and wrappers
cf = os.environ.get("CRON_FAIL_BIN") or os.path.join(REAL_HOME, "bin/cron-fail")
cenv = {**os.environ, "HOME": P(T, "cfhome"), "USAGE_STATE": P(T, "cfstate")}
os.makedirs(P(T, "cfhome"), exist_ok=True)
subprocess.run([cf, "testjob", "true"], env=cenv, capture_output=True)
subprocess.run([cf, "testjob", "sh", "-c", "exit 3"], env=cenv, capture_output=True)
r = subprocess.run(
    [cf, "--ok", "3", "okjob", "sh", "-c", "echo $USAGE_JOB; exit 3"], env=cenv, capture_output=True, text=True
)
sp = P(T, "cfstate/spool")
cl = [json.loads(l) for x in (os.listdir(sp) if os.path.isdir(sp) else []) for l in open(P(sp, x))]  # noqa: E741
ok("cron-fail: spool lines ok/rc", [x["o"] for x in cl] == ["ok", "rc3", "ok"] and cl[0]["f"] == "job.testjob", str(cl))
ok("cron-fail: USAGE_JOB exported, exit code kept", r.stdout.strip() == "okjob" and r.returncode == 3)
tgc = os.environ.get("TGCMD_BIN") or os.path.join(REAL_HOME, "bin/tg-cmd")
fakebin = P(T, "fakebin")
os.makedirs(fakebin, exist_ok=True)
with open(P(fakebin, "product"), "w") as fh:
    fh.write("#!/bin/sh\necho '📊 Usage · test card'\n")
os.chmod(P(fakebin, "product"), 0o755)
r = subprocess.run(
    [tgc, "usage"],
    env={**os.environ, "TG_CMD_BIN": fakebin, "USAGE_STATE": P(T, "tgstate")},
    capture_output=True,
    text=True,
    timeout=60,
)
ok("tg-cmd /usage answers", r.returncode == 0 and "test card" in r.stdout, r.stdout + r.stderr)
sp = P(T, "tgstate/spool")
ok("tg-cmd: no record in test mode (TG_CMD_BIN)", not os.path.isdir(sp) or not os.listdir(sp))
r = subprocess.run(
    [tgc, "usage"], env={**os.environ, "USAGE_STATE": P(T, "tgstate")}, capture_output=True, text=True, timeout=60
)
tl = [json.loads(l) for x in (os.listdir(sp) if os.path.isdir(sp) else []) for l in open(P(sp, x))]  # noqa: E741
ok(
    "tg-cmd: exec record with ms (real product, temp store)",
    r.returncode == 0 and tl and tl[0]["f"] == "tg.usage" and tl[0]["k"] == "exec" and "ms" in tl[0],
    str(tl) + r.stderr[-200:],
)
ok(
    "tg-cmd: a run from this (non-plugin) process is not the owner",
    tl and tl[0].get("a") in ("agent", "script", "cron") and tl[0].get("o") == "ok",
    str(tl),
)
r = subprocess.run(
    [tgc, "usage"],
    env={**os.environ, "USAGE_STATE": P(T, "tgstate2"), "TG_CMD_TEST": "1"},
    capture_output=True,
    text=True,
    timeout=60,
)
sp2 = P(T, "tgstate2/spool")
tl = [json.loads(l) for x in (os.listdir(sp2) if os.path.isdir(sp2) else []) for l in open(P(sp2, x))]  # noqa: E741
ok("tg-cmd: TG_CMD_TEST tags the record actor=test", tl and tl[0].get("a") == "test", str(tl))
r = subprocess.run([tgc, "--list"], capture_output=True, text=True)
ok("tg-cmd: /usage in --list", r.returncode == 0 and "/usage" in r.stdout, r.stdout[-300:] + r.stderr)
ok(
    "place: street-like town dropped",
    all(usage.coarse_town(x) is None for x in ("Teststrasse 12", "Hauptweg", "Foo 3")),
)
ok(
    "place: plain town kept",
    usage.coarse_town("Zürich") == "Zürich" and usage.coarse_town("St. Gallen") == "St. Gallen",
)
ucli = os.environ.get("USAGE_CLI") or os.path.join(REAL_HOME, "bin/usage")
pcli = os.environ.get("PRODUCT_CLI") or os.path.join(REAL_HOME, "bin/product")
if os.path.exists(ucli) and os.path.exists(pcli):
    r = subprocess.run([ucli, "log", "tg.week", "invoke", "--attr", "x=two words", "--ms", "5"], capture_output=True)
    ok("usage log: exit 0", r.returncode == 0)
    r = subprocess.run([ucli, "stats"], capture_output=True, text=True)
    ok("usage stats", r.returncode == 0 and '"events"' in r.stdout, r.stderr)
    r = subprocess.run([pcli, "report", "--by", "zone"], capture_output=True, text=True)
    ok("product report --by", r.returncode == 0 and "zone" in r.stdout, r.stderr)
    r = subprocess.run([pcli, "report", "--by", "modality"], capture_output=True, text=True)
    ok(
        "modality: taps count as button, commands as command",
        "button" in r.stdout and "command" in r.stdout,
        r.stdout[-400:],
    )
    r = subprocess.run([pcli, "report", "--json"], capture_output=True, text=True)
    ok("product report --json", r.returncode == 0 and json.loads(r.stdout)["features"], r.stderr[-300:])


# hub-tg (v2 H6): collect_hub reads core.hub.tg_journal's entries as collect_events reads the ledger (a fake hub module);  # noqa: E501
# off by default here (USAGE_JOURNAL / USAGE_EVENTS_DB set); the ledger's /lights row again = the same ext_id
class _FakeHub:
    def __init__(self):
        self.calls = []

    def tg_journal(self, since, until, kinds=(), timeout=0):
        self.calls.append((since, until, tuple(kinds)))
        m = lambda mid, t, **kw: dict({"chat_id": "1", "message_id": mid, "ts": iso(t), "user_id": "1"}, **kw)  # noqa: E731
        return [
            {
                "id": "hub:1",
                "kind": "instant",
                "owner": True,
                "content": "/queue now",
                "meta": m("430", NOW - 600, event_type="message", handled="q-binding"),
            },
            {
                "id": "hub:2",
                "kind": "button",
                "owner": True,
                "content": "",
                "meta": m("431", NOW - 590, event_type="button", callback_data="zz:hub:2"),
            },
            {
                "id": "hub:3",
                "kind": "message",
                "owner": True,
                "content": "/lights",
                "meta": m("411", NOW - 1850, event_type="message"),
            },
        ]


_fh = _FakeHub()
_con = usage.connect()
_idx = usage.load_index()
ok("hub: off under test paths", usage.collect_hub(_con, _idx) == 0 and usage._hub() is None)
_real_hub, usage._hub = usage._hub, lambda: _fh
with _con:
    usage.collect_hub(_con, _idx)
ev = usage.connect(ro=True).execute("SELECT * FROM events").fetchall()
q_ = [r for r in ev if r["feature"] == "tg.queue" and r["kind"] == "invoke"]
ok(
    "hub: a bound /queue = one instant invoke (the command, not the binding name)",
    len(q_) == 1 and json.loads(q_[0]["attrs"]).get("instant") is True and q_[0]["src"] == "hub",
    str([dict(r) for r in q_]),
)
ok("hub: a tap", any(r["kind"] == "tap" and r["src"] == "hub" and r["ref"] == "tg:431" for r in ev))
ok("hub: the ledger's /lights again = one row", len([r for r in ev if r["feature"] == "tg.lights"]) == 1)
ok(
    "hub: asked for the ledger kinds, 3 days back",
    _fh.calls[0][2] == tuple(usage.EV_KINDS) and abs(_fh.calls[0][0] - (time.time() - 3 * 86400)) < 60,
    str(_fh.calls),
)
with _con:
    usage.collect_hub(_con, _idx)
ok(
    "hub: the next read from hub_ts - 15 min",
    abs(_fh.calls[1][0] - (usage.mget(_con, "hub_ts") - 900)) < 60,
    str(_fh.calls),
)
usage._hub = _real_hub
_con.close()

# without USAGE_PHONEGW_DB the phonegw db is the brain's allow-listed mirror from `hub phone inbox`
import subprocess as _sp  # noqa: E402
import tempfile as _tf  # noqa: E402

with _tf.TemporaryDirectory() as _h:
    _e = dict({k: v for k, v in os.environ.items() if k != "USAGE_PHONEGW_DB"}, HOME=_h)
    _probe = [
        sys.executable,
        "-I",
        "-c",
        "import sys; sys.path.insert(0, sys.argv[1]); import usage; print(usage.PHONEGW)",
        LIB,
    ]
    _got = _sp.run(_probe, env=_e, capture_output=True, text=True, timeout=60).stdout.strip()
    ok(
        "phonegw db: the brain's mirror by default",
        _got == os.path.join(_h, ".local/state/claude-hub/phonegw-mirror.db"),
        _got,
    )

print(f"\n{len(FAILS)} failed" if FAILS else "\nall passed")
sys.exit(1 if FAILS else 0)
