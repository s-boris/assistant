#!/usr/bin/env python3
"""product: feature catalogue, usage report and weekly product review over the usage store (lib usage.py).

catalogue  build(): ~/.config/product/features.json from the sources (the active tg-bind command rows of the events
           store + the tools registry's menu lines, commands.json, callbacks.json, tg-cmd, skills, ~/bin, crontab + the
           schedule registry core/schedules.toml (jobs on hub schedules, keepers or planned keep their entries and
           schedule; $USAGE_SCHEDULES names another registry file), voice tools, app events) + CURATED below + the private overlay
           ~/.config/product/curated.json ({"features": {id: {field: value}}, "scripts": {script: feature id}}).
           Each feature: id, name, area, kind, purpose, entry points, owner_facing, notifies, menu, start, metric,
           code_lines, tags (rare = silent by design: never flagged unused). index = lookups used by usage.resolve().
           Optional: ms = what its events' ms measures when it is no wait ("session", "call", "decide", "dwell",
           "queue", or {kind: that}); session_end = outcome tokens that close a session (its length, not a use);
           session_gap = seconds: owner events of the feature less than that apart (sliding) are one session = one
           use (a page left open that re-reports itself); opened_by = button prefixes whose tap on a message (ref
           tg:<id>) opens that feature's notification (a reminder's rm:* snooze/done), one open per message.
analyse()  per feature over a window: owner uses (intent events + distinct owner-turn execs), trend vs the previous
           window, last use, channel and context mix, notifications sent/opened/ignored, errors, complaints after use,
           retries, previous-feature funnels, runs/latency/cost; flags unused / untried / declining / noisy / broken /
           slow, each flag with "why not used" hypotheses (discoverability, friction, channel, time, superseded).
           Latency (p50, slow) = waits only: an app screen visit's dwell and the catalogue's ms durations go to
           duration_ms instead.
review()   weekly keep / improve / retire / build-next with evidence -> ~/reports/product/ + one ~/inbox note.
card()     the /usage instant answer (<= 10 lines).
"""
import bisect
import collections
import datetime as dt
import json
import os
import re
import sqlite3
import statistics
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import usage  # noqa: E402

HOME = usage.HOME
CONF = os.path.dirname(usage.CATALOGUE)
OVERLAY = os.path.join(CONF, "curated.json")
TG_DIR = os.environ.get("USAGE_TG_DIR") or os.path.join(HOME, ".claude/channels/telegram")
SKILLS = os.environ.get("USAGE_SKILLS") or os.path.join(HOME, ".claude/skills")
CRONTAB = os.environ.get("USAGE_CRONTAB") or os.path.join(HOME, "crontab")
SCHEDULES = os.environ.get("USAGE_SCHEDULES") or None      # None = core.schedules' default ($ASSISTANT_SCHEDULES too)
WORKSPACE = os.environ.get("USAGE_WORKSPACE") or os.path.join(HOME, "workspace")
REPORTS = os.environ.get("USAGE_REPORTS") or os.path.join(HOME, "reports/product")
INBOX = os.environ.get("USAGE_INBOX") or os.path.join(HOME, "inbox")
SECURITY_POSTURE = os.environ.get("USAGE_SECURITY_POSTURE") or os.path.join(HOME, ".local/state/security-watch/posture.txt")
BRAIN = os.environ.get("USAGE_BRAIN_GIT") or os.path.join(HOME, ".brain.git")

VOICE_TOOLS = ("weather", "way_home", "calendar", "trains", "space", "downloads", "gym", "cat", "finish",
               "rave", "trip", "bills_due", "reminders", "parcels", "identify_track")
APP_TILES = ("talk", "trackid", "capture", "leaving", "sleepin", "private")
METRIC = {
    "command": "used >= 4x in 30 days; answers < 2 s; < 5 % errors",
    "session-command": "used >= 2x a month",
    "skill": ">= 2 owner turns a month; no correction or complaint after use",
    "notifier": ">= 30 % of its messages get a tap, reply or reaction; < 50 % ignored",
    "script": "0 failed runs; used by an owner turn or a feature",
    "job": "runs on time; no failure streaks",
    "voice-tool": "used in calls >= 2x a month; p50 < 1 s; no fallbacks",
    "app": "used >= 1x a week",
    "channel": "volume and reply latency (a channel, not a feature)",
    "subsystem": "healthy; cost proportional to use",
}
# Generic curated entries (no personal values; the private overlay adds his).
CURATED = {
    "chat.telegram": dict(name="Telegram chat with me", area="core", kind="channel", owner_facing=True,
                          purpose="Free text, voice notes and files to the assistant"),
    "chat.app": dict(name="App chat", area="app", kind="channel", owner_facing=True, purpose="Chat in the phone app"),
    "session.reply": dict(name="My Telegram messages", area="core", kind="channel", notifies=True,
                          purpose="Replies and proactive messages from the session"),
    "session.poll": dict(name="Polls", area="core", kind="channel", notifies=True, purpose="Multi-choice polls"),
    "session.react": dict(name="My reactions", area="core", kind="subsystem", purpose="Emoji acknowledgements"),
    "bin.recall": dict(name="recall", area="memory", kind="script", owner_facing=False, notifies=False,
                       purpose="My search over my own notes and memory (internal)"),
    "session.buttons": dict(name="Question buttons", area="core", kind="channel", owner_facing=True,
                            purpose="His taps on the either/or buttons of my questions"),
    "session.draft": dict(name="Draft progress", area="core", kind="subsystem", purpose="Streaming drafts (Stop presses)"),
    "tg.reaction": dict(name="His reactions", area="telegram", kind="channel", owner_facing=True,
                        purpose="Reactions as answers"),
    "tg.poll": dict(name="Poll answers", area="telegram", kind="channel", owner_facing=True, purpose="Answers to polls"),
    "tg.status": dict(name="/status", area="telegram", kind="command", owner_facing=True, menu=True,
                      purpose="Plugin status board on demand", entry={"commands": ["/status"]}),
    "session.tg_shell": dict(name="tg from the shell", area="core", kind="subsystem", notifies=True,
                      purpose="Texts sent with tg outside any script"),
    "voice.call": dict(name="Voice calls", area="voice", kind="channel", owner_facing=True,
                       purpose="Live voice: Telegram call to the second account or in-app call"),
    "voice.call_out": dict(name="Calls I place", area="voice", kind="notifier", notifies=True,
                           purpose="Outbound calls (wake, last train, driving updates)"),
    "voice.talk_button": dict(name="📞 Talk button", area="voice", kind="command", owner_facing=True,
                              purpose="One tap on the board or event card rings him",
                              entry={"buttons": ["call:talk"], "scripts": ["talk-button"]}),
    "voice.ask_assistant": dict(name="Voice hand-off", area="voice", kind="voice-tool", owner_facing=True,
                                purpose="A call question the session answers"),
    "voice.decision_deck": dict(name="Decision round by call", area="voice", kind="voice-tool", owner_facing=True,
                                purpose="Waiting-on-you items as spoken either/or"),
    "voice.reply": dict(name="Call reply latency", area="voice", kind="subsystem", purpose="Gemini reply latency"),
    "app": dict(name="Phone app health", area="app", kind="subsystem", purpose="Crashes and connects of the app"),
    "app.push": dict(name="App pushes", area="app", kind="notifier", notifies=True, purpose="Pushes to the phone app"),
    "app.live": dict(name="Live countdown chips", area="app", kind="notifier", notifies=True,
                     purpose="Status-bar countdowns for trains, flights, last call"),
    "app.card.decision": dict(name="App decision cards", area="app", kind="notifier", notifies=True, owner_facing=True,
                              purpose="Either/or cards in the app"),
    "app.buds_guard": dict(name="Buds left behind", area="app", kind="notifier", notifies=True, tags=["rare"],
                           purpose="Warns when the buds stay behind"),
    "app.battery_watch": dict(name="App battery watchdog", area="app", kind="subsystem", tags=["rare"],
                              purpose="Flags the app's own battery use"),
    "arch.subagent": dict(name="Subagents", area="architecture", kind="subsystem",
                          purpose="Background agents for real work"),
    "arch.web": dict(name="Web research", area="architecture", kind="subsystem", purpose="WebSearch / WebFetch"),
    "mem.facts": dict(name="FACTS.md", area="memory", kind="subsystem", purpose="Setup facts + done/declined ledger"),
    "mem.todo": dict(name="TODO.md", area="memory", kind="subsystem", purpose="Task board"),
    "mem.memory": dict(name="Auto memory", area="memory", kind="subsystem", purpose="Preferences and people"),
    "mem.claude_md": dict(name="CLAUDE.md", area="memory", kind="subsystem", purpose="How I operate"),
    "mem.goals": dict(name="GOALS.md", area="memory", kind="subsystem", purpose="Goal steps"),
    "mem.problems": dict(name="PROBLEMS.md", area="memory", kind="subsystem", purpose="Suspected problems"),
    "mem.docs": dict(name="TOOLS/JOBS docs", area="memory", kind="subsystem", purpose="Script and job reference"),
    "mem.log": dict(name="CHANGELOG/PROACTIVE", area="memory", kind="subsystem", purpose="Change and work log"),
    "mem.skills": dict(name="Skill files", area="memory", kind="subsystem", purpose="Skill instructions"),
}
for _t in VOICE_TOOLS:
    CURATED[f"voice.tool.{_t}"] = dict(name=f"voice: {_t.replace('_', ' ')}", area="voice", kind="voice-tool",
                                       owner_facing=True, purpose=f"Fast-lane answer in calls: {_t.replace('_', ' ')}",
                                       entry={"voice": [_t]})
