"""hubkit.kit: render the hub install kit from a source tree (a git archive of main) and the profile. Stdlib only.

render(src, rev)   -> info; writes ~/.local/share/claude-hub/kit/ (the previous one stays as kit.prev):
    compose.yaml          ops/hub/compose.yaml as it is (values come from .env)
    hub.env               HUB_ROOT, BOT_HOME, BRAIN_NETWORK, TZ (profile), HUB_LOADER (loader hash), HUB_KIT (kit id)
    loader/hubloader.py   packages/hub/loader.py as it is
    secrets.copy          "DEST SRC" lines: credential files the paste copies from the brain's home into the hub's
                          secrets folder (names only; from ~/.config/claude-hub/secrets.copy, validated)
    install.sh            the console paste (install = update); refuses to run when those four files changed
    rollback.sh           the remove paste
    card.txt              the owner's step card
    kit.json              id, loader hash, rev, profile source, paste lines, pull size
  H6, the Telegram adapter in the hub (rendered when the brain's live adapter config exists; not part of the kit id,
  so the hub kit stays installed = rendered until the cut-over):
    compose.tg.yaml       ops/hub/compose.tg.yaml (override: hub's files mount, the tg service)
    loader/tgloader.ts    ops/hub/tgloader.ts
    tg/live.json          the hub adapter's config (ops/hub/tg-live.json + the bot name and send flag of the brain's
                          ~/.config/tg-adapter/live.json; no secrets)
    tg-install.sh         the tg paste: dirs, token + allowlist copied by name from the brain's home, config, override
                          and loader, hub recreated with the files mount, tg started; refuses while the brain's own
                          live adapter is on or running
    tg-rollback.sh        stops and removes claude-hub-tg (data and the token copy stay)
The kit id changes only when compose, loader, values or the credential names change: hub.env carries it, so a
paste recreates the hub containers exactly when the kit changed, and /health reports it (kit drift check).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import tomllib
from pathlib import Path

from . import CONTAINERS, HubError, kit_dir, secrets_list_file, settings

PASTE_MAX = 25
TEMPLATES = {"install.sh": "ops/hub/install.sh", "rollback.sh": "ops/hub/rollback.sh", "card.txt": "ops/hub/card.txt"}
COMPOSE = "ops/hub/compose.yaml"
LOADER = "packages/hub/loader.py"
PINS = "ops/hub/pins.toml"
DEST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SRC = re.compile(r"^\.?[A-Za-z0-9_][A-Za-z0-9._-]*(?:/\.?[A-Za-z0-9_][A-Za-z0-9._-]*)*$")
RESERVED = {"pg_password", "token_brain", "token_tg", "tg_live_token"}
TG_FILES = {"compose.tg.yaml": "ops/hub/compose.tg.yaml", "loader/tgloader.ts": "ops/hub/tgloader.ts"}
TG_TEMPLATES = {"tg-install.sh": "ops/hub/tg-install.sh", "tg-rollback.sh": "ops/hub/tg-rollback.sh"}
TG_CONFIG = "ops/hub/tg-live.json"
# The voice relay in the hub (rendered when the brain has the relay installed and a live tg adapter config
# names the allowlist file; not part of the kit id, like tg):
#   compose.voice.yaml, loader/voiceloader.py, voice/live.json, voice-install.sh (the voice paste: key files copied
#   BY NAME from the brain's home, refuses while the in-container relay may run), voice-rollback.sh,
#   voice-relogin.sh (the call account's login again INSIDE claude-hub-voice: `run.py tg login` through docker exec,
#   the code typed at a prompt and piped in, then one restart; nothing copied from the brain; voice-relogin.md)
VOICE_FILES = {"compose.voice.yaml": "ops/hub/compose.voice.yaml", "loader/voiceloader.py": "ops/hub/voiceloader.py"}
VOICE_TEMPLATES = {"voice-install.sh": "ops/hub/voice-install.sh", "voice-rollback.sh": "ops/hub/voice-rollback.sh",
                   "voice-relogin.sh": "ops/hub/voice-relogin.sh"}  # fmt: skip
VOICE_CONFIG = "ops/hub/voice-live.json"
# the relay's key files, relative to the brain's home (names only; the paste checks and copies them)
VOICE_KEYS = {
    "GEMINI_SRC": ".secrets/gemini-api-key",
    "TGJSON_SRC": ".secrets/voice-tg.json",
    "ENV_SRC": ".config/voice-relay.env",
    "SESSION_SRC": ".secrets/voice-tg.session",
    "LINK_SRC": ".secrets/voice-link-token",  # phone gateway <-> relay link token (`hub voice link prep` makes it)
}
VOICE_HOME = "opt/voice-relay"  # the in-container relay: no relay here = nothing to move
# The phone gateway in the hub (rendered when the brain has the in-container gateway and its config; not part of
# the kit id, like voice):
#   compose.phone.yaml (the front's host port filled in), loader/phoneloader.py, phone/live.json, phone/ports.py (the
#   port move: claude-bot's compose override), phone-install.sh (key files copied BY NAME, refuses while the
#   in-container gateway may run), phone-rollback.sh (the port back to claude-bot)
PHONE_FILES = {"loader/phoneloader.py": "ops/hub/phoneloader.py", "phone/ports.py": "ops/hub/phone-ports.py"}
PHONE_COMPOSE = "ops/hub/compose.phone.yaml"
PHONE_TEMPLATES = {"phone-install.sh": "ops/hub/phone-install.sh", "phone-rollback.sh": "ops/hub/phone-rollback.sh"}
PHONE_CONFIG = "ops/hub/phone-live.json"
PHONE_HOME = "opt/phonegw"  # the in-container gateway: none here = nothing to move
# the gateway's key files and state, relative to the brain's home (names only; the paste checks and copies them)
PHONE_PKI = tuple(
    f".secrets/phonegw/{n}" for n in ("ca.key", "ca.pem", "server.key", "server.pem", "public.key", "public.pem")
)
PHONE_KEYS = {
    "CLI_TOKEN_SRC": ".secrets/phone-cli-token",  # `hub phone token prep`: the brain CLI's own internal-API token
    "LINK_SRC": ".secrets/voice-link-token",  # the voice link token: app calls through claude-hub-voice:8797
    "GWCONF_SRC": ".config/phonegw/config.json",
    "GWSTATE_SRC": ".local/state/phonegw",
}
PHONE_IMAGE_SERVICE = "phone"
# His headed Chrome in claude-hub-browser (rendered when the source tree has the browser kit and the brain has its
# Chrome profile; not part of the kit id, like voice):
#   compose.browser.yaml, loader/browserloader.py, browser/live.json, browser/build/Dockerfile (the image, built on the
#   server), browser/browser.env (image tag, base and Chrome pins; the paste adds the home's uid/gid),
#   browser-install.sh (profile COPIED once, never moved; refuses while a Chrome in the brain runs on it)
BROWSER_FILES = {"compose.browser.yaml": "ops/hub/compose.browser.yaml", "loader/browserloader.py":
                 "ops/hub/browserloader.py", "browser/build/Dockerfile": "ops/hub/browser/Dockerfile"}  # fmt: skip
BROWSER_TEMPLATES = {"browser-install.sh": "ops/hub/browser-install.sh"}
BROWSER_CONFIG = "ops/hub/browser-live.json"
BROWSER_PINS = "ops/hub/browser-pins.toml"
BROWSER_KEYS = {
    "PROFILE_SRC": ".local/share/browser/profile",  # his Chrome profile in the brain's home (copied, never moved)
    "CTL_SRC": ".secrets/browser-ctl-token",  # `browser-hub prep`: the brain's control-API token
    "VNC_SRC": ".secrets/novnc.txt",  # the VNC password he types into noVNC
}
BROWSER_VIEW = {"novnc_host": "claude-hub-browser", "novnc_port": 6080}  # the phone front's view target
BOT_NAME = re.compile(r"^@?[A-Za-z0-9_]{3,64}$")
MARK = re.compile(r"@@[A-Z_]+@@")


def secrets_list() -> list[tuple[str, str]]:
    """Validated (dest name, path relative to the brain's home) pairs; # comments and blank lines skipped."""
    f = secrets_list_file()
    if not f.is_file():
        return []
    out, seen = [], set()
    for n, raw in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 2:
            raise HubError(f"{f}:{n}: want 'DEST_NAME SRC_PATH' (two fields)")
        d, s = parts
        if not DEST.match(d) or d in RESERVED or d.startswith("token_") or d in seen:
            raise HubError(f"{f}:{n}: bad or duplicate DEST_NAME (plain name, not token_*, not {sorted(RESERVED)})")
        if not SRC.match(s) or ".." in s.split("/") or s.startswith("/"):
            raise HubError(f"{f}:{n}: SRC_PATH must be relative to the home, plain names, no '..'")
        seen.add(d)
        out.append((d, s))
    return out


def tg_values() -> tuple[dict[str, str], str]:
    """The tg paste's values from the brain's live adapter config (key values only; never printed): the bot name, the
    send flag and the token and allowlist files as paths relative to the brain's home. ({}, why) when it can't."""
    from core import paths

    try:
        cfg = json.loads((paths.home() / ".config" / "tg-adapter" / "live.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, "no readable live adapter config (~/.config/tg-adapter/live.json)"
    home = os.path.normpath(str(paths.home()))
    out = {}
    for key, mark in (("token_file", "TOKEN_SRC"), ("allow_from_file", "ACCESS_SRC")):
        v = cfg.get(key)
        if not isinstance(v, str) or not v:
            return {}, f"live.json has no {key}"
        p = os.path.normpath(os.path.join(home, v[2:]) if v.startswith("~/") else v)
        if not p.startswith(home + os.sep):
            return {}, f"live.json {key} is outside the home: the paste can't copy it"
        rel = p[len(home) + 1 :]
        if not SRC.match(rel) or ".." in rel.split("/"):
            return {}, f"live.json {key}: not a plain path"
        out[mark] = rel
    bot = cfg.get("bot")
    if not isinstance(bot, str) or not BOT_NAME.match(bot):
        return {}, "live.json has no usable bot name"
    out["TG_BOT"] = bot
    out["TG_SEND"] = "false" if cfg.get("send") is False else "true"
    return out, ""


def build_tg(src: Path, values: dict[str, str]) -> tuple[dict[str, bytes], dict]:
    """The tg kit files + their info (state "rendered", or "not rendered" with why)."""
    if not all((src / v).is_file() for v in (*TG_FILES.values(), *TG_TEMPLATES.values(), TG_CONFIG)):
        return {}, {"state": "not rendered", "why": "source tree has no tg kit"}
    tv, why = tg_values()
    if not tv:
        return {}, {"state": "not rendered", "why": why}
    files = {k: (src / v).read_bytes() for k, v in TG_FILES.items()}
    files["tg/live.json"] = fill((src / TG_CONFIG).read_text(encoding="utf-8"), tv).encode()
    json.loads(files["tg/live.json"])  # a broken template fails the render, never the paste
    blob = files["compose.tg.yaml"] + files["loader/tgloader.ts"] + files["tg/live.json"]
    vals = {
        **values,
        **tv,
        "TGSUM": _sha(blob)[:16],
        "TGKIT": _sha(blob + (tv["TOKEN_SRC"] + tv["ACCESS_SRC"]).encode())[:12],
    }
    for v in vals.values():
        if "'" in v or "\n" in v:
            raise HubError("a tg kit value holds a quote or newline: refusing to render a paste")
    lines = {}
    for k, t in TG_TEMPLATES.items():
        text = fill((src / t).read_text(encoding="utf-8"), vals)
        lines[k] = len(text.rstrip("\n").splitlines())
        if lines[k] > PASTE_MAX:
            raise HubError(f"{k} has {lines[k]} lines (max {PASTE_MAX})")
        files[k] = text.encode()
    return files, {"state": "rendered", "kit": vals["TGKIT"], "paste_lines": lines["tg-install.sh"],
                   "rollback_lines": lines["tg-rollback.sh"]}  # fmt: skip


def build_voice(src: Path, values: dict[str, str]) -> tuple[dict[str, bytes], dict]:
    """The voice kit files + their info (state "rendered", or "not rendered" with why)."""
    from core import paths

    if not all((src / v).is_file() for v in (*VOICE_FILES.values(), *VOICE_TEMPLATES.values(), VOICE_CONFIG)):
        return {}, {"state": "not rendered", "why": "source tree has no voice kit"}
    if not (paths.home() / VOICE_HOME).is_dir():
        return {}, {"state": "not rendered", "why": "no in-container voice relay (~/opt/voice-relay)"}
    tv, why = tg_values()
    if not tv:
        return {}, {"state": "not rendered", "why": f"the owner's allowlist file: {why}"}
    files = {k: (src / v).read_bytes() for k, v in VOICE_FILES.items()}
    files["voice/live.json"] = (src / VOICE_CONFIG).read_bytes()
    json.loads(files["voice/live.json"])
    blob = files["compose.voice.yaml"] + files["loader/voiceloader.py"] + files["voice/live.json"]
    keys = {**VOICE_KEYS, "ACCESS_SRC": tv["ACCESS_SRC"]}
    vals = {
        **values,
        **keys,
        "VOICESUM": _sha(blob)[:16],
        "VOICEKIT": _sha(blob + "".join(sorted(keys.values())).encode())[:12],
    }
    for v in vals.values():
        if "'" in v or "\n" in v:
            raise HubError("a voice kit value holds a quote or newline: refusing to render a paste")
    lines = {}
    for k, t in VOICE_TEMPLATES.items():
        text = fill((src / t).read_text(encoding="utf-8"), vals)
        lines[k] = len(text.rstrip("\n").splitlines())
        if lines[k] > PASTE_MAX:
            raise HubError(f"{k} has {lines[k]} lines (max {PASTE_MAX})")
        files[k] = text.encode()
    return files, {"state": "rendered", "kit": vals["VOICEKIT"], "paste_lines": lines["voice-install.sh"],
                   "rollback_lines": lines["voice-rollback.sh"], "relogin_lines": lines["voice-relogin.sh"],
                   "keys": sorted(keys)}  # fmt: skip


def phone_ports(home: Path) -> tuple[int, ...] | None:
    """The host ports the phone gateway takes over from claude-bot: the front alone (front_port in the brain's gateway
    config, default 6080; only that number is read). The public route's port (public.port) is not one of them: it is
    the web VM's nginx stream listener, which forwards to the front. None when the config is missing or unreadable."""
    try:
        cfg = json.loads((home / PHONE_KEYS["GWCONF_SRC"]).read_text(encoding="utf-8"))
        ports = (int(cfg.get("front_port", 6080)),)
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    return ports if all(0 < p < 65536 for p in ports) else None


def phone_image(src: Path) -> str:
    for p in tomllib.loads((src / PINS).read_text(encoding="utf-8")).get("image", []):
        if p.get("services") == [PHONE_IMAGE_SERVICE]:
            return f"{p['ref']}@{p['digest']}"
    raise HubError("pins.toml: no image for the phone service")


def build_phone(src: Path, values: dict[str, str]) -> tuple[dict[str, bytes], dict]:
    """The phone kit files + their info (state "rendered", or "not rendered" with why)."""
    from core import paths

    need = (*PHONE_FILES.values(), PHONE_COMPOSE, *PHONE_TEMPLATES.values(), PHONE_CONFIG)
    if not all((src / v).is_file() for v in need):
        return {}, {"state": "not rendered", "why": "source tree has no phone kit"}
    home = paths.home()
    if not (home / PHONE_HOME).is_dir():
        return {}, {"state": "not rendered", "why": "no in-container phone gateway (~/opt/phonegw)"}
    ports = phone_ports(home)
    if ports is None:
        return {}, {"state": "not rendered", "why": "the gateway config is missing or unreadable"}
    pv = {"FRONT_PORT": str(ports[0])}
    files = {k: (src / v).read_bytes() for k, v in PHONE_FILES.items()}
    files["compose.phone.yaml"] = fill((src / PHONE_COMPOSE).read_text(encoding="utf-8"), pv).encode()
    files["phone/live.json"] = (src / PHONE_CONFIG).read_bytes()
    pl = json.loads(files["phone/live.json"])
    pl.setdefault("gw", {}).update(BROWSER_VIEW)  # his 🖥 view lives in claude-hub-browser
    files["phone/live.json"] = (json.dumps(pl, indent=2, ensure_ascii=False) + "\n").encode()
    blob = (
        files["compose.phone.yaml"]
        + files["loader/phoneloader.py"]
        + files["phone/live.json"]
        + files["phone/ports.py"]
    )
    keys = {**PHONE_KEYS, "PKI_FILES": " ".join(f"'{f}'" for f in PHONE_PKI)}
    vals = {
        **values,
        **PHONE_KEYS,
        **pv,
        "PY_IMAGE": phone_image(src),
        "PHONESUM": _sha(blob)[:16],
        "PHONEKIT": _sha(blob + "".join(sorted(keys.values())).encode())[:12],
    }
    for v in vals.values():
        if "'" in v or "\n" in v:
            raise HubError("a phone kit value holds a quote or newline: refusing to render a paste")
    vals["PKI_FILES"] = keys["PKI_FILES"]  # quoted on purpose (a list of names)
    lines = {}
    for k, t in PHONE_TEMPLATES.items():
        text = fill((src / t).read_text(encoding="utf-8"), vals)
        lines[k] = len(text.rstrip("\n").splitlines())
        if lines[k] > PASTE_MAX:
            raise HubError(f"{k} has {lines[k]} lines (max {PASTE_MAX})")
        files[k] = text.encode()
    return files, {"state": "rendered", "kit": vals["PHONEKIT"], "paste_lines": lines["phone-install.sh"],
                   "rollback_lines": lines["phone-rollback.sh"], "ports": list(ports),
                   "keys": sorted([*PHONE_KEYS, "PKI_FILES"])}  # fmt: skip


def browser_env(src: Path, kitsum: str) -> bytes:
    pins = tomllib.loads((src / BROWSER_PINS).read_text(encoding="utf-8"))
    b, c = pins["base"], pins["chrome"]
    for v in (b["ref"], b["digest"], c["version"], c["sha256"]):
        if not re.fullmatch(r"[A-Za-z0-9._:/@-]{1,200}", str(v)):
            raise HubError("browser-pins.toml: a value with odd characters")
    tag = re.sub(r"[^A-Za-z0-9_.-]", "-", f"{c['version']}-{kitsum[:8]}")
    return (f"# claude-hub-browser image inputs (ops/hub/browser-pins.toml); the paste appends BROWSER_UID/GID.\n"
            f"BROWSER_TAG={tag}\nBROWSER_BASE={b['ref']}@{b['digest']}\nCHROME_VERSION={c['version']}\n"
            f"CHROME_SHA256={c['sha256']}\n").encode()  # fmt: skip


def build_browser(src: Path, values: dict[str, str]) -> tuple[dict[str, bytes], dict]:
    """The browser kit files + their info (state "rendered", or "not rendered" with why)."""
    from core import paths

    need = (*BROWSER_FILES.values(), *BROWSER_TEMPLATES.values(), BROWSER_CONFIG, BROWSER_PINS)
    if not all((src / v).is_file() for v in need):
        return {}, {"state": "not rendered", "why": "source tree has no browser kit"}
    if not (paths.home() / BROWSER_KEYS["PROFILE_SRC"]).is_dir():
        return {}, {"state": "not rendered", "why": "no Chrome profile in the brain's home (~/.local/share/browser)"}
    files = {k: (src / v).read_bytes() for k, v in BROWSER_FILES.items()}
    files["browser/live.json"] = (src / BROWSER_CONFIG).read_bytes()
    json.loads(files["browser/live.json"])
    pre = files["compose.browser.yaml"] + files["loader/browserloader.py"] + files["browser/live.json"]
    files["browser/browser.env"] = browser_env(src, _sha(pre + files["browser/build/Dockerfile"]))
    blob = pre + files["browser/build/Dockerfile"] + files["browser/browser.env"]
    vals = {
        **values,
        **BROWSER_KEYS,
        "BROWSERSUM": _sha(blob)[:16],
        "BROWSERKIT": _sha(blob + "".join(sorted(BROWSER_KEYS.values())).encode())[:12],
    }
    for v in vals.values():
        if "'" in v or "\n" in v:
            raise HubError("a browser kit value holds a quote or newline: refusing to render a paste")
    lines = {}
    for k, t in BROWSER_TEMPLATES.items():
        text = fill((src / t).read_text(encoding="utf-8"), vals)
        lines[k] = len(text.rstrip("\n").splitlines())
        if lines[k] > PASTE_MAX:
            raise HubError(f"{k} has {lines[k]} lines (max {PASTE_MAX})")
        files[k] = text.encode()
    pins = tomllib.loads((src / BROWSER_PINS).read_text(encoding="utf-8"))
    return files, {"state": "rendered", "kit": vals["BROWSERKIT"], "paste_lines": lines["browser-install.sh"],
                   "chrome": pins["chrome"]["version"], "build_mb": int(pins["chrome"].get("size_mb", 0)),
                   "keys": sorted(BROWSER_KEYS)}  # fmt: skip


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def pull_mb(src: Path) -> int:
    """Compressed MB the install pulls (pins of the services the paste starts: everything but the tg profile)."""
    try:
        pins = tomllib.loads((src / PINS).read_text(encoding="utf-8")).get("image", [])
    except (OSError, ValueError):
        return 0
    return sum(int(p.get("size_mb", 0)) for p in pins if p.get("services") not in (["tg"], ["voice"], ["phone"]))


def fill(text: str, values: dict[str, str]) -> str:
    out = MARK.sub(lambda m: values.get(m.group(0)[2:-2], m.group(0)), text)
    left = MARK.findall(out)
    if left:
        raise HubError(f"template markers left unfilled: {', '.join(sorted(set(left)))}")
    return out


def build(src: Path, rev: str) -> dict[str, bytes | str | dict]:
    """The kit's files in memory: {name: bytes} plus 'info'."""
    s = settings()
    try:
        compose = (src / COMPOSE).read_bytes()
        loader = (src / LOADER).read_bytes()
        templates = {k: (src / v).read_text(encoding="utf-8") for k, v in TEMPLATES.items()}
    except OSError as e:
        raise HubError(f"source tree incomplete: {e.filename}") from None
    sec = "".join(f"{d} {p}\n" for d, p in secrets_list()).encode()
    loader_id = _sha(loader)[:12]
    ci = ci_image_files(src)
    ci_bytes = b"".join(ci[k] for k in sorted(ci))  # the paste's checksum cats them in this (LC_ALL=C sort) order
    core = (
        f"HUB_ROOT={s['hub_root']}\nBOT_HOME={s['bot_home']}\nBRAIN_NETWORK={s['brain_network']}\nTZ={s['tz']}\n"
        f"HUB_LOADER={loader_id}\n"
    )
    kit_id = _sha(compose + loader + sec + core.encode() + ci_bytes)[:12]
    env = (
        "# claude-hub settings, rendered by `hub kit render` / `assistant deploy --target hub` from the profile.\n"
        "# No secrets here: those are files in HUB_ROOT/secrets.\n" + core + f"HUB_KIT={kit_id}\n"
    ).encode()
    kitsum = _sha(compose + env + loader + sec + ci_bytes)[:16]
    values = {
        "KIT": kit_id,
        "REV": rev[:12],
        "KIT_DIR": s["bot_home"] + "/.local/share/claude-hub/kit",
        "HUB_ROOT": s["hub_root"],
        "BOT_HOME": s["bot_home"],
        "BRAIN_NETWORK": s["brain_network"],
        "KITSUM": kitsum,
        "CONTAINERS": " ".join(CONTAINERS),
        "PULL_MB": str(pull_mb(src)),
    }
    for v in values.values():
        if "'" in v or "\n" in v:
            raise HubError("a kit value holds a quote or newline: refusing to render a paste")
    rendered = {k: fill(t, values) for k, t in templates.items()}
    lines = len(rendered["install.sh"].rstrip("\n").splitlines())
    if lines > PASTE_MAX:
        raise HubError(f"install paste has {lines} lines (max {PASTE_MAX})")
    info = {
        "kit": kit_id,
        "loader": loader_id,
        "rev": rev,
        "kitsum": kitsum,
        "rendered": round(time.time(), 3),
        "profile": s["source"],
        "defaulted": s["defaulted"],
        "paste_lines": lines,
        "rollback_lines": len(rendered["rollback.sh"].rstrip("\n").splitlines()),
        "credentials": len(sec.splitlines()),
        "pull_mb": int(values["PULL_MB"]),
    }
    tg_files, info["tg"] = build_tg(src, values)
    voice_files, info["voice"] = build_voice(src, values)
    phone_files, info["phone"] = build_phone(src, values)
    browser_files, info["browser"] = build_browser(src, values)
    return {
        **browser_files,
        **tg_files,
        **voice_files,
        **phone_files,
        "compose.yaml": compose,
        **ci,
        "hub.env": env,
        "loader/hubloader.py": loader,
        "secrets.copy": sec,
        "install.sh": rendered["install.sh"].encode(),
        "rollback.sh": rendered["rollback.sh"].encode(),
        "card.txt": rendered["card.txt"].encode(),
        "info": info,
    }


def ci_image_files(src: Path) -> dict:
    """claude-hub-ci's build context (compose.yaml builds the ci image on the server: the pinned python base + the
    heavy media/ML tools for the legacy suites): ci-image/build/{Dockerfile,requirements/*}. The install paste copies
    them to HUB_ROOT/ci-image/build (compose's build context) and checks them with the rest of the kit (kitsum)."""
    d = src / "ops/hub/ci"
    if not (d / "Dockerfile").is_file():
        return {}
    out = {"ci-image/build/Dockerfile": (d / "Dockerfile").read_bytes()}
    for f in sorted((d / "requirements").iterdir()):
        out[f"ci-image/build/requirements/{f.name}"] = f.read_bytes()
    return out


def write(files: dict, out: Path | None = None) -> Path:
    """Write the kit atomically enough for a paste: kit.new filled, kit -> kit.prev, kit.new -> kit."""
    out = out or kit_dir()
    new = out.with_name(out.name + ".new")
    prev = out.with_name(out.name + ".prev")
    if new.exists():
        shutil.rmtree(new)
    for name, data in files.items():
        if name == "info":
            continue
        q = new / name
        q.parent.mkdir(parents=True, exist_ok=True)
        q.write_bytes(data)  # type: ignore[arg-type]
        os.chmod(q, 0o644)
    (new / "kit.json").write_text(json.dumps(files["info"], indent=1, sort_keys=True) + "\n", encoding="utf-8")
    for dp, dns, _f in os.walk(new):
        os.chmod(dp, 0o755)
        for dn in dns:
            os.chmod(os.path.join(dp, dn), 0o755)
    if out.exists():
        if prev.exists():
            shutil.rmtree(prev)
        os.replace(out, prev)
    os.replace(new, out)
    return out


def render(src: Path, rev: str, out: Path | None = None) -> dict:
    files = build(src, rev)
    write(files, out)
    return files["info"]  # type: ignore[return-value]


def info(out: Path | None = None) -> dict | None:
    try:
        return json.loads(((out or kit_dir()) / "kit.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def archive(repo: Path, rev: str, dest: Path) -> str:
    """git archive of rev into dest (a fresh dir) -> the full sha."""
    r = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--end-of-options", rev + "^{commit}"],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        raise HubError(f"unknown revision {rev!r} in {repo}", 2)
    sha = r.stdout.strip()
    dest.mkdir(parents=True)
    a = subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", sha], capture_output=True)
    if a.returncode != 0:
        raise HubError("git archive failed")
    x = subprocess.run(["tar", "-x", "-C", str(dest)], input=a.stdout, capture_output=True)
    if x.returncode != 0:
        raise HubError("tar -x failed")
    return sha


def render_rev(repo: Path, rev: str = "main", out: Path | None = None) -> dict:
    with tempfile.TemporaryDirectory(prefix="hubkit-") as t:
        src = Path(t) / "src"
        sha = archive(repo, rev, src)
        return render(src, sha, out)
