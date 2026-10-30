"""hubkit.ci: the brain's side of the hub's CI runner. Stdlib only (core.hub: the API + token).

submit(wt, head, tree, base, branch)  an incremental bundle from worktree wt -> POST /ci/jobs -> {id, state}
job(id) / wait(id, timeout)           GET /ci/jobs/<id> until done | lost (or the time is up: the last answer)
record(tree=, commit=, ok_only=)      the hub's newest finished record for that tree or commit, or None
verify(record, sig)                   the hub checks the signature (the brain has no key; it can't sign)
slots(n="get")                        the runner's job slots: GET /ci/slots, or POST N (1..4) / None (the release's
                                      default) -> {set, runner: {slots, jobs}}; `hub ci slots [N|default]`
cache(answer) / cached(tree)          a copy in ~/.local/state/assistant/ci-signed/<tree>.json (audit; re-verified
                                      through the hub before any gate trusts it)
The bundle holds no history (main alone passed the 5 MB cap, 29.10): two parentless-chain copies of the trees, B' (the
base's tree) and H' (HEAD's tree, parent B'), on top of the seed S the runner already keeps in its mirror (ciworker:
<work>/mirror.git) -> only the objects new since S (KBs). The seed ref refs/ci/seed (shared by every worktree) moves
to B' after each accepted submit, so the chain follows main. No seed yet, or the runner said "seed missing" (mirror
lost; wait() drops the ref): one full bundle of the trees (~4 MB), then incremental again. The job's commit is H' (its
tree is HEAD's: records and the gate key on the tree); base B' gives the runner the same diff as base..HEAD.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

from core import hub, paths

MAX_BUNDLE = 16 << 20  # hubci's (hubd takes 24 MB bodies on /ci/jobs); a cold seed is the whole tree
SEED_REF = "refs/ci/seed"
_ID = {f"GIT_{w}_{k}": v for w in ("AUTHOR", "COMMITTER") for k, v in
       (("NAME", "assistant-ci"), ("EMAIL", "assistant-ci@localhost"), ("DATE", "@0 +0000"))}  # fmt: skip


def _git(wt: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", "-C", str(wt), *args], capture_output=True, text=True, timeout=120, env={**os.environ, **_ID}
    )
    if r.returncode != 0:
        raise hub.HubError(f"git {args[0]}: {(r.stderr.strip().splitlines() or ['?'])[-1][:200]}")
    return r.stdout.strip()


def _seed(wt: Path) -> str | None:
    r = subprocess.run(["git", "-C", str(wt), "rev-parse", "--verify", "-q", SEED_REF], capture_output=True, text=True)
    return r.stdout.strip() or None if r.returncode == 0 else None


def flat(wt: Path, head: str, base: str | None) -> tuple[bytes, str, str | None]:
    """-> (bundle, H', B'): the trees of base and HEAD as commits on the seed (deterministic: same trees, same ids)."""
    seed = _seed(wt)
    par = ["-p", seed] if seed else []
    b = _git(wt, "commit-tree", *par, "-m", "ci base", base + "^{tree}") if base else None
    h = _git(wt, "commit-tree", *(["-p", b] if b else par), "-m", "ci head", head + "^{tree}")
    tag = f"refs/ci/job-{os.getpid()}-{h[:12]}"
    refs = [(tag + "-h", h)] + ([(tag + "-b", b)] if b else [])
    for ref, sha in refs:
        _git(wt, "update-ref", ref, sha)
    try:
        with tempfile.TemporaryDirectory(prefix="ci-bundle-") as d:
            out = os.path.join(d, "x.bundle")
            _git(wt, "bundle", "create", "--quiet", out, *[r for r, _s in refs], *(["--not", seed] if seed else []))
            data = Path(out).read_bytes()
    finally:
        for ref, _sha in refs:
            subprocess.run(["git", "-C", str(wt), "update-ref", "-d", ref], capture_output=True)
    if len(data) > MAX_BUNDLE:
        raise hub.HubError(f"bundle is {len(data) >> 20} MB (limit {MAX_BUNDLE >> 20} MB)")
    return data, h, b


def reset_seed(wt: Path | None = None) -> None:
    """The runner lost its mirror ("seed missing"): the next submit sends the full trees."""
    subprocess.run(["git", "-C", str(wt or paths.repo()), "update-ref", "-d", SEED_REF], capture_output=True)


def bundle(wt: Path, with_main: bool = True) -> bytes:
    """HEAD (+ main unless with_main is False: a full run, nothing to diff against)."""
    with tempfile.TemporaryDirectory(prefix="ci-bundle-") as d:
        out = os.path.join(d, "x.bundle")
        refs = ["HEAD"] + (["main"] if with_main and _has_main(wt) else [])
        r = subprocess.run(
            ["git", "-C", str(wt), "bundle", "create", "--quiet", out, *refs],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if r.returncode != 0:
            raise hub.HubError(f"git bundle: {(r.stderr.strip().splitlines() or ['?'])[-1][:200]}")
        data = Path(out).read_bytes()
    if len(data) > MAX_BUNDLE:
        raise hub.HubError(f"bundle is {len(data) >> 20} MB (limit {MAX_BUNDLE >> 20} MB)")
    return data


def _has_main(wt: Path) -> bool:
    r = subprocess.run(["git", "-C", str(wt), "rev-parse", "--verify", "-q", "refs/heads/main"], capture_output=True)
    return r.returncode == 0


def submit(wt: Path, head: str, tree: str, base: str | None, branch: str, with_main: bool = True) -> dict:
    """with_main False: no base (a full run: the runner has nothing to diff against)."""
    data, h, b = flat(wt, head, base if with_main else None)
    body = {
        "rid": hub.new_rid(),
        "commit": h,
        "tree": tree,
        "base": b,
        "branch": branch[:80],
        "bundle": base64.b64encode(data).decode(),
    }
    ans = hub.call("POST", "/ci/jobs", body, timeout=60)
    if b:  # accepted: the runner fetches it into its mirror first thing; the chain follows main
        _git(wt, "update-ref", SEED_REF, b)
    return ans


def slots(n: int | None | str = "get") -> dict:
    """The runner's job slots (hubci r_slots_*): "get" reads, an int sets, None goes back to the release's default."""
    if n == "get":
        return hub.call("GET", "/ci/slots", timeout=15)
    return hub.call("POST", "/ci/slots", {"slots": n}, timeout=15)


def job(jid: str, timeout: float = 15.0) -> dict:
    return hub.call("GET", f"/ci/jobs/{jid}", timeout=timeout)


def wait(jid: str, timeout: float, every: float = 2.0) -> dict:
    """The job's answer once done or lost, else its last state after `timeout` s. The hub down for a while (a hub
    deploy restarts it; queued jobs stay in its spool) is waited out within the same timeout."""
    end = time.monotonic() + max(0.0, timeout)
    while True:
        try:
            got = job(jid)
        except hub.HubDown:
            if time.monotonic() >= end:
                raise
            time.sleep(min(max(every, 5.0), max(0.1, end - time.monotonic())))
            continue
        if got.get("state") in ("done", "lost") or time.monotonic() >= end:
            if str((got.get("record") or {}).get("error") or "").startswith("seed missing"):
                reset_seed()
            return got
        time.sleep(min(every, max(0.1, end - time.monotonic())))


def record(tree: str | None = None, commit: str | None = None, ok_only: bool = False) -> dict | None:
    q = f"tree={tree}" if tree else f"commit={commit}"
    try:
        return hub.call("GET", f"/ci/records?{q}" + ("&ok=1" if ok_only else ""), timeout=15)
    except hub.Refused as e:
        if e.code == 404:
            return None
        raise


def verify(rec: dict, sig: str) -> bool:
    return bool(hub.call("POST", "/ci/verify", {"record": rec, "sig": sig}, timeout=15).get("valid"))


def signed_green(ans: dict | None) -> bool:
    """A finished job whose record the hub signed, says valid and says ok."""
    return bool(ans and ans.get("state") == "done" and ans.get("sig") and ans.get("valid") is True and ans.get("ok"))


def _cache_dir() -> Path:
    return paths.state("ci-signed")


def cache(ans: dict) -> None:
    rec = ans.get("record") or {}
    tree = rec.get("tree") or ans.get("tree")
    if not tree or not ans.get("sig"):
        return
    d = _cache_dir()
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f".{tree}.{os.getpid()}.tmp"
    tmp.write_text(
        json.dumps({"record": rec, "sig": ans["sig"], "job": ans.get("id"), "fetched": time.time()}, sort_keys=True),
        encoding="utf-8",
    )
    os.chmod(tmp, 0o600)
    os.replace(tmp, d / f"{tree}.json")


def cached(tree: str) -> dict | None:
    try:
        return json.loads((_cache_dir() / f"{tree}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