for _t in APP_TILES:
    CURATED[f"app.tile.{_t}"] = dict(name=f"app tile: {_t}", area="app", kind="app", owner_facing=True,
                                     purpose=f"Quick tile in the phone app: {_t}", entry={"app": [f"tile:{_t}"]})
BUTTON_FAMILY = {"rm": "skill.reminders", "pinb": "bin.tg-status", "call": "voice.talk_button",
                 "wled": "skill.home-assistant", "wtune": "skill.home-assistant", "wtest": "skill.home-assistant"}
SKILL_AREA = {"admin-desk": "money", "bills": "money", "browser": "architecture", "calendar": "calendar",
              "calendar-gap-pass": "calendar", "container-watch": "server",
              "deadlines": "money", "deals": "shopping", "divelog": "travel", "home-assistant": "home",
              "kk-compare": "money", "llm-local": "privacy",
              "media-help": "media", "media-requests": "media", "night-plan": "travel", "paperless": "money",
              "rave-radar": "music", "reminders": "core", "server-health": "server", "shopping": "shopping",
              "tgread": "privacy", "trackid": "music", "unraid-capacity": "server", "vault": "privacy",
              "voice-action": "core", "voice-notes": "core", "watch": "core"}
SCRIPT_OWNER = {"media": "skill.media-requests", "remind": "skill.reminders", "bills": "skill.bills",
                "ha": "skill.home-assistant", "unraid-health": "skill.server-health", "hddheat": "skill.server-health",
                "hwtrend": "skill.server-health", "cputemp-log": "skill.server-health",
                "preclear-watch": "skill.server-health", "unifi-health": "skill.server-health",
                "tgread": "skill.tgread", "vault": "skill.vault", "watch": "skill.watch", "paperless": "skill.paperless",
                "lists": "skill.voice-action", "calcache": "skill.calendar", "letter": "skill.admin-desk",
                "pdfform": "skill.admin-desk", "transcribe": "skill.voice-notes"}
AREA_RULES = ((r"^(vault|tgread|llm-|secretscan)", "privacy"), (r"^(unraid|hdd|hw|cputemp|preclear|unifi|container)", "server"),
              (r"^(media|emby|arr-|seerr|vibe)", "media"), (r"^(ha|ha-|party|wled|cat-|damp|window|vacuum)", "home"),
              (r"^(train|flight|drive|strike|trip|route|gohome|night-|lastcall|battery-guard|event-card|ticket|pack|"
               r"pretrip|whereabouts|driving)", "travel"),
              (r"^(brain|codebase|self-|test-all|log-rotate|tmp-tidy|transcript|plugin-drift|model-drift|cache-ttl|"
               r"risk|agent-watch|channel-check|hb-gate|restart|board-lint|docs|cron-fail|usage|product|menu-usage|"
               r"profile|boot|waiting)", "self"),
              (r"^(tg|telegram)", "telegram"), (r"^(voice|tts|talk|decision|whisper)", "voice"),
              (r"^(phone|push|livecount)", "app"), (r"^(gym|dance|practice)", "fitness"),
              (r"^(rave|track|crate|lists)", "music"),
              (r"^(bills|deadline|fx-|contract|kk-|missing-docs|paperless|admin)", "money"),
              (r"^(deals|shop|tutti)", "shopping"), (r"^(remind|sleepin|tripclock|context|dayline|dash|gift)", "core"))
RARE = {"lastcall", "battery-guard", "flight-watch", "strike-watch", "train-watch", "drive-watch", "event-card",
        "ticket-check", "channel-check", "agent-watch", "restart-cause", "gift-radar", "kk-compare", "deadlines",
        "admin-desk", "unraid-capacity", "server-health", "container-watch", "night-plan", "divelog", "voicecheck",
        "ha-autopilot-watch", "cat-corner", "damp-watch", "cron-fail", "scraper-canary", "media-help", "app.buds_guard",
        "app.battery_watch", "voice.call_out", "unraid-health", "unifi-health", "hddheat", "vault-alerts"}
NOTIFY_RE = re.compile(r"(^|[\s\"'(\[,;|&])(tg|tg-buttons|tg-voice|voice-say|phone push|push --level|voice-relay call)"
                       r"([\s\"')\],;]|$)|\[\"tg\"|'tg'[,\]]|\"tg-buttons\"")


def _json(p, d=None):
    return usage._json(p, d)


def _lines(path):
    try:
        with open(path, errors="replace") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def _first_sentence(s, n=150):
    s = re.sub(r"\s+", " ", (s or "").strip())
    m = re.match(r"(.+?[.!?])(\s|$)", s)
    s = m.group(1) if m else s
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _frontmatter(path):
    try:
        txt = open(path, errors="replace").read()
    except OSError:
        return {}, ""
    m = re.match(r"---\n(.*?)\n---\n", txt, re.S)
    fm = {}
    for line in (m.group(1).splitlines() if m else []):
        k, _, v = line.partition(":")
        if k.strip() and v.strip():
            fm[k.strip()] = v.strip().strip("\"'")
    return fm, txt


def _brain_dates():
    """First-add date per path in the brain repo (approximate start dates)."""
    try:
        r = subprocess.run(["git", f"--git-dir={BRAIN}", "log", "--diff-filter=A", "--format=@%as", "--name-only"],
                           capture_output=True, text=True, timeout=60)
    except Exception:
        return {}
    out, day = {}, None
    for line in r.stdout.splitlines():
        if line.startswith("@"):
            day = line[1:]
        elif line.strip() and day:
            out[line.strip()] = day                  # log is newest first: the last seen is the first add
    return out


def _changelog_firsts(terms):
    """Earliest CHANGELOG.md day ("- DD.MM …" lines) mentioning each term as a word."""
    out = {}
    try:
        lines = open(os.path.join(WORKSPACE, "CHANGELOG.md"), errors="replace").read().splitlines()
    except OSError:
        return out
    today = dt.date.today()
    rx = {t: re.compile(re.escape(t) + r"(?![\w-])") for t in terms}
    for line in lines:
        m = re.match(r"- (\d\d)\.(\d\d)\b", line)
        if not m:
            continue
        day, mon = int(m.group(1)), int(m.group(2))
        year = today.year if mon <= today.month else today.year - 1
        try:
            d = dt.date(year, mon, day).isoformat()
        except ValueError:
            continue
        for t, r in rx.items():
            if r.search(line) and (t not in out or d < out[t]):
                out[t] = d
    return out


def _area(script):
    for rx, a in AREA_RULES:
        if re.search(rx, script):
            return a
    return "misc"


def _bound_commands():
    """[(command, tool)]: the events store's active, unexpired tg-bind command rows in row (= menu) order, read-only
    (menu-usage's read_menu() reads them the same way); [] when the store or the table is missing."""
    if not os.path.exists(usage.EVENTS_DB):
        return []
    try:
        con = sqlite3.connect(f"file:{usage.EVENTS_DB}?mode=ro", uri=True, timeout=5)
        try:
            return [(str(k).lower(), str(t)) for k, t in con.execute(
                "SELECT key, tool FROM bindings WHERE kind = 'command' AND active = 1"
                " AND (expires IS NULL OR expires > ?) ORDER BY id", (time.time(),))]
        finally:
            con.close()
    except sqlite3.Error:
        return []


def _registry():
    """The tools registry tg-adapter reads (tgadapter.bind: name -> {menu, description, text_arg, …});
    $USAGE_TOOLS overrides its path (tests); {} when unreadable."""
    try:
        from tgadapter import bind
        return bind.load_registry(os.environ.get("USAGE_TOOLS") or bind.registry_path("live"))
    except Exception:
        return {}


