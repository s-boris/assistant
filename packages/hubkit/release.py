"""hubkit.release: hub releases on the brain side. Stdlib only.

build(src, rid, sha)   RELEASES/<rid>/{app/*, app/pg/*.sql, vendor/*, tg/adapter.js + tg/tools.toml, release.json}:
                       copied from a source tree (a git
                       archive of main) plus the pinned wheels of ops/hub/vendor.toml (hubkit.vendor),
                       staged and renamed into place; an existing complete release is reused as it is when it
                       carries every entry this build would (optional()); one that lacks some of them gets
                       the missing trees added in place (_amend: nothing it has changes, release.json last)
verify(rid)            None when complete (every listed file present with its sha256), else the reason (the
                       loader checks the same before it runs a release)
runnable(rid, role)    None when rid verifies and carries role's entry (what that role's loader runs), else why
read_current()         (rid, line) from RELEASES/CURRENT, or None
write_current(rid)     CURRENT = "<rid> <attempt>" (atomic); a new attempt lets the loader retry an id it marked bad
restore_current(line)  put an earlier CURRENT line back (deploy undo)
kept() / prune(keep)   release ids newest first / remove all but the newest N plus `keep`
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
from pathlib import Path

from . import HubError, citool, releases_dir, vendor

FORMAT = 1
RID = re.compile(r"^[0-9a-f]{7,40}$")
NOW = ":now"  # CURRENT attempt suffix (voiceloader.NOW): switch the voice relay even during a live call
# release path -> source path in the repo tree
FILES = {
    "app/hubd.py": "packages/hub/hubd.py",
    "app/hubstore.py": "packages/hub/hubstore.py",
    "app/ciworker.py": "packages/hub/ciworker.py",
    "app/hubci.py": "packages/hub/hubci.py",
    "app/hubfeed.py": "packages/hub/hubfeed.py",  # the status feed
    "app/hubtg.py": "packages/hub/hubtg.py",  # the Telegram transport
    "app/hubvoice.py": "packages/hub/hubvoice.py",  # the voice relay's hold and events
    "app/hubphone.py": "packages/hub/hubphone.py",  # the phone gateway's hold and events
    "app/hubbrowser.py": "packages/hub/hubbrowser.py",  # claude-hub-browser's approval read + commit receipt
    "app/hubboard.py": "packages/hub/hubboard.py",  # the board's records (board_items, board_log)
    "app/hubwfapi.py": "packages/hub/hubwfapi.py",  # the workflow engine's switch and API (stdlib)
    "app/hubwf.py": "packages/hub/hubwf.py",  # the DBOS workflows and schedules
    # protected (hubverify.PROTECTED): a change loads only after his ✅ on the set's digest
    "app/hubverify.py": "packages/hub/hubverify.py",
    "app/hubsecrets.py": "packages/hub/hubsecrets.py",
    "app/hubapprovals.py": "packages/hub/hubapprovals.py",
    # "app/hubredact.py" (protected) joins the release through H10_FILES below
    # the runner's CI driver: main's `assistant ci` and core when the release was built (never the job's own)
    "app/ci/assistant.py": "ops/assistant.py",
}
# The hub's secret store, result redaction and fingerprint export (packages/hub/hubredact.py, a PROTECTED path): a
# change to it changes the release's protected digest, which needs his ✅ (assistant deploy --target hub exits 8 and
# requests it).
H10_IN_RELEASE = True
H10_FILES = {"app/hubredact.py": "packages/hub/hubredact.py"}
# The egress injector (packages/hub/hubpolicy_egress.py + its bindings packages/hub/policy/egress.toml, both
# PROTECTED: app/hubpolicy*, app/policy/*): a change to either needs his ✅ on the release's digest. A release without
# them makes hubd answer /egress/* with 503 egress_off.
EGRESS_IN_RELEASE = True
EGRESS_FILES = {
    "app/hubpolicy_egress.py": "packages/hub/hubpolicy_egress.py",
    "app/policy/egress.toml": "packages/hub/policy/egress.toml",
}
CI_CORE = ("core", "app/ci/lib/core")  # every core/*.py -> app/ci/lib/core/
SQL_DIR = "schemas/pg"  # every *.sql here -> app/pg/ (hubstore's forward-only migrations)
# The gateway: the typed tool registry, its Cedar policy and tools.hubgw, imported by hubd as tools.* from app/.
# (release dir -> source dir, file patterns); the brain-only modules (CLI, docs generator, executor) stay out.
TREES = {
    "app/tools": ("packages/tools", ("*.py", "*.cedar")),
    "app/tools/domains": ("packages/tools/domains", ("*.py",)),
}
TREE_SKIP = {"cli.py", "gen.py", "worker.py"}
ENTRY = {"hub": "app/hubd.py", "ci": "app/ciworker.py"}
KEEP = 3
# The Telegram adapter for claude-hub-tg. ops/hub/tgloader.ts runs tg/adapter.js: adapters/tg/src/cli.ts as ONE
# `bun build --target=bun` bundle (grammy and the DDL files inlined; no node_modules in a release, so every release
# path keeps the hub loader's segment rule), plus tg/tools.toml (the bindings registry; tgloader passes it as
# TG_ADAPTER_TOOLS). Built in a scratch copy (src, package.json, bun.lock and a real copy of the node_modules bun
# installed from that lock: HUB_TG_NODE_MODULES, else the tree's own, else the repo checkout's), so the bundle names
# no build path and the same inputs give the same bytes. Versions that don't match bun.lock stop the build, and so does
# a missing bun (HUB_TG_BUN, else PATH, else ~/.bun/bin/bun). release.json "tg" = {bun, lock_sha256, packages}.
TG_SRC = "adapters/tg"
TG_ENTRY = "tg/adapter.js"
TG_TOOLS = "tg/tools.toml"
TG_SKIP_DIRS = {".bin", ".cache"}
TG_IN_RELEASE = True
# The voice relay for claude-hub-voice. ops/hub/voiceloader.py runs voice/run.py (adapters/voice/run.py) with voice/app
# = the relay (voice_relay/*.py, prompts, static) and voice/vendor = the runtime wheels of ops/hub/voice-vendor.toml
# (cp311, the image's python). In every release, like tg: claude-hub-voice's loader runs CURRENT's voice/run.py, and a
# release without it leaves the loader idle ('no voice entry', then 'CURRENT bad, no last good'). A release's content
# follows from its commit, whose id names it. A source tree without the relay (adapters/voice/run.py; tests' partial
# trees) builds without it.
VOICE_SRC = "adapters/voice"
VOICE_ENTRY = "voice/run.py"
VOICE_PINS = "ops/hub/voice-vendor.toml"
VOICE_TREES = {
    "voice/app/voice_relay": ("voice_relay", "*.py"),
    "voice/app/prompts": ("prompts", "*.md"),
    "voice/app/static": ("static", "*"),
}
VOICE_IN_RELEASE = True


def voice_sources(src: Path) -> dict[str, Path]:
    """Release path -> source file of the relay's tree (no tests, no bin, no lock); {} for a tree without the relay."""
    base = src / VOICE_SRC
    if not (base / "run.py").is_file():
        return {}
    out = {VOICE_ENTRY: base / "run.py"}
    for rdir, (sdir, pat) in VOICE_TREES.items():
        for f in sorted((base / sdir).glob(pat)):
            if f.is_file():
                out[f"{rdir}/{f.name}"] = f
    return out


