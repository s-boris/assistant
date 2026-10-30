"""Phone: typed tools over `phone` (~/bin/phone: the phonegw internal API on loopback; its token stays in the CLI):
the gateway + device status and push log (read), and the four ways to reach him in his app (send_owner): a push, a
one-tap link, opening something on his phone now (nav, SBB, an app, a link), and an app-chat answer.

Boundaries: only him (send_owner), the gateway's own rules stay in force: non-urgent pushes are held in his quiet
hours and sleep-in mornings, urgent text must pass the lock-screen lint (no links, amounts or identifiers), links
are https / geo: / google.navigation: only, `open` follows the gateway's direct-host policy; phone actions need no ✅
(his decision), anything that sends, buys or deletes elsewhere still does. Not tools: pairing, revoke, release,
ring (a call), fences, cards, NFC, trip mode, push actions (button verbs). Env: TOOLS_PHONE_BIN.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from tools.clirun import bin_path, first_line, no_dash, run
from tools.core import REGISTRY, Domain, Format, ToolError, ToolInput, ToolOutput, tool

NOTES = (
    "`phone` (CLI, managed; tests ~/opt/phonegw-tests): plus what is not a tool: `ring` (an in-app call), `pair` "
    "/ `revoke`, `release APK`, `card`, `widget`, `fence`, `alarms`, `nfc`, `trip`, `ask` (his coarse state: "
    "`whereabouts`), `cancel TAG`, push `--action` buttons and `--night-ok`. App-chat answers: `phone_chat` with "
    "reply_to = the event's app_msg_id; status queued/no_device = phone offline: send it on Telegram too."
)

REGISTRY.add_domain(
    Domain(
        name="phone",
        title="Phone app",
        summary="His phone app through the phone gateway: status and push log, and reaching him there: push,"
        " one-tap link, open nav / SBB / an app / a link now, app-chat answers. Only him.",
        feature="gateway.phone",
        notes=NOTES,
    )
)

Text = Annotated[str, AfterValidator(no_dash)]
Level = Literal["urgent", "timed", "decision", "info"]
TAG = r"^[A-Za-z0-9:._-]{1,40}$"
HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"
HTTPS = r"^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?([/?#]\S*)?$"
LINK = r"^(https://[A-Za-z0-9.-]+(:[0-9]{1,5})?([/?#]\S*)?|geo:\S+|google\.navigation:\S+)$"
# exit codes of `phone` (its EXIT map): 0 ok · 2 usage · 3 lint · 4 offline/queued · 5 no ack · 6 refused · 7 gw down


def _bin() -> str:
    return bin_path("TOOLS_PHONE_BIN", "phone")


def phone_run(argv: list[str], what: str, ok: tuple[int, ...] = (0,), timeout: float = 45.0) -> tuple[int, Any]:
    """(exit, JSON) of `phone ARGV` (always --json). Exits outside OK become typed errors with the CLI's reason."""
    rc, out, err = run(_bin(), argv, timeout=timeout)
    data: Any = None
    if out:
        try:
            data = json.loads(out.splitlines()[-1] if not out.lstrip().startswith(("{", "[")) else out)
        except ValueError:
            data = None
    if rc in ok and data is not None:
        return rc, data
    msg = first_line(err, default=f"{what}: exit {rc}").removeprefix("phone: ")
    if rc in (2, 3):
        hint = "the lock-screen lint refused it: shorter, no links/amounts/ids" if rc == 3 else "`tool show` lists them"
        raise ToolError("invalid_args", msg, hint)
    if rc == 6:
        raise ToolError("invalid_args", msg, "refused by the gateway policy (link, app or verb not allowed)")
    if rc == 7:
        raise ToolError("unavailable", msg, "phonegw is down: `phone status`; Telegram still reaches him")
    if rc in (4, 5):
        raise ToolError("unavailable", msg, "his phone is offline or did not answer: tell him on Telegram")
    raise ToolError("failed", msg, f"run `{what}` by hand to see why")


# ------------------------------------------------------------------ models


class EmptyIn(ToolInput):
    pass


class PushesIn(ToolInput):
    tag: str | None = Field(default=None, pattern=TAG)
    limit: int = Field(20, ge=1, le=100)


