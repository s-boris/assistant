"""ha-backup-pull tests: fake HA on 127.0.0.1, fake `ssh` (runs the command locally against a temp "remote"
folder), fake `unraid-ssh mode`. HOME is a temp dir, so ~/inbox, ~/.local/state and ~/backups are too.
HA_BACKUP_PULL_BIN=/path overrides the script under test (test a copy before installing it)."""

import datetime as dt
import glob
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
import unittest

from fake_ha_backup import TOKEN, FakeHA

HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.path.abspath(
    os.environ.get("HA_BACKUP_PULL_BIN") or os.path.join(HERE, "..", "..", "..", "scripts", "ha-backup-pull")
)
AUDIT = os.path.expanduser("~/bin/ha-audit")  # the live ha-audit (its fake-HA helpers)


def tar_bytes(tag):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        data = (f"synthetic backup {tag}\n" * 500).encode()
        ti = tarfile.TarInfo("backup.json")
        ti.size = len(data)
        t.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def backup(bid, days_ago, name="Automatic backup 2026.10.1", data=None, **kw):
    when = dt.datetime.now(dt.UTC) - dt.timedelta(days=days_ago)
    return dict(
        backup_id=bid,
        name=name,
        date=when.isoformat(timespec="seconds"),
        _data=tar_bytes(bid) if data is None else data,
        **kw,
    )


