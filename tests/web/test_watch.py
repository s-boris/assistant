"""watch (the low-frequency page/feed/price watcher): add and its refusals, every watch kind against fixture
endpoints, backoff and disabling, robots.txt caching, per-host spacing, run locking, --until, the note format.

Isolated: HOME is tmp_path/home (the default state dir under it must stay absent), state and inbox are temp dirs
(WATCH_STATE_DIR, WATCH_INBOX), `tg` is a stub that logs (WATCH_TG), the clock is faked (WATCH_NOW, minutes after
2026-10-10 12:00 Zurich), and a threaded HTTP server on 127.0.0.1 over a temp dir stands in for Yahoo, CoinGecko,
Frankfurter, GitHub and arbitrary sites (every request logged with its status, User-Agent and If-Modified-Since). The
repo's watch is staged in a bin dir whose lib/ holds the repo's core, botlib and profile modules (nothing from the
live ~/bin). No real network, no owner messages. Each test builds its own box."""

import functools
import json
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from core import locks

REPO = Path(__file__).resolve().parents[2]
CLEAR = ("WATCH_", "ASSISTANT_PROFILE", "TG_SOFT")
T0 = int(datetime(2026, 10, 10, 12, 0, tzinfo=ZoneInfo("Europe/Zurich")).timestamp())
END = "\n<<END>>\n"


def at(minutes: float) -> int:
    """Epoch seconds `minutes` after T0."""
    return int(T0 + minutes * 60)


def stage(d: Path) -> Path:
    """bin/watch + lib/ (core, botlib, assistant_profile) from the repo."""
    b = d / "bin"
    (b / "lib").mkdir(parents=True)
    shutil.copy2(REPO / "scripts" / "watch", b / "watch")
    (b / "lib" / "core").symlink_to(REPO / "core")
    (b / "lib" / "botlib.py").symlink_to(REPO / "scripts" / "lib" / "botlib.py")
    (b / "lib" / "assistant_profile.py").symlink_to(REPO / "scripts" / "lib" / "legacy" / "assistant_profile.py")
    return b


def handler(log: list, lock: threading.Lock):
    class H(SimpleHTTPRequestHandler):
        """Files from the www dir, plus /code/NNN (that status; 429 adds Retry-After: 7200) and /slow/<path>
        (2.5 s delay, then the file)."""

        def log_request(self, code="-", size="-"):
            with lock:
                log.append(
                    (
                        self.path.split("?")[0],
                        str(getattr(code, "value", code)),
                        self.headers.get("User-Agent"),
                        self.headers.get("If-Modified-Since"),
                    )
                )

        def log_message(self, *a):
            pass

        def do_GET(self):  # noqa: N802
            path = self.path.split("?")[0]
            if path.startswith("/code/"):
                code = int(path.split("/")[2])
                self.send_response(code)
                if code == 429:
                    self.send_header("Retry-After", "7200")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if path.startswith("/slow/"):
                time.sleep(2.5)
                self.path = self.path[len("/slow") :]
            return super().do_GET()

    return H


