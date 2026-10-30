"""IP/token masking in the relay log (fake addresses from documentation ranges, fake tokens)."""
import io
import logging

from voice_relay import logmask
from voice_relay.logmask import IP_MASK, TOKEN_MASK, mask, setup_serve_logging

FAKE_TOKEN = "".join(['Zk3_q9', 'XbT0aL', 'w7Rr2Y', 'p-Vd8s', 'Nc1Hm5', 'Ej4Uo6', 'Gi'])  # not a real secret
DOC_IP, LAN_IP = ".".join(("203", "0", "113", "9")), ".".join(("10", "0", "0", "1"))


def test_ipv4_masked_in_candidates_and_with_ports():
    out = mask("candidate:1 1 udp 2122260223 192.0.2.17 50001 typ host; relay 198.51.100.4:3478 ok.")
    assert "192.0.2.17" not in out and "198.51.100.4" not in out
    assert out.count(IP_MASK) == 2 and ":3478 ok." in out


def test_ipv4_at_sentence_end_and_after_equals():
    out = mask(f"peer={DOC_IP}. done; from {LAN_IP}")
    assert DOC_IP not in out and LAN_IP not in out


def test_ipv6_forms_masked():
    for ip in ("2001:db8::1", "[2001:db8:85a3::8a2e:370:7334]:443", "fe80::1%eth0", "::1", "::ffff:192.0.2.5",
               "2001:0db8:0000:0000:0000:ff00:0042:8329"):
        out = mask("connect %s now" % ip)
        assert IP_MASK in out, ip
        assert "2001" not in out and "fe80" not in out and "192.0.2.5" not in out, out


def test_times_versions_and_words_stay():
    s = "2026-10-20 16:50:12 call 12:34:56 PyTgCalls v3.0.0 NTgCalls v3.0.5 state=CONNECTED ratio 1.25"
    assert mask(s) == s


def test_tokens_masked_known_and_generic():
    assert "s3cr3t-value" not in mask("auth s3cr3t-value", secrets=("s3cr3t-value",))
    out = mask("Authorization: Bearer " + FAKE_TOKEN)
    assert FAKE_TOKEN not in out and TOKEN_MASK in out
    path = "/home/node/opt/voice-relay/voice_relay/server.py"
    assert mask(path) == path


def test_handler_masks_message_args_and_tracebacks_with_timestamp():
    buf = io.StringIO()
    setup_serve_logging(secrets=("fake-local-token-1234",), stream=buf)
    try:
        lg = logging.getLogger("ntgcalls")
        lg.info("connected to %s via fake-local-token-1234", "192.0.2.44")
        try:
            raise OSError("bind failed on 2001:db8::7")
        except OSError:
            lg.exception("oops")
        logging.getLogger("webrtc").info("ice candidate 192.0.2.99")  # below WARNING: dropped
        out = buf.getvalue()
    finally:
        logging.getLogger().handlers[:] = []
    assert "192.0.2.44" not in out and "2001:db8::7" not in out and "fake-local-token-1234" not in out
    assert "192.0.2.99" not in out and "ice candidate" not in out
    assert "connected to %s" % IP_MASK in out
    first = out.splitlines()[0]
    assert first[:4].isdigit() and first[10] == " " and " INFO ntgcalls: " in first
    assert logging.getLogger("ntgcalls").level == logging.INFO
    assert logging.getLogger("webrtc").level == logging.WARNING
    assert logmask.MaskingFormatter  # exported
