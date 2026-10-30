"""Offline tests for ~/bin/push (prototype, dry run): fake sleepin/driving and fake senders on PATH that must never
run, a fictional config, isolated HOME and state, simulated clock (--at). PUSH_BIN=/path/to/copy to test a copy."""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

REAL_HOME = os.path.expanduser("~")
BIN = os.environ.get("PUSH_BIN") or os.path.join(REAL_HOME, "bin/push")
# the voice relay's answer filter from this checkout (the suite lives in it; CI hosts have no ~/opt/assistant)
FILTER = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../../adapters/voice/voice_relay/answer_filter.py")
)
LLM_LOCAL = os.path.join(REAL_HOME, "bin/llm-local")
SVC = "notify.mobile_app_test_phone"
# PII-like lint fixtures (IBAN, phone, address, email) live outside the synced tree; skipped when missing
FIXTURES = os.environ.get("PUSH_FIXTURES") or os.path.join(REAL_HOME, "opt/push-fixtures/lint.json")

T = tempfile.mkdtemp(prefix="push-tests-")
HOME = os.path.join(T, "home")
FAKEBIN = os.path.join(T, "fakebin")
CALLS = os.path.join(T, "calls.log")
FLAGS = os.path.join(T, "flags")
for d in (HOME, FAKEBIN, FLAGS):
    os.makedirs(d)
CONF = os.path.join(T, "config.json")


def fake(name, body):
    p = os.path.join(FAKEBIN, name)
    with open(p, "w") as f:
        f.write("#!/bin/sh\n" + body + "\n")
    os.chmod(p, 0o755)
    return p


# probes: exit 0 only while the flag file exists
SLEEPIN = fake("sleepin", f'[ "$1" = active ] && [ -e "{FLAGS}/sleepin" ]')
DRIVING = fake("driving", f'[ -e "{FLAGS}/driving" ]')
# senders: a dry run must never execute any of them
for n in ("tg", "tg-buttons", "voice-relay", "talk-button", "remind", "curl", "ha"):
    fake(n, f'echo "{n} $*" >> "{CALLS}"; exit 0')


# phone CLI stub (the app route; spec §3.8 contract): logs argv as JSON lines, exit code and stdout from flag files.
# It also sits on PATH as `phone`: without PUSH_PHONE it must never run (test state never reaches a real phone).
PLOG = os.path.join(T, "phone.log")
PHONE = os.path.join(FAKEBIN, "phone")
with open(PHONE, "w") as _f:
    _f.write(f"""#!/usr/bin/env python3
import json, os, sys
with open({PLOG!r}, "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1:2] == ["pushes"]:
    pf = {FLAGS!r} + "/phone_pushes"
    print(open(pf).read() if os.path.exists(pf) else '{{"ok": true, "pushes": []}}')
    sys.exit(0)
rc = int(open({FLAGS!r} + "/phone_rc").read()) if os.path.exists({FLAGS!r} + "/phone_rc") else 0
out = {FLAGS!r} + "/phone_out"
print(open(out).read() if os.path.exists(out) else "")
sys.exit(rc)
""")
os.chmod(PHONE, 0o755)
# remind stub for `push hook` (its own log: the PATH fakes must stay silent)
RLOG = os.path.join(T, "remind.log")
REMIND_STUB = os.path.join(T, "remind-stub")
with open(REMIND_STUB, "w") as _f:
    _f.write(f'#!/bin/sh\necho "$*" >> "{RLOG}"\n')
os.chmod(REMIND_STUB, 0o755)


def tapped(verb, push_id=None):
    with open(os.path.join(FLAGS, "phone_pushes"), "w") as f:
        json.dump({"ok": True, "pushes": [{"id": push_id or DELIVERED["id"], "verb": verb, "arg": None}]}, f)


DELIVERED = {"id": "01JTESTPUSH0000000000000001", "nonce": "a1b2c3d4", "status": "delivered"}


def phone_set(rc=0, out=DELIVERED):
    with open(os.path.join(FLAGS, "phone_rc"), "w") as f:
        f.write(str(rc))
    with open(os.path.join(FLAGS, "phone_out"), "w") as f:
        f.write(json.dumps(out) if out is not None else "")


def phone_calls(clear=False):
    calls = [json.loads(x) for x in open(PLOG)] if os.path.exists(PLOG) else []
    if clear and os.path.exists(PLOG):
        os.unlink(PLOG)
    return calls