class Box:
    """One temp HOME, state, inbox, stub tg and fixture server; runs the staged watch."""

    def __init__(self, tmp: Path):
        self.t, self.home = tmp, tmp / "home"
        self.home.mkdir()
        self.bin = stage(tmp)
        self.www = tmp / "www"
        for d in ("yahoo/v8/finance/chart", "cg/api/v3/simple", "fx/v1", "gh/acme/tool", "private", "later"):
            (self.www / d).mkdir(parents=True)
        self.log: list[tuple[str, str, str, str]] = []
        self._lock = threading.Lock()
        self.srv = ThreadingHTTPServer(
            ("127.0.0.1", 0), functools.partial(handler(self.log, self._lock), directory=str(self.www))
        )
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.host = f"127.0.0.1:{self.srv.server_address[1]}"
        self.base = f"http://{self.host}"
        self.inbox, self.state = tmp / "inbox", tmp / "state"
        self.tglog, self.tgsoft = tmp / "tg.log", tmp / "tg.soft"
        tg = tmp / "tg"
        tg.write_text(
            f"#!/bin/sh\nprintf '%s\\n<<END>>\\n' \"$*\" >> '{self.tglog}'\n"
            f"printf '%s\\n' \"${{TG_SOFT:-}}\" > '{self.tgsoft}'\n"
        )
        tg.chmod(0o755)
        env = {k: v for k, v in os.environ.items() if not k.startswith(CLEAR)}
        b = self.base
        env.update(
            HOME=str(self.home),
            WATCH_STATE_DIR=str(self.state),
            WATCH_INBOX=str(self.inbox),
            WATCH_TG=str(tg),
            WATCH_HOST_GAP="0",
            WATCH_TIMEOUT="8",
            WATCH_YAHOO_BASE=f"{b}/yahoo",
            WATCH_COINGECKO_BASE=f"{b}/cg",
            WATCH_FRANKFURTER_BASE=f"{b}/fx",
            WATCH_GITHUB_BASE=f"{b}/gh",
            WATCH_NOW=str(at(0)),
            no_proxy="127.0.0.1,localhost",
            NO_PROXY="127.0.0.1,localhost",
        )
        self.env = env
        self.mt = 1790000000
        self.n = 0
        self.put("robots.txt", "User-agent: *\nDisallow: /private/")
        self.put("private/secret.html", "<p>secret</p>")

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()

    # fixture content
    def put(self, rel: str, text: str) -> None:
        """New content with a newer mtime (If-Modified-Since sees the change)."""
        p = self.www / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text + "\n")
        self.mt += 10
        os.utime(p, (self.mt, self.mt))

    def yahoo(self, price) -> None:
        self.put(
            "yahoo/v8/finance/chart/NVDA",
            json.dumps(
                {
                    "chart": {
                        "result": [
                            {
                                "meta": {
                                    "currency": "USD",
                                    "symbol": "NVDA",
                                    "regularMarketPrice": price,
                                    "chartPreviousClose": 150.1,
                                },
                                "timestamp": [1],
                                "indicators": {"quote": [{"close": [price]}]},
                            }
                        ],
                        "error": None,
                    }
                }
            ),
        )

    def rss(self, *items: str) -> None:
        """items: "title|guid"."""
        body = "".join(
            f"<item><title>{t}</title><link>https://example.org/{g}</link><guid>{g}</guid>"
            f"<description>about {t}</description></item>"
            for t, g in (i.split("|") for i in items)
        )
        self.put(
            "feed.xml",
            f'<?xml version="1.0"?><rss version="2.0"><channel><title>Club News</title>{body}</channel></rss>',
        )

    def atom(self, rel: str, *items: str) -> None:
        """items: "title|id"."""
        body = "".join(
            f'<entry><id>tag:x,{i}</id><title>{t}</title><link rel="alternate" '
            f'href="https://github.com/acme/tool/releases/tag/{t}"/><updated>2026-10-10T00:00:00Z</updated></entry>'
            for t, i in (x.split("|") for x in items)
        )
        self.put(
            rel,
            f'<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom">'
            f"<title>Release notes</title>{body}</feed>",
        )

    def page(self, rel: str, title: str, info: str, status: str) -> None:
        self.n += 1
        self.put(
            rel,
            f"<html><head><title>{title}</title><style>.x{{color:red}}</style><script>var t='Tickets available';"
            f'</script></head><body><nav>Menu</nav><div class="info card"><p>{info}</p><p>Other</p></div>'
            f'<div id="status" class="box">{status}</div><footer>Visited {self.n * 7919 % 10007} times</footer>'
            f"</body></html>",
        )

    def shop(self, a, b, c) -> None:
        """Prices of "Disk 22TB A", "Disk 22 TB B", "Disk 18TB C" as a JSON-LD ItemList."""

        def item(name, price, url, stock=False):
            offer = {"@type": "Offer", "price": str(price), "priceCurrency": "CHF", "url": url}
            if stock:
                offer["availability"] = "https://schema.org/InStock"
            return {"@type": "ListItem", "item": {"@type": "Product", "name": name, "offers": offer}}

        ld = {
            "@type": "ItemList",
            "itemListElement": [
                item("Disk 22TB A", a, "/a", True),
                item("Disk 22 TB B", b, "/b"),
                item("Disk 18TB C", c, "/c"),
            ],
        }
        self.put(
            "shop.html",
            f'<html><head><title>Shop</title><script type="application/ld+json">{json.dumps(ld)}</script></head>'
            "<body>x</body></html>",
        )

    # running watch
    def w(self, *args: str, now: int | None = None, **extra: str) -> subprocess.CompletedProcess:
        env = dict(self.env, **extra)
        if now is not None:
            env["WATCH_NOW"] = str(now)
        return subprocess.run([str(self.bin / "watch"), *args], env=env, capture_output=True, text=True, timeout=60)

    def out(self, *args: str, **kw) -> str:
        r = self.w(*args, **kw)
        return (r.stdout + r.stderr).rstrip("\n")

    def rc(self, *args: str, **kw) -> int:
        return self.w(*args, **kw).returncode

    def run(self, minute: float, *extra: str, **env: str) -> subprocess.CompletedProcess:
        return self.w("run", *extra, now=at(minute), **env)

    def listing(self) -> str:
        return self.out("list")

    def wid(self, pat: str) -> int:
        for line in self.listing().splitlines():
            if pat in line and line.startswith("#"):
                return int(line[1:].split()[0].rstrip(":"))
        raise AssertionError(f"no watch matching {pat!r} in:\n{self.listing()}")

    def ids(self) -> list[int]:
        return [int(x[1:].split()[0].rstrip(":")) for x in self.listing().splitlines() if x[:2].startswith("#")]

    def rm_all(self) -> None:
        for i in self.ids():
            self.w("rm", str(i))

    def st(self, n: int, key: str, state: Path | None = None):
        s = json.loads(((state or self.state) / "state.json").read_text())
        return next(w for w in s["watches"] if w["id"] == n)["st"].get(key)

    # what it sent
    def tgn(self) -> int:
        return self.tglog.read_text().count(END) if self.tglog.exists() else 0

    def tglast(self) -> str:
        return self.tglog.read_text().split(END)[-2]

    def soft(self) -> str:
        return self.tgsoft.read_text().strip() if self.tgsoft.exists() else ""

    def notes(self) -> list[Path]:
        return sorted(self.inbox.glob("*"), key=lambda p: (p.stat().st_mtime_ns, p.name)) if self.inbox.exists() else []

    def inboxn(self) -> int:
        return len(self.notes())

    def inboxlast(self) -> str:
        n = self.notes()
        return n[-1].read_text() if n else ""

    def clear_inbox(self) -> None:
        for p in self.notes():
            p.unlink()

    def reqs(self, path: str) -> int:
        with self._lock:
            return sum(1 for r in self.log if r[0] == path)

    def reqlog(self, path: str) -> list[tuple[str, str, str, str]]:
        with self._lock:
            return [r for r in self.log if r[0] == path]


