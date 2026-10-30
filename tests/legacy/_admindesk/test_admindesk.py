#!/usr/bin/env python3
"""Offline smoke tests for ~/bin/_admindesk.py (shared helpers of letter and pdfform), run by ./run.sh.

run.sh sets the env hooks before import (the module reads them at import time): ADMIN_DESK_NOW, ADMIN_DESK_PAPERLESS
and ADMIN_DESK_REMIND (fakes that log argv), LETTER_CONFIG_DIR, KK_PROFILE, ASSISTANT_PROFILE (synthetic fixture).
Nothing is uploaded, no reminder is set, no network."""

import json
import os
import sys
from datetime import date, datetime

T = os.environ["ADT_DIR"]
SRC = os.environ.get("ADMINDESK_SRC") or "/home/node/bin/_admindesk.py"  # ADMINDESK_SRC: test a copy
sys.path.insert(0, os.path.dirname(SRC))
import importlib.util  # noqa: E402

spec = importlib.util.spec_from_file_location("_admindesk", SRC)
ad = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ad)

passed = failed = 0


def check(name, cond, got=None):
    global passed, failed
    if cond:
        passed += 1
        print(f"ok   - {name}")
    else:
        failed += 1
        print(f"FAIL - {name}" + (f"\n       got: {got!r}" if got is not None else ""))


def exits(fn, *a, **kw):
    """-> (exit code or None, stderr text) for a call that may die()."""
    import io

    old, sys.stderr = sys.stderr, io.StringIO()
    try:
        fn(*a, **kw)
        return None, sys.stderr.getvalue()
    except SystemExit as e:
        return e.code, sys.stderr.getvalue()
    finally:
        sys.stderr = old


def calls(name):
    p = os.path.join(T, name + ".log")
    return [json.loads(x) for x in open(p).read().splitlines()] if os.path.exists(p) else []


def reset(name):
    p = os.path.join(T, name + ".log")
    if os.path.exists(p):
        os.remove(p)


print("# A. env hooks and dates")
check(
    "hooks: fake paperless/remind used",
    ad.PAPERLESS.startswith(T) and ad.REMIND.startswith(T),
    (ad.PAPERLESS, ad.REMIND),
)
check("now() honours ADMIN_DESK_NOW", ad.now() == datetime(2026, 10, 10, 11, 0), ad.now())
check("today()", ad.today() == date(2026, 10, 10))
check("parse_date ISO", ad.parse_date("2026-11-30") == date(2026, 11, 30))
check("parse_date Swiss", ad.parse_date(" 30.11.2026 ") == date(2026, 11, 30))
check("parse_date short year", ad.parse_date("1.2.27") == date(2027, 2, 1))
rc, err = exits(ad.parse_date, "2026-13-01", "send-by date")
check("parse_date bad -> exit 2", rc == 2, rc)
check("parse_date bad -> names the field", "bad send-by date" in err, err)
rc, _ = exits(ad.parse_date, None)
check("parse_date None -> exit 2 (no crash)", rc == 2, rc)
check("fmt_date de", ad.fmt_date(date(2026, 10, 10)) == "10. Oktober 2026", ad.fmt_date(date(2026, 10, 10)))
check(
    "fmt_date en + weekday",
    ad.fmt_date(date(2026, 10, 10), "en", True) == "Saturday, 10 October 2026",
    ad.fmt_date(date(2026, 10, 10), "en", True),
)
check("fmt_date de + weekday", ad.fmt_date(date(2026, 3, 2), weekday=True) == "Montag, 2. März 2026")
check("add_months clamps 31.01 + 1 -> 28.02", ad.add_months(date(2026, 1, 31), 1) == date(2026, 2, 28))
check("add_months over year end", ad.add_months(date(2026, 11, 30), 3) == date(2027, 2, 28))
check("add_months negative", ad.add_months(date(2026, 3, 31), -1) == date(2026, 2, 28))
check("next_workday Sat -> Mon", ad.next_workday(date(2026, 10, 10)) == date(2026, 10, 12))
check("prev_workday Sun -> Fri", ad.prev_workday(date(2026, 10, 11)) == date(2026, 10, 9))
check("workday unchanged on a Wednesday", ad.next_workday(date(2026, 10, 14)) == date(2026, 10, 14))
check(
    "slug umlauts",
    ad.slug("Kündigung Fitnessstudio Größe") == "kuendigung-fitnessstudio-groesse",
    ad.slug("Kündigung Fitnessstudio Größe"),
)
check("slug empty -> letter", ad.slug("!!!") == "letter")
check("slug length cap, no trailing dash", ad.slug("a" * 39 + " b", 40) == "a" * 39, ad.slug("a" * 39 + " b", 40))

