#!/usr/bin/env python3
"""Tests for ~/bin/paperless-inbox-review with a mocked `paperless` incl. the vault's `suggest` (no vault, no writes).
Run: python3 -I ~/opt/paperless-inbox-tests/test_paperless_inbox_review.py"""

import json
import os
import resource
import shutil
import subprocess
import tempfile
import textwrap
import unittest

TOOL = os.environ.get("PINB_BIN") or os.path.expanduser("~/bin/paperless-inbox-review")  # PINB_BIN: test a copy
SECRET = "ZZ-CONTENT-MARKER-77"  # must never show up in any output

# The mocked `paperless`: bash answering from the files write_fx() renders (MOCK_DIR), not a python per call: a
# 150-document review is 150 calls, and a python start each (~1300 in the large suite) cut the suite off at its 240 s
# limit in the hub CI runner, where the suites share the CPUs. Its argv log: one line per call, fields ended by \x1f.
MOCK_PL = textwrap.dedent(r"""
    #!/bin/bash
    R=$MOCK_DIR
    { printf '%s\037' "$@"; echo; } >> "$MOCK_LOG"
    cmd=$1
    if [ -e "$R/fail/$cmd" ]; then echo "vault unreachable" >&2; exit 1; fi
    case $cmd in
      recent|rules) exec cat "$R/$cmd" ;;
      suggest)  # the vault: existing names only, never content
        if [ -n "$MOCK_SUGGEST_FAIL" ]; then echo "vault: local model busy" >&2; exit 1; fi
        [ -f "$R/suggest/$2" ] && exec cat "$R/suggest/$2"
        printf '{"id": %d, "current": {}, "suggested": {"document_type": null, ' "$2"
        echo '"correspondent": null, "tags": []}, "changes": {}, "_warnings": []}' ;;
      get) echo "get must not be called (plan B: no content)" >&2; exit 9 ;;
      rule)
        case $2 in
          test)  # ids + current values only, never titles or content
            f="$R/rule_test/$5"; [ -n "$5" ] && [ -f "$f" ] || f="$R/rule_test_none"
            IFS= read -r line < "$f"
            printf '%s\n' "${line/@ALG@/${4#--}}" ;;
          set)
            n=${4//\\/\\\\}; n=${n//\"/\\\"}
            printf '{"action": "created", "kind": "correspondents", "name": "%s"}\n' "$n" ;;
          *) exit 2 ;;
        esac ;;
      set)
        printf '{"id": %d, "title": "x", "document_type": "Rechnung", ' "$2"
        echo '"correspondent": null, "tags": ["Rechnung"], "changed": ["document_type", "tags"]}' ;;
      *) exit 2 ;;
    esac
""").lstrip()


def render_mock(fx, out):
    """fx (the fixture) -> the mock's answers in the directory out, as the python mock printed them from fx."""
    shutil.rmtree(out, ignore_errors=True)
    for sub in ("suggest", "rule_test", "fail"):
        os.makedirs(os.path.join(out, sub))

    def put(rel, obj):
        with open(os.path.join(out, rel), "w") as f:
            f.write(json.dumps(obj) + "\n")

    put("recent", {"results": fx["docs"]})
    put("rules", fx["rules"])
    for i, sug in fx["suggest"].items():
        put(os.path.join("suggest", str(int(i))), {"id": int(i), "current": {}, "suggested": sug, "changes": {},
                                                    "_warnings": []})  # fmt: skip

    def rule_test(ids):
        return {"kind": "correspondents", "algorithm": "@ALG@", "is_insensitive": True, "checked": len(fx["docs"]),
                "count": len(ids), "matches": [{"id": i, "current": None} for i in ids]}  # fmt: skip

    put("rule_test_none", rule_test([]))
    for key, ids in fx.get("rule_test", {}).items():
        assert key and "/" not in key and key not in (".", ".."), key  # the key is the file name
        put(os.path.join("rule_test", key), rule_test(ids))
    for cmd in fx.get("fail", []):
        open(os.path.join(out, "fail", cmd), "w").close()


def doc(i, title, tags=(), dtype=None, added="2026-10-10"):
    return {
        "id": i,
        "created": "2026-10-01",
        "added": added,
        "title": title,
        "correspondent": None,
        "document_type": dtype,
        "tags": list(tags),
        "url": f"http://x/documents/{i}/details",
    }


def rule(i, name, inbox=False, alg="any", match=""):
    return {
        "id": i,
        "name": name,
        "document_count": 1,
        "algorithm": alg,
        "match": match,
        "is_insensitive": True,
        "is_inbox_tag": inbox,
    }