def config(**kw):
    c = {
        "notify_service": SVC,
        "night": "hold",
        "urgent_cap": 2,
        "daily_cap": 10,
        "quiet": ["00:00", "08:00"],
        "batch_times": ["09:00", "13:00", "18:00"],
        "ack_minutes": 10,
        "snooze_minutes": 15,
    }
    c.update(kw)
    with open(CONF, "w") as f:
        json.dump(c, f)


def reset(**kw):
    shutil.rmtree(os.path.join(T, "state"), ignore_errors=True)
    for f in (os.path.join(FLAGS, "phone_pushes"), RLOG):
        if os.path.exists(f):
            os.unlink(f)
    for n in ("sleepin", "driving"):
        flag(n, False)
    config(**kw)


def flag(name, on):
    p = os.path.join(FLAGS, name)
    if on:
        open(p, "w").close()
    elif os.path.exists(p):
        os.unlink(p)


def run(*args, filt=FILTER, stdin=None, phone=None, remind=None):
    env = {
        "HOME": HOME,
        "PATH": FAKEBIN + ":/usr/local/bin:/usr/bin:/bin",  # python3 is /usr/local/bin/python3 in the CI runner
        "PUSH_CONFIG": CONF,
        "PUSH_STATE": os.path.join(T, "state"),
        "PUSH_TZ": "Europe/Berlin",
        "PUSH_FILTER": filt,
        "VOICE_FILTER_SOURCE": LLM_LOCAL,
        "PUSH_SLEEPIN": SLEEPIN,
        "PUSH_DRIVING": DRIVING,
        "LANG": "C.UTF-8",
    }
    if phone:
        env["PUSH_PHONE"] = PHONE if phone is True else phone
    if remind:
        env["PUSH_REMIND"] = REMIND_STUB
    p = subprocess.run([BIN, *args], env=env, input=stdin, capture_output=True, text=True, timeout=60)
    return p


def js(*args, **kw):
    p = run(*args, "--json", **kw)
    assert p.returncode in (0, 1, 3), (p.returncode, p.stderr)
    return p.returncode, json.loads(p.stdout)


def send(level, title, body="", at="2026-10-21 10:00", extra=(), **kw):
    return js("--level", level, "--title", title, "--body", body, "--at", at, *extra, **kw)


def ha(dec):
    return [w for w in dec["would"] if w["kind"] == "ha_service"]


def argvs(dec):
    return [w["argv"] for w in dec.get("would", []) if w["kind"] == "exec"]


TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


@test
def urgent_payload_shape():
    reset()
    rc, d = send(
        "urgent",
        "Array: a disk is disabled",
        "Check the server when you can",
        extra=("--tag", "srv", "--action", "Seen:seen", "--action", "Snooze:snooze", "--action", "Open:telegram"),
    )
    assert rc == 0 and d["route"] == "ha" and d["dry_run"] is True
    (call,) = ha(d)
    assert call["method"] == "POST" and call["path"] == "/api/services/notify/mobile_app_test_phone"
    data = call["json"]["data"]
    assert call["json"]["title"] == "Array: a disk is disabled"
    for k, v in {
        "channel": "assistant_urgent",
        "importance": "high",
        "priority": "high",
        "ttl": 0,
        "visibility": "public",
        "tag": "srv",
        "sticky": "true",
    }.items():
        assert data[k] == v, (k, data[k])
    assert "car_ui" not in data
    nonce = d["nonce"]
    assert re.fullmatch(r"[0-9a-f]{8}", nonce)
    acts = data["actions"]
    assert acts[0] == {"action": f"CLAUDE_SEEN_{nonce}", "title": "Seen"}
    assert acts[1] == {"action": f"CLAUDE_SNOOZE_{nonce}", "title": "Snooze", "authenticationRequired": True}
    assert acts[2]["action"] == "URI" and acts[2]["uri"].startswith("app://")
    assert ["tg", "🚨 Array: a disk is disabled\nCheck the server when you can"] in argvs(d)
    assert d["ladder_at"] == "21.10 10:10" and d["ladder_argv"][:3] == ["voice-relay", "call", "--reason"]


@test
def driving_alarm_private():
    reset()
    flag("driving", True)
    _, d = send("urgent", "Leave now", "Train in 20 min", extra=("--alarm-stream", "on", "--private"))
    data = ha(d)[0]["json"]["data"]
    assert data["car_ui"] is True and data["channel"] == "alarm_stream" and data["visibility"] == "private"
    _, d = send("urgent", "Leave now 2", extra=("--alarm-stream", "max"))
    assert ha(d)[0]["json"]["data"]["channel"] == "alarm_stream_max"


