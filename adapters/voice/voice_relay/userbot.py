"""voice-tg: login, status and hardening for the call bridge's second Telegram *user* account.

Login while a human relays the code. Telegram only accepts a code on the connection that requested it, so
  login start [PHONE]   -> spawns a detached waiter that sends the code and keeps that one client connected
                           (10 min), then prints "code sent via ..."
  login finish CODE [--reverse] [--password-file F]   -> hands the code (and the 2FA password FILE path, never the
                           password on argv) to the waiter through a 0600 file; prints "logged in: name (id N)"
  login cancel          -> stops a waiting login
  --phone-stdin / --code-stdin / --password-stdin   the same values from one stdin line instead (never argv): the
                           hub relogin paste pipes what the owner typed through `docker exec -i`
Credentials (api_id/api_hash, optional phone) are read only in code from ~/.secrets/voice-tg.json and never printed;
every message and log line goes through a scrubber.
In claude-hub-voice the release's voice/run.py runs this as `run.py tg ...` (ENTRY) with the relay's container paths
from the environment, the same names the relay reads (voiceloader FIXED): VOICE_TG_CONFIG_FILE (the api config),
VOICE_TG_SESSION_DIR (session, login session and the login's state/input/log files) and VOICE_TG_ACCESS_FILE.
This module never reads the bot token or the telegram-plus plugin's files: the only file it reads from the bot's side
is access.json (allowFrom[0] = the owner's user id).
"""
import argparse
import asyncio
import json
import logging
import os
import re
import secrets
import signal
import sys
import time

EXIT_OK, EXIT_USAGE, EXIT_PASSWORD, EXIT_TG, EXIT_REFUSED = 0, 2, 3, 4, 5
LOGIN_TIMEOUT_S = 10 * 60
WAITING = ("code_sent", "password_needed")
CLI_HINT = re.compile(r": (?:run|write) .*$", re.S)  # prompted flow: "wrong code: run login finish ..." -> "wrong code"
VIA = {"APP": "Telegram", "SMS": "SMS", "FRAGMENT_SMS": "SMS", "FIREBASE_SMS": "SMS", "SMS_WORD": "SMS",
       "SMS_PHRASE": "SMS", "CALL": "phone call", "FLASH_CALL": "phone call", "MISSED_CALL": "phone call",
       "EMAIL_CODE": "email"}


class Fail(Exception):
    def __init__(self, msg, code=EXIT_TG):
        super().__init__(msg)
        self.code = code


ENTRY = None  # run.py sets [its path, "tg"] in a hub release: the waiter starts through it (wheels in voice/vendor)


class Paths:
    def __init__(self, secrets=None, home=None, env=None):
        """Explicit secrets/home (tests) win; else the hub's container paths from env (VOICE_TG_*), else ~/.secrets."""
        env = os.environ if env is None else env
        default = secrets is None and home is None
        home = home or os.path.expanduser("~")
        hub_dir = env.get("VOICE_TG_SESSION_DIR") if default else None
        self.secrets = os.path.realpath(secrets or hub_dir or os.path.join(home, ".secrets"))
        self.creds = (env.get("VOICE_TG_CONFIG_FILE") if default else None) or os.path.join(self.secrets,
                                                                                             "voice-tg.json")
        self.session_name = "voice-tg"
        self.session = os.path.join(self.secrets, "voice-tg.session")
        self.login_name = "voice-tg-login"
        self.login_session = os.path.join(self.secrets, "voice-tg-login.session")
        self.state = os.path.join(self.secrets, "voice-tg-login.json")      # waiter -> CLI (phase, message)
        self.login_input = os.path.join(self.secrets, "voice-tg-login.in")  # CLI -> waiter (code, password path)
        self.login_log = os.path.join(self.secrets, "voice-tg-login.log")   # waiter stderr (scrubbed)
        self.login_password = os.path.join(self.secrets, "voice-tg-login.pw")  # --password-stdin: until taken
        self.access = (env.get("VOICE_TG_ACCESS_FILE") if default else None) or os.path.join(
            home, ".claude", "channels", "telegram", "access.json")


class Scrubber:
    """Masks every known secret value (api_id, api_hash, phone, code, password, phone_code_hash) in text."""

    def __init__(self):
        self.values = set()

    def add(self, *vals):
        for v in vals:
            if v is None:
                continue
            v = str(v).strip()
            if len(v) >= 4:
                self.values.add(v)
                digits = re.sub(r"\D", "", v)
                if len(digits) >= 5:
                    self.values.add(digits)

    def __call__(self, text):
        text = str(text)
        for v in sorted(self.values, key=len, reverse=True):
            text = text.replace(v, "***")
        return text


