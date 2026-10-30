"""ha-health-watch tests: fake ha-audit JSON snapshots and fake `ha list` battery rows (no network).
HA_HEALTH_WATCH_BIN=/path overrides the script under test (test a copy before installing it). Every run gets
its own temp state dir, inbox and known-offline file; fake ha-audit / ha executables cover the subprocess path."""
import datetime as dt
import glob
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.path.abspath(os.environ.get('HA_HEALTH_WATCH_BIN') or
                      os.path.join(HERE, '..', '..', 'bin', 'ha-health-watch'))
T0 = dt.datetime(2030, 3, 4, 6, 40, tzinfo=dt.timezone.utc)


def read(p):
    with open(p) as fh:
        return fh.read()


def read_json(p):
    with open(p) as fh:
        return json.load(fh)


def day(n, hours=0):
    return (T0 + dt.timedelta(days=n, hours=hours)).isoformat()


def dev(did, name, integ='zha', model='M1', n=3, area='Kitchen'):
    return {'id': did, 'name': name, 'model': model, 'integration': integ, 'area': area, 'disabled_by': None,
            'n_entities': n}


def ent(eid, device='', integ='zha', days=3.0, cls='unavailable', restored=False, sensitive=False):
    return {'entity_id': eid, 'domain': eid.split('.', 1)[0], 'integration': integ, 'cls': cls, 'days': days,
            'basis': 'history', 'device': device, 'area': '', 'restored': restored, 'sensitive': sensitive}


def dead_device(did, name, integ='zha', days=3.0, domains=('light', 'sensor'), **kw):
    """(dev row, its unavailable entity rows)"""
    ents = [ent(f'{d}.{did}_{i}', device=name, integ=integ, days=days) for i, d in enumerate(domains)]
    return dev(did, name, integ=integ, n=len(ents), **kw), ents


def entry(eid, domain='wled', state='setup_retry', title=None, reason=None):
    return {'entry_id': eid, 'domain': domain, 'title': title or domain.upper(), 'state': state,
            'source': 'user', 'disabled_by': None, 'reason': reason}


def issue(iid, domain='homeassistant', tk='config_entry_reauth'):
    return {'domain': domain, 'issue_id': iid, 'severity': 'error', 'translation_key': tk, 'is_fixable': False,
            'ignored': False, 'breaks_in': None, 'days': 1.0}


def audit(devices=(), extra_bad=(), entries=(), issues=(), sup=(), errors=(), phones=(), uptime=5.0, states=100):
    dev_dead, bad = [], list(extra_bad)
    for d, ents in devices:
        dev_dead.append(d)
        bad += ents
    return {'ha_version': '2030.3.0', 'generated': '2030-03-04 06:40 UTC', 'errors': list(errors),
            'uptime_days': uptime, 'history_ok': True, 'counts': {'states': states},
            'bad': bad, 'dev_dead': dev_dead, 'int_failing': list(entries), 'issues_active': list(issues),
            'supervisor': {'resolution': {'issues': list(sup), 'unhealthy': [], 'unsupported': []}},
            'phones': list(phones)}