@test
def refused_verbs_and_live():
    reset()
    p = run("--level", "urgent", "--title", "x", "--action", "Approve:approve", "--at", "2026-10-21 10:00")
    assert p.returncode == 2 and "low-risk" in p.stderr
    p = run("--level", "urgent", "--title", "x", *sum((["--action", f"A{i}:seen"] for i in range(4)), []))
    assert p.returncode == 2 and "at most 3" in p.stderr
    p = run("--level", "urgent", "--title", "x", "--live")
    assert p.returncode == 2 and "owner's OK" in p.stderr
    p = run("--level", "loud", "--title", "x")
    assert p.returncode == 2
    assert not os.path.exists(os.path.join(T, "state", "dry", "state.json"))


@test
def lockscreen_lint_blocks():
    reset()
    bad = [
        ("Bill due", "CHF 49.90 by Friday"),
        ("Link", "https://x.example/a?token=abc"),
        ("Parcel", "number 482913 ready"),
        ("Short", "x" * 81),
        ("T" * 41, "ok"),
    ]
    try:
        with open(FIXTURES) as f:
            bad += [tuple(x) for x in json.load(f)["blocked"]]
    except OSError:
        print("     (private PII lint fixtures missing: those cases skipped)")
    for filt in (FILTER, "/nonexistent/answer_filter.py"):
        for title, body in bad:
            if filt != FILTER and title in ("Where",):
                continue  # street addresses: only the voice relay filter knows them
            p = run(
                "--level", "urgent", "--title", title, "--body", body, "--json", "--at", "2026-10-21 10:00", filt=filt
            )
            d = json.loads(p.stdout)
            assert p.returncode == 3 and d["status"] == "blocked" and d["would"] == [], (filt, title, d)
            assert body not in json.dumps(d["problems"]) or len(body) > 80
        p = run(
            "--level",
            "urgent",
            "--title",
            "Last S9 home in 25 min",
            "--body",
            "Platform 41, 00:08 on 22.10.2026",
            "--json",
            "--at",
            "2026-10-21 10:00",
            "--preview",
            filt=filt,
        )
        assert p.returncode == 0 and json.loads(p.stdout)["route"] == "ha", p.stdout
    p = run("--level", "urgent", "--title", "x", "--at", "2026-10-21 10:00", "--preview")
    # ~/bin/llm-local is not in the repo: where it is missing (a CI host) the filter runs its own fallback rules
    eng = "llm-local regex pass" if os.path.exists(LLM_LOCAL) else "voice-relay answer_filter (its fallback)"
    assert eng in p.stdout.splitlines()[0], p.stdout
    p = run("--level", "urgent", "--title", "x", "--at", "2026-10-21 10:00", "--preview", filt="/nonexistent")
    assert "fallback lint" in p.stdout.splitlines()[0]


@test
def normal_and_info_mask_not_block():
    reset()
    rc, d = send("normal", "Bill reminder", "CHF 49.90 due Friday")
    assert rc == 0 and d["route"] == "telegram"
    ((cmd, soft, text),) = argvs(d)
    assert [cmd, soft] == ["tg", "--soft"] and "49.90" not in text and "[amount]" in text
    rc, d = send("info", "Paid", "see https://x.example/a")
    assert d["status"] == "batched" and "https" not in d["body"]


@test
def urgent_cap_two_distinct_tags_per_day():
    reset()
    for i, tag in enumerate(("a", "b")):
        _, d = send("urgent", f"Urgent {tag}", at=f"2026-10-21 1{i}:00", extra=("--tag", tag))
        assert d["route"] == "ha"
    _, d = send("urgent", "Urgent a again", at="2026-10-21 12:00", extra=("--tag", "a"))
    assert d["route"] == "ha" and "replaces" in " ".join(d["notes"])
    _, d = send("urgent", "Urgent c", at="2026-10-21 13:00", extra=("--tag", "c"))
    assert d["status"] == "capped" and argvs(d) == [["tg", "🚨 Urgent c"]] and not ha(d)
    _, d = send("urgent", "Urgent c", at="2026-10-22 09:00", extra=("--tag", "c"))
    assert d["route"] == "ha"
    reset(urgent_cap=1)
    send("urgent", "one", extra=("--tag", "x"))
    _, d = send("urgent", "two", extra=("--tag", "y"))
    assert d["status"] == "capped"


