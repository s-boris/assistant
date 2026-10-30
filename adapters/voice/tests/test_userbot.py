"""voice-tg (second Telegram account): login through one waiting connection, status, harden. Fake client only,
no network; the waiter runs as an in-process task instead of a detached process."""
import asyncio
import json
import logging
import os

import pytest
from pyrogram import errors

from voice_relay import userbot

API_ID, API_HASH = 7654321, "".join(['f00d', 'feed', 'cafe', '0123', '4567', '89ab', 'cdef', '0123'])
PHONE = "".join(['+41', ' 79', ' 55', '5 1', '2 3', '4'])
E164 = "".join(['+41', '795', '551', '234'])
OWNER, ME, CODE, PW, HASH = 111222333, 4242, "54321", "correct horse battery", "hash-abc123xyz"
SECRETS = [str(API_ID), API_HASH, "41795551234", CODE, PW, HASH]


class FakeUser:
    def __init__(self, uid=ME, first="Relay", is_bot=False):
        self.id, self.first_name, self.is_bot = uid, first, is_bot


class FakeSent:
    def __init__(self, kind="APP"):
        self.phone_code_hash = HASH
        self.type = type("T", (), {"name": kind})()


class FakeTg:
    """Records calls; behaviour knobs per test. Creates the session file on connect, like FileStorage."""
    instances = []

    def __init__(self, name, workdir, conf, *, kind="APP", password=None, sign_in_error=None, user=None,
                 resolvable=True, contacts=(), bad_codes=()):
        self.name, self.workdir, self.conf = name, workdir, conf
        self.kind, self.password, self.sign_in_error = kind, password, sign_in_error
        self.user, self.resolvable, self.contacts = user or FakeUser(), resolvable, list(contacts)
        self.bad_codes = set(bad_codes)
        self.calls = []
        FakeTg.instances.append(self)

    async def connect(self):
        self.calls.append("connect")
        with open(os.path.join(self.workdir, self.name + ".session"), "a"):
            pass
        logging.getLogger("pyrogram.session").warning("connected with %s / %s", self.conf["api_hash"], PHONE)

    async def disconnect(self):
        self.calls.append("disconnect")

    async def send_phone_number_code(self, phone):
        self.calls.append(("send_code", phone))
        return FakeSent(self.kind)

    async def sign_in(self, phone, code_hash, code):
        self.calls.append(("sign_in", phone, code_hash, code))
        if code in self.bad_codes:
            raise errors.PhoneCodeInvalid()
        if self.sign_in_error:
            raise self.sign_in_error
        if self.password:
            raise errors.SessionPasswordNeeded()
        return self.user

    async def check_password(self, pw):
        self.calls.append(("check_password", pw))
        if pw != self.password:
            raise errors.PasswordHashInvalid()
        return self.user

    async def log_out(self):
        self.calls.append("log_out")

    async def get_me(self):
        return self.user

    async def set_privacy(self, key, rules):
        self.calls.append(("privacy", key.name, [type(r).__name__ for r in rules]))

    async def get_dialogs(self, limit=0):
        self.calls.append(("get_dialogs", limit))
        self.dialogs_loaded = True
        for c in self.contacts:
            yield c

    async def add_contact(self, uid, first):
        if not self.resolvable or (self.resolvable == "after_dialogs" and not getattr(self, "dialogs_loaded", 0)):
            raise errors.PeerIdInvalid()
        self.calls.append(("add_contact", uid, first))
        self.contacts.append(FakeUser(uid, first))

    async def get_contacts(self):
        return self.contacts


def factory(**kw):
    def make(name, workdir, conf):
        return FakeTg(name, workdir, conf, **kw)
    return make


@pytest.fixture
def env(tmp_path):
    FakeTg.instances.clear()
    userbot.SCRUB.values.clear()
    sec = tmp_path / ".secrets"
    sec.mkdir(mode=0o700)
    creds = sec / "voice-tg.json"
    creds.write_text(json.dumps({"api_id": API_ID, "api_hash": API_HASH, "phone": PHONE}))
    creds.chmod(0o600)
    acc = tmp_path / ".claude" / "channels" / "telegram"
    acc.mkdir(parents=True)
    (acc / "access.json").write_text(json.dumps({"allowFrom": [str(OWNER)]}))
    return userbot.Paths(secrets=str(sec), home=str(tmp_path))