FRESH = {
    "last_completed_automatic_backup": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
    "last_attempted_automatic_backup": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hbp-test-")
        self.home = os.path.join(self.tmp, "home")
        self.remote = os.path.join(self.tmp, "remote", "ha")
        self.fakebin = os.path.join(self.tmp, "fakebin")
        os.makedirs(self.home)
        os.makedirs(self.fakebin)
        self.sshlog = os.path.join(self.tmp, "ssh.log")
        with open(os.path.join(self.fakebin, "ssh"), "w") as f:  # last arg = the remote command
            f.write(
                '#!/bin/sh\nfor a; do last=$a; done\nprintf "%s\\n" "$last" >> "$FAKE_SSH_LOG"\nexec sh -c "$last"\n'
            )
        with open(os.path.join(self.fakebin, "unraid-ssh"), "w") as f:
            f.write('#!/bin/sh\n[ "$1" = mode ] && { echo "$FAKE_MODE"; exit 0; }\nexit 2\n')
        for x in ("ssh", "unraid-ssh"):
            os.chmod(os.path.join(self.fakebin, x), 0o755)
        self.token = os.path.join(self.tmp, "token")
        with open(self.token, "w") as f:
            f.write(TOKEN + "\n")
        self.local = os.path.join(self.home, "backups", "ha")
        self.state_file = os.path.join(self.home, ".local", "state", "ha-backup-pull", "state.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_pull(self, ha, *args, mode="verbs", env=None):
        e = dict(
            os.environ,
            HOME=self.home,
            PATH=self.fakebin + os.pathsep + os.environ.get("PATH", ""),
            HA_AUDIT_LIB=AUDIT,
            UNRAID_SSH_BIN=os.path.join(self.fakebin, "unraid-ssh"),
            FAKE_MODE=mode,
            FAKE_SSH_LOG=self.sshlog,
            HA_BACKUP_DEST=self.remote,
            PYTHONDONTWRITEBYTECODE="1",
        )
        for k in ("HA_BACKUP_TARGET", "HA_BACKUP_LOCAL_DEST", "HA_BACKUP_MIN_FREE_MB"):
            e.pop(k, None)
        e.update(env or {})
        r = subprocess.run(
            ["python3", BIN, "--url", ha.url, "--token-file", self.token, *args],
            env=e,
            capture_output=True,
            text=True,
            timeout=120,
            stdin=subprocess.DEVNULL,
        )
        return r

    def notes(self):
        return sorted(glob.glob(os.path.join(self.home, "inbox", "ha-backup-pull-*.md")))

    def ssh_calls(self):
        try:
            with open(self.sshlog) as f:
                return f.read().splitlines()
        except FileNotFoundError:
            return []

    def state(self):
        with open(self.state_file) as f:
            return json.load(f)

    def tars(self, d):
        return sorted(os.path.basename(p) for p in glob.glob(os.path.join(d, "*.tar")))


class LocalTarget(Base):
    def test_verbs_mode_copies_locally_verified_and_private(self):
        bks = [backup("aaaa1111", 2), backup("bbbb2222", 1)]
        with FakeHA(bks, FRESH) as ha:
            r = self.run_pull(ha)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("2 copied", r.stdout)
            self.assertIn("local:" + self.local, r.stdout)
            log = list(ha.log)
        self.assertEqual(self.notes(), [])  # silent on success: no inbox note
        self.assertEqual(self.ssh_calls(), [])  # no ssh in the local target
        self.assertFalse(os.path.exists(self.remote))
        names = self.tars(self.local)
        self.assertEqual(len(names), 2)
        self.assertEqual(os.stat(self.local).st_mode & 0o777, 0o700)
        for b in bks:
            n = next(x for x in names if f"_{b['backup_id']}__" in x)
            p = os.path.join(self.local, n)
            self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)
            with open(p, "rb") as f:
                self.assertEqual(f.read(), b["_data"])
            with open(p + ".sha256") as f:
                self.assertEqual(f.read(), f"{hashlib.sha256(b['_data']).hexdigest()}  {n}\n")
        self.assertEqual([x for x in os.listdir(self.local) if x.startswith(".")], [])  # no partial/tmp left
        st = self.state()
        self.assertEqual(st["target"], "local:" + self.local)
        self.assertEqual({v["target"] for v in st["pulled"].values()}, {"local:" + self.local})
        # read-only towards HA: only the websocket + GET downloads with agent hassio.local
        rest = [x for x in log if x[0] == "REST"]
        self.assertTrue(all(m == "GET" for _k, m, _p in rest))
        dl = [p for _k, _m, p in rest if p != "/api/websocket"]
        self.assertEqual(len(dl), 2)
        self.assertTrue(all(p.startswith("/api/backup/download/") and p.endswith("?agent_id=hassio.local") for p in dl))
        self.assertEqual({t for k, t, _ in log if k == "WS"}, {"auth", "backup/info"})

    def test_second_run_copies_nothing(self):
        with FakeHA([backup("aaaa1111", 1)], FRESH) as ha:
            self.assertEqual(self.run_pull(ha).returncode, 0)
            r = self.run_pull(ha)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("0 copied", r.stdout)
            self.assertEqual(sum(1 for x in ha.log if x[0] == "REST" and "download" in x[2]), 1)

    def test_keep_rotation_partials_and_foreign_files(self):
        os.makedirs(self.local)
        foreign = os.path.join(self.local, "notes.txt")
        with open(foreign, "w") as f:
            f.write("not ours\n")
        stale = os.path.join(self.local, ".partial-2026-01-01_0315_dead0000__Automatic_backup.tar")
        fresh_part = os.path.join(self.local, ".partial-2026-01-02_0315_dead1111__Automatic_backup.tar")
        for p in (stale, fresh_part):
            with open(p, "wb") as f:
                f.write(b"x")
        old = time.time() - 2 * 86400
        os.utime(stale, (old, old))
        bks = [backup("aaaa1111", 3), backup("bbbb2222", 2), backup("cccc3333", 1)]
        with FakeHA(bks, FRESH) as ha:
            r = self.run_pull(ha, "--keep", "2")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("3 copied, 3 rotated out, 2 in local:", r.stdout)
            names = self.tars(self.local)
            self.assertEqual(len(names), 2)
            self.assertFalse(any("aaaa1111" in x for x in os.listdir(self.local)))  # tar + .sha256 gone
            self.assertTrue(os.path.exists(foreign))
            self.assertFalse(os.path.exists(stale))
            self.assertTrue(os.path.exists(fresh_part))  # < 24 h: may still be in use
            r = self.run_pull(ha, "--keep", "2")  # the rotated-out one is not pulled again
            self.assertIn("0 copied", r.stdout)

    def test_size_mismatch_one_note_per_day_and_no_leftovers(self):
        with FakeHA([backup("aaaa1111", 1, _size=12345)], FRESH) as ha:
            r = self.run_pull(ha)
            self.assertEqual(r.returncode, 1)
            self.assertIn("size mismatch", r.stdout)
            r = self.run_pull(ha)
            self.assertEqual(r.returncode, 1)
        self.assertEqual(len(self.notes()), 1)
        with open(self.notes()[0]) as f:
            body = f.read()
        self.assertIn("size mismatch", body)
        self.assertIn("local:" + self.local, body)
        self.assertEqual(os.listdir(self.local), [])

    def test_not_a_tar_fails_tar_check(self):
        with FakeHA([backup("aaaa1111", 1, data=os.urandom(4096))], FRESH) as ha:
            r = self.run_pull(ha)
        self.assertEqual(r.returncode, 1)
        self.assertIn("tar check failed", r.stdout)
        self.assertEqual(os.listdir(self.local), [])
        self.assertEqual(self.state()["pulled"], {})

    def test_low_disk_space_refuses_before_download(self):
        with FakeHA([backup("aaaa1111", 1)], FRESH) as ha:
            r = self.run_pull(ha, env={"HA_BACKUP_MIN_FREE_MB": str(1 << 40)})
            self.assertEqual(r.returncode, 1)
            self.assertIn("not enough free space", r.stdout)
            self.assertFalse(any("download" in x[2] for x in ha.log if x[0] == "REST"))
        self.assertEqual(os.listdir(self.local), [])

    def test_legacy_pulled_entries_belong_to_unraid(self):
        """A pulled entry without 'target' belongs to the unraid target, so the local target still copies it."""
        os.makedirs(os.path.dirname(self.state_file))
        with open(self.state_file, "w") as f:
            json.dump(
                {
                    "since": "2026-10-15T21:57:48+00:00",
                    "pulled": {"aaaa1111": {"file": "x.tar", "bytes": 1, "sha256": "0", "at": "x"}},
                },
                f,
            )
        with FakeHA([backup("aaaa1111", 1)], FRESH) as ha:
            r = self.run_pull(ha)
            self.assertIn("1 copied", r.stdout)
            self.assertEqual(self.state()["pulled"]["aaaa1111"]["target"], "local:" + self.local)

    def test_unknown_mode_and_explicit_local(self):
        with FakeHA([backup("aaaa1111", 1)], FRESH) as ha:
            r = self.run_pull(ha, env={"UNRAID_SSH_BIN": os.path.join(self.tmp, "missing")}, mode="root")
            self.assertIn("1 copied", r.stdout)
            r = self.run_pull(ha, "--target", "local", mode="root")
            self.assertIn("0 copied", r.stdout)
        self.assertEqual(self.ssh_calls(), [])

    def test_dry_run_writes_nothing(self):
        with FakeHA([backup("aaaa1111", 1)], FRESH) as ha:
            r = self.run_pull(ha, "--dry-run")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("[dry-run] would copy", r.stdout)
        self.assertFalse(os.path.exists(self.local))
        self.assertFalse(os.path.exists(self.state_file))
        self.assertEqual(self.notes(), [])

    def test_status_shows_target_and_files(self):
        with FakeHA([backup("aaaa1111", 1)], FRESH) as ha:
            self.run_pull(ha)
            r = self.run_pull(ha, "--status")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("target: local:" + self.local, r.stdout)
        self.assertIn("unraid-ssh mode verbs", r.stdout)
        self.assertIn("_aaaa1111__", r.stdout)
        self.assertNotIn('"pulled"', r.stdout)  # the per-backup block stays out of --status


class UnraidTarget(Base):
    def test_root_mode_uses_ssh_to_remote_dest(self):
        bks = [backup("aaaa1111", 1)]
        with FakeHA(bks, FRESH) as ha:
            r = self.run_pull(ha, mode="root")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("in unraid:" + self.remote, r.stdout)
        names = self.tars(self.remote)
        self.assertEqual(len(names), 1)
        with open(os.path.join(self.remote, names[0] + ".sha256")) as f:
            self.assertTrue(f.read().startswith(hashlib.sha256(bks[0]["_data"]).hexdigest()))
        self.assertTrue(self.ssh_calls())
        self.assertFalse(os.path.exists(self.local))
        self.assertEqual(self.notes(), [])

    def test_root_mode_skips_legacy_pulled(self):
        os.makedirs(os.path.dirname(self.state_file))
        with open(self.state_file, "w") as f:
            json.dump({"pulled": {"aaaa1111": {"file": "x.tar", "bytes": 1, "sha256": "0", "at": "x"}}}, f)
        with FakeHA([backup("aaaa1111", 1)], FRESH) as ha:
            r = self.run_pull(ha, mode="root")
        self.assertIn("0 copied", r.stdout)

    def test_unraid_target_refused_under_lockdown(self):
        with FakeHA([backup("aaaa1111", 1)], FRESH) as ha:
            r = self.run_pull(ha, "--target", "unraid", mode="verbs")
            self.assertEqual(r.returncode, 1)
            self.assertIn("needs unraid-ssh mode root", r.stdout)
            self.assertEqual(ha.log, [])  # HA not even contacted
        self.assertEqual(self.ssh_calls(), [])
        self.assertEqual(len(self.notes()), 1)
        with FakeHA([]) as ha2:
            r2 = self.run_pull(ha2, "--status", "--target", "unraid", mode="verbs")
        self.assertEqual(r2.returncode, 1)
        self.assertIn("PROBLEM:", r2.stdout)


class Allowlist(Base):
    def test_ws_and_download_allowlist(self):
        os.environ["HA_AUDIT_LIB"] = AUDIT
        loader = importlib.machinery.SourceFileLoader("hbp_under_test", BIN)
        spec = importlib.util.spec_from_loader("hbp_under_test", loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        with FakeHA([backup("aaaa1111", 1)], FRESH) as ha:
            ws = mod.WS(ha.url, TOKEN)
            try:
                for t, kw in (("backup/generate", {}), ("backup/info", {"x": 1}), ("backup/delete", {})):
                    with self.assertRaises(mod.NotAllowed):
                        ws.call(t, **kw)
            finally:
                ws.close()
            with self.assertRaises(mod.NotAllowed):
                mod.download(ha.url, TOKEN, "aaaa1111", "cloud.agent", lambda c: None)
            with self.assertRaises(mod.NotAllowed):
                mod.download(ha.url, TOKEN, "../etc", "hassio.local", lambda c: None)
            self.assertEqual({t for k, t, _ in ha.log if k == "WS"}, {"auth"})
        with self.assertRaises(mod.NotAllowed):
            mod.LocalStore(self.local)._path("../x")


if __name__ == "__main__":
    unittest.main()
