"""inbox.notes: what ingest reads from an ~/inbox note — its title and Next line, never more — and which files count.

  scan(inbox_dir, now, min_age=2.0) -> (notes, young_names)   regular, non-hidden files directly in the inbox
  read_note(path) -> NoteInfo                                  title, next, fingerprint (size, mtime, sha1), text
  parse(text, name) -> (title, next)                           what ingest derives (writers reuse it for their rows)
  clean(s, n)                                                  one line, control characters out, cut to n chars

Skipped: subdirectories (~/inbox/files/ and any other), symlinks, hidden names (.x), editor/temp leftovers
(*.tmp, *.part, *.swp, *~), and files younger than min_age seconds (still being written; picked up next run).
The title is the first "# " heading within the first 8 lines (else the filename); the Next line is the text after
the first "Next:" (at a line start or after a sentence) up to the end of that line. The text is returned to the
caller for label_lines only; nothing of the body is stored.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass

__all__ = ["NoteInfo", "clean", "fingerprint_of", "parse", "read_note", "scan", "skip_name"]

MAX_READ = 4 * 1024 * 1024
_CTRL = re.compile(r"[\x00-\x1f\x7f​-‏  ]+")
_NEXT = re.compile(r"(?:^|(?<=[\s.;:)\]]))Next:[ \t]*(\S[^\n]*)", re.M)
_TMP = re.compile(r"(\.tmp|\.part|\.swp|\.swx|~)$")


def clean(s: str, n: int) -> str:
    s = " ".join(_CTRL.sub(" ", s or "").split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def skip_name(name: str) -> bool:
    return name.startswith(".") or bool(_TMP.search(name))


@dataclass
class NoteInfo:
    name: str
    path: str
    size: int
    mtime: float
    sha1: str
    title: str
    next: str
    text: str

    def fingerprint(self) -> dict[str, object]:
        return {"size": self.size, "mtime": round(self.mtime, 3), "sha1": self.sha1}


def read_note(path: str) -> NoteInfo:
    st = os.lstat(path)
    with open(path, "rb") as f:
        raw = f.read(MAX_READ)
    h = hashlib.sha1(raw)
    if st.st_size > MAX_READ:
        h.update(str(st.st_size).encode())
    text = raw.decode("utf-8", errors="replace")
    name = os.path.basename(path)
    title, nxt = parse(text, name)
    return NoteInfo(
        name=name,
        path=path,
        size=st.st_size,
        mtime=st.st_mtime,
        sha1=h.hexdigest(),
        title=title,
        next=nxt,
        text=text,
    )


def parse(text: str, name: str) -> tuple[str, str]:
    """(title, next) as ingest stores them: the first "#" heading in the first 8 lines (else the name) and the
    Next line."""
    title = ""
    for line in text.splitlines()[:8]:
        if line.startswith("#"):
            title = line.lstrip("#").strip()
            break
    m = _NEXT.search(text)
    return clean(title, 160) or clean(name, 160), clean(m.group(1), 300) if m else ""


def fingerprint_of(path: str) -> dict[str, object]:
    """The fingerprint ingest stores for the note at path (raises OSError)."""
    return read_note(path).fingerprint()


def scan(inbox_dir: str, now: float, min_age: float = 2.0) -> tuple[list[str], set[str]]:
    """(stable note paths, names of files present but too young). Missing inbox = nothing."""
    stable: list[str] = []
    young: set[str] = set()
    try:
        it = os.scandir(inbox_dir)
    except (FileNotFoundError, NotADirectoryError):
        return stable, young
    with it:
        for e in it:
            if skip_name(e.name):
                continue
            try:
                if not e.is_file(follow_symlinks=False):
                    continue
                mt = e.stat(follow_symlinks=False).st_mtime
            except FileNotFoundError:
                continue
            if now - mt < min_age:
                young.add(e.name)
            else:
                stable.append(e.path)
    stable.sort()
    return stable, young