def run(env, capsys, argv, **kw):
    rc = userbot.main(argv, paths=env, factory=factory(**kw))
    out, err = capsys.readouterr()
    for s in SECRETS:
        assert s not in out and s not in err, "secret leaked: %r" % s
    return rc, out.strip(), err.strip()


def mode(p):
    return os.stat(p).st_mode & 0o777


class Login:
    """login start -> waiter task (same fake connection throughout) -> login finish / cancel."""

    def __init__(self, env, timeout=60, **kw):
        self.env, self.timeout, self.kw, self.tasks, self.msgs = env, timeout, kw, [], []

    def spawn(self, paths, phone, timeout):
        self.tasks.append(asyncio.ensure_future(
            userbot.login_waiter(paths, phone, factory(**self.kw), timeout, poll=0.01)))

    async def start(self, phone=None, **kw):
        m = await userbot.login_start(self.env, phone, spawn=self.spawn, timeout=self.timeout, poll=0.01, **kw)
        self.msgs.append(m)
        return m

    async def finish(self, code="", **kw):
        try:
            m = await userbot.login_finish(self.env, code, poll=0.01, wait=5, **kw)
            self.msgs.append(m)
            return 0, m
        except userbot.Fail as e:
            self.msgs.append(str(e))
            return e.code, str(e)

    async def result(self):
        return await asyncio.wait_for(self.tasks[-1], 5)

    def assert_clean(self):
        blob = "\n".join(self.msgs)
        for p in (self.env.state, self.env.login_input):
            if os.path.exists(p):
                blob += open(p).read()
        for sec in SECRETS:
            assert sec not in blob, "secret leaked: %r" % sec


def go(coro):
    return asyncio.run(coro)


def pwfile(tmp_path, text, mode_=0o600):
    p = tmp_path / ("pw-%d" % len(list(tmp_path.iterdir())))
    p.write_text(text)
    p.chmod(mode_)
    return str(p)


def test_code_is_used_on_the_same_connection_that_requested_it(env):
    async def main():
        lg = Login(env)
        assert await lg.start() == "code sent via Telegram (waiting 1 min for: login finish CODE)"
        st = json.load(open(env.state))
        assert st["phase"] == "code_sent" and mode(env.state) == 0o600 and mode(env.login_session) == 0o600
        assert "phone_code_hash" not in st  # the hash stays in the waiter's memory
        assert await lg.finish("54 321") == (0, "logged in: Relay (id 4242)")
        assert await lg.result() == 0
        (c,) = FakeTg.instances  # ONE client: connect, send_code, sign_in, disconnect
        assert c.calls == ["connect", ("send_code", E164), ("sign_in", E164, HASH, CODE),
                           "disconnect"]
        assert c.name == "voice-tg-login" and mode(env.session) == 0o600
        assert not os.path.exists(env.login_session) and not os.path.exists(env.login_input)
        lg.assert_clean()
    go(main())


def test_reverse_flag_and_sms_and_phone_argument(env):
    async def main():
        lg = Login(env, kind="SMS")
        assert (await lg.start(PHONE)).startswith("code sent via SMS")
        assert await lg.finish("1 2 3 4 5", reverse=True) == (0, "logged in: Relay (id 4242)")
        assert FakeTg.instances[0].calls[2][3] == CODE
    go(main())


def test_wrong_code_keeps_the_same_login_waiting(env):
    async def main():
        lg = Login(env, bad_codes={"11111"})
        await lg.start()
        rc, msg = await lg.finish("11111")
        assert rc == userbot.EXIT_TG and "wrong code" in msg and "--reverse" in msg
        assert await lg.finish(CODE) == (0, "logged in: Relay (id 4242)")
        assert len(FakeTg.instances) == 1
    go(main())


def test_two_factor_is_handled_by_the_same_waiter(env, tmp_path):
    async def main():
        lg = Login(env, password=PW)
        await lg.start()
        rc, msg = await lg.finish(CODE)
        assert rc == userbot.EXIT_PASSWORD and "--password-file" in msg
        assert json.load(open(env.state))["phase"] == "password_needed"
        rc, msg = await lg.finish()                                        # no file: refused before the waiter
        assert rc == userbot.EXIT_PASSWORD
        rc, msg = await lg.finish(password_file=pwfile(tmp_path, PW, 0o644))
        assert rc == userbot.EXIT_USAGE and "0600" in msg
        rc, msg = await lg.finish(password_file=pwfile(tmp_path, "nope-pass\n"))
        assert rc == userbot.EXIT_PASSWORD and "wrong 2FA" in msg
        assert await lg.finish(password_file=pwfile(tmp_path, PW + "\n")) == (0, "logged in: Relay (id 4242)")
        (c,) = FakeTg.instances
        assert [x[0] for x in c.calls if isinstance(x, tuple)].count("sign_in") == 1
        assert ("check_password", PW) in c.calls and mode(env.session) == 0o600
        lg.assert_clean()
    go(main())