@test
def quiet_hours_hold_and_ring():
    reset()
    _, d = send("urgent", "Server down", at="2026-10-21 02:10", extra=("--tag", "q"))
    assert d["status"] == "held" and d["until"] == "21.10 08:00" and not d["would"]
    _, f = js("flush", "--at", "2026-10-21 07:59")
    assert f["items"] == []
    _, f = js("flush", "--at", "2026-10-21 08:00")
    (rel,) = f["items"]
    assert rel["route"] == "ha" and "released" in " ".join(rel["notes"])
    _, d = send("urgent", "Server down 2", at="2026-10-21 02:10", extra=("--night", "ring"))
    assert d["route"] == "ha"
    reset(night="ring")
    _, d = send("urgent", "Server down", at="2026-10-21 03:00")
    assert d["route"] == "ha"
    _, d = send("normal", "Done", "the scan finished", at="2026-10-21 03:00")
    assert d["status"] == "held" and d["until"] == "21.10 08:00"
    # DST night (clock change 25.10 03:00 -> 02:00): still held to 08:00 local
    _, d = send("normal", "Done", at="2026-10-25 01:30")
    assert d["until"] == "25.10 08:00"


@test
def quiet_hours_follow_the_trip_zone():
    cfgdir = os.path.join(HOME, ".config", "tripclock")
    os.makedirs(cfgdir, exist_ok=True)
    trips = os.path.join(cfgdir, "trips.json")
    with open(trips, "w") as f:
        json.dump({"trips": [{"name": "t", "tz": "Asia/Dubai", "from": "2027-01-15", "to": "2027-01-30"}]}, f)
    try:
        reset()
        _, d = send("normal", "Done", "the scan finished", at="2027-01-20 22:30")  # 01:30 his time
        assert d["status"] == "held" and d["until"] == "21.01 05:00", d  # 08:00 his time
        _, d = send("normal", "Done", "the scan finished", at="2027-01-21 05:30")  # 08:30 his time
        assert d["status"] != "held", d
        _, d = send("normal", "Done", "the scan finished", at="2027-01-10 22:30")  # at home: not quiet
        assert d["status"] != "held", d
    finally:
        os.remove(trips)


@test
def sleep_in_guard_for_non_urgent_only():
    reset()
    flag("sleepin", True)
    _, d = send("normal", "Results", "the report is ready", at="2026-10-21 08:30")
    assert argvs(d)[0][:2] == ["tg", "--soft"] and "sleep-in" in " ".join(d["notes"])
    _, d = send("urgent", "Leave now", at="2026-10-21 08:30")
    assert d["route"] == "ha" and "sleep-in" not in " ".join(d["notes"])


@test
def info_batch_digest():
    reset()
    send("info", "Scan done", "3 new items", at="2026-10-21 07:00")
    send("info", "Backup ok", at="2026-10-21 08:30")
    _, f = js("flush", "--at", "2026-10-21 08:45")
    assert f["items"] == []
    _, f = js("flush", "--at", "2026-10-21 09:00")
    (dig,) = f["items"]
    ((cmd, soft, text),) = argvs(dig)
    assert [cmd, soft] == ["tg", "--soft"] and text.startswith("🗂 2 updates") and "• Backup ok" in text
    _, f = js("flush", "--at", "2026-10-21 09:30")
    assert f["items"] == []
    send("info", "Late", at="2026-10-21 23:50")
    _, f = js("flush", "--at", "2026-10-22 00:30")
    assert f["items"] == []  # never in quiet hours
    _, f = js("flush", "--at", "2026-10-22 09:05")
    assert len(f["items"]) == 1


@test
def ack_ladder():
    reset()
    _, d = send("urgent", "Last train", "Leaves in 25 min", at="2026-10-21 23:30", extra=("--tag", "lt"))
    _, l = js("ladder", "--at", "2026-10-21 23:39")  # noqa: E741
    assert l["items"] == []
    _, l = js("ladder", "--at", "2026-10-21 23:40")  # noqa: E741
    (item,) = l["items"]
    assert argvs(item) == [
        ["voice-relay", "call", "--reason", "Urgent and not acknowledged: Last train. Leaves in 25 min"]
    ]
    _, l = js("ladder", "--at", "2026-10-21 23:50")  # noqa: E741
    assert l["items"] == []  # once per push
    send("urgent", "Disk", at="2026-10-21 10:00", extra=("--tag", "dk"))
    p = run("ack", "dk", "--at", "2026-10-21 10:02")
    assert p.returncode == 0 and "acked 1" in p.stdout
    _, l = js("ladder", "--at", "2026-10-21 10:30")  # noqa: E741
    assert l["items"] == []


def event(action=None, etype="mobile_app_notification_action", **data):
    if action:
        data["action"] = action
    return json.dumps({"event_type": etype, "data": data})


