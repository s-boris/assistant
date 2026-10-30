#!/usr/bin/env python3
"""Large-batch tests for ~/bin/paperless-inbox-review: a 150-document mail-import, paging, 40 sender candidates,
batch apply with documents changed since the review, --except/--name, idempotent re-apply, skip expiry, speed.
Fixture data only: the mocked `paperless` (incl. the vault's suggest and rule test) from the main suite.
Run: python3 -I ~/opt/paperless-inbox-tests/test_paperless_inbox_large.py"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # -I drops the script dir
from test_paperless_inbox_review import SECRET, Base, doc, rule  # noqa: E402

N = 150
IDS = list(range(1000, 1000 + N))
# Speed without wall time (CI runs the suites side by side on a shared box, where a run mostly waits for a CPU): each
# mock call stands for a vault/paperless round trip, so the tests pin what each run calls, and the old wall bounds
# (10, 30, 3 s) hold as CPU seconds of the run (the tool and the mock calls it waited for), at any load.
TYPES = ("Contract", "Insurance", "Invoice")
NEW_SENDERS = 30  # senders.json entries (new correspondents)
OLD_SENDERS = 10  # existing correspondents (5 with their own rule, 5 matched by name)


def big_fixture():
    imp = ["Inbox", "mail-import"]
    docs = []
    for k, i in enumerate(IDS):
        # 10 documents share each "added" day: paging must break the ties by id, never drop or repeat one
        docs.append(doc(i, f"Mail attachment {k}", imp, TYPES[k % 3], f"2026-10-{1 + k // 10:02d}"))
    docs.append(doc(1, "Scan 1", ["Inbox"]))  # a normal inbox doc: not in the import
    docs.append(doc(2, "Filed", ["Contract"], "Contract"))  # filed: not a candidate
    corrs = [
        rule(300 + j, f"Old Corp {j:02d}", alg="any" if j < 5 else "none", match=f"oldcorp{j:02d}" if j < 5 else "")
        for j in range(OLD_SENDERS)
    ]
    fx = {
        "docs": docs,
        "rules": {
            "tags": [rule(10, "Inbox", inbox=True), rule(16, "mail-import"), rule(17, "Housing")],
            "document_types": [rule(20 + j, t) for j, t in enumerate(TYPES)],
            "correspondents": corrs,
        },
        "suggest": {},
        "rule_test": {},
    }
    for k, i in enumerate(IDS):
        typ = TYPES[k % 3]
        if k % 10 == 9:
            typ = TYPES[(k + 1) % 3]  # the vault disagrees: review batch
        fx["suggest"][str(i)] = {"document_type": typ, "correspondent": None, "tags": ["Housing"] if k % 7 == 0 else []}
    # sender hits: new sender s gets docs 1000+3s .. 1000+3s+2 (s < 30 -> docs 1000-1089);
    # old corp j gets docs 1090+3j .. +2 (j < 10 -> docs 1090-1119); docs 1120-1149 have no known sender
    for s in range(NEW_SENDERS):
        fx["rule_test"][f"sender{s:02d}"] = [1000 + 3 * s + x for x in range(3)]
    for j in range(OLD_SENDERS):
        key = f"oldcorp{j:02d}" if j < 5 else f"Old Corp {j:02d}"
        fx["rule_test"][key] = [1090 + 3 * j + x for x in range(3)]
    fx["rule_test"]["sender07"].append(1095)  # also hit by Old Corp 01: ambiguous
    fx["rule_test"]["sender29"].append(1)  # outside the selection: ignored
    fx["rule_test"]["sender28"] += [999999]  # an id paperless does not have
    fx["rule_test"]["unknown-new"] = []  # a new sender with no documents yet
    return fx


def senders_file():
    out = [
        {"name": f"Sender {s:02d}", "any": f"sender{s:02d}", **({"guess": True} if s % 4 == 0 else {})}
        for s in range(NEW_SENDERS)
    ]
    out.append({"name": "Unknown New", "any": "unknown-new"})
    out.append({"name": "Bad", "regex": ""})  # no usable rule: dropped
    out[5]["types"] = ["Invoice"]  # only its Invoice docs count
    return {"senders": out}


class Large(Base):
    def setUp(self):
        super().setUp()
        self.fx_data = big_fixture()
        self.write_fx(self.fx_data)
        self.senders = os.path.join(self.d, "senders.json")
        with open(self.senders, "w") as f:
            json.dump(senders_file(), f)
        self.env["PINB_SENDERS"] = self.senders

    def rj(self, *args, env=None):
        r = self.run_tool("--json", *args, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn(SECRET, r.stdout + r.stderr)
        return json.loads(r.stdout)

    def sets(self):
        return [c for c in self.calls() if c[0] == "set"]

    def calls(self):
        return super().calls() if os.path.exists(self.log) else []

    def at(self, day):
        return dict(self.env, PINB_NOW=day)

    def drop_inbox(self, ids):
        """what paperless looks like after these were applied: Inbox gone"""
        for d in self.fx_data["docs"]:
            if d["id"] in ids:
                d["tags"] = [t for t in d["tags"] if t != "Inbox"]
        self.write_fx(self.fx_data)

    # ------------------------------------------------------------------------------------------ paging
    def test_paging_150_no_doc_twice_none_skipped(self):
        seen = []
        for off in range(0, N + 20, 20):
            n = len(self.calls())
            r = self.rj("--tag", "mail-import", "-n", "20", "--offset", str(off))
            self.assertEqual((r["total"], r["offset"], r["held"]), (N, off, 0))
            self.assertEqual(r["count"], max(0, min(20, N - off)))
            page = [d["id"] for d in r["docs"]]
            seen += page
            # a page costs one list read and one vault call per doc on it, whatever the import's size
            run = self.calls()[n:]
            self.assertEqual(sorted(c[0] for c in run if c[0] != "suggest"), ["recent", "rules"], off)
            self.assertEqual(sorted(int(c[1]) for c in run if c[0] == "suggest"), sorted(page), off)
        self.assertEqual(len(seen), len(set(seen)))  # no doc twice
        self.assertEqual(sorted(seen), IDS)  # none skipped, nothing outside the tag
        self.assertEqual(seen[:3], [1149, 1148, 1147])  # newest first, ties by id
        sug = [c[1] for c in self.calls() if c[0] == "suggest"]
        self.assertEqual(sorted(int(i) for i in sug), IDS)  # one vault call per doc, never repeated
        self.rj("--tag", "mail-import", "-n", "20")  # warm-up run again: all cached
        self.assertEqual(len([c for c in self.calls() if c[0] == "suggest"]), N)
        self.assertFalse(self.sets())
        self.assertFalse([c for c in self.calls() if c[0] in ("get", "fields")])

    def test_warmup_run_then_next_page_after_apply(self):
        """the 01:00 shape: -n 20 without --offset; applied docs leave the inbox, the next run takes the next 20"""
        seen = []
        for _ in range(8):
            r = self.rj("--tag", "mail-import", "-n", "20", "--no-llm")
            ids = [d["id"] for d in r["docs"]]
            if not ids:
                break
            self.assertFalse(set(ids) & set(seen))
            seen += ids
            self.drop_inbox(set(ids))
        self.assertEqual(sorted(seen), IDS)

    def test_default_run_holds_the_import(self):
        r = self.rj()
        self.assertEqual(([d["id"] for d in r["docs"]], r["held"]), ([1], N))
        self.assertFalse([c for c in self.calls() if c[0] == "suggest" and int(c[1]) >= 1000])

    # ------------------------------------------------------------------------------------------ senders
    def test_40_senders(self):
        r = self.rj("--tag", "mail-import", "--senders", "--no-llm", "-n", "0")
        self.assertLess(self.cpu, 10)
        self.assertEqual(sorted(c[0] for c in self.calls() if c[0] != "rule"), ["recent", "rules"])  # no vault call
        tests = [c for c in self.calls() if c[0] == "rule"]
        self.assertFalse([c for c in tests if c[1] != "test"])  # read-only
        self.assertEqual(len(tests), NEW_SENDERS + 1 + OLD_SENDERS)  # 30 new + unknown + 10 existing; "Bad" dropped
        self.assertIn(["rule", "test", "correspondent", "--literal", "Old Corp 07", "--json"], tests)
        self.assertIn(["rule", "test", "correspondent", "--any", "oldcorp01", "--json"], tests)
        corr = {b["corr"]["name"]: b for b in r["batches"] if b["kind"] == "corr"}
        self.assertNotIn("Unknown New", corr)  # no documents: no proposal
        self.assertNotIn("Bad", corr)
        self.assertEqual(r["ambiguous"], 1)  # #1095: Sender 07 and Old Corp 01
        self.assertNotIn("1095", corr["Old Corp 01"]["ids"])
        self.assertNotIn("1095", corr["Sender 07"]["ids"])
        self.assertEqual(sorted(corr["Sender 05"]["ids"]), ["1017"])  # types filter: only its Invoice doc
        self.assertIn("name guessed", corr["Sender 04"]["label"])
        self.assertIn("also matches 1 other", corr["Sender 29"]["label"])  # #1 is outside the selection
        self.assertTrue(corr["Sender 00"]["corr"]["new"])
        self.assertEqual(corr["Sender 00"]["corr"]["rule"], {"alg": "any", "match": "sender00"})
        self.assertFalse(corr["Old Corp 07"]["corr"]["new"])
        self.assertIsNone(corr["Old Corp 07"]["corr"]["rule"])
        self.assertIn("correspondent Old Corp 03: 3 doc(s) without one", corr["Old Corp 03"]["label"])
        self.assertEqual(len(corr), NEW_SENDERS + OLD_SENDERS)
        grouped = [int(i) for b in corr.values() for i in b["ids"]]
        self.assertEqual(len(grouped), len(set(grouped)))  # a doc in one sender group at most
        self.assertFalse({i for i in grouped} & set(range(1120, 1150)))  # unknown senders stay ungrouped
        self.assertNotIn(999999, grouped)
        # the review rows of grouped docs skip the other batches; the rest still gets clear/review batches
        r = self.rj("--tag", "mail-import", "--senders", "--no-llm", "-n", "150")
        other = {int(i) for b in r["batches"] if b["kind"] != "corr" for i in b["ids"]}
        self.assertFalse(other & set(grouped))

    def test_sender_groups_ignore_skipped_docs_and_muted_senders(self):
        r = self.rj("--tag", "mail-import", "--senders", "--no-llm", "-n", "0")
        tok = next(b["token"] for b in r["batches"] if b["kind"] == "corr" and b["corr"]["name"] == "Sender 03")
        self.assertEqual(self.run_tool("skip", "--batch", tok).returncode, 0)
        self.assertEqual(self.run_tool("skip", "1000").returncode, 0)
        r = self.rj("--tag", "mail-import", "--senders", "--no-llm", "-n", "0")
        corr = {b["corr"]["name"]: sorted(b["ids"]) for b in r["batches"] if b["kind"] == "corr"}
        self.assertNotIn("Sender 03", corr)
        self.assertEqual(corr["Sender 00"], ["1001", "1002"])

    # ------------------------------------------------------------------------------------------ batch apply
    def review_all(self):
        return self.rj("--tag", "mail-import", "-n", "150")

    def test_apply_refuses_5_changed_they_come_back(self):
        r = self.review_all()
        b = max((b for b in r["batches"] if b["kind"] == "clear"), key=lambda b: len(b["ids"]))
        tok, ids = b["token"], sorted(int(i) for i in b["ids"])
        self.assertGreater(len(ids), 30)
        changed = ids[:5]
        for d in self.fx_data["docs"]:
            if d["id"] in changed:
                d["title"] += " (renamed)"
        self.write_fx(self.fx_data)
        n = len(self.calls())
        out = self.run_tool("apply", "--batch", tok, "--confirm", f"pinb:all:{tok}")
        self.assertLess(self.cpu, 30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual([c[0] for c in self.calls()[n:] if c[0] != "set"], ["recent"])  # one read finds the 5 changed
        self.assertIn(f"applied {len(ids) - 5} of {len(ids)}", out.stdout)
        self.assertIn("refused 5 changed since the review", out.stdout)
        self.assertEqual(sorted(int(c[1]) for c in self.sets()), ids[5:])
        self.drop_inbox(set(ids[5:]))
        n_sug = len([c for c in self.calls() if c[0] == "suggest"])
        r2 = self.review_all()
        back = sorted(d["id"] for d in r2["docs"] if d["id"] in ids)
        self.assertEqual(back, changed)  # they come back in the next run
        self.assertEqual(len([c for c in self.calls() if c[0] == "suggest"]), n_sug)  # stored vault answers
        nb = [b for b in r2["batches"] if set(int(i) for i in b["ids"]) & set(changed)]
        self.assertTrue(nb)
        self.assertNotIn(tok, [b["token"] for b in r2["batches"]])
        for b2 in nb:
            t = b2["token"]
            self.assertEqual(self.run_tool("apply", "--batch", t, "--confirm", f"pinb:all:{t}").returncode, 0)
        self.assertEqual(sorted(int(c[1]) for c in self.sets() if int(c[1]) in changed), changed)

    def test_except_ranges_and_idempotent_reapply(self):
        r = self.review_all()
        b = max((b for b in r["batches"] if b["kind"] == "clear"), key=lambda b: len(b["ids"]))
        tok, ids = b["token"], sorted(int(i) for i in b["ids"])
        ex = ids[:3] + ids[10:12]
        spec = f"{ids[0]}-{ids[2]},#{ids[10]}, {ids[11]},999998"
        out = self.run_tool("apply", "--batch", tok, "--confirm", f"pinb:all:{tok}", "--except", spec)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("left out", out.stdout)
        self.assertNotIn("999998", out.stdout)  # ids outside the batch are ignored
        first = sorted(int(c[1]) for c in self.sets())
        self.assertEqual(first, [i for i in ids if i not in ex])
        for _ in range(2):  # second and third tap: no writes
            out = self.run_tool("apply", "--batch", tok, "--confirm", f"pinb:all:{tok}", "--except", spec)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertIn("nothing left to apply", out.stdout)
        self.assertEqual(len(self.sets()), len(first))
        out = self.run_tool("apply", "--batch", tok, "--confirm", f"pinb:all:{tok}")  # now without --except
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(sorted(int(c[1]) for c in self.sets()[len(first) :]), sorted(ex))
        self.assertIn("applied 5 of 5", out.stdout)
        out = self.run_tool("apply", "--batch", tok, "--confirm", f"pinb:all:{tok}")
        self.assertIn("nothing left", out.stdout)
        self.assertEqual(len(self.sets()), len(ids))  # every doc written exactly once
        self.review_all()  # same review again (mock unchanged)
        out = self.run_tool("apply", "--batch", tok, "--confirm", f"pinb:all:{tok}")
        self.assertIn("nothing left", out.stdout)  # the stored applied list survives a review
        self.assertEqual(len(self.sets()), len(ids))
        for bad in ("", "abc", "5-3", "1-99999"):
            self.assertEqual(
                self.run_tool("apply", "--batch", tok, "--confirm", f"pinb:all:{tok}", "--except", bad).returncode,
                2,
                bad,
            )

    def test_name_override(self):
        r = self.rj("--tag", "mail-import", "--senders", "--no-llm", "-n", "0")
        corr = {b["corr"]["name"]: b["token"] for b in r["batches"] if b["kind"] == "corr"}
        clear = next(b["token"] for b in self.review_all()["batches"] if b["kind"] == "clear")
        self.assertEqual(
            self.run_tool("apply", "--batch", clear, "--confirm", f"pinb:all:{clear}", "--name", "X").returncode, 2
        )  # --name: corr batches only
        self.assertEqual(self.run_tool("apply", "1000", "--confirm", "pinb:ok:1000", "--name", "X").returncode, 2)
        t = corr["Sender 01"]
        for bad in ("unknown", "   ", "a;rm -rf x|y", "x" * 60):
            self.assertEqual(
                self.run_tool("apply", "--batch", t, "--confirm", f"pinb:all:{t}", "--name", bad).returncode, 2, bad
            )
        self.assertFalse(self.sets())
        out = self.run_tool("apply", "--batch", t, "--confirm", f"pinb:all:{t}", "--name", "  Sender One  AG ")
        self.assertEqual(out.returncode, 0, out.stderr)
        writes = [c for c in self.calls() if c[0] == "set" or c[:2] == ["rule", "set"]]
        self.assertEqual(
            writes[0], ["rule", "set", "correspondent", "Sender One AG", "--any", "sender01", "--create", "--json"]
        )
        self.assertEqual([c[3] for c in writes[1:]], ["Sender One AG"] * 3)
        # --name naming an EXISTING correspondent (other case): no new rule, its exact name is used
        t = corr["Sender 02"]
        n0 = len(writes)
        out = self.run_tool("apply", "--batch", t, "--confirm", f"pinb:all:{t}", "--name", "old corp 03")
        self.assertEqual(out.returncode, 0, out.stderr)
        writes = [c for c in self.calls() if c[0] == "set" or c[:2] == ["rule", "set"]][n0:]
        self.assertFalse([c for c in writes if c[0] == "rule"])
        self.assertEqual({c[3] for c in writes}, {"Old Corp 03"})

    # ------------------------------------------------------------------------------------------ skip
    def test_skip_4_weeks_then_reappear(self):
        r = self.review_all()
        b = next(b for b in r["batches"] if b["kind"] == "review")
        tok, ids = b["token"], sorted(int(i) for i in b["ids"])
        self.assertEqual(self.run_tool("skip", "--batch", tok).returncode, 0)
        self.assertEqual(self.run_tool("skip", "1149").returncode, 0)
        gone = set(ids) | {1149}
        for day, hidden in (
            ("2026-10-18", True),
            ("2026-11-01", True),
            ("2026-11-15", True),
            ("2026-11-16", False),
            ("2026-12-20", False),
        ):
            r = self.rj("--tag", "mail-import", "-n", "150", "--no-llm", env=self.at(day))
            shown = {d["id"] for d in r["docs"]}
            self.assertEqual(not (gone & shown), hidden, day)
            self.assertEqual(r["total"], N - (len(gone) if hidden else 0), day)
            self.assertEqual(r["skipped"], len(gone) if hidden else 0, day)
            if hidden:  # --all still shows them
                a = self.rj("--tag", "mail-import", "-n", "150", "--no-llm", "--all", env=self.at(day))
                self.assertTrue(gone <= {d["id"] for d in a["docs"]})
        # the skip file is pruned once expired
        with open(os.path.join(self.d, "state", "skips.json")) as f:
            before = json.load(f)
        self.assertEqual(self.run_tool("skip", "1000", env=self.at("2026-12-20")).returncode, 0)
        with open(os.path.join(self.d, "state", "skips.json")) as f:
            after = json.load(f)
        self.assertTrue(len(before) > 1)
        self.assertEqual(list(after), ["1000"])

    # ------------------------------------------------------------------------------------------ speed
    def test_performance(self):
        def run_calls(*args):
            """one run: the list read once, no vault call, under 3 CPU s -> its stdout"""
            n = len(self.calls())
            out = self.run_tool(*args)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(sorted(c[0] for c in self.calls()[n:]), ["recent", "rules"], args)
            self.assertLess(self.cpu, 3, args)
            return out.stdout

        run_calls("--json", "--tag", "mail-import", "-n", "150", "--no-llm")
        self.review_all()  # cold: 150 mocked vault calls
        r = json.loads(run_calls("--json", "--tag", "mail-import", "-n", "150"))  # warm cache: no vault calls
        self.assertEqual(len([c for c in self.calls() if c[0] == "suggest"]), N)
        run_calls("--plain", "--tag", "mail-import", "-n", "150")
        self.assertEqual(sum(len(b["ids"]) for b in r["batches"]) + len(r["pending"]) + len(r["no_suggestion"]), N)
        rev = [b for b in r["batches"] if b["kind"] == "review"]
        self.assertTrue(all(len(b["ids"]) <= 10 for b in rev))
        self.assertEqual(len({b["token"] for b in r["batches"]}), len(r["batches"]))


if __name__ == "__main__":
    unittest.main(verbosity=1)
