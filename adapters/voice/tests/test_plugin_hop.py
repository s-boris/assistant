"""The staged telegram-plus voice inbox + the real MCP SDK over stdio: relay POST → channel notification."""
import json
import os
import queue
import shutil
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

HOP = os.path.expanduser("~/opt/staged/voice-inbox/mcp-hop.ts")
BUN = shutil.which("bun")
pytestmark = pytest.mark.skipif(not (BUN and os.path.exists(HOP)), reason="bun or the staged harness missing")


def test_handoff_reaches_the_session_as_a_channel_notification(tmp_path):
    tok = tmp_path / "tok"
    tok.write_text("hop-secret\n")
    os.chmod(tok, 0o600)
    p = subprocess.Popen([BUN, HOP], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         env=dict(os.environ, HOP_PORT="0", HOP_TOKEN_FILE=str(tok)), cwd=str(tmp_path))
    out = queue.Queue()
    threading.Thread(target=lambda: [out.put((time.perf_counter(), ln)) for ln in p.stdout], daemon=True).start()
    try:
        port, end = None, time.time() + 20
        while time.time() < end and port is None:
            ln = p.stderr.readline().decode()
            if "hop_port" in ln:
                port = json.loads(ln)["hop_port"]
        assert port

        def send(o):
            p.stdin.write((json.dumps(o) + "\n").encode())
            p.stdin.flush()

        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "hop", "version": "1"}}})
        assert json.loads(out.get(timeout=10)[1])["id"] == 1
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})

        def post(task, token="hop-secret"):
            req = urllib.request.Request("http://127.0.0.1:%d/voice" % port, method="POST",
                                         data=json.dumps({"id": "v0123abcd", "task": task}).encode(),
                                         headers={"Authorization": "Bearer " + token,
                                                  "Content-Type": "application/json"})
            try:
                return urllib.request.urlopen(req, timeout=5).status
            except urllib.error.HTTPError as e:
                return e.code

        assert post("nope", token="wrong") == 401
        lat = []
        for i in range(20):
            t0 = time.perf_counter()
            assert post("task %d" % i) == 202
            t1, ln = out.get(timeout=5)
            msg = json.loads(ln)
            assert msg["method"] == "notifications/claude/channel"
            assert msg["params"]["meta"]["via"] == "voice" and "task %d" % i in msg["params"]["content"]
            lat.append((t1 - t0) * 1000)
        with pytest.raises(queue.Empty):
            out.get(timeout=0.3)
        print("LATENCY relay POST -> MCP channel notification: median %.2f ms, max %.2f ms (n=20)"
              % (statistics.median(lat), max(lat)), file=sys.stderr)
    finally:
        p.kill()
        p.wait()