@test
def action_events():
    reset(urgent_cap=5)
    _, d = send(
        "urgent",
        "Disk",
        "check it",
        extra=("--tag", "dk", "--action", "Seen:seen", "--action", "Call me:call", "--action", "Snooze:snooze"),
    )
    n = d["nonce"]
    _, e = js("event", "-", "--at", "2026-10-21 10:01", stdin=event(f"CLAUDE_DONE_{n}"))
    assert e["result"] == "ignored" and "not offered" in e["why"]
    _, e = js("event", "-", "--at", "2026-10-21 10:01", stdin=event(f"CLAUDE_CALL_{n}"))
    assert e["result"] == "ok" and argvs(e) == [["talk-button"]]
    _, e = js("event", "-", "--at", "2026-10-21 10:01", stdin=event(f"CLAUDE_CALL_{n}"))
    assert e["result"] == "ignored"
    _, l = js("ladder", "--at", "2026-10-21 10:30")  # noqa: E741
    assert l["items"] == []  # call = reachable, no ladder
    _, e = js("event", "-", "--at", "2026-10-21 10:02", stdin=event(f"CLAUDE_SNOOZE_{n}"))
    assert e["result"] == "ok" and "10:17" in e["effect"]
    _, e = js("event", "-", "--at", "2026-10-21 10:03", stdin=event(f"CLAUDE_SEEN_{n}"))
    assert e["result"] == "ignored" and e["why"] == "nonce already used"
    _, f = js("flush", "--at", "2026-10-21 10:17")
    (again,) = f["items"]
    assert again["route"] == "ha" and "replaces" in " ".join(again["notes"])
    n2 = again["nonce"]
    _, e = js("event", "-", "--at", "2026-10-21 10:18", stdin=event(f"CLAUDE_SEEN_{n2}"))
    assert e["result"] == "ok" and "ladder stopped" in e["effect"]
    for bad in (
        event("CLAUDE_SEEN_deadbeef"),
        event("approve_all"),
        event(etype="call_service", action="x"),
        "not json",
    ):
        p = run("event", "-", "--json", "--at", "2026-10-21 10:20", stdin=bad)
        assert p.returncode in (0, 2), p.stderr
        if p.returncode == 0:
            assert json.loads(p.stdout)["result"] == "ignored"
    # snooze with a remind ID, and swipe-away = seen
    _, d = send("urgent", "Pills", extra=("--tag", "pl", "--action", "Snooze:snooze:abc123"))
    _, e = js("event", "-", "--at", "2026-10-21 10:05", stdin=event(f"CLAUDE_SNOOZE_{d['nonce']}"))
    assert argvs(e) == [["remind", "snooze", "abc123", "+15m"]]
    send("urgent", "Disk 2", extra=("--tag", "d2"))
    _, e = js("event", "-", "--at", "2026-10-21 10:06", stdin=event(etype="mobile_app_notification_cleared", tag="d2"))
    assert e["result"] == "ok"
    _, l = js("ladder", "--at", "2026-10-21 11:00")  # noqa: E741
    assert l["items"] == []


@test
def status_and_missing_target():
    reset()
    send("urgent", "Disk", extra=("--tag", "dk"))
    send("info", "x")
    _, s = js("status", "--at", "2026-10-21 10:05")
    assert s["urgent_today"] == 1 and s["batch"] == 1 and s["open_acks"][0]["ladder_at"] == "21.10 10:10"
    assert s["notify_service_set"] is True
    with open(CONF, "w") as f:
        json.dump({}, f)
    p = run("--level", "urgent", "--title", "x", "--at", "2026-10-21 10:00", "--preview")
    assert "no notify_service in config" in p.stdout and "mobile_app_DEVICE" in p.stdout


@test
def human_output_and_preview_writes_nothing():
    reset()
    p = run("--level", "urgent", "--title", "Disk", "--body", "check it", "--preview", "--at", "2026-10-21 10:00")
    assert p.returncode == 0 and p.stdout.startswith("push DRY RUN (nothing sent)")
    assert "would POST <ha_url>/api/services/notify/mobile_app_test_phone" in p.stdout
    assert not os.path.exists(os.path.join(T, "state", "dry", "state.json"))


def app_event(body, at="2026-10-21 10:03"):
    return js("event", "-", "--at", at, stdin=json.dumps(body), phone=True)[1]