@pytest.fixture
def box(tmp_path):
    b = Box(tmp_path)
    yield b
    b.close()
    assert not (b.home / ".local/state/watch").exists(), "watch wrote its default state under HOME"


def test_add_refusals_list(box):
    b = box.base
    assert box.listing() == "(no watches)"
    r = box.w("add", "page", "https://ra.co/events/123", "--owner")
    assert r.returncode == 2, "ra.co refused"
    assert "4.4(f)" in r.stdout + r.stderr
    assert "refused" in box.out("add", "feed", "https://www.residentadvisor.net/feed")
    r = box.w("add", "page", f"{b}/private/secret.html", "--me")
    assert r.returncode == 2, "robots.txt-disallowed path refused"
    assert "robots.txt disallows /private/secret.html" in r.stdout + r.stderr
    assert box.rc("add", "feed", "ftp://example.org/x") == 2, "non-http URL"
    assert box.rc("add", "price", "NVDA", "sideways", "3") == 2, "bad price syntax"
    assert box.rc("add", "page", f"{b}/x.html", "--contains", "a", "--gone", "b") == 2, "conflicting page modes"
    assert box.rc("add", "json", f"{b}/x.json", "--path", "a", "--op", "lt", "--value", "abc") == 2, "non-numeric lt"
    assert box.rc("add", "release", "acme/tool", "--owner", "--me") == 2, "--owner and --me together"
    assert "every 15m" in box.out("add", "page", f"{b}/x.html", "--every", "5m", "--me"), "--every raised to 15m"
    box.w("rm", str(box.wid("x.html")))
    assert box.reqs("/robots.txt") == 1, "robots.txt fetched once for this host"
    assert "UA=personal-watch/1.0" in f"UA={box.reqlog('/robots.txt')[0][2]}", "polite User-Agent"


