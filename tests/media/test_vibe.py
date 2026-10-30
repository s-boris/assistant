"""`media vibe` (4 library movies for a mood or "like <film>", local embedding index). fake_vibe.py stands in for
Emby (a fictional movie library, his played/partly watched ids) and the embedding server (/v1/embeddings: keyword-count
vectors, every embedded text logged); fake_tonight.py for Seerr (his user's linked Emby account, the link template).
MEDIA_VIBE_EMBED_URL points at the fake, so no llama-server runs. Checks: hint, first build, mood ranking, seen
penalty, rating floor, runtime caps, like <film> and a bare title, unknown film, diversity, incremental sync (new,
changed, removed), --refresh/--full/--quiet, JSON buttons and links, play on/off, Seerr/Emby/embedding failures, a
missing model, GET-only, only his own history read, no key leaks."""

from __future__ import annotations

import json

import pytest
from mediatest import KEYS, dead_port

EMBY_KEY = "EMBY-KEY-Vb42"
COZY = "Cozy Little Cafe | Rainy Window | Autumn Tea House | Long Rain"


@pytest.fixture
def vb(media):
    t = media.t
    b = media.serve("fake_tonight.py")
    (t / "scenario").write_text("ok\n")
    e = media.serve("fake_vibe.py", t / "emb.log", t / "scenario", log=t / "emby.log", err=t / "emby.err")
    media.e = e

    def cfg(name, seerr, emby=None):
        d = {"seerr": {"url": seerr, "key": KEYS[0]}}
        if emby:
            d["emby"] = {"url": emby, "key": EMBY_KEY}
        (t / name).write_text(json.dumps(d))

    cfg("cfg.json", f"{b}/seerr", e)
    cfg("noemby.json", f"{b}/seerr")
    cfg("seerr-down.json", f"http://127.0.0.1:{dead_port()}", e)
    (t / "play-on.json").write_text('{"enabled": true, "tv": "media_player.test_tv", "device": "TESTTV"}\n')
    media.env.update(
        MEDIA_SECRETS=str(t / "cfg.json"),
        MEDIA_TIMEOUT="5",
        MEDIA_PAGE_SIZE="4",
        MEDIA_VIBE_DIR=str(t / "vibe"),
        MEDIA_VIBE_EMBED_URL=e,
    )
    yield media
    elog = media.log(t / "emby.log").splitlines()
    assert [x for x in elog if not x.startswith("GET ")] == []
    assert [x for x in elog if "/Users/" in x and "/Users/aaaa1111" not in x] == []  # only his own history
    media.safety(keys=(*KEYS, EMBY_KEY))
    assert media.log(t / "emby.err") == ""


