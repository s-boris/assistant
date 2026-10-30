import os, shutil, stat, subprocess, sys, tempfile, unittest
from pathlib import Path
# BOTLIB_PATH=/copy/botlib.py tests a copy (`assistant ci` passes its staged overlay); default: the live ~/bin/lib one.
BOTLIB = os.environ.get("BOTLIB_PATH") or os.path.expanduser("~/bin/lib/botlib.py")
sys.path.insert(0, os.path.dirname(os.path.realpath(BOTLIB)))
import botlib  # noqa: E402
assert os.path.realpath(botlib.__file__) == os.path.realpath(BOTLIB), botlib.__file__


def script(d, name, body):
    p = Path(d) / name
    p.write_text("#!/bin/sh\n" + body + "\n")
    p.chmod(0o755)
    return str(p)


class T(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def test_atomic_write_creates_dirs_and_leaves_no_tmp(self):
        p = Path(self.d) / "a" / "b" / "x.txt"
        botlib.atomic_write(p, "hé\n")
        botlib.atomic_write(str(p), "two\n")
        self.assertEqual(p.read_text(encoding="utf-8"), "two\n")
        self.assertEqual(os.listdir(p.parent), ["x.txt"])

    def test_json_roundtrip_and_defaults(self):
        p = Path(self.d) / "s" / "state.json"
        self.assertEqual(botlib.load_json(p, {"d": 1}), {"d": 1})
        botlib.save_json(p, {"b": 1, "a": [1]}, indent=1, sort_keys=True)
        self.assertEqual(p.read_text(), '{\n "a": [\n  1\n ],\n "b": 1\n}')
        self.assertEqual(botlib.load_json(p, None), {"a": [1], "b": 1})
        p.write_text("{broken")
        self.assertIsNone(botlib.load_json(p))

    def test_write_inbox_never_overwrites(self):
        ib = Path(self.d) / "inbox"
        f1 = botlib.write_inbox(ib, "cw", "one")
        f2 = botlib.write_inbox(ib, "cw", "two")
        self.assertNotEqual(f1, f2)
        self.assertRegex(f1.name, r"^cw-\d+\.md$")
        self.assertRegex(f2.name, r"^cw-\d+(-\d+)?\.md$")
        self.assertEqual(sorted(p.read_text() for p in ib.iterdir()), ["one", "two"])

    def test_send_tg(self):
        log = Path(self.d) / "sent"
        ok = script(self.d, "tg-ok", f'printf "%s" "$1" > {log}')
        bad = script(self.d, "tg-bad", 'echo "boom: no chat" >&2; exit 3')
        self.assertEqual(botlib.send_tg(ok, "hi there"), (True, ""))
        self.assertEqual(log.read_text(), "hi there")
        self.assertEqual(botlib.send_tg(bad, "x"), (False, "boom: no chat"))
        env = script(self.d, "tg-env", f'printf "%s|%s" "${{TG_SOFT:-}}" "$1" > {log}')
        os.environ.pop("TG_SOFT", None)
        self.assertEqual(botlib.send_tg(env, "alert")[0], True)
        self.assertEqual(log.read_text(), "|alert")          # default: urgent (no TG_SOFT), argv unchanged
        self.assertEqual(botlib.send_tg(env, "report", soft=True)[0], True)
        self.assertEqual(log.read_text(), "1|report")        # soft: TG_SOFT=1, argv unchanged
        self.assertNotIn("TG_SOFT", os.environ)              # the caller's env is left alone
        r = botlib.send_tg(os.path.join(self.d, "missing"), "x")
        self.assertFalse(r[0])
        self.assertLessEqual(len(r[1]), 200)

    def test_lockdown_mode(self):
        for out, want in (("verbs", "verbs"), ("emulate", "emulate"), ("root", "root"), ("junk", "root")):
            self.assertEqual(botlib.lockdown_mode(script(self.d, "us-" + out, f"echo {out}")), want)
        self.assertEqual(botlib.lockdown_mode(os.path.join(self.d, "missing")), "root")

    def test_delegates_to_core(self):
        """The deployed shim finds the harness core next to it (~/bin/lib/core, or lib/core in a staged overlay)."""
        self.assertTrue(botlib.CORE)
        self.assertTrue(botlib._js.__name__.endswith("jsonstate"))

    def test_private_file_stays_private(self):
        p = Path(self.d) / "state.json"
        botlib.save_json(p, {"a": 1})
        os.chmod(p, 0o600)
        botlib.save_json(p, {"a": 2})
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
        self.assertEqual(botlib.load_json(p), {"a": 2})

    def test_fallback_without_core(self):
        """A copy with no core next to it still works (the pre-core bodies)."""
        d = Path(self.d) / "copy"
        d.mkdir()
        shutil.copy(BOTLIB, d / "botlib.py")
        code = (
            "import sys; sys.path.insert(0, sys.argv[1]); import botlib, pathlib\n"
            "assert not botlib.CORE\n"
            "p = pathlib.Path(sys.argv[1]) / 's' / 'x.json'\n"
            "botlib.save_json(p, {'k': [1]}, indent=1)\n"
            "assert botlib.load_json(p) == {'k': [1]} and botlib.load_json(p.with_name('no'), 5) == 5\n"
            "a = botlib.write_inbox(pathlib.Path(sys.argv[1]) / 'in', 'fb', 'one')\n"
            "b = botlib.write_inbox(pathlib.Path(sys.argv[1]) / 'in', 'fb', 'two')\n"
            "assert a != b and a.read_text() == 'one' and b.read_text() == 'two'\n"
            "print('fallback ok')\n"
        )
        env = dict(os.environ, HOME=self.d)
        r = subprocess.run([sys.executable, "-I", "-c", code, str(d)], capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("fallback ok", r.stdout)


if __name__ == "__main__":
    r = unittest.main(exit=False, verbosity=0).result
    n, f = r.testsRun, len(r.failures) + len(r.errors)
    print(f"{n - f} passed, {f} failed")
    sys.exit(1 if f else 0)
