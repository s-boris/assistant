"""secretscan: the hub fingerprint engine (v2 Phase 2 H10). Stdlib unittest, temp sources, synthetic values only.

Run: python3 -I -m unittest discover -s ~/opt/secretscan-tests -v
Env: SECRETSCAN_UNDER_TEST (default ~/bin/secretscan.py), SECRETSCAN_ORIG (the pre-H10 copy, for the parity case),
HUBREDACT (default: this tree's packages/hub/hubredact.py, the hub's export, so both sides agree on the format).
"""

import contextlib
import importlib.util
import io
import json
import os
import secrets
import sys
import tempfile
import time
import unittest
from pathlib import Path

HOME = Path(os.path.expanduser("~"))
UNDER = Path(os.environ.get("SECRETSCAN_UNDER_TEST") or HOME / "bin" / "secretscan.py")
ORIG = os.environ.get("SECRETSCAN_ORIG")
HUBREDACT = Path(os.environ.get("HUBREDACT") or Path(__file__).resolve().parents[3] / "packages" / "hub" / "hubredact.py")


def load(path, tag):
    spec = importlib.util.spec_from_file_location(f"ss_{tag}_{time.monotonic_ns()}", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def tok(n=32):
    return secrets.token_urlsafe(n)[:n]


class Base(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        self.src = self.root / "home"
        (self.src / ".secrets").mkdir(parents=True)
        self.local = tok(40)
        (self.src / ".secrets" / "local-token").write_text(self.local + "\n")
        self.hub_only = tok(36)
        self.card = "4111111111111111"  # secretscan:allow
        self.fpfile = self.root / "hub-fingerprints.json"
        self.env = {k: os.environ.get(k) for k in ("SECRETSCAN_SOURCES", "SECRETSCAN_FP_FILE", "SECRETSCAN_ENGINE")}
        os.environ["SECRETSCAN_SOURCES"] = str(self.src)
        os.environ["SECRETSCAN_FP_FILE"] = str(self.fpfile)
        os.environ.pop("SECRETSCAN_ENGINE", None)
        self.hr = load(HUBREDACT, "hubredact")
        files = {"vault_token": self.hub_only, "local_copy": self.local, "card.json": json.dumps({"number": self.card})}
        self.key = secrets.token_bytes(32)
        self.export = self.hr.Store.from_texts(files).fingerprints(self.key)
        self.fpfile.write_text(json.dumps(self.export))
        self.ss = load(UNDER, "new")

    def tearDown(self):
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.td.cleanup()

    def fresh(self):
        self.ss = load(UNDER, "new")
        return self.ss


class FingerprintEngine(Base):
    def test_hub_only_value_is_found_by_fingerprint(self):
        for text in (f"x {self.hub_only} y", f"Bearer{self.hub_only}", f"k={self.hub_only}&z=1", self.hub_only,
                     f'{{"t": "{self.hub_only}"}}', f"aaaaaaaaaaaaaaaaaaaaaaaa{self.hub_only}bbbbbbbbbbbbbb"):
            self.assertIn("value:hub:vault_token", self.ss.scan(text), text[:10])
        self.assertEqual(self.ss.scan("nothing secret here " * 50), [])

    def test_card_number_by_fingerprint_with_separators(self):
        self.assertIn("value:hub:card.json:number", self.ss.scan("pay 4111 1111 1111 1111 now"))  # secretscan:allow

    def test_local_values_stay_on_the_plain_path_and_are_skipped(self):
        st = self.ss.fp_state()
        self.assertEqual(st["engine"], "fp")
        self.assertEqual(st["skipped"], 1)  # local_copy: matched by the plain search already
        self.assertEqual(self.ss.scan(f"a {self.local} b"), ["value:secrets/local-token:line1"])

    def test_plain_engine_ignores_the_export(self):
        os.environ["SECRETSCAN_ENGINE"] = "plain"
        ss = self.fresh()
        self.assertEqual(ss.scan(f"x {self.hub_only} y"), [])
        self.assertEqual(ss.fp_state()["items"], 0)

    def test_redact_masks_fingerprint_hits(self):
        out = self.ss.redact(f"a {self.hub_only} b {self.local} c")
        self.assertNotIn(self.hub_only, out)
        self.assertNotIn(self.local, out)
        self.assertEqual(out.count("[redacted]"), 2)

    def test_coverage(self):
        rows = dict(self.ss.coverage())
        self.assertEqual(rows, {"secrets/local-token:line1": True})
        (self.src / ".secrets" / "new-key").write_text(tok(30) + "\n")
        rows = dict(self.fresh().coverage(["secrets/new"]))
        self.assertEqual(rows, {"secrets/new-key:line1": False})
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            self.assertEqual(self.ss.main(["--coverage"]), 1)
        self.fpfile.unlink()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.fresh().main(["--coverage"]), 2)

    def test_corrupt_export_fails_closed(self):
        self.fpfile.write_text('{"v": 1, "alg": "hmac-sha256", "key": "zz"}')
        err = io.StringIO()
        sys.stdin = io.StringIO("hello")
        try:
            with contextlib.redirect_stderr(err):
                self.assertEqual(self.fresh().main(["--stdin"]), 2)
        finally:
            sys.stdin = sys.__stdin__
        self.assertIn("hub fingerprint file unusable", err.getvalue())

    def test_sources_lists_names_never_values(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(self.ss.main(["--sources"]), 0)
        both = out.getvalue() + err.getvalue()
        self.assertIn("hub:vault_token", both)
        for v in (self.hub_only, self.local, self.card, self.export["key"]):
            self.assertNotIn(v, both)

    def test_no_export_means_the_old_behaviour(self):
        self.fpfile.unlink()
        ss = self.fresh()
        self.assertEqual(ss.fp_state()["items"], 0)
        self.assertEqual(ss.scan(f"x {self.hub_only} y"), [])

    def test_speed_one_megabyte(self):
        many = {f"t{i}": tok(20 + i) for i in range(40)}
        self.fpfile.write_text(json.dumps(self.hr.Store.from_texts(many).fingerprints(self.key)))
        ss = self.fresh()
        code = "".join(p.read_text(errors="replace") for p in sorted(UNDER.parent.glob("*.py"))[:40]) or "x = 1\n"
        text = (code * (1_000_000 // len(code) + 1))[:1_000_000]
        t0 = time.perf_counter()
        ss.scan(text + " " + many["t7"])
        dt = time.perf_counter() - t0
        self.assertLess(dt, 4.0, f"{dt:.2f}s for 1 MB")
        print(f"\n  fp scan of 1 MB: {dt:.2f}s", file=sys.stderr)


@unittest.skipUnless(ORIG, "SECRETSCAN_ORIG not set")
class Parity(Base):
    def test_same_hits_as_before_without_an_export(self):
        self.fpfile.unlink()
        (self.src / ".secrets" / "media.json").write_text(json.dumps({"radarr": {"key": tok(32)}, "url": "http://x"}))
        (self.src / ".ssh").mkdir()
        new, old = self.fresh(), load(ORIG, "orig")
        texts = [f"a {self.local} b ghp_" + "A" * 36, "-----BEGIN PRIVATE KEY-----", "4111 1111 1111 1111 x",  # secretscan:allow
                 json.dumps(new.load_secrets())[:0] + "plain words only"]
        for t in texts:
            self.assertEqual(new.find(t), old.find(t))
            self.assertEqual(new.redact(t), old.redact(t))


if __name__ == "__main__":
    unittest.main()