def build(write=True):
    cat, now = {}, time.time()

    def add(fid, **kw):
        f = cat.setdefault(fid, {"id": fid, "entry": {}, "source": "auto"})
        for k, v in kw.items():
            if k == "entry":
                for ek, ev in v.items():
                    f["entry"].setdefault(ek, [])
                    f["entry"][ek] += [x for x in ev if x not in f["entry"][ek]]
            elif v is not None and (k not in f or f[k] in (None, "", [], False)):
                f[k] = v
        return f
    bins = sorted(n for n in usage._ls(usage.BIN) if usage.is_bin(n))
    texts = {}
    for n in bins:
        try:
            with open(os.path.join(usage.BIN, n), errors="replace") as fh:
                texts[n] = fh.read(400000)
        except OSError:
            texts[n] = ""
    # telegram: instant commands (the active tg-bind command rows, run by tg-adapter), session commands
    reg = _registry()
    for cmd, tool in _bound_commands():
        t = reg.get(tool) or {}
        ent = {"commands": [f"/{cmd}"]}
        if tool.startswith("tg-cmd."):   # only tg-cmd's own subcommands credit tg-cmd (other tools: no script credit)
            ent.update(scripts=["tg-cmd"], verbs=[tool[len("tg-cmd."):]])
        add(f"tg.{cmd}", name=f"/{cmd}", area="telegram", kind="command", owner_facing=True, menu=bool(t.get("menu")),
            args=bool(t.get("text_arg")), purpose=t.get("menu") or t.get("description") or None, entry=ent)
    for c in _json(os.path.join(TG_DIR, "commands.json"), []) or []:
        cmd = str(c.get("command", "")).lower()
        if cmd:
            add(f"tg.{cmd}", name=f"/{cmd}", area="telegram", kind="session-command", owner_facing=True, menu=True,
                purpose=c.get("description"), entry={"commands": [f"/{cmd}"]})
    # skills
    script_owner = dict(SCRIPT_OWNER)
    for name in sorted(usage._ls(SKILLS)):
        path = os.path.join(SKILLS, name, "SKILL.md")
        if not os.path.isfile(path):
            continue
        fm, txt = _frontmatter(path)
        desc = fm.get("description", "")
        uses = re.findall(r"~/bin/([A-Za-z0-9._-]+)", " ".join(re.findall(r"Uses? [^.]*", desc + "\n" + txt[:3000])))
        btns = sorted(set(re.findall(r"`([a-z_]{2,12}):[^`]*`", desc)) |
                      set(re.findall(r"\b(shop):(?:order|run|swap|pick|buy)", desc)))
        fid = f"skill.{name}"
        add(fid, name=name, area=SKILL_AREA.get(name, "misc"), kind="skill", owner_facing=True,
            purpose=_first_sentence(desc), entry={"skills": [name], "buttons": btns,
                                                  "scripts": [u for u in uses if u in texts]},
            code_lines=_lines(path))
        for u in uses:
            script_owner.setdefault(u, fid)
    for s, fid in script_owner.items():
        if fid in cat and s in texts:
            add(fid, entry={"scripts": [s]})
    # ~/bin scripts not owned by a skill
    for s in bins:
        notifies = bool(NOTIFY_RE.search(texts.get(s, "")))
        fid = script_owner.get(s)
        if fid:
            if notifies:
                cat[fid]["notifies"] = True
            continue
        area = _area(s)
        add(f"bin.{s}", name=s, area=area, kind="notifier" if notifies else "script", notifies=notifies,
            owner_facing=notifies and area not in ("self",), purpose=_script_purpose(texts.get(s, "")),
            entry={"scripts": [s]})
    # jobs (crontab lines + the registry's lines that run elsewhere): entry points of their script's feature; cues
    # are their own routine features; each registry job also counts under its own name, credited to its argv's script
    jobs = {}
    for sched, cmd, regs in _job_lines():
        m = re.search(r"cron-fail(?:\s+--\S+(?:\s+\d[\d,]*)?)*\s+([A-Za-z0-9][A-Za-z0-9._-]*)\s+", cmd)
        sc = [s for s, _ in usage.scripts(cmd[m.end():] if m else cmd) if s not in ("cron-fail", "timeout")]
        fid = None
        if sc:
            fid = script_owner.get(sc[0]) or f"bin.{sc[0]}"
            name = m.group(1) if m else sc[0]
            jobs.setdefault(name, fid)
            jobs.setdefault(sc[0], fid)
            if fid in cat:
                add(fid, entry={"jobs": [name]}, schedule=sched)
        else:
            cue = re.search(r"inbox/([a-z][a-z0-9-]*?)(?:-\$\(|\.md|-\d|\")", cmd)
            if cue:
                fid = f"job.cue.{cue.group(1)}"
                add(fid, name=f"cue: {cue.group(1)}", area="routine", kind="job", purpose="Scheduled cue note for me",
                    entry={"jobs": [cue.group(1)]}, schedule=sched)
                jobs.setdefault(cue.group(1), fid)
        for e in regs:
            esc = [s for s, _ in usage.scripts(" ".join(e.argv)) if s not in ("cron-fail", "timeout")]
            efid = (script_owner.get(esc[0]) or f"bin.{esc[0]}") if esc else fid
            if efid:
                jobs.setdefault(e.name, efid)
                if efid in cat:
                    add(efid, entry={"jobs": [e.name]}, schedule=e.cron)
    # curated generic + private overlay
    over = _json(OVERLAY, {}) or {}
    for fid, kw in list(CURATED.items()) + list((over.get("features") or {}).items()):
        f = add(fid, **{k: v for k, v in kw.items() if k != "entry"}, entry=kw.get("entry") or {})
        f["source"] = "curated" if f["source"] == "auto" else f["source"]
        for k, v in kw.items():
            if k != "entry":
                f[k] = v
    for s, fid in (over.get("scripts") or {}).items():
        script_owner[s] = fid
    for p, fid in (over.get("buttons") or {}).items():      # question-button prefixes -> the feature they trigger
        if fid in cat:
            add(fid, entry={"buttons": [p]})
    # defaults: metric, rare tags, start dates, code size
    dates = _brain_dates()
    first = min(dates.values()) if dates else None
    cl = _changelog_firsts([f"/{fid[3:]}" for fid in cat if fid.startswith("tg.")])
    for fid, f in cat.items():
        f.setdefault("area", "misc")
        f.setdefault("kind", "script")
        f.setdefault("owner_facing", False)
        f.setdefault("notifies", False)
        f.setdefault("menu", False)
        f.setdefault("name", fid)
        f.setdefault("purpose", "")
        f["metric"] = f.get("metric") or METRIC.get(f["kind"], METRIC["script"])
        key = fid.split(".", 1)[-1]
        tags = set(f.get("tags") or [])
        if key in RARE or fid in RARE or any(s in RARE for s in f["entry"].get("scripts", [])):
            tags.add("rare")
        f["tags"] = sorted(tags)
        starts = [dates.get(f"bin/{s}") for s in f["entry"].get("scripts", [])] + \
                 [dates.get(f".claude/skills/{s}/SKILL.md") for s in f["entry"].get("skills", [])]
        starts = [d for d in starts if d]
        if not f.get("start"):
            f["start"] = min(starts) if starts and fid.startswith(("skill.", "bin.")) else cl.get(f"/{fid[3:]}")
            f["start_note"] = "brain first commit (or earlier)" if f["start"] == first else None
        lines = sum(_lines(os.path.join(usage.BIN, s)) for s in f["entry"].get("scripts", [])
                    if not (fid.startswith("tg.") and s == "tg-cmd"))
        f["code_lines"] = (f.get("code_lines") or 0) + lines
    # scripts each tg-cmd command runs directly -> the features owning them are reachable by that command ("via")
    src = texts.get("tg-cmd", "")
    owner_of = {}
    for fid, f in cat.items():
        if not fid.startswith("tg."):
            for s in f["entry"].get("scripts", []):
                owner_of.setdefault(s, set()).add(fid)
    for m in re.finditer(r'^\s+"([a-z_]+)": \("[^"]+", (cmd_[a-z_0-9]+)\)', src, re.M):
        body = re.search(rf"^def {m.group(2)}\(.*?(?=^def |\Z)", src, re.M | re.S)
        runs = sorted({x for x in re.findall(r'\[\s*"([a-z][a-z0-9_-]*)"', body.group(0) if body else "")
                       if x in texts and x != "tg-cmd"})
        if f"tg.{m.group(1)}" in cat and runs:
            cat[f"tg.{m.group(1)}"]["entry"]["runs"] = runs
            for x in runs:
                for fid in owner_of.get(x, ()):
                    via = cat[fid]["entry"].setdefault("via", [])
                    if f"/{m.group(1)}" not in via:
                        via.append(f"/{m.group(1)}")
    idx = {"scripts": {}, "verbs": {}, "buttons": {}, "skills": {}, "jobs": jobs, "voice": {}, "app": {}}
    for fid, f in cat.items():
        e = f["entry"]
        for s in e.get("scripts", []):
            if fid.startswith("tg.") and s == "tg-cmd":
                for v in e.get("verbs", []):
                    idx["verbs"][f"tg-cmd {v}"] = fid
            else:
                idx["scripts"].setdefault(s, fid)
        for b in e.get("buttons", []):
            idx["buttons"].setdefault(b, fid)
        for s in e.get("skills", []):
            idx["skills"][s] = fid
    for s, fid in script_owner.items():
        idx["scripts"][s] = fid
    for b, fid in BUTTON_FAMILY.items():
        if fid in cat:
            idx["buttons"].setdefault(b, fid)
            if b not in cat[fid]["entry"].setdefault("buttons", []):
                cat[fid]["entry"]["buttons"].append(b)        # a button reaches it (review's one-tap check)
    out = {"version": 1, "generated": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
           "features": sorted(cat.values(), key=lambda f: (f["area"], f["id"])), "index": idx}
    if write:
        usage._atomic(usage.CATALOGUE, out)
    return out


