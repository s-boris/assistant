from core import drive

NOW = 1_792_800_000


def test_fresh_active_answer_is_driving():
    a = {"drive": {"active": True, "since": "x", "minutes": 12}, "age_min": 2}
    assert drive.verdict(a) == ("yes", 12)
    assert drive.active(a)


def test_stale_answer_is_unknown_not_driving():
    # a stuck flag: still on in an answer older than the rule's 60 min
    a = {"drive": {"active": True, "minutes": 5}, "age_min": 61}
    assert drive.verdict(a) == ("unknown", None)
    assert not drive.active(a)
    assert drive.active({"drive": {"active": True}, "age_min": 60})


def test_inactive_missing_and_odd_forms():
    assert drive.verdict({"drive": {"active": False, "minutes": None}, "age_min": 1}) == ("no", None)
    assert drive.verdict({"drive": None, "age_min": 1}) == ("unknown", None)
    assert drive.verdict({"drive": {"since": None}, "age_min": 1}) == ("unknown", None)
    assert drive.verdict({"drive": True}) == ("yes", None)  # boolean form, no age: counts
    assert drive.verdict({"drive": {"active": 1}}) == ("no", None)  # only a real true
    assert drive.verdict({"drive": {"active": True}, "age_min": True}) == ("yes", None)  # a bool is no age
    assert drive.verdict("garbage") == ("unknown", None)
    assert drive.verdict(None) == ("unknown", None)


def test_env_and_explicit_max_age(monkeypatch):
    a = {"drive": {"active": True}, "age_min": 45}
    monkeypatch.setenv("DRIVING_MAX_AGE_MIN", "30")
    assert drive.max_age_min() == 30
    assert not drive.active(a)
    assert drive.active(a, max_age=50)
    monkeypatch.setenv("DRIVING_MAX_AGE_MIN", "junk")
    assert drive.max_age_min() == drive.MAX_AGE_MIN == 60


def test_stored_flag_row():
    on = {"active": True}
    assert drive.stored(on, NOW - 30 * 60, NOW)
    assert not drive.stored(on, NOW - 61 * 60, NOW)  # the row itself is older than the rule
    assert drive.stored(on, (NOW - 60) * 1000, NOW)  # ms timestamps
    assert not drive.stored({"active": False}, NOW, NOW)
    assert not drive.stored(on, None, NOW)
    assert not drive.stored("x", NOW, NOW)
    # the gateway's own end (until) wins once passed, even on a fresh row
    assert not drive.stored({"active": True, "until": NOW - 1}, NOW - 60, NOW)
    assert drive.stored({"active": True, "until": NOW + 600}, NOW - 60, NOW)
