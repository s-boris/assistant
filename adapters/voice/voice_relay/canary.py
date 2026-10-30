"""Provider canary for the daily voice health check (`voicecheck`): no call, no audio, no turn.
  rest  GET {gemini_rest_url}/models/{model} (free, no tokens): key valid (400 API_KEY_INVALID / 401 / 403 = key),
        model still listed (404 = gone), its version and bidiGenerateContent support.
  live  optional: one Live setup with a minimal config (model + AUDIO only; no system prompt, no tools) closed right
        after setupComplete. It proves billing: a depleted prepaid balance closes the setup with 1011 "prepayment
        credits are depleted". Nothing is sent after the setup, so no tokens are processed (expected cost: 0).
Result dict: provider, model, key (ok|invalid|error), listed (True|False|None), version, bidi, live
(ok|billing|model|rejected|timeout|error|skipped), detail (short, never the key)."""
import asyncio
import json

import aiohttp

from .config import read_secret

BILLING_WORDS = ("prepay", "credit", "billing", "quota", "exhausted", "payment")


def _short(text, n=160):
    return " ".join(str(text or "").split())[:n]


def classify_close(code, reason):
    r = (reason or "").lower()
    if any(w in r for w in BILLING_WORDS):
        return "billing"
    if "not found" in r or "not supported" in r or "unknown model" in r:
        return "model"
    return "rejected"


async def rest_check(cfg, key, http, timeout=15, model=None):
    url = "%s/models/%s" % (cfg.gemini_rest_url.rstrip("/"), model or cfg.gemini_model)
    out = {"key": "error", "listed": None, "version": "", "bidi": None, "detail": ""}
    try:
        async with http.get(url, headers={"x-goog-api-key": key},
                            timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            raw = await r.text()
            try:
                body = json.loads(raw or "{}")
            except ValueError:
                body = {}
            if r.status == 200:
                out.update(key="ok", listed=True, version=_short(body.get("version"), 60),
                           bidi="bidiGenerateContent" in (body.get("supportedGenerationMethods") or []))
                return out
            err = body.get("error") if isinstance(body.get("error"), dict) else {}
            reasons = [d.get("reason") for d in err.get("details") or [] if isinstance(d, dict) and d.get("reason")]
            out["detail"] = _short("HTTP %s %s %s" % (r.status, err.get("status") or "", " ".join(reasons)))
            if r.status == 404:
                out.update(key="ok", listed=False)
            elif r.status in (401, 403) or "API_KEY_INVALID" in reasons or "API_KEY" in " ".join(reasons):
                out["key"] = "invalid"
            return out
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
        out["detail"] = _short("REST %s" % type(e).__name__)
        return out


async def live_check(cfg, key, http, timeout=15):
    """Minimal Live setup, closed at once. (state, detail)."""
    setup = {"setup": {"model": "models/" + cfg.gemini_model,
                       "generationConfig": {"responseModalities": ["AUDIO"]}}}
    try:
        ws = await asyncio.wait_for(http.ws_connect(cfg.gemini_ws_url, headers={"x-goog-api-key": key},
                                                    max_msg_size=4 << 20), timeout)
    except aiohttp.WSServerHandshakeError as e:
        return ("billing" if e.status == 429 else "rejected"), _short("handshake HTTP %s" % e.status)
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
        return "error", _short("connect %s" % type(e).__name__)
    try:
        await ws.send_str(json.dumps(setup))
        try:
            msg = await asyncio.wait_for(ws.receive(), timeout)
        except asyncio.TimeoutError:
            return "timeout", "no setupComplete within %d s" % timeout
        if msg.type in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
            data = msg.data if isinstance(msg.data, str) else msg.data.decode("utf-8", "replace")
            try:
                d = json.loads(data)
            except ValueError:
                d = {}
            if "setupComplete" in d:
                return "ok", ""
            return "rejected", _short("unexpected first message: %s" % ",".join(sorted(d))[:60])
        code, reason = ws.close_code, _short(msg.extra if msg.type == aiohttp.WSMsgType.CLOSE else "")
        return classify_close(code, reason), _short("close %s %s" % (code, reason))
    finally:
        if not ws.closed:
            await ws.close()


async def run(cfg, live=True, key=None):
    res = {"provider": cfg.provider, "model": cfg.gemini_model, "key": "error", "listed": None, "version": "",
           "bidi": None, "live": "skipped", "detail": ""}
    if cfg.provider != "gemini":
        res["detail"] = "provider %s: no canary" % cfg.provider
        return res
    key = key or read_secret(cfg.gemini_key_file)
    if not key:
        res.update(key="missing", detail="no Gemini API key file")
        return res
    async with aiohttp.ClientSession() as http:
        res.update(await rest_check(cfg, key, http))
        fb = []
        for m in tuple(getattr(cfg, "gemini_fallbacks", ()) or ()):
            r = await rest_check(cfg, key, http, model=m)
            fb.append({"model": m, "listed": r["listed"], "version": r["version"], "bidi": r["bidi"]})
        res["fallbacks"] = fb
        if live and res["key"] == "ok" and res["listed"]:
            res["live"], d = await live_check(cfg, key, http)
            if d:
                res["detail"] = _short(("%s; " % res["detail"] if res["detail"] else "") + d)
    return res


def ok(res):
    return res["key"] == "ok" and res["listed"] is True and res["live"] in ("ok", "skipped")


async def live_models(cfg, key=None, timeout=15):
    """Every model the key can open a Live session with (models.list, free): [{model, version, name}]."""
    key = key or read_secret(cfg.gemini_key_file)
    if not key:
        raise RuntimeError("no Gemini API key file")
    out, token = [], ""
    async with aiohttp.ClientSession() as http:
        for _ in range(10):
            url = "%s/models?pageSize=200%s" % (cfg.gemini_rest_url.rstrip("/"), "&pageToken=" + token if token else "")
            async with http.get(url, headers={"x-goog-api-key": key},
                                timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                if r.status != 200:
                    raise RuntimeError("models.list HTTP %s" % r.status)
                body = await r.json()
            for m in body.get("models") or []:
                if "bidiGenerateContent" in (m.get("supportedGenerationMethods") or []):
                    out.append({"model": str(m.get("name", "")).split("/", 1)[-1], "version": _short(m.get("version"), 60),
                                "name": _short(m.get("displayName"), 60)})
            token = body.get("nextPageToken") or ""
            if not token:
                break
    return out