def _script_purpose(txt):
    m = re.search(r'^(?:#|""")\s*(?:[\w.-]+\s*(?:[:\-—]|\[[^\]]*\]:?))?\s*(.{12,200})$', txt[:3000], re.M)
    for line in txt.splitlines()[1:12]:
        line = line.strip().lstrip("#").strip().strip('"').strip()
        if len(line) > 15 and not line.startswith(("!", "-*-", "import", "from ", "set -", "usage")):
            return _first_sentence(re.sub(r"^[\w.-]+(\s+\[[^\]]*\])?\s*[:—-]\s*", "", line))
    return _first_sentence(m.group(1)) if m else ""


def _cron_lines():
    out = []
    try:
        lines = open(CRONTAB).read().splitlines()
    except OSError:
        return out
    for line in lines:
        s = line.strip()
        if not s or s.startswith("#") or re.match(r"^[A-Z_]+=", s):
            continue
        parts = s.split(None, 5)
        if s.startswith("@"):
            out.append((parts[0], s.split(None, 1)[1] if len(parts) > 1 else ""))
        elif len(parts) == 6:
            out.append((" ".join(parts[:5]), parts[5]))
    return out


def _job_lines():
    """[(schedule, command, [registry entries])]: every active ~/crontab line with the registry jobs its
    `# assistant:<line>` tag names, then every registry line (core/schedules.toml, any runs_on but retired) that has no
    crontab line: jobs on hub schedules, keepers or planned keep their entries (schedule = the registry's cron,
    command = its cron_line, else its argv). Ordered as the registry (its census order = the crontab's), so the first
    line crediting a feature, which sets its schedule, stays the same when a line leaves cron. An unreadable registry
    = the crontab alone, as before."""
    cron = _cron_lines()
    try:
        from core import schedules as reg
        r = reg.load(SCHEDULES)
    except Exception:
        return [(s, c, []) for s, c in cron]
    live = lambda es: [e for e in es if e.runs_on != "retired"]  # noqa: E731
    rank = {t: i for i, t in enumerate(r.lines)}
    rows, seen, last = [], set(), -1
    for n, (s, c) in enumerate(cron):
        t = reg.TAG_RE.search(c)
        tag = t.group(1) if t else None
        if tag in rank:
            seen.add(tag)
            last = rank[tag]
        rows.append(((last, 0, n), s, c, live(r.lines.get(tag, [])) if tag else []))
    for tag, es in r.lines.items():
        es = live(es)
        if tag in seen or not es:
            continue
        line = es[0].cron_line.strip()
        parts = line.split(None, 5)
        if parts and parts[0].startswith("@") and len(parts) > 1:
            cmd = line.split(None, 1)[1]
        elif len(parts) == 6:
            cmd = parts[5]
        else:
            cmd = " ".join(es[0].argv)
        rows.append(((rank[tag], 1, 0), es[0].cron, cmd, es))
    return [(s, c, es) for _, s, c, es in sorted(rows, key=lambda x: x[0])]


def catalogue():
    j = _json(usage.CATALOGUE)
    return j if isinstance(j, dict) and j.get("features") else build(write=True)


# ---------------------------------------------------------------- analysis

MAINT_VERBS = usage.MAINT_VERBS          # verbs that maintain a feature rather than use it
DIMS = ("hour_band", "weekend", "zone", "driving", "event", "office_day", "screen", "modality", "trigger_ch", "trip")
ERR_OUT = {"error", "fail", "failed", "crash", "timeout"}


def _rows(con, t0, t1):
    return con.execute("SELECT * FROM events WHERE ts >= ? AND ts < ? ORDER BY ts", (t0, t1)).fetchall()


MOD_BY_KIND = {"tap": "button", "react": "reaction", "answer": "poll", "call": "live_call", "invoke": "command"}


def _turns(con, t0):
    """{turn: (label, primary feature)} for labelled owner turns since t0 ({} on a store without the turns table)."""
    try:
        return {r[0]: (r[1], r[2]) for r in con.execute(
            "SELECT turn, label, primary_f FROM turns WHERE ts >= ? AND label IS NOT NULL", (t0,))}
    except Exception:
        return {}


def _use_rows(rows, feats=None, turns=None):
    """(row, attrs, ctx) for owner uses: intent events (a question-button tap credited to the feature its turn was
    about counts for that feature), plus my execs in his turns: with a labelled turn only the turn's primary feature
    and only when the turn is a use (not build / test / maintenance work on it); unlabelled turns (old data) count
    the first exec of each owner-facing feature. Bare ~/bin scripts and notifiers count through their
    notifications, not my runs of them."""
    seen, out, last, turns, gap_last = set(), [], (0, None), turns or {}, {}
    for r in rows:
        a, c = json.loads(r["attrs"] or "{}"), json.loads(r["ctx"] or "{}")
        mod = a.get("modality") or c.get("modality")
        if r["actor"] == "owner" and r["kind"] in ("msg", "reply") and mod:
            last = (r["ts"], mod)
        if r["actor"] == "owner" and r["kind"] in usage.INTENT and _session_end(feats, r):
            continue                                      # a session's end (e.g. off the internet route): no new use
        if r["actor"] == "owner" and r["kind"] in usage.INTENT and _same_session(feats, r, gap_last):
            continue                                      # within session_gap of its last event: the same session
        if r["actor"] == "owner" and r["kind"] in usage.INTENT:
            if not mod and r["kind"] in MOD_BY_KIND:
                c = dict(c, modality=MOD_BY_KIND[r["kind"]])
            if a.get("credit") and r["feature"] in ("session.buttons", "tg.poll"):
                r = dict(r, feature=a["credit"])
            if r["turn"]:
                seen.add((r["turn"], r["feature"]))
            out.append((r, a, c))
        elif r["kind"] == "exec" and c.get("trigger") == "owner" and \
                (feats is None or ((feats.get(r["feature"]) or {}).get("owner_facing") and
                                   (feats.get(r["feature"]) or {}).get("kind") not in ("script", "notifier"))) and \
                a.get("verb") not in MAINT_VERBS and \
                not r["feature"].startswith(("mem.", "arch.", "session.")):
            t = turns.get(r["turn"])
            if t is not None and (t[0] != "use" or t[1] != r["feature"]):
                continue                                  # work on the feature, or spillover from another ask
            key = (r["turn"], r["feature"])
            if key not in seen:
                seen.add(key)
                if not mod and last[1] and 0 <= r["ts"] - last[0] < 900:
                    c = dict(c, modality=last[1])         # my work in his turn inherits how he asked
                out.append((r, a, c))
    return out


def _session_end(feats, r):
    """True for a row that closes a session of a feature whose catalogue names session_end outcomes."""
    ends = ((feats or {}).get(r["feature"]) or {}).get("session_end") or ()
    return bool(r["outcome"]) and r["outcome"] in ends