@test
def app_route_delivered_then_seen_stops_ladder():
    reset()
    phone_set(0)
    phone_calls(clear=True)
    rc, d = send(
        "urgent",
        "Array: a disk is disabled",
        "Check the server when you can",
        phone=True,
        extra=(
            "--tag",
            "srv",
            "--action",
            "Seen:seen",
            "--action",
            "Snooze:snooze:1a2b3c4d",
            "--action",
            "Open:telegram",
        ),
    )
    assert rc == 0 and d["route"] == "app" and d["status"] == "sent" and d["app"]["status"] == "delivered"
    assert ha(d) == [] and argvs(d) == [], "app delivered: no HA push, no Telegram copy"
    assert d["ladder_at"] == "21.10 10:10" and d["ladder_argv"][:2] == ["voice-relay", "call"]
    (argv,) = phone_calls()
    assert argv[:3] == ["push", "--level", "urgent"], argv
    for pair in (
        ["--title", "Array: a disk is disabled"],
        ["--body", "Check the server when you can"],
        ["--tag", "srv"],
        ["--action", "Seen:seen"],
        ["--action", "Snooze:snooze:r:1a2b3c4d"],
        ["--action", "Open:open"],
        ["--ttl", "30m"],
        ["--wait", "30"],
    ):
        assert any(argv[j : j + 2] == pair for j in range(len(argv))), (pair, argv)
    assert "--sticky" in argv and "--json" in argv and "--private" not in argv
    _, s = js("status", "--at", "2026-10-21 10:01", phone=True)
    assert s["app"] == "on" and s["app_cli"] is True and s["open_acks"][0]["route"] == "app"
    e = app_event({"push_id": DELIVERED["id"], "nonce": DELIVERED["nonce"], "verb": "seen", "arg": "", "at": 1})
    assert e["result"] == "ok" and e["verb"] == "seen" and "ladder stopped" in e["effect"], e
    _, s = js("status", "--at", "2026-10-21 10:04", phone=True)
    assert s["open_acks"] == [] and s["recent_acks"][-1]["why"] == "seen", s
    assert s["recent_acks"][-1]["via"] == "app" and s["recent_acks"][-1]["route"] == "app"
    _, l = js("ladder", "--at", "2026-10-21 11:00", phone=True)  # noqa: E741
    assert l["items"] == []
    e = app_event({"push_id": DELIVERED["id"], "verb": "seen"})
    assert e["result"] == "ignored" and e["why"] == "push already closed"
    p = run("ack", "nothing-open", "--at", "2026-10-21 11:01", phone=True)
    assert p.returncode == 1


@test
def app_route_ladder_replace_and_wrapped_actions():
    reset(urgent_cap=5)
    phone_set(0)
    send("urgent", "Last S9 in 25 min", extra=("--tag", "lt"), phone=True)
    _, l = js("ladder", "--at", "2026-10-21 10:11", phone=True)  # noqa: E741
    (item,) = l["items"]
    assert item["route"] == "app" and [w["argv"][0] for w in item["would"]][1:] == ["voice-relay", "tg"], item
    ring = item["would"][0]["argv"]  # M3: the in-app ring comes first
    assert ring[1:3] == ["ring", "--reason"] and ring[ring.index("--show") + 1] == "Last S9 in 25 min", ring
    phone_set(0, {"id": "01JX2", "nonce": "ffff0000", "status": "delivered"})
    _, d = send("urgent", "Last S9 in 20 min", at="2026-10-21 10:12", extra=("--tag", "lt"), phone=True)
    assert any("replaces" in n for n in d["notes"]), d["notes"]
    e = app_event({"t": "action", "body": {"nonce": "ffff0000", "verb": "snooze"}})
    assert e["result"] == "ignored" and "not offered" in e["why"], e
    e = app_event({"t": "action", "body": {"push_id": "01JX2", "verb": "dismiss"}})
    assert e["result"] == "ok" and e["verb"] == "cleared"
    e = app_event({"push_id": "01JOTHER", "nonce": "99999999", "verb": "done", "arg": "r:1a2b3c4d"})
    assert e["result"] == "ignored" and "remind act" in e["why"], e
    for bad in ({"push_id": "01JX9", "verb": "approve"}, {"verb": ""}, {"t": "action", "body": {}}):
        assert app_event(bad)["result"] == "ignored"
    # snooze offered with a remind ID: the effect names `remind snooze`, the ladder stops
    phone_set(0, {"id": "01JX3", "nonce": "0000aaaa"})
    send(
        "urgent", "Pills", at="2026-10-21 10:20", extra=("--tag", "pl", "--action", "Later:snooze:abc12345"), phone=True
    )
    e = app_event({"push_id": "01JX3", "verb": "snooze", "arg": "r:abc12345"}, at="2026-10-21 10:21")
    assert argvs(e) == [["remind", "snooze", "abc12345", "+15m"]], e
    # by our tag or the app push id via `push ack`
    phone_set(0, {"id": "01JX4", "nonce": "0000bbbb"})
    send("urgent", "Disk 4", at="2026-10-21 10:22", extra=("--tag", "d4"), phone=True)
    assert json.loads(run("ack", "01JX4", "--json", "--at", "2026-10-21 10:23", phone=True).stdout)["acked"]


