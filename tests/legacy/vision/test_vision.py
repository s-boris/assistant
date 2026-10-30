"""Offline tests for ~/bin/vision (the local paperwork gate of skill vision-actions).
Synthetic images only, drawn at run time with Pillow in a temp dir (no photos, no PII, nothing kept).
VISION_BIN tests a copy before installing. USAGE_STATE points into the temp dir. Run: python3 -I test_vision.py
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # the CI runner: Pillow comes with the CI toolchain's site (on PYTHONPATH, which -I ignores)
    sys.path.append(os.path.join(os.environ.get("ASSISTANT_CI_TOOLCHAIN", "/nonexistent"), "site"))
    from PIL import Image, ImageDraw, ImageFont

BIN = os.environ.get("VISION_BIN") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "scripts", "vision"
)
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

CASES = {
    # name: (lines, expected route, expected kind)
    "poster": (
        [
            "TECHNO NIGHT",
            "Saturday 14.11.2026",
            "Doors 22:00",
            "Club Example, Teststrasse",
            "Line-up: Artist A, Artist B",
            "Tickets CHF 25",
        ],
        "look",
        None,
    ),
    "label": (["Espresso Machine X1-200", "Model: X1-200", "230 V ~ 50 Hz 1200 W", "Made in EU"], "look", None),
    "whiteboard": (["Sprint plan", "1. fix login", "2. write docs", "3. demo Friday"], "look", None),
    "qrbill": (
        [
            "Empfangsschein",
            "Konto / Zahlbar an",
            "Referenz",
            "Zahlteil",
            "Waehrung Betrag",
            "CHF 120.00",
            "Annahmestelle",
        ],
        "vault",
        "bill",
    ),
    "receipt": (
        [
            "Example Store",
            "Quittung",
            "Kaffeemaschine 199.00",
            "Entkalker 12.50",
            "Total CHF 211.50",
            "MWST 8.1% 15.90",
            "Kartenzahlung",
        ],
        "vault",
        "receipt",
    ),
    "letter": (
        [
            "Example Company AG",
            "Betreff: Ihre Anfrage",
            "Sehr geehrter Herr Muster",
            "wir bestaetigen den Eingang.",
            "Freundliche Gruesse",
        ],
        "vault",
        "letter",
    ),
}


def draw(path, lines, size=(1400, 1000)):
    im = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(im)
    f = ImageFont.truetype(FONT, 48)
    y = 60
    for ln in lines:
        d.text((60, y), ln, fill="black", font=f)
        y += 90
    im.save(path, quality=92)


class VisionGate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="vision-tests.")
        cls.env = dict(os.environ, USAGE_STATE=os.path.join(cls.tmp.name, "usage"), USAGE_TEST="1")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_gate(self, path, *extra):
        p = subprocess.run([BIN, "gate", path, *extra], env=self.env, capture_output=True, text=True, timeout=90)
        return p.returncode, p.stdout.strip(), p.stderr.strip()

    def test_cases(self):
        for name, (lines, route, kind) in CASES.items():
            with self.subTest(name=name):
                path = os.path.join(self.tmp.name, name + ".jpg")
                draw(path, lines)
                rc, out, err = self.run_gate(path, "--json")
                res = json.loads(out)
                self.assertEqual((res["route"], res["kind"]), (route, kind), (name, res, err))
                self.assertEqual(rc, 10 if route == "vault" else 0)
                # categories and counts only: no OCR words in the output
                for ln in lines:
                    for w in ln.split():
                        if len(w) > 4 and w.isalpha():
                            self.assertNotIn(w.lower(), out.lower(), name)

    def test_phone_photo_of_paper(self):
        """A 4000x3000 'phone photo': the paper slightly rotated and blurred on a darker table."""
        from PIL import ImageFilter

        paper = os.path.join(self.tmp.name, "paper.png")
        draw(paper, CASES["letter"][0] + ["", "Seite 1"], size=(1700, 2200))
        with Image.open(paper) as pg:
            pg = pg.rotate(4, expand=True, fillcolor=(90, 80, 70)).filter(ImageFilter.GaussianBlur(1.6))
            bg = Image.new("RGB", (4000, 3000), (90, 80, 70))
            bg.paste(pg.resize((int(pg.width * 1.2), int(pg.height * 1.2))), (900, 80))
            path = os.path.join(self.tmp.name, "photo.jpg")
            bg.save(path, quality=80)
        rc, out, err = self.run_gate(path, "--json")
        res = json.loads(out)
        self.assertEqual((rc, res["route"], res["kind"]), (10, "vault", "letter"), res)
        self.assertLess(res["ms"], 30000)

    def test_plain_output_blank_and_bad_input(self):
        blank = os.path.join(self.tmp.name, "blank.png")
        Image.new("RGB", (800, 600), (40, 90, 160)).save(blank)
        rc, out, _ = self.run_gate(blank)
        self.assertEqual((rc, out), (0, "look - 0"))
        notimg = os.path.join(self.tmp.name, "x.txt")
        with open(notimg, "w") as f:
            f.write("Rechnung")
        self.assertEqual(self.run_gate(notimg)[0], 2)
        self.assertEqual(self.run_gate(os.path.join(self.tmp.name, "missing.jpg"))[0], 2)
        p = subprocess.run([BIN, "frob"], env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 2)

    def test_usage_events_tokens_only(self):
        path = os.path.join(self.tmp.name, "u.jpg")
        draw(path, CASES["receipt"][0])
        self.run_gate(path)
        spool = os.path.join(self.tmp.name, "usage", "spool")
        recs = []
        for n in os.listdir(spool):
            with open(os.path.join(spool, n)) as f:
                recs += [json.loads(x) for x in f if x.strip()]
        mine = [r for r in recs if r.get("f") == "skill.vision-actions"]
        self.assertTrue(mine)
        # the route is a result, not a failure: outcome ok (any other exec outcome counts as a failed run + an
        # error in the usage store), the route rides in attrs
        self.assertIn(("exec", "ok", "vault"), [(r["k"], r["o"], (r.get("at") or {}).get("route")) for r in mine])
        self.assertEqual({r["o"] for r in mine if r["k"] == "exec"}, {"ok"})
        self.assertNotIn("Kaffeemaschine", json.dumps(recs))


class ScoreUnit(unittest.TestCase):
    """score() directly on text (no OCR): shapes and thresholds."""

    @classmethod
    def setUpClass(cls):
        import importlib.machinery
        import importlib.util

        loader = importlib.machinery.SourceFileLoader("vision_mod", BIN)
        spec = importlib.util.spec_from_loader("vision_mod", loader)
        cls.m = importlib.util.module_from_spec(spec)
        loader.exec_module(cls.m)

    def test_scores(self):
        s = self.m.score
        self.assertEqual(s("Open Air Festival 2026 Tickets CHF 49.00")[1], 0)
        self.assertEqual(s("Lohnausweis 2025")[0], "tax")
        self.assertGreaterEqual(s("Rechnung Nr 12, zahlbar bis 30.11. Kundennummer 9")[1], 5)
        self.assertLess(s("Rechnung")[1], 5)  # one weak word alone is not enough
        self.assertEqual(s("Pay to CH00 0000 0000 0000 0000 0")[0], "bill")  # IBAN shape (all zeros)
        self.assertEqual(s("Coop 12.50 3.20 Total 15.70 CHF")[0], "receipt")
        self.assertEqual(self.m.fold("Grüße Kündigung"), "gruesse kuendigung")


if __name__ == "__main__":
    unittest.main(verbosity=1)
