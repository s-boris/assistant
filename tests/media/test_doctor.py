"""`media doctor` against fake_doctor.py (a stateful fake Seerr/Radarr/Sonarr/Emby, restarted for a fresh state).
Checks every verdict, the dry run (GET-only), the exact fixes of a real run (and nothing else: no library deletes),
throttling on a second run, title mode (+ forward lines, the requested match wins), --grab, --json, --cron (weekly:
fixes + an inbox note only from 3 stuck on him), the season pack already on disk, no key leaks."""

from __future__ import annotations

import json
import shutil
from datetime import datetime

import pytest
from mediatest import KEYS

EMBY_KEY = "EMBY-KEY-Tt55"


class Dr:
    def __init__(self, w):
        self.w, self.t = w, w.t
        w.env.update(
            MEDIA_SECRETS=str(self.t / "cfg.json"),
            MEDIA_NOW="2026-10-20T12:00:00+02:00",
            MEDIA_PAGE_SIZE="2",
            MEDIA_TIMEOUT="5",
            MEDIA_READY_USER="Owner",
            MEDIA_DOCTOR_STATE=str(self.t / "state" / "doctor.json"),
        )
        self.start()

    def start(self, **env):
        """A fresh fake (its state starts over), an empty request log."""
        self.w.stop()
        self.w.log_path.write_text("")
        b = self.w.serve("fake_doctor.py", env=env)
        (self.t / "cfg.json").write_text(
            json.dumps(
                {
                    "seerr": {"url": f"{b}/seerr", "key": KEYS[0]},
                    "radarr": {"url": f"{b}/radarr", "key": KEYS[1]},
                    "sonarr": {"url": f"{b}/sonarr", "key": KEYS[2]},
                    "emby": {"url": f"{b}/embysvc", "key": EMBY_KEY},
                }
            )
        )

    def run(self, *a, **env):
        return self.w.m("doctor", *a, env=env, timeout=60)

    def writes(self):
        return [x for x in self.w.log().splitlines() if not x.startswith("GET ")]

    def fresh_state(self):
        shutil.rmtree(self.t / "state", ignore_errors=True)


@pytest.fixture
def dr(media):
    w = Dr(media)
    yield w
    media.safety(keys=(*KEYS, EMBY_KEY), get_only=False)


def test_all_dry_real_throttled(dr):
    m, t = dr.w, dr.t
    out = dr.run("--dry-run")
    assert m.rc == 0
    for part in (
        "🩺 Request doctor · 13 open requests · dry run · 3 need you",
        "🎬 Mismatch Movie (2022) · Ana · 7 w\n   🔤 name mismatch: 5 releases, named 2021/2015, 2022 in the arr"
        ' · 👉 `media doctor "Mismatch Movie" --grab` grabs the best match',
        "⛔ import blocked: Manual import required · 👉 arr-unblock imports it",
        "⏸ waiting for approval in Seerr",
        "🔁 download failed (No files found are eligible for import) → (dry run) dropped + blocklisted it",
        "🔁 stalled torrent → (dry run) dropped",
        "📺 Failed Show S1 · Ana · 10 d\n   🔁 Seerr failed to hand it to Sonarr → (dry run) retried the request",
        "🔎 1 of 1 releases usable → (dry run) monitored it again; (dry run) searched again",
        "🔎 2 of 4 aired eps missing (S1: 2), 1 of 1 releases usable → (dry run) monitored 1 missing episode again;"
        " (dry run) searched the 2 missing episodes again",
        "🔍 last grab 10.09 failed, no release on the indexers (searched 20.10)",
        "Waiting normally:\n  ⬇️ Downloading Movie: 40% · ETA 1h30m (~13:30)",
        "📅 Cinema Movie: in cinemas from 19.11.2026",
        "📅 Airing Show S1: up to date (3 eps), next S01E04 22.10",
        "Already there or gone: 1 (Emby Twin ✅)",
        "Skipped: 1 old failed, 1 older than 180 d, 1 removed (--days 0 = all)",
    ):
        assert part in out, part
    assert "tmdb:101" not in out  # the 4K request ignored
    assert dr.writes() == []
    assert not (t / "state").exists() or not any((t / "state").iterdir())

    # real run: exactly the routine fixes
    out = dr.run()
    assert m.rc == 0
    assert "13 open requests · 7 fixed · 3 need you" in out
    assert "→ ✅ dropped + blocklisted it; the arr searches for another" in out
    w = dr.writes()
    assert len(w) == 7
    ws = "\n".join(w)
    for part in (
        "DELETE /radarr/api/v3/queue/501?removeFromClient=true&blocklist=true&skipRedownload=false",
        "DELETE /radarr/api/v3/queue/502?removeFromClient=true&blocklist=true&skipRedownload=false",
        "POST /seerr/api/v1/request/9/retry",
        'PUT /radarr/api/v3/movie/editor {"movieIds": [16], "monitored": true}',
        'POST /radarr/api/v3/command {"name": "MoviesSearch", "movieIds": [16]}',
        'PUT /sonarr/api/v3/episode/monitor {"episodeIds": [2204], "monitored": true}',
        'POST /sonarr/api/v3/command {"name": "EpisodeSearch", "episodeIds": [2203, 2204]}',
    ):
        assert part in ws, part
    for never in ("DELETE /radarr/api/v3/movie", "file", "/api/v3/release"):
        assert never not in ws
    assert "radarr DELETE /api/v3/queue/501 dropped + blocklisted it" in (t / "state" / "doctor.log").read_text()

    # second run: throttled, nothing sent again
    m.log_path.write_text("")
    out = dr.run()
    assert m.rc == 0 and dr.writes() == []
    assert "retried 20.10, still not there" in out
    assert "1 usable release(s) found (checked 20.10); the arr should grab one" in out
    assert m.logn("GET /radarr/api/v3/release?movieId=16") == 0


