#!/usr/bin/env python3
"""hubverify: the hub release verifier for protected paths. Stdlib only, one file. PROTECTED.

A hub release whose protected files (the secret loader, approvals, policy, redaction and this verifier) differ from
every digest the hub has accepted loads only after the owner's ✅ on that digest. hubd runs gate() before it serves:
a refused release exits 3, the loader marks that CURRENT attempt bad and runs the last good release again.

  PROTECTED              release-relative globs (fnmatch; "*" also crosses "/")
  scan(release_dir)      {path: sha256} of the protected files on disk (every file, listed in release.json or not)
  digest(files)          sha256 over the sorted "path sha256" lines (an empty set has a digest too)
  action_sha(digest)     the approvals action_sha of "load this protected set" (canonical JSON, sha256)
  gate(release_dir, state_dir, lookup)   -> (ok, info); lookup(action_sha) -> nonce of an approval HIS tap decided
                         (the hub's tg caller), or None. Accepted digests are kept in STATE_DIR/protected.json.

Accepted when: the digest is accepted already · no STATE_DIR/protected.json yet (the first release with this
verifier: "baseline"; an unreadable or malformed file refuses instead) · an approval for its action_sha was decided
yes through the tg caller · the owner's console wrote the digest into STATE_DIR/protected-approved (one per line; the
interim path until the hub has its own tg consumer).
The brain can request an approval but can't decide one, and it can't write the hub's state folder.

Limit (said, not hidden): the loader (a console paste) runs whatever entry release.json names, so this check binds
releases built by `assistant deploy --target hub`; a hard guarantee needs the loader itself to call it.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import time
from collections.abc import Awaitable, Callable

PROTECTED = (
    "app/hubverify.py",  # the release verifier (changing the rules is itself a protected change)
    "app/hubsecrets.py",  # the secret loader: caller tokens, the database password
    "app/hubapprovals.py",  # approvals: requests, his decisions, consume
    "app/tools/*.cedar",  # the gateway's Cedar policy (packages/tools/policy.cedar)
    "app/hubpolicy*",  # policy
    "app/policy/*",
    "app/hubredact*",  # redaction
    "app/redact/*",
)
ACTION = "hub.release.protected"
STATE_FILE = "protected.json"
OWNER_FILE = "protected-approved"
HEX64 = frozenset("0123456789abcdef")
STATE: dict = {"digest": None, "how": None, "files": 0}  # what the running hub verified (its /health shows it)


def _sha(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def is_protected(rel: str) -> bool:
    return any(fnmatch.fnmatchcase(rel, g) for g in PROTECTED)


def scan(release_dir: str) -> dict[str, str]:
    out: dict[str, str] = {}
    root = os.path.realpath(release_dir)
    for dp, dns, fns in os.walk(root):
        dns.sort()
        for fn in sorted(fns):
            p = os.path.join(dp, fn)
            rel = os.path.relpath(p, root).replace(os.sep, "/")
            if is_protected(rel) and os.path.isfile(p):
                out[rel] = _sha(p)
    return out


def digest(files: dict[str, str]) -> str:
    body = "".join(f"{k} {files[k]}\n" for k in sorted(files))
    return hashlib.sha256(body.encode()).hexdigest()


def action(dg: str) -> dict:
    return {"action": ACTION, "digest": dg}


def action_sha(dg: str) -> str:
    return hashlib.sha256(json.dumps(action(dg), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _is_digest(s: str) -> bool:
    return len(s) == 64 and set(s) <= HEX64


def load(state_dir: str) -> dict:
    """The accepted digests. No file = none yet (the first baseline); a file that can't be read or parsed (permissions
    changed, corrupt) carries "error": the gate refuses until the owner fixes or removes it (never a new baseline)."""
    try:
        with open(os.path.join(state_dir, STATE_FILE), encoding="utf-8") as f:
            d = json.load(f)
    except FileNotFoundError:
        return {"accepted": {}}
    except (OSError, ValueError) as e:
        return {"accepted": {}, "error": type(e).__name__}
    if isinstance(d, dict) and isinstance(d.get("accepted"), dict):
        return d
    return {"accepted": {}, "error": "malformed"}


def _save(state_dir: str, st: dict) -> None:
    os.makedirs(state_dir, exist_ok=True)
    p = os.path.join(state_dir, STATE_FILE)
    tmp = f"{p}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, sort_keys=True, indent=1)
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


def owner_approved(state_dir: str) -> set[str]:
    try:
        with open(os.path.join(state_dir, OWNER_FILE), encoding="utf-8") as f:
            return {ln.strip() for ln in f if _is_digest(ln.strip())}
    except OSError:
        return set()


async def decide(
    dg: str, state_dir: str, lookup: Callable[[str], Awaitable[str | None]] | None
) -> tuple[bool, str, str | None]:
    """-> (accepted, how, nonce): how = accepted | baseline | approval | console | refused | unchecked | state
    unreadable (...)."""
    st = load(state_dir)
    if st.get("error"):
        return False, f"state unreadable ({st['error']})", None
    acc = st["accepted"]
    if dg in acc:
        return True, "accepted", acc[dg].get("nonce")
    if not acc:
        return True, "baseline", None
    if dg in owner_approved(state_dir):
        return True, "console", None
    if lookup is None:
        return False, "unchecked", None
    nonce = await lookup(action_sha(dg))
    return (True, "approval", nonce) if nonce else (False, "refused", None)


async def gate(
    release_dir: str, state_dir: str, lookup: Callable[[str], Awaitable[str | None]] | None, release: str = ""
) -> tuple[bool, dict]:
    files = scan(release_dir)
    dg = digest(files)
    try:
        ok, how, nonce = await decide(dg, state_dir, lookup)
    except Exception as e:  # the store is down: only an accepted digest may load (fail closed)
        ok, how, nonce = False, f"lookup failed ({type(e).__name__})", None
    info = {"digest": dg, "how": how, "files": len(files), "names": sorted(files)}
    if ok:
        st = load(state_dir)
        if dg not in st["accepted"]:
            st["accepted"][dg] = {"how": how, "nonce": nonce, "release": release, "ts": round(time.time(), 3)}
            try:
                _save(state_dir, st)
            except OSError as e:  # an acceptance that can't be kept is no acceptance (fail closed)
                ok, how = False, f"state not writable ({type(e).__name__})"
                info["how"] = how
    if ok:
        STATE.update(digest=dg, how=how, files=len(files))
    return ok, info


if __name__ == "__main__":  # `python3 hubverify.py RELEASE_DIR`: print the protected set and its digest
    import sys

    fs = scan(sys.argv[1] if len(sys.argv) > 1 else ".")
    print(json.dumps({"digest": digest(fs), "action_sha": action_sha(digest(fs)), "files": fs}, indent=1))