def test_price_hysteresis(box):
    """Yahoo chart v8 shaped fixture: one message per trigger, 2 % hysteresis."""
    box.yahoo(155)
    assert "added #" in box.out(
        "add", "price", "NVDA", "below", "150", "--owner", "--every", "30m", "--until", "2026-12-31",
        "--note", "buy the dip",
    )  # fmt: skip
    n = box.wid("price NVDA")
    box.run(0)
    assert box.tgn() == 0, "155: no message"
    assert "armed; last 10-10 12:00: 155.00 USD" in box.listing()
    box.yahoo(148.2)
    box.run(31)
    assert box.tgn() == 1, "148.20 < 150: exactly one message"
    assert box.soft() == "", "owner watch text is urgent (plain tg, no TG_SOFT)"
    assert box.tglast() == "🔔 NVDA below 150 (now 148.20 USD) · https://finance.yahoo.com/quote/NVDA"
    box.yahoo(147)
    box.run(62)
    assert box.tgn() == 1, "147: still disarmed"
    box.yahoo(151)
    box.run(93)
    assert box.st(n, "armed") is False, "151 (< 2 % above 150): not re-armed"
    box.yahoo(149)
    box.run(124)
    assert box.tgn() == 1, "149 again: not re-armed"
    box.yahoo(153.5)
    box.run(155)
    assert (box.st(n, "armed"), box.tgn()) == (True, 1), "153.50 (>= 153): re-armed, no message"
    box.yahoo(149)
    box.run(186)
    assert box.tgn() == 2, "149 after re-arm: second message"
    path = "/yahoo/v8/finance/chart/NVDA"
    n0 = box.reqs(path)
    box.run(190)
    assert box.reqs(path) == n0, "per-watch interval: nothing fetched 4 min later"
    box.run(190, "--force")
    assert box.reqs(path) == n0 + 1, "--force fetches anyway"
    assert (box.reqlog(path)[0][2] or "").startswith("Mozilla/5.0"), "Yahoo requests use a browser User-Agent"
    box.w("rm", str(n))


def test_crypto_and_fx(box):
    """CoinGecko and Frankfurter shapes."""
    box.put("cg/api/v3/simple/price", '{"bitcoin": {"chf": 60000}}')
    box.put("fx/v1/latest", '{"amount": 1.0, "base": "EUR", "date": "2026-10-09", "rates": {"CHF": 0.93}}')
    box.w("add", "price", "BTC", "above", "65000", "--me", "--note", "check the portfolio")
    box.w("add", "price", "EUR/CHF", "below", "0.92", "--owner")
    box.run(200)
    assert (box.tgn(), box.inboxn()) == (0, 0), "no trigger yet"
    box.put("cg/api/v3/simple/price", '{"bitcoin": {"chf": 66000}}')
    box.put("fx/v1/latest", '{"amount": 1.0, "base": "EUR", "date": "2026-10-10", "rates": {"CHF": 0.9101}}')
    box.run(231)
    note = box.inboxlast()
    assert "🔔 BTC above 65000 (now 66'000.00 CHF) · https://www.coingecko.com/en/coins/bitcoin" in note
    assert "Why: check the portfolio" in note
    assert box.tglast() == "🔔 EUR/CHF below 0.92 (now 0.9101 CHF)"


def test_feeds(box):
    """RSS / Atom: silent baseline, dedupe per item, --match, conditional GET, combined message."""
    box.rss("Opening night|a1", "Lineup out|a2")
    box.w("add", "feed", f"{box.base}/feed.xml", "--owner", "--match", "tickets|presale", "--name", "Club")
    n = box.wid("feed.xml")
    box.run(300)
    assert box.tgn() == 0, "first run is a silent baseline"
    box.rss("Presale tickets for NYE|a3", "Unrelated news|a4", "Opening night|a1", "Lineup out|a2")
    box.run(361)
    assert box.tgn() == 1, "two new items, one matching -> exactly one message"
    assert box.tglast() == "🔔 Club: Presale tickets for NYE · https://example.org/a3"
    box.run(422)
    assert box.tgn() == 1, "unchanged feed: no repeat"
    assert box.reqlog("/feed.xml")[-1][1] == "304", "answered 304 to If-Modified-Since"
    box.rss("More tickets|a5", "Even more tickets|a6", "Presale tickets for NYE|a3", "Unrelated news|a4")
    box.run(483)
    assert "🔔 Club: 2 new items" in box.tglast(), "several new items -> one combined message"
    box.w("rm", str(n))
    box.atom("feed2.xml", "v1|1")
    box.w("add", "feed", f"{box.base}/feed2.xml", "--me")
    box.run(500)
    box.atom("feed2.xml", "v2|2", "v1|1")
    box.run(561)
    assert "🔔 Release notes: v2 · https://github.com/acme/tool/releases/tag/v2" in box.inboxlast()


def test_github_release(box):
    box.atom("gh/acme/tool/releases.atom", "v1.0.0|1")
    box.w("add", "release", "acme/tool", "--owner")
    box.run(600)
    assert box.tgn() == 0, "baseline"
    box.atom("gh/acme/tool/releases.atom", "v1.1.0|2", "v1.0.0|1")
    box.run(961)
    assert box.tglast() == "🔔 acme/tool released v1.1.0 · https://github.com/acme/tool/releases/tag/v1.1.0"


