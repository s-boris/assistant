#!/usr/bin/env python3
"""hubci: the hub's side of the CI runner. hubd imports it; stdlib + hubstore.

The brain sends a git bundle (POST /ci/jobs); the hub writes it into the spool it shares with the ci container (no
network, no secrets), which runs `assistant ci` on it and writes a result back. The hub signs every result (HMAC-SHA256
with a key only it holds: STATE/ci-sign.key, made on first use, never exported) and keeps it in ci_jobs. The brain
can't write results (the spool isn't in its container) and can't sign them.

Spool (HUB_SPOOL/ci): jobs/<id>.bundle + jobs/<id>.json (the job; .json last = visible) -> the runner moves the json
to run/ while it works -> results/<id>.json -> the hub signs, stores and moves it to done/ (pruned after 7 days).

Routes (brain): POST /ci/jobs {rid, commit, tree, base, branch, bundle (base64, <= 5 MB), min_mem_gb} -> {id, state}
  (min_mem_gb, optional: the land queue's memory gate; the runner claims the job only while the host's MemAvailable
  is at least that, 0 = off; missing = the runner's default, core.hostmem.LAND_MIN_GB)
  GET /ci/jobs/<id>             {id, state (queued|running|done|lost), ok, record, sig, valid}
  GET /ci/records?tree=T|commit=C   the newest finished record for that tree or commit (+ sig, valid) or 404
  POST /ci/verify {record, sig}  {valid}: the brain checks a record it kept
  GET /ci/slots                 {set (N or null), runner: {slots, jobs, ts, release, host_mem_gb, mem_hold}}: the
                                runner's job slots and memory gate (heartbeat)
  POST /ci/slots {slots: N|null}   N (1..SLOTS_MAX) jobs at once from the runner's next claim on, no restart;
                                null = the release's default (ciworker SLOTS); rollback to one at a time: 1
  GET /protected/<digest>        hubverify: is that protected set accepted (+ the running one)
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import time
from typing import Any

import hubstore as hs
import hubverify

SPOOL = os.environ.get("HUB_SPOOL", "/var/spool/hub")
STATE = os.environ.get("HUB_STATE") or os.path.dirname(os.environ.get("HUB_LOADER_STATE", "")) or "/var/lib/hub"
RELEASE = os.environ.get("HUB_RELEASE", "")
MAX_BUNDLE = 16 << 20  # hubkit.ci's; incremental bundles are KBs, a cold mirror gets the whole tree
MIN_MEM_MAX = 1024  # GB: a job's min_mem_gb (the land queue's memory gate) above this is refused
# a pytest step's test selection (impacted|full + counts), its one retry and quarantined ids, the legacy step's red
# or slow suites {suite: [ok, secs]} and the red ones' stderr tails {suite: text}: signed with the step
STEP_EXTRA = ("selection", "rerun", "quarantined", "suites", "tails")


def _small(v: Any) -> bool:
    try:
        return len(json.dumps(v)) <= 6000
    except (TypeError, ValueError):
        return False


SHA = re.compile(r"^[0-9a-f]{40}$")
JOB = re.compile(r"^[0-9a-f]{16}$")
BRANCH = re.compile(r"^[A-Za-z0-9._/-]{1,80}$")
LOST_AFTER = 3600.0
SLOTS_FILE = "slots.json"  # ciworker.SLOTS_FILE: HUB_SPOOL/ci/slots.json, read by the runner before each claim
SLOTS_MAX = 4  # ciworker.SLOTS_MAX
DONE_KEEP = 7 * 86_400
KEY_FILE = "ci-sign.key"


class NotFound(Exception):
    pass


def _dir(*parts: str) -> str:
    d = os.path.join(SPOOL, "ci", *parts)
    os.makedirs(d, mode=0o2770, exist_ok=True)
    for x in (os.path.join(SPOOL, "ci"), d):  # the umask drops group write: the ci container (same group) needs it
        try:
            if os.stat(x).st_uid == os.getuid():
                os.chmod(x, 0o2770)
        except OSError:
            pass
    return d


def _write(path: str, data: bytes) -> None:
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.chmod(tmp, 0o640)
    os.replace(tmp, path)


def canonical(o: Any) -> bytes:
    return json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _key() -> bytes:
    p = os.path.join(STATE, KEY_FILE)
    try:
        with open(p, "rb") as f:
            k = f.read().strip()
        if len(k) >= 32:
            return k
    except OSError:
        pass
    os.makedirs(STATE, exist_ok=True)
    k = secrets.token_hex(32).encode()
    fd = os.open(p + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(k)
    os.replace(p + ".tmp", p)
    return k


def key_id() -> str:
    return hashlib.sha256(b"key-id:" + _key()).hexdigest()[:12]


def sign(record: dict) -> str:
    return hmac.new(_key(), canonical(record), hashlib.sha256).hexdigest()


def valid(record: Any, sig: Any) -> bool:
    if not isinstance(record, dict) or not isinstance(sig, str):
        return False
    return hmac.compare_digest(sign(record), sig)


def counts() -> dict[str, int]:
    out = {}
    for name, sub, ext in (("queued", "jobs", ".json"), ("running", "run", ".json"), ("unsigned", "results", ".json")):
        try:
            out[name] = sum(1 for n in os.listdir(os.path.join(SPOOL, "ci", sub)) if n.endswith(ext))
        except OSError:
            out[name] = 0
    return out


def _out(r: dict[str, Any]) -> dict[str, Any]:
    rec, sig = r.get("record"), r.get("sig")
    if isinstance(rec, str):
        rec = json.loads(rec)
    state = r["state"]
    if state == "queued" and os.path.exists(os.path.join(SPOOL, "ci", "run", r["id"] + ".json")):
        state = "running"
    return {
        "id": r["id"],
        "state": state,
        "ok": r.get("ok"),
        "commit": r["commit_sha"],
        "tree": r["tree"],
        "created": r["created"],
        "finished": r.get("finished"),
        "secs": r.get("secs"),
        "record": rec,
        "sig": sig,
        "valid": valid(rec, sig) if rec else None,
    }


# ------------------------------------------------------------------------------------------------ collector


async def collect(h: Any) -> int:
    """Sign and store every result the runner left; lost jobs (no result after LOST_AFTER) are closed. -> count."""
    res_dir, done_dir = _dir("results"), _dir("done")
    n = 0
    for name in sorted(os.listdir(res_dir)):
        if not name.endswith(".json") or not JOB.match(name[:-5]):
            continue
        p = os.path.join(res_dir, name)
        try:
            with open(p, encoding="utf-8") as f:
                res = json.load(f)
            if not isinstance(res, dict):
                raise ValueError("not an object")
        except (OSError, ValueError):
            os.replace(p, os.path.join(done_dir, name + ".bad"))
            continue
        jid = name[:-5]
        raw = res.get("schema")
        schema = raw if isinstance(raw, str) and hs.SCHEMA_RE.match(raw) else hs.LIVE
        async with h.store.tx(schema) as c:
            job = await hs.one(c, "SELECT * FROM ci_jobs WHERE id=%s FOR UPDATE", (jid,))
            if job is None or job["state"] not in ("queued", "lost"):
                os.replace(p, os.path.join(done_dir, name + ".orphan"))
                continue
            steps = [
                {
                    k: s.get(k)
                    for k in ("name", "ok", "detail", "secs", "failed", *STEP_EXTRA)
                    if k in s and (k not in STEP_EXTRA or _small(s[k]))
                }
                for s in res.get("steps") or []
                if isinstance(s, dict)
            ]
            ok = bool(res.get("ok")) and bool(steps) and all(s["ok"] for s in steps)
            if res.get("commit") != job["commit_sha"] or res.get("tree") != job["tree"]:
                ok, steps = False, steps + [{"name": "identity", "ok": False, "detail": "result is for another tree"}]
            record = {
                "v": 1,
                "job": jid,
                "commit": job["commit_sha"],
                "tree": job["tree"],
                "base": job["base"],
                "branch": job["branch"],
                "bundle_sha256": job["bundle_sha256"],
                "ok": ok,
                "steps": steps,
                "error": str(res.get("error") or "")[:300],
                "started": res.get("started"),
                "ended": res.get("ended"),
                "secs": res.get("secs"),
                "runner": res.get("runner") or {},
                "hub_release": RELEASE,
                "signed_at": round(time.time(), 3),
                "key_id": key_id(),
                "signer": "claude-hub",
            }
            sig = sign(record)
            await hs.run(
                c,
                "UPDATE ci_jobs SET state='done', ok=%s, finished=%s, secs=%s, record=%s, sig=%s WHERE id=%s",
                (ok, record["signed_at"], res.get("secs"), json.dumps(record), sig, jid),
            )
        os.replace(p, os.path.join(done_dir, name))
        n += 1
    now = time.time()
    for name in os.listdir(done_dir):
        q = os.path.join(done_dir, name)
        try:
            if now - os.stat(q).st_mtime > DONE_KEEP:
                os.unlink(q)
        except OSError:
            pass
    return n


async def collector(h: Any, stop: asyncio.Event, every: float = 2.0) -> None:
    while not stop.is_set():
        if h.store is not None:
            try:
                await collect(h)
            except Exception as e:  # keep collecting; the event says what broke
                print(json.dumps({"event": "ci_collect_error", "error": type(e).__name__}), flush=True)
        try:
            await asyncio.wait_for(stop.wait(), every)
        except TimeoutError:
            pass


# ------------------------------------------------------------------------------------------------ routes


async def r_submit(h: Any, who: str, s: str, a: dict, q: dict) -> Any:
    commit, tree, base, branch = a.get("commit"), a.get("tree"), a.get("base"), a.get("branch") or ""
    if not (isinstance(commit, str) and SHA.match(commit) and isinstance(tree, str) and SHA.match(tree)):
        raise hs.BadRequest("commit and tree: 40 hex")
    if base is not None and not (isinstance(base, str) and SHA.match(base)):
        raise hs.BadRequest("base: 40 hex or null")
    if not isinstance(branch, str) or (branch and not BRANCH.match(branch)):
        raise hs.BadRequest("branch: letters, digits and ._/-")
    try:
        bundle = base64.b64decode(str(a.get("bundle") or ""), validate=True)
    except (binascii.Error, ValueError):
        raise hs.BadRequest("bundle: base64") from None
    if not bundle.startswith(b"# v2 git bundle") and not bundle.startswith(b"# v3 git bundle"):
        raise hs.BadRequest("bundle: not a git bundle")
    if len(bundle) > MAX_BUNDLE:
        raise hs.BadRequest(f"bundle: over {MAX_BUNDLE >> 20} MB")
    min_mem = a.get("min_mem_gb")  # the land queue's memory gate: the runner claims the job only at or above it
    if min_mem is not None and (
        not isinstance(min_mem, (int, float)) or isinstance(min_mem, bool) or not 0 <= min_mem <= MIN_MEM_MAX
    ):
        raise hs.BadRequest(f"min_mem_gb: 0..{MIN_MEM_MAX} or null")

    async def make() -> dict[str, Any]:
        jid = secrets.token_hex(8)
        bsha = hashlib.sha256(bundle).hexdigest()
        jobs = _dir("jobs")
        _dir("run"), _dir("results")
        _write(os.path.join(jobs, jid + ".bundle"), bundle)
        job = {
            "id": jid,
            "commit": commit,
            "tree": tree,
            "base": base,
            "branch": branch,
            "bundle_sha256": bsha,
            "schema": s,
            "created": round(time.time(), 3),
        }
        if min_mem is not None:
            job["min_mem_gb"] = float(min_mem)
        await hs.run(
            c,
            "INSERT INTO ci_jobs(id, commit_sha, tree, base, branch, bundle_sha256, state, created)"
            " VALUES (%s,%s,%s,%s,%s,%s,'queued',%s)",
            (jid, commit, tree, base, branch, bsha, job["created"]),
        )
        _write(os.path.join(jobs, jid + ".json"), json.dumps(job).encode())
        return {"id": jid, "state": "queued"}

    async with h.store.tx(s) as c:
        return await hs.once(c, a.get("rid"), who, "ci_submit", make)


def _slots_view() -> dict[str, Any]:
    try:
        with open(os.path.join(SPOOL, "ci", SLOTS_FILE), encoding="utf-8") as f:
            n = json.load(f).get("slots")
    except (OSError, ValueError, AttributeError):
        n = None
    try:
        with open(os.path.join(SPOOL, "ci-heartbeat.json"), encoding="utf-8") as f:
            hb = json.load(f)
    except (OSError, ValueError):
        hb = {}
    hb = hb if isinstance(hb, dict) else {}
    keys = ("slots", "jobs", "ts", "release", "host_mem_gb", "mem_hold")
    return {"set": n, "runner": {k: hb.get(k) for k in keys}}


async def r_slots_get(h: Any, who: str, s: str, a: dict, q: dict) -> Any:
    return _slots_view()


async def r_slots_set(h: Any, who: str, s: str, a: dict, q: dict) -> Any:
    n = a.get("slots")
    if n is not None and (not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= SLOTS_MAX):
        raise hs.BadRequest(f"slots: 1..{SLOTS_MAX} or null (the release's default)")
    p = os.path.join(_dir(), SLOTS_FILE)
    if n is None:
        try:
            os.unlink(p)
        except FileNotFoundError:
            pass
    else:
        _write(p, json.dumps({"slots": n, "at": round(time.time(), 3), "by": str(who)[:40]}).encode())
    return _slots_view()


async def r_job(h: Any, who: str, s: str, a: dict, q: dict, jid: str) -> Any:
    await collect(h)
    async with h.store.tx(s) as c:
        r = await hs.one(c, "SELECT * FROM ci_jobs WHERE id=%s", (jid,))
        if r is None:
            raise NotFound(f"ci job {jid}")
        if r["state"] == "queued" and time.time() - r["created"] > LOST_AFTER:
            await hs.run(c, "UPDATE ci_jobs SET state='lost' WHERE id=%s AND state='queued'", (jid,))
            r["state"] = "lost"
    return _out(r)


async def r_records(h: Any, who: str, s: str, a: dict, q: dict) -> Any:
    tree, commit = q.get("tree"), q.get("commit")
    if tree and SHA.match(tree):
        where, arg = "tree=%s", tree
    elif commit and SHA.match(commit):
        where, arg = "commit_sha=%s", commit
    else:
        raise hs.BadRequest("tree=<40 hex> or commit=<40 hex>")
    ok_only = q.get("ok") in ("1", "true")
    await collect(h)
    async with h.store.tx(s) as c:
        r = await hs.one(
            c,
            f"SELECT * FROM ci_jobs WHERE {where} AND state='done'"
            + (" AND ok" if ok_only else "")
            + " ORDER BY finished DESC LIMIT 1",
            (arg,),
        )
    if r is None:
        raise NotFound("no finished CI record for it")
    return _out(r)


async def r_verify(h: Any, who: str, s: str, a: dict, q: dict) -> Any:
    return {"valid": valid(a.get("record"), a.get("sig")), "key_id": key_id()}


async def r_protected(h: Any, who: str, s: str, a: dict, q: dict, dg: str) -> Any:
    async def lookup(sha: str) -> str | None:
        return await approved_nonce(h, sha)

    ok, how, nonce = await hubverify.decide(dg, STATE, lookup)
    return {
        "digest": dg,
        "accepted": ok,
        "how": how,
        "nonce": nonce,
        "action_sha": hubverify.action_sha(dg),
        "running": dict(hubverify.STATE),
    }


async def approved_nonce(h: Any, sha: str) -> str | None:
    """A yes decided through the tg caller (his tap) for this action; marks it used by the verifier."""
    async with h.store.tx(hs.LIVE) as c:
        r = await hs.one(
            c,
            "SELECT nonce FROM approvals WHERE action_sha=%s AND status='approved' AND decided_by IS NOT NULL"
            " ORDER BY decided_ts DESC LIMIT 1",
            (sha,),
        )
        if r is None:
            return None
        await hs.run(
            c,
            "UPDATE approvals SET used_ts=%s, used_by='hub-verifier' WHERE nonce=%s AND used_ts IS NULL",
            (time.time(), r["nonce"]),
        )
        return str(r["nonce"])


def spool_clear(jid: str) -> None:
    """Tests: drop one job's spool files."""
    for sub, ext in (("jobs", ".json"), ("jobs", ".bundle"), ("run", ".json"), ("results", ".json")):
        p = os.path.join(SPOOL, "ci", sub, jid + ext)
        if os.path.exists(p):
            os.unlink(p)


def wipe_spool() -> None:
    shutil.rmtree(os.path.join(SPOOL, "ci"), ignore_errors=True)