# The phone gateway for claude-hub-phone. ops/hub/phoneloader.py runs phone/run.py (adapters/phone/run.py) with
# phone/app = the gateway (phonegw/*.py), the app protocol's schemas (adapters/phone/protocol/schema, a snapshot) and
# the lock-screen filter (adapters/voice/voice_relay/answer_filter.py, which phonegw.lint loads by path), and
# phone/vendor = the runtime wheels of ops/hub/phone-vendor.toml (cp311). In every release, like voice and tg:
# claude-hub-phone's loader runs CURRENT's phone/run.py, and a release without it is marked bad there ('no phone entry':
# the last good release runs on, or with none the loader idles). claude-hub-phone starts only with the owner's phone
# paste, and ~/bin/phone-produce (the producers' code from CURRENT) is called only by that container. A source tree
# without the gateway builds without it.
PHONE_SRC = "adapters/phone"
PHONE_ENTRY = "phone/run.py"
PHONE_PINS = "ops/hub/phone-vendor.toml"
PHONE_TREES = {
    "phone/app/phonegw": ("phonegw", "*.py"),
    "phone/app/protocol/schema": ("protocol/schema", "*.json"),
}
PHONE_FILTER = ("phone/app/answer_filter.py", "adapters/voice/voice_relay/answer_filter.py")
PHONE_IN_RELEASE = True


