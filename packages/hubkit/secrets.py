"""hubkit.secrets: `hub secrets status|pull|paste` — the brain's side of the hub's secret store. Stdlib only.

  hub secrets status [--json]   counts only: the hub's secret store (files, values, refused, needles), the gateway's
                                masked results, the local fingerprint export (age, items) and secretscan's engine
  hub secrets pull              GET /secrets/fingerprints (brain caller) -> ~/.local/state/secretscan/
                                hub-fingerprints.json (0600, atomic): what secretscan's "fp" engine matches. Never
                                prints the key or a fingerprint. Exit 1 when the running release has no secret
                                store (secrets_off) or the answer is malformed (the old file stays).
  hub secrets paste [--list F]  render the owner's console paste that copies credential files BY PATH from this home
                                into the hub's secrets folder (0600, the hub's uid) and restarts claude-hub so it loads
                                them; print it. Never runs it, never reads a credential. The list (default
                                ~/.config/claude-hub/secrets.h10, the main session's file): "DEST SRC [DOMAIN [FORM]]"
                                lines, SRC relative to the home, DOMAIN "-" for none. FORM: "copy" (default, the file
                                as it is) or "userpass" (the paste, on the console, joins the file's "user" and
                                "password" values, "key: value" or "key=value" lines, CR/quotes stripped, into ONE
                                "user:password" line, no newline: what a Basic slot such as dash takes). Refused:
                                Telegram files (the tg paste copies those), the brain's own hub token, the exceptions
                                (Claude login, Chrome profile and the browser view, voice relay, phonegw), retired
                                copies, reserved or duplicate DEST names, unknown FORMs.
Files: rendered paste in ~/.local/share/claude-hub/secrets-kit/ (secrets.h10 + secrets-install.sh).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from core import paths

from . import HubError, settings, share_dir
from . import client as C
from . import kit as K

TEMPLATE = "ops/hub/secrets-install.sh"
LIST = ("claude-hub", "secrets.h10")
FP_FILE = ("secretscan", "hub-fingerprints.json")
DOMAIN = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
# how the paste turns SRC into the hub's DEST file (the brain never reads SRC; the console does)
FORMS = ("copy", "userpass")
# SRC prefixes that never go into this paste (path relative to the home, "/" separated)
DENY = (
    (".secrets/hub-token", "the brain's own caller token stays here"),
    (".secrets/tg-", "Telegram tokens go with the tg paste"),
    (".claude/", "Claude login and the Telegram plugin's state stay (exception)"),
    (".local/share/browser/", "the Chrome profile stays (exception)"),
    (".secrets/novnc", "the browser view stays with the Chrome profile (exception)"),
    (".secrets/voice-", "voice relay stays (exception)"),
    (".secrets/gemini-api-key", "voice relay stays (exception)"),
    (".config/voice-relay", "voice relay stays (exception)"),
    (".secrets/phonegw/", "phonegw stays (exception)"),
    (".secrets/retired/", "retired copies are deleted, never moved"),
)


def list_file() -> Path:
    return paths.guard(paths.home() / ".config" / LIST[0] / LIST[1])


def fp_file() -> Path:
    return paths.guard(paths.home() / ".local" / "state" / FP_FILE[0] / FP_FILE[1])


def kit_dir() -> Path:
    return share_dir("secrets-kit")


def read_list(f: Path) -> list[tuple[str, str, str, str]]:
    """Validated (dest, src, domain, form) rows. HubError names the line, never a value (the file holds names only)."""
    if not f.is_file():
        raise HubError(f"no list at {f}: 'DEST SRC [DOMAIN [FORM]]' lines (names only)")
    out: list[tuple[str, str, str, str]] = []
    seen: set[str] = set()
    for n, raw in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) not in (2, 3, 4):
            raise HubError(f"{f}:{n}: want 'DEST SRC [DOMAIN [FORM]]'")
        d, s = parts[0], parts[1]
        dom = parts[2] if len(parts) >= 3 else "-"
        form = parts[3] if len(parts) == 4 else "copy"
        if form not in FORMS:
            raise HubError(f"{f}:{n}: FORM must be one of {', '.join(FORMS)}")
        if not K.DEST.match(d) or d in K.RESERVED or d.startswith(("token_", ".")) or d in seen:
            raise HubError(f"{f}:{n}: bad, reserved or duplicate DEST {d!r}")
        if not K.SRC.match(s) or ".." in s.split("/") or s.startswith("/"):
            raise HubError(f"{f}:{n}: SRC must be a plain path relative to the home")
        for pre, why in DENY:
            if s == pre.rstrip("/") or s.startswith(pre):
                raise HubError(f"{f}:{n}: {s} is not for this paste: {why}")
        if dom != "-" and not DOMAIN.match(dom):
            raise HubError(f"{f}:{n}: bad DOMAIN")
        seen.add(d)
        out.append((d, s, dom, form))
    if not out:
        raise HubError(f"{f}: no rows")
    return out


def render(rows: list[tuple[str, str, str, str]], src: Path, out: Path | None = None) -> dict[str, Any]:
    """Write secrets.h10 + secrets-install.sh into the secrets kit; return {id, lines, count, dir, paste}.
    The kit's list is "DEST SRC" per copy row (as before) and "DEST SRC FORM" for the other forms."""
    s = settings()
    body = "".join(f"{d} {p}\n" if fm == "copy" else f"{d} {p} {fm}\n" for d, p, _, fm in rows).encode()
    secsum = hashlib.sha256(body).hexdigest()[:16]
    kid = hashlib.sha256(body + (src / TEMPLATE).read_bytes()).hexdigest()[:12]
    paste = K.fill(
        (src / TEMPLATE).read_text(encoding="utf-8"),
        {
            "SECKIT": kid,
            "SECSUM": secsum,
            "COUNT": str(len(rows)),
            "KIT_DIR": s["bot_home"] + "/.local/share/claude-hub/secrets-kit",
            "HUB_ROOT": s["hub_root"],
            "BOT_HOME": s["bot_home"],
        },
    )
    d = out or kit_dir()
    d.mkdir(parents=True, exist_ok=True)
    for name, data in (("secrets.h10", body), ("secrets-install.sh", paste.encode())):
        tmp = d / f".{name}.tmp"
        tmp.write_bytes(data)
        os.chmod(tmp, 0o644)
        os.replace(tmp, d / name)
    return {"id": kid, "lines": len(paste.splitlines()), "count": len(rows), "dir": str(d), "paste": paste}