def bat(eid, val, name=None, binary=False):
    return {'entity_id': eid, 'name': name or eid.split('.', 1)[1].replace('_', ' ').title(), 'state': str(val),
            'area': 'Hallway', 'hidden': None,
            'attributes': {'device_class': 'battery'} if binary else
            {'device_class': 'battery', 'unit_of_measurement': '%'}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='hhw-test-')
        self.state = os.path.join(self.tmp, 'state')
        self.inbox = os.path.join(self.tmp, 'inbox')
        self.known = os.path.join(self.tmp, 'known.json')
        self.n = 0

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def env(self, **kw):
        e = dict(os.environ, HA_HEALTH_DIR=self.state, HA_HEALTH_INBOX=self.inbox, HA_HEALTH_KNOWN=self.known,
                 HA_HEALTH_RETRY_SECS='0', HA_AUDIT_BIN=os.path.join(self.tmp, 'no-audit'),
                 HA_BIN=os.path.join(self.tmp, 'no-ha'))
        e.update(kw)
        return e

    def cli(self, *args, env=None):
        r = subprocess.run([sys.executable, BIN, *args], capture_output=True, text=True, timeout=60,
                           env=env or self.env(), stdin=subprocess.DEVNULL)
        return r.returncode, r.stdout + r.stderr

    def run_once(self, A, now, batteries=(), dry=False, extra=()):
        self.n += 1
        ap = os.path.join(self.tmp, f'audit{self.n}.json')
        bp = os.path.join(self.tmp, f'bat{self.n}.json')
        with open(ap, 'w') as fh:
            json.dump(A, fh)
        with open(bp, 'w') as fh:
            json.dump(list(batteries), fh)
        args = ['--audit-json', ap, '--batteries-json', bp, '--now', now, *extra]
        if dry:
            args.append('--dry-run')
        before = set(self.notes())
        rc, out = self.cli(*args)
        new = sorted(set(self.notes()) - before)
        return rc, out, [read(p) for p in new]

    def notes(self):
        return glob.glob(os.path.join(self.inbox, '*.md'))

    def snaps(self):
        return sorted(glob.glob(os.path.join(self.state, 'snap-*.json')))

    def write_known(self, obj):
        with open(self.known, 'w') as fh:
            json.dump(obj, fh)


class TestDiff(Base):
    def test_baseline_then_no_change_is_silent(self):
        A = audit(devices=[dead_device('d1', 'Kitchen Bulb')], entries=[entry('e1')], issues=[issue('i1')])
        rc, out, notes = self.run_once(A, day(0), [bat('sensor.door_battery', 8)])
        self.assertEqual(rc, 0, out)
        self.assertIn('baseline recorded', out)
        self.assertEqual(notes, [])
        self.assertEqual(len(self.snaps()), 1)
        rc, out, notes = self.run_once(A, day(1), [bat('sensor.door_battery', 8)])
        self.assertEqual((rc, notes), (0, []), out)
        self.assertIn('0 new', out)

    def test_new_dead_device_noted_once_entities_rolled_up(self):
        self.run_once(audit(), day(0))
        d = dead_device('d2', 'Desk Lamp', integ='zha', days=2.0)
        rc, out, notes = self.run_once(audit(devices=[d]), day(1))
        self.assertEqual(len(notes), 1, out)
        n = notes[0]
        self.assertIn('Dead devices (1 new)', n)
        self.assertIn('Desk Lamp', n)
        self.assertIn('`dev:d2`', n)
        self.assertIn('Fix: Zigbee device silent', n)
        self.assertNotIn('Unavailable entities', n)        # its entities roll up into the device
        rc, out, notes = self.run_once(audit(devices=[d]), day(2))
        self.assertEqual(notes, [], 'persisting dead device must stay silent')

    def test_already_dead_in_baseline_is_silent(self):
        d = dead_device('d3', 'Old Plug', integ='wiz', days=30)
        self.run_once(audit(devices=[d]), day(0))
        rc, out, notes = self.run_once(audit(devices=[d]), day(1))
        self.assertEqual(notes, [])

    def test_loose_unavailable_entity(self):
        self.run_once(audit(), day(0))
        rc, out, notes = self.run_once(audit(extra_bad=[ent('sensor.attic_temp', device='Attic Node',
                                                            integ='esphome', days=1.0)]), day(1))
        self.assertEqual(len(notes), 1, out)
        self.assertIn('Unavailable entities (1 new)', notes[0])
        self.assertIn('ESPHome node offline', notes[0])

    def test_restored_entity_gets_cleanup_hint_and_location_skipped(self):
        self.run_once(audit(), day(0))
        A = audit(extra_bad=[ent('sensor.gone_thing', integ='spotify', restored=True),
                             ent('device_tracker.some_phone', integ='mobile_app'),
                             ent('sensor.phone_geocoded', integ='mobile_app', sensitive=True)])
        rc, out, notes = self.run_once(A, day(1))
        self.assertEqual(len(notes), 1, out)
        self.assertIn('cleanup candidate', notes[0])
        self.assertNotIn('device_tracker', notes[0])
        self.assertNotIn('geocoded', notes[0])

    def test_threshold_pending_then_noted(self):
        self.run_once(audit(), day(0))
        e = ent('switch.pump', device='Pump Plug', integ='tuya', days=0.2)
        rc, out, notes = self.run_once(audit(extra_bad=[e]), day(1))
        self.assertEqual(notes, [], 'below 12 h: pending, silent')
        self.assertIn('1 pending', out)
        rc, out, notes = self.run_once(audit(extra_bad=[dict(e, days=1.2)]), day(2))
        self.assertEqual(len(notes), 1, out)

    def test_media_only_device_needs_72h(self):
        self.run_once(audit(), day(0))
        tv = dead_device('tv1', 'Living TV', integ='cast', days=1.0, domains=('media_player',))
        rc, out, notes = self.run_once(audit(devices=[tv]), day(1))
        self.assertEqual(notes, [], 'a media player off for a day is not a problem')
        tv = dead_device('tv1', 'Living TV', integ='cast', days=3.5, domains=('media_player',))
        rc, out, notes = self.run_once(audit(devices=[tv]), day(3))
        self.assertEqual(len(notes), 1, out)
        self.assertIn('Cast device', notes[0])

    def test_cooldown_for_flapping(self):
        d = dead_device('d4', 'Flaky Bulb', days=1.0)
        self.run_once(audit(), day(0))
        self.assertEqual(len(self.run_once(audit(devices=[d]), day(1))[2]), 1)
        self.assertEqual(self.run_once(audit(), day(2))[2], [])            # recovered: silent
        self.assertEqual(self.run_once(audit(devices=[d]), day(3))[2], [], 'within 7 days: not again')
        self.assertEqual(self.run_once(audit(), day(4))[2], [])
        rc, out, notes = self.run_once(audit(devices=[d]), day(9))
        self.assertEqual(len(notes), 1, 'after the cooldown it is new again')

    def test_resolved_listed_in_next_note(self):
        d = dead_device('d5', 'Hall Bulb', days=2)
        self.run_once(audit(devices=[d]), day(0))
        rc, out, notes = self.run_once(audit(issues=[issue('new_issue')]), day(1))
        self.assertEqual(len(notes), 1)
        self.assertIn('Recovered since the last run: 1 (dev:d5)', notes[0])


