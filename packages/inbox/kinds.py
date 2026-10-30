"""inbox.kinds: the kind registry (schemas/kinds.toml) and filename -> kind classification.

  load(path=None) -> Registry      $ASSISTANT_KINDS, else kinds.toml next to this package, else the repo's schemas/
  Registry.classify(name) -> Match  kind, entry (or None), payload extras from the entry's regex groups
  Registry.labels_for(kind, text)   default labels + label_lines hits
  prefix_of(name)                   the filename up to its first variable part (number, date, hash, T<id>)

A missing or broken registry is not an error: every note then becomes note.<prefix> with delivery beat.
"""

from __future__ import annotations

import fnmatch
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["Match", "Registry", "load", "prefix_of"]

_VAR = re.compile(r"^(?:\d+|(?=[0-9a-f]*\d)[0-9a-f]{4,}|t\d+|\d{1,2}\.\d{1,2}(?:\.\d{2,4})?)$")
# built in, so a missing registry still gives cron.fail for NAME-failed-<epoch> notes (cron-fail and the jobs'
# own failure lines)
_FAILED = re.compile(r"^(?P<job>.+?)-failed-\d{6,}(?:-\d+)?(?:\.[A-Za-z0-9]{1,5})?$")
_EXT = re.compile(r"\.[A-Za-z0-9]{1,5}$")


def prefix_of(name: str) -> str:
    """'vault-events-1792602808-2.md' -> 'vault-events'; 'restart-cause-20261021-1930.md' -> 'restart-cause';
    'phone-chat-20261020-224817-c2c4.md' -> 'phone-chat'. Lowercase, [a-z0-9._-] only; 'misc' if nothing is left."""
    stem = _EXT.sub("", name)
    keep = []
    for tok in stem.split("-"):
        if _VAR.match(tok.lower()):
            break
        keep.append(tok)
    p = re.sub(r"[^a-z0-9._-]+", "-", "-".join(keep).lower()).strip("-.")
    return p or "misc"


def _literal_len(glob: str) -> int:
    m = re.search(r"[*?\[]", glob)
    return len(glob) if m is None else m.start()


@dataclass
class Match:
    kind: str
    entry: dict[str, Any] | None
    extras: dict[str, str] = field(default_factory=dict)


@dataclass
class Registry:
    kinds: dict[str, dict[str, Any]] = field(default_factory=dict)
    source: str | None = None
    error: str | None = None

    def classify(self, name: str) -> Match:
        best: tuple[tuple[int, int, int], str, dict[str, Any], dict[str, str]] | None = None
        for kind, e in self.kinds.items():
            for g in e.get("match") or []:
                if not fnmatch.fnmatchcase(name, g):
                    continue
                extras: dict[str, str] = {}
                rx = e.get("regex")
                if rx:
                    m = re.match(rx, name)
                    if not m:
                        continue
                    extras = {k: v for k, v in m.groupdict().items() if v is not None}
                score = (int(e.get("priority") or 0), _literal_len(g), len(g))
                if best is None or score > best[0]:
                    best = (score, kind, e, extras)
        if best is None:
            m = _FAILED.match(name)
            if m:
                return Match("cron.fail", None, {"job": m.group("job")})
            return Match("note." + prefix_of(name), None)
        return Match(best[1], best[2], best[3])

    def entry(self, kind: str) -> dict[str, Any] | None:
        return self.kinds.get(kind)

    def delivery(self, kind: str) -> str:
        e = self.kinds.get(kind) or {}
        d = e.get("delivery") or "beat"
        return d if d in ("now", "beat", "digest", "mute") else "beat"

    def labels_for(self, kind: str, text: str = "") -> list[str]:
        e = self.kinds.get(kind) or {}
        out = set(e.get("labels") or [])
        for label, rx in (e.get("label_lines") or {}).items():
            try:
                if text and re.search(rx, text, re.M):
                    out.add(label)
            except re.error:
                pass
        return sorted(out)

    def hint(self, kind: str) -> str:
        return str((self.kinds.get(kind) or {}).get("hint") or "")


def _candidates() -> list[Path]:
    env = os.environ.get("ASSISTANT_KINDS", "")
    here = Path(__file__).resolve().parent
    out = [Path(env)] if env else []
    out += [here / "kinds.toml", here.parent.parent / "schemas" / "kinds.toml"]
    return out


def load(path: str | os.PathLike[str] | None = None) -> Registry:
    for p in [Path(path)] if path else _candidates():
        if not p.is_file():
            continue
        try:
            with open(p, "rb") as f:
                data = tomllib.load(f)
            kinds = data.get("kinds") or {}
            if not isinstance(kinds, dict):
                raise ValueError("[kinds] must be a table")
            return Registry({str(k): dict(v) for k, v in kinds.items() if isinstance(v, dict)}, str(p))
        except (OSError, ValueError, tomllib.TOMLDecodeError) as e:
            return Registry({}, str(p), f"{type(e).__name__}: {e}"[:200])
    return Registry({}, None, "no kinds.toml found")