def phone_sources(src: Path) -> dict[str, Path]:
    """Release path -> source file of the gateway's tree (no tests, no bin, no lock); {} for a tree without it."""
    base = src / PHONE_SRC
    if not (base / "run.py").is_file():
        return {}
    out = {PHONE_ENTRY: base / "run.py", PHONE_FILTER[0]: src / PHONE_FILTER[1]}
    for rdir, (sdir, pat) in PHONE_TREES.items():
        for f in sorted((base / sdir).glob(pat)):
            if f.is_file():
                out[f"{rdir}/{f.name}"] = f
    return out


# The owner's headed Chrome in claude-hub-browser. ops/hub/browserloader.py runs browser/run.py
# (adapters/browser/run.py) with browser/app = browsersvc (packages/browsersvc, the CDP gate) and hubgate (the
# container's token check). Stdlib only: no wheels. In every release, like voice (a kept release of the same commit
# without it gets the tree added by _amend): inert until the owner's browser paste starts claude-hub-browser, whose
# loader idles on a release without it ('no browser entry'). A source tree without adapters/browser/run.py (tests'
# partial trees) builds without it.
BROWSER_ENTRY = "browser/run.py"
BROWSER_FILES = {
    BROWSER_ENTRY: "adapters/browser/run.py",
    "browser/app/hubgate.py": "adapters/browser/hubgate.py",
    "browser/app/browsersvc/__init__.py": "packages/browsersvc/__init__.py",
}
BROWSER_IN_RELEASE = True


def browser_sources(src: Path) -> dict[str, Path]:
    """Release path -> source file of the browser tree; {} for a tree without it."""
    if not (src / BROWSER_FILES[BROWSER_ENTRY]).is_file():
        return {}
    return {rel: src / s for rel, s in BROWSER_FILES.items()}


TG_BUILD_TIMEOUT = 180


def _lock_versions(lock: Path) -> dict[str, str]:
    """bun.lock (JSONC: trailing commas) -> {package: version}."""
    import re as _re

    text = _re.sub(r",(\s*[}\]])", r"\1", lock.read_text(encoding="utf-8"))
    try:
        pk = json.loads(text).get("packages") or {}
    except ValueError:
        raise HubError(f"tg: {lock.name} is not readable JSON") from None
    out = {}
    for name, spec in pk.items():
        if isinstance(spec, list) and spec and isinstance(spec[0], str) and "@" in spec[0][1:]:
            out[name] = spec[0].rsplit("@", 1)[1]
    return out


def tg_node_modules(src: Path) -> Path:
    env = os.environ.get("HUB_TG_NODE_MODULES")
    if env:
        return Path(env)
    own = src / TG_SRC / "node_modules"
    if own.is_dir():
        return own
    from core import paths

    return paths.repo(TG_SRC, "node_modules")


def tg_bun() -> str:
    env = os.environ.get("HUB_TG_BUN")
    if env:
        return env
    found = shutil.which("bun")
    if found:
        return found
    home = Path.home() / ".bun" / "bin" / "bun"
    if home.is_file():
        return str(home)
    raise HubError("tg: bun is missing (tg/adapter.js is a bun bundle): install bun or set HUB_TG_BUN")


def _copy_modules(nm: Path, dest: Path) -> None:
    """A real copy of node_modules (symlinks skipped: bun names a link's target path in the bundle's comments)."""
    for dp, dns, fns in os.walk(nm):
        dns[:] = sorted(d for d in dns if d not in TG_SKIP_DIRS and not os.path.islink(os.path.join(dp, d)))
        rel = Path(dp).relative_to(nm)
        (dest / rel).mkdir(parents=True, exist_ok=True)
        for fn in sorted(fns):
            f = Path(dp) / fn
            if f.is_file() and not f.is_symlink():
                shutil.copyfile(f, dest / rel / fn)