def test_title_mode_and_json(dr):
    m = dr.w
    out = dr.run("Mismatch Movie")
    assert m.rc == 0
    assert "🩺 🎬 Mismatch Movie (2022) · Ana · 7 w" in out  # the requested match wins
    assert '🇨🇭 "Mismatch Movie" hängt noch an einem Detail, ich schau\'s mir an. 🔧' in out
    assert '🇬🇧 "Mismatch Movie" is stuck on a detail; I\'m on it. 🔧' in out
    assert '🇨🇭 "Waiting Approval" ist angefragt und wartet noch auf Freigabe.' in dr.run("Waiting Approval")
    dr.run("Nothing Like This")
    assert m.rc == 1
    dr.run("--grab")
    assert m.rc == 1
    m.log_path.write_text("")
    out = dr.run("Mismatch Movie (2022)", "--grab")
    assert m.rc == 0
    assert "✅ grabbed Mismatch.Movie.2021.1080p.BluRay-GRP2" in out  # Bluray-1080p = the cutoff
    assert len(dr.writes()) == 1 and '"guid": "g-bluray", "indexerId": 5, "movieId": 11' in dr.writes()[0]

    dr.start()
    dr.fresh_state()
    out = dr.run("--json", "--dry-run", "--no-probe")
    d = json.loads(out)
    assert m.rc == 0
    assert (
        len(d["items"]),
        "/doctor TITLE" in d["text"],
        d["message"].startswith("🩺"),
        d["skipped"]["removed"],
        d["probes"],
    ) == (
        13,
        True,
        True,
        1,
        0,
    )
    assert m.logn("/release") == 0
    assert (d["items"][0]["code"], d["items"][0]["de"][:16]) == ("import_blocked", '"Blocked Import"')


def test_cron(dr):
    m, t = dr.w, dr.t
    inbox = t / "inbox"
    m.env["MEDIA_DOCTOR_INBOX"] = str(inbox)
    note = inbox / "media-doctor-2026-10-20.md"
    out = dr.run("--cron", "--dry-run")
    assert m.rc == 0
    assert "doctor --cron: 13 open requests, 0 fixed, 3 need you" in out
    assert f"(dry run) would write {note}" in out
    assert not inbox.exists() and dr.writes() == []
    out = dr.run("--cron")
    assert m.rc == 0
    assert out == f"doctor --cron: 13 open requests, 7 fixed, 3 need you\nnote: {note}"
    n = note.read_text()
    assert "# media-doctor 2026-10-20: 3 requests stuck on the owner" in n
    assert "- 🎬 Mismatch Movie (2022) · Ana · 7 w\n  🔤 name mismatch" in n
    assert "⏸ waiting for approval in Seerr" in n
    assert "Downloading Movie" not in n
    assert "Skipped (left alone on purpose): 1 old failed, 1 older than 180 d, 1 removed" in n
    ws = "\n".join(dr.writes())
    assert len(dr.writes()) == 7
    for never in (
        "DELETE /radarr/api/v3/movie",
        "file",
        "POST /radarr/api/v3/movie ",
        "POST /sonarr/api/v3/series",
        "/api/v3/release",
    ):
        assert never not in ws
    assert len(list(inbox.iterdir())) == 1
    dr.start()
    dr.fresh_state()
    note.unlink()
    dr.run("--cron", MEDIA_DOCTOR_NOTE_MIN=4)
    assert m.rc == 0 and list(inbox.iterdir()) == []  # below the threshold: silent, no note
    dr.run("--cron", "Mismatch Movie")
    assert m.rc == 1
    dr.run("--cron", "--json")
    assert m.rc == 1


def test_season_pack_on_disk(dr):
    m, t = dr.w, dr.t
    dr.start(FAKE_SAME_PACK="same")
    out = dr.run("--dry-run")
    assert (
        "🔍 2 of 4 aired eps missing (S1: 2), nothing new on the indexers: 2 releases, all the Q release already on"
        " disk (searched 20.10)" in out
    )
    assert "13 open requests · dry run · 3 need you" in out  # not counted as needing him
    assert "would replace files" not in out
    assert m.logn("GET /sonarr/api/v3/episodefile") >= 1
    assert dr.writes() == []
    for kind in ("bigger", "other"):  # a far bigger pack with the same name, another group's pack: still need him
        dr.start(FAKE_SAME_PACK=kind)
        out = dr.run("--dry-run")
        assert "2 releases, all packs that would replace files already on disk" in out, kind
        assert "13 open requests · dry run · 4 need you" in out, kind
    dr.start(FAKE_SAME_PACK="same")
    (t / "state").mkdir(exist_ok=True)
    ts = int(datetime.fromisoformat("2026-10-19T12:00:00+02:00").timestamp())
    (t / "state" / "doctor.json").write_text(
        json.dumps(
            {"probe": {"sonarr:22:1": {"kind": "existing", "n": 2, "count": 2, "reason": "Existing file", "t": ts}}}
        )
    )
    out = dr.run("--dry-run", "--no-probe")
    assert "2 releases, all packs that would replace files already on disk" in out  # old cached 'existing' kept
    out = dr.run()
    assert "nothing new on the indexers: 2 releases, all the Q release already on disk" in out  # probed again
    e = json.loads((t / "state" / "doctor.json").read_text())["probe"]["sonarr:22:1"]
    assert (e["kind"], e.get("group"), e.get("disk_checked")) == ("same_on_disk", "Q", True)
    assert "EpisodeSearch" not in "\n".join(dr.writes())
    out = dr.run("--dry-run", "--no-probe")
    assert "nothing new on the indexers: 2 releases, all the Q release already on disk (searched 20.10)" in out