print("# B. sender layering")
s = ad.load_sender()
check("sender from the fixture profile", s["name"] == "Robin Tester" and s["plz_city"] == "4321 Testhausen", s)
check("sender extra is a list", s["extra"] == [], s["extra"])
os.makedirs(ad.CONFIG_DIR, exist_ok=True)
with open(ad.KK_PROFILE, "w") as f:
    json.dump({"street": "Kkweg 1", "birthdate": "1990-01-01"}, f)
with open(os.path.join(ad.CONFIG_DIR, "sender.json"), "w") as f:
    json.dump({"street": "Senderweg 2", "extra": "Versicherten-Nr. siehe Karte"}, f)
s = ad.load_sender()
check("sender.json wins over kk profile", s["street"] == "Senderweg 2", s["street"])
check("kk profile adds birthdate", s.get("birthdate") == "1990-01-01", s)
check("string extra -> list", s["extra"] == ["Versicherten-Nr. siehe Karte"], s["extra"])
with open(os.path.join(ad.CONFIG_DIR, "sender.json"), "w") as f:
    f.write("{broken")
check("broken sender.json is skipped", ad.load_sender()["street"] == "Kkweg 1")
os.remove(ad.KK_PROFILE)
os.remove(os.path.join(ad.CONFIG_DIR, "sender.json"))

print("# C. paperless (fake): meta parsing and upload command")
reset("paperless")
tags, types = ad.paperless_tags_and_types()
check("meta: tags parsed", tags == ["Korrespondenz", "Rechnung"], tags)
check("meta: document types parsed", types == ["Brief", "Formular"], types)
line, notes = ad.paperless_file("/x/k.pdf", "Kündigung Test", date(2026, 10, 10), doc_type="brief")
up = calls("paperless")[-1]["argv"]
check(
    "upload argv",
    up
    == [
        "upload",
        "/x/k.pdf",
        "--title",
        "Kündigung Test",
        "--created",
        "2026-10-10",
        "--tag",
        "Korrespondenz",
        "--type",
        "Brief",
    ],
    up,
)
check("upload returns the last output line", line == "document 123: http://paperless.test/123", line)
check("no notes when the tag exists", notes == [], notes)
os.environ["FAKE_META"] = "notag"
line, notes = ad.paperless_file("/x/k.pdf", "T", date(2026, 10, 10), doc_type="Unknown")
up = calls("paperless")[-1]["argv"]
check("missing tag: no --tag, no unknown --type", "--tag" not in up and "--type" not in up, up)
check("missing tag: note says ask the owner", notes and "ask the owner" in notes[0], notes)
ad.paperless_file("/x/k.pdf", "T", date(2026, 10, 10), create_tag=True)
up = calls("paperless")[-1]["argv"]
check("create_tag: --tag Korrespondenz --create", up[-3:] == ["--tag", "Korrespondenz", "--create"], up)
os.environ["FAKE_META"] = "fail"
rc, err = exits(ad.paperless_tags_and_types)
check("meta failure -> exit 1 with message", rc == 1 and "paperless meta failed" in err, (rc, err))
os.environ["FAKE_META"] = ""
os.environ["FAKE_UPLOAD_RC"] = "3"
rc, err = exits(ad.paperless_file, "/x/k.pdf", "T", date(2026, 10, 10))
check("upload failure -> exit 1 with message", rc == 1 and "paperless upload failed" in err, (rc, err))
os.environ["FAKE_UPLOAD_RC"] = "0"

