"""gym plan: drafts from a synthetic log (made-up values), push/pull against an in-memory loopback Jefit routine
API, the push through the hub's egress (a fake hub), plan next, travel weeks and other activities. No network."""

import copy
import datetime as dt
import email.utils
import http.server
import json
import os
import re
import stat
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gym_testlib as base  # noqa: E402
from gym_testlib import (  # noqa: E402
    LOG,
    NOW,
    REFS,
    FakeHub,
    WStub,
    add_session,
    day_exs,
    jid,
    make_db,
    plan_routine,
    writes,
)


class TestPlan:
    @classmethod
    def setup_class(cls):
        cls.m = base.load_module()
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), WStub)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def teardown_class(cls):
        cls.srv.shutdown()

    def setup_method(self):
        WStub.reset()
        self.e = base.Env(now=NOW)
        self.e.env.update(
            JEFIT_API_BASE=self.base,
            GYM_WRITE_DELAY="0",
            GYM_RETRY_BASE="0",
            GYM_TOKEN_RW_FILE=os.path.join(self.e.dir, "secrets", "jefit-token-rw"),
        )
        self.conn, self.xid = make_db(self.e.env["GYM_DB"], self.m)
        self.state = os.path.dirname(self.e.env["GYM_DB"])
        self.hubs = []

    def teardown_method(self):
        for hub in self.hubs:
            hub.shutdown()
            hub.server_close()
        self.conn.close()
        self.e.close()

    def draft(self, *extra):
        return json.loads(self.e.run("plan", "draft", "--minutes", "60", "--json", *extra).stdout)

    def plan_file(self):
        with open(os.path.join(self.state, "plan.json")) as fh:
            return json.load(fh)

    def test_draft_picks_from_log_and_saves_privately(self):
        p = self.draft()
        a, b = p["days"]
        assert [x["name"] for x in a["exercises"]] == [
            "Barbell Squat",
            "Barbell Bench Press",
            "Cable Lat Pulldown (Wide Grip)",
            "Machine Seated Leg Curl",
            "Dumbbell Lateral Raise",
            "Dumbbell Tricep Extension",
            "Weighted Crunch",
        ]
        assert [x["label"] for x in a["exercises"]] == ["1", "2", "3a", "3b", "4a", "4b", "5"]
        assert b["exercises"][0]["name"] == "Barbell Deadlift"
        assert "Machine Hack Squat" not in json.dumps(p)  # fewer sessions than the barbell squat
        x = {x["name"]: x for d in p["days"] for x in d["exercises"]}
        # bench 80 x 6 -> e1RM 96; 4-6 range starts at 96 / (1 + 7/30) = 77.8 -> 77.5 (barbell step 2.5)
        assert (x["Barbell Bench Press"]["weight_kg"], x["Barbell Bench Press"]["reps"]) == (77.5, 5)
        assert (x["Barbell Squat"]["sets"], x["Barbell Squat"]["lo"], x["Barbell Squat"]["hi"]) == (3, 4, 6)
        assert x["Machine Leg Press"]["weight_kg"] == 140.0  # 150 x 10 -> 200 / 1.4 = 142.9 -> step 5
        assert x["Barbell Squat"]["inc_kg"] == 5.0
        assert x["Machine Leg Press"]["inc_kg"] == 10.0
        assert x["Hanging Leg Raise"]["weight_kg"] is None
        assert x["Barbell Bench Press"]["jefit_id"] == jid("Barbell Bench Press")
        mode = os.stat(os.path.join(self.state, "plan.json")).st_mode
        assert stat.S_IMODE(mode) == 0o600
        assert p["volume"]["chest"] == 6.0
        card = self.e.run("plan", "show").stdout
        assert "3a Cable Lat Pulldown (Wide Grip)  3×8–12 @ 55 kg" in card
        assert "not pushed yet" in card
        assert WStub.log == []  # drafting never calls the API

    def test_draft_swap_minutes_and_dry_run(self):
        p = self.draft("--swap", "Dumbbell Tricep Extension=Cable Tricep Pushdown (Rope)")
        assert "Cable Tricep Pushdown (Rope)" in [x["name"] for x in p["days"][0]["exercises"]]
        assert "not in your log" in self.e.run("plan", "draft", "--swap", "A=Nope", code=2).stderr
        assert "Old name=New name" in self.e.run("plan", "draft", "--swap", "nonsense", code=2).stderr
        short = json.loads(self.e.run("plan", "draft", "--minutes", "40", "--json", "--dry-run").stdout)
        assert any("left out" in n for n in short["notes"])
        assert self.plan_file()["minutes"] == 60  # --dry-run did not overwrite
        other = base.Env(now=NOW)
        assert "no plan yet" in other.run("plan", "push", code=2).stderr
        other.close()

    def test_push_dry_run_sends_nothing(self):
        self.draft()
        self.e.token()
        out = self.e.run("plan", "push").stdout
        assert WStub.log == []
        assert "Dry run: nothing sent" in out
        assert 'POST   /api/v2/user/routines {"create_day":true}' in out
        assert 'PATCH  /api/v2/routines/{T} {"data":{"name":"Assistant test"}}' in out
        assert (
            'POST   /api/v2/days/{{D1}}/day_exercises {{"data":{{"exercise":{{"id":"{}"}}}}}}'.format(
                jid("Barbell Squat")
            )
            in out
        )
        assert f'"weight_lbs":{round(77.5 / 0.45359237, 4)}' in out  # 77.5 kg in pounds
        assert base.TOKEN not in out
        assert "/users/" not in out
        res = json.loads(self.e.run("plan", "push", "--json").stdout)
        assert res["dry_run"] and res["test_sequence"]
        assert {r["step"] for r in res["requests"]} == {"test", "plan"}

    def test_apply_tests_then_creates_only_its_routine(self):
        self.draft()
        self.e.token()
        refs_before = copy.deepcopy({rid: WStub.routines[rid] for rid, _ in REFS})
        out = self.e.run("plan", "push", "--apply").stdout
        assert "Set it as the current plan" in out
        assert "tap Sync on the Progress tab" in out  # the phone app pulls only on Sync
        w = writes()
        assert all(not any(rid in p for rid, _ in REFS) for _, p in w)  # never touched his routines
        assert {rid: WStub.routines[rid] for rid, _ in REFS} == refs_before
        test_id = re.search(r"u_\d{12}", w[1][1]).group(0)  # the PATCH that named the test routine
        assert w[0] == ("POST", "/api/v2/user/routines")
        assert ("DELETE", f"/api/v2/routines/{test_id}") in w
        assert test_id not in WStub.routines
        r = plan_routine()
        assert "[gym-plan]" in r["description"]
        assert [d["name"] for d in sorted(r["days"], key=lambda d: d["index"])] == [
            "A · Squat + Bench Press",
            "B · Deadlift + Military Press",
        ]
        a = day_exs(r, 0)
        assert a[1]["exercise"]["id"] == jid("Barbell Bench Press")
        assert (a[1]["count"], len(a[1]["sets"]), a[1]["number_of_reps"]) == (3, 3, [5, 5, 5])
        assert round(abs(a[1]["sets"][0]["weight_lbs"] - 77.5 / 0.45359237), 3) == 0
        assert a[2]["rest_time"] == 30  # 3a: switch to the pair partner
        with open(os.path.join(self.state, "plan-pushed.json")) as fh:
            pushed = json.load(fh)
        assert (pushed["status"], pushed["routine_id"]) == ("complete", r["id"])
        assert pushed["verified"]
        assert stat.S_IMODE(os.stat(os.path.join(self.state, "plan-push.log")).st_mode) == 0o600
        WStub.log = []
        assert "Jefit already matches" in self.e.run("plan", "push").stdout
        res = json.loads(self.e.run("plan", "push", "--apply", "--json").stdout)
        assert writes() == []  # nothing to change, no second test
        assert not res["app_sync"]  # no write, no Sync hint
        assert not res["dry_run"]

    def test_update_progresses_and_pushes_only_the_diff(self):
        self.draft()
        self.e.token()
        self.e.run("plan", "push", "--apply")
        r = plan_routine()
        bench_de = day_exs(r, 0)[1]["id"]
        later = dt.date.fromisoformat(NOW) + dt.timedelta(days=2)
        add_session(self.conn, later, [("Barbell Bench Press", 77.5, 6), ("Barbell Squat", 92.5, 5)], self.xid)
        self.conn.commit()
        WStub.log = []
        dry = self.e.run("plan", "update", now="2026-10-21").stdout
        assert "Barbell Bench Press: 20.10. top of the range on every set: +2.5 kg" in dry
        assert "Not saved" in dry
        assert self.plan_file()["version"] == 1
        res = json.loads(self.e.run("plan", "update", "--apply", "--json", now="2026-10-21").stdout)
        assert res["version"] == 2
        assert res["push"]["app_sync"]  # changed in Jefit: Sync hint
        assert "Barbell Squat: target 6 reps" in res["changes"]
        assert all(m == "PATCH" for m, _ in writes())
        assert {p for _, p in writes()} - {
            f"/api/v2/day_exercises/{bench_de}",
            f"/api/v2/day_exercises/{day_exs(r, 0)[0]['id']}",
        } == set()
        bench = day_exs(plan_routine(), 0)[1]
        assert round(abs(bench["sets"][0]["weight_lbs"] - 80 / 0.45359237), 3) == 0
        assert bench["number_of_reps"] == [4, 4, 4]
        assert self.plan_file()["version"] == 2
        assert os.listdir(os.path.join(self.state, "plan-history"))

    def test_stalled_lift_is_swapped(self):
        self.draft()
        self.e.token()
        self.e.run("plan", "push", "--apply")
        for k in range(3):  # squat at the plan weight but stuck at 4 reps: three exposures without progress
            add_session(
                self.conn, dt.date(2026, 10, 20) + dt.timedelta(days=7 * k), [("Barbell Squat", 92.5, 4)], self.xid
            )
        self.conn.commit()
        WStub.log = []
        res = json.loads(self.e.run("plan", "update", "--apply", "--json", now="2026-11-05").stdout)
        assert "Barbell Squat → Machine Hack Squat (stalled 3 sessions)" in res["changes"]
        a = day_exs(plan_routine(), 0)
        assert a[0]["exercise"]["id"] == jid("Machine Hack Squat")
        assert len(a) == 7
        assert "DELETE" in {m for m, _ in writes()}
        assert ("POST", "/api/v2/user/routines") not in writes()

    def test_guard_refuses_a_routine_changed_by_hand(self):
        self.draft()
        self.e.token()
        self.e.run("plan", "push", "--apply")
        plan_routine()["name"] = "My own now"
        self.draft("--swap", "Dumbbell Tricep Extension=Cable Tricep Pushdown (Rope)")
        WStub.log = []
        p = self.e.run("plan", "push", "--apply", code=1)
        assert "not touching it" in p.stderr
        assert writes() == []

    def test_read_only_token_stops_at_the_first_write(self):
        self.draft()
        self.e.token()
        WStub.read_only = True
        p = self.e.run("plan", "push", "--apply", code=1)
        assert "write scope" in p.stderr
        assert base.TOKEN not in p.stdout + p.stderr
        assert writes() == [("POST", "/api/v2/user/routines")]
        assert len(WStub.routines) == 2

    def egress_env(self, mode):
        """The live default token file under a throwaway HOME, the egress switch for "gym", a fake hub."""
        home = os.path.join(self.e.dir, "home")
        os.makedirs(os.path.join(home, ".config", "assistant"))
        with open(os.path.join(home, ".config", "assistant", "egress.json"), "w") as fh:
            json.dump({"handles": {"gym": mode}}, fh)
        hub = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeHub)
        threading.Thread(target=hub.serve_forever, daemon=True).start()
        self.hubs.append(hub)
        FakeHub.seen = []
        self.e.env.update(
            HOME=home,
            GYM_TOKEN_FILE=os.path.join(home, ".secrets", "jefit-token"),
            ASSISTANT_TEST="1",
            HUB_URL=f"http://127.0.0.1:{hub.server_address[1]}",
        )
        self.e.token()
        with open(os.path.join(home, ".secrets", "hub-token"), "w") as fh:
            fh.write("hub-tok\n")

    def test_push_with_the_read_token_goes_by_handle_through_the_hub(self):
        self.draft()
        self.egress_env("hub")
        self.e.run("plan", "push", "--apply")
        assert plan_routine()
        fetched = [(h, m, p) for v, h, m, p, _ in FakeHub.seen if v == "/egress/fetch"]
        assert {h for h, _, _ in fetched} == {"gym"}
        assert [(m, p) for _, m, p in fetched if m != "GET"] == writes()  # every write went by the hub
        assert not any(leak for *_, leak in FakeHub.seen)  # the token never left the "brain"

    def test_push_in_hub_mode_needs_no_token_file(self):
        self.draft()
        self.egress_env("hub")
        os.remove(self.e.env["GYM_TOKEN_FILE"])  # the hub holds the token
        self.e.run("plan", "push", "--apply")
        assert plan_routine()
        fetched = [(m, p) for v, h, m, p, _ in FakeHub.seen if v == "/egress/fetch" and h == "gym"]
        assert [x for x in fetched if x[0] != "GET"] == writes()
        assert not any(leak for *_, leak in FakeHub.seen)

    def test_push_in_local_mode_without_the_token_file_is_a_clear_error(self):
        self.draft()
        self.egress_env("local")
        os.remove(self.e.env["GYM_TOKEN_FILE"])
        p = self.e.run("plan", "push", "--apply", code=1)
        assert "core.egress gym, local mode" in p.stderr
        assert (writes(), FakeHub.seen) == ([], [])

    def test_push_in_shadow_mode_checks_every_write(self):
        self.draft()
        self.egress_env("shadow")
        self.e.run("plan", "push", "--apply")
        checked = [(m, p) for v, _, m, p, _ in FakeHub.seen if v == "/egress/check" and m != "GET"]
        assert checked == writes()
        assert not any(leak for *_, leak in FakeHub.seen)

    def test_writer_allowlist(self):
        w = self.m.PlanWriter({"base": self.base}, base.TOKEN, delay=0)
        w.owned.add("u_000000000555")
        assert w.allowed("POST", "/api/v2/user/routines")
        assert w.allowed("PATCH", "/api/v2/routines/u_000000000555")
        assert not w.allowed("PATCH", "/api/v2/routines/u_000000000101")  # not ours
        assert not w.allowed("DELETE", "/api/v2/routines/u_000000000555")  # owned but not deletable
        assert not w.allowed("POST", "/api/v2/update-setting")
        assert not w.allowed("POST", "/api/v2/routines/u_000000000555/publish")
        with pytest.raises(self.m.PlanError):
            w.call("DELETE", "/api/v2/routines/u_000000000101")
        assert WStub.log == []

    def pushed(self):
        with open(os.path.join(self.state, "plan-pushed.json")) as fh:
            return json.load(fh)

    def journal(self):
        with open(os.path.join(self.state, "plan-push.log")) as fh:
            return [json.loads(x) for x in fh]

    def test_429_is_retried_and_each_exercise_is_one_post_and_one_patch(self):
        p = self.draft()
        self.e.token()
        WStub.throttle = {"from": 12, "left": 2, "after": "0"}
        out = self.e.run("plan", "push", "--apply").stdout
        assert "2 rate-limit retries" in out
        assert [j["status"] for j in self.journal()].count(429) == 2
        assert self.pushed()["status"] == "complete"
        n = sum(len(d["exercises"]) for d in p["days"])
        fields = {"count", "sets", "number_of_reps", "rest_time"}
        batched = {
            pa for m, pa, b in WStub.log if m == "PATCH" and "/day_exercises/" in pa and set(b["data"]) == fields
        }
        assert len(batched) == n  # all four fields in one PATCH
        plan_writes = [w for w in writes() if w[0] != "GET"]
        assert len(plan_writes) == 5 + 2 + 2 * n + 2 + 2  # test 5, create+name, day patch+add, 2 retries
        assert len(day_exs(plan_routine(), 0)) == len(p["days"][0]["exercises"])

    def test_push_stopped_half_way_resumes_in_the_same_routine(self):
        p = self.draft()
        self.e.token()
        refs_before = copy.deepcopy({rid: WStub.routines[rid] for rid, _ in REFS})
        WStub.throttle = {"from": 20, "left": 99}  # write 20 is an exercise PATCH: never passes
        err = self.e.run("plan", "push", "--apply", code=1).stderr
        assert "still rate-limited" in err
        assert self.pushed()["status"] == "incomplete"
        half = plan_routine()
        assert sum(len(d["day_exercises"]) for d in half["days"]) < sum(len(d["exercises"]) for d in p["days"])
        assert "stopped half-way" in self.e.run("plan", "show").stdout
        dry = self.e.run("plan", "push").stdout
        assert "Resume:" in dry
        WStub.throttle, WStub.log = None, []
        out = self.e.run("plan", "push", "--apply").stdout
        assert "resumed the half-done push" in out
        assert "Set it as the current plan" in out
        w = writes()
        assert ("POST", "/api/v2/user/routines") not in w  # no second routine, no rebuild
        assert not [x for x in w if x[0] == "DELETE"]
        assert len(w) < 2 * sum(len(d["exercises"]) for d in p["days"])
        r = plan_routine()
        assert r["id"] == half["id"]
        for i, d in enumerate(p["days"]):
            assert [e["exercise"]["id"] for e in day_exs(r, i)] == [x["jefit_id"] for x in d["exercises"]]
            assert all(
                e["count"] == x["sets"] and len(e["sets"]) == x["sets"]
                for e, x in zip(day_exs(r, i), d["exercises"], strict=False)
            )
        assert self.pushed()["status"] == "complete"
        assert {rid: WStub.routines[rid] for rid, _ in REFS} == refs_before
        WStub.log = []
        self.e.run("plan", "push", "--apply")
        assert writes() == []

    def test_leftovers_are_deleted_only_when_this_tool_created_them(self):
        self.draft()
        self.e.token()
        mine, his = "u_000000009001", "u_000000009002"
        for rid in (mine, his):
            WStub.routines[rid] = {
                "id": rid,
                "name": "Assistant test",
                "description": "",
                "last_updated": 5,
                "days": [],
            }
        os.makedirs(self.state, exist_ok=True)
        with open(os.path.join(self.state, "plan-push.log"), "a") as fh:
            fh.write(
                json.dumps({"method": "POST", "path": "/api/v2/user/routines", "status": 201, "new_id": mine}) + "\n"
            )
        err = self.e.run("plan", "push", "--apply", code=1).stderr
        assert "did not create" in err
        assert writes() == []
        del WStub.routines[his]
        out = self.e.run("plan", "push", "--apply").stdout
        assert "deleted leftover Assistant test" in out
        assert writes()[0] == ("DELETE", f"/api/v2/routines/{mine}")
        assert mine not in WStub.routines
        plan_routine()

    def test_created_ids_from_the_journal(self):
        path = os.path.join(self.e.dir, "push.log")
        with open(path, "w") as fh:
            for j in (
                {"method": "POST", "path": "/api/v2/user/routines", "status": 201},  # older line: no new_id
                {"method": "PATCH", "path": "/api/v2/routines/u_000000000777", "status": 200},
                {"method": "PATCH", "path": "/api/v2/routines/u_000000000778", "status": 200},
                {"method": "POST", "path": "/api/v2/user/routines", "status": 429},
                {"method": "PATCH", "path": "/api/v2/routines/u_000000000779", "status": 200},
                {"method": "POST", "path": "/api/v2/user/routines", "status": 201, "new_id": "u_000000000780"},
            ):
                fh.write(json.dumps(j) + "\n")
            fh.write("not json\n")
        assert self.m.created_routine_ids(path) == {"u_000000000777", "u_000000000780"}

    def test_writer_retry_after_backoff_and_pacing(self):
        waits = []
        w = self.m.PlanWriter({"base": self.base}, base.TOKEN, delay=0, sleep=waits.append, backoff=5)
        WStub.throttle = {"from": 1, "left": 2, "after": "7", "get": True}
        w.call("GET", "/api/v2/user/routines")
        assert (waits, w.retries) == ([7.0, 7.0], 2)
        waits.clear()
        WStub.throttle = {"from": 1, "left": 99, "get": True}  # no Retry-After: 5 s doubling, cap 60
        with pytest.raises(self.m.PlanError) as cm:
            w.call("GET", "/api/v2/user/routines")
        assert "still rate-limited" in str(cm.value)
        assert waits == [5, 10, 20, 40, 60, 60]
        WStub.throttle = {"from": 1, "left": 1, "after": "900", "get": True}
        with pytest.raises(self.m.PlanError) as cm:
            w.call("GET", "/api/v2/user/routines")
        assert "asks to wait 15 min" in str(cm.value)
        WStub.throttle, waits[:] = None, []
        w = self.m.PlanWriter({"base": self.base}, base.TOKEN, delay=1.0, sleep=waits.append, backoff=0)
        for _ in range(3):
            w.call("GET", "/api/v2/user/routines")
        assert len(waits) == 2  # one pause before each later request
        assert all(0.5 < x <= 1.0 for x in waits), waits
        now = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=30)
        assert abs(self.m.retry_after_s(email.utils.format_datetime(now, usegmt=True)) - 30) < 3
        assert (self.m.retry_after_s("12"), self.m.retry_after_s("soon"), self.m.retry_after_s(None)) == (
            12.0,
            None,
            None,
        )
        assert base.TOKEN not in str(cm.value)