def _same_session(feats, r, gap_last):
    """True for an owner event less than the feature's catalogue session_gap (seconds) after its previous event
    (sliding: every event extends the session). gap_last = {feature: last ts}, updated here."""
    gap = ((feats or {}).get(r["feature"]) or {}).get("session_gap")
    if not isinstance(gap, (int, float)) or gap <= 0:
        return False
    prev, gap_last[r["feature"]] = gap_last.get(r["feature"]), r["ts"]
    return prev is not None and r["ts"] - prev < gap


def _opened_by(feats):
    """{button prefix: feature} from the catalogue's opened_by lists."""
    return {str(p): fid for fid, f in (feats or {}).items() for p in ((f or {}).get("opened_by") or ())}


def _open_rows(rows, feats):
    """(row, feature) for every notification open: notify_opened rows, plus his taps on a feature's own notification
    buttons (opened_by prefixes; a tap on a message, ref tg:<id>; one open per message and feature)."""
    obm, done = _opened_by(feats), set()
    for r in rows:
        if r["kind"] == "notify_opened":
            yield r, r["feature"]
        elif obm and r["kind"] == "tap" and r["actor"] == "owner" and str(r["ref"] or "").startswith("tg:"):
            p = str(json.loads(r["attrs"] or "{}").get("prefix") or "")
            f = obm.get(p) or obm.get(p.split(":")[0])
            if f and (f, r["ref"]) not in done:
                done.add((f, r["ref"]))
                yield r, f


def _ms_what(f, kind, a):
    """What a use row's ms measures: "latency" (he waited for it; p50 and the slow flag) unless the row or the
    catalogue says otherwise: an app screen visit (usagefwd marks views) carries his dwell on the screen; a catalogue
    "ms" ("session", "call", "decide", ..., or {kind: that}) names a length that is no wait for me."""
    if a.get("view"):
        return "dwell"
    m = (f or {}).get("ms")
    if isinstance(m, dict):
        m = m.get(kind)
    return m if isinstance(m, str) and m else "latency"


def _dur(s):
    if not s["dur"]:
        return None
    v = sorted(s["dur"])
    return {"what": s["dur_what"].most_common(1)[0][0], "n": len(v), "p50": statistics.median(v),
            "p90": v[min(len(v) - 1, int(0.9 * len(v)))]}


def _pct(x, n):
    return round(100.0 * x / n) if n else None


def _hist_q(h, q):
    tot = sum(h)
    if not tot:
        return None
    edges = list(usage.HIST) + [16000]
    acc = 0
    for i, n in enumerate(h):
        acc += n
        if acc >= q * tot:
            return edges[i]
    return edges[-1]