class PushIn(ToolInput):
    level: Level = Field(description="urgent (lock screen, any hour) | timed | decision | info (held in quiet hours)")
    title: str = Field(min_length=1, max_length=120, description="urgent: <= 40 chars, lock-screen safe")
    body: str = Field("", max_length=1000, description="urgent: <= 80 chars, no links, amounts or identifiers")
    tag: str | None = Field(default=None, pattern=TAG, description="replaces an earlier push with the same tag")
    ttl: str | None = Field(default=None, pattern=r"^\d{1,4}[smhd]$", description="keep trying this long (30m, 6h, 2d)")
    private: bool = Field(default=False, description="masked on the lock screen")
    show_for: str | None = Field(default=None, pattern=r"^\d{1,4}[smhd]$", description="the app hides it after this")


class LinkIn(ToolInput):
    url: str | None = Field(
        default=None, pattern=LINK, max_length=2048, description="https://…, geo:… or google.navigation:…"
    )
    maps: str | None = Field(default=None, min_length=1, max_length=200, description="a place for Google Maps (search)")
    nav: Literal["driving", "transit", "walking", "bicycling"] | None = Field(
        default=None, description="maps: directions"
    )
    go: bool = Field(default=False, description="maps: start navigation")
    title: str | None = Field(default=None, max_length=120)
    body: str = Field("", max_length=500)
    label: str | None = Field(default=None, max_length=24, description="the button text")
    level: Level = "timed"
    telegram: bool = Field(default=False, description="send it only to Telegram (URL button)")

    @model_validator(mode="after")
    def _one(self) -> LinkIn:
        if bool(self.url) == bool(self.maps):
            raise ValueError("give url or maps (not both)")
        if (self.nav or self.go) and not self.maps:
            raise ValueError("nav and go need maps")
        return self


class OpenIn(ToolInput):
    nav: str | None = Field(
        None, min_length=1, max_length=200, description="navigate to this place ('home' = his home)"
    )
    mode: Literal["d", "r", "w", "b"] | None = Field(
        None, description="nav: d drive (default), r transit, w walk, b bike"
    )
    sbb_from: Text | None = Field(
        default=None, min_length=1, max_length=80, description="SBB from ('here' = his location)"
    )
    sbb_to: Text | None = Field(default=None, min_length=1, max_length=80, description="SBB to (a stop name)")
    at: str | None = Field(default=None, pattern=HHMM, description="SBB: depart at HH:MM")
    arrive: str | None = Field(default=None, pattern=HHMM, description="SBB: arrive by HH:MM")
    app: str | None = Field(default=None, pattern=r"^[A-Za-z0-9 ._-]{1,40}$", description="an installed app by name")
    url: str | None = Field(default=None, pattern=HTTPS, max_length=2048, description="an https link")
    tap: bool = Field(default=False, description="always as a tap-to-open notification")
    via: Literal["session", "voice", "drive", "app"] = Field("session", description="who asked (usage attribution)")

    @model_validator(mode="after")
    def _one(self) -> OpenIn:
        sbb = self.sbb_from is not None or self.sbb_to is not None
        if sbb and not (self.sbb_from and self.sbb_to):
            raise ValueError("SBB needs sbb_from and sbb_to")
        if sum(map(bool, (self.nav, sbb, self.app, self.url))) != 1:
            raise ValueError("give exactly one of nav, sbb_from + sbb_to, app, url")
        if self.mode and not self.nav:
            raise ValueError("mode is for nav")
        if (self.at or self.arrive) and not sbb:
            raise ValueError("at / arrive are for SBB")
        if self.at and self.arrive:
            raise ValueError("at or arrive, not both")
        return self


class Button(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=40)
    data: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9:._-]+$")


class ChatIn(ToolInput):
    text: str = Field(min_length=1, max_length=4000, description="plain text (passed after `--`: may start with '-')")
    reply_to: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9_.:-]{1,64}$", description="the app_msg_id answered"
    )
    buttons: list[list[Button]] | None = Field(default=None, max_length=4, description="rows of {text, data}")