class TestPlanNext:
    """`gym plan next` (the instant /gym card): local data only, no API."""

    @classmethod
    def setup_class(cls):
        cls.m = base.load_module()

    def setup_method(self):
        self.e = base.Env(now=NOW)
        self.e.env.update(JEFIT_API_BASE="http://127.0.0.1:9")  # any API call would fail the run
        self.e.env["GYM_CALCACHE"] = "off"  # no live calendar in the card
        self.conn, self.xid = make_db(self.e.env["GYM_DB"], self.m)
        self.state = os.path.dirname(self.e.env["GYM_DB"])

    def teardown_method(self):
        self.conn.close()
        self.e.close()

    def log(self, day, names):
        add_session(self.conn, dt.date.fromisoformat(day), [(n, LOG[n][1], LOG[n][2]) for n in names], self.xid)
        self.conn.commit()

    def plan(self):
        self.e.run("plan", "draft", "--minutes", "60", "--json")
        with open(os.path.join(self.state, "plan.json")) as fh:
            return json.load(fh)

    def test_no_plan_gives_the_summary_line(self):
        out = self.e.run("plan", "next").stdout
        assert out.splitlines()[0] == "🏋️ Gym · no plan yet"
        assert re.search(
            r"^\d+ sessions in 4 wk \([\d.]+/wk\) · streak \d+ wk · last 11\.10\. \(7 d ago\)$", out.splitlines()[1]
        )

    def test_empty_log_and_no_plan(self):
        e = base.Env(now=NOW)
        e.env["GYM_CALCACHE"] = "off"
        try:
            assert e.run("plan", "next").stdout.strip() == "🏋️ Gym · no plan and no sessions logged yet"
        finally:
            e.close()

    def test_alternates_from_the_last_session(self):
        a, b = self.plan()["days"]

        def names(d):
            return [x["name"] for x in d["exercises"]]

        out = self.e.run("plan", "next").stdout  # last = full-body session: a tie
        assert out.startswith("🏋️ Next: A · "), out
        assert "Last session 11.10. (7 d ago) · streak " in out
        self.log("2026-10-20", names(a)[:4])  # partly A
        out = self.e.run("plan", "next", now="2026-10-21").stdout
        assert out.startswith("🏋️ Next: B · "), out
        assert "Last session 20.10. (A, yesterday)" in out
        assert "1  Barbell Deadlift  3×4–6 @ " in out
        assert "6b Hanging Leg Raise  2×8–12 @ bodyweight" in out
        assert "a/b = pair" in out
        assert "Plan v1 · not in Jefit yet" in out
        self.log("2026-10-22", names(b))
        out = self.e.run("plan", "next", now="2026-10-22").stdout
        assert out.startswith("🏋️ Next: A · "), out
        assert "(B, today)" in out
        assert "3a Cable Lat Pulldown (Wide Grip)  3×8–12 @ " in out
        assert len(out) < 1500

    def test_push_state_and_sync_time(self):
        self.plan()
        with open(os.path.join(self.state, "plan-pushed.json"), "w") as fh:
            json.dump({"status": "complete", "plan_version": 1, "pushed_at": "2026-10-18T10:00:00"}, fh)
        self.conn.execute("INSERT INTO imports (at, source) VALUES ('2026-10-18T16:15:00+00:00', 'api')")
        self.conn.commit()
        last = self.e.run("plan", "next").stdout.strip().splitlines()[-1]
        assert last == "Plan v1 · in Jefit · log synced 18.10. 16:15"  # OWNER_TZ=UTC in tests
        with open(os.path.join(self.state, "plan-pushed.json"), "w") as fh:
            json.dump({"status": "complete", "plan_version": 0}, fh)
        assert "Jefit has v0" in self.e.run("plan", "next").stdout

    def test_json_block_for_the_dashboard(self):
        d = json.loads(self.e.run("plan", "next", "--json").stdout)  # no plan yet
        assert d["plan"] is None
        assert d["next"] is None
        assert d["free_evenings"] is None  # GYM_CALCACHE=off
        assert d["last_session"] == {"date": "2026-10-11", "days_ago": 7, "day": None}
        assert len(d["weeks"]) == 8
        assert d["week"] == {"start": "2026-10-12", "sessions": 0, "target": None, "status": "open"}
        assert [x["lift"] for x in d["lifts"]] == ["squat", "bench", "deadlift"]
        a, _b = self.plan()["days"]
        self.log("2026-10-20", [x["name"] for x in a["exercises"]][:4])  # partly A
        d = json.loads(self.e.run("plan", "next", "--json", now="2026-10-21").stdout)
        assert d["week"] == {
            "start": "2026-10-19",
            "sessions": 1,
            "target": d["plan"]["days_per_week"],
            "status": "gym",
        }
        assert d["last_session"] == {"date": "2026-10-20", "days_ago": 1, "day": "A"}
        assert (d["next"]["key"], d["next"]["lead"]) == ("B", "Barbell Deadlift")
        assert d["streak_weeks"] > 0
        sq, bench, dl = d["lifts"]
        assert (sq["exercise"], sq["last"], sq["last_date"], sq["change"]) == (
            "Barbell Squat",
            116.7,
            "2026-10-20",
            0.0,
        )  # 100 kg × 5, same as 4 weeks ago
        assert bench["exercise"] == "Barbell Bench Press"
        assert (dl["last_date"], dl["change"]) == ("2026-10-11", 0.0)
        assert all(p[0] >= "2026-07-29" for p in sq["series"]) and sq["series"][-1] == ["2026-10-20", 116.7]
        d = json.loads(self.e.run("plan", "next", "--json", now="2026-12-01").stdout)  # nothing in the last 4 weeks
        assert [x["change"] for x in d["lifts"]] == [None, None, None]
        assert d["week"]["sessions"] == 0

    def test_plan_next_is_listed_in_usage(self):
        assert "draft | show | next | push" in self.e.run("plan", code=None).stderr


