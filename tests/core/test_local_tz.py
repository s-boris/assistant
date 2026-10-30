"""core.watchjobs.local_tz: the zone a hub schedule gets when the caller has no TZ (cron's jobs, the tool worker)."""

import os

from core import watchjobs


def _files(tmp_path, link_to, timezone_text):
    zi = tmp_path / "usr" / "share" / "zoneinfo" / link_to
    zi.parent.mkdir(parents=True, exist_ok=True)
    zi.write_bytes(b"TZif")
    lt = tmp_path / "localtime"
    os.symlink(zi, lt)
    tzf = tmp_path / "timezone"
    tzf.write_text(timezone_text)
    return str(lt), str(tzf)


def test_tz_env_wins(monkeypatch, tmp_path):
    lt, tzf = _files(tmp_path, "Europe/Zurich", "Etc/UTC\n")
    monkeypatch.setenv("TZ", ":America/New_York")
    assert watchjobs.local_tz(lt, tzf) == "America/New_York"


def test_localtime_link_beats_stale_etc_timezone(monkeypatch, tmp_path):
    # the container: /etc/timezone still says the image's Etc/UTC, libc (and cron) use the Europe/Zurich link
    lt, tzf = _files(tmp_path, "Europe/Zurich", "Etc/UTC\n")
    monkeypatch.delenv("TZ", raising=False)
    assert watchjobs.local_tz(lt, tzf) == "Europe/Zurich"


def test_etc_timezone_when_localtime_is_no_zoneinfo_link(monkeypatch, tmp_path):
    lt = tmp_path / "localtime"
    lt.write_bytes(b"TZif")  # a copied file, not a link
    tzf = tmp_path / "timezone"
    tzf.write_text("Europe/Zurich\n")
    monkeypatch.delenv("TZ", raising=False)
    assert watchjobs.local_tz(str(lt), str(tzf)) == "Europe/Zurich"


def test_utc_when_nothing_says(monkeypatch, tmp_path):
    monkeypatch.delenv("TZ", raising=False)
    assert watchjobs.local_tz(str(tmp_path / "none"), str(tmp_path / "none2")) == "UTC"
