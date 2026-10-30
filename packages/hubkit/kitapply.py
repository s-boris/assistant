"""hubkit.kitapply: the brain side of the one-tap kit apply (dispatcher 3.3; design docs/runbooks/kit-apply.md).
Stdlib only.

The server decides; the brain carries. `unraid-ssh run hub-kit-review PART` snapshots the rendered kit on the server,
compares it with what runs and answers the facts: tier (0 no tap · 1 one tap · paste), the sha256 of the facts, an
80-character summary and the card lines, all written by the server. Tier 1 then needs a hub approval for exactly the
server's action string and summary; the server reads his decision in the hub's database itself and consumes it once
(`hub-kit-apply PART FACTS NONCE`). Nothing here can raise a tier, change the card's facts or approve anything.

  review(part)                 the server's review -> dict (part, op, tier, facts, summary, action, card[], why[])
  request(part, mint=True)     review + for tier 1 a pending approval (`approvals new`) -> the card payload to send
  apply(part, facts, nonce)    `hub-kit-apply` -> (exit code, the server's answer)
  log()                        the server's last audit lines

Seams (tests): HUBKIT_UNRAID_SSH_BIN (default ~/bin/unraid-ssh), HUBKIT_APPROVALS_BIN (default ~/bin/approvals).
"""

from __future__ import annotations

import json
import os
import re
import subprocess

from core import paths

from . import HubError

PARTS = ("tg", "voice", "phone", "phoenix", "ci", "tg-off", "voice-off")
PASTE = {
    "tg": "tg-install",
    "tg-off": "tg-rollback",
    "voice": "voice-install",
    "voice-off": "voice-rollback",
    "phone": "phone-install",
    "phoenix": "install",
    "ci": "install",
}
TTL = 1800  # his card stays valid 30 min (the server refuses windows over 24 h)
HEX64 = re.compile(r"^[0-9a-f]{64}$")
NONCE = re.compile(r"^[0-9a-f]{32}$")
REVIEW_TIMEOUT = 1200  # a review may pull the new image to compare digests
APPLY_TIMEOUT = 2400


def _bin(env: str, name: str) -> str:
    return os.environ.get(env) or str(paths.home() / "bin" / name)


def action(part: str, facts: str) -> str:
    """The exact action string his approval covers (the server builds the same one and hashes it)."""
    return json.dumps({"action": "hub.kit.apply", "facts": facts, "part": part}, sort_keys=True, separators=(",", ":"))