class TestTravel:
    """`gym plan travel` / `push --travel` / `return`: bodyweight trip plan in its own routine, re-entry after."""

    @classmethod
    def setup_class(cls):
        cls.m = base.load_module()
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), WStub)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def teardown_class(cls):
        cls.srv.shutdown()

    def setup_method(self):
        WStub.reset()
        self.e = base.Env(now=NOW)
        self.e.env.update(
            JEFIT_API_BASE=self.base,
            GYM_WRITE_DELAY="0",
            GYM_RETRY_BASE="0",
            GYM_CALCACHE="off",
            GYM_TOKEN_RW_FILE=os.path.join(self.e.dir, "secrets", "jefit-token-rw"),
            GYM_TRIPS=os.path.join(self.e.dir, "trips.json"),
        )
        self.conn, self.xid = make_db(self.e.env["GYM_DB"], self.m)
        self.state = os.path.dirname(self.e.env["GYM_DB"])

    def teardown_method(self):
        self.conn.close()
        self.e.close()

    def travel(self, *extra):
        return json.loads(
            self.e.run("plan", "travel", "--from", "2026-11-20", "--to", "2026-12-06", "--json", *extra).stdout
        )

    def read(self, name):
        with open(os.path.join(self.state, name)) as fh:
            return json.load(fh)

    def push_main(self):
        self.e.run("plan", "draft", "--minutes", "60", "--json")
        self.e.token()
        self.e.run("plan", "push", "--apply")

    def test_travel_draft_is_bodyweight_from_the_catalogue(self):
        self.e.run("plan", "draft", "--minutes", "60", "--json")
        main_before = self.read("plan.json")
        p = self.travel()
        catalogue = {t[1] for c in self.m.TRAVEL_EX.values() for t in c}
        exs = [x for d in p["days"] for x in d["exercises"]]
        assert (len(p["days"]), p["name"], p["kit"]) == (2, "Assistant travel", [])
        assert all(
            x["jefit_id"] in catalogue and x["weight_kg"] is None and x["equipment"] == "bodyweight" for x in exs
        )
        assert all(d["est_min"] <= 40 for d in p["days"])
        assert p["trip"] == {"name": None, "from": "2026-11-20", "to": "2026-12-06", "days": 17, "sessions": 5}
        a = {x["name"]: x for x in p["days"][0]["exercises"]}
        assert (a["Split Squat"]["for"], a["Push-Up"]["for"]) == ("Squat", "Bench Press")
        assert (a["Split Squat"]["sets"], a["Split Squat"]["lo"], a["Split Squat"]["hi"]) == (3, 8, 12)
        assert not any("Band" in x["name"] or x["name"] == "Pull-Up" for x in exs)
        for d in p["days"]:  # no exercise twice on one day
            assert len({x["name"] for x in d["exercises"]}) == len(d["exercises"])
        f = os.path.join(self.state, "plan-travel.json")
        assert stat.S_IMODE(os.stat(f).st_mode) == 0o600
        assert self.read("plan.json") == main_before  # the main plan is not touched
        out = self.e.run("plan", "show", "--travel").stdout
        assert 'Plan "Assistant travel" v1' in out
        assert "(17 days): ~5 sessions A B A B A" in out
        assert "Split Squat  3×8–12 @ bodyweight  rest 1:30  (for Squat)" in out
        assert "Jefit: not pushed yet" in out
        assert WStub.log == []
        assert self.travel()["version"] == 2
        self.e.run("plan", "travel", "--from", "2026-11-20", "--to", "2026-12-06", "--dry-run")
        assert self.read("plan-travel.json")["version"] == 2  # a dry run saves nothing

    def test_travel_kit_days_and_trip_dates(self):
        with open(self.e.env["GYM_TRIPS"], "w") as fh:
            json.dump(
                {
                    "trips": {
                        "old": {"name": "Past", "from": "2026-01-01", "to": "2026-01-10"},
                        "isl": {"name": "Island", "from": "2026-11-20", "to": "2026-12-06"},
                    }
                },
                fh,
            )
        p = json.loads(self.e.run("plan", "travel", "--days-per-week", "3", "--band", "--bar", "--json").stdout)
        names = {x["name"] for d in p["days"] for x in d["exercises"]}
        assert (len(p["days"]), p["kit"], p["trip"]["name"], p["trip"]["sessions"]) == (3, ["band", "bar"], "Island", 7)
        assert {"Pull-Up", "Band Reverse Fly", "Band Shoulder Press", "Band Alternating Bicep Curl"} <= names
        assert "Back Extension (Prone)" not in names
        assert all(x["equipment"] in ("band", "bar", "bodyweight") for d in p["days"] for x in d["exercises"])
        assert "@ band" in self.e.run("plan", "show", "--travel").stdout
        os.remove(self.e.env["GYM_TRIPS"])
        r = self.e.run("plan", "travel", code=2)
        assert "give --from and --to" in r.stderr
        assert (
            "before --from" in self.e.run("plan", "travel", "--from", "2026-12-06", "--to", "2026-11-20", code=2).stderr
        )

    def test_travel_push_dry_run_sends_nothing(self):
        assert "no travel plan yet" in self.e.run("plan", "push", "--travel", code=2).stderr
        self.travel()
        self.e.token()
        out = self.e.run("plan", "push", "--travel").stdout
        assert WStub.log == []
        assert "Dry run: nothing sent. `gym plan push --travel --apply` would send" in out
        assert '"name":"Assistant travel"' in out
        assert "[gym-travel]" in out
        assert '{"data":{"exercise":{"id":"d_000000001341"}}}' in out  # Split Squat
        assert "After --apply: In the Jefit app, tap Sync" in out
        assert 'Assistant plan"' not in out.split("After --apply")[0]

    def test_travel_push_never_touches_his_routines_or_the_main_plan(self):
        self.push_main()
        main = plan_routine()
        self.travel()
        before = copy.deepcopy(WStub.routines)
        WStub.log = []
        out = self.e.run("plan", "push", "--travel", "--apply").stdout
        assert 'Jefit routine "Assistant travel" matches the travel plan' in out
        assert 'switch back to "Assistant plan" after' in out
        assert '"Assistant travel" then shows under All Plans' in out  # Sync reminder
        tr = [r for r in WStub.routines.values() if r["name"] == "Assistant travel"]
        assert len(tr) == 1
        tr = tr[0]

        def own_ids(r):
            return {r["id"]} | {d["id"] for d in r["days"]} | {e["id"] for d in r["days"] for e in d["day_exercises"]}

        ours = own_ids(tr)
        for m, p in writes():
            assert p == "/api/v2/user/routines" or set(re.findall(r"u_\d{12}", p)) <= ours, (m, p)
        assert {k: v for k, v in WStub.routines.items() if k != tr["id"]} == before
        assert "[gym-travel]" in tr["description"]
        plan = self.read("plan-travel.json")
        assert [[e["exercise"]["id"] for e in day_exs(tr, i)] for i in range(2)] == [
            [x["jefit_id"] for x in d["exercises"]] for d in plan["days"]
        ]
        assert all(s.get("weight_lbs") is None for i in range(2) for e in day_exs(tr, i) for s in e["sets"])
        assert not any(m == "PATCH" and p.endswith(main["id"]) for m, p in writes())
        assert self.read("plan-travel-pushed.json")["status"] == "complete"
        assert self.read("plan-pushed.json")["routine_id"] == main["id"]  # main push state untouched
        # redraft with a band: only a diff inside the travel routine
        self.travel("--band")
        before, ours_before = copy.deepcopy(WStub.routines), own_ids(WStub.routines[tr["id"]])
        WStub.log = []
        self.e.run("plan", "push", "--travel", "--apply")
        assert ("POST", "/api/v2/user/routines") not in writes()
        ours = ours_before | own_ids(WStub.routines[tr["id"]])
        assert writes() and all(set(re.findall(r"u_\d{12}", p)) <= ours for _, p in writes())
        assert {k: v for k, v in WStub.routines.items() if k != tr["id"]} == {
            k: v for k, v in before.items() if k != tr["id"]
        }
        # the main plan's push leaves the travel routine alone
        travel_now = copy.deepcopy(WStub.routines[tr["id"]])
        WStub.log = []
        self.e.run("plan", "push", "--apply")
        assert writes() == []
        assert WStub.routines[tr["id"]] == travel_now

    def test_travel_push_refuses_a_hand_made_routine_of_that_name(self):
        self.push_main()
        WStub.routines["u_000000000555"] = {
            "id": "u_000000000555",
            "name": "Assistant travel",
            "description": "",
            "last_updated": 1700000000,
            "days": [],
        }
        self.travel()
        WStub.log = []
        r = self.e.run("plan", "push", "--travel", "--apply", code=1)
        assert "did not create" in r.stderr
        assert writes() == []

    def test_return_drops_weights_and_writes_only_with_apply(self):
        self.push_main()
        plan = self.read("plan.json")
        WStub.log = []
        ret = json.loads(self.e.run("plan", "return", "--from", "2026-12-07", "--json").stdout)
        assert WStub.log == []  # a draft sends nothing
        assert (ret["from"], ret["to"], ret["applied"]) == ("2026-12-07", "2026-12-20", {})
        assert self.read("plan.json") == plan
        bench = {
            w["week"]: next(x for d in w["plan"]["days"] for x in d["exercises"] if x["name"] == "Barbell Bench Press")
            for w in ret["weeks"]
        }
        pb = next(x for d in plan["days"] for x in d["exercises"] if x["name"] == "Barbell Bench Press")
        ref = 80 * (1 + 6 / 30) / (1 + (5 + 2) / 30)  # last sets 80 × 6 at the plan's 4-6 reps
        for wk, f in ((1, 0.85), (2, 0.90)):
            assert bench[wk]["weight_kg"] == min(pb["weight_kg"], (ref * f // 2.5) * 2.5)
        assert (bench[1]["sets"], bench[2]["sets"], bench[1]["reps"]) == (2, 3, pb["lo"])
        assert bench[1]["weight_kg"] < bench[2]["weight_kg"]
        assert bench[2]["weight_kg"] <= pb["weight_kg"]
        squat = next(x for d in ret["weeks"][0]["plan"]["days"] for x in d["exercises"] if x["name"] == "Barbell Squat")
        assert squat["weight_kg"] == 80.0  # 100 × 5: e1RM 116.7 → 94.6 × 0.85
        out = self.e.run("plan", "return", "--from", "2026-12-07").stdout
        assert "Draft only" in out
        assert "Barbell Bench Press  wk1 2×4 @ 65" in out
        # --apply: week 1 into "Assistant plan" only
        refs = copy.deepcopy({rid: WStub.routines[rid] for rid, _ in REFS})
        out = self.e.run("plan", "return", "--from", "2026-12-07", "--apply", "--week", "1").stdout
        assert "holds return week 1" in out
        assert "tap Sync" in out
        r = plan_routine()
        b = next(e for e in day_exs(r, 0) if e["exercise"]["id"] == jid("Barbell Bench Press"))
        assert b["count"] == 2
        assert round(abs(b["sets"][0]["weight_lbs"] - 65 / 0.45359237), 3) == 0
        assert {rid: WStub.routines[rid] for rid, _ in REFS} == refs
        assert self.read("plan.json") == plan
        assert "1" in self.read("plan-return.json")["applied"]
        # the plan push afterwards puts the plan weights back
        self.e.run("plan", "push", "--apply")
        b = next(e for e in day_exs(plan_routine(), 0) if e["exercise"]["id"] == jid("Barbell Bench Press"))
        assert b["count"] == 3
        assert round(abs(b["sets"][0]["weight_lbs"] - pb["weight_kg"] / 0.45359237), 3) == 0

    def test_return_from_defaults_to_the_day_after_the_trip_and_update_skips_it(self):
        self.push_main()
        self.travel()
        ret = json.loads(self.e.run("plan", "return", "--json").stdout)
        assert ret["from"] == "2026-12-07"
        self.e.run("plan", "return", "--apply", "--week", "1")
        for day in ("2026-12-08", "2026-12-15"):  # deliberately light sessions
            add_session(self.conn, dt.date.fromisoformat(day), [("Barbell Bench Press", 60.0, 3)], self.xid)
        self.conn.commit()
        res = json.loads(self.e.run("plan", "update", "--json", now="2026-12-21").stdout)
        assert not any("-10 %" in c for c in res["changes"]), res["changes"]
        assert res["sessions"] == 0


class TestOtherActivity:
    """Dance (shuffle dancing, rave nights) from calcache titles: persisted per event, shown next to the sessions,
    never in the streak; a week with dance and no gym is 'active'."""

    @classmethod
    def setup_class(cls):
        cls.m = base.load_module()

    def setup_method(self):
        self.e = base.Env(now=NOW)
        self.e.env.update(JEFIT_API_BASE="http://127.0.0.1:9")
        self.conn, self.xid = make_db(self.e.env["GYM_DB"], self.m)
        self.state = os.path.dirname(self.e.env["GYM_DB"])
        self.feed = os.path.join(self.e.dir, "feed.json")
        cal = os.path.join(self.e.dir, "calcache")
        with open(cal, "w") as fh:  # stub: `calcache show --days 21 --json` prints the feed
            fh.write(f'#!/bin/sh\n[ "$1" = show ] || exit 2\ncat "{self.feed}"\n')
        rr = os.path.join(self.e.dir, "rave-radar")
        with open(rr, "w") as fh:
            fh.write('#!/bin/sh\necho \'[{"name": "Nova Pulse", "aliases": ["NPX"]}]\'\n')
        for f in (cal, rr):
            os.chmod(f, 0o755)
        self.e.env.update(GYM_CALCACHE=cal, GYM_RAVE_RADAR=rr)
        self.feed_with(os.path.join(base.FIX, "calcache-dance.json"))

    def teardown_method(self):
        self.conn.close()
        self.e.close()

    def feed_with(self, path=None):
        with open(self.feed, "w") as fh:
            fh.write(open(path).read() if path else "[]")

    def nxt(self, now):
        return json.loads(self.e.run("plan", "next", "--json", now=now).stdout)

    def test_classify(self):
        evs = {x["id"]: x for x in json.load(open(os.path.join(base.FIX, "calcache-dance.json")))}

        def names():
            return ["nova pulse"]

        def k(i, a=names):
            return self.m.activity_kind(evs[i], a)

        assert k("ev-shuffle") == ("shuffle", 1.0)
        assert k("ev-artist") == ("rave", 6.0)  # artist with a typo
        assert k("ev-artist", lambda: []) is None  # no rave-radar: no match
        assert k("ev-openair") == ("rave", 12.0)  # 14 h capped at 12
        for i in ("ev-leg", "ev-drinks", "ev-dinner", "ev-fest"):  # travel leg, < 2 h, no words, all-day
            assert k(i) is None, i

    def test_persists_past_events_and_counts_finished_ones(self):
        d = self.nxt("2026-10-14T21:30")  # class still running
        assert d["other_activity"]["this_week"]["shuffle"] == {"count": 0, "hours": 0.0}
        d = self.nxt("2026-10-14")  # end of that day
        assert d["other_activity"]["this_week"] == {
            "shuffle": {"count": 1, "hours": 1.0},
            "rave": {"count": 0, "hours": 0.0},
        }
        path = os.path.join(self.state, "activity.json")
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        raw = open(path).read()
        assert "Nova" not in raw  # no titles stored
        assert json.loads(raw)["since"] == "2026-10-14"
        self.feed_with()  # all left calcache's window
        d = self.nxt("2026-10-18T12:00")
        oa = d["other_activity"]
        assert oa["this_week"] == {"shuffle": {"count": 1, "hours": 1.0}, "rave": {"count": 1, "hours": 6.0}}
        assert len(oa["weeks"]) == 8
        assert sorted(json.load(open(path))["events"]) == ["ev-artist", "ev-shuffle"]  # future one dropped
        assert d["week"]["sessions"] == 0  # dance is not gym
        assert d["week"]["status"] == "active"
        d2 = self.nxt("2026-10-20")
        assert d["streak_weeks"] > 0
        assert d2["streak_weeks"] == 0  # a dance-only week is no gym week
        st = {w["start"]: w["status"] for w in d2["weeks"]}
        assert (st["2026-10-12"], st["2026-10-19"]) == ("active", "open")
        st = {w["start"]: w["status"] for w in self.nxt("2026-10-27")["weeks"]}
        assert (st["2026-10-12"], st["2026-10-19"]) == ("active", "missed")
        assert d2["other_activity"]["total"]["rave"] == {"count": 1, "hours": 6.0}

    def test_cards_stats_review(self):
        out = self.e.run("plan", "next", now="2026-10-18").stdout
        assert "\nDance: 🕺 shuffle 1 h · 🪩 rave 6 h this week · 8 wk: shuffle 1× 1 h, rave 1× 6 h" in out
        self.e.run("plan", "draft", "--minutes", "60", "--json")
        out = self.e.run("plan", "next", now="2026-10-20").stdout.splitlines()
        assert out[2] == "Dance (8 wk): shuffle 1× 1 h, rave 1× 6 h"  # right under the last-session line
        s = json.loads(self.e.run("stats", "--json", now="2026-10-18").stdout)
        assert s["other_activity"]["this_week"]["rave"]["hours"] == 6.0
        assert (
            "Dance (not in the streak): shuffle 1× 1 h, rave 1× 6 h in 8 wk"
            in self.e.run("stats", now="2026-10-18").stdout
        )
        assert (
            "Also active: shuffle 1× 1 h, rave 1× 6 h dancing in 4 wk" in self.e.run("review", now="2026-10-18").stdout
        )
        self.e.env["GYM_CALCACHE"] = "off"  # off: the stored file still counts
        assert "Dance: " in self.e.run("plan", "next", now="2026-10-18").stdout


class TestEgressNoFile:
    """The live default token file under a throwaway HOME, removed: hub mode syncs by handle through the fake
    hub (which puts the made-up token in place of the placeholder for the read-API stub); local mode is a clear
    error before anything is sent; status says how the token goes."""

    @classmethod
    def setup_class(cls):
        cls.api = http.server.ThreadingHTTPServer(("127.0.0.1", 0), base.Stub)
        cls.hub = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeHub)
        for s in (cls.api, cls.hub):
            threading.Thread(target=s.serve_forever, daemon=True).start()

    @classmethod
    def teardown_class(cls):
        for s in (cls.api, cls.hub):
            s.shutdown()
            s.server_close()

    def setup_method(self):
        base.Stub.log, base.Stub.scheme = [], "Bearer"
        base.Stub.start_ts = int(dt.datetime(2026, 10, 12, 18, 0, tzinfo=dt.UTC).timestamp())
        FakeHub.seen = []
        self.e = base.Env(now=NOW)
        home = os.path.join(self.e.dir, "home")
        os.makedirs(os.path.join(home, ".config", "assistant"))
        os.makedirs(os.path.join(home, ".secrets"))
        with open(os.path.join(home, ".secrets", "hub-token"), "w") as fh:
            fh.write("hub-tok\n")
        self.switch = os.path.join(home, ".config", "assistant", "egress.json")
        self.e.env.update(
            HOME=home,
            GYM_TOKEN_FILE=os.path.join(home, ".secrets", "jefit-token"),
            ASSISTANT_TEST="1",
            JEFIT_API_BASE=f"http://127.0.0.1:{self.api.server_address[1]}",
            HUB_URL=f"http://127.0.0.1:{self.hub.server_address[1]}",
        )

    def teardown_method(self):
        self.e.close()

    def mode(self, m):
        with open(self.switch, "w") as fh:
            json.dump({"handles": {"gym": m}}, fh)

    def test_hub_mode_syncs_without_the_token_file(self):
        self.mode("hub")
        assert not os.path.exists(self.e.env["GYM_TOKEN_FILE"])
        p = self.e.run("sync", "--json")
        out = json.loads(p.stdout)
        assert (out["new"], out["skipped"]) == (2, 1)
        assert base.Stub.log and all(m == "GET" for m, *_ in base.Stub.log)
        assert FakeHub.seen and {(v, h) for v, h, *_ in FakeHub.seen} == {("/egress/fetch", "gym")}
        assert not any(leak for *_, leak in FakeHub.seen)
        assert base.TOKEN not in p.stdout + p.stderr
        assert "Jefit token: by handle (hub)" in self.e.run("status").stdout
        assert json.loads(self.e.run("status", "--json").stdout)["token"] == "by handle (hub)"

    def test_local_and_shadow_mode_without_the_token_file_are_a_clear_error(self):
        for m in ("local", "shadow"):
            self.mode(m)
            p = self.e.run("sync", code=3)
            assert f"core.egress gym, {m} mode" in p.stdout
            assert "hub mode needs no file" in p.stdout
            assert (base.Stub.log, FakeHub.seen) == ([], [])
            assert "Jefit token: missing" in self.e.run("status").stdout

    def test_hub_mode_with_the_file_still_goes_by_handle(self):
        self.mode("hub")
        self.e.token()
        self.e.run("sync", "--json")
        assert FakeHub.seen and all(h == "gym" for _, h, *_ in FakeHub.seen)
        assert not any(leak for *_, leak in FakeHub.seen)  # the placeholder went, never the value