class TestBatteries(Base):
    def test_low_battery_new_then_silent(self):
        self.run_once(audit(), day(0), [bat('sensor.door_battery', 60)])
        rc, out, notes = self.run_once(audit(), day(1), [bat('sensor.door_battery', 12, 'Door Sensor Battery')])
        self.assertEqual(len(notes), 1, out)
        self.assertIn('Low batteries (1 new)', notes[0])
        self.assertIn('Door Sensor Battery [Hallway]: sensor.door_battery, 12 %', notes[0])
        self.assertIn('Replace the battery', notes[0])
        rc, out, notes = self.run_once(audit(), day(2), [bat('sensor.door_battery', 11)])
        self.assertEqual(notes, [])

    def test_phone_batteries_and_hidden_skipped(self):
        self.run_once(audit(), day(0))
        rows = [bat('sensor.pixel_x_battery_level', 5),
                {'entity_id': 'sensor.pixel_x_battery_state', 'name': 'x', 'state': 'discharging', 'area': None,
                 'hidden': None, 'attributes': {'device_class': 'enum'}},
                bat('sensor.tab_one_battery_level', 3),
                dict(bat('sensor.[bt:xiaomi_ble:abc123]', 4), hidden='bluetooth'),
                bat('sensor.remote_battery', 'unavailable')]
        A = audit(phones=[{'name': 'Tab One'}])
        rc, out, notes = self.run_once(A, day(1), rows)
        self.assertEqual(notes, [], out)

    def test_binary_battery_low(self):
        self.run_once(audit(), day(0), [bat('binary_sensor.smoke_battery', 'off', binary=True)])
        rc, out, notes = self.run_once(audit(), day(1), [bat('binary_sensor.smoke_battery', 'on', binary=True)])
        self.assertEqual(len(notes), 1, out)
        self.assertIn('binary_sensor.smoke_battery, low', notes[0])

    def test_battery_source_failure_carries_over(self):
        env = self.env(HA_BIN=self.fake_ha(fail=True))
        A = os.path.join(self.tmp, 'a.json')
        with open(A, 'w') as fh:
            json.dump(audit(), fh)
        self.run_once(audit(), day(0), [bat('sensor.door_battery', 9)])
        r = subprocess.run([sys.executable, BIN, '--audit-json', A, '--now', day(1)], env=env,
                           capture_output=True, text=True, timeout=60)
        self.assertIn('carried over: bat', r.stdout)
        rc, out, notes = self.run_once(audit(), day(2), [bat('sensor.door_battery', 9)])
        self.assertEqual(notes, [], 'recovered battery source must not make old findings new')

    def fake_ha(self, fail=False, rows=()):
        p = os.path.join(self.tmp, 'fake-ha')
        with open(p, 'w') as fh:
            fh.write('#!/bin/sh\n' + ('echo "ha: HA error" >&2; exit 1\n' if fail else
                                       f"cat <<'EOF'\n{json.dumps(list(rows))}\nEOF\n"))
        os.chmod(p, stat.S_IRWXU)
        return p