print("# D. reminder (fake remind)")
reset("remind")
at, out = ad.send_reminder(date(2026, 10, 20), "Kündigung abschicken", "letter")
r = calls("remind")[-1]["argv"]
check("2 days before at 09:00", at == datetime(2026, 10, 18, 9, 0), at)
check("remind argv", r == ["add", "--owner", "--tag", "letter", "2026-10-18 09:00", "Kündigung abschicken"], r)
check("remind output returned", out == "reminder r1 set", out)
at, _ = ad.send_reminder(date(2026, 10, 11), "x", "letter")
check("too late for 2 days: next 5-min slot after now+10", at == datetime(2026, 10, 10, 11, 10), at)
os.environ["ADMIN_DESK_NOW"] = "2026-10-10 22:30"
at, _ = ad.send_reminder(date(2026, 10, 11), "x", "letter")
check("after 21:00: next morning 09:00", at == datetime(2026, 10, 11, 9, 0), at)
os.environ["ADMIN_DESK_NOW"] = "2026-10-10 03:00"
at, _ = ad.send_reminder(date(2026, 10, 10), "x", "letter")
check("quiet hours: 09:00 the same day", at == datetime(2026, 10, 10, 9, 0), at)
os.environ["ADMIN_DESK_NOW"] = "2026-10-10 22:30"
n = len(calls("remind"))
rc, err = exits(ad.send_reminder, date(2026, 10, 10), "x", "letter")
check("send-by passed -> exit 2, no reminder", rc == 2 and len(calls("remind")) == n, (rc, err))
os.environ["ADMIN_DESK_NOW"] = "2026-10-10 11:00"
os.environ["FAKE_REMIND_RC"] = "1"
rc, err = exits(ad.send_reminder, date(2026, 10, 20), "x", "letter")
check("remind failure -> exit 1 with message", rc == 1 and "remind failed" in err, (rc, err))
os.environ["FAKE_REMIND_RC"] = "0"

print("# E. signature")
check("no signature file -> None", ad.signature_path() is None)
try:
    from PIL import Image, ImageDraw
except ImportError:  # the CI runner: Pillow comes with the CI toolchain's site (on PYTHONPATH, which -I ignores)
    sys.path.append(os.path.join(os.environ.get("ASSISTANT_CI_TOOLCHAIN", "/nonexistent"), "site"))
    from PIL import Image, ImageDraw

src, dst = os.path.join(T, "sig.jpg"), os.path.join(T, "cfg", "signature.png")
im = Image.new("RGB", (800, 400), (245, 245, 240))
ImageDraw.Draw(im).line([(100, 250), (300, 150), (500, 260), (700, 140)], fill=(20, 20, 20), width=12)
im.save(src)
w, h = ad.clean_signature(src, dst)
check("clean_signature trims to the ink", 600 <= w <= 640 and 120 <= h <= 160, (w, h))
check(
    "clean_signature: PNG with alpha, mode 0600",
    Image.open(dst).mode == "RGBA" and oct(os.stat(dst).st_mode & 0o777) == "0o600",
    (Image.open(dst).mode, oct(os.stat(dst).st_mode)),
)
check("signature_path finds it", ad.signature_path() == dst, ad.signature_path())
blank = os.path.join(T, "blank.png")
Image.new("RGB", (200, 100), (250, 250, 250)).save(blank)
rc, err = exits(ad.clean_signature, blank, os.path.join(T, "out.png"))
check("blank image -> exit 1 'no ink'", rc == 1 and "no ink" in err, (rc, err))

print("# F. never sends")
code = open(SRC).read()
check("no smtp/http client in the module", not any(x in code for x in ("smtplib", "requests", "urllib", "socket")))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
