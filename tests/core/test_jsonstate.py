import json
import os
import stat
import threading

import pytest

from core import jsonstate


def mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


def test_load_missing_broken_and_strict(tmp_path):
    p = tmp_path / "s.json"
    assert jsonstate.load(p, {"d": 1}) == {"d": 1}
    assert jsonstate.load(p, None, strict=True) is None  # missing is never an error
    p.write_text("{broken")
    assert jsonstate.load(p) is None
    assert jsonstate.load(p, []) == []
    with pytest.raises(ValueError):
        jsonstate.load(p, {}, strict=True)
    d = tmp_path / "dir.json"
    d.mkdir()
    assert jsonstate.load(d, 7) == 7
    with pytest.raises(OSError):
        jsonstate.load(d, 7, strict=True)


def test_save_bytes_match_json_dumps(tmp_path):
    p = tmp_path / "a" / "b" / "state.json"
    obj = {"b": 1, "a": ["é", 2]}
    jsonstate.save(p, obj, indent=1, sort_keys=True)
    assert p.read_text(encoding="utf-8") == json.dumps(obj, indent=1, sort_keys=True)
    jsonstate.save(p, obj, newline=True, ensure_ascii=False, indent=1)
    assert p.read_text(encoding="utf-8") == json.dumps(obj, ensure_ascii=False, indent=1) + "\n"
    assert jsonstate.load(p) == obj
    assert sorted(os.listdir(p.parent)) == ["state.json"]  # no temp file left behind


def test_modes_new_kept_and_explicit(tmp_path):
    old = os.umask(0o022)
    try:
        p = tmp_path / "new.txt"
        jsonstate.write_text(p, "x")
        assert mode(p) == 0o644  # like open(): 0666 minus the umask
        os.chmod(p, 0o600)
        jsonstate.write_text(p, "y")
        assert mode(p) == 0o600 and p.read_text() == "y"  # a private file stays private
        jsonstate.write_text(p, "z", mode=0o640)
        assert mode(p) == 0o640
        q = tmp_path / "priv" / "s.json"
        jsonstate.save(q, {}, mode=0o600, dir_mode=0o700)
        assert mode(q) == 0o600 and mode(q.parent) == 0o700
        assert jsonstate.current_umask() == 0o022
    finally:
        os.umask(old)


def test_failed_write_leaves_old_file_and_no_temp(tmp_path, monkeypatch):
    p = tmp_path / "s.json"
    jsonstate.save(p, {"ok": 1})
    with pytest.raises(TypeError):
        jsonstate.save(p, {"bad": object()})
    assert jsonstate.load(p) == {"ok": 1}

    def bad_fsync(fd):
        raise OSError(5, "simulated I/O error")

    monkeypatch.setattr(os, "fsync", bad_fsync)
    with pytest.raises(OSError):
        jsonstate.write_bytes(p, b"half")
    monkeypatch.undo()
    assert jsonstate.load(p) == {"ok": 1}
    assert os.listdir(tmp_path) == ["s.json"]


def test_readers_never_see_half_a_file(tmp_path):
    p = tmp_path / "big.json"
    blobs = [{"n": i, "pad": "x" * 200_000} for i in range(2)]
    jsonstate.save(p, blobs[0])
    stop = threading.Event()
    bad = []

    def reader():
        while not stop.is_set():
            try:
                d = json.loads(p.read_text())
                if d.get("pad") != "x" * 200_000:
                    bad.append("pad")
            except ValueError:
                bad.append("half")

    t = threading.Thread(target=reader)
    t.start()
    for i in range(60):
        jsonstate.save(p, blobs[i % 2])
    stop.set()
    t.join()
    assert not bad


def test_relative_path_and_str_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    jsonstate.write_text("rel.txt", "r")
    assert (tmp_path / "rel.txt").read_text() == "r"
    jsonstate.save(str(tmp_path / "s.json"), [1])
    assert jsonstate.load(str(tmp_path / "s.json")) == [1]
