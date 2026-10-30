"""The reduced risk-check rule set against the full one.

hooks/guards/risk_check.py is a verbatim copy of the live hook (the full set, what runs while the switch is absent);
hooks/guards/risk_check_h11.py is the reduced set (ops/guards/make_h11.py derives it). Over the corpus
(tests/guards/h11_corpus.json: the live test tables, scrubbed, plus targeted cases) the reduced set must
  - keep every keep-class finding (false negatives on the keep classes: 0),
  - only ever remove findings, and only retired ones, in their scope,
  - fire the server-scoped rules again as soon as the SSH lockdown is not proven,
and the wrapper must pick the reduced set only for the exact switch line. Never touches the live hooks or their state:
profile, state, log, lockdown files and the switch all live in a temp dir. The local LLM is off.
"""

from __future__ import annotations

import ast
import collections
import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
GUARDS = REPO / "hooks" / "guards"
CORPUS = json.loads((Path(__file__).with_name("h11_corpus.json")).read_text(encoding="utf-8"))["cases"]
TABLE = tomllib.loads((GUARDS / "h11_rules.toml").read_text(encoding="utf-8"))["rule"]
GONE = {"array", "server-config", "mover"}  # code removed in the reduced set
FIXTURE = {
    "owner": {"github": "example-user"},
    "network": {
        "server_name": "Testsrv",
        "server_host": "192.0.2.10",
        "lan_prefix": "192.0.2.",
        "domain": "example.org",
        "hosts": {"web_vm": "web@192.0.2.20"},
    },
}
AGENT = "a0123456789abcdef"


def _load(path: Path, name: str):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("h11")
    profile = tmp / "profile.json"
    profile.write_text(json.dumps(FIXTURE))
    probe = tmp / "probe"
    probe.write_text("verbs 1792688521\n")
    saved = dict(os.environ)
    os.environ.update(
        {
            "ASSISTANT_PROFILE": str(profile),
            "RISK_CHECK_LLM": "off",
            "RISK_CHECK_STATE": str(tmp / "state"),
            "RISK_CHECK_LOG": str(tmp / "risk-check.log"),
            "RISK_CHECK_H11_PROBE": str(probe),
            "RISK_CHECK_H11_MODE_FILE": str(tmp / "no-mode-file"),
        }
    )
    try:
        old = _load(GUARDS / "risk_check.py", "h11_rc_full")
        new = _load(GUARDS / "risk_check_h11.py", "h11_rc_reduced")
        yield {"tmp": tmp, "old": old, "new": new, "probe": probe, "env": dict(os.environ)}
    finally:
        os.environ.clear()
        os.environ.update(saved)


def _lockdown(new, probe_text: str | None, tmp: Path, mode_text: str | None = None):
    p = tmp / "probe-case"
    if probe_text is None:
        p.unlink(missing_ok=True)
    else:
        p.write_text(probe_text)
    m = tmp / "mode-case"
    if mode_text is None:
        m.unlink(missing_ok=True)
    else:
        m.write_text(mode_text)
    new.LOCKDOWN_PROBE, new.LOCKDOWN_MODE = str(p), str(m)
    new._LOCKDOWN.clear()


def _findings(mod, cmd: str, agent: str | None):
    _, rest = mod.split_override(cmd)
    ctx = mod.Ctx("local", cwd=mod.HOME, agent=agent)
    found: list = []
    mod.analyze(rest, ctx, found)
    return collections.Counter((lv, rule, amb) for lv, rule, _, amb in found)


def _cases():
    for c in CORPUS:
        yield c["cmd"], (AGENT if c["agent"] else None), c["src"]


def _keep(new) -> set[str]:
    return set().union(*new.KEEP_CLASS_RULES.values())


def _is_retired(new, rule: str) -> bool:
    if rule in GONE:
        return True
    return rule in new.H11_RETIRED or ("-" in rule and rule.split("-", 1)[0] + "-*" in new.H11_RETIRED)


