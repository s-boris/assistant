"""strike-watch when the Italian Ministry of Transport strike list gets no connection: scioperi.mit.gov.it accepts
Italian IP addresses only, so from elsewhere every fetch times out at connect. That is reported as "unreachable"
(no connection, why, what covers Italy meanwhile), not as a page change; check and status say Italy rests on the
news queries (or is not covered without one); the failed note says so and is rewritten when the kind of failure
changes (unreachable <-> HTTP or parser error), once.

Fixtures (fixtures/): a Google News RSS answer for an Italian rail-strike query recorded live (4 items kept,
descriptions emptied) and the MIT table page the shell suite uses. scripts/strike-watch is loaded from the repo;
HTTP goes through a stub of core.http's open, so no network."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import json
import urllib.error
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "strike-watch"
FIX = Path(__file__).with_name("fixtures")
NOW = "2026-11-01T07:40:00+01:00"
CONFIG = {
    "margin_days": 1,
    "trips": [
        {
            "id": "t1",
            "label": "Test trip",
            "legs": [
                {"country": "IT", "from": "2026-11-08", "to": "2026-11-10", "sectors": ["rail", "air", "transit"]}
            ],
        }
    ],
    "places": {"IT": {"regions": ["Piemonte"], "provinces": ["Torino"], "rail": ["TORINO"]}},
    "news": {"IT": [{"q": "it rail", "hl": "it", "gl": "IT"}]},
    "news_hints": {"IT": {"places": ["itali*", "torino"], "words": ["trenitalia", "italo"]}},
}


@pytest.fixture
def sw(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(CONFIG), encoding="utf-8")
    for k, v in {
        "STRIKE_WATCH_CONFIG": str(cfg),
        "STRIKE_WATCH_DIR": str(tmp_path / "state"),
        "STRIKE_WATCH_INBOX": str(tmp_path / "inbox"),
        "STRIKE_WATCH_NOW": NOW,
        "STRIKE_WATCH_PAUSE": "0",
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("STRIKE_WATCH_BASE", raising=False)
    loader = importlib.machinery.SourceFileLoader(f"strike_watch_{tmp_path.name}", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    monkeypatch.setattr(mod.locks, "acquire", lambda *a, **k: 99)  # one process runs cron several times
    mod.tmp = tmp_path
    return mod


class Net:
    """core.http.open stand-in: the MIT host answers per `mit` (timeout, an HTTP status or a fixture), news with
    the recorded RSS; anything else fails the test."""

    def __init__(self, sw, monkeypatch, mit="timeout"):
        self.mit = mit
        self.urls: list[str] = []
        monkeypatch.setattr(sw.web, "open", self.open)

    def open(self, url, headers=None, timeout=None):
        self.urls.append(url)
        assert headers and "User-Agent" in headers and timeout == 25
        if "scioperi.mit.gov.it" in url:
            if self.mit == "timeout":  # what a connect from outside Italy gets
                raise urllib.error.URLError(TimeoutError("timed out"))
            if isinstance(self.mit, int):
                raise urllib.error.HTTPError(url, self.mit, "Error", {}, io.BytesIO(b"error"))  # type: ignore[arg-type]
            return io.BytesIO((FIX / self.mit).read_bytes())
        if url.startswith("https://news.google.com/rss/search"):
            return io.BytesIO((FIX / "news-it-treni.xml").read_bytes())
        raise AssertionError(f"unexpected fetch {url}")


def run(sw, capsys, *args):
    rc = sw.main(list(args))
    return rc, capsys.readouterr().out


def failed_note(sw):
    p = sw.tmp / "inbox" / "strike-watch-failed-mit.md"
    return p.read_text(encoding="utf-8") if p.exists() else ""


def state(sw):
    return json.loads((sw.tmp / "state" / "state.json").read_text(encoding="utf-8"))


def test_connect_timeout_is_unreachable(sw, monkeypatch):
    Net(sw, monkeypatch)
    with pytest.raises(sw.Unreachable, match=r"unreachable \(timed out\)"):
        sw.fetch(sw.MIT_URL)


def test_http_error_is_a_plain_failure(sw, monkeypatch):
    Net(sw, monkeypatch, mit=503)
    with pytest.raises(sw.Fail) as e:
        sw.fetch(sw.MIT_URL)
    assert not isinstance(e.value, sw.Unreachable) and str(e.value) == "HTTP 503"


def test_check_names_the_geo_fence_and_the_news_fallback(sw, monkeypatch, capsys):
    Net(sw, monkeypatch)
    rc, out = run(sw, capsys, "check", "--json")
    got = json.loads(out)
    mit = got["sources"]["mit"]
    assert rc == 0 and not mit["ok"] and mit["unreachable"]
    assert "unreachable (timed out)" in mit["error"] and "Italian IP addresses only" in mit["error"]
    assert got["coverage"] == {"IT": "official list down (mit): news headlines only, unconfirmed"}
    # the recorded national rail strike still comes through the news
    italo = [r for r in got["relevant"] if "Italo" in r["title"]]
    assert italo and all(r["start"] == "2026-11-09" and r["hit"] == ["rail"] and not r["official"] for r in italo)
    rc, out = run(sw, capsys, "check")
    assert "IT: official list down (mit): news headlines only, unconfirmed" in out


def test_without_a_news_query_italy_is_not_covered(sw, monkeypatch, capsys):
    cfg = dict(CONFIG, news={})
    Path(sw.CONFIG).write_text(json.dumps(cfg), encoding="utf-8")
    Net(sw, monkeypatch)
    rc, out = run(sw, capsys, "check", "--json")
    assert rc == 1  # the only source failed
    assert "NOT COVERED" in json.loads(out)["coverage"]["IT"]


def test_cron_notes_unreachable_once_then_again_on_a_new_kind(sw, monkeypatch, capsys):
    net = Net(sw, monkeypatch)
    for _ in range(3):
        rc, _out = run(sw, capsys, "check", "--cron")
        assert rc == 0
    note = failed_note(sw)
    assert note.startswith("# strike-watch: source mit unreachable")
    assert "Not a parser problem" in note and "Italian IP addresses only" in note
    assert "Italy meanwhile: official list down (mit): news headlines only" in note
    assert "Fix the parser" not in note
    s = state(sw)["sources"]["mit"]
    assert s["fails"] == 3 and s["unreachable"] and s["noted_kind"] == "unreachable"
    (sw.tmp / "inbox" / "strike-watch-failed-mit.md").unlink()
    run(sw, capsys, "check", "--cron")
    assert failed_note(sw) == ""  # same kind: not repeated
    net.mit = 500
    run(sw, capsys, "check", "--cron")
    note = failed_note(sw)
    assert "HTTP 500" in note and "Fix the parser" in note and "Not a parser problem" not in note
    net.mit = "mit.html"
    run(sw, capsys, "check", "--cron")
    s = state(sw)["sources"]["mit"]
    assert s["fails"] == 0 and not s["unreachable"] and not s["noted"] and s["noted_kind"] == ""


def test_a_note_from_before_the_kinds_is_rewritten_once(sw, monkeypatch, capsys):
    st = sw.tmp / "state"
    st.mkdir()
    (st / "state.json").write_text(
        json.dumps({"sources": {"mit": {"fails": 8, "noted": True, "error": "unreachable (timed out)"}}}),
        encoding="utf-8",
    )
    Net(sw, monkeypatch)
    run(sw, capsys, "check", "--cron")
    assert "Not a parser problem" in failed_note(sw)
    (sw.tmp / "inbox" / "strike-watch-failed-mit.md").unlink()
    run(sw, capsys, "check", "--cron")
    assert failed_note(sw) == ""


def test_status_says_unreachable_and_what_covers_italy(sw, monkeypatch, capsys):
    Net(sw, monkeypatch)
    run(sw, capsys, "check", "--cron")
    rc, out = run(sw, capsys, "status")
    assert rc == 0
    assert "source mit: unreachable x1: unreachable (timed out); no connection from here" in out
    assert "IT: official list down (mit): news headlines only, unconfirmed" in out
