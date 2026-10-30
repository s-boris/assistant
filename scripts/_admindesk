"""Shared helpers for ~/bin/letter and ~/bin/pdfform (the "admin desk").

Local only: nothing here sends anything anywhere. --file uploads to the owner's own paperless (LAN) and
--send-by adds an owner reminder; both are explicit opt-in flags.

Test hooks (env):
  ADMIN_DESK_NOW="YYYY-MM-DD HH:MM"  fake clock (letter date, reminder times)
  ADMIN_DESK_PAPERLESS=/path         command used instead of ~/bin/paperless
  ADMIN_DESK_REMIND=/path            command used instead of ~/bin/remind
  LETTER_SIGNATURE=/path.png         signature image instead of ~/.config/letter/signature.png
  LETTER_CONFIG_DIR=/dir             config dir instead of ~/.config/letter
  KK_PROFILE=/path.json              kk-compare profile instead of ~/.config/kk/profile.json
  ASSISTANT_PROFILE=/path.json       owner profile instead of ~/.config/assistant/profile.json
"""
import json, os, re, subprocess, sys
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "lib"))
import assistant_profile  # noqa: E402  (owner name and home address: the sender defaults)

HOME = os.path.expanduser("~")
CONFIG_DIR = os.environ.get("LETTER_CONFIG_DIR") or os.path.join(HOME, ".config", "letter")
SIGNATURE = os.environ.get("LETTER_SIGNATURE") or os.path.join(CONFIG_DIR, "signature.png")
KK_PROFILE = os.environ.get("KK_PROFILE") or os.path.join(HOME, ".config", "kk", "profile.json")
PAPERLESS = os.environ.get("ADMIN_DESK_PAPERLESS") or os.path.join(HOME, "bin", "paperless")
REMIND = os.environ.get("ADMIN_DESK_REMIND") or os.path.join(HOME, "bin", "remind")
FILE_TAG = "Korrespondenz"          # paperless tag for letters and filled forms (used only if it exists)
MM = 72 / 25.4                       # points per mm

MONTHS_DE = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September",
             "Oktober", "November", "Dezember"]
MONTHS_EN = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
             "October", "November", "December"]
WEEKDAYS_DE = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
WEEKDAYS_EN = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def die(msg, code=1):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def warn(msg):
    print(f"warning: {msg}", file=sys.stderr)


def now():
    fake = os.environ.get("ADMIN_DESK_NOW")
    if fake:
        return datetime.strptime(fake, "%Y-%m-%d %H:%M")
    return datetime.now()


def today():
    return now().date()


def parse_date(s, what="date"):
    """YYYY-MM-DD or DD.MM.YYYY."""
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    die(f"bad {what} {s!r} (want YYYY-MM-DD or DD.MM.YYYY)", 2)


def fmt_date(d, lang="de", weekday=False):
    """10. Oktober 2026 / 10 October 2026 (optionally with weekday)."""
    if lang == "en":
        s = f"{d.day} {MONTHS_EN[d.month - 1]} {d.year}"
        return f"{WEEKDAYS_EN[d.weekday()]}, {s}" if weekday else s
    s = f"{d.day}. {MONTHS_DE[d.month - 1]} {d.year}"
    return f"{WEEKDAYS_DE[d.weekday()]}, {s}" if weekday else s


def add_months(d, n):
    y, m = divmod(d.month - 1 + n, 12)
    y += d.year
    m += 1
    import calendar
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def next_workday(d):
    """Sat/Sun -> Monday (public holidays not modelled)."""
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def prev_workday(d):
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def load_sender():
    """Sender data: owner name and home address from the assistant profile, overridden by the kk-compare
    profile, then by ~/.config/letter/sender.json (later wins)."""
    first, last = assistant_profile.get("owner.first_name"), assistant_profile.get("owner.last_name")
    postcode, town = str(assistant_profile.get("home.postcode") or ""), assistant_profile.get("home.town")
    s = {"name": " ".join(x for x in (first, last) if x), "street": assistant_profile.get("home.street"),
         "plz_city": " ".join(x for x in (postcode, town) if x), "city": town, "extra": []}
    for path in (KK_PROFILE, os.path.join(CONFIG_DIR, "sender.json")):
        try:
            with open(path) as f:
                d = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        for k in ("name", "street", "plz_city", "city", "extra", "birthdate"):
            if d.get(k):
                s[k] = d[k]
    if isinstance(s.get("extra"), str):
        s["extra"] = [s["extra"]]
    return s


def slug(s, n=40):
    t = s.lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss"), ("é", "e"), ("è", "e"), ("à", "a")):
        t = t.replace(a, b)
    t = re.sub(r"[^a-z0-9]+", "-", t).strip("-")
    return t[:n].rstrip("-") or "letter"


# ---------------------------------------------------------------- paperless (opt-in --file)