def test_vibe(vb):
    m, t = vb, vb.t
    emb = t / "emb.log"

    def mj(*a, **env):
        return m.m("vibe", *a, env=env, merge=False)

    def titles():
        return " | ".join(p["title"] for p in m.j["picks"])

    def docs():
        return sum(x.startswith("doc ") for x in m.log(emb).splitlines())

    def vectors():
        return len(list((t / "vibe").glob("vectors-*.f32")))

    # hint
    out = m.m("vibe")
    assert m.rc == 0 and "/vibe cozy rainy sunday" in out
    mj("--json")
    assert m.j["hint"].splitlines()[0] + " " + str(m.j["picks"]) == "🎬 /vibe + a mood or a film, e.g. []"
    assert not (t / "vibe").exists()  # a hint builds nothing

    # the first query builds the index
    emb.write_text("")
    mj("--json", "cozy", "rainy")
    d = m.j
    assert m.rc == 0
    assert docs() == 15  # all titles embedded once (paged library)
    assert [x for x in m.log(emb).splitlines() if x.startswith("query ")] == ["query cozy rainy movie"]
    assert (t / "vibe" / "index.json").stat().st_size > 0
    assert vectors() == 1
    assert oct((t / "vibe").stat().st_mode & 0o777) == "0o700"
    assert d["index"]["items"] == 15
    assert titles() == COZY
    assert "Puddle Jump" not in titles()  # rating floor
    assert [(p["seen"], "👁 seen" in p["why"]) for p in d["picks"] if p["title"] == "Rainy Window"][0] == (
        "played",
        True,
    )
    assert d["label"] == "cozy rainy"
    assert d["picks"][0]["why"] == "Comedy · cozy · “Pull up a chair.”"  # German genre -> Comedy
    assert d["picks"][0]["link"] == "https://emby.example.test/web/index.html#!/item?id=115&context=home&serverId=srv1"
    assert " | ".join(",".join(b.get("url", b.get("data", ""))[:40] for b in r) for r in d["buttons"]) == " | ".join(
        ["https://emby.example.test/web/index.html"] * 4
    )
    assert "**Cozy Little Cafe** (2022) · 84 min · ★6.9" in d["message"]
    txt = d["text"]
    assert "▶ https://emby.example.test/web/index.html#!/item?id=115" in txt
    assert "**" not in txt
    assert "1️⃣ Cozy Little Cafe (2022) · 84 min · ★6.9" in txt

    # no rebuild on the next query
    emb.write_text("")
    mj("--json", "cozy", "rainy", MEDIA_PLAY_CONFIG=str(t / "play-on.json"))
    assert docs() == 0 and titles() == COZY
    b = m.j["buttons"][0][1]
    assert (b["text"], b["data"], m.j["play_on_tv"]) == ("📺 Play on TV", "mt:play:115", True)

    # runtime caps
    mj("--json", "short", "cozy")
    assert all(p["runtime_min"] <= 100 for p in m.j["picks"]) and len(m.j["picks"]) == 4
    assert (m.j["label"], m.j["max_min"]) == ("cozy · ≤ 100 min", 100)
    mj("--json", "90min", "cozy")
    assert sorted(p["title"] for p in m.j["picks"]) == [
        "Cozy Little Cafe",
        "Ghost Lake Returns",
        "Puddle Jump",
        "Tiny Signal",
    ]
    assert "Puddle Jump" in titles()  # low-rated fills in when too few fit
    mj("--json", "heist", "under", "2h")
    assert (m.j["max_min"], m.j["picks"][0]["title"]) == (120, "Glass Vault")
    mj("--json", "Oceans", "11", "heist")
    assert m.j["max_min"] is None  # a bare number is no time cap

    # like <film>
    mj("--json", "like", "First Signal")
    d = m.j
    assert (d["ref"]["title"], d["ref"]["year"]) == ("First Signal", 2017)
    assert "First Signal |" not in titles()
    assert d["label"] == "like First Signal (2017)"
    assert (d["picks"][0]["title"], d["picks"][0]["seen"]) == ("Signal From Beyond", "partly")
    assert "Sci-Fi + Mystery like First Signal" in d["picks"][0]["why"]
    assert "👁 started" in d["picks"][0]["why"]
    assert m.log(emb).splitlines()[-1][:6] == "query "
    emb.write_text("")
    mj("--json", "first", "signal")
    assert m.j["ref"]["title"] == "First Signal"  # a bare title = like it (any case)
    assert not any(x.startswith("query") for x in m.log(emb).splitlines())
    mj("--json", "laugh", "like", "Harbor Lights")
    assert m.j["label"] == "like Harbor Lights (2018) · laugh"
    mj("--json", "something", "like", "Nowhere Film")
    assert "“Nowhere Film” isn't in the library" in m.j["message"] and len(m.j["picks"]) == 4
    mj("--json", "I'd", "like", "something", "cozy")
    assert "isn't in the library" not in m.j["message"]

    # diversity
    mj("--json", "-n", "2", "ghost", "lake", "movie")
    assert titles() == "Ghost Lake | Lake Phantom"  # a near-duplicate sequel gives way

    # incremental sync (new, changed, removed)
    (t / "scenario").write_text("grown\n")
    emb.write_text("")
    mj("--json", "cozy", "ghost")
    assert sorted(x for x in m.log(emb).splitlines() if x.startswith("doc ")) == [
        "doc Harbor Lights",
        "doc The Lantern Keeper",
    ]
    assert m.j["index"]["items"] == 15
    assert "The Lantern Keeper" in titles()
    assert vectors() == 1
    mj("--json", "like", "Dust Road")
    assert "Dust Road" not in str(m.j["ref"]) + " " + " ".join(p["title"] for p in m.j["picks"])

    # refresh
    emb.write_text("")
    out = m.m("vibe", "--refresh")
    assert m.rc == 0 and "vibe index: 15 movies (0 new, 0 changed, 0 removed; 0 embedded" in out
    assert m.m("vibe", "--refresh", "--quiet") == ""
    m.m("vibe", "--refresh", "--full", "--quiet")
    assert docs() == 15

    # failures
    mj("--json", "cozy", "rainy", MEDIA_SECRETS=str(t / "seerr-down.json"))
    assert m.rc == 0
    assert "couldn't check what you've seen" in m.j["message"]
    assert bool(m.j["picks"][0]["link"])  # links from the stored template
    (t / "scenario").write_text("emby-down\n")
    mj("--json", "cozy", "rainy")
    assert len(m.j["picks"]) == 4 and "Emby library" in " ".join(m.j["warnings"])
    out = m.m("vibe", "cozy", env={"MEDIA_VIBE_DIR": t / "empty"})
    assert m.rc == 1 and "no index yet and Emby is unreachable" in out
    (t / "scenario").write_text("embed-down\n")
    out = m.m("vibe", "cozy")
    assert m.rc == 1 and "media: embedding: HTTPError" in out
    (t / "scenario").write_text("ok\n")
    out = m.m("vibe", "cozy", env={"MEDIA_VIBE_EMBED_URL": "", "MEDIA_VIBE_MODEL": t / "none.gguf"})
    assert m.rc == 1 and "embedding: missing" in out
    m.m("vibe", "cozy", env={"MEDIA_SECRETS": t / "noemby.json"})
    assert m.rc == 1
    assert "-n must be 1..8" in m.m("vibe", "-n", "9", "cozy")