def analyse(days=30, now=None, con=None, cat=None):
    now = time.time() if now is None else now
    cat = cat or catalogue()
    feats = {f["id"]: f for f in cat["features"]}
    con = con or usage.connect(ro=True)
    t_cur, t_prev = now - days * 86400, now - 2 * days * 86400
    rows = _rows(con, t_prev, now)
    allrows_first = {r[0]: r[1] for r in con.execute("SELECT feature, min(ts) FROM events GROUP BY feature")}
    last_any = {r[0]: r[1] for r in con.execute(
        "SELECT feature, max(ts) FROM events WHERE (actor='owner' AND kind IN ('invoke','tap','msg','reply','react',"
        "'answer','call')) OR (kind='exec' AND json_extract(ctx,'$.trigger')='owner') GROUP BY feature")}
    S = collections.defaultdict(lambda: {"uses": 0, "uses_prev": 0, "self": 0, "channels": collections.Counter(),
                                         "ctx": {d: collections.Counter() for d in DIMS}, "sent": 0, "opened": 0,
                                         "dismissed": 0, "ignored": 0, "ignored_active": 0, "open_ms": [],
                                         "errors": 0, "owner_errors": 0, "job_errors": 0, "cmd_errors": 0, "complaints": 0, "praise": 0, "retries": 0, "ms": [],
                                         "dur": [], "dur_what": collections.Counter(),
                                         "prev": collections.Counter(), "sent_bands": collections.Counter(),
                                         "open_bands": collections.Counter(), "since_owner": [], "work": 0})
    turns = _turns(con, t_prev - 86400)
    uses = _use_rows(rows, feats, turns)
    turn_mix = collections.Counter()
    try:
        for lab, prim in con.execute("SELECT label, primary_f FROM turns WHERE ts >= ? AND ts < ? AND label IS NOT NULL",
                                     (t_cur, now)):
            turn_mix[lab] += 1
            if lab != "use" and prim:
                S[prim]["work"] += 1                 # build / test / maintenance turns about this feature
    except Exception:
        pass
    owner_ts = [r["ts"] for r, _, _ in uses if r["actor"] == "owner"]
    last_use = {}
    prev_use = None
    for r, a, c in uses:
        f, cur = r["feature"], r["ts"] >= t_cur
        s = S[f]
        if not cur:
            s["uses_prev"] += 1
            continue
        s["uses"] += 1
        s["channels"][c.get("trigger_ch") or r["channel"] or "?"] += 1
        for d in DIMS:
            v = c.get(d) if d not in ("modality",) else (a.get("modality") or c.get("modality"))
            if v is not None:
                s["ctx"][d][str(v)] += 1
        if isinstance(r["ms"], int) and r["kind"] != "call":
            what = _ms_what(feats.get(f), r["kind"], a)
            if what == "latency":
                s["ms"].append(r["ms"])
            else:                                    # dwell, a call's or session's length, his time to decide
                s["dur"].append(r["ms"])
                s["dur_what"][what] += 1
        lu = last_use.get(f)
        if lu and r["ts"] - lu < 120 and r["actor"] == "owner":
            s["retries"] += 1
        last_use[f] = r["ts"]
        if prev_use and prev_use[1] != f and r["ts"] - prev_use[0] < 1800:
            s["prev"][prev_use[1]] += 1
        before = [t for t in owner_ts if t < r["ts"] - 1]
        if before:
            s["since_owner"].append((r["ts"] - before[-1]) / 60)
        if not f.startswith(("chat.", "tg.reaction", "tg.poll")):
            prev_use = (r["ts"], f)
    # complaints / praise: his next message within 10 min after a use (or after my reply in that turn)
    msgs = [(r["ts"], json.loads(r["attrs"] or "{}").get("fb")) for r in rows
            if r["actor"] == "owner" and r["feature"].startswith("chat.") and r["ts"] >= t_cur]
    for r, a, c in uses:
        if r["ts"] < t_cur or r["feature"].startswith(("chat.", "tg.reaction")):
            continue
        nxt = next(((t, fb) for t, fb in msgs if r["ts"] < t <= r["ts"] + 600), None)
        if nxt and nxt[1] in ("correction", "complaint"):
            S[r["feature"]]["complaints"] += 1
        elif nxt and nxt[1] == "praise":
            S[r["feature"]]["praise"] += 1
    for r in rows:
        if r["ts"] < t_cur:
            continue
        f, k = r["feature"], r["kind"]
        s = S[f]
        band = usage.hour_band(r["hour"]) if r["hour"] is not None else "?"
        if k == "notify_sent":
            s["sent"] += 1
            s["sent_bands"][band] += 1
        elif k == "dismissed":
            s["dismissed"] += 1
        elif k == "ignored":
            s["ignored"] += 1
            s["ignored_active"] += 1 if json.loads(r["attrs"] or "{}").get("active") else 0
        if k == "exec" and r["src"] == "transcript" and (r["outcome"] or "") in ERR_OUT:
            s["cmd_errors"] += 1                     # a non-zero shell exit in my turn: weak signal, not "broken"
        elif (k == "error" or (r["outcome"] or "") in ERR_OUT) and (r["actor"] or "") not in usage.NON_OWNER:
            if _job_row(r):
                s["job_errors"] += 1                 # a background run failing: its own line, never "broken"
            else:
                s["errors"] += 1                     # dev/test runs (actor test/agent/session) never count
            if r["actor"] == "owner" or (r["actor"] is None and r["channel"] in usage.OWNER_CHANNELS):
                s["owner_errors"] += 1               # he got the failure (an instant command's "⚠️ failed")
        if k == "exec" and json.loads(r["ctx"] or "{}").get("trigger") in ("heartbeat", "agent", "system"):
            s["self"] += 1
        if isinstance(r["ms"], int) and r["actor"] == "owner" and _session_end(feats, r):
            what = _ms_what(feats.get(f), k, {})     # a session's end carries its length (not a use, not a wait)
            s["dur"].append(r["ms"])
            s["dur_what"][what if what != "latency" else "session"] += 1
    sends = collections.defaultdict(list)                # per feature, ascending (rows are by ts)
    for r in rows:
        if r["kind"] == "notify_sent":
            sends[r["feature"]].append(r["ts"])
    for r, f in _open_rows(rows, feats):                 # opens: the app's / derive's, plus taps on its own buttons
        if r["ts"] < t_cur:
            continue
        s = S[f]
        s["opened"] += 1
        s["open_bands"][usage.hour_band(r["hour"]) if r["hour"] is not None else "?"] += 1
        ms = r["ms"]
        if r["kind"] != "notify_opened":                 # a credited tap: time since that feature's last send (<= 1 day)
            i = bisect.bisect_right(sends[f], r["ts"]) - 1
            ms = (r["ts"] - sends[f][i]) * 1000 if i >= 0 and r["ts"] - sends[f][i] <= 86400 else None
        if ms is not None:
            s["open_ms"].append(ms)
    runs = collections.defaultdict(lambda: {"n": 0, "fails": 0, "owner_fails": 0, "cron_n": 0, "cron_fails": 0, "ms_sum": 0, "ms_n": 0, "hist": [0] * 7,
                                            "by": collections.Counter()})
    day0 = usage.local(t_cur)[0].date().isoformat()
    for r in con.execute("SELECT * FROM runs WHERE day >= ?", (day0,)):
        x = runs[r["feature"]]
        x["n"] += r["n"]
        if r["actor"] not in usage.NON_OWNER:        # dev/test runs: counted, never failures
            x["fails"] += r["fails"]
        if r["actor"] in ("owner",) + usage.OWNER_CHANNELS:
            x["owner_fails"] += r["fails"]
        elif r["actor"] == "cron":
            x["cron_n"] += r["n"]
            x["cron_fails"] += r["fails"]
        x["ms_sum"] += r["ms_sum"]
        x["ms_n"] += r["ms_n"]
        x["hist"] = [a + b for a, b in zip(x["hist"], json.loads(r["hist"]))]
        x["by"][r["actor"]] += r["n"]
    ids = set(feats) | set(S) | set(runs)
    first_ts = con.execute("SELECT min(ts) FROM events").fetchone()[0]
    coverage = (now - first_ts) / 86400 if first_ts else 0
    out = []
    for fid in ids:
        f = feats.get(fid, {"id": fid, "name": fid, "area": "unknown", "kind": "unknown", "owner_facing": False,
                            "entry": {}, "tags": [], "uncatalogued": True})
        s, x = S.get(fid) or S[fid], runs.get(fid) or runs[fid]
        start = f.get("start") or (dt.date.fromtimestamp(allrows_first[fid]).isoformat() if fid in allrows_first else None)
        age = (now - time.mktime(time.strptime(start, "%Y-%m-%d"))) / 86400 if start else None
        rec = {"id": fid, "name": f.get("name") or fid, "area": f.get("area"), "kind": f.get("kind"),
               "owner_facing": bool(f.get("owner_facing")), "menu": bool(f.get("menu")), "tags": f.get("tags") or [],
               "instrumented": f.get("instrumented"),
               "start": start, "age_days": round(age) if age is not None else None,
               "uses": s["uses"], "uses_prev": s["uses_prev"], "self_uses": s["self"], "work_turns": s["work"],
               "last_used": dt.datetime.fromtimestamp(last_any[fid]).astimezone().isoformat(timespec="minutes")
               if fid in last_any else None,
               "channels": dict(s["channels"]),
               "context": {d: dict(c.most_common(4)) for d, c in s["ctx"].items() if c},
               "notify": {"sent": s["sent"], "opened": s["opened"], "dismissed": s["dismissed"], "ignored": s["ignored"],
                          "ignored_while_active": s["ignored_active"],
                          "open_rate": _pct(s["opened"], s["sent"]),
                          "median_open_min": round(statistics.median(s["open_ms"]) / 60000, 1) if s["open_ms"] else None},
               "errors": s["errors"] + x["fails"] - x["cron_fails"],    # use errors: owner/session calls
               "job_fails": max(s["job_errors"], x["cron_fails"]),     # cron runs (event + run row of one failure)
               "jobs": (f.get("entry") or {}).get("jobs") or [],
               "owner_errors": max(s["owner_errors"], x["owner_fails"]), "cmd_errors": s["cmd_errors"], "complaints": s["complaints"],
               "praise": s["praise"],
               "retries": s["retries"],
               "latency_ms": {"p50": statistics.median(s["ms"]) if s["ms"] else _hist_q(x["hist"], 0.5),
                              "p90": _hist_q(x["hist"], 0.9),
                              "mean": round(x["ms_sum"] / x["ms_n"]) if x["ms_n"] else None},
               "duration_ms": _dur(s),
               "runs": {"n": x["n"], "fails": x["fails"], "cron_n": x["cron_n"], "cron_fails": x["cron_fails"], "per_day": round(x["n"] / days, 1), "by": dict(x["by"])},
               "after": dict(s["prev"].most_common(3)),
               "since_owner_min": round(statistics.median(s["since_owner"]), 1) if s["since_owner"] else None,
               "code_lines": f.get("code_lines") or 0, "purpose": f.get("purpose") or "",
               "metric": f.get("metric") or "", "entry": f.get("entry") or {},
               "uncatalogued": bool(f.get("uncatalogued"))}
        rec["flags"], rec["why"] = _flags(rec, f, s, days, coverage)
        out.append(rec)
    out.sort(key=lambda r: (-r["uses"], -r["notify"]["sent"], r["id"]))
    meta = {"days": days, "now": dt.datetime.fromtimestamp(now).astimezone().isoformat(timespec="minutes"),
            "events": len([r for r in rows if r["ts"] >= t_cur]), "owner_uses": sum(r["uses"] for r in out),
            "coverage_days": round(coverage, 1),
            "turns": dict(turn_mix),
            "sources": {r[0]: dt.datetime.fromtimestamp(r[1]).astimezone().isoformat(timespec="minutes")
                        for r in con.execute("SELECT src, max(ts) FROM events GROUP BY src")},
            "since": (lambda t: dt.datetime.fromtimestamp(t).astimezone().date().isoformat() if t else None)(
                con.execute("SELECT min(ts) FROM events").fetchone()[0])}
    mu = usage._json(usage.MENU_COUNTS, {}) or {}
    if mu.get("updated"):
        try:
            meta["sources"]["menu-usage store"] = dt.datetime.fromisoformat(mu["updated"]).astimezone().isoformat(
                timespec="minutes")
        except ValueError:
            pass
    if "menu-usage" in meta["sources"]:
        meta["sources"]["menu-usage gap-fill"] = meta["sources"].pop("menu-usage")
    return {"meta": meta, "features": out, "catalogue": feats}


def _job_row(r):
    """A background job's row (cron, or a cron-channel row without an actor), not a use."""
    return r["actor"] == "cron" or (r["actor"] is None and r["channel"] == "cron")