class TestIssuesIntegrations(Base):
    def test_new_repair_and_supervisor_issue(self):
        self.run_once(audit(issues=[issue('old')]), day(0))
        A = audit(issues=[issue('old'), issue('deprecated_yaml_x', domain='template', tk='deprecated_yaml')],
                  sup=[{'type': 'no_current_backup', 'context': 'system', 'reference': None}])
        rc, out, notes = self.run_once(A, day(1))
        self.assertEqual(len(notes), 1, out)
        n = notes[0]
        self.assertIn('Repair issues (1 new)', n)
        self.assertIn('deprecated_yaml (template)', n)
        self.assertIn('Supervisor issues (1 new)', n)
        self.assertIn('ha-backup-pull --status', n)
        self.assertNotIn('`issue:homeassistant:old`', n)

    def test_failed_integration_and_ip_masked(self):
        self.run_once(audit(), day(0))
        A = audit(entries=[entry('e7', domain='esphome', reason='Timeout connecting to 198.51.100.23:6053')])
        rc, out, notes = self.run_once(A, day(1))
        self.assertEqual(len(notes), 1, out)
        self.assertIn('Failed integrations (1 new)', notes[0])
        self.assertIn('<ip>', notes[0])
        self.assertNotIn('198.51.100.23', notes[0])

    def test_setup_in_progress_never_starts_but_keeps_alive(self):
        self.run_once(audit(), day(0))
        rc, out, notes = self.run_once(audit(entries=[entry('e8', state='setup_in_progress')]), day(1))
        self.assertEqual(notes, [])
        self.assertEqual(len(self.run_once(audit(entries=[entry('e8')]), day(2))[2]), 1)
        rc, out, notes = self.run_once(audit(entries=[entry('e8', state='setup_in_progress')]), day(3))
        self.assertIn('0 recovered', out)
        self.assertEqual(notes, [])

    def test_auth_reason_hint(self):
        self.run_once(audit(), day(0))
        rc, out, notes = self.run_once(audit(entries=[entry('e9', 'cloudthing', 'setup_error',
                                                            reason='Device verification required')]), day(1))
        self.assertIn('re-authentication', notes[0])

    def test_restart_carries_entries(self):
        self.run_once(audit(entries=[entry('e10')]), day(0))
        rc, out, notes = self.run_once(audit(entries=[entry('e11')], uptime=0.01), day(1))
        self.assertEqual(notes, [], 'HA just restarted: entries are carried over, nothing new')
        self.assertIn('carried over: int', out)
        rc, out, notes = self.run_once(audit(entries=[entry('e10')]), day(2))
        self.assertEqual(notes, [])


