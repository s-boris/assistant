"""Test bootstrap: runs ~/bin/container-watch the way cron does, but with every socket blocked (any attempt is
logged to $CW_NET_LOG and fails) and the clock fixed to $CW_TEST_NOW. Used by the Cron tests only."""

import datetime as dt
import importlib.machinery
import importlib.util
import os
import socket
import sys


def _blocked(*a, **k):
    with open(os.environ["CW_NET_LOG"], "a") as f:
        f.write(repr(a[1:3]) + "\n")
    raise OSError("network blocked by the container-watch tests")


socket.socket.connect = _blocked
socket.create_connection = _blocked
socket.getaddrinfo = _blocked

_ld = importlib.machinery.SourceFileLoader("cw", os.environ["CW_BIN"])
_sp = importlib.util.spec_from_loader("cw", _ld)
cw = importlib.util.module_from_spec(_sp)
_ld.exec_module(cw)
if os.environ.get("CW_TEST_NOW"):
    _now = dt.datetime.fromisoformat(os.environ["CW_TEST_NOW"])
    cw.now_utc = lambda: _now
sys.argv = ["container-watch"] + sys.argv[1:]
sys.exit(cw.main())