@test
def app_route_fallbacks_and_fast_ack():
    for code, status in ((4, "offline"), (5, "no-ack"), (6, "refused"), (7, "gateway-down"), (2, "usage")):
        reset()
        phone_set(code, None)
        rc, d = send("urgent", "Disk 3 is hot", extra=("--tag", "hot"), phone=True)
        assert rc == 0 and d["route"] == "ha" and d["app"]["status"] == status, (code, d)
        assert len(ha(d)) == 1 and ["tg", "🚨 Disk 3 is hot"] in argvs(d), d
        assert any(f"app route: {status}" in n for n in d["notes"]), d["notes"]
    # queued for an offline phone (exit 4): a late app ack still stops the ladder
    reset()
    phone_set(4, {"id": "01JQUEUED", "nonce": "0badc0de", "status": "queued"})
    send("urgent", "Disk 5", extra=("--tag", "d5"), phone=True)
    assert app_event({"push_id": "01JQUEUED", "verb": "seen"}, at="2026-10-21 10:08")["result"] == "ok"
    assert js("ladder", "--at", "2026-10-21 10:30", phone=True)[1]["items"] == []
    # phone's own lint blocks -> exit 3, nothing else runs
    reset()
    phone_set(3, None)
    rc, d = send("urgent", "Disk 6", extra=("--tag", "d6"), phone=True)
    assert rc == 3 and d["status"] == "blocked" and d["would"] == []
    # phone missing -> the old path, no app key
    reset()
    rc, d = send("urgent", "Disk 7", phone=os.path.join(T, "no-such-phone"))
    assert d["route"] == "ha" and "app" not in d
    # acked within the wait (phone's JSON carries the verb) -> closed, no ladder
    reset()
    phone_set(0, {"id": "01JFAST", "nonce": "12345678", "status": "acked", "verb": "seen"})
    rc, d = send("urgent", "Disk 8", extra=("--tag", "d8"), phone=True)
    assert d["route"] == "app" and d["status"] == "acked" and "ladder_at" not in d, d
    assert js("ladder", "--at", "2026-10-21 11:00", phone=True)[1]["items"] == []
    # config app off -> phone never runs; the cap counts app pushes
    reset(app="off")
    phone_calls(clear=True)
    assert send("urgent", "Disk 9", phone=True)[1]["route"] == "ha" and phone_calls() == []
    reset()
    phone_set(0)
    for t in ("a1", "a2"):
        assert send("urgent", "Disk " + t, extra=("--tag", t), phone=True)[1]["route"] == "app"
    _, d = send("urgent", "Disk a3", extra=("--tag", "a3"), phone=True)
    assert d["route"] == "telegram" and d["status"] == "capped"
    # preview: shows the phone call first, never runs it
    reset()
    phone_calls(clear=True)
    p = run("--level", "urgent", "--title", "Disk", "--preview", "--at", "2026-10-21 10:00", phone=True)
    assert p.returncode == 0 and "app route first (preview: not run)" in p.stdout and phone_calls() == [], p.stdout
    # human output of a live app send
    phone_set(0)
    p = run("--level", "urgent", "--title", "Disk", "--at", "2026-10-21 10:00", phone=True)
    assert p.stdout.startswith("push DRY RUN (nothing sent but the app route)") and "app: delivered" in p.stdout


def hook(payload, remind=True, at="2026-10-21 10:04"):
    p = run(
        "hook",
        "--at",
        at,
        stdin=payload if isinstance(payload, str) else json.dumps(payload),
        phone=True,
        remind=remind,
    )
    return p.returncode, (json.loads(p.stdout.strip().splitlines()[-1]) if p.stdout.strip() else {})


def rlog():
    return open(RLOG).read().splitlines() if os.path.exists(RLOG) else []