def paperless_tags_and_types():
    """Read-only: existing tag and document-type names (parsed from `paperless meta`)."""
    try:
        out = subprocess.run([PAPERLESS, "meta"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        die(f"paperless meta failed: {e}")
    if out.returncode != 0:
        die(f"paperless meta failed: {out.stderr.strip()[-300:]}")
    kinds, cur = {"tags": [], "correspondents": [], "document types": []}, None
    for line in out.stdout.splitlines():
        m = re.match(r"^(tags|correspondents|document types) \(\d+\):", line)
        if m:
            cur = m.group(1)
            continue
        m = re.match(r"^\s+(.+?)\s+\[id \d+", line)
        if m and cur:
            kinds[cur].append(m.group(1))
    return kinds["tags"], kinds["document types"]


def paperless_file(pdf, title, created, doc_type=None, create_tag=False):
    """Upload pdf to paperless. Tag Korrespondenz if it exists (or create_tag); doc_type only if it exists.
    Returns the paperless output line (document <id>: <url>)."""
    tags, types = paperless_tags_and_types()
    cmd = [PAPERLESS, "upload", pdf, "--title", title, "--created", created.isoformat()]
    notes = []
    tag = next((t for t in tags if t.lower() == FILE_TAG.lower()), None)
    if tag:
        cmd += ["--tag", tag]
    elif create_tag:
        cmd += ["--tag", FILE_TAG, "--create"]
        notes.append(f"created new paperless tag {FILE_TAG!r}")
    else:
        notes.append(f"paperless has no tag {FILE_TAG!r}: filed without a tag "
                     f"(--create-tag adds it; ask the owner first)")
    if doc_type:
        t = next((t for t in types if t.lower() == doc_type.lower()), None)
        if t:
            cmd += ["--type", t]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if out.returncode != 0:
        die(f"paperless upload failed: {(out.stderr or out.stdout).strip()[-400:]}")
    line = (out.stdout.strip().splitlines() or ["uploaded"])[-1]
    return line, notes


# ---------------------------------------------------------------- reminder (opt-in --send-by)

def send_reminder(send_by, text, tag, hour=9):
    """Owner reminder 2 days before send_by at 09:00; if that's past, the next sensible slot up to send_by.
    Returns (datetime, remind output)."""
    n = now()
    at = datetime(send_by.year, send_by.month, send_by.day, hour, 0) - timedelta(days=2)
    if at < n:
        # round up to the next 5 minutes, but not into quiet hours (00:00-08:00) or after 21:00
        at = (n + timedelta(minutes=10)).replace(second=0, microsecond=0)
        at -= timedelta(minutes=at.minute % 5)
        if at.hour < 8:
            at = at.replace(hour=hour, minute=0)
        elif at.hour >= 21:
            at = (at + timedelta(days=1)).replace(hour=hour, minute=0)
        if at.date() > send_by:
            die(f"--send-by {send_by.isoformat()} has already passed (now {n:%Y-%m-%d %H:%M})", 2)
    out = subprocess.run([REMIND, "add", "--owner", "--tag", tag, at.strftime("%Y-%m-%d %H:%M"), text],
                         capture_output=True, text=True, timeout=30)
    if out.returncode != 0:
        die(f"remind failed: {(out.stderr or out.stdout).strip()}")
    return at, out.stdout.strip()


# ---------------------------------------------------------------- signature image

def signature_path():
    return SIGNATURE if os.path.isfile(SIGNATURE) else None


NO_SIGNATURE_HINT = ("no signature image at ~/.config/letter/signature.png: the owner can send a photo of his "
                     "signature on white paper, then `letter signature import PHOTO` stores a cleaned copy")


def clean_signature(src, dst, ink=(20, 30, 90)):
    """Photo/scan of a signature -> transparent PNG: threshold the paper away, trim, recolour the ink."""
    from PIL import Image, ImageOps
    im = ImageOps.exif_transpose(Image.open(src)).convert("L")
    im = ImageOps.autocontrast(im, cutoff=1)
    # ink = darker than the paper; threshold halfway between the dark tail and the paper
    hist = im.histogram()
    total = sum(hist)
    acc, dark = 0, 0
    for v, c in enumerate(hist):
        acc += c
        if acc >= total * 0.01:
            dark = v
            break
    thr = (dark + 230) // 2
    mask = im.point(lambda v: 255 if v < thr else 0)
    bbox = mask.getbbox()
    if not bbox:
        die("no ink found in the image (is it a signature on light paper?)")
    pad = 8
    bbox = (max(0, bbox[0] - pad), max(0, bbox[1] - pad), min(im.width, bbox[2] + pad), min(im.height, bbox[3] + pad))
    mask = mask.crop(bbox)
    # soft edges: alpha from darkness within the crop
    alpha = im.crop(bbox).point(lambda v: 0 if v >= thr else min(255, int((thr - v) * 255 / max(1, thr - dark))))
    out = Image.new("RGBA", mask.size, ink + (0,))
    out.putalpha(alpha)
    if out.width > 1200:
        out = out.resize((1200, int(out.height * 1200 / out.width)), Image.LANCZOS)
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        out.save(f, "PNG")
    os.chmod(dst, 0o600)
    return out.size