SCRUB = Scrubber()


class _LogScrub(logging.Filter):
    def filter(self, record):
        record.msg, record.args = SCRUB(record.getMessage()), None
        return True


def setup_logging():
    h = logging.StreamHandler(sys.stderr)
    h.addFilter(_LogScrub())
    h.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [h]
    root.setLevel(logging.WARNING)


def _check_private(path):
    st = os.stat(path)
    if st.st_mode & 0o077:
        raise Fail("%s must be 0600" % os.path.basename(path), EXIT_USAGE)


def load_creds(paths):
    try:
        _check_private(paths.creds)
        with open(paths.creds, encoding="utf-8") as fh:
            conf = json.load(fh)
    except FileNotFoundError:
        raise Fail("voice-tg.json is missing in ~/.secrets", EXIT_USAGE)
    except ValueError:
        raise Fail("voice-tg.json is not valid JSON", EXIT_USAGE)
    if any(k in conf for k in ("bot_token", "token", "bot")):
        raise Fail("bot tokens are not allowed here: user account only", EXIT_REFUSED)
    if not conf.get("api_id") or not conf.get("api_hash"):
        raise Fail("voice-tg.json needs api_id and api_hash", EXIT_USAGE)
    SCRUB.add(conf.get("api_id"), conf.get("api_hash"), conf.get("phone"))
    return conf


def owner_id(paths):
    """allowFrom[0] of the bot's access.json (read-only); 0 when unknown."""
    try:
        with open(paths.access, encoding="utf-8") as fh:
            allow = json.load(fh).get("allowFrom") or []
        return int(allow[0]) if allow else 0
    except (OSError, ValueError, TypeError):
        return 0


def _write_private(path, data):
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _rm(*paths):
    for p in paths:
        for q in (p, p + "-journal", p + "-wal", p + "-shm"):
            try:
                os.remove(q)
            except FileNotFoundError:
                pass


def _normalize_phone(phone):
    digits = re.sub(r"\D", "", str(phone or ""))
    if len(digits) < 7:
        raise Fail("no usable phone number (argument or \"phone\" in voice-tg.json)", EXIT_USAGE)
    return "+" + digits


def make_client(name, workdir, conf):
    """The real Kurigram (pyrogram) user client. no_updates: login/status/harden never consume updates."""
    from pyrogram import Client
    return Client(name, api_id=int(conf["api_id"]), api_hash=str(conf["api_hash"]), workdir=workdir,
                  no_updates=True, device_model="voice-relay", app_version="voice-relay 1.0")


def _errname(e):
    return getattr(e, "ID", None) or type(e).__name__