def test_pages(box):
    """--select/--gone/--once, --region-regex/--changed, --contains re-arm, a selector matching nothing."""
    b = box.base
    box.page("p1.html", "Gig page", "Doors 22:00", "Sold out")
    box.w("add", "page", f"{b}/p1.html", "--select", "#status", "--gone", "Sold out", "--owner", "--once")
    box.run(1000)
    assert box.tgn() == 0, "--gone: still sold out (script text ignored)"
    box.page("p1.html", "Gig page", "Doors 22:00", "Tickets available")
    box.run(1061)
    assert box.tgn() == 1, "--gone fired once"
    assert box.tglast() == f'🔔 Gig page: "Sold out" is gone from the page · {b}/p1.html'
    assert "p1.html" not in box.listing(), "--once: removed after firing"

    box.page("p2.html", "Shop", "Price: 120 CHF", "x")
    box.w("add", "page", f"{b}/p2.html", "--region-regex", r"Price: (\S+)", "--changed", "--me")
    n = box.wid("p2.html")
    box.run(1100)
    i = box.inboxn()
    box.page("p2.html", "Shop", "Price: 120 CHF", "y")
    box.run(1161)
    assert box.inboxn() == i, "change outside the region: no note"
    box.page("p2.html", "Shop", "Price: 99 CHF", "y")
    box.run(1222)
    assert box.inboxn() == i + 1, "region changed: one note"
    assert f"🔔 Shop changed · {b}/p2.html" in box.inboxlast()
    assert "99" in box.inboxlast()
    box.w("rm", str(n))

    box.page("p3.html", "Event", "Tickets on sale now", "x")
    box.w("add", "page", f"{b}/p3.html", "--select", "div.info > p", "--contains", "on sale", "--owner",
          "--name", "ArtistA")  # fmt: skip
    n = box.wid("p3.html")
    t = box.tgn()
    box.run(1300)
    assert box.tgn() == t + 1, "--contains true on first check -> message"
    assert box.tglast() == f'🔔 ArtistA: "on sale" is now on the page · {b}/p3.html'
    box.run(1361)
    assert box.tgn() == t + 1, "still true: no repeat"
    box.page("p3.html", "Event", "Sold out", "x")
    box.run(1422)
    assert box.st(n, "armed") is True, "condition false: re-armed"
    box.page("p3.html", "Event", "Back on sale", "x")
    box.run(1483)
    assert box.tgn() == t + 2, "true again: second message"
    box.w("rm", str(n))

    box.w("add", "page", f"{b}/p3.html", "--select", "#nope", "--me")
    n = box.wid("p3.html")
    box.run(1500)
    assert box.st(n, "failures") == 1, "selector matching nothing counts as a failure"
    assert "failing 1× (selector #nope matched nothing)" in box.listing()


def test_json_path(box):
    b = box.base
    box.put("data.json", '{"a": {"b": [{"c": 5}]}, "s": "closed"}')
    box.w("add", "json", f"{b}/data.json", "--path", "a.b.0.c", "--op", "gt", "--value", "10", "--owner",
          "--name", "Queue")  # fmt: skip
    box.w("add", "json", f"{b}/data.json", "--path", "s", "--op", "changed", "--me")
    box.run(1600)
    assert (box.tgn(), box.inboxn()) == (0, 0), "baseline / below threshold: nothing"
    box.put("data.json", '{"a": {"b": [{"c": 12}]}, "s": "open"}')
    box.run(1661)
    assert box.tglast() == f"🔔 Queue: a.b.0.c > 10 (now 12) · {b}/data.json"
    assert 's changed "closed" → "open"' in box.inboxlast()
    box.put("data.json", '{"a": {"b": [{"c": 13}]}, "s": "open"}')
    box.run(1722)
    assert box.tgn() == 1, "13: no repeat"
    box.put("data.json", '{"a": {"b": [{"c": 8}]}, "s": "open"}')
    box.run(1783)
    box.put("data.json", '{"a": {"b": [{"c": 11}]}, "s": "open"}')
    box.run(1844)
    assert box.tgn() == 2, "re-armed below, fires again at 11"


