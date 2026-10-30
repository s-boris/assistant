#!/usr/bin/env python3
"""securefill's vault client on core.egress (P3-12, handle "vault"): fakes only (a fake vault, a fake hub, a throwaway
HOME with a fake token). Each case runs the client in its own process (HOME, the egress switch and the module's cache
differ per case) and reports what reached which fake: paths, header names, whether the token or a placeholder went
where. No answer body is printed anywhere.
Run: python3 test_securefill_egress.py   (a copy: SECUREFILL_LIB=<dir holding securefill.py and core>)"""
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LIB = os.environ.get("SECUREFILL_LIB") or os.path.join(os.path.expanduser("~"), "bin", "lib")
TOKEN = "tok-fake-vault"
ANSWER = {"ok": True, "n": 3}

CHILD = r"""
import json, os, sys
sys.path.insert(0, os.environ["SF_LIB"])
import securefill as sf
out = {}
try:
    got = sf.vault(os.environ["SF_METHOD"], "/t/one", {"a": 1} if os.environ["SF_METHOD"] == "POST" else None)
    out["same"] = got == json.loads(os.environ["SF_EXPECT"])
except sf.VaultErr as e:
    out["err"], out["code"], out["status"] = str(e), e.code, e.status
out["egress"] = sf._egress() is not None
print(json.dumps(out))
"""


class Fake(BaseHTTPRequestHandler):
    seen = None
    answer = None

    def log_message(self, *a):
        pass

    def _do(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        self.seen.append({"path": self.path, "method": self.command, "auth": self.headers.get("Authorization", ""),
                          "body": body})
        code, ans = self.answer(self.path, body)
        raw = json.dumps(ans).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_GET = do_POST = _do


def serve(answer):
    seen = []
    h = type("H", (Fake,), {"seen": seen, "answer": staticmethod(answer)})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), h)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, seen


class EgressClient(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="sf-eg-")
        os.makedirs(os.path.join(self.home, ".secrets"), mode=0o700)
        os.makedirs(os.path.join(self.home, ".config", "assistant"))
        for name, val in (("vault-token", TOKEN), ("hub-token", "tok-fake-hub")):
            with open(os.path.join(self.home, ".secrets", name), "w") as f:
                f.write(val + "\n")
            os.chmod(os.path.join(self.home, ".secrets", name), 0o600)
        self.vault, self.vseen = serve(lambda p, b: (200, ANSWER))
        self.hub_code = 200
        self.hub, self.hseen = serve(self._hub)
        self.vurl = "http://127.0.0.1:%d" % self.vault.server_address[1]

    def tearDown(self):
        self.vault.shutdown()
        self.hub.shutdown()
        shutil.rmtree(self.home, ignore_errors=True)

    def _hub(self, path, body):
        if path == "/egress/check":
            return 200, {"decision": "allow"}
        if self.hub_code != 200:
            return self.hub_code, {"error": "egress_denied", "hint": "no rule"}
        raw = json.dumps(ANSWER).encode()
        return 200, {"status": 200, "reason": "OK", "headers": [["Content-Type", "application/json"]],
                     "body_b64": base64.b64encode(raw).decode()}

    def run_child(self, mode=None, method="POST", token_file=None):
        if mode:
            with open(os.path.join(self.home, ".config", "assistant", "egress.json"), "w") as f:
                json.dump({"handles": {"vault": mode}}, f)
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": self.home, "ASSISTANT_TEST": "1",
               "HUB_URL": "http://127.0.0.1:%d" % self.hub.server_address[1], "SF_LIB": LIB, "SF_METHOD": method,
               "SF_EXPECT": json.dumps(ANSWER), "SECUREFILL_VAULT_URL": self.vurl}
        if token_file:
            env["SECUREFILL_VAULT_TOKEN_FILE"] = token_file
        p = subprocess.run([sys.executable, "-I", "-c", CHILD], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr[-600:])
        self.assertNotIn(TOKEN, p.stdout + p.stderr)
        return json.loads(p.stdout.strip().splitlines()[-1])

    def test_local_mode_is_the_direct_call_with_the_token(self):
        r = self.run_child()
        self.assertTrue(r["egress"] and r["same"])
        self.assertEqual([(s["method"], s["path"], s["auth"]) for s in self.vseen], [("POST", "/t/one", "Bearer " + TOKEN)])
        self.assertEqual(self.hseen, [])

    def test_shadow_mode_adds_a_check_without_the_token_or_the_body(self):
        r = self.run_child("shadow")
        self.assertTrue(r["same"])
        self.assertEqual(len(self.vseen), 1)
        self.assertEqual([s["path"] for s in self.hseen], ["/egress/check"])
        chk = json.loads(self.hseen[0]["body"])
        self.assertEqual((chk["handle"], chk["method"]), ("vault", "POST"))
        blob = json.dumps(chk)
        self.assertNotIn(TOKEN, blob)
        self.assertIn("hub:secret:vault", blob)
        self.assertNotIn("body_b64", chk)

    def test_hub_mode_goes_through_the_hub_with_the_placeholder(self):
        r = self.run_child("hub", method="GET")
        self.assertTrue(r["same"])
        self.assertEqual(self.vseen, [])
        self.assertEqual([s["path"] for s in self.hseen], ["/egress/fetch"])
        req = json.loads(self.hseen[0]["body"])
        self.assertEqual((req["handle"], req["method"], req["url"]), ("vault", "GET", self.vurl + "/t/one"))
        self.assertIn(["Authorization", "Bearer hub:secret:vault"], req["headers"])
        self.assertNotIn(TOKEN, json.dumps(req))
        self.assertEqual(self.hseen[0]["auth"], "Bearer tok-fake-hub")

    def test_hub_mode_post_carries_the_body_to_the_hub_only(self):
        r = self.run_child("hub")
        self.assertTrue(r["same"])
        req = json.loads(self.hseen[0]["body"])
        self.assertEqual(json.loads(base64.b64decode(req["body_b64"])), {"a": 1})
        self.assertEqual(self.vseen, [])

    def test_a_hub_refusal_is_unreachable_and_names_no_value(self):
        self.hub_code = 403
        r = self.run_child("hub")
        self.assertEqual((r["code"], r["status"]), ("unreachable", None))
        self.assertIn("egress refused", r["err"])
        self.assertEqual(self.vseen, [])

    def test_a_tests_own_token_file_keeps_the_direct_path_in_hub_mode(self):
        tf = os.path.join(self.home, "own-token")
        with open(tf, "w") as f:
            f.write("tok-own\n")
        r = self.run_child("hub", token_file=tf)
        self.assertFalse(r["egress"])
        self.assertTrue(r["same"])
        self.assertEqual([s["auth"] for s in self.vseen], ["Bearer tok-own"])
        self.assertEqual(self.hseen, [])


if __name__ == "__main__":
    unittest.main(verbosity=1)