def test_code_and_password_file_in_one_go(env, tmp_path):
    async def main():
        lg = Login(env, password=PW)
        await lg.start()
        assert await lg.finish(CODE, password_file=pwfile(tmp_path, PW)) == (0, "logged in: Relay (id 4242)")
    go(main())


def test_expired_code_ends_the_login(env):
    async def main():
        lg = Login(env, sign_in_error=errors.PhoneCodeExpired())
        await lg.start()
        rc, msg = await lg.finish(CODE)
        assert rc == userbot.EXIT_TG and "expired" in msg
        assert await lg.result() == userbot.EXIT_TG
        assert not os.path.exists(env.login_session) and not os.path.exists(env.session)
        assert "disconnect" in FakeTg.instances[0].calls
        rc, msg = await lg.finish(CODE)
        assert rc == userbot.EXIT_USAGE and "no login is waiting" in msg
    go(main())


@pytest.mark.parametrize("user", [FakeUser(OWNER, "Owner"), FakeUser(ME, "Bot", is_bot=True)])
def test_owner_account_and_bots_are_refused_and_logged_out(env, user):
    async def main():
        lg = Login(env, user=user)
        await lg.start()
        rc, msg = await lg.finish(CODE)
        assert rc == userbot.EXIT_TG and "refused" in msg
        assert await lg.result() == userbot.EXIT_REFUSED
        assert "log_out" in FakeTg.instances[0].calls
        assert not os.path.exists(env.session) and not os.path.exists(env.login_session)
    go(main())


def test_timeout_and_cancel_clean_up(env):
    async def main():
        lg = Login(env, timeout=0.3)
        await lg.start()
        assert await lg.result() == userbot.EXIT_TG
        st = json.load(open(env.state))
        assert st["phase"] == "failed" and "timed out" in st["msg"] and not os.path.exists(env.login_session)
        lg = Login(env)
        await lg.start()
        assert await userbot.login_cancel(env, poll=0.01) == "login cancelled"
        assert await lg.result() == userbot.EXIT_USAGE
        assert await userbot.login_cancel(env) == "no login was waiting"
    go(main())


def test_start_refusals(env):
    async def main():
        lg = Login(env)
        await lg.start()
        with pytest.raises(userbot.Fail, match="already waiting"):
            await lg.start()
        await userbot.login_cancel(env, poll=0.01)
        await lg.result()
        open(env.session, "w").close()
        with pytest.raises(userbot.Fail, match="--force"):
            await lg.start()
        assert (await lg.start(force=True)).startswith("code sent")
        await userbot.login_cancel(env, poll=0.01)
        assert os.path.exists(env.session)  # a failed/cancelled re-login never touches the live session
    go(main())


def test_finish_without_a_waiter_or_with_a_dead_one(env, capsys):
    rc, _, err = run(env, capsys, ["login", "finish", CODE])
    assert rc == userbot.EXIT_USAGE and "no login is waiting" in err
    with open(env.state, "w") as fh:
        json.dump({"pid": 2 ** 22 + 12345, "phase": "code_sent"}, fh)
    rc, _, err = run(env, capsys, ["login", "finish", CODE])
    assert rc == userbot.EXIT_USAGE


def test_spawn_detaches_and_keeps_the_phone_off_argv(env, monkeypatch):
    import subprocess
    seen = {}

    class P:
        def __init__(self, args, **kw):
            seen.update(args=args, **kw)

    monkeypatch.setattr(subprocess, "Popen", P)
    userbot._spawn_waiter(env, E164, 600)
    assert seen["start_new_session"] is True and seen["stdin"] == subprocess.DEVNULL
    assert seen["args"][-4:] == ["voice_relay.userbot", "_wait", "--timeout", "600"]
    assert E164 not in " ".join(seen["args"]) and seen["env"]["VOICE_TG_PHONE"] == E164
    assert mode(env.login_log) == 0o600