def tg_sources(src: Path, work: Path) -> tuple[dict[str, Path], dict]:
    """(release path -> built or source file, provenance) of the tg tree; ({}, {}) when the tree has no adapter."""
    base = src / TG_SRC
    if not (base / "src" / "cli.ts").is_file():
        return {}, {}
    for n in ("package.json", "bun.lock"):
        if not (base / n).is_file():
            raise HubError(f"tg: {TG_SRC}/{n} missing")
    if not (src / "schemas" / "tools.toml").is_file():
        raise HubError("tg: schemas/tools.toml missing")
    nm = tg_node_modules(src)
    want = _lock_versions(base / "bun.lock")
    for name, ver in want.items():
        try:
            got = json.loads((nm / name / "package.json").read_text(encoding="utf-8")).get("version")
        except (OSError, ValueError):
            got = None
        if got != ver:
            raise HubError(f"tg: node_modules/{name} is {got or 'missing'}, bun.lock wants {ver}: run `bun install"
                           f" --frozen-lockfile` in {TG_SRC}")  # fmt: skip
    bun = tg_bun()
    w = work / "tg"
    (w / "src").mkdir(parents=True)
    for f in sorted((base / "src").iterdir()):
        if f.is_file() and not f.is_symlink() and f.suffix in (".ts", ".sql"):
            shutil.copyfile(f, w / "src" / f.name)
    for n in ("package.json", "bun.lock"):
        shutil.copyfile(base / n, w / n)
    _copy_modules(nm, w / "node_modules")
    out = work / "adapter.js"
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(work), "NO_COLOR": "1"}
    try:
        r = subprocess.run([bun, "build", "src/cli.ts", "--target=bun", "--outfile", str(out)], cwd=w, env=env,
                           capture_output=True, text=True, timeout=TG_BUILD_TIMEOUT)  # fmt: skip
        v = subprocess.run([bun, "--version"], env=env, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise HubError(f"tg: bun build failed ({type(e).__name__})") from None
    if r.returncode != 0 or not out.is_file():
        why = " ".join((r.stderr or r.stdout or "").split())[-300:]
        raise HubError(f"tg: bun build exited {r.returncode}: {why}")
    prov = {"bun": v.stdout.strip(), "lock_sha256": _sha(base / "bun.lock"), "packages": dict(sorted(want.items()))}
    return {TG_ENTRY: out, TG_TOOLS: src / "schemas" / "tools.toml"}, prov


def protected(d: Path) -> dict | None:
    """{digest, action_sha, files} of release dir d's protected set, by the release's own hubverify (or None)."""
    v = d / "app" / "hubverify.py"
    if not v.is_file():
        return None
    import importlib.util

    spec = importlib.util.spec_from_file_location(f"hubverify_{os.getpid()}", v)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    files = mod.scan(str(d))
    dg = mod.digest(files)
    return {"digest": dg, "action_sha": mod.action_sha(dg), "files": sorted(files)}


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _root(root: Path | None) -> Path:
    return root if root is not None else releases_dir()


def verify(rid: str, root: Path | None = None) -> str | None:
    d = _root(root) / rid
    try:
        meta = json.loads((d / "release.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "no readable release.json"
    if not isinstance(meta, dict) or meta.get("format") != FORMAT or meta.get("rid") != rid:
        return "release.json: wrong format or id"
    files = meta.get("files") or {}
    for role, entry in ENTRY.items():
        if (meta.get("entry") or {}).get(role) != entry or entry not in files:
            return f"release.json: no {role} entry"
    for rel, info in files.items():
        p = d / rel
        if p.is_symlink() or not p.is_file():
            return f"{rel}: missing"
        if _sha(p) != (info or {}).get("sha256"):
            return f"{rel}: sha256 mismatch"
    return None


def _meta(rid: str, root: Path | None = None) -> dict:
    try:
        meta = json.loads((_root(root) / rid / "release.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return meta if isinstance(meta, dict) else {}


def entries(rid: str, root: Path | None = None) -> set[str]:
    """The entry roles kept release rid carries (hub, ci, tg, voice, phone, browser); empty without a readable
    release.json."""
    e = _meta(rid, root).get("entry")
    return set(e) if isinstance(e, dict) else set()


def optional(src: Path) -> set[str]:
    """The optional entries a build of src carries: tg, voice, phone, browser, each when its switch is on and src
    has it."""
    out = set()
    if TG_IN_RELEASE and (src / TG_SRC / "src" / "cli.ts").is_file():
        out.add("tg")
    if VOICE_IN_RELEASE and (src / VOICE_SRC / "run.py").is_file():
        out.add("voice")
    if PHONE_IN_RELEASE and (src / PHONE_SRC / "run.py").is_file():
        out.add("phone")
    if BROWSER_IN_RELEASE and (src / BROWSER_FILES[BROWSER_ENTRY]).is_file():
        out.add("browser")
    return out


def missing(src: Path, rid: str, root: Path | None = None) -> set[str]:
    """Entries a build of src would carry that kept release rid lacks."""
    return optional(src) - entries(rid, root)


def runnable(rid: str, role: str, root: Path | None = None) -> str | None:
    """None when release rid verifies and carries role's entry (what that role's loader runs), else why not."""
    why = verify(rid, root)
    if why:
        return why
    meta = _meta(rid, root)
    e = (meta.get("entry") or {}).get(role)
    if not e or e not in (meta.get("files") or {}):
        return f"no {role} entry (built without it)"
    return None


def build(src: Path, rid: str, sha: str, root: Path | None = None) -> tuple[Path, bool]:
    """-> (release dir, built or amended now?). Files 0644, dirs 0755 (the hub reads them as another uid)."""
    if not RID.match(rid):
        raise HubError(f"bad release id {rid!r}", 2)
    base = _root(root)
    base.mkdir(parents=True, exist_ok=True)
    os.chmod(base, 0o755)
    d = base / rid
    amend = False
    if d.is_dir() and verify(rid, base) is None:
        if not missing(src, rid, base):
            return d, False
        amend = True  # the same commit built without some entries: add what it lacks (_amend), never reuse it as is
    stage = base / f".stage-{rid}-{os.getpid()}"
    if stage.exists():
        shutil.rmtree(stage)
    sources: dict[str, str | Path] = dict(FILES)
    work = Path(tempfile.mkdtemp(prefix="hub-tg-"))  # the bundle's scratch build, gone when build() returns
    try:
        return _build(src, rid, sha, base, d, stage, sources, work, amend)
    finally:
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(stage, ignore_errors=True)  # left only by a failed build or an amend


def _amend(d: Path, stage: Path, rid: str) -> None:
    """Add the files a fresh build (stage) carries and kept release d lacks, in place. Every file d lists must come out
    of the fresh build with the same sha256 (same commit, same bytes), else refuse: nothing changes. New files land
    first, then release.json (old entries and files kept, the new ones added) in one rename, so a loader running d
    never sees a listed file missing or changed. The protected set must not change either (it can't for one commit)."""
    old = json.loads((d / "release.json").read_text(encoding="utf-8"))
    new = json.loads((stage / "release.json").read_text(encoding="utf-8"))
    if (old.get("protected") or {}).get("digest") != (new.get("protected") or {}).get("digest"):
        raise HubError(f"release {rid}: a fresh build has another protected set; not amended (land a new commit)", 3)
    add = []
    for rel, info in sorted(new["files"].items()):
        have = old["files"].get(rel)
        if have is None:
            add.append(rel)
        elif have.get("sha256") != info.get("sha256"):
            raise HubError(f"release {rid}: {rel} differs from a fresh build of the same commit; not amended (land a"
                           " new commit)", 3)  # fmt: skip
    for rel in add:
        q = d / rel
        for p in reversed(Path(rel).parents):
            if not (d / p).is_dir():
                (d / p).mkdir()
                os.chmod(d / p, 0o755)
        os.replace(stage / rel, q)
    for rel, info in old["files"].items():
        new["files"].setdefault(rel, info)
    for role, e in (old.get("entry") or {}).items():
        new["entry"].setdefault(role, e)
    new["built"], new["amended"] = old.get("built", new.get("built")), round(time.time(), 3)
    tmp = d / f".release.json.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(new, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o644)
    os.replace(tmp, d / "release.json")


def _build(src: Path, rid: str, sha: str, base: Path, d: Path, stage: Path, sources: dict[str, str | Path],
           work: Path, amend: bool = False) -> tuple[Path, bool]:  # fmt: skip
    files = {}
    if H10_IN_RELEASE:
        sources.update(H10_FILES)
    if EGRESS_IN_RELEASE:
        sources.update(EGRESS_FILES)
    tg, tg_prov = tg_sources(src, work) if TG_IN_RELEASE else ({}, {})
    sources.update(tg)
    voice = voice_sources(src) if VOICE_IN_RELEASE else {}
    sources.update(voice)
    phone = phone_sources(src) if PHONE_IN_RELEASE else {}
    sources.update(phone)
    browser = browser_sources(src) if BROWSER_IN_RELEASE else {}
    sources.update(browser)
    for rdir, (sdir, pats) in TREES.items():
        for pat in pats:
            for f in sorted((src / sdir).glob(pat)):
                if f.is_file() and f.name not in TREE_SKIP:
                    sources[f"{rdir}/{f.name}"] = f"{sdir}/{f.name}"
    sql_dir = src / SQL_DIR
    if sql_dir.is_dir():
        for f in sorted(sql_dir.glob("*.sql")):
            sources[f"app/pg/{f.name}"] = f"{SQL_DIR}/{f.name}"
    if (src / CI_CORE[0]).is_dir():
        for f in sorted((src / CI_CORE[0]).glob("*.py")):
            sources[f"{CI_CORE[1]}/{f.name}"] = f"{CI_CORE[0]}/{f.name}"
    for rel, srel in sources.items():
        s = srel if isinstance(srel, Path) else src / srel
        if not s.is_file():
            if rel.startswith("app/ci/"):  # the runner's CI driver: a partial tree (tests) builds without it
                continue
            raise HubError(f"source tree has no {srel}")
        q = stage / rel
        q.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(s, q)
        os.chmod(q, 0o644)
        files[rel] = {"sha256": _sha(q), "size": q.stat().st_size}
    files.update(vendor.unpack(src, stage / "vendor"))
    if voice:
        files.update(vendor.unpack(src, stage / "voice" / "vendor", VOICE_PINS, prefix="voice/vendor"))
    if phone:
        files.update(vendor.unpack(src, stage / "phone" / "vendor", PHONE_PINS, prefix="phone/vendor"))
    for dp, dns, _fns in os.walk(stage):
        os.chmod(dp, 0o755)
        for dn in dns:
            os.chmod(os.path.join(dp, dn), 0o755)
    entry = {**ENTRY, "tg": TG_ENTRY} if tg else dict(ENTRY)
    if voice:
        entry["voice"] = VOICE_ENTRY
    if phone:
        entry["phone"] = PHONE_ENTRY
    if browser:
        entry["browser"] = BROWSER_ENTRY
    meta = {"format": FORMAT, "rid": rid, "sha": sha, "built": round(time.time(), 3), "entry": entry, "files": files}
    if tg_prov:
        meta["tg"] = tg_prov
    prot = protected(stage)
    if prot is not None:
        meta["protected"] = prot
    tc = citool.build(src)
    if tc is not None:
        meta["ci"] = tc
    (stage / "release.json").write_text(json.dumps(meta, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(stage / "release.json", 0o644)
    if amend:
        _amend(d, stage, rid)
        return d, True
    if d.exists() or d.is_symlink():
        junk = base / f".broken-{rid}-{os.getpid()}"
        os.replace(d, junk)
        shutil.rmtree(junk, ignore_errors=True)
    os.replace(stage, d)
    return d, True


def read_current(root: Path | None = None) -> tuple[str, str] | None:
    try:
        parts = (_root(root) / "CURRENT").read_text(encoding="utf-8").split()
    except OSError:
        return None
    if not parts or len(parts) > 2 or not RID.match(parts[0]):
        return None
    return parts[0], " ".join(parts)


def _write_line(line: str, root: Path | None) -> None:
    base = _root(root)
    base.mkdir(parents=True, exist_ok=True)
    tmp = base / f".CURRENT.{os.getpid()}.tmp"
    tmp.write_text(line + "\n", encoding="utf-8")
    os.chmod(tmp, 0o644)
    os.replace(tmp, base / "CURRENT")


def write_current(rid: str, root: Path | None = None, now: bool = False) -> str:
    """CURRENT = "<rid> <attempt>". now=True (rollback, deploy --force) ends the attempt in NOW: claude-hub-voice's
    loader then switches at once; any other attempt waits for the running relay's live calls to end."""
    if not RID.match(rid):
        raise HubError(f"bad release id {rid!r}", 2)
    line = f"{rid} {time.strftime('%Y%m%dT%H%M%S')}-{os.getpid()}{NOW if now else ''}"
    _write_line(line, root)
    return line


def restore_current(line: str | None, root: Path | None = None) -> None:
    if line is None:
        try:
            (_root(root) / "CURRENT").unlink()
        except FileNotFoundError:
            pass
        return
    _write_line(line, root)


def kept(root: Path | None = None) -> list[str]:
    base = _root(root)
    if not base.is_dir():
        return []
    out = []
    for d in base.iterdir():
        if d.is_dir() and RID.match(d.name):
            try:
                built = json.loads((d / "release.json").read_text(encoding="utf-8")).get("built", 0)
            except (OSError, ValueError, AttributeError):
                built = 0
            out.append((built or 0, d.name))
    return [n for _, n in sorted(out, reverse=True)]


def prune(keep: set[str], n: int = KEEP, root: Path | None = None) -> list[str]:
    """Remove releases beyond the newest n, never one in keep (CURRENT, last good, the previous one)."""
    base = _root(root)
    names = kept(base)
    keep = set(keep) | set(names[:n])
    cur = read_current(base)
    if cur:
        keep.add(cur[0])
    gone = []
    for name in names:
        if name not in keep:
            shutil.rmtree(base / name, ignore_errors=True)
            gone.append(name)
    return gone