class TestKnown(Base):
    def test_known_keys_names_and_rules(self):
        self.write_known({'keys': [{'key': 'dev:w1', 'label': 'strip, owner knows', 'name': 'Shelf Strip'},
                                   {'key': 'int:ew1', 'label': 'strip entry'}],
                          'rules': [{'integration': 'vacuumco', 'why': 'owner knows'}]})
        self.run_once(audit(), day(0))
        # strip alive but one entity down (known by device name), robot dead (rule), both entries failing
        A = audit(devices=[dead_device('v1', 'Robot', integ='vacuumco')],
                  extra_bad=[ent('select.shelf_strip_preset', device='Shelf Strip', integ='wled')],
                  entries=[entry('ew1'), entry('ev1', 'vacuumco', 'setup_error')])
        rc, out, notes = self.run_once(A, day(1))
        self.assertEqual(notes, [], out)
        self.assertIn('4 findings (4 known-offline)', out)
        A = audit(devices=[dead_device('w1', 'Shelf Strip', integ='wled'),
                           dead_device('v1', 'Robot', integ='vacuumco')],
                  entries=[entry('ew1'), entry('ev1', 'vacuumco', 'setup_error')])
        rc, out, notes = self.run_once(A, day(2))
        self.assertEqual(notes, [], out)
        self.assertIn('4 findings (4 known-offline)', out)

    def test_ack_known_unack(self):
        self.run_once(audit(devices=[dead_device('d9', 'Garden Light')]), day(0))
        rc, out = self.cli('ack', 'dev:d9', '--label', 'seasonal')
        self.assertEqual(rc, 0, out)
        k = read_json(self.known)
        self.assertEqual(k['keys'][0], {'key': 'dev:d9', 'label': 'seasonal', 'name': 'Garden Light'})
        self.assertEqual(oct(os.stat(self.known).st_mode & 0o777), '0o600')
        rc, out = self.cli('known')
        self.assertIn('dev:d9  seasonal', out)
        self.assertEqual(self.cli('ack', 'bogus')[0], 2)
        self.run_once(audit(), day(1))
        self.assertEqual(self.run_once(audit(devices=[dead_device('d9', 'Garden Light')]), day(9))[2], [])
        self.cli('unack', 'dev:d9')
        self.assertEqual(read_json(self.known)['keys'], [])