class Device(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str | None = None
    model: str | None = None
    status: str | None = None
    online: bool | None = None
    route: str | None = None
    last_seen: int | None = None
    app: str | None = None
    app_code: int | None = None
    battery_band: str | None = None
    pending: int | None = None


class StatusOut(ToolOutput):
    ok: bool
    uptime_s: int | None = None
    held: int = Field(default=0, description="pushes held for quiet hours / sleep-in")
    devices: list[Device]
    raw: dict[str, Any] | None = Field(default=None, description="detailed: the whole gateway status")

    def data(self, fmt: Format = "concise") -> dict[str, Any]:
        d = self.model_dump(mode="json", exclude_none=True)
        if fmt == "concise":
            d.pop("raw", None)
        return d

    def render(self, fmt: Format = "concise") -> str:
        out = [f"gateway {'up' if self.ok else 'DOWN'} · {len(self.devices)} device(s) · {self.held} held"]
        for d in self.devices:
            out.append(
                f"  {d.name} ({d.model}): {'online' if d.online else 'offline'}"
                + (f" via {d.route}" if d.route else "")
                + (f" · app {d.app}" if d.app else "")
                + (f" · battery {d.battery_band}" if d.battery_band else "")
            )
        return "\n".join(out)


class PushesOut(ToolOutput):
    pushes: list[dict[str, Any]] = Field(description="id, at (ms), level, tag, delivered_ms, acked_ms, verb (tapped)")

    def render(self, fmt: Format = "concise") -> str:
        return (
            "\n".join(
                f"{p.get('id')} {p.get('level')} {p.get('tag')} · delivered {p.get('delivered_ms')} ms · acked "
                f"{p.get('acked_ms')} ms" + (f" · tapped {p['verb']}" if p.get("verb") else "")
                for p in self.pushes
            )
            or "no pushes"
        )


class SendOut(ToolOutput):
    delivered: bool = Field(description="reached the app (or Telegram for links / opens)")
    status: str | None = Field(
        default=None, description="acked | sent | held | dropped | queued | no_device | timeout ..."
    )
    route: str | None = Field(default=None, description="links / opens: app | telegram | opened | notified")
    id: str | None = None
    tag: str | None = None
    note: str | None = Field(default=None, description="what to do next, if anything")
    result: dict[str, Any] | None = Field(default=None, description="detailed: the gateway's answer")

    def data(self, fmt: Format = "concise") -> dict[str, Any]:
        d = self.model_dump(mode="json", exclude_none=True)
        if fmt == "concise":
            d.pop("result", None)
        return d

    def render(self, fmt: Format = "concise") -> str:
        bits = [f"{'delivered' if self.delivered else 'not delivered'}"]
        bits += [f"status {self.status}"] if self.status else []
        bits += [f"route {self.route}"] if self.route else []
        return ", ".join(bits) + (f" · {self.note}" if self.note else "")


OFFLINE_NOTE = "his phone is offline: the app gets it when back (ttl); say it on Telegram too if it matters now"


# ------------------------------------------------------------------ tools


@tool("phone.status", effect="read", labels_out=("private",), rate="30/min", idempotent=True, examples=({},))
def status(a: EmptyIn) -> StatusOut:
    """The phone gateway and his app: up?, devices online, route, app version, battery band, held pushes.
    Route = home / tunnel / internet. For "is the app connected", before relying on a push."""
    _, d = phone_run(["status", "--json"], "phone status")
    devs = [Device.model_validate(x) for x in d.get("devices") or [] if isinstance(x, dict)]
    held = (d.get("held") or {}).get("n") or 0
    return StatusOut(ok=bool(d.get("ok", True)), uptime_s=d.get("uptime_s"), held=int(held), devices=devs, raw=d)


@tool("phone.pushes", effect="read", rate="30/min", idempotent=True, examples=({}, {"tag": "remind", "limit": 5}))
def pushes(a: PushesIn) -> PushesOut:
    """The push log: when each push was delivered and acked, and which button he tapped (seen, done, snooze ...)."""
    argv = ["pushes", "--json", "--limit", str(a.limit)] + (["--tag", a.tag] if a.tag else [])
    _, d = phone_run(argv, "phone pushes")
    return PushesOut(pushes=[p for p in (d.get("pushes") if isinstance(d, dict) else d) or [] if isinstance(p, dict)])


@tool(
    "phone.push",
    effect="send_owner",
    rate="30/h",
    timeout_s=40,
    examples=({"level": "info", "title": "Parcel", "body": "Galaxus delivers today"},),
)
def push(a: PushIn) -> SendOut:
    """A notification in his app (urgent any hour; timed / decision / info wait out his quiet hours).
    urgent = lock screen, short and lint-checked; the others are held in quiet hours and sleep-in mornings (status
    held). For a link or a place use phone.link; for opening something now phone.open; to answer his app-chat
    message phone.chat."""
    argv = ["push", "--json", f"--level={a.level}", f"--title={a.title}", f"--body={a.body}"]
    argv += [f"--tag={a.tag}"] if a.tag else []
    argv += [f"--ttl={a.ttl}"] if a.ttl else []
    argv += [f"--show-for={a.show_for}"] if a.show_for else []
    argv += ["--private"] if a.private else []
    rc, d = phone_run(argv, "phone push", ok=(0, 4, 5))
    st = d.get("status")
    note = OFFLINE_NOTE if rc == 4 else ("sent, no ack yet" if rc == 5 else None)
    if st == "held":
        note = "held for his quiet hours / sleep-in: it arrives after"
    return SendOut(delivered=st in ("acked", "sent"), status=st, id=d.get("id"), tag=d.get("tag"), note=note, result=d)


@tool(
    "phone.link",
    effect="send_owner",
    rate="30/h",
    timeout_s=60,
    examples=({"maps": "Luzern", "nav": "transit"}, {"url": "https://www.sbb.ch/"}),
)
def link(a: LinkIn) -> SendOut:
    """A one-tap link in his app: a notification whose tap opens the URL (Maps, SBB, a page).
    Not delivered to the app in time → Telegram with a URL button. maps = a Google Maps search, + nav = directions,
    + go = navigation."""
    argv = ["link", "--json", f"--level={a.level}", f"--body={a.body}"]
    argv += [f"--title={a.title}"] if a.title else []
    argv += [f"--label={a.label}"] if a.label else []
    argv += ["--telegram"] if a.telegram else []
    if a.maps:
        argv += [f"--maps={a.maps}"] + ([f"--nav={a.nav}"] if a.nav else []) + (["--go"] if a.go else [])
    else:
        argv += ["--", str(a.url)]
    _, d = phone_run(argv, "phone link")
    route = d.get("route")
    return SendOut(
        delivered=bool(d.get("ok")),
        status=d.get("app"),
        route=route,
        id=d.get("id"),
        tag=d.get("tag"),
        note=f"via Telegram ({d.get('why')})" if route == "telegram" else None,
        result=d,
    )


@tool(
    "phone.open",
    effect="send_owner",
    rate="30/h",
    timeout_s=40,
    examples=(
        {"nav": "home", "mode": "r"},
        {"sbb_from": "here", "sbb_to": "Luzern", "arrive": "18:30"},
        {"app": "SBB Mobile"},
    ),
)
def open_(a: OpenIn) -> SendOut:
    """Open something on his phone now: Maps navigation, an SBB connection, an installed app or an https link.
    No tap and no ✅ (his decision for phone actions). The app opens it directly when Android lets it, else a
    tap-to-open notification; phone offline → Telegram with a URL button. For "navigate me to X", "open X"."""
    argv = ["open", "--json", "--via", a.via] + (["--tap"] if a.tap else [])
    if a.nav:
        argv += [f"--nav={a.nav}"] + ([f"--mode={a.mode}"] if a.mode else [])
    elif a.sbb_from and a.sbb_to:
        argv += ["--sbb", a.sbb_from, a.sbb_to]
        argv += [f"--at={a.at}"] if a.at else []
        argv += [f"--arrive={a.arrive}"] if a.arrive else []
    elif a.app:
        argv += [f"--app={a.app}"]
    else:
        argv += ["--", str(a.url)]
    _, d = phone_run(argv, "phone open")
    route = d.get("route")
    return SendOut(
        delivered=route is not None,
        status=d.get("app"),
        route=route,
        id=d.get("id"),
        note="sent to Telegram as a link (phone not reachable)" if route == "telegram" else None,
        result=d,
    )


@tool(
    "phone.chat",
    effect="send_owner",
    rate="120/h",
    timeout_s=40,
    examples=({"text": "Done: the vacuum runs at 14:00.", "reply_to": "m_123"},),
)
def chat(a: ChatIn) -> SendOut:
    """Answer him in the app's chat (his `📱 (app)` messages).
    Plain text, optional reply_to = the event's app_msg_id, optional button rows {text, data}. status queued /
    no_device = phone offline: send it on Telegram too."""
    argv = ["chat", "--json"] + ([f"--reply-to={a.reply_to}"] if a.reply_to else [])
    if a.buttons:
        rows = [[b.model_dump() for b in row] for row in a.buttons]
        argv += [f"--buttons={json.dumps(rows, ensure_ascii=False)}"]
    argv += ["--", a.text]
    rc, d = phone_run(argv, "phone chat", ok=(0, 4, 5))
    st = d.get("status")
    return SendOut(
        delivered=st in ("acked", "sent"),
        status=st,
        id=str(d["msg_id"]) if d.get("msg_id") is not None else None,
        note="phone offline: send it on Telegram too" if rc == 4 else None,
        result=d,
    )


# the phone gateway's data lookups, run here for principal phone (claude-hub-phone has none of these CLIs): only
# read-only commands, each argv must start with one of these prefixes (what phonegw's quiet hours, reminders screen,
# decision cards, trip mode and widget producers ran in this container before the move; `phone-produce KIND` runs the
# gateway's own producers here). The phone.* tools above stay brain-run (this container's `phone` CLI reaches the
# gateway's internal API with its own token); taps, chat and shares reach the brain as phone.* hub events.
LOOKUPS = (
    ("tripclock", "tz"),
    ("sleepin", "status", "--json"),
    ("sleepin", "active"),
    ("remind", "list"),
    ("remind", "when"),
    ("remind", "actions"),
    ("decision-deck", "--json"),
    ("calcache", "show"),
    ("cat-supplies", "--json"),
    ("parcels", "speak"),
    ("trip-conditions",),
    ("phone-produce",),
)
OUT_MAX = 4 << 20  # a geo pack or a trip pack: well under this


class LookupIn(ToolInput):
    argv: list[str] = Field(min_length=1, max_length=12, description="a gateway lookup command and its arguments")
    timeout_s: float = Field(default=10.0, gt=0, le=180.0)


class LookupOut(ToolOutput):
    rc: int
    out: str = Field(description="the command's stdout (JSON or a short line)")

    def render(self, fmt: Format = "concise") -> str:
        return self.out if self.rc == 0 else f"exit {self.rc}"


@tool("phone.lookup", effect="read", labels_out=("owner_text", "private"), rate="120/min", timeout_s=200)
def lookup(a: LookupIn) -> LookupOut:
    """The phone gateway's data lookups (quiet hours, the reminders list, decision cards, ...): one read-only CLI from
    its list, run here. For the gateway in claude-hub-phone (principal phone); nothing else needs it."""
    if any(len(x) > 200 or "\x00" in x for x in a.argv) or not any(tuple(a.argv[: len(p)]) == p for p in LOOKUPS):
        raise ToolError("invalid_args", "not a phone gateway lookup", "the gateway's read-only lookups only")
    d = os.environ.get("TOOLS_PHONE_LOOKUP_DIR", "")  # tests: a dir of fake CLIs
    rc, out, _err = run(
        os.path.join(d, a.argv[0]) if d else bin_path("TOOLS_NO_SUCH_ENV", a.argv[0]), a.argv[1:], timeout=a.timeout_s
    )
    return LookupOut(rc=rc, out=out[:OUT_MAX])


# the phone gateway's writes, run here for principal phone (what its reminders screen, trip mode, health sync
# and Track ID ran as brain CLIs before the move). argv must match one shape exactly: "*" any word, "#" digits,
# "@out" a fresh temp file whose text comes back as `file`, "@file" the gateway's stored file `file` names (fetched
# first with `phone file get`). Reminder changes run with REMIND_VIA=app (the app's, for analytics).
ACTS: tuple[tuple[str, ...], ...] = (
    ("remind", "add", "--owner", "*", "*"),
    ("remind", "snooze", "*", "*"),
    ("remind", "move", "*", "*"),
    ("remind", "done", "*"),
    ("remind", "cancel", "*"),
    ("remind", "skip", "*"),
    ("usage", "log", "reminders.app", "invoke", "--channel", "app", "--actor", "owner", "--attr", "*", "--attr", "*"),
    ("divelog", "add", "--quick", "*"),
    ("health", "ingest", "--received", "#"),
    ("trip-pack", "*", "--out", "@out"),
    ("trackid", "@file", "--json", "--source", "app", "--min-store", "medium"),
)
FILE_ID = r"^[A-Za-z0-9_-][A-Za-z0-9._-]{0,63}/[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}$"


def act_allowed(argv: list[str]) -> bool:
    def word(a: str, s: str) -> bool:
        return s == "*" or (s == "#" and a.isdigit()) or a == s

    return any(len(argv) == len(s) and all(word(a, w) for a, w in zip(argv, s, strict=True)) for s in ACTS)


class ActIn(ToolInput):
    argv: list[str] = Field(min_length=1, max_length=14, description="one of the gateway's write commands")
    stdin: str | None = Field(default=None, max_length=512 << 10, description="health ingest: the day's JSON")
    file: str | None = Field(default=None, pattern=FILE_ID, description="@file: the gateway's stored file SHARE/NAME")
    timeout_s: float = Field(default=60.0, gt=0, le=180.0)


class ActOut(ToolOutput):
    rc: int
    out: str = Field(description="the command's stdout")
    err: str = Field("", description="its stderr's start (the reminders screen shows remind's reason)")
    file: str | None = Field(default=None, description="@out: the file the command wrote")

    def render(self, fmt: Format = "concise") -> str:
        return self.out if self.rc == 0 else f"exit {self.rc}"


@tool("phone.act", effect="home", labels_out=("owner_text", "private"), rate="60/min", timeout_s=200)
def act(a: ActIn) -> ActOut:
    """The phone gateway's writes for his app (a reminder change from its reminders screen, a dive he logged offline,
    the health sync, a trip pack, a Track ID clip): one allow-listed command, run here. For the gateway in
    claude-hub-phone (principal phone); nothing else needs it."""
    if any(len(x) > 4000 or "\x00" in x for x in a.argv) or not act_allowed(a.argv):
        raise ToolError("invalid_args", "not a phone gateway write", "the gateway's allow-listed writes only")
    if ("@file" in a.argv) != bool(a.file):
        raise ToolError("invalid_args", "@file needs file (and file needs @file)", "`tool show phone.act`")
    d = os.environ.get("TOOLS_PHONE_LOOKUP_DIR", "")  # tests: a dir of fake CLIs
    exe = os.path.join(d, a.argv[0]) if d else bin_path("TOOLS_NO_SUCH_ENV", a.argv[0])
    with tempfile.TemporaryDirectory(prefix="phone-act-") as tmp:
        argv, out_file = list(a.argv[1:]), os.path.join(tmp, "out")
        if "@out" in argv:
            argv[argv.index("@out")] = out_file
        if a.file:
            got = os.path.join(tmp, a.file.rsplit("/", 1)[1])
            rc, _o, err = run(_bin(), ["file", "get", a.file, "--out", got], timeout=60)
            if rc != 0:
                why = first_line(err, default=f"exit {rc}")
                raise ToolError("unavailable", "the gateway's file could not be fetched", why)
            argv[argv.index("@file")] = got
        env = dict(os.environ, REMIND_VIA="app") if a.argv[0] == "remind" else None
        try:
            if a.stdin is None:
                p = subprocess.run([exe, *argv], capture_output=True, text=True, timeout=a.timeout_s, env=env,
                                   stdin=subprocess.DEVNULL)  # fmt: skip
            else:
                p = subprocess.run([exe, *argv], capture_output=True, text=True, timeout=a.timeout_s, env=env,
                                   input=a.stdin)  # fmt: skip
        except FileNotFoundError as e:
            raise ToolError("unavailable", f"{a.argv[0]} is not installed", f"is ~/bin/{a.argv[0]} there?") from e
        except subprocess.TimeoutExpired as e:
            raise ToolError("timeout", f"{a.argv[0]} took over {a.timeout_s:.0f} s", "retry once") from e
        f = None
        if "@out" in a.argv and os.path.isfile(out_file) and os.path.getsize(out_file) <= OUT_MAX:
            f = Path(out_file).read_text(encoding="utf-8", errors="replace")
    return ActOut(rc=p.returncode, out=p.stdout[:65536], err=p.stderr.strip()[:1000], file=f)