@test
def hook_routes_reminders_and_push_acks():
    reset(urgent_cap=5)
    phone_set(0)
    send("urgent", "Array: a disk is disabled", extra=("--tag", "srv"), phone=True)
    rc, r = hook(
        {
            "push_id": DELIVERED["id"],
            "tag": "srv",
            "level": "urgent",
            "verb": "seen",
            "arg": None,
            "at": 1,
            "device_id": "d_1",
        }
    )
    assert rc == 0 and r["result"] == "ok" and r["verb"] == "seen", r
    s = js("status", "--at", "2026-10-21 10:05", phone=True)[1]
    assert s["open_acks"] == [] and s["recent_acks"][-1]["why"] == "seen" and s["recent_acks"][-1]["via"] == "app"
    rc, r = hook(
        {"push_id": "01JREM", "tag": "rem-abc12345", "level": "timed", "verb": "snooze", "arg": "r:abc12345:s1h"}
    )
    assert rc == 0 and rlog() == ["act snooze r:abc12345:s1h"], (r, rlog())
    rc, r = hook({"push_id": "01JREM", "tag": "rem-abc12345", "verb": "dismissed", "arg": "r:abc12345"})
    assert rc == 0 and len(rlog()) == 1 and "nothing to change" in r["effect"]
    rc, r = hook({"push_id": "01JREM2", "tag": "rem-abc12345", "verb": "done", "arg": "r:abc12345"}, remind=False)
    assert rc == 0 and r["would"][0]["argv"] == ["remind", "act", "done", "r:abc12345"] and len(rlog()) == 1
    assert hook("not json")[0] == 2 and hook({"push_id": "x"})[0] == 2
    rc, r = hook({"push_id": "01JUNKNOWN", "tag": "zz", "verb": "seen"})
    assert rc == 0 and r["result"] == "ignored"
    # an urgent push offering snooze with a remind ID: remind act runs AND the ladder stops
    phone_set(0, {"id": "01JPILL", "status": "acked"})
    send(
        "urgent", "Pills", at="2026-10-21 10:10", extra=("--tag", "pl", "--action", "Later:snooze:abc12345"), phone=True
    )
    rc, r = hook({"push_id": "01JPILL", "tag": "pl", "verb": "snooze", "arg": "r:abc12345"}, at="2026-10-21 10:11")
    assert rc == 0 and rlog()[-1] == "act snooze r:abc12345", rlog()
    assert js("ladder", "--at", "2026-10-21 11:00", phone=True)[1]["items"] == []


@test
def ladder_and_status_sync_tapped_verbs():
    reset()
    phone_set(0)
    send("urgent", "Last S9 in 25 min", extra=("--tag", "lt"), phone=True)
    tapped("seen")
    _, l = js("ladder", "--at", "2026-10-21 10:11", phone=True)  # noqa: E741
    assert l["items"] == [], "tapped in the app: the ladder must not ring"
    s = js("status", "--at", "2026-10-21 10:12", phone=True)[1]
    assert s["recent_acks"][-1]["why"] == "seen" and s["recent_acks"][-1]["via"] == "app", s
    reset()
    phone_set(0)
    send("urgent", "Disk 2", extra=("--tag", "d2"), phone=True)
    assert js("status", "--at", "2026-10-21 10:01", phone=True)[1]["open_acks"][0]["tag"] == "d2"
    tapped("dismissed")
    s = js("status", "--at", "2026-10-21 10:02", phone=True)[1]
    assert s["open_acks"] == [] and s["recent_acks"][-1]["why"] == "cleared", s
    reset()
    phone_set(0)
    send("urgent", "Disk 3", extra=("--tag", "d3"), phone=True)
    tapped("seen", push_id="01JSOMETHINGELSE")
    assert len(js("ladder", "--at", "2026-10-21 10:11", phone=True)[1]["items"]) == 1


@test
def no_phone_without_env():
    reset()
    phone_calls(clear=True)
    send("urgent", "Disk", extra=("--tag", "x1"))
    run("--level", "urgent", "--title", "Disk 2", "--at", "2026-10-21 10:01")
    assert phone_calls() == [], "the phone stub on PATH ran without PUSH_PHONE"


@test
def never_sends_anything():
    src = open(BIN).read()
    for mod in ("urllib", "http.client", "socket", "requests", "websocket"):
        assert f"import {mod}" not in src, mod
    assert not os.path.exists(CALLS), open(CALLS).read()


def main():
    failed = 0
    try:
        for fn in TESTS:
            try:
                fn()
                print(f"ok   {fn.__name__}")
            except Exception as e:  # noqa: BLE001
                failed += 1
                print(f"FAIL {fn.__name__}: {e!r}")
    finally:
        shutil.rmtree(T, ignore_errors=True)
    print(f"{len(TESTS) - failed}/{len(TESTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
