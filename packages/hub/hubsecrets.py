#!/usr/bin/env python3
"""hubsecrets: the hub's secret loader. Stdlib only, one file. PROTECTED:
a release that changes it loads only after the owner's ✅ on its digest (hubverify).

  SECRETS                   HUB_SECRETS (the read-only secrets mount), default /run/hub-secrets
  load_tokens(dir)          {caller: token bytes} from token_<caller> files (>= 32 bytes; others ignored)
  read_secret(name, env)    one secret file's value (stripped), "" when missing; env names a file that wins
Values never leave this process: no logging, no export, no API route returns them.
"""

from __future__ import annotations

import os
import re

CALLER = re.compile(r"^token_([a-z][a-z0-9_-]{0,31})$")
MIN_TOKEN = 32
NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
SECRETS = os.environ.get("HUB_SECRETS", "/run/hub-secrets")


def load_tokens(d: str) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return out
    for n in names:
        m = CALLER.match(n)
        if not m:
            continue
        try:
            with open(os.path.join(d, n), "rb") as f:
                tok = f.read(4096).strip()
        except OSError:
            continue
        if len(tok) >= MIN_TOKEN:
            out[m.group(1)] = tok
    return out


def read_secret(name: str, env: str = "") -> str:
    if not NAME.match(name):
        return ""
    p = os.environ.get(env, "") if env else ""
    try:
        with open(p or os.path.join(SECRETS, name), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""