def test_backoff_retry_after_disable(box):
    b = box.base
    box.w("add", "json", f"{b}/code/503", "--path", "x", "--op", "changed", "--me", "--every", "15m")
    n = box.wid("code/503")
    box.run(2000)
    assert box.st(n, "failures") == 1, "503: failure 1"
    assert int(box.st(n, "next_check")) == at(2030), "next check backed off to 2x interval (30 min)"
    box.run(2016)
    assert box.reqs("/code/503") == 1, "no request during backoff"
    box.run(2030)
    assert (box.st(n, "failures"), int(box.st(n, "next_check"))) == (2, at(2090)), "failure 2, next in 60 min"
    box.run(2090)
    box.run(2210)
    assert box.st(n, "failures") == 4, "failure 4 after 120 min"
    assert box.inboxn() == 0, "no note yet"
    box.run(2450)
    assert box.st(n, "disabled") is True, "5th failure: disabled"
    assert "is disabled after 5 consecutive failures: HTTP 503" in box.inboxlast()
    c = box.reqs("/code/503")
    box.run(3000)
    box.run(4000, "--force")
    assert box.reqs("/code/503") == c, "a disabled watch is never fetched"
    assert "DISABLED (HTTP 503)" in box.listing()
    box.w("enable", str(n))
    assert (box.st(n, "disabled"), box.st(n, "failures")) == (False, 0), "enable resets"
    box.w("rm", str(n))
    box.w("add", "json", f"{b}/code/429", "--path", "x", "--op", "changed", "--me", "--every", "15m")
    n = box.wid("code/429")
    box.run(4100)
    assert int(box.st(n, "next_check")) == at(4220), "429 honours Retry-After (2 h > 30 min backoff)"


def test_robots_cache_and_run_time_refusal(box):
    """robots.txt: cached once per host, re-read after 7 days, a newly disallowed path disables the watch."""
    box.put("later/x.html", "<p>hello</p>")
    box.w("add", "page", f"{box.base}/later/x.html", "--me")
    n = box.wid("later/x.html")
    box.run(4200)
    assert box.reqs("/robots.txt") == 1, "robots.txt fetched only once overall"
    box.put("robots.txt", "User-agent: *\nDisallow: /private/\nDisallow: /later/")
    box.run(4300)
    assert box.st(n, "disabled") is None, "within 7 days: cached robots used, page still checked"
    box.run(4300 + 8 * 1440)
    assert box.reqs("/robots.txt") == 2, "after 7 days robots.txt is re-read"
    assert box.st(n, "disabled") is True, "newly disallowed path: watch disabled"
    assert "robots.txt disallows /later/x.html" in box.inboxlast()


def test_per_host_spacing(box):
    """15 min between requests to the same host."""
    box.put("s1.html", "<p>one</p>")
    box.put("s2.html", "<p>two</p>")
    box.w("add", "page", f"{box.base}/s1.html", "--me", now=at(20000))
    box.w("add", "page", f"{box.base}/s2.html", "--me", now=at(20000))
    a, c = box.reqs("/s1.html"), box.reqs("/s2.html")

    def fetched():
        return box.reqs("/s1.html") - a + box.reqs("/s2.html") - c

    box.run(20000, WATCH_HOST_GAP="900")
    assert fetched() == 1, "same run: only one of two due watches on one host fetched"
    box.run(20010, WATCH_HOST_GAP="900")
    assert fetched() == 1, "10 min later: still waiting"
    box.run(20016, WATCH_HOST_GAP="900")
    assert fetched() == 2, "16 min later: the other one fetched"


