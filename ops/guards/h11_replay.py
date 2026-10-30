#!/usr/local/bin/python3.13 -I
"""h11_replay: replay a risk-check log through the full rule set and the H11 reduced one; aggregates only.

Never prints, stores or returns command text: the log holds the owner's commands (redacted, cut at 300 chars).
Each logged decision (MEDIUM allow, HIGH block / OVERRIDE, HANG block) is decided again by both modules with the
local LLM off, from the home directory (the log keeps no cwd), as main session or subagent like the original.
Reports: how faithfully the full set reproduces the logged rule, per logged rule what the reduced set does, the
keep-class false negatives (a keep-class finding of the full set missing in the reduced set: must be 0), and the
drop in blocks, logged MEDIUM lines and local-LLM consultations.
Usage: h11_replay.py --old risk_check.py --new risk_check_h11.py --log ~/.local/state/risk-check.log
                     [--since YYYY-MM-DD] [--probe FILE] [--json]
The modules read their profile as the hook does (ASSISTANT_PROFILE or the default); state and log go to a temp dir.
"""

import argparse
import collections
import importlib.machinery
import importlib.util
import json
import os
import sys
import tempfile

LEVELS = ("LOW", "MEDIUM", "HIGH")


def load(path, name):
    loader = importlib.machinery.SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def parse(line):
    """-> (ts, level, action, rule, who, cmd) or None. cmd stays inside this process."""
    parts = line.rstrip("\n").split(" ")
    if len(parts) < 6 or parts[1] not in ("LOW", "MEDIUM", "HIGH", "HANG"):
        return None
    i = line.rfind(' cmd="')
    if i < 0 or not line.rstrip("\n").endswith('"'):
        return None
    fields = dict(p.split("=", 1) for p in parts[3:8] if "=" in p)
    return (
        parts[0],
        parts[1],
        parts[2],
        fields.get("rule", "-"),
        fields.get("who", "main"),
        line.rstrip("\n")[i + 6 : -1],
    )


def findings(mod, cmd, agent):
    _, rest = mod.split_override(cmd)
    ctx = mod.Ctx("local", cwd=mod.HOME, agent=agent)
    found = []
    mod.analyze(rest, ctx, found)
    return found


def would_ask_llm(found):
    certain = [f for f in found if not f[3]]
    top = max((f[0] for f in certain), default=0)
    return any(f[3] and f[0] > top for f in found)


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", required=True)
    ap.add_argument("--new", required=True)
    ap.add_argument("--log", required=True)
    ap.add_argument("--since", default="")
    ap.add_argument("--probe")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    tmp = tempfile.mkdtemp(prefix="h11-replay-")
    os.environ.update(
        {
            "RISK_CHECK_LLM": "off",
            "RISK_CHECK_STATE": os.path.join(tmp, "state"),
            "RISK_CHECK_LOG": os.path.join(tmp, "risk-check.log"),
        }
    )
    if a.probe:
        os.environ["RISK_CHECK_H11_PROBE"] = a.probe
    old, new = load(a.old, "rc_old"), load(a.new, "rc_new")
    keep = set().union(*new.KEEP_CLASS_RULES.values())

    per_rule = collections.defaultdict(collections.Counter)
    tot = collections.Counter()
    first = last = None
    with open(a.log, encoding="utf-8", errors="replace") as f:
        for line in f:
            p = parse(line)
            if not p or p[0][:10] < a.since:
                continue
            ts, level, action, rule, who, cmd = p
            first, last = first or ts[:10], ts[:10]
            agent = None if who == "main" else "replay-agent"
            fo, fn = findings(old, cmd, agent), findings(new, cmd, agent)
            do, dn = old.decide(cmd, agent=agent, use_llm=False), new.decide(cmd, agent=agent, use_llm=False)
            c = per_rule[(level, action, rule)]
            c["n"] += 1
            tot["lines"] += 1
            if level != "HANG":
                same = LEVELS[do.level] == level and do.rule == rule
                c["old_reproduced"] += same
                tot["old_reproduced"] += same
                tot["comparable"] += 1
            else:
                c["old_reproduced"] += bool(do.hang)
            if do.hang or dn.hang:
                c["hang_same"] += do.hang == dn.hang
            # keep-class findings of the full set must all be in the reduced set (same level, same rule)
            ko = collections.Counter((lv, r) for lv, r, _, _ in fo if r in keep)
            kn = collections.Counter((lv, r) for lv, r, _, _ in fn if r in keep)
            missing = sum((ko - kn).values())
            c["keep_fn"] += missing
            tot["keep_fn"] += missing
            tot["keep_findings_old"] += sum(ko.values())
            # outcome under the reduced set
            if dn.hang:
                out = "hang (same)" if do.hang else "hang (new)"
            elif dn.level == do.level and dn.rule == do.rule:
                out = "unchanged"
            elif dn.level == 0:
                out = "silent"
            elif dn.level < do.level:
                out = "lowered"
            else:
                out = "other rule"
            c[out] += 1
            for name, d in (("old", do), ("new", dn)):
                if d.hang or d.level == 2:
                    tot[f"{name}_blocks"] += 1
                elif d.level == 1:
                    tot[f"{name}_medium_lines"] += 1
            tot["old_llm"] += would_ask_llm(fo) and not do.hang
            tot["new_llm"] += would_ask_llm(fn) and not dn.hang
            if rule == "rm-unresolved":
                tot["rm_unresolved_lines"] += 1
                tot["rm_unresolved_guarded_var"] += ":?}" in cmd
    res = {
        "window": [first, last],
        "totals": dict(tot),
        "per_rule": {" ".join(k): dict(v) for k, v in sorted(per_rule.items(), key=lambda kv: -kv[1]["n"])},
    }
    if a.json:
        print(json.dumps(res, indent=1))
    else:
        print(f"window {first} .. {last}: {tot['lines']} logged decisions")
        print(
            f"full set reproduces the logged level+rule: {tot['old_reproduced']}/{tot['comparable']} "
            "(LLM off, cwd unknown, commands cut at 300 chars)"
        )
        print(f"keep-class findings (full set): {tot['keep_findings_old']}; missing in reduced: {tot['keep_fn']}")
        for k in ("blocks", "medium_lines", "llm"):
            print(f"{k}: full {tot['old_' + k]} -> reduced {tot['new_' + k]}")
        print(
            f"rm-unresolved lines: {tot['rm_unresolved_lines']}, with a ${{VAR:?}} guard: "
            f"{tot['rm_unresolved_guarded_var']}"
        )
        for k, v in res["per_rule"].items():
            outs = ", ".join(
                f"{o} {v[o]}"
                for o in ("unchanged", "silent", "lowered", "other rule", "hang (same)", "hang (new)")
                if v.get(o)
            )
            print(
                f"  {v['n']:4d}  {k:38s} reproduced {v.get('old_reproduced', 0):4d} | {outs}"
                + (f" | KEEP FN {v['keep_fn']}" if v.get("keep_fn") else "")
            )
    return 1 if tot["keep_fn"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
