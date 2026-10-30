"""Optional: a real nginx stream server (SNI map + proxy_protocol, like the web VM's streams-enabled/phonegw.conf) in
front of the test gateway. Runs only with NGINX_BIN and NGINX_STREAM_SO set (e.g. a Debian nginx + libnginx-mod-stream
extracted with dpkg -x); skipped otherwise."""
import asyncio
import os
import socket
import subprocess

import pytest

from test_public import HOST, PUB, VM, get, refused

NGINX, SO = os.environ.get("NGINX_BIN"), os.environ.get("NGINX_STREAM_SO")
pytestmark = pytest.mark.skipif(not (NGINX and SO), reason="NGINX_BIN / NGINX_STREAM_SO not set")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.mark.parametrize("env", [{"public": PUB}], indirect=True)
async def test_real_nginx_passthrough(env, phone, tmp_path):
    port = free_port()
    (tmp_path / "logs").mkdir()
    conf = tmp_path / "nginx.conf"
    conf.write_text(f"""load_module {SO};
error_log {tmp_path}/logs/error.log;
pid {tmp_path}/nginx.pid;
events {{}}
stream {{
    map $ssl_preread_server_name $phonegw_backend {{ {HOST} phonegw_gw; default phonegw_none; }}
    upstream phonegw_gw   {{ server 127.0.0.1:{env.gw.ports["front"]}; }}
    upstream phonegw_none {{ server 127.0.0.1:9; }}
    server {{
        listen 127.0.0.1:{port};
        ssl_preread on;
        preread_timeout 5s;
        proxy_pass $phonegw_backend;
        proxy_protocol on;
        proxy_bind {VM};
        proxy_connect_timeout 3s;
    }}
}}
""")
    p = subprocess.Popen([NGINX, "-p", str(tmp_path), "-c", str(conf), "-g", "daemon off;"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", port), 0.2).close()
                break
            except OSError:
                await asyncio.sleep(0.1)
        base = "https://127.0.0.1:%d" % port
        assert await get(phone, base, "/v1/health") == 200                 # device cert through nginx
        assert await get(phone, base, "/v1/pair", method="POST") == 404
        assert await refused(get(phone, base, "/v1/health", cert=False))   # no device cert
        r, w = await asyncio.open_connection("127.0.0.1", port)            # wrong SNI: nginx closes, nothing reaches us
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
        with pytest.raises((ssl.SSLError, ConnectionError, OSError)):
            await asyncio.wait_for(w.start_tls(ctx, server_hostname="wrong.test"), 5)
        w.close()
        src = env.gw.front.status()["sources"]
        assert src["proxy"] >= 3 and not src.get("proxy_bad") and not src.get("proxy_bare_tls")
        assert env.gw.guard.status()["counters"]["ok"] >= 2
    finally:
        p.terminate()
        p.wait(5)
