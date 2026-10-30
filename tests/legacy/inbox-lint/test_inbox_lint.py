#!/usr/local/bin/python3.13 -I
"""Self-test for inbox_lint.py. Run: python3 -I ~/opt/inbox-lint-tests/test_inbox_lint.py
Placeholder fixtures (fixtures/) prove it catches bad writers (shell, Python, Python in a heredoc, a symlinked
script, a verbatim-helper caller, crontab lines); mutations of the real ~/bin and ~/crontab prove the detection is
not vacuous: stripping the marker from any real writer must make the lint fail. Nothing is written outside a temp dir."""
import importlib.machinery, importlib.util, os, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.realpath(__file__))
FIX = os.path.join(HERE, "fixtures")
LINT = os.path.join(HERE, "inbox_lint.py")
REAL_BIN, REAL_CRONTAB = os.path.expanduser("~/bin"), os.path.expanduser("~/crontab")
REAL_ALLOW = os.path.join(HERE, "allowlist.txt")


def load():
    loader = importlib.machinery.SourceFileLoader("inbox_lint", LINT)
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("inbox_lint", loader))
    loader.exec_module(mod)
    return mod


L = load()


def failing(problems):
    return {p.split(":", 1)[0] for p in problems}


class Fixtures(unittest.TestCase):
    def setUp(self):
        self.allow = os.path.join(FIX, "allowlist.txt")

    def test_bad_writers_caught(self):
        problems, _, infos = L.lint(os.path.join(FIX, "bin"), None, self.allow)
        self.assertEqual(failing(problems), {"bad-shell", "bad-python", "bad-heredoc", "linked-bad", "plain-caller"})
        writers = {i.rel for i in infos if i.sites}
        self.assertNotIn("reader", writers)          # reads the inbox, writes elsewhere: not a writer
        for ok in ("good-shell", "variant-python", "helper-caller", "plain-caller-ok", "lib/addlib.py",
                   "lib/plainlib.py"):
            self.assertIn(ok, writers)

    def test_how_each_passes(self):
        infos = {i.rel: i for i in L.scan_bin(os.path.join(FIX, "bin"))}
        allow, _ = L.load_allowlist(self.allow)
        self.assertEqual(L.verdict(infos["helper-caller"], allow), (True, True, "all writes via marker-adding helpers"))
        self.assertEqual(L.verdict(infos["lib/plainlib.py"], allow), (True, True, "allowlisted"))
        self.assertEqual(L.verdict(infos["lib/plainlib.py"], {}), (True, False, "no marker"))
        self.assertEqual(L.verdict(infos["variant-python"], allow)[2], "marker in script")
        self.assertFalse(infos["bad-shell"].marker)  # the marker in a comment does not count

    def test_crontab(self):
        p, n = L.lint_crontab(os.path.join(FIX, "crontab-bad"))
        self.assertEqual(n, 2)
        self.assertEqual(len(p), 2)
        self.assertIn("crontab:2:", p[0])
        self.assertTrue(p[1].startswith("crontab:3:") and "/home/node/inbox" in p[1])
        self.assertEqual(L.lint_crontab(os.path.join(FIX, "crontab-good")), ([], 1))

    def test_allowlist_stale_and_reasonless(self):
        with tempfile.TemporaryDirectory() as t:
            a = os.path.join(t, "allow.txt")
            with open(a, "w") as f:
                f.write("# comment\nlib/plainlib.py  # helper\nreader  # x\ngood-shell  # x\nmissing  # x\nbad-python\n")
            problems, _, _ = L.lint(os.path.join(FIX, "bin"), None, a)
        text = "\n".join(problems)
        self.assertIn("'reader' (not an inbox writer", text)
        self.assertIn("'good-shell' (passes without it)", text)
        self.assertIn("'missing' (no such script", text)
        self.assertIn("allowlist:6: entry 'bad-python' has no '# reason'", text)
        self.assertNotIn("bad-python: writes", text)  # allowlisted (reasonless entries fail separately)

    def test_cli_exit_codes(self):
        def run(*args):
            return subprocess.run([sys.executable, "-I", LINT, *args], stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True, timeout=60)
        r = run("--bin", os.path.join(FIX, "bin"), "--crontab", os.path.join(FIX, "crontab-bad"),
                "--allowlist", self.allow)
        self.assertEqual(r.returncode, 1)
        self.assertIn("FAIL bad-shell:", r.stdout)
        with tempfile.TemporaryDirectory() as t:
            good = os.path.join(t, "bin")
            os.mkdir(good)
            with open(os.path.join(FIX, "bin", "good-shell")) as src, open(os.path.join(good, "good-shell"), "w") as d:
                d.write(src.read())
            r = run("--bin", good, "--crontab", os.path.join(FIX, "crontab-good"), "--allowlist", os.devnull)
            self.assertEqual(r.returncode, 0, r.stdout)
            self.assertEqual(run("--bin", os.path.join(t, "nope")).returncode, 2)


@unittest.skipUnless(os.path.isdir(REAL_BIN), "no ~/bin")
class RealSetup(unittest.TestCase):
    """The detection works on the real scripts: every real writer that passes by its marker fails without it."""

    @classmethod
    def setUpClass(cls):
        cls.files = L.read_bin(REAL_BIN)
        cls.infos = L.analyze(cls.files)
        cls.allow, _ = L.load_allowlist(REAL_ALLOW)

    def test_finds_many_writers(self):
        self.assertGreaterEqual(sum(1 for i in self.infos if i.sites), 20)  # guards against a regex that finds none

    def test_stripping_the_marker_fails(self):
        registry, rels = L.build_registry(self.infos), [i.rel for i in self.infos]
        checked = 0
        for info in self.infos:
            if L.verdict(info, self.allow)[2] != "marker in script":
                continue
            m = L.FileInfo(info.rel, L.MARKER.sub("Placeholder text", info.text))
            L.taint(m)
            L.direct_sites(m)
            L.helper_sites(m, registry, rels)
            if all(how == "helper call" and adds for _, how, _, adds in m.sites):
                continue
            self.assertEqual(L.verdict(m, self.allow)[1], False, f"{info.rel} still passes without its marker")
            checked += 1
        self.assertGreaterEqual(checked, 20)

    @unittest.skipUnless(os.path.isfile(REAL_CRONTAB), "no ~/crontab")
    def test_crontab_mutations(self):
        with open(REAL_CRONTAB) as f:
            text = f.read()
        lines = text.split("\n")
        writers = [i for i, l in enumerate(lines)
                   if not L.comment_line(l) and "/inbox/" in l and L.MARKER.search(l) and ">" in l]
        self.assertTrue(writers, "no inbox-writing crontab lines found")
        with tempfile.TemporaryDirectory() as t:
            p = os.path.join(t, "crontab")
            for i in writers:
                for old, new, want in ((None, None, "without"), ("$HOME/inbox", "/home/node/inbox", "literal")):
                    mut = list(lines)
                    mut[i] = L.MARKER.sub("Placeholder", mut[i]) if old is None else mut[i].replace(old, new)
                    if mut[i] == lines[i]:
                        continue
                    with open(p, "w") as f:
                        f.write("\n".join(mut))
                    probs, _ = L.lint_crontab(p)
                    self.assertTrue(any(f"crontab:{i + 1}:" in x and want in x for x in probs), (i + 1, want, probs))


if __name__ == "__main__":
    unittest.main(verbosity=1)