def _flags(r, f, s, days, coverage=None):
    flags, why = [], []
    of, rare = r["owner_facing"], "rare" in r["tags"]
    if "retired" in r["tags"]:
        return flags, why                            # out of the menu on purpose: no unused / broken verdicts
    n = r["notify"]
    if of and not rare and r["uses"] == 0 and not n["sent"]:
        if r["age_days"] is not None and r["age_days"] < days:
            flags.append("untried")
        elif coverage is not None and coverage < days:
            flags.append("idle")                     # not used in the data we have (< a full window): no verdict yet
        elif r["uses_prev"] == 0:
            flags.append("unused")
        else:
            flags.append("stopped")
    if r["uses_prev"] >= 4 and r["uses"] <= r["uses_prev"] / 2:
        flags.append("declining")
    if n["sent"] >= 5 and (n["open_rate"] or 0) < 20 and n["ignored"] >= 0.5 * n["sent"]:
        flags.append("noisy")
    rn = r["runs"]
    use_n = rn["n"] - rn.get("cron_n", 0)            # job (cron) runs and their failures: the review's jobs line
    total = max(1, r["uses"] + use_n)
    if of:   # owner-facing: only failures he met (instant command: 1 is enough)
        instant = r["kind"] == "command" and "tg-cmd" in (r["entry"].get("scripts") or [])
        if (instant and r["owner_errors"] >= 1) or r["owner_errors"] >= 2:
            flags.append("broken")
    elif (use_n >= 10 and r["errors"] / use_n >= 0.2) or (r["errors"] >= 2 and r["errors"] / total >= 0.3):
        flags.append("broken")
    p50 = r["latency_ms"]["p50"]
    if of and p50 and p50 > (8000 if r["kind"] == "skill" else 3000) and r["uses"] >= 2:
        flags.append("slow")
    if r["kind"] != "channel" and (r["complaints"] >= 3 or (r["complaints"] >= 2 and r["complaints"] / max(1, r["uses"]) >= 0.25)):
        flags.append("friction")
    if not any(x in flags for x in ("unused", "stopped", "idle", "untried", "declining", "noisy", "friction")):
        return flags, why
    e = r["entry"]
    if not e.get("commands") and not e.get("buttons") and not e.get("via") \
            and r["kind"] in ("skill", "voice-tool", "app", "notifier") and not r["menu"]:
        why.append("discoverability: no menu command or button; reachable only by asking or by its trigger")
    elif e.get("commands") and not r["menu"]:
        why.append("discoverability: has a command but it is not in the / menu")
    if r["kind"] != "channel" and (r["retries"] >= 2 or (p50 and p50 > 3000) or f.get("args")):
        why.append(f"friction: {'needs an argument; ' if f.get('args') else ''}"
                   f"{'p50 ' + str(round(p50 / 1000, 1)) + ' s; ' if p50 else ''}{r['retries']} quick retries")
    if r["complaints"]:
        why.append(f"friction: {r['complaints']} correction/complaint message(s) right after use")
    if n["sent"] and s["sent_bands"]:
        band, cnt = s["sent_bands"].most_common(1)[0]
        best = s["open_bands"].most_common(1)[0][0] if s["open_bands"] else None
        if cnt / n["sent"] >= 0.5 and (best is None or best != band):
            why.append(f"wrong time: {round(100 * cnt / n['sent'])} % sent in the {band}"
                       f"{', opened mostly in the ' + best if best else ', none opened'}")
        if n["ignored_while_active"] >= max(2, n["ignored"] // 2):
            why.append(f"wrong channel or content: {n['ignored_while_active']} ignored while he was active elsewhere")
    if r["channels"] and len(r["channels"]) == 1 and e.get("commands") and "voice" in r["channels"]:
        why.append("wrong channel: used only by voice, never from the menu")
    if "declining" in flags or "stopped" in flags:
        why.append("superseded? check newer features in the same area: " + r["area"])
    why.append("need: ask him whether he still wants it (it may solve a rare problem)")
    return flags, why


# ---------------------------------------------------------------- views

def label(r):
    i = r["id"]
    if i.startswith("tg.") and r.get("kind") in ("command", "session-command", None):
        return "/" + i[3:]
    if i.startswith("skill."):
        return i[6:]
    if i.startswith("voice.tool."):
        return "voice " + i[11:].replace("_", " ")
    if i.startswith("app.tile."):
        return "app " + i[9:]
    if i.startswith("bin."):
        return i[4:]
    return r.get("name") or i


def text_report(an, top=40, show_all=False):
    m, out = an["meta"], []
    out.append(f"Usage {m['days']} days to {m['now']} (data since {m['since']}): {m['events']} events, "
               f"{m['owner_uses']} owner uses")
    if m.get("turns"):
        out.append("Owner turns: " + ", ".join(f"{k} {v}" for k, v in sorted(m["turns"].items(), key=lambda kv: -kv[1]))
                   + "  (work = build/test/maint turns about a feature: not counted as uses)")
    out.append(f"{'feature':34} {'uses':>5} {'prev':>5} {'work':>5} {'sent':>5} {'open%':>5} {'err':>4} {'p50ms':>6} "
               f"{'runs/d':>6}  flags")
    rows = [r for r in an["features"] if show_all or r["uses"] or r["notify"]["sent"] or r["flags"] or r["errors"]
            or r.get("work_turns")]
    for r in rows[:top] if not show_all else rows:
        n = r["notify"]
        out.append(f"{label(r)[:34]:34} {r['uses']:>5} {r['uses_prev']:>5} {r.get('work_turns') or '':>5} {n['sent'] or '':>5} "
                   f"{n['open_rate'] if n['open_rate'] is not None else '':>5} {r['errors'] or '':>4} "
                   f"{r['latency_ms']['p50'] or '':>6} {r['runs']['per_day'] or '':>6}  {','.join(r['flags'])}")
    flagged = [r for r in an["features"] if r["why"]]
    if flagged:
        out.append("\nWhy not used (hypotheses):")
        for r in flagged[:25]:
            out.append(f"- {label(r)} [{','.join(r['flags'])}]: " + " | ".join(r["why"][:3]))
    app = [r for r in an["features"] if r.get("area") == "app" and r["owner_facing"]]
    if app:
        n = lambda r: r["uses"] or r["notify"]["sent"]
        used = sorted((r for r in app if n(r)), key=lambda r: -n(r))
        idle = [r for r in app if not n(r)]
        out.append(f"\nPhone app: {len(used)} of {len(app)} surfaces used" +
                   (": " + ", ".join(f"{label(r)} {n(r)}" for r in used[:12]) if used else ""))
        if idle:
            out.append("  not used yet: " + ", ".join(label(r) for r in idle[:30]) + (f" +{len(idle) - 30}" if len(idle) > 30 else ""))
    out.append("\nSources (last event): " + ", ".join(f"{k} {v[5:16]}" for k, v in sorted(m["sources"].items())))
    return "\n".join(out)


def slice_by(con, dim, feature=None, days=30, now=None):
    """Uses (and notifications) of one feature or all, grouped by any context dimension or core column."""
    now = time.time() if now is None else now
    rows = _rows(con, now - days * 86400, now)
    feats = {f["id"]: f for f in catalogue()["features"]}
    uses = _use_rows(rows, feats)
    col = dim in ("channel", "kind", "hour", "dow", "zone", "src", "actor", "feature")
    cnt, opened, sent = collections.Counter(), collections.Counter(), collections.Counter()

    def val(r, a, c):
        return r[dim] if col else (c.get(dim) if dim in c else a.get(dim))
    for r, a, c in uses:
        if feature and r["feature"] != feature:
            continue
        cnt[str(val(r, a, c))] += 1
    for r, f in [(r, r["feature"]) for r in rows if r["kind"] == "notify_sent"] + list(_open_rows(rows, feats)):
        if feature and f != feature:
            continue
        a, c = json.loads(r["attrs"] or "{}"), json.loads(r["ctx"] or "{}")
        (sent if r["kind"] == "notify_sent" else opened)[str(val(r, a, c))] += 1
    keys = sorted(set(cnt) | set(sent), key=lambda k: -(cnt[k] + sent[k]))
    tot = sum(cnt.values()) or 1
    lines = [f"{feature or 'all features'} by {dim} ({days} days)", f"{'value':24} {'uses':>5} {'share':>6} {'sent':>5} {'opened':>6}"]
    for k in keys:
        lines.append(f"{k[:24]:24} {cnt[k]:>5} {round(100 * cnt[k] / tot):>5}% {sent[k] or '':>5} {opened[k] or '':>6}")
    return "\n".join(lines), {"dim": dim, "feature": feature, "uses": dict(cnt), "sent": dict(sent), "opened": dict(opened)}


def card(days=30, now=None):
    try:
        an = analyse(days, now)
    except Exception as e:
        return f"📊 Usage: no data yet ({type(e).__name__})."
    fs = an["features"]
    owner = [r for r in fs if r["uses"] and not r["id"].startswith(("chat.", "tg.reaction", "tg.poll", "session."))]
    top = " · ".join(f"{label(r)} {r['uses']}" for r in owner[:5]) or "none yet"
    unused = [label(r) for r in fs if {"unused", "stopped", "idle"} & set(r["flags"]) and r["menu"]]
    cov = an["meta"].get("coverage_days") or 0
    untried = [label(r) for r in fs if "untried" in r["flags"] and r["menu"]]
    noisy = [f"{label(r)} {r['notify']['opened']}/{r['notify']['sent']} opened" for r in fs if "noisy" in r["flags"]]
    broken = [label(r) for r in fs if "broken" in r["flags"] and r["owner_facing"]]
    sent = sum(r["notify"]["sent"] for r in fs)
    opened = sum(r["notify"]["opened"] + r["notify"]["dismissed"] for r in fs)
    lines = [f"📊 Usage · last {days} days (since {an['meta']['since']})",
             f"Top: {top}",
             (f"Unused {days} d: " if cov >= days else f"Not used in {round(cov)} d of data: ")
             + (', '.join(unused[:6]) or 'none') + (f" +{len(unused) - 6}" if len(unused) > 6 else ""),
             f"Not tried yet: {', '.join(untried[:5]) or 'none'}" + (f" +{len(untried) - 5}" if len(untried) > 5 else ""),
             f"Noisy: {'; '.join(noisy[:3]) or 'none'}",
             f"Broken: {', '.join(broken[:4]) or 'none'}",
             f"Unprompted messages: {sent}, {round(100 * opened / sent) if sent else 0} % got a tap, reply or reaction",
             "Details: ask me for the product report"]
    return "\n".join(lines)


# ---------------------------------------------------------------- weekly review

def _ledger():
    try:
        txt = open(os.path.join(WORKSPACE, "FACTS.md"), errors="replace").read()
    except OSError:
        return []
    part = txt.split("## Done / declined ledger", 1)[-1]
    return [ln.strip() for ln in part.splitlines() if ln.startswith("- ")]


def _ledger_hit(r, ledger):
    words = {label(r).lstrip("/").lower(), r["id"].split(".")[-1].lower()}
    for ln in ledger:
        low = ln.lower()
        if any(len(w) > 3 and re.search(r"(^|[\s/(])" + re.escape(w) + r"($|[\s),:])", low) for w in words):
            return ln[:110]
    return None


def security_posture(max_age_days=8):
    """security-watch's one-line weekly posture (posture.txt, rewritten each daily run); None when stale/missing."""
    p = SECURITY_POSTURE
    try:
        if time.time() - os.path.getmtime(p) > max_age_days * 86400:
            return None
        with open(p) as f:
            return f.read().strip()[:300] or None
    except OSError:
        return None


def review(days=30, now=None, write=True):
    now = time.time() if now is None else now
    an = analyse(days, now)
    fs, ledger = an["features"], _ledger()
    day = dt.datetime.fromtimestamp(now).astimezone().date().isoformat()
    keep = [r for r in fs if r["owner_facing"] and r["uses"] >= 3 and not set(r["flags"]) & {"broken", "noisy", "friction"}]
    improve = sorted([r for r in fs if set(r["flags"]) & {"broken", "slow", "noisy", "friction", "declining"}
                      and (r["owner_facing"] or "broken" in r["flags"])], key=lambda r: (-r["uses"], -r["errors"]))
    retire = []
    for r in fs:
        if not ({"unused", "stopped"} & set(r["flags"]) or ("noisy" in r["flags"] and (r["notify"]["open_rate"] or 0) < 10)):
            continue
        hit = _ledger_hit(r, ledger)
        if hit and re.search(r"\b(kept|decided)\b", hit):
            continue
        retire.append((r, hit))
    build, onetap = [], 0
    for r in fs:
        sess = r["channels"].get("telegram", 0) + r["channels"].get("terminal", 0)
        e = r["entry"]
        if r["kind"] in ("skill", "script", "notifier") and sess >= 5 and onetap < 5 \
                and not e.get("commands") and not e.get("via") and not e.get("buttons"):
            onetap += 1
            build.append((r, f"asked {sess}x by message in {days} d and no / command or button reaches it: "
                             "check which repeated asks a one-tap entry could serve"))
        if r["id"].startswith("voice.tool.") and r["uses"] >= 3:
            build.append((r, f"used {r['uses']}x by voice: check the Telegram/app equivalent is as quick"))
        for d in (() if r["id"].startswith("bin.") or r["kind"] == "channel" else ("driving", "event", "zone", "hour_band")):
            c = r["context"].get(d) or {}
            if r["uses"] >= 5 and c:
                k, v = max(c.items(), key=lambda kv: kv[1])
                if v / r["uses"] >= 0.6 and k not in ("False", "none", "?"):
                    build.append((r, f"{round(100 * v / r['uses'])} % of uses with {d}={k}: offer it proactively there"))
                    break
    quiet = [r for r in fs if r["owner_facing"] and not r["uses"] and not r["notify"]["sent"] and not r["runs"]["n"]]
    # no source sees them yet; app features the app reports itself (catalogue `instrumented`) are only unused
    uncovered = [r for r in quiet if r["kind"] == "app" and not r.get("instrumented")]
    m = an["meta"]
    cov = (dt.date.fromtimestamp(now) - dt.date.fromisoformat(m["since"])).days if m.get("since") else 0
    unseen = [r for r in quiet if r["kind"] in ("command", "skill", "voice-tool")] if cov < days else []
    L = [f"# Product review {day} ({days}-day window)", "",
         f"Data since {m['since']}: {m['events']} events, {m['owner_uses']} owner uses"
         + (" (owner turns: " + ", ".join(f"{k} {v}" for k, v in sorted((m.get("turns") or {}).items(),
                                                                       key=lambda kv: -kv[1])) + ")"
            if m.get("turns") else "") + ". "
         "Uses = his taps/commands/messages plus the one feature each use turn was about; build, test and "
         "maintenance turns on a feature are not uses. "
         "Evidence = usage store (~/bin/usage, `product report`). Hypotheses are guesses until he confirms.", "",
         "## Keep (used, healthy)"]
    L += [f"- {label(r)}: {r['uses']} uses" + (f" (prev {r['uses_prev']})" if cov >= 2 * days else "")
          + f", last {(r['last_used'] or '')[:16]}" for r in keep[:12]] or ["- none yet"]
    L += ["", "## Improve (ranked by use)"]
    for r in improve[:15]:
        n = r["notify"]
        ev = [f"{r['uses']} uses", f"errors {r['errors']}" if r["errors"] else "",
              f"opened {n['opened']}/{n['sent']}" if n["sent"] else "", f"p50 {r['latency_ms']['p50']} ms" if r["latency_ms"]["p50"] else "",
              f"complaints {r['complaints']}" if r["complaints"] else ""]
        L.append(f"- {label(r)} [{','.join(r['flags'])}]: {', '.join(x for x in ev if x)}"
                 + (f" — {r['why'][0]}" if r["why"] else ""))
    if not improve:
        L.append("- none")
    jobs = sorted([r for r in fs if r["job_fails"]], key=lambda r: (-r["job_fails"], r["id"]))
    L += ["", "## Jobs (failed background runs; not use errors, never \"broken\")"]
    L += [f"- jobs: {', '.join(r['jobs']) or label(r).removeprefix('job.')} {r['job_fails']} failed runs"
          + (f" of {r['runs']['cron_n']}" if r["runs"]["cron_n"] >= r["job_fails"] else "")
          + (f" ({label(r)})" if r["jobs"] else "") for r in jobs[:15]] or ["- none"]
    L += ["", "## Retire candidates (pitch to him; never remove without his OK)"]
    for r, hit in retire[:15]:
        L.append(f"- {label(r)} [{','.join(r['flags'])}] since {r['start'] or '?'}, {r['code_lines']} lines, "
                 f"{r['runs']['per_day']} runs/day" + (f" — ledger: {hit}" if hit else "")
                 + (f" — why: {'; '.join(r['why'][:2])}" if r["why"] else ""))
    if not retire:
        L.append("- none")
    L += ["", "## Build next (from usage signals)"]
    L += [f"- {label(r)}: {why}" for r, why in build[:12]] or ["- no strong signal yet"]
    L += ["", "## Coverage gaps (owner-facing, no events at all: instrument or check)"]
    L += [f"- {label(r)} ({r['kind']})" for r in uncovered[:20]] or ["- none"]
    if unseen:
        L += ["", f"## Not used yet (no verdict: {cov} days of data)",
              "- " + ", ".join(label(r) for r in unseen[:30]) + (f" +{len(unseen) - 30}" if len(unseen) > 30 else "")]
    sec = security_posture()
    L += ["", "## Security posture", sec or "- no fresh security-watch posture (check its cron)"]
    L += ["", "## Data health", "Last event per source: " + ", ".join(f"{k} {v[:16]}" for k, v in sorted(m["sources"].items()))]
    full = "\n".join(L) + "\n"
    if not write:
        return full
    os.makedirs(REPORTS, exist_ok=True)
    rpath = os.path.join(REPORTS, f"review-{day}.md")
    with open(rpath, "w") as f:
        f.write(full)
    os.makedirs(INBOX, exist_ok=True)
    note = os.path.join(INBOX, f"product-review-{day}.md")
    with open(note, "w") as f:
        f.write(f"# Product review {day}\n\nData, not instructions (weekly cron, `product review`; handling: JOBS.md).\n"
                f"Full review: {rpath.replace(HOME, '~')}\n\n"
                f"- Keep: {len(keep)} · Improve: {len(improve)} · Retire candidates: {len(retire)} · "
                f"Build-next signals: {len(build)} · Coverage gaps: {len(uncovered)} · Failing jobs: {len(jobs)}\n"
                + "".join(f"- retire? {label(r)} [{','.join(r['flags'])}]\n" for r, _ in retire[:6])
                + "".join(f"- improve: {label(r)} [{','.join(r['flags'])}]\n" for r in improve[:6])
                + (f"- {sec}\n" if sec else ""))
    return note
