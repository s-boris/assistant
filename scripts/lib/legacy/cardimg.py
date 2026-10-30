"""cardimg: phone-width picture cards (1080x1350, dark theme) for Telegram instant commands.

    from cardimg import Card, photo_json
    c = Card("Server space", "as of 20.10 03:17")
    c.big("86 %", "full · 18.0 TB free of 132 TB")
    c.progress(86.4)
    c.line_chart([(0, 110.2), (1, 110.9)], limit=(116.0, "full"), projection=[(1, 110.9), (80, 116.0)])
    path = c.save("space")                    # $CARDIMG_DIR or ~/.local/state/cardimg/space.png
    print(photo_json(path, "caption", "fallback text"))

The telegram-plus plugin (0.1.18+) sends a picture when an instant command prints exactly
{"photo": PATH, "caption": ..., "text": ...}; it only accepts files under ~/.local/state/cardimg or /tmp.
Pillow only (python3-pil); fonts: Noto Sans, else DejaVu Sans, else Pillow's default. No emoji in images
(Pillow can't draw colour emoji at these sizes): emoji belong in the caption.
Posters: c.poster_grid(items) = a collage of 1-6 poster tiles filling the rest of the card, c.poster_rows(items) =
one row per item (poster left, text right; c.trim() then cuts the unused bottom); items are dicts {image: path|None, title, lines: [str|(str, colour)],
progress: 0-100|None, rating: float|None, badge: str}. Posters are fitted whole on a blurred copy of themselves; a
missing or unreadable image gets a placeholder tile with the title's initials.
Trips: c.board(rows) = a departure board (one panel per ride: time, line pills, destination, platform, coloured
status, a note line); c.route(label, stops, hops) = one journey as a line of stops (flights); c.trip_strip(legs, start,
end) = a timeline with one coloured segment per place leg; c.tiles(items) = equal columns with a colour bar on top.
Noto Sans has no → or ★: c.rich(x, y, s, f) draws text with both glyphs drawn (c.arrow, c.star).
Elements are stacked top-down; each returns the y it ended at. Text that doesn't fit is cut with "…".
"""
import json
import os
import re
import tempfile

from PIL import Image, ImageDraw, ImageFont

W, H = 1080, 1350
PAD = 56
BG = (15, 17, 21)
PANEL = (26, 29, 36)
GRID = (44, 49, 58)
TEXT = (234, 236, 240)
TEXT2 = (150, 157, 168)
ACCENT = (88, 166, 255)
GOOD = (63, 200, 120)
WARN = (255, 180, 60)
BAD = (255, 92, 92)
FAINT = (60, 84, 120)