def fixture():
    return {
        "docs": [
            doc(1, "Lohnabrechnung Foxtrail 2026-08", ["Lohnabrechnung", "Foxtrail"], "Lohnabrechnung", "2026-09-01"),
            doc(2, "Scan 2026-10-09", ["Inbox"]),  # Swisscom bill (LLM), content mentions Foxtrail? no
            doc(3, "Praktikumszeugnis"),  # untagged, title alias -> existing type
            doc(4, "Brief privat", ["Inbox", "private"]),  # private: never fetched
            doc(5, "Arbeitsvertrag Explorial", ["Arbeitsvertrag"], "Arbeitsvertrag"),  # filed, not a candidate
            doc(6, "Lohn Sept", ["Inbox"], added="2026-10-11"),  # LLM -> existing type + employer tag
        ],
        "rules": {
            "tags": [
                rule(10, "Inbox", inbox=True),
                rule(11, "Lohnabrechnung"),
                rule(12, "Foxtrail"),
                rule(13, "Arbeitszeugnis"),
                rule(14, "Arbeitsvertrag"),
                rule(15, "private"),
            ],
            "document_types": [
                rule(20, "Lohnabrechnung"),
                rule(21, "Arbeitszeugnis"),
                rule(22, "Arbeitsvertrag"),
                rule(23, "Rechnung"),
            ],
            "correspondents": [rule(30, "Swisscom"), rule(31, "Foxtrail AG")],
        },
        # what the vault's suggest answers (it read the content inside the vault; only names come out)
        "suggest": {
            "2": {"document_type": "Rechnung", "correspondent": "Swisscom", "tags": ["Telekom"]},
            "4": {"document_type": "Rechnung", "correspondent": None, "tags": [SECRET]},
            "6": {"document_type": "Lohnabrechnung", "correspondent": "Foxtrail AG", "tags": []},
        },
    }


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        for name, body in (("paperless", MOCK_PL),):
            p = os.path.join(self.d, name)
            with open(p, "w") as f:
                f.write(body)
            os.chmod(p, 0o755)
        self.fx = os.path.join(self.d, "fx")
        self.write_fx(fixture())
        self.log = os.path.join(self.d, "log")
        self.inbox = os.path.join(self.d, "inbox")
        self.env = dict(
            os.environ,
            PINB_PAPERLESS=os.path.join(self.d, "paperless"),
            PINB_STATE_DIR=os.path.join(self.d, "state"),
            PINB_INBOX=self.inbox,
            PINB_NOW="2026-10-18",
            MOCK_DIR=self.fx,
            MOCK_LOG=self.log,
        )
        self.cpu = 0.0

    def tearDown(self):
        shutil.rmtree(self.d)

    def write_fx(self, fx):
        render_mock(fx, self.fx)

    def run_tool(self, *args, env=None):
        """-> the CompletedProcess; self.cpu = the run's CPU seconds (the tool and the mock calls it waited for)"""
        r0 = resource.getrusage(resource.RUSAGE_CHILDREN)
        r = subprocess.run([TOOL, *args], capture_output=True, text=True, env=env or self.env, timeout=60)
        r1 = resource.getrusage(resource.RUSAGE_CHILDREN)
        self.cpu = (r1.ru_utime + r1.ru_stime) - (r0.ru_utime + r0.ru_stime)
        return r

    def calls(self):
        with open(self.log) as f:
            return [ln.split("\x1f")[:-1] for ln in f.read().split("\n") if ln]

    def review(self):
        r = self.run_tool("--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        return {d["id"]: d for d in json.loads(r.stdout)["docs"]}, r


class T(Base):
    def test_candidates_and_suggestions(self):
        docs, r = self.review()
        self.assertEqual(list(docs), [6, 4, 3, 2])  # inbox-tagged + untagged, newest first (then id)
        self.assertEqual(docs[3]["suggest"]["document_type"], {"name": "Arbeitszeugnis", "new": False})
        self.assertEqual(docs[3]["suggest"]["confidence"], "high")
        self.assertEqual([t["name"] for t in docs[3]["suggest"]["tags"]], ["Arbeitszeugnis"])
        s2 = docs[2]["suggest"]
        self.assertEqual(s2["document_type"], {"name": "Rechnung", "new": False})  # the vault: existing names
        self.assertEqual(s2["correspondent"], {"name": "Swisscom", "new": False})
        self.assertIn("vault", s2["basis"])
        self.assertEqual(s2["remove_tags"], ["Inbox"])
        self.assertNotIn("Telekom", [t["name"] for t in s2["tags"]])  # LLM may not invent tags
        s6 = docs[6]["suggest"]
        self.assertEqual(s6["document_type"]["name"], "Lohnabrechnung")
        self.assertIn("Foxtrail", [t["name"] for t in s6["tags"]])  # employer tag from the correspondent
        self.assertNotIn(SECRET, r.stdout + r.stderr)
        self.assertFalse([c for c in self.calls() if c[0] == "get"])  # plan B: never a content/card read
        self.assertIn(["suggest", "2", "--json"], self.calls())

    def test_private_never_fetched(self):
        docs, _ = self.review()
        self.assertFalse([c for c in self.calls() if c[0] in ("get", "suggest") and c[1] == "4"])
        self.assertIn("private", docs[4]["suggest"]["basis"])
        self.assertIsNone(docs[4]["suggest"]["document_type"])
        n = len(self.calls())
        self.review()  # private: complete, cached
        self.assertFalse([c for c in self.calls()[n:] if c[0] == "suggest"])

    def test_plain_and_limit(self):
        r = self.run_tool("--plain", "-n", "2")
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.strip().splitlines()
        self.assertEqual(lines[0], "paperless inbox: 2 to review (of 4)")
        self.assertTrue(lines[1].startswith('#6 2026-10-01 "Lohn Sept" [inbox] → type Lohnabrechnung'))
        r = self.run_tool("--plain", "-n", "2", "--offset", "2")
        self.assertEqual(r.stdout.splitlines()[0], "paperless inbox: 2 to review (of 4, from 3)")
        self.assertEqual([x.split()[0] for x in r.stdout.splitlines()[1:3]], ["#3", "#2"])
        self.assertNotIn(SECRET, r.stdout)

    def test_cron_note_only_when_nonempty(self):
        r = self.run_tool("--cron")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(os.listdir(self.inbox), ["paperless-inbox-review.md"])
        note = open(os.path.join(self.inbox, "paperless-inbox-review.md")).read()
        self.assertIn("pinb:all:<token>", note)
        self.assertRegex(note, r"## [0-9a-f]{8} · review")
        self.assertIn('"Praktikumszeugnis"', note)
        self.assertNotIn(SECRET, note)
        shutil.rmtree(self.inbox)
        fx = fixture()
        fx["docs"] = [d for d in fx["docs"] if d["id"] in (1, 5)]
        self.write_fx(fx)
        r = self.run_tool("--cron")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(self.inbox))

    def test_cron_failure_writes_note(self):
        fx = fixture()
        fx["fail"] = ["recent"]
        self.write_fx(fx)
        r = self.run_tool("--cron")
        self.assertEqual(r.returncode, 1)
        self.assertIn("failed", open(os.path.join(self.inbox, "paperless-inbox-review.md")).read())

    def test_skip(self):
        r = self.run_tool("skip", "3")
        self.assertEqual(r.returncode, 0, r.stderr)
        docs, _ = self.review()
        self.assertNotIn(3, docs)
        self.assertIn(3, [d["id"] for d in json.loads(self.run_tool("--json", "--all").stdout)["docs"]])
        self.assertIn("1 skipped", self.run_tool("--plain").stdout)

    def test_apply_gate_and_write(self):
        self.review()
        for args in (["apply", "2"], ["apply", "2", "--confirm", "pinb:ok:3"], ["apply", "2", "--confirm", "yes"]):
            r = self.run_tool(*args)
            self.assertEqual(r.returncode, 2, args)
        self.assertFalse(any(c[0] == "set" for c in self.calls()))
        r = self.run_tool("apply", "2", "--confirm", "pinb:ok:2")
        self.assertEqual(r.returncode, 0, r.stderr)
        sets = [c for c in self.calls() if c[0] == "set"]
        self.assertEqual(
            sets, [["set", "2", "--type", "Rechnung", "--correspondent", "Swisscom", "--tag=-Inbox", "--json"]]
        )  # existing names: no --create
        r = self.run_tool("apply", "2", "--confirm", "pinb:ok:2")  # suggestion consumed
        self.assertEqual(r.returncode, 1)

    def test_apply_refuses_stale(self):
        self.review()
        fx = fixture()
        fx["docs"][1]["title"] = "Rechnung Swisscom 2026-10"  # someone filed it meanwhile
        self.write_fx(fx)
        r = self.run_tool("apply", "2", "--confirm", "pinb:ok:2")
        self.assertEqual(r.returncode, 1)
        self.assertIn("changed since the review", r.stderr)
        self.assertFalse(any(c[0] == "set" for c in self.calls()))

    def test_suggest_failure_falls_back_and_retries(self):
        env = dict(self.env, MOCK_SUGGEST_FAIL="1")
        r = self.run_tool("--json", env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        docs = {d["id"]: d for d in json.loads(r.stdout)["docs"]}
        self.assertEqual(docs[3]["suggest"]["document_type"]["name"], "Arbeitszeugnis")  # title heuristics work
        self.assertIsNone(docs[2]["suggest"]["document_type"])  # no content alias any more (plan B)
        n = sum(1 for c in self.calls() if c[0] == "suggest")
        self.review()  # incomplete -> recomputed by the vault
        self.assertGreater(sum(1 for c in self.calls() if c[0] == "suggest"), n)
        n = sum(1 for c in self.calls() if c[0] == "suggest")
        self.review()  # complete -> cached, no new suggest
        self.assertEqual(sum(1 for c in self.calls() if c[0] == "suggest"), n)

    def test_no_llm_skips_suggest(self):
        r = self.run_tool("--json", "--no-llm")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse([c for c in self.calls() if c[0] == "suggest"])


def import_fixture():
    """the default fixture plus a held bulk import: ids 40-45, tag mail-import, type set by the import"""
    fx = fixture()
    fx["rules"]["tags"].append(rule(16, "mail-import"))
    fx["rules"]["tags"].append(rule(17, "Housing"))
    fx["rules"]["document_types"] += [rule(24, "Contract"), rule(25, "Insurance")]
    imp = ["Inbox", "mail-import"]
    fx["docs"] += [
        doc(40, "Mail attachment 1", imp, "Contract", "2026-10-17"),
        doc(41, "Mail attachment 2", imp, "Contract", "2026-10-17"),
        doc(42, "Mail attachment 3", imp, "Insurance", "2026-10-17"),
        doc(43, "Mail attachment 4", imp, "Insurance", "2026-10-17"),
        doc(44, "Mail attachment 5", imp, "Insurance", "2026-10-17"),
        doc(45, "Mail attachment 6", imp, "Contract", "2026-10-17"),
    ]
    fx["suggest"].update(
        {
            "40": {"document_type": "Contract", "correspondent": None, "tags": []},  # confirmed
            "41": {"document_type": "Contract", "correspondent": None, "tags": ["Housing"]},  # confirmed + a tag
            "42": {"document_type": "Insurance", "correspondent": None, "tags": []},
            "43": {"document_type": "Insurance", "correspondent": None, "tags": []},
            "44": {"document_type": "Insurance", "correspondent": None, "tags": []},
            "45": {"document_type": "Insurance", "correspondent": None, "tags": []},  # disagrees: review
        }
    )
    # rule test hits per match text (ids only, as the vault answers)
    fx["rule_test"] = {"insurera": [42, 43, 2], "insurerb": [44, 43], "Swisscom": []}
    return fx


class Batches(Base):
    def setUp(self):
        super().setUp()
        self.write_fx(import_fixture())
        self.senders = os.path.join(self.d, "senders.json")
        with open(self.senders, "w") as f:
            json.dump(
                {
                    "senders": [
                        {"name": "Insurer A", "any": "insurera", "types": ["Insurance"], "guess": True},
                        {"name": "Insurer B", "any": "insurerb"},
                    ]
                },
                f,
            )
        self.env["PINB_SENDERS"] = self.senders

    def rj(self, *args):
        r = self.run_tool("--json", *args)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn(SECRET, r.stdout + r.stderr)
        return json.loads(r.stdout)

    def batch(self, r, kind, label=""):
        return next(b for b in r["batches"] if b["kind"] == kind and label in b["label"])

    def test_hold_by_default(self):
        r = self.rj()
        self.assertEqual([d["id"] for d in r["docs"]], [6, 4, 3, 2])
        self.assertEqual(r["held"], 6)
        r = self.run_tool("--cron")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("6 held", r.stdout)
        note = open(os.path.join(self.inbox, "paperless-inbox-review.md")).read()
        self.assertNotIn("Mail attachment", note)
        self.assertFalse([c for c in self.calls() if c[0] == "suggest" and int(c[1]) >= 40])
        self.assertFalse([c for c in self.calls() if c[0] == "rule"])  # no sender lookups unless --senders

    def test_selection(self):
        r = self.rj("--tag", "mail-import")
        self.assertEqual([d["id"] for d in r["docs"]], [45, 44, 43, 42, 41, 40])
        self.assertEqual((r["held"], r["total"]), (0, 6))
        r = self.rj("--tag", "mail-import", "-n", "2", "--offset", "2")
        self.assertEqual([d["id"] for d in r["docs"]], [43, 42])
        r = self.rj("--ids", "2-3,41")  # --ids picks held ones too
        self.assertEqual([d["id"] for d in r["docs"]], [41, 3, 2])
        self.assertEqual(self.run_tool("--ids", "9-3").returncode, 2)
        r = self.run_tool("--tag", "mail-import", "--cron")
        self.assertEqual(sorted(os.listdir(self.inbox)), ["paperless-inbox-review-sel.md"])
        self.assertIn("--tag mail-import", open(os.path.join(self.inbox, "paperless-inbox-review-sel.md")).read())

    def test_confirmed_and_clear_batches(self):
        r = self.rj("--tag", "mail-import")
        docs = {d["id"]: d["suggest"] for d in r["docs"]}
        self.assertEqual(
            (docs[40]["confidence"], docs[40]["keep_type"], docs[40]["document_type"]), ("confirmed", "Contract", None)
        )
        self.assertEqual(docs[41]["confidence"], "confirmed")
        self.assertEqual(docs[45]["confidence"], "medium")  # the vault disagrees: a change
        self.assertIsNone(docs[45]["keep_type"])
        c = self.batch(r, "clear", "Contract")
        self.assertEqual(sorted(c["ids"]), ["40", "41"])
        self.assertIn("+Housing on 1", c["label"])
        self.assertEqual(sorted(self.batch(r, "clear", "Insurance")["ids"]), ["42", "43", "44"])
        self.assertEqual(list(self.batch(r, "review")["ids"]), ["45"])
        self.assertIn("type Contract ✓ · -Inbox  (confirmed", self.run_tool("--plain", "--tag", "mail-import").stdout)
        again = self.rj("--tag", "mail-import")  # same review -> same tokens
        self.assertEqual([b["token"] for b in again["batches"]], [b["token"] for b in r["batches"]])

    def test_metadata_change_reevaluated_locally(self):
        self.rj("--tag", "mail-import")
        n = sum(1 for c in self.calls() if c[0] == "suggest")
        fx = import_fixture()
        fx["docs"][-1]["document_type"] = "Insurance"  # someone fixed #45 meanwhile
        self.write_fx(fx)
        r = self.rj("--tag", "mail-import")
        self.assertEqual(sum(1 for c in self.calls() if c[0] == "suggest"), n)  # no new vault call
        self.assertEqual({d["id"]: d["suggest"]["confidence"] for d in r["docs"]}[45], "confirmed")

    def test_batch_apply_one_tap(self):
        tok = self.batch(self.rj("--tag", "mail-import"), "clear", "Contract")["token"]
        for conf in (None, "pinb:ok:40", f"pinb:all:{tok[::-1]}"):
            r = self.run_tool("apply", "--batch", tok, *(["--confirm", conf] if conf else []))
            self.assertEqual(r.returncode, 2, conf)
        self.assertFalse([c for c in self.calls() if c[0] == "set"])
        r = self.run_tool("apply", "--batch", tok, "--dry-run")
        self.assertIn("dry run: set 40 --tag=-Inbox", r.stdout)
        self.assertFalse([c for c in self.calls() if c[0] == "set"])
        r = self.run_tool("apply", "--batch", tok, "--confirm", f"pinb:all:{tok}")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("applied 2 of 2", r.stdout)
        self.assertEqual(
            sorted(c for c in self.calls() if c[0] == "set"),
            [["set", "40", "--tag=-Inbox", "--json"], ["set", "41", "--tag=+Housing", "--tag=-Inbox", "--json"]],
        )
        r = self.run_tool("apply", "--batch", tok, "--confirm", f"pinb:all:{tok}")  # a second tap: no-op
        self.assertIn("nothing left", r.stdout)
        self.assertEqual(len([c for c in self.calls() if c[0] == "set"]), 2)
        self.assertEqual(self.run_tool("apply", "--batch", "0000aaaa", "--confirm", "pinb:all:0000aaaa").returncode, 1)

    def test_batch_refuses_changed_and_except(self):
        r = self.rj("--tag", "mail-import")
        tok = self.batch(r, "clear", "Insurance")["token"]
        fx = import_fixture()
        fx["docs"][-4]["title"] = "renamed meanwhile"  # #42 changed since the review
        self.write_fx(fx)
        r = self.run_tool("apply", "--batch", tok, "--confirm", f"pinb:all:{tok}", "--except", "44")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([c[1] for c in self.calls() if c[0] == "set"], ["43"])
        self.assertIn("refused 1 changed since the review (#42)", r.stdout)
        self.assertIn("left out #44", r.stdout)

    def test_skip_batch(self):
        tok = self.batch(self.rj("--tag", "mail-import"), "clear", "Contract")["token"]
        r = self.run_tool("skip", "--batch", tok)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([d["id"] for d in self.rj("--tag", "mail-import")["docs"]], [45, 44, 43, 42])

    def test_old_cache_entries(self):
        os.makedirs(os.path.join(self.d, "state"))
        old = {
            "sig": None,
            "complete": True,
            "at": "2026-10-01",
            "suggest": {
                "document_type": None,
                "correspondent": None,
                "tags": [],
                "remove_tags": ["Inbox"],
                "basis": "vault",
                "confidence": "low",
            },
        }
        r = self.rj("--tag", "mail-import", "--no-llm")
        sigs = {d["id"]: d["sig"] for d in r["docs"]}
        with open(os.path.join(self.d, "state", "suggestions.json"), "w") as f:
            json.dump({"40": dict(old, sig=sigs[40])}, f)
        r = self.rj("--tag", "mail-import", "--no-llm")  # older format kept as shown
        self.assertEqual({d["id"]: d["suggest"]["confidence"] for d in r["docs"]}[40], "low")
        r = self.rj("--tag", "mail-import")  # with the vault: asked again
        self.assertIn(["suggest", "40", "--json"], self.calls())
        self.assertEqual({d["id"]: d["suggest"]["confidence"] for d in r["docs"]}[40], "confirmed")

    def test_sender_groups(self):
        r = self.rj("--tag", "mail-import", "--senders")
        a = self.batch(r, "corr", "Insurer A")
        self.assertEqual(sorted(a["ids"]), ["42"])  # #43 hits both senders: left out; #2 not selected
        self.assertIn("new correspondent Insurer A: 1 doc(s)", a["label"])
        self.assertIn("also matches 2 other", a["label"])
        self.assertIn("name guessed", a["label"])
        self.assertEqual(sorted(self.batch(r, "corr", "Insurer B")["ids"]), ["44"])
        self.assertEqual(r["ambiguous"], 1)
        self.assertEqual(sorted(self.batch(r, "clear", "Insurance")["ids"]), ["43"])  # grouped docs wait
        tests = [c for c in self.calls() if c[0] == "rule"]
        self.assertIn(["rule", "test", "correspondent", "--any", "insurera", "--json"], tests)
        self.assertIn(["rule", "test", "correspondent", "--literal", "Swisscom", "--json"], tests)
        self.assertFalse([c for c in tests if c[1] != "test"])  # read-only until his tap
        self.assertFalse([c for c in self.calls() if c[0] in ("get", "fields", "set")])
        self.assertFalse([c for c in self.calls() if {"--content", "--text", "--reason"} & set(c)])
        tok = a["token"]
        r = self.run_tool("apply", "--batch", tok, "--confirm", f"pinb:all:{tok}", "--name", "Insurer A Ltd")
        self.assertEqual(r.returncode, 0, r.stderr)
        writes = [c for c in self.calls() if c[0] == "set" or c[:2] == ["rule", "set"]]
        self.assertEqual(
            writes,
            [
                ["rule", "set", "correspondent", "Insurer A Ltd", "--any", "insurera", "--create", "--json"],
                ["set", "42", "--correspondent", "Insurer A Ltd", "--json"],
            ],
        )
        self.assertEqual(self.run_tool("apply", "--batch", tok, "--confirm", "x", "--name", "y").returncode, 2)

    def test_skip_sender_proposal(self):
        tok = self.batch(self.rj("--tag", "mail-import", "--senders"), "corr", "Insurer B")["token"]
        r = self.run_tool("skip", "--batch", tok)
        self.assertIn("Insurer B", r.stdout)
        r = self.rj("--tag", "mail-import", "--senders")
        self.assertFalse([b for b in r["batches"] if "Insurer B" in b["label"]])
        self.assertIn(44, [d["id"] for d in r["docs"]])  # the documents stay in the review
        self.assertIn("44", self.batch(r, "clear", "Insurance")["ids"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