def test_creds_must_be_private_and_bot_tokens_refused(env, capsys):
    os.chmod(env.creds, 0o644)
    rc, _, err = run(env, capsys, ["login", "start"])
    assert rc == userbot.EXIT_USAGE and "0600" in err
    with open(env.creds, "w") as fh:
        json.dump({"api_id": API_ID, "api_hash": API_HASH, "bot_token": "1:x"}, fh)
    os.chmod(env.creds, 0o600)
    rc, _, err = run(env, capsys, ["login", "start"])
    assert rc == userbot.EXIT_REFUSED and not FakeTg.instances


def logged_in(env):
    with open(env.session, "w"):
        pass
    os.chmod(env.session, 0o600)


def test_status_offline_and_live(env, capsys):
    rc, out, _ = run(env, capsys, ["status"])
    assert rc == 1 and "session: none" in out and "owner id: from access.json" in out
    logged_in(env)
    rc, out, err = run(env, capsys, ["status"])
    assert rc == 0 and "account: Relay (id 4242)" in out and "session: present (600)" in out
    assert FakeTg.instances[-1].name == "voice-tg"
    assert "***" in err  # the fake logs api_hash + phone through the pyrogram logger: scrubbed


def test_harden_sets_privacy_and_adds_owner(env, capsys):
    logged_in(env)
    rc, out, _ = run(env, capsys, ["harden"])
    assert rc == 0, out
    calls = FakeTg.instances[-1].calls
    assert ("privacy", "PHONE_CALL", ["InputPrivacyRuleAllowContacts"]) in calls
    assert ("privacy", "PHONE_P2P", ["InputPrivacyRuleDisallowAll"]) in calls
    assert ("add_contact", OWNER, "Owner") in calls
    assert "contacts: owner only" in out


def test_harden_loads_dialogs_first_so_the_owner_resolves(env, capsys):
    logged_in(env)
    rc, out, _ = run(env, capsys, ["harden"], resolvable="after_dialogs")
    assert rc == 0 and "owner added as contact" in out
    calls = FakeTg.instances[-1].calls
    assert calls.index(("get_dialogs", 50)) < calls.index(("add_contact", OWNER, "Owner"))


def test_harden_owner_not_resolvable_yet_says_what_to_do(env, capsys):
    logged_in(env)
    rc, out, _ = run(env, capsys, ["harden"], resolvable=False)
    assert rc == userbot.EXIT_TG and "calls: contacts only" in out and "one message" in out


def test_userbot_never_touches_the_bot_token_or_plugin():
    code = open(userbot.__file__, encoding="utf-8").read().split('"""', 2)[2]  # without the module docstring
    for bad in ('/.env', '.env"', "TELEGRAM_BOT_TOKEN", "sign_in_bot", "bot_token=", "telegram-plus", "getUpdates"):
        assert bad not in code
    assert "no_updates=True" in code


# ---- the hub relogin paste (ops/hub/voice-relogin.sh): container paths from env, values from stdin, run.py entry
def test_paths_take_the_hub_container_paths_from_env_but_explicit_dirs_win(tmp_path):
    env = {"VOICE_TG_CONFIG_FILE": "/run/secrets/voice_tg_json", "VOICE_TG_SESSION_DIR": str(tmp_path),
           "VOICE_TG_ACCESS_FILE": "/srv/voice/config/access.json"}
    p = userbot.Paths(env=env)
    assert p.creds == "/run/secrets/voice_tg_json" and p.access == "/srv/voice/config/access.json"
    assert p.secrets == os.path.realpath(str(tmp_path)) and p.session == os.path.join(p.secrets, "voice-tg.session")
    assert {os.path.dirname(x) for x in (p.login_session, p.state, p.login_input, p.login_log, p.login_password)} \
        == {p.secrets}  # everything the login writes stays in the session dir (the only writable mount)
    q = userbot.Paths(secrets=str(tmp_path / "s"), home=str(tmp_path), env=env)
    assert q.creds.endswith("/s/voice-tg.json") and q.access.startswith(str(tmp_path))
    d = userbot.Paths(env={})
    assert d.creds == os.path.expanduser("~/.secrets/voice-tg.json")


def test_spawn_through_run_py_in_a_hub_release(env, monkeypatch):
    import subprocess
    seen = {}

    class P:
        def __init__(self, args, **kw):
            seen.update(args=args, **kw)

    monkeypatch.setattr(subprocess, "Popen", P)
    monkeypatch.setattr(userbot, "ENTRY", ["/srv/releases/r1/voice/run.py", "tg"])
    userbot._spawn_waiter(env, E164, 600)
    assert seen["args"][1:] == ["-I", "-B", "/srv/releases/r1/voice/run.py", "tg", "_wait", "--timeout", "600"]
    assert E164 not in " ".join(seen["args"]) and seen["env"]["VOICE_TG_PHONE"] == E164
    assert seen["start_new_session"] is True