def _valid_export(x: Any) -> bool:
    if not (isinstance(x, dict) and x.get("v") == 1 and x.get("alg") == "hmac-sha256"):
        return False
    if not (isinstance(x.get("key"), str) and re.fullmatch(r"[0-9a-f]{64}", x["key"])):
        return False
    if not (isinstance(x.get("w"), int) and isinstance(x.get("items"), list) and isinstance(x.get("alphabet"), str)):
        return False
    for it in x["items"]:
        if not (isinstance(it, dict) and isinstance(it.get("name"), str) and it.get("kind") in ("str", "digits")):
            return False
        if not (
            isinstance(it.get("n"), int) and isinstance(it.get("fp"), str) and re.fullmatch(r"[0-9a-f]{64}", it["fp"])
        ):
            return False
        if it["kind"] == "str" and not isinstance(it.get("a"), int):
            return False
    return True


def pull(a: argparse.Namespace) -> int:
    code, body, why = C.get("/secrets/fingerprints", auth=True, timeout=10.0)
    if code is None:
        print(f"hub secrets: hub unreachable ({why})", file=sys.stderr)
        return 3
    if code != 200 or body is None or not _valid_export(body):
        err = (body or {}).get("error") if isinstance(body, dict) else None
        print(
            f"hub secrets: no usable export (HTTP {code}{', ' + str(err) if err else ''}); the old file stays",
            file=sys.stderr,
        )
        return 1
    f = fp_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.parent / f".{f.name}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(body, fh)
    os.replace(tmp, f)
    weak, refused = body.get("weak") or [], body.get("refused") or []
    print(f"hub secrets: {len(body['items'])} fingerprint(s) ({len(weak)} weak) -> {f}")
    if weak or refused:
        print(f"hub secrets: weak: {', '.join(weak) or 'none'} · refused for use: {', '.join(refused) or 'none'}")
    return 0


def _export_state() -> dict[str, Any]:
    f = fp_file()
    try:
        x = json.loads(f.read_text(encoding="utf-8"))
        return {"file": str(f), "items": len(x.get("items") or []), "age_s": round(time.time() - f.stat().st_mtime),
                "weak": len(x.get("weak") or [])}  # fmt: skip
    except (OSError, ValueError, AttributeError):
        return {"file": str(f), "items": 0, "age_s": None, "weak": 0}


def _engine() -> str:
    e = os.environ.get("SECRETSCAN_ENGINE", "").strip()
    if not e:
        try:
            e = ((paths.home() / ".config" / "secretscan" / "engine").read_text(encoding="utf-8").split() or [""])[0]
        except OSError:
            e = ""
    return e if e in ("fp", "plain") else "fp"


def status(a: argparse.Namespace) -> int:
    code, h, why = C.health(timeout=6.0)
    hub = (h or {}).get("secrets") if isinstance(h, dict) else None
    gw = (h or {}).get("gateway") if isinstance(h, dict) else None
    out = {"hub": hub, "gateway": {k: (gw or {}).get(k) for k in ("redaction", "redacted")} if gw else None,
           "export": _export_state(), "engine": _engine(), "reachable": code is not None}  # fmt: skip
    if a.json:
        print(json.dumps(out, indent=1))
    else:
        if code is None:
            print(f"hub: unreachable ({why})")
        elif not hub:
            print("hub: this release reports no secret store (secrets off)")
        else:
            refused = hub.get("refused") or 0
            print(f"hub: {hub.get('state')} · {hub.get('files', 0)} file(s), {hub.get('values', 0)} value(s), "
                  f"{hub.get('needles', 0)} needle(s) · refused: {refused}")  # fmt: skip
        if gw:
            on = "on" if gw.get("redaction") else "off"
            print(f"gateway: redaction {on} · {gw.get('redacted') or 0} result(s) masked")
        ex = out["export"]
        age = "none" if ex["age_s"] is None else f"{ex['age_s']} s old"
        print(f"export: {ex['items']} fingerprint(s), {ex['weak']} weak, {age} · secretscan engine {out['engine']}")
    return 0 if code is not None else 3


def paste(a: argparse.Namespace) -> int:
    rows = read_list(Path(a.list) if a.list else list_file())
    info = render(rows, paths.repo())
    sys.stdout.write(info["paste"])
    print(f"# {info['count']} file(s), {info['lines']} lines, kit {info['id']} in {info['dir']}", file=sys.stderr)
    return 0


def add_parser(sub: Any) -> None:
    p = sub.add_parser("secrets", help="the hub's secret store, fingerprints, the copy paste")
    ps = p.add_subparsers(dest="secrets_cmd", required=True)
    s = ps.add_parser("status")
    s.add_argument("--json", action="store_true")
    ps.add_parser("pull")
    s = ps.add_parser("paste")
    s.add_argument("--list")


def main(a: argparse.Namespace) -> int:
    return {"status": status, "pull": pull, "paste": paste}[a.secrets_cmd](a)
