"""Domain modules: importing one registers its tools. Order = the docs' and tools.toml's order."""

from __future__ import annotations

import importlib

DOMAINS = (
    "bindings",
    "reminders",
    "vault",
    "paperless",
    "ha",
    "calendar",
    "media",
    "web",
    "cat",
    "server",
    "phone",
    "traces",
    "deploy",
    "workflow",
    "voice",
)


def load_all() -> None:
    for d in DOMAINS:
        importlib.import_module(f"tools.domains.{d}")