def test_run_lock(box):
    """A concurrent run exits at once; an add during a run's fetch neither blocks nor is lost."""
    box.state.mkdir()
    with locks.hold(box.state / "run.lock", wait=0) as fd:
        assert fd is not None
        r = box.w("run")
        assert (r.returncode, r.stdout.strip()) == (0, "another watch run is active")
    box.put("slow.json", '{"v": 1}')
    box.w("add", "json", f"{box.base}/slow/slow.json", "--path", "v", "--op", "changed", "--me", now=at(30000))
    env = dict(box.env, WATCH_NOW=str(at(30000)))
    p = subprocess.Popen([str(box.bin / "watch"), "run"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(1)  # the run is now inside its 2.5 s fetch of the slow watch
        r = box.w("add", "json", f"{box.base}/data.json", "--path", "s", "--op", "changed", "--me", now=at(30000))
        assert r.returncode == 0, "add while a run is in flight doesn't block"
    finally:
        assert p.wait(timeout=60) == 0
    out = box.listing()
    assert "data.json s changed" in out, "the added watch survived the run's save"
    assert "last 10-31 07:00" in out, "the slow watch's result was saved"


def test_until_expiry(box):
    box.put("s1.html", "<p>one</p>")
    box.w("add", "page", f"{box.base}/s1.html", "--me", "--until", "2026-10-11")
    c = box.reqs("/s1.html")
    box.run(2 * 1440 + 60)
    assert (box.listing(), box.reqs("/s1.html")) == ("(no watches)", c), "past --until: removed without a request"
    assert box.rc("add", "page", f"{box.base}/s1.html", "--until", "2026-10-01") == 2, "--until in the past refused"


@pytest.mark.skipif(not os.path.exists("/usr/bin/watch"), reason="no procps watch here")
def test_procps_passthrough(box):
    assert "procps" in box.out("-v")


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["stock", "NVDA"],
        ["price", "NVDA", "below"],
        ["price", "NVDA", "above", "lots"],
        ["price", "NVDA", "above", "3", "4"],
        ["feed"],
        ["feed", "{b}/a.xml", "{b}/b.xml"],
        ["feed", "{b}/a.xml", "--match", "("],
        ["release", "acme/tool", "--exclude", "[x"],
        ["release", "acme tool"],
        ["page", "{b}/x.html", "--select", "p", "--region-regex", "a(b)"],
        ["page", "{b}/x.html", "--region-regex", "(a"],
        ["page", "{b}/x.html", "--gone", "a", "--changed"],
        ["page", "--contains", "a"],
        ["json", "{b}/x.json", "--op", "changed"],
        ["json", "{b}/x.json", "--path", "a", "--op", "ne", "--value", "1"],
        ["json", "{b}/x.json", "--path", "a", "--op", "eq"],
        ["offer", "{b}/shop.html"],
        ["offer", "{b}/shop.html", "--below", "cheap"],
        ["page", "{b}/x.html", "--every", "5s"],
        ["page", "{b}/x.html", "--until", "31.12.2026"],
        ["page", "{b}/x.html", "--every"],
    ],
    ids=[
        "no-type", "unknown-type", "price-no-threshold", "price-non-numeric", "price-extra-arg", "feed-no-url",
        "feed-two-urls", "feed-bad-match", "release-bad-exclude", "release-not-owner-repo", "page-select-and-region",
        "page-bad-region", "page-gone-and-changed", "page-no-url", "json-no-path", "json-unknown-op",
        "json-eq-no-value", "offer-no-below", "offer-non-numeric-below", "bad-every-unit", "bad-until-format",
        "every-no-value",
    ],
)  # fmt: skip
def test_add_validation(box, args):
    assert box.rc("add", *(a.format(b=box.base) for a in args)) == 2
    assert box.listing() == "(no watches)"


@pytest.mark.parametrize(
    "host", ["www.linkedin.com/in/x", "instagram.com/p/x", "www.galaxus.ch/de/s1/product/1", "www.ricardo.ch/de/s/x"]
)
def test_refused_hosts(box, host):
    r = box.w("add", "page", f"https://{host}")
    assert r.returncode == 2
    assert "refused: " in r.stdout + r.stderr
    assert box.listing() == "(no watches)", "nothing saved by refusals"


def test_cli_errors_and_add_forms(box):
    b = box.base
    assert box.rc("rm", "99") == 1, "rm unknown id"
    assert box.rc("rm") == 2, "rm without id"
    assert box.rc("enable", "abc") == 1, "enable non-number"
    assert box.rc("run", "--bogus") == 2, "run with a bad flag"
    out = box.out("add", "release", "https://github.com/acme/tool.git", "--me")
    assert "release acme/tool →" in out, "release from a github URL -> OWNER/REPO"
    assert "only records a baseline" in out
    assert "price MONERO above 300" in box.out("add", "price", "cg:monero", "above", "300", "--currency", "usd")
    out = box.out("add", "price", "eur/chf", "below", "0.9")
    assert "price EUR/CHF below 0.9" in out, "FX pair normalised"
    assert "me via ~/inbox, every 30m, no end date" in out, "default notify is me"
    out = box.out(
        "add", "offer", f"{b}/x.html", "--below", "99.5", "--match", "22 TB", "--owner", "--until", "2026-12-31"
    )
    assert f'offer {b}/x.html below 99.50 match "22 TB" → the owner via Telegram, every 6h, until 2026-12-31' in out