FONT_DIRS = {
    False: ("/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    True: ("/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
}
_fonts = {}


def font(size, bold=False):
    key = (size, bold)
    if key not in _fonts:
        f = None
        for p in FONT_DIRS[bold]:
            if os.path.exists(p):
                try:
                    f = ImageFont.truetype(p, size)
                    break
                except OSError:
                    pass
        _fonts[key] = f or ImageFont.load_default()
    return _fonts[key]


def out_dir():
    return os.path.expanduser(os.environ.get("CARDIMG_DIR") or "~/.local/state/cardimg")


def photo_json(path, caption="", text=""):
    """The plugin's picture answer (one line of JSON)."""
    o = {"photo": path}
    if caption:
        o["caption"] = caption
    if text:
        o["text"] = text
    return json.dumps(o, ensure_ascii=False)


def severity(pct, warn=90, bad=95):
    return BAD if pct >= bad else WARN if pct >= warn else ACCENT


STAR = (255, 196, 70)


def poster_image(path, w, h):
    """The image at path fitted whole into w x h, centred on a blurred, darkened cover-fill of itself; None if unreadable."""
    from PIL import ImageEnhance, ImageFilter
    try:
        src = Image.open(path).convert("RGB")
    except Exception:
        return None
    w, h = max(1, int(w)), max(1, int(h))
    cover = max(w / src.width, h / src.height)
    cw, ch = max(1, round(src.width * cover)), max(1, round(src.height * cover))
    bg = src.resize((max(1, cw // 8), max(1, ch // 8)), Image.BILINEAR).resize((cw, ch), Image.BILINEAR)
    bg = bg.crop(((cw - w) // 2, (ch - h) // 2, (cw - w) // 2 + w, (ch - h) // 2 + h)).filter(ImageFilter.GaussianBlur(6))
    tile = ImageEnhance.Brightness(bg).enhance(0.45)
    fit = min(w / src.width, h / src.height)
    fw, fh = max(1, round(src.width * fit)), max(1, round(src.height * fit))
    tile.paste(src.resize((fw, fh), Image.LANCZOS), ((w - fw) // 2, (h - fh) // 2))
    return tile


def placeholder_image(w, h, title):
    """A tile for a missing poster: the title's initials on the panel colour."""
    w, h = max(1, int(w)), max(1, int(h))
    tile = Image.new("RGB", (w, h), PANEL)
    d = ImageDraw.Draw(tile)
    words = [x for x in str(title or "").replace("-", " ").split() if x[:1].isalnum()]
    ini = "".join(x[0] for x in words[:2]).upper() or "?"
    d.text((w / 2, h / 2), ini, font=font(max(20, int(min(w, h) * 0.28)), True), fill=FAINT, anchor="mm")
    return tile


class Card:
    def __init__(self, title, subtitle="", width=W, height=H):
        self.w, self.h = width, height
        self.img = Image.new("RGB", (width, height), BG)
        self.d = ImageDraw.Draw(self.img)
        self.y = PAD
        self.d.text((PAD, self.y), self.fit(title, font(56, True), width - 2 * PAD), font=font(56, True), fill=TEXT)
        self.y += 76
        if subtitle:
            self.d.text((PAD, self.y), self.fit(subtitle, font(30), width - 2 * PAD), font=font(30), fill=TEXT2)
            self.y += 44
        self.y += 24

    # -- helpers
    def tw(self, s, f):
        return self.d.textlength(s, font=f)

    def fit(self, s, f, maxw):
        s = str(s)
        if self.tw(s, f) <= maxw:
            return s
        while s and self.tw(s + "…", f) > maxw:
            s = s[:-1]
        return s.rstrip() + "…"

    def room(self, h):
        """True if h more pixels fit above the bottom margin."""
        return self.y + h <= self.h - PAD

    def gap(self, h=24):
        self.y += h
        return self.y

    # -- elements
    def big(self, value, label="", color=TEXT):
        """A big number with a label to its right, baseline-aligned."""
        fv, fl = font(120, True), font(34)
        self.d.text((PAD, self.y), value, font=fv, fill=color)
        if label:
            x = PAD + self.tw(value, fv) + 28
            self.d.text((x, self.y + 120 - 46), self.fit(label, fl, self.w - PAD - x), font=fl, fill=TEXT2)
        self.y += 150
        return self.y

    def progress(self, pct, color=None, height=34):
        pct = max(0.0, min(100.0, float(pct)))
        x0, x1 = PAD, self.w - PAD
        r = height // 2
        self.d.rounded_rectangle((x0, self.y, x1, self.y + height), radius=r, fill=PANEL)
        fill_x = x0 + (x1 - x0) * pct / 100
        if fill_x - x0 >= height:
            self.d.rounded_rectangle((x0, self.y, fill_x, self.y + height), radius=r, fill=color or severity(pct))
        self.y += height + 28
        return self.y

    def stats(self, items):
        """Up to 4 tiles in a row: [(value, label) or (value, label, color)]."""
        n = max(1, len(items))
        gap = 20
        tw_ = (self.w - 2 * PAD - gap * (n - 1)) / n
        h = 150
        for i, it in enumerate(items):
            value, label = it[0], it[1]
            color = it[2] if len(it) > 2 else TEXT
            x = PAD + i * (tw_ + gap)
            self.d.rounded_rectangle((x, self.y, x + tw_, self.y + h), radius=22, fill=PANEL)
            self.d.text((x + 24, self.y + 18), self.fit(value, font(56, True), tw_ - 40), font=font(56, True), fill=color)
            self.d.text((x + 24, self.y + 98), self.fit(label, font(26), tw_ - 40), font=font(26), fill=TEXT2)
        self.y += h + 28
        return self.y

    def heading(self, s):
        self.d.text((PAD, self.y), self.fit(s, font(34, True), self.w - 2 * PAD), font=font(34, True), fill=TEXT)
        self.y += 52
        return self.y

    def lines(self, rows, size=30, color=TEXT2, max_lines=None, spacing=1.45):
        """Text rows (str or (str, color)); stops at the bottom margin."""
        f = font(size)
        lh = int(size * spacing)
        for i, row in enumerate(rows):
            if max_lines is not None and i >= max_lines:
                break
            if not self.room(lh):
                break
            s, c = (row, color) if isinstance(row, str) else row
            self.d.text((PAD, self.y), self.fit(s, f, self.w - 2 * PAD), font=f, fill=c)
            self.y += lh
        return self.y

    def _frame(self, height):
        x0, x1 = PAD, self.w - PAD
        y0, y1 = self.y, self.y + height
        self.d.rounded_rectangle((x0, y0, x1, y1), radius=26, fill=PANEL)
        return x0, y0, x1, y1

    def bar_chart(self, values, labels=(), height=380, colors=None, value_fmt=str, note=""):
        """Vertical bars in a panel; colors: one per bar (default ACCENT); labels under the bars."""
        x0, y0, x1, y1 = self._frame(height)
        top = y0 + (64 if note else 40)
        if note:
            self.d.text((x0 + 28, y0 + 18), self.fit(note, font(26), x1 - x0 - 56), font=font(26), fill=TEXT2)
        base = y1 - 64
        n = max(1, len(values))
        vmax = max([v for v in values if v is not None] + [1])
        slot = (x1 - x0 - 56) / n
        bw = min(84, slot * 0.62)
        fl, fv = font(24), font(28, True)
        self.d.line((x0 + 28, base, x1 - 28, base), fill=GRID, width=2)
        for i, v in enumerate(values):
            cx = x0 + 28 + slot * (i + 0.5)
            if v:
                bh = (base - top - 40) * v / vmax
                c = (colors[i] if colors and i < len(colors) else ACCENT)
                self.d.rounded_rectangle((cx - bw / 2, base - bh, cx + bw / 2, base), radius=min(12, bw / 3), fill=c)
                self.d.text((cx, base - bh - 8), value_fmt(v), font=fv, fill=TEXT, anchor="md")
            elif v is not None:
                self.d.text((cx, base - 8), value_fmt(v), font=fv, fill=TEXT2, anchor="md")
            if i < len(labels) and labels[i]:
                self.d.text((cx, base + 14), self.fit(labels[i], fl, slot), font=fl, fill=TEXT2, anchor="ma")
        self.y = y1 + 28
        return self.y

    def line_chart(self, points, height=440, limit=None, projection=None, x_labels=(), y_fmt=str, note="", y_min=None):
        """points: [(x, y)] in data units (x ascending); limit: (y, label) as a horizontal line;
        projection: [(x, y)] drawn dashed; x_labels: [(x, text)]."""
        x0, y0, x1, y1 = self._frame(height)
        if note:
            self.d.text((x0 + 28, y0 + 18), self.fit(note, font(26), x1 - x0 - 56), font=font(26), fill=TEXT2)
        px0, px1 = x0 + 120, x1 - 36
        py0, py1 = y0 + (78 if note else 48), y1 - 66
        allp = list(points) + list(projection or [])
        if not allp:
            self.y = y1 + 28
            return self.y
        xs = [p[0] for p in allp]
        ys = [p[1] for p in allp] + ([limit[0]] if limit else [])
        xmin, xmax = min(xs), max(xs)
        ymin = min(ys) if y_min is None else y_min
        ymax = max(ys)
        pad = (ymax - ymin) * 0.08 or 1
        ymin, ymax = ymin - (pad if y_min is None else 0), ymax + pad
        xr = (xmax - xmin) or 1

        def sx(x):
            return px0 + (px1 - px0) * (x - xmin) / xr

        def sy(y):
            return py1 - (py1 - py0) * (y - ymin) / (ymax - ymin)

        fs = font(24)
        for k in range(4):
            yv = ymin + (ymax - ymin) * k / 3
            yy = sy(yv)
            self.d.line((px0, yy, px1, yy), fill=GRID, width=1)
            self.d.text((px0 - 14, yy), y_fmt(yv), font=fs, fill=TEXT2, anchor="rm")
        if limit:
            ly = sy(limit[0])
            self.d.line((px0, ly, px1, ly), fill=BAD, width=3)
            if limit[1]:
                self.d.text((px0 + 10, ly - 8), limit[1], font=font(24, True), fill=BAD, anchor="ld")
        if len(points) > 1:
            self.d.line([(sx(x), sy(y)) for x, y in points], fill=ACCENT, width=6, joint="curve")
        if points:
            lx, ly_ = points[-1]
            self.d.ellipse((sx(lx) - 10, sy(ly_) - 10, sx(lx) + 10, sy(ly_) + 10), fill=ACCENT)
        if projection and len(projection) > 1:
            self._dashed([(sx(x), sy(y)) for x, y in projection], WARN, 5)
            ex, ey = projection[-1]
            self.d.ellipse((sx(ex) - 9, sy(ey) - 9, sx(ex) + 9, sy(ey) + 9), outline=WARN, width=4)
        for x, t in x_labels:
            if xmin <= x <= xmax:
                anchor = "la" if sx(x) - px0 < 60 else "ra" if px1 - sx(x) < 60 else "ma"
                self.d.text((sx(x), py1 + 16), t, font=fs, fill=TEXT2, anchor=anchor)
        self.y = y1 + 28
        return self.y

    def _dashed(self, pts, color, width, dash=18, gap_=12):
        for (ax, ay), (bx, by) in zip(pts, pts[1:]):
            dist = ((bx - ax) ** 2 + (by - ay) ** 2) ** 0.5
            if not dist:
                continue
            t = 0.0
            while t < dist:
                e = min(t + dash, dist)
                self.d.line((ax + (bx - ax) * t / dist, ay + (by - ay) * t / dist,
                             ax + (bx - ax) * e / dist, ay + (by - ay) * e / dist), fill=color, width=width)
                t += dash + gap_

    def paste(self, path, max_height=None):
        """Another PNG scaled to the content width, in a rounded frame; skipped (False) when unreadable."""
        try:
            src = Image.open(path).convert("RGB")
        except Exception:
            return False
        inner = self.w - 2 * PAD
        h = min(round(src.height * inner / src.width), max_height or 10 ** 6, self.h - PAD - self.y)
        if h < 80:
            return False
        # scale to fit (never crop), centred on the image's own background colour
        scale = min(inner / src.width, h / src.height)
        sw, sh = max(1, round(src.width * scale)), max(1, round(src.height * scale))
        frame = Image.new("RGB", (inner, h), src.getpixel((0, 0)))
        frame.paste(src.resize((sw, sh), Image.LANCZOS), ((inner - sw) // 2, (h - sh) // 2))
        mask = Image.new("L", (inner, h), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, inner - 1, h - 1), radius=26, fill=255)
        self.img.paste(frame, (PAD, self.y), mask)
        self.y += h + 28
        return True

    def wrap(self, s, f, maxw, max_lines=2):
        """Word-wrapped lines of s within maxw; the last allowed line is cut with "…"."""
        words, out, cur = str(s).split(), [], ""
        for i, wd in enumerate(words):
            t = f"{cur} {wd}".strip()
            if self.tw(t, f) <= maxw or not cur:
                cur = t
                continue
            out.append(cur)
            cur = wd
            if len(out) == max_lines - 1:
                cur = " ".join(words[i:])
                break
        if cur:
            out.append(cur)
        out = out[:max_lines]
        return [self.fit(x, f, maxw) for x in out] or [""]

    def star(self, x, y, size, fill=STAR):
        """A five-pointed star in the box (x, y, size): Noto Sans has no ★ glyph."""
        import math
        cx, cy, r = x + size / 2, y + size / 2 + size * 0.04, size / 2
        pts = []
        for i in range(10):
            a = math.pi / 2 + i * math.pi / 5
            rr = r if i % 2 == 0 else r * 0.45
            pts.append((cx + rr * math.cos(a), cy - rr * math.sin(a)))
        self.d.polygon(pts, fill=fill)

    def pill(self, x, y, s, size=26, fill=(0, 0, 0), color=TEXT, star=False, right=False):
        """A rounded label (optionally with a star first) at (x, y); right=True: x is its right edge. Returns its width."""
        f = font(size, True)
        sw = size * 0.9 if star else 0
        w = int(self.tw(s, f) + sw + (8 if star else 0) + size * 0.9)
        h = int(size * 1.55)
        x0 = x - w if right else x
        self.d.rounded_rectangle((x0, y, x0 + w, y + h), radius=h // 2, fill=fill)
        tx = x0 + size * 0.45
        if star:
            self.star(tx, y + (h - sw) / 2, sw)
            tx += sw + 8
        self.d.text((tx, y + h / 2), s, font=f, fill=color, anchor="lm")
        return w

    def _tile(self, it, x, y, w, h, badge_size=26):
        """One poster (or placeholder) with rounded corners, a badge top-left and the rating top-right."""
        img = poster_image(it["image"], w, h) if it.get("image") else None
        img = img or placeholder_image(w, h, it.get("title"))
        mask = Image.new("L", (int(w), int(h)), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, int(w) - 1, int(h) - 1), radius=18, fill=255)
        self.img.paste(img, (int(x), int(y)), mask)
        dark = (12, 14, 18)
        if it.get("badge"):
            self.pill(x + 12, y + 12, str(it["badge"]), size=badge_size, fill=ACCENT, color=(255, 255, 255))
        if it.get("rating"):
            self.pill(x + w - 12, y + 12, f"{float(it['rating']):.1f}", size=badge_size, fill=dark, star=True, right=True)

    def _bar(self, x, y, w, pct, color=GOOD, height=12):
        pct = max(0.0, min(100.0, float(pct)))
        self.d.rounded_rectangle((x, y, x + w, y + height), radius=height // 2, fill=GRID)
        if pct > 0:
            self.d.rounded_rectangle((x, y, max(x + height, x + w * pct / 100), y + height), radius=height // 2, fill=color)

    def poster_grid(self, items, cols=None, gap=24, reserve=0):
        """1-6 poster tiles in a grid that fills the rest of the card (2 columns for 2 or 4, else up to 3); under each:
        the title (2 lines), its lines (one line each) and a progress bar; reserve = px kept free at the bottom (a footer).
        Returns the y it ended at."""
        items = list(items)[:6]
        if not items:
            return self.y
        n = len(items)
        cols = cols or (n if n <= 3 and n != 2 else 2 if n in (2, 4) else 3)
        rows = -(-n // cols)
        cw = (self.w - 2 * PAD - (cols - 1) * gap) / cols
        ts, ls = (34, 27) if cols <= 2 else (29, 23)
        ft, fl = font(ts, True), font(ls)
        t_lines = max(len(self.wrap(it.get("title", ""), ft, cw)) for it in items)
        n_lines = max(len(it.get("lines") or []) for it in items)
        has_bar = any(it.get("progress") is not None for it in items)
        text_h = 14 + t_lines * int(ts * 1.25) + n_lines * int(ls * 1.4) + (24 if has_bar else 0) + 6
        room = self.h - PAD - reserve - self.y - (rows - 1) * gap
        ph = min(cw * 1.5, room / rows - text_h)
        if ph < 120:
            raise ValueError("no room for the posters")
        for k, it in enumerate(items):
            r, c = divmod(k, cols)
            x = PAD + c * (cw + gap)
            y = self.y + r * (ph + text_h + gap)
            self._tile(it, x, y, cw, ph, badge_size=26 if cols <= 2 else 23)
            ty = y + ph + 14
            for ln in self.wrap(it.get("title", ""), ft, cw):
                self.d.text((x, ty), ln, font=ft, fill=TEXT)
                ty += int(ts * 1.25)
            for row in it.get("lines") or []:
                sx, col = (row, TEXT2) if isinstance(row, str) else row
                self.d.text((x, ty), self.fit(sx, fl, cw), font=fl, fill=col)
                ty += int(ls * 1.4)
            if it.get("progress") is not None:
                self._bar(x, ty + 8, cw, it["progress"], it.get("accent") or GOOD)
        self.y += rows * (ph + text_h) + (rows - 1) * gap + 20
        return self.y

    def poster_rows(self, items, poster_w=210, gap=28):
        """One row per item: the poster left, the title (2 lines), its lines and a progress bar right; rows that
        don't fit are left out. Returns the y it ended at."""
        ph = int(poster_w * 1.5)
        ft, fl = font(36, True), font(28)
        x = PAD + poster_w + 32
        tw_ = self.w - PAD - x
        for it in items:
            t = self.wrap(it.get("title", ""), ft, tw_)
            lines = [(ln, col) for row in it.get("lines") or []
                     for sx, col in [(row, TEXT2) if isinstance(row, str) else row] for ln in self.wrap(sx, fl, tw_)]
            text_h = len(t) * 46 + len(lines) * 40 + (30 if it.get("progress") is not None else 0)
            h = max(ph, text_h)
            if not self.room(h):
                break
            self._tile(it, PAD, self.y, poster_w, ph, badge_size=24)
            ty = self.y + 4
            for ln in t:
                self.d.text((x, ty), ln, font=ft, fill=TEXT)
                ty += 46
            for ln, col in lines:
                self.d.text((x, ty), ln, font=fl, fill=col)
                ty += 40
            if it.get("progress") is not None:
                self._bar(x, ty + 10, tw_, it["progress"], it.get("accent") or ACCENT, height=14)
            self.y += h + gap
        return self.y

    # -- drawn glyphs and text with them (Noto Sans has neither → nor ★)
    def arrow(self, x, y, w, color=TEXT2, width=3):
        """A right arrow from x to x + w on the middle line y; returns w."""
        head = max(7, int(w * 0.38))
        self.d.line((x, y, x + w - head + 2, y), fill=color, width=width)
        self.d.polygon([(x + w - head, y - head * 0.62), (x + w, y), (x + w - head, y + head * 0.62)], fill=color)
        return w

    def _rich_parts(self, s):
        parts = [p for p in re.split(r"(\s*→\s*|★)", str(s)) if p]
        return [("→" if p.strip() == "→" else p) for p in parts]

    def rich_w(self, s, f):
        """Width of rich(s) at font f."""
        size, w = f.size, 0
        for p in self._rich_parts(s):
            w += size * 1.55 if p == "→" else size * 0.95 if p == "★" else self.tw(p, f)
        return w

    def rich(self, x, y, s, f, fill=TEXT, maxw=None, glyph=None):
        """s at (x, y) (y = the middle line) with → and ★ drawn; cut with "…" at maxw. Returns the width drawn."""
        size, cx = f.size, x
        limit = x + maxw if maxw else None
        for p in self._rich_parts(s):
            if p in ("→", "★"):
                need = size * 1.55 if p == "→" else size * 0.95
                if limit is not None and cx + need > limit:
                    if limit - cx >= self.tw("…", f):
                        self.d.text((cx, y), "…", font=f, fill=fill, anchor="lm")
                        cx += self.tw("…", f)
                    break
                if p == "→":
                    self.arrow(cx + size * 0.3, y, size * 0.95, glyph or fill, width=max(2, size // 12))
                else:
                    self.star(cx + size * 0.02, y - size * 0.42, size * 0.8, fill=glyph or STAR)
                cx += need
                continue
            t = p if limit is None else self.fit(p, f, max(0, limit - cx))
            if t and t != "…":
                self.d.text((cx, y), t, font=f, fill=fill, anchor="lm")
                cx += self.tw(t, f)
            if t != p:
                break
        return cx - x

    # -- trips
    def board(self, rows, footer_room=40):
        """A departure board, one panel per ride, top-down while they fit; returns how many were drawn.
        rows: dicts {time "HH:MM", day str, today bool, dest str, origin str, lines [str], arr "HH:MM"|"",
        platform str|"", platform_new bool (drawn in WARN), status str, color (the status colour: text + edge),
        note str|"" (a line under the ride, with → drawn; in note_color, default color)}."""
        x0, x1 = PAD, self.w - PAD
        f_time, f_day, f_dest, f_sub = font(50, True), font(24), font(36, True), font(25)
        f_line, f_stat, f_pl, f_note = font(23, True), font(30, True), font(32, True), font(26)
        mx, rw = x0 + 214, 230                      # middle column start, right column width
        hy = self.y
        for s_, x_, anchor in (("Departs", x0 + 30, "la"), ("To", mx, "la"), ("Status · platform", x1 - 30, "ra")):
            self.d.text((x_, hy), s_.upper(), font=font(20, True), fill=TEXT2, anchor=anchor)
        self.y += 38
        drawn = 0
        mw = x1 - 30 - rw - mx
        for r in rows:
            note = r.get("note") or ""
            lines = (r.get("lines") or [])[:4]
            pw = sum(self.tw(ln, f_line) + 26 for ln in lines) + 36 * max(0, len(lines) - 1)
            sub = " · ".join(b for b in (f"arr {r['arr']}" if r.get("arr") else "",
                                         f"from {r['origin']}" if r.get("origin") else "") if b)
            own = bool(sub and lines and pw + 14 + self.rich_w(sub, f_sub) > mw)   # from/arr on a line of their own
            h = 148 + (40 if own else 0) + (44 if note else 0)
            if self.y + h > self.h - PAD - footer_room:
                break
            y, col = self.y, r.get("color") or TEXT2
            self.d.rounded_rectangle((x0, y, x1, y + h), radius=22, fill=PANEL)
            self.d.rounded_rectangle((x0, y, x0 + 12, y + h), radius=6, fill=col)
            self.d.text((x0 + 32, y + 18), r.get("time") or "?", font=f_time, fill=TEXT)
            self.d.text((x0 + 34, y + 88), self.fit(r.get("day") or "", f_day, mx - x0 - 50), font=f_day,
                        fill=ACCENT if r.get("today") else TEXT2)
            self.rich(mx, y + 42, r.get("dest") or "?", f_dest, TEXT, maxw=mw)
            px, ly = mx, y + 98                      # line pills joined by drawn arrows, then arr/from
            for i, ln in enumerate(lines):
                if i:
                    px += self.arrow(px + 6, ly, 18, TEXT2, width=2) + 12
                w_ = int(self.tw(ln, f_line) + 26)
                if px + w_ > mx + mw:
                    break
                self.d.rounded_rectangle((px, ly - 19, px + w_, ly + 19), radius=10, fill=(52, 60, 74))
                self.d.text((px + 13, ly), ln, font=f_line, fill=TEXT, anchor="lm")
                px += w_
            if own:
                px, ly = mx, ly + 40
            if sub:
                px += 14 if px > mx else 0
                self.rich(px, ly, sub, f_sub, TEXT2, maxw=mx + mw - px)
            self.d.text((x1 - 30, y + 40), self.fit(r.get("status") or "", f_stat, rw), font=f_stat, fill=col, anchor="rm")
            if r.get("platform"):                    # a platform sign: the number in a box (amber when it changed)
                pl = self.fit(str(r["platform"]), f_pl, rw - 30)
                bw = max(56, int(self.tw(pl, f_pl) + 28))
                new_ = r.get("platform_new")
                self.d.rounded_rectangle((x1 - 30 - bw, y + 76, x1 - 30, y + 124), radius=10,
                                         fill=(82, 58, 20) if new_ else (52, 60, 74))
                self.d.text((x1 - 30 - bw / 2, y + 100), pl, font=f_pl, fill=WARN if new_ else TEXT, anchor="mm")
            if note:
                self.rich(mx, y + 160 + (40 if own else 0), note, f_note, r.get("note_color") or col, maxw=x1 - 30 - mx)
            self.y = y + h + 16
            drawn += 1
        self.y -= 16 if drawn else 0
        return drawn

    def route(self, label, stops, hops=(), color=ACCENT):
        """One journey as a line of stops in a panel: label (head, sub) on the left, stops [(code, under)] spread
        over the rest, hops [str] (e.g. flight numbers) over the segments between them; returns y."""
        x0, x1, y = PAD, self.w - PAD, self.y
        h = 124
        self.d.rounded_rectangle((x0, y, x1, y + h), radius=22, fill=PANEL)
        head, sub = (label if isinstance(label, (tuple, list)) else (label, ""))[:2]
        self.d.text((x0 + 28, y + 22), self.fit(head, font(32, True), 200), font=font(32, True), fill=TEXT)
        if sub:
            self.d.text((x0 + 28, y + 70), self.fit(sub, font(24), 200), font=font(24), fill=TEXT2)
        a, b = x0 + 300, x1 - 80
        n = len(stops)
        xs = [a + (b - a) * i / (n - 1) for i in range(n)] if n > 1 else [(a + b) / 2]
        ly = y + 68
        if n > 1:
            self.d.line((xs[0], ly, xs[-1], ly), fill=color, width=4)
            for i in range(n - 1):
                mid = (xs[i] + xs[i + 1]) / 2
                self.arrow(mid - 12, ly, 24, color, width=4)
                if i < len(hops) and hops[i]:
                    self.d.text((mid, ly - 14), self.fit(hops[i], font(20, True), xs[i + 1] - xs[i] - 90),
                                font=font(20, True), fill=TEXT2, anchor="mb")
        for (code, under), x in zip(stops, xs):
            self.d.ellipse((x - 9, ly - 9, x + 9, ly + 9), fill=color, outline=PANEL, width=3)
            self.d.text((x, y + 30), code, font=font(30, True), fill=TEXT, anchor="mm")
            if under:
                self.d.text((x, y + 100), self.fit(under, font(22), 190), font=font(22), fill=TEXT2, anchor="mm")
        self.y = y + h + 16
        return self.y

    def trip_strip(self, legs, start, end, today=None, height=64):
        """A timeline from start to end (dates): one coloured segment per leg ({name, from, to, color}; a leg runs
        until the next one starts), grey travel ends before the first and after the last leg, a white cut on each
        hop day, date labels under the cuts (dropped where they would overlap) and a today marker. Returns y."""
        legs = sorted([l for l in legs if l.get("from") and l.get("to")], key=lambda l: l["from"])
        total = max(1, (end - start).days)
        x0, x1 = PAD, self.w - PAD
        top = self.y + (40 if today and start <= today <= end else 0)

        def xof(d):
            return x0 + (x1 - x0) * min(total, max(0, (d - start).days)) / total
        self.d.rounded_rectangle((x0, top, x1, top + height), radius=height // 2, fill=GRID)
        segs = []
        for i, l in enumerate(legs):
            until = legs[i + 1]["from"] if i + 1 < len(legs) else l["to"]
            segs.append((xof(l["from"]), xof(until), l))
        pal = [(38, 166, 154), (102, 187, 106), (88, 150, 240), (230, 160, 70), (186, 104, 200)]
        for i, (a, b, l) in enumerate(segs):
            col = l.get("color") or pal[i % len(pal)]
            l["_color"] = col
            r = height // 2 if (i == 0 and a <= x0 + 1) or (i == len(segs) - 1 and b >= x1 - 1) else 0
            self.d.rounded_rectangle((a, top, max(a + 1, b), top + height), radius=r, fill=col)
            f = font(28, True)
            name = self.fit(l.get("name") or "", f, b - a - 20)
            if name and name != "…":
                self.d.text(((a + b) / 2, top + height / 2), name, font=f, fill=BG, anchor="mm")
        cuts = list(dict.fromkeys([l["from"] for l in legs] + ([legs[-1]["to"]] if legs else []) + [start, end]))
        for i, (a, b, l) in enumerate(segs):
            if i:
                self.d.line((a, top - 4, a, top + height + 4), fill=TEXT, width=4)
        if legs:
            self.d.line((segs[-1][1], top, segs[-1][1], top + height), fill=BG, width=3)
            if segs[0][0] > x0 + 1:
                self.d.line((segs[0][0], top, segs[0][0], top + height), fill=BG, width=3)
        f, taken = font(22), []
        for d in cuts:                          # place legs first; a label that would touch a drawn one is dropped
            x, s = xof(d), f"{d.day}.{d.month}"
            w = self.tw(s, f)
            lx = min(max(x - w / 2, x0), x1 - w)
            if any(lx < b + 12 and lx + w + 12 > a for a, b in taken):
                continue
            self.d.text((lx, top + height + 10), s, font=f, fill=TEXT2)
            taken.append((lx, lx + w))
        if today and start <= today <= end:
            x = xof(today) + (x1 - x0) / total / 2 if today < end else xof(today)
            self.d.polygon([(x - 12, top - 16), (x + 12, top - 16), (x, top - 2)], fill=TEXT)
            self.d.text((x, top - 22), "today", font=font(22, True), fill=TEXT, anchor="mb")
        self.y = top + height + 46
        return self.y

    def tiles(self, items, gap=20, max_h=None):
        """Equal columns in a row, each a panel with a colour bar on top: items [{title, star (bool: a drawn star
        after the title), sub [str], pill (text, fill, colour) or None, lines [str | (str, colour)]}]; returns y."""
        n = max(1, len(items))
        x0, x1 = PAD, self.w - PAD
        cw = (x1 - x0 - gap * (n - 1)) / n
        ft, fs, fl, fp = font(34, True), font(23), font(24), font(22, True)
        rows = max([len(it.get("sub") or []) + len(it.get("lines") or []) + (1.4 if it.get("pill") else 0)
                    for it in items] or [0])
        h = int(84 + rows * 36)
        if max_h:
            h = min(h, max_h)
        y = self.y
        for i, it in enumerate(items):
            x = x0 + i * (cw + gap)
            self.d.rounded_rectangle((x, y, x + cw, y + h), radius=20, fill=PANEL)
            self.d.rounded_rectangle((x, y, x + cw, y + 10), radius=5, fill=it.get("color") or ACCENT)
            tw_ = self.rich(x + 20, y + 44, it.get("title") or "", ft, TEXT, maxw=cw - 40 - (34 if it.get("star") else 0))
            if it.get("star"):
                self.star(x + 20 + tw_ + 8, y + 30, 28)
            ty = y + 76
            for s_ in it.get("sub") or []:
                if ty + 30 > y + h:
                    break
                self.d.text((x + 20, ty + 15), self.fit(s_, fs, cw - 40), font=fs, fill=TEXT2, anchor="lm")
                ty += 34
            if it.get("pill") and ty + 44 <= y + h:
                s_, fill, col = it["pill"]
                self.pill(x + 18, ty + 4, self.fit(s_, fp, cw - 60), size=22, fill=fill, color=col)
                ty += 50
            for ln in it.get("lines") or []:
                if ty + 30 > y + h:
                    break
                s_, col = (ln, TEXT) if isinstance(ln, str) else ln
                self.rich(x + 20, ty + 16, s_, fl, col, maxw=cw - 40)
                ty += 36
        self.y = y + h + 20
        return self.y

    def trim(self, min_height=0, footer=False):
        """Cut the card below what was drawn (+ the bottom margin, + room for a footer), at least min_height tall."""
        h = max(min_height, min(self.h, int(self.y) + PAD + (36 if footer else 0)))
        if h < self.h:
            self.img = self.img.crop((0, 0, self.w, h))
            self.d = ImageDraw.Draw(self.img)
            self.h = h
        return self.h

    def footer(self, s):
        f = font(24)
        self.d.text((PAD, self.h - PAD + 8), self.fit(s, f, self.w - 2 * PAD), font=f, fill=TEXT2, anchor="ld")

    def save(self, name, directory=None):
        """Write <dir>/<name>.png atomically (0600; dir 0700); returns the path."""
        d = directory or out_dir()
        os.makedirs(d, mode=0o700, exist_ok=True)
        path = os.path.join(d, f"{name}.png")
        fd, tmp = tempfile.mkstemp(prefix=f".{name}.", suffix=".png", dir=d)
        try:
            with os.fdopen(fd, "wb") as fh:
                self.img.save(fh, "PNG", optimize=True)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return path