def _read_state(paths):
    try:
        with open(paths.state, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def _waiting(st):
    return bool(st) and st.get("phase") in WAITING and _alive(st.get("pid"))


def _take_input(paths):
    """The waiter's inbox: one 0600 JSON request written by login finish/cancel; read once, then deleted."""
    try:
        _check_private(paths.login_input)
        with open(paths.login_input, encoding="utf-8") as fh:
            req = json.load(fh)
    except FileNotFoundError:
        return None
    except (Fail, ValueError, OSError):
        req = {"id": None, "bad": True}
    _rm(paths.login_input)
    return req


def _spawn_waiter(paths, phone, timeout):
    """Detached `voice-tg _wait`: holds ONE connected client from send_code to sign_in (Telegram expires codes
    that are used on another connection). The phone goes through the environment, never argv."""
    import subprocess
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ, VOICE_TG_PHONE=phone)
    fd = os.open(paths.login_log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    # a hub release has no venv: the waiter starts through run.py too, which puts voice/app + voice/vendor on the path
    cmd = [sys.executable, "-I", "-B", *ENTRY] if ENTRY else [sys.executable, "-E", "-s", "-m", "voice_relay.userbot"]
    try:
        subprocess.Popen([*cmd, "_wait", "--timeout", str(int(timeout))], cwd=root, env=env, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=fd, start_new_session=True)
    finally:
        os.close(fd)


async def login_waiter(paths, phone, factory=make_client, timeout=LOGIN_TIMEOUT_S, poll=0.5):
    """Runs detached: sends the code, then waits (same connection) for login finish to hand over the code and,
    if the account has 2FA, the password file. Reports through the 0600 state file; nothing on stdout."""
    conf = load_creds(paths)
    phone = _normalize_phone(phone or conf.get("phone"))
    SCRUB.add(phone)
    errs = _errors()
    st = {"pid": os.getpid(), "phase": "starting", "msg": "", "req": None,
          "started": int(time.time()), "deadline": int(time.time() + timeout)}

    def put(**kw):
        st.update(kw, ts=int(time.time()))
        _write_private(paths.state, st)

    put()
    _rm(paths.login_session, paths.login_input)
    client = factory(paths.login_name, paths.secrets, conf)
    connected = done = False
    try:
        await client.connect()
        connected = True
        sender = getattr(client, "send_phone_number_code", None) or client.send_code
        try:
            sent = await sender(phone)
        except Exception as e:
            put(phase="failed", msg="Telegram refused the code request: " + _errname(e))
            return EXIT_TG
        SCRUB.add(sent.phone_code_hash)
        os.chmod(paths.login_session, 0o600)
        put(phase="code_sent", via=VIA.get(getattr(getattr(sent, "type", None), "name", ""), "Telegram/SMS"))
        end = time.monotonic() + timeout
        user = None
        while time.monotonic() < end:
            req = _take_input(paths)
            if req is None:
                await asyncio.sleep(poll)
                continue
            rid = req.get("id")
            if req.get("bad"):
                put(msg="unreadable request", req=rid)
                continue
            if req.get("cancel"):
                put(phase="failed", msg="cancelled", req=rid)
                return EXIT_USAGE
            if st["phase"] == "code_sent":
                code = re.sub(r"\D", "", str(req.get("code") or ""))
                SCRUB.add(code)
                try:
                    user = await client.sign_in(phone, sent.phone_code_hash, code)
                except errs["SessionPasswordNeeded"]:
                    st["phase"] = "password_needed"
                except errs["PhoneCodeInvalid"]:
                    put(msg="wrong code", req=rid)
                    continue
                except errs["PhoneCodeExpired"]:
                    put(phase="failed", msg="the code expired: run login start again", req=rid)
                    return EXIT_TG
                except Exception as e:
                    put(phase="failed", msg="sign-in failed: " + _errname(e), req=rid)
                    return EXIT_TG
                if user is not None and not hasattr(user, "is_bot"):  # False / TermsOfService = sign-up needed
                    put(phase="failed", msg="this number has no Telegram account yet: sign up in the app first",
                        req=rid)
                    return EXIT_REFUSED
            if user is None:
                try:
                    password = _read_password(req.get("password_file"))
                except (Fail, OSError) as e:
                    put(phase="password_needed", msg=str(e), req=rid)
                    continue
                if not password:
                    put(phase="password_needed", msg="this account has a 2FA password", req=rid)
                    continue
                try:
                    user = await client.check_password(password)
                except errs["PasswordHashInvalid"]:
                    put(phase="password_needed", msg="wrong 2FA password", req=rid)
                    continue
                except Exception as e:
                    put(phase="failed", msg="2FA check failed: " + _errname(e), req=rid)
                    return EXIT_TG
            oid = owner_id(paths)
            if getattr(user, "is_bot", False) or (oid and int(user.id) == oid):
                await _safe_logout(client)
                put(phase="failed", msg="refused: a bot or the owner's own account (session logged out again)",
                    req=rid)
                return EXIT_REFUSED
            connected = False
            await client.disconnect()
            _rm(paths.session)
            os.chmod(paths.login_session, 0o600)
            os.replace(paths.login_session, paths.session)
            os.chmod(paths.session, 0o600)
            done = True
            put(phase="done", msg="logged in: %s (id %s)" % (SCRUB(getattr(user, "first_name", "") or "?"),
                                                              int(user.id)), req=rid)
            return EXIT_OK
        put(phase="failed", msg="timed out waiting for the code: run login start again")
        return EXIT_TG
    except asyncio.CancelledError:
        put(phase="failed", msg="cancelled")
        raise
    except Exception as e:
        put(phase="failed", msg="login waiter error: " + _errname(e))
        return EXIT_TG
    finally:
        if connected:
            try:
                await client.disconnect()
            except Exception:
                pass
        if not done:
            _rm(paths.login_session)
        _rm(paths.login_input, paths.login_password)


async def _await_state(paths, pred, wait, poll):
    end = time.monotonic() + wait
    while time.monotonic() < end:
        st = _read_state(paths)
        if st and pred(st):
            return st
        if st and st.get("pid") and not _alive(st["pid"]) and st.get("phase") in WAITING + ("starting",):
            raise Fail("the login waiter stopped unexpectedly (see ~/.secrets/voice-tg-login.log): run login "
                       "start again", EXIT_TG)
        await asyncio.sleep(poll)
    raise Fail("no answer from the login waiter in %d s" % wait, EXIT_TG)


async def login_start(paths, phone=None, force=False, spawn=_spawn_waiter, timeout=LOGIN_TIMEOUT_S, wait=60,
                      poll=0.25):
    conf = load_creds(paths)
    phone = _normalize_phone(phone or conf.get("phone"))
    SCRUB.add(phone)
    if os.path.exists(paths.session) and not force:
        raise Fail("already logged in (voice-tg status); use --force to log in again", EXIT_USAGE)
    if _waiting(_read_state(paths)):
        raise Fail("a login is already waiting for its code: login finish CODE, or login cancel", EXIT_USAGE)
    _rm(paths.state, paths.login_input)
    spawn(paths, phone, timeout)
    st = await _await_state(paths, lambda s: s.get("phase") in ("code_sent", "failed"), wait, poll)
    if st["phase"] == "failed":
        raise Fail(st.get("msg") or "login failed", EXIT_TG)
    return "code sent via %s (waiting %d min for: login finish CODE)" % (st.get("via", "Telegram/SMS"),
                                                                         timeout // 60)


async def login_finish(paths, code="", reverse=False, password_file=None, wait=90, poll=0.25):
    load_creds(paths)
    st = _read_state(paths)
    if not _waiting(st):
        raise Fail("no login is waiting: run login start (a code only works on the connection that asked "
                   "for it)", EXIT_USAGE)
    digits = re.sub(r"\D", "", str(code or ""))  # "12 345", "1-2-3-4-5" are fine
    if reverse:
        digits = digits[::-1]
    SCRUB.add(digits)
    if st["phase"] == "code_sent" and not 4 <= len(digits) <= 8:
        raise Fail("the code must have 4-8 digits", EXIT_USAGE)
    if password_file:
        _read_password(password_file)  # fail early: missing, empty or not 0600
        password_file = os.path.abspath(password_file)
    elif st["phase"] == "password_needed":
        raise Fail("this account has a 2FA password: write it to a 0600 file and run login finish "
                   "--password-file FILE (no code needed)", EXIT_PASSWORD)
    rid = secrets.token_hex(8)
    _write_private(paths.login_input, {"id": rid, "code": digits, "password_file": password_file})
    st = await _await_state(paths, lambda s: s.get("req") == rid, wait, poll)
    if st["phase"] == "done":
        return st["msg"]
    if st["phase"] == "password_needed":
        raise Fail(st.get("msg", "") + ": write the 2FA password to a 0600 file and run login finish "
                   "--password-file FILE (no code needed)", EXIT_PASSWORD)
    if st["phase"] == "code_sent":
        raise Fail(st.get("msg", "") + ": run login finish again with the right code (--reverse if it was "
                   "relayed backwards); the login keeps waiting", EXIT_TG)
    raise Fail(st.get("msg") or "login failed", EXIT_TG)


async def login_cancel(paths, wait=5, poll=0.25):
    st = _read_state(paths)
    if not _waiting(st):
        _rm(paths.state, paths.login_input, paths.login_session, paths.login_password)
        return "no login was waiting"
    rid = secrets.token_hex(8)
    _write_private(paths.login_input, {"id": rid, "cancel": True})
    try:
        await _await_state(paths, lambda s: s.get("req") == rid, wait, poll)
    except Fail:
        try:
            os.kill(int(st["pid"]), signal.SIGTERM)
        except OSError:
            pass
    _rm(paths.login_input, paths.login_session, paths.login_password)
    return "login cancelled"


def _read_password(path):
    if not path:
        return None
    _check_private(path)
    with open(path, encoding="utf-8") as fh:
        pw = fh.read().rstrip("\r\n")
    if not pw:
        raise Fail("the password file is empty", EXIT_USAGE)
    SCRUB.add(pw)
    return pw


async def _safe_logout(client):
    try:
        await client.log_out()
    except Exception:
        pass


def _errors():
    from pyrogram import errors
    return {k: getattr(errors, k) for k in ("SessionPasswordNeeded", "PhoneCodeInvalid", "PhoneCodeExpired",
                                            "PasswordHashInvalid", "PeerIdInvalid")}


async def _session_client(paths, factory):
    conf = load_creds(paths)
    if not os.path.exists(paths.session):
        raise Fail("not logged in: voice-tg login start / finish", EXIT_USAGE)
    _check_private(paths.session)
    client = factory(paths.session_name, paths.secrets, conf)
    await client.connect()
    return client


async def status(paths, factory=make_client, live=True):
    lines = []
    try:
        conf = load_creds(paths)
        lines.append("credentials: ok (api_id/api_hash%s)" % (", phone" if conf.get("phone") else ""))
    except Fail as e:
        lines.append("credentials: " + str(e))
        conf = None
    lines.append("owner id: %s" % ("from access.json" if owner_id(paths) else "MISSING (access.json allowFrom)"))
    st = _read_state(paths)
    if st and st.get("phase") in WAITING + ("starting",):
        lines.append("pending login: %s, %s, %d min left" % (
            st["phase"].replace("_", " "), "waiter running" if _alive(st.get("pid")) else "waiter GONE (login start)",
            max(0, int(st.get("deadline", 0) - time.time()) // 60)))
    if not os.path.exists(paths.session):
        lines.append("session: none")
        return "\n".join(lines), 1
    mode = os.stat(paths.session).st_mode & 0o777
    lines.append("session: present (%o)" % mode)
    if not live or conf is None:
        return "\n".join(lines), 0
    client = await _session_client(paths, factory)
    try:
        me = await client.get_me()
        lines.append("account: %s (id %s)%s" % (SCRUB(me.first_name or "?"), int(me.id),
                                                 " BOT: REFUSED" if me.is_bot else ""))
    except Exception as e:
        lines.append("account: not authorized (%s)" % _errname(e))
        return "\n".join(lines), 1
    finally:
        await client.disconnect()
    return "\n".join(lines), 0


async def harden(paths, factory=make_client, owner_name="Owner"):
    """Calls: contacts only; P2P: never; the owner as the account's contact. Re-runnable."""
    from pyrogram import enums, types
    errs = _errors()
    owner = owner_id(paths)
    if not owner:
        raise Fail("owner id missing (access.json allowFrom[0])", EXIT_USAGE)
    client = await _session_client(paths, factory)
    out = []
    try:
        me = await client.get_me()
        if me.is_bot or int(me.id) == owner:
            raise Fail("refused: this session is a bot or the owner's own account", EXIT_REFUSED)
        await client.set_privacy(enums.PrivacyKey.PHONE_CALL, [types.InputPrivacyRuleAllowContacts()])
        out.append("calls: contacts only")
        await client.set_privacy(enums.PrivacyKey.PHONE_P2P, [types.InputPrivacyRuleDisallowAll()])
        out.append("peer-to-peer: never")
        try:  # no_updates=True never caches peers: load recent dialogs so the owner's access hash is known
            async for _ in client.get_dialogs(limit=50):
                pass
        except Exception as e:
            out.append("dialogs: not loaded (%s)" % _errname(e))
        try:
            await client.add_contact(owner, owner_name)
            out.append("owner added as contact")
        except (errs["PeerIdInvalid"], KeyError, ValueError):
            out.append("owner NOT added yet: the account has never seen him. He sends this account one message "
                       "(or calls it once), then run voice-tg harden again")
            return "\n".join(out), EXIT_TG
        try:
            others = [c for c in await client.get_contacts() if int(c.id) != owner]
            out.append("contacts: owner only" if not others else
                       "contacts: %d besides the owner (they could call too)" % len(others))
        except Exception as e:
            out.append("contacts: not checked (%s)" % _errname(e))
    finally:
        await client.disconnect()
    return "\n".join(out), EXIT_OK


def _stdin_line(what, exact=False):
    """One line from stdin (the relogin paste pipes what the owner typed); scrubbed from every later message.
    exact: only the line break goes (a 2FA password keeps its spaces, as _read_password reads it back)."""
    raw = sys.stdin.readline()
    line = raw.rstrip("\r\n") if exact else raw.strip()
    if not line:
        raise Fail("no %s on stdin" % what, EXIT_USAGE)
    SCRUB.add(line)
    return line


def _finish_with_password(paths, code, reverse, password):
    """--password-stdin: the waiter takes a FILE path, so the password sits in a 0600 file next to the session (the
    session dir, 0700) only until this login finish returns."""
    tmp = paths.login_password + ".tmp"
    _rm(tmp)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(password + "\n")
    os.replace(tmp, paths.login_password)
    try:
        return asyncio.run(login_finish(paths, code, reverse, paths.login_password))
    finally:
        _rm(paths.login_password)


def _run_waiter(paths, timeout):
    async def go():
        task = asyncio.ensure_future(login_waiter(paths, os.environ.pop("VOICE_TG_PHONE", None), timeout=timeout))
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGHUP):
            loop.add_signal_handler(sig, task.cancel)
        try:
            return await task
        except asyncio.CancelledError:
            return EXIT_USAGE
    return asyncio.run(go())


def main(argv=None, paths=None, factory=make_client):
    os.umask(0o077)
    setup_logging()
    ap = argparse.ArgumentParser(prog="voice-tg", description="Second Telegram account for the voice call bridge")
    sub = ap.add_subparsers(dest="cmd", required=True)
    lg = sub.add_parser("login").add_subparsers(dest="step", required=True)
    s = lg.add_parser("start")
    s.add_argument("phone", nargs="?")
    s.add_argument("--phone-stdin", action="store_true", help="the phone number from one stdin line (empty line: "
                   "the one in voice-tg.json)")
    s.add_argument("--force", action="store_true")
    s.add_argument("--timeout", type=int, default=LOGIN_TIMEOUT_S)
    f = lg.add_parser("finish")
    f.add_argument("code", nargs="?", default="")
    f.add_argument("--reverse", action="store_true", help="the code was relayed backwards")
    fin = f.add_mutually_exclusive_group()
    fin.add_argument("--password-file")
    fin.add_argument("--code-stdin", action="store_true", help="the code from one stdin line, never argv")
    fin.add_argument("--password-stdin", action="store_true", help="the 2FA password from one stdin line, never argv")
    lg.add_parser("cancel")
    st = sub.add_parser("status")
    st.add_argument("--offline", action="store_true")
    sub.add_parser("harden")
    w = sub.add_parser("_wait")  # internal: the detached login waiter
    w.add_argument("--timeout", type=int, default=LOGIN_TIMEOUT_S)
    a = ap.parse_args(argv)
    paths = paths or Paths()
    try:
        if a.cmd == "_wait":
            return _run_waiter(paths, a.timeout)
        if a.cmd == "login" and a.step == "start":
            phone = a.phone
            if a.phone_stdin and not phone:
                phone = sys.stdin.readline().strip() or None
                SCRUB.add(phone)
            msg, rc = asyncio.run(login_start(paths, phone, a.force, timeout=a.timeout)), EXIT_OK
            if a.phone_stdin:  # the relogin paste prompts for the code itself
                msg = msg.replace(" for: login finish CODE", "")
        elif a.cmd == "login" and a.step == "finish" and a.password_stdin:
            msg, rc = _finish_with_password(paths, a.code, a.reverse, _stdin_line("2FA password", exact=True)), EXIT_OK
        elif a.cmd == "login" and a.step == "finish":
            code = _stdin_line("code") if a.code_stdin else a.code
            msg, rc = asyncio.run(login_finish(paths, code, a.reverse, a.password_file)), EXIT_OK
        elif a.cmd == "login":
            msg, rc = asyncio.run(login_cancel(paths)), EXIT_OK
        elif a.cmd == "status":
            msg, rc = asyncio.run(status(paths, factory, live=not a.offline))
        else:
            msg, rc = asyncio.run(harden(paths, factory))
    except Fail as e:
        prompted = getattr(a, "phone_stdin", False) or getattr(a, "code_stdin", False) or getattr(
            a, "password_stdin", False)
        print("voice-tg: " + (CLI_HINT.sub("", SCRUB(e)) if prompted else SCRUB(e)), file=sys.stderr)
        return e.code
    except Exception as e:  # never echo raw exception text: it may carry request data
        print("voice-tg: unexpected %s" % _errname(e), file=sys.stderr)
        return EXIT_TG
    print(SCRUB(msg))
    return rc


if __name__ == "__main__":
    sys.exit(main())