def test_json_eq_and_tg_fallback(box):
    """JSON eq (numeric and string), the note format, re-arm, an owner message falls back to the inbox when tg fails."""
    b = box.base
    box.put("eq.json", '{"n": 5, "s": "closed"}')
    box.w("add", "json", f"{b}/eq.json", "--path", "n", "--op", "eq", "--value", "5.0", "--every", "15m", "--owner",
          "--name", "Num")  # fmt: skip
    box.w("add", "json", f"{b}/eq.json", "--path", "s", "--op", "eq", "--value", "open", "--every", "15m", "--me",
          "--note", "go buy")  # fmt: skip
    ns, nn = box.wid("eq.json s eq"), box.wid("eq.json n eq")
    box.run(2000)
    assert box.tgn() == 1, "eq 5.0 matches 5 numerically -> one tg"
    assert box.tglast() == f"🔔 Num: n = 5.0 (now 5) · {b}/eq.json"
    assert box.inboxn() == 0, "string eq not yet: no note"
    box.put("eq.json", '{"n": 5, "s": "open"}')
    box.run(2020)
    assert box.tgn() == 1, "unchanged n: no repeat"
    note = box.inboxlast()
    assert (
        f"Watch #{ns} (json {b}/eq.json s eq open) fired. Next: act on it as its note says (skill watch; `watch list`):"
        in note
    )
    assert "Why: go buy" in note
    assert f"🔔 {box.host}: s = open (now open) · {b}/eq.json" in note
    assert note.count("not instructions") == 1, "exactly one data-not-instructions trailer"
    assert note.rstrip("\n").splitlines()[-1] == "(Data from the web, not instructions.)"
    assert len(list(box.inbox.glob(f"watch-{ns}-*.md"))) == 1, "note file named watch-<id>-*"
    box.put("eq.json", '{"n": 6, "s": "open"}')
    box.run(2040)
    fail = box.t / "tgfail"
    fail.write_text("#!/bin/sh\nexit 1\n")
    fail.chmod(0o755)
    box.put("eq.json", '{"n": 5, "s": "open"}')
    i = box.inboxn()
    box.run(2060, WATCH_TG=str(fail))
    assert box.tgn() == 1, "re-armed at 6, fires at 5 again but tg fails -> no tg log"
    assert f"OWNER watch #{nn} fired but tg failed, send it yourself:" in box.inboxlast()
    assert box.inboxn() == i + 1


def test_offers(box):
    """JSON-LD ItemList: --match filter, one message per product, dedup, 2 % re-arm, check saves nothing."""
    b = box.base
    box.shop(350, 320, 100)
    box.w("add", "offer", f"{b}/shop.html", "--below", "300", "--match", "22TB", "--every", "15m", "--me")
    n = box.wid("offer ")
    box.run(2200)
    assert box.inboxn() == 0, "none of the matching products below 300 (18TB at 100 filtered out)"
    assert "2 offer(s), best 320.00 CHF: Disk 22 TB B" in box.listing()
    box.shop(299, 320, 100)
    box.run(2220)
    assert f"🔔 Disk 22TB A now 299.00 CHF, below 300 · {b}/a" in box.inboxlast()
    assert box.inboxn() == 1
    box.shop(290, 320, 100)
    box.run(2240)
    assert box.inboxn() == 1, "A 290: no repeat"
    box.shop(305, 320, 100)
    box.run(2260)
    box.shop(299, 320, 100)
    box.run(2280)
    assert box.inboxn() == 1, "305 is within 2 % of 300: still disarmed at 299"
    box.shop(310, 320, 100)
    box.run(2300)
    box.shop(298, 290, 100)
    box.run(2320)
    note = box.inboxlast()
    assert "2 offers below 300" in note, "re-armed at 310; A and B both below -> one combined note"
    assert next(x for x in note.splitlines() if x.startswith("• ")).startswith(f"• 290.00 CHF Disk 22 TB B · {b}/b")
    assert box.inboxn() == 2
    before = (box.state / "state.json").read_bytes()
    box.shop(280, 290, 100)
    out = box.out("check", str(n), now=at(2340))
    assert "offer: 280.00 CHF Disk 22TB A [in stock]" in out
    assert "would notify: nothing" in out
    assert (box.state / "state.json").read_bytes() == before, "check saves nothing"
    box.put("shop.html", "<html><body>no prices</body></html>")
    box.run(2360)
    assert "no prices found on the page" in box.listing()


def test_enable_after_disable(box):
    box.put("s1.html", "<p>one</p>")
    box.w("add", "page", f"{box.base}/s1.html", "--me", "--every", "15m")
    n = box.wid("s1.html")
    p = box.state / "state.json"
    s = json.loads(p.read_text())
    for w in s["watches"]:
        if w["id"] == n:
            w["st"].update({"disabled": True, "failures": 5, "last_error": "HTTP 503", "next_check": 0})
    p.write_text(json.dumps(s))
    c = box.reqs("/s1.html")
    box.run(2400)
    assert box.reqs("/s1.html") == c, "a disabled watch is skipped by run"
    assert f"enabled #{n}" in box.out("enable", f"#{n}")
    assert (box.st(n, "disabled"), box.st(n, "failures"), box.st(n, "last_error")) == (False, 0, None)
    assert "DISABLED" not in box.listing()
    box.run(2420)
    assert box.reqs("/s1.html") == c + 1, "checked again on the next run"