class TestModesAndFailures(Base):
    def test_dry_run_writes_nothing(self):
        self.run_once(audit(), day(0))
        st_before = read(os.path.join(self.state, 'state.json'))
        rc, out, notes = self.run_once(audit(devices=[dead_device('d6', 'Porch Bulb')]), day(1), dry=True)
        self.assertEqual(rc, 0, out)
        self.assertIn('[dry-run] would write ~/inbox note', out)
        self.assertIn('Porch Bulb', out)
        self.assertEqual(notes, [])
        self.assertEqual(len(self.snaps()), 1)
        self.assertEqual(read(os.path.join(self.state, 'state.json')), st_before)
        rc, out, notes = self.run_once(audit(devices=[dead_device('d6', 'Porch Bulb')]), day(1))
        self.assertEqual(len(notes), 1, 'the real run still finds it new')

    def test_dry_run_without_baseline(self):
        rc, out, notes = self.run_once(audit(devices=[dead_device('d1', 'X')]), day(0), dry=True)
        self.assertIn('no baseline yet', out)
        self.assertEqual((notes, self.snaps()), ([], []))

    def fake_audit(self, mode):
        p = os.path.join(self.tmp, 'fake-audit')
        good = json.dumps(audit(devices=[dead_device('d1', 'Lamp')]))
        with open(p, 'w') as fh:
            fh.write('#!/bin/sh\n'
                     f'[ "$(cat {self.tmp}/mode)" = fail ] && {{ echo "HA unreachable at 198.51.100.7" >&2; exit 1; }}\n'
                     f"cat > \"$2\" <<'EOF'\n{good}\nEOF\necho summary\n")
        os.chmod(p, stat.S_IRWXU)
        with open(os.path.join(self.tmp, 'mode'), 'w') as fh:
            fh.write(mode)
        return p

    def set_mode(self, mode):
        with open(os.path.join(self.tmp, 'mode'), 'w') as fh:
            fh.write(mode)

    def test_live_path_and_failure_streak(self):
        fa = self.fake_audit('ok')
        fh = TestBatteries.fake_ha(self, rows=[bat('sensor.x_battery', 50)])
        env = self.env(HA_AUDIT_BIN=fa, HA_BIN=fh)
        rc, out = self.cli('--now', day(0), env=env)
        self.assertEqual(rc, 0, out)
        self.assertIn('baseline recorded: 1 findings', out)
        self.assertTrue(os.path.exists(os.path.join(self.state, 'audit-latest.json')))
        self.set_mode('fail')
        rc, out = self.cli('--now', day(1), env=env)
        self.assertEqual(rc, 1, out)
        notes = self.notes()
        self.assertEqual(len(notes), 1)
        body = read(notes[0])
        self.assertIn('not readable', body)
        self.assertNotIn('198.51.100.7', body)
        self.assertEqual(self.cli('--now', day(2), env=env)[0], 1)
        self.assertEqual(len(self.notes()), 1, 'one note per failure streak')
        self.set_mode('ok')
        rc, out = self.cli('--now', day(3), env=env)
        self.assertEqual(rc, 0, out)
        self.assertIn('0 new', out)
        rc, out = self.cli('status', env=env)
        self.assertIn('Dead devices: 1', out)
        self.set_mode('fail')
        self.cli('--now', day(4), env=env)
        self.assertEqual(len(self.notes()), 2, 'a new streak notes again')

    def test_no_states_is_a_failure(self):
        rc, out, notes = self.run_once(audit(states=0), day(0))
        self.assertEqual(rc, 1)
        self.assertEqual(len(notes), 1)
        self.assertEqual(self.snaps(), [])

    def test_partial_repairs_error_carries_issues(self):
        self.run_once(audit(issues=[issue('i5')]), day(0))
        rc, out, notes = self.run_once(audit(errors=['repairs: HAError: boom']), day(1))
        self.assertIn('carried over: issue', out)
        self.assertIn('0 recovered', out)
        rc, out, notes = self.run_once(audit(issues=[issue('i5')]), day(2))
        self.assertEqual(notes, [])

    def test_privacy_gate_on_note(self):
        self.run_once(audit(), day(0))
        name = 'Lamp near 10.1234567 owner@example.com Bahnhofstrasse 12'
        rc, out, notes = self.run_once(audit(devices=[dead_device('d7', name)]), day(1))
        n = notes[0]
        for raw in ('10.1234567', 'owner@example.com', 'Bahnhofstrasse 12'):
            self.assertNotIn(raw, n)
        for tag in ('<coord>', '<email>', '<address>'):
            self.assertIn(tag, n)

    def test_many_new_are_capped(self):
        self.run_once(audit(), day(0))
        devs = [dead_device(f'm{i:02d}', f'Bulb {i}', integ='zha' if i % 2 else 'hue') for i in range(20)]
        rc, out, notes = self.run_once(audit(devices=devs), day(1))
        self.assertIn('Dead devices (20 new)', notes[0])
        self.assertIn('+5 more:', notes[0])

    def test_snapshot_retention(self):
        for i in range(33):
            self.run_once(audit(), day(i))
        self.assertEqual(len(self.snaps()), 30)

    def test_read_only_by_construction(self):
        src = read(BIN)
        for bad in ("'ha-apply'", 'call_service', 'ha-admin-token', "'POST'", 'import urllib', 'import socket',
                    'import requests', 'http.client'):
            self.assertNotIn(bad, src)
        calls = [ln.strip() for ln in src.splitlines() if 'subprocess.run(' in ln]
        self.assertEqual(len(calls), 2, calls)
        self.assertTrue(calls[0].startswith("r = subprocess.run([AUDIT_BIN, '--json', out]"), calls[0])
        self.assertTrue(calls[1].startswith("r = subprocess.run([HA_BIN, 'list', dom, '--json'"), calls[1])


if __name__ == '__main__':
    unittest.main()