# ---------------------------------------------------------------------------------------------- the keep classes
@pytest.mark.parametrize("probe", ["verbs 1\n", "root 1\n", None])
def test_keep_class_false_negatives_are_zero(env, probe):
    old, new = env["old"], env["new"]
    _lockdown(new, probe, env["tmp"])
    keep, misses, seen = _keep(new), [], 0
    for cmd, agent, src in _cases():
        fo, fn = _findings(old, cmd, agent), _findings(new, cmd, agent)
        ko = collections.Counter({k: v for k, v in fo.items() if k[1] in keep})
        kn = collections.Counter({k: v for k, v in fn.items() if k[1] in keep})
        seen += sum(ko.values())
        if ko != kn:
            misses.append((src, sorted(ko - kn), sorted(kn - ko)))
    assert seen >= 120, f"the corpus exercises too few keep-class findings ({seen})"
    assert misses == [], f"keep-class findings changed in {len(misses)} cases: {misses[:5]}"


def test_keep_classes_cover_the_brief(env):
    k = env["new"].KEEP_CLASS_RULES
    assert set(k) == {
        "vault flags",
        "Telegram state",
        "public repos",
        "crontab",
        "force-push",
        "secrets",
        "subagent / owner OK",
    }
    for rule in _keep(env["new"]):
        assert not _is_retired(env["new"], rule), rule


# ---------------------------------------------------------------------------------------------- only removals
def test_reduced_set_only_removes_retired_findings(env):
    old, new = env["old"], env["new"]
    _lockdown(new, "verbs 1\n", env["tmp"])
    bad = []
    for cmd, agent, src in _cases():
        fo, fn = _findings(old, cmd, agent), _findings(new, cmd, agent)
        if fn - fo:
            bad.append((src, "added", sorted(fn - fo)))
        removed = [k for k in (fo - fn) if not _is_retired(new, k[1])]
        if removed:
            bad.append((src, "removed a kept rule", removed))
    assert bad == [], bad[:5]


def test_same_findings_same_decision_and_hang(env):
    old, new = env["old"], env["new"]
    _lockdown(new, "verbs 1\n", env["tmp"])
    diff = []
    for cmd, agent, src in _cases():
        do, dn = old.decide(cmd, agent=agent, use_llm=False), new.decide(cmd, agent=agent, use_llm=False)
        if do.hang != dn.hang:
            diff.append((src, "hang", do.hang, dn.hang))
        if _findings(old, cmd, agent) == _findings(new, cmd, agent) and (
            do.level,
            do.rule,
            do.why,
            do.hash,
            do.code,
        ) != (dn.level, dn.rule, dn.why, dn.hash, dn.code):
            diff.append((src, "decision", do.rule, dn.rule))
    assert diff == [], diff[:5]


def test_every_retirement_is_exercised_and_scoped(env):
    old, new = env["old"], env["new"]
    _lockdown(new, "verbs 1\n", env["tmp"])
    dropped, kept = collections.Counter(), collections.Counter()
    for cmd, agent, _ in _cases():
        fo, fn = _findings(old, cmd, agent), _findings(new, cmd, agent)
        for lv, rule, amb in fo:
            key = rule if rule in new.H11_RETIRED or rule in GONE else rule.split("-", 1)[0] + "-*"
            if (lv, rule, amb) in fn:
                kept[key] += 1
            else:
                dropped[key] += 1
    for key in list(new.H11_RETIRED) + sorted(GONE):
        assert dropped[key] > 0, f"{key}: no corpus case shows the retirement"
    for key, where in new.H11_RETIRED.items():
        if where != new.ALL_KINDS:
            assert kept[key] > 0, f"{key}: no corpus case shows it still firing outside {sorted(where)}"


# ---------------------------------------------------------------------------------------------- lockdown fail-safe
@pytest.mark.parametrize(
    "probe,mode",
    [
        ("root 1\n", None),
        (None, None),
        ("garbage\n", None),
        ("", None),
        ("verbs 1\n", "root\n"),
        ("verbs 1\n", "emulate\n"),
    ],
)
def test_server_rules_fire_without_a_proven_lockdown(env, probe, mode):
    old, new = env["old"], env["new"]
    _lockdown(new, probe, env["tmp"], mode)
    assert new.lockdown_on() is False
    everywhere = {r for r, w in new.H11_RETIRED.items() if w == new.ALL_KINDS} | GONE
    diff = []
    for cmd, agent, src in _cases():
        fo = collections.Counter({k: v for k, v in _findings(old, cmd, agent).items() if k[1] not in everywhere})
        fn = _findings(new, cmd, agent)
        server_only = [k for k in fo - fn if k[1] in new.H11_RETIRED and new.H11_RETIRED[k[1]] == {"server"}]
        if server_only:
            diff.append((src, server_only))
        if cmd.startswith(("ssh unraid ", "ssh -o BatchMode=yes unraid ")) and fo != fn:
            diff.append((src, "server command", sorted(fo - fn), sorted(fn - fo)))  # local+server rules too
    assert diff == [], diff[:5]