class _Stdin:
    def __init__(self, text):
        self.lines = text.splitlines(True)

    def readline(self):
        return self.lines.pop(0) if self.lines else ""


def test_code_and_phone_from_stdin_never_argv(env, capsys, monkeypatch):
    got = {}

    async def fake_finish(paths, code="", reverse=False, password_file=None, **kw):
        got.update(code=code, pwf=password_file)
        return "logged in: Relay (id 4242)"

    async def fake_start(paths, phone=None, force=False, **kw):
        got.update(phone=phone)
        return "code sent via SMS (waiting 10 min for: login finish CODE)"

    monkeypatch.setattr(userbot, "login_finish", fake_finish)
    monkeypatch.setattr(userbot, "login_start", fake_start)
    monkeypatch.setattr(userbot.sys, "stdin", _Stdin(CODE + "\n"))
    rc, out, _ = run(env, capsys, ["login", "finish", "--code-stdin"])
    assert rc == 0 and got == {"code": CODE, "pwf": None} and out == "logged in: Relay (id 4242)"
    monkeypatch.setattr(userbot.sys, "stdin", _Stdin(PHONE + "\n"))
    rc, out, _ = run(env, capsys, ["login", "start", "--phone-stdin"])
    assert rc == 0 and got["phone"] == PHONE and out == "code sent via SMS (waiting 10 min)"
    monkeypatch.setattr(userbot.sys, "stdin", _Stdin("\n"))  # empty line: the phone from voice-tg.json
    rc, _, _ = run(env, capsys, ["login", "start", "--phone-stdin"])
    assert rc == 0 and got["phone"] is None
    monkeypatch.setattr(userbot.sys, "stdin", _Stdin(""))
    rc, _, err = run(env, capsys, ["login", "finish", "--code-stdin"])
    assert rc == userbot.EXIT_USAGE and "no code on stdin" in err


def test_password_from_stdin_sits_in_a_0600_file_only_until_finish_returns(env, capsys, monkeypatch):
    seen = {}

    async def fake_finish(paths, code="", reverse=False, password_file=None, **kw):
        seen.update(path=password_file, mode=mode(password_file), text=open(password_file).read())
        raise userbot.Fail("wrong 2FA password: write the 2FA password to a 0600 file and run login finish "
                           "--password-file FILE (no code needed)", userbot.EXIT_PASSWORD)

    monkeypatch.setattr(userbot, "login_finish", fake_finish)
    monkeypatch.setattr(userbot.sys, "stdin", _Stdin(PW + "\n"))
    rc, _, err = run(env, capsys, ["login", "finish", "--password-stdin"])
    assert rc == userbot.EXIT_PASSWORD and err == "voice-tg: wrong 2FA password"  # no CLI hint in the prompted flow
    assert seen["path"] == env.login_password and seen["mode"] == 0o600 and seen["text"] == PW + "\n"
    assert not os.path.exists(env.login_password) and not os.path.exists(env.login_password + ".tmp")
    with pytest.raises(SystemExit) as ex:  # one stdin value per call (argparse: not allowed together)
        userbot.main(["login", "finish", "--password-file", "/x", "--code-stdin"], paths=env)
    assert ex.value.code == 2 and "not allowed with" in capsys.readouterr().err


def test_prompted_two_factor_through_the_real_waiter(env, capsys, monkeypatch, tmp_path):
    """--password-stdin end to end on the in-process waiter: the file the waiter reads is gone afterwards."""
    async def main():
        lg = Login(env, password=PW)
        await lg.start()
        assert (await lg.finish(CODE))[0] == userbot.EXIT_PASSWORD
        monkeypatch.setattr(userbot.sys, "stdin", _Stdin(PW + "\n"))
        out = await asyncio.get_running_loop().run_in_executor(
            None, lambda: userbot.main(["login", "finish", "--password-stdin"], paths=env))
        assert out == 0 and await lg.result() == 0
        assert not os.path.exists(env.login_password) and mode(env.session) == 0o600
        lg.assert_clean()
    go(main())
    out, err = capsys.readouterr()
    assert PW not in out + err and "logged in: Relay (id 4242)" in out


def test_cancel_removes_a_left_password_file(env):
    with open(env.login_password, "w") as fh:
        fh.write(PW)
    assert go(userbot.login_cancel(env)) == "no login was waiting" and not os.path.exists(env.login_password)