def _server(*words: str, timeout: int) -> tuple[int, dict]:
    try:
        p = subprocess.run(
            [_bin("HUBKIT_UNRAID_SSH_BIN", "unraid-ssh"), "run", *words],
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise HubError(f"unraid-ssh run {words[0]}: {type(e).__name__}", 3) from e
    kv: dict = {"card": [], "why": []}
    for line in p.stdout.splitlines():
        k, sep, v = line.partition("\t")
        if not sep:
            continue
        if k in ("card", "why"):
            kv[k].append(v)
        else:
            kv.setdefault(k, v)
    if not kv["why"] and p.returncode and p.stderr.strip():
        kv["why"].append(p.stderr.strip().splitlines()[-1][:300])
    return p.returncode, kv


def _part(part: str) -> str:
    if part not in PARTS:
        raise HubError(f"part must be one of {', '.join(PARTS)}", 2)
    return part


def review(part: str) -> dict:
    rc, kv = _server("hub-kit-review", _part(part), timeout=REVIEW_TIMEOUT)
    if rc != 0 or "facts" not in kv:
        why = "; ".join(kv["why"]) or kv.get("result") or f"exit {rc}"
        raise HubError(f"the server's review failed: {why}", 3)
    facts, tier, summ = kv.get("facts", ""), kv.get("tier"), kv.get("summary", "")
    if not HEX64.match(facts) or tier not in ("0", "1", "paste") or not 0 < len(summ) <= 80 or kv.get("part") != part:
        raise HubError("the server's review is malformed (an older dispatcher?)", 3)
    if kv.get("action") != action(part, facts):
        raise HubError("the server's action string differs from this client's: refusing (version skew?)", 3)
    return kv


def request(part: str, mint: bool = True) -> tuple[int, dict]:
    """The card payload: tier 0 -> the apply command (no tap) · tier 1 -> a minted approval + the card to send ·
    paste -> the reasons and the paste's name. Exit 0, or 3 for paste."""
    r = review(part)
    out = {
        "part": part,
        "container": r.get("container"),
        "op": r.get("op"),
        "tier": r["tier"],
        "facts": r["facts"],
        "summary": r["summary"],
        "card": "\n".join(r["card"]),
        "why": r["why"],
    }
    if r["tier"] == "paste":
        out["next"] = f"hub kit paste {PASTE[part]}"
        return 3, out
    if r["tier"] == "0":
        out["next"] = f"hub kit apply {part} --facts {r['facts']}"
        return 0, out
    if not mint:
        out["next"] = f"hub kit apply-request {part} (without --no-approval) mints his card"
        return 0, out
    ref = f"hubkit:{part}:{r['facts'][:12]}"
    try:
        p = subprocess.run(
            [
                _bin("HUBKIT_APPROVALS_BIN", "approvals"),
                "new",
                "--action",
                "-",
                "--summary",
                r["summary"],
                "--ttl",
                str(TTL),
                "--ref",
                ref,
                "--yes",
                "✅ Apply",
                "--no",
                "❌ Cancel",
                "--json",
            ],
            input=action(part, r["facts"]),
            capture_output=True,
            text=True,
            timeout=60,
        )
        ap = json.loads(p.stdout) if p.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError) as e:
        raise HubError(f"approvals new: {type(e).__name__}", 3) from e
    if not isinstance(ap, dict) or not NONCE.match(str(ap.get("nonce", ""))):
        raise HubError(f"approvals new failed (exit {p.returncode})", 3)
    if ap.get("summary") not in (None, r["summary"]):
        raise HubError("the hub changed the summary: the server would refuse this approval", 3)
    out["approval"] = {"nonce": ap["nonce"], "expires": ap.get("expires"), "ref": ref}
    # one outbox/reply row: the server's card text, his ✅/❌ row comes from the nonce (adapter approvalSend)
    out["send"] = {"text": out["card"], "approval": {"nonce": ap["nonce"]}}
    out["next"] = f"after his ✅: hub kit apply {part} --facts {r['facts']} --nonce {ap['nonce']}"
    return 0, out


def apply(part: str, facts: str, nonce: str | None = None) -> tuple[int, dict]:
    if not HEX64.match(facts or ""):
        raise HubError("--facts: the 64-hex facts from apply-request", 2)
    if nonce is not None and not NONCE.match(nonce):
        raise HubError("--nonce: the 32-hex nonce of his approval (ap:<nonce>:y data works too)", 2)
    words = ["hub-kit-apply", _part(part), facts] + ([nonce] if nonce else [])
    return _server(*words, timeout=APPLY_TIMEOUT)


def log() -> tuple[int, str]:
    try:
        p = subprocess.run(
            [_bin("HUBKIT_UNRAID_SSH_BIN", "unraid-ssh"), "run", "hub-kit-log"],
            capture_output=True,
            text=True,
            timeout=60,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise HubError(f"unraid-ssh run hub-kit-log: {type(e).__name__}", 3) from e
    return p.returncode, p.stdout


def nonce_of(v: str | None) -> str | None:
    """--nonce accepts the bare nonce or his button's callback data ap:<nonce>:y."""
    if v is None:
        return None
    m = re.fullmatch(r"(?:ap:)?([0-9a-f]{32})(?::[yn])?", v.strip())
    return m.group(1) if m else v