def test_lockdown_on_needs_verbs(env):
    new = env["new"]
    for probe, mode, want in (
        ("verbs 1\n", None, True),
        ("verbs 1\n", "auto\n", True),
        ("verbs 1\n", "verbs\n", True),
        ("verbs\n", "", True),
        ("root 1\n", "verbs\n", False),
        (None, None, False),
    ):
        _lockdown(new, probe, env["tmp"], mode)
        assert new.lockdown_on() is want, (probe, mode)


# ---------------------------------------------------------------------------------------------- the wrapper switch
WRAP_TOOLS = shutil.which("timeout") and os.path.exists("/usr/bin/python3")


def _wrapper(env, tmp: Path, switch: str | None, cmd: str, drop_reduced: bool = False):
    d = tmp / "hooks"
    if d.exists():
        shutil.rmtree(d)
    d.mkdir()
    for f in ("risk-check.sh", "risk_check.py", "risk_check_h11.py"):
        if not (drop_reduced and f == "risk_check_h11.py"):
            shutil.copy2(GUARDS / f, d / f)
    if switch is not None:
        (d / "risk-check.h11").write_text(switch)
    log = tmp / "wrapper.log"
    log.unlink(missing_ok=True)
    e = dict(env["env"], RISK_CHECK_LOG=str(log), RISK_CHECK_H11_PROBE=str(env["probe"]))
    e.pop("RISK_CHECK_H11_SWITCH", None)
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": "/home/node"})
    r = subprocess.run(
        ["/bin/sh", str(d / "risk-check.sh")], input=payload, capture_output=True, text=True, env=e, timeout=60
    )
    return r, (log.read_text() if log.exists() else "")


@pytest.mark.skipif(not WRAP_TOOLS, reason="needs /usr/bin/python3 and timeout")
@pytest.mark.parametrize(
    "switch,reduced",
    [
        (None, False),
        ("reduced-v1\n", True),
        ("reduced-v1", True),
        ("reduced-v2\n", False),
        ("", False),
        ("REDUCED-V1\n", False),
        (" reduced-v1 x\n", False),
    ],
)
def test_wrapper_picks_the_set_by_the_exact_switch_line(env, tmp_path, switch, reduced):
    r, log = _wrapper(env, tmp_path, switch, "ssh unraid 'mkfs.xfs -f /dev/sdd1'")
    assert r.returncode == 0
    if reduced:
        assert r.stdout.strip() == "", "the reduced set retires format on the locked server"
    else:
        assert '"permissionDecision": "deny"' in r.stdout
        assert "prof=h11" not in log


@pytest.mark.skipif(not WRAP_TOOLS, reason="needs /usr/bin/python3 and timeout")
def test_wrapper_switch_without_the_reduced_script_runs_the_full_set(env, tmp_path):
    r, _ = _wrapper(env, tmp_path, "reduced-v1\n", "ssh unraid 'mkfs.xfs -f /dev/sdd1'", drop_reduced=True)
    assert r.returncode == 0 and '"permissionDecision": "deny"' in r.stdout


@pytest.mark.skipif(not WRAP_TOOLS, reason="needs /usr/bin/python3 and timeout")
@pytest.mark.parametrize("cmd", ["crontab -r", "git push --force origin main", "gh repo delete example-user/x --yes"])
def test_wrapper_reduced_set_still_blocks_keep_classes(env, tmp_path, cmd):
    r, log = _wrapper(env, tmp_path, "reduced-v1\n", cmd)
    assert r.returncode == 0 and '"permissionDecision": "deny"' in r.stdout
    assert "prof=h11" in log


def test_wrapper_differs_from_the_live_one_only_by_the_switch():
    live = (GUARDS / "risk-check.live.sh").read_text().splitlines()
    new = (GUARDS / "risk-check.sh").read_text().splitlines()
    extra = [ln for ln in new if ln not in live]
    gone = [ln for ln in live if ln not in new]
    assert len(gone) == 2 and all("risk_check.py" in ln for ln in gone)
    assert all(
        ("$py" in ln or "h11" in ln.lower() or "switch" in ln or ln.startswith(("py=", "  v=", "fi", "if ")))
        for ln in extra
    ), extra


# ---------------------------------------------------------------------------------------------- the rule table
def _rule_ids(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()

    def names(e):
        if isinstance(e, ast.Constant) and isinstance(e.value, str):
            return [e.value]
        if isinstance(e, ast.IfExp):
            return names(e.body) + names(e.orelse)
        return ["<dynamic>"]

    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "add" and len(n.args) >= 3:
            out.update(names(n.args[2]))
    return out


def test_rule_table_matches_the_code(env):
    new = env["new"]
    rows = {r["id"]: r for r in TABLE if r["hook"] == "risk-check"}
    ids_old = _rule_ids(GUARDS / "risk_check.py")
    assert ids_old - {"<dynamic>", "<rule>"} | {"zfs-*", "zpool-*", "rm-hang-var", "rm-hang-cd"} == set(rows)
    assert not (_rule_ids(GUARDS / "risk_check_h11.py") & GONE)
    for rid, r in rows.items():
        d = r["decision"]
        if d == "retire-now":
            assert rid in GONE or new.H11_RETIRED.get(rid) == new.ALL_KINDS, rid
            assert r.get("scope") == "everywhere", rid
        elif d == "retire-scoped":
            want = {"local+server": {"local", "server"}, "server": {"server"}}[r["scope"]]
            assert set(new.H11_RETIRED.get(rid, ())) == want, rid
        else:
            assert d in ("keep", "retire-later") and not _is_retired(new, rid), rid
        if r["class"]:
            assert rid in new.KEEP_CLASS_RULES[r["class"]] and d == "keep", rid
    for members in new.KEEP_CLASS_RULES.values():
        for rid in members:
            assert rows[rid]["decision"] == "keep" and rows[rid]["class"] in new.KEEP_CLASS_RULES, rid
    assert set(new.H11_RETIRED) | GONE == {
        rid for rid, r in rows.items() if r["decision"].startswith("retire-") and r["decision"] != "retire-later"
    }
    for r in TABLE:
        assert r["decision"] in ("keep", "retire-now", "retire-scoped", "retire-later"), r["id"]
        assert r["covered"] in (
            "keep",
            "boundary",
            "gateway-later",
            "target-gone",
            "log-only",
            "duplicate",
            "browser-gate",
        ), r["id"]
        if r["hook"] != "risk-check":
            assert r["decision"] == "keep", r["id"]


# ---------------------------------------------------------------------------------------------- the regex count
def test_regex_count_method():
    sys.path.insert(0, str(REPO / "ops" / "guards"))
    try:
        import regex_count
    finally:
        sys.path.pop(0)
    src = (
        "import re\nA = re.compile(r'a')\nB = 'b+'\n"
        "def f(x):\n    return re.search(A, x) or re.match(B, x) or A.search(x) or re.sub(r'c', '', x)\n"
        "def g(x):\n    return re.findall(r'd', x)\n"
    )
    assert regex_count.count_python(src) == 4  # A, the B match, the sub, the findall
    assert regex_count.count_python(src, "g") == 1
    assert regex_count.count_shell("grep -qE 'x|y' f\n# grep -E 'no'\negrep z f\ncase $a in *x*) ;; esac\n") == 2
    full = regex_count.count(str(GUARDS / "risk_check.py"))
    reduced = regex_count.count(str(GUARDS / "risk_check_h11.py"))
    assert reduced < full


# ---------------------------------------------------------------------------------------------- the live baseline
@pytest.mark.skipif(os.environ.get("H11_CHECK_LIVE") != "1", reason="opt-in on the host: H11_CHECK_LIVE=1")
def test_repo_baseline_is_the_live_hook():
    live = Path.home() / ".claude" / "hooks"
    assert (GUARDS / "risk_check.py").read_bytes() == (live / "risk_check.py").read_bytes()
    assert (GUARDS / "risk-check.live.sh").read_bytes() == (live / "risk-check.sh").read_bytes()
