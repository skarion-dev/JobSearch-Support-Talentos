"""
LLM gateway — OpenAI-compatible proxy in front of the OpenCode Go subscription.

Runs on the spare PC, one process, port 8787 by default. Two kinds of callers:

  * /v1/*      any issued client token (Talentos, this app, future callers).
               Sees and can request ONLY gpt-5.6-luna, deepseek-v4-flash,
               deepseek-v4-pro — see gateway/config.py ALLOWED_MODELS. Nothing
               else in the OpenCode catalogue is reachable through here.
  * /admin/*   a single admin secret (GATEWAY_ADMIN_TOKEN), for issuing and
               managing client tokens and flipping the kill switch.

Why this exists rather than pointing callers at OpenCode directly: a leaked
OpenCode key spends the whole subscription on whatever the leaker wants: a
leaked gateway token spends it on three named, rate-limited, revocable models
and every call is attributed and logged.

    uvicorn gateway.main:app --host 127.0.0.1 --port 8787 --workers 1

MUST run as exactly one worker — see gateway/keys.py's module docstring for
why the key-rotation state cannot be shared across processes.
"""
import json
import logging
import time

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse

from gateway import store
from gateway.auth import token_prefix, verify_token
from gateway.config import (
    ALLOWED_MODELS,
    FORCED_REASONING,
    GATEWAY_ADMIN_TOKEN,
    REQUEST_TIMEOUT,
    UPSTREAM_BASE_URL,
)
from gateway.keys import pool
from gateway.limiter import check_and_record


# force=True: uvicorn's own dictConfig runs before this module is imported and
# leaves the root logger with handlers already attached, which makes a plain
# basicConfig() call a silent no-op per the stdlib's own documented behaviour.
# Without force, every log.info/warning/error in this package -- including
# the reasoning-effort override notice below -- reliably went nowhere.
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", force=True)
log = logging.getLogger("gateway")

app = FastAPI(title="Skarion LLM Gateway")


@app.exception_handler(HTTPException)
async def openai_shaped_http_exception(request: Request, exc: HTTPException):
    """FastAPI's default handler wraps HTTPException.detail as {"detail": ...},
    which buries our {"error": {...}} one level too deep for the OpenAI SDK's
    error parser to find it. Every raise HTTPException(..., detail=openai_error(...))
    in this file relies on this handler to put "error" back at the top level —
    without it, every 4xx/5xx from this gateway looks like a network error to
    client SDKs instead of a parsed APIError with a real message."""
    body = exc.detail if isinstance(exc.detail, dict) and "error" in exc.detail else openai_error(str(exc.detail))
    return JSONResponse(status_code=exc.status_code, content=body, headers=getattr(exc, "headers", None) or {})


def openai_error(message: str, err_type: str = "invalid_request_error", code: str | None = None) -> dict:
    """OpenAI SDKs parse error responses expecting this exact shape — match it
    so a rejected call surfaces as a normal APIError to the caller's SDK
    rather than an unparseable body."""
    return {"error": {"message": message, "type": err_type, "code": code}}


# ------------------------------------------------------------------ auth --

async def authenticate(request: Request) -> dict:
    authz = request.headers.get("authorization", "")
    if not authz.lower().startswith("bearer "):
        raise HTTPException(401, detail=openai_error("Missing bearer token"))
    token = authz.split(" ", 1)[1].strip()

    candidates = store.find_client_by_prefix(token_prefix(token))
    client = next((c for c in candidates if verify_token(token, c["token_hash"])), None)
    if not client:
        raise HTTPException(401, detail=openai_error("Invalid API key"))
    if not client["enabled"]:
        raise HTTPException(403, detail=openai_error("This key has been disabled", "permission_error"))
    if not store.global_enabled():
        raise HTTPException(503, detail=openai_error(
            "Gateway is temporarily disabled by the operator", "service_unavailable"))
    return client


def require_admin(request: Request):
    if not GATEWAY_ADMIN_TOKEN:
        raise HTTPException(503, detail=openai_error(
            "GATEWAY_ADMIN_TOKEN is not configured on this server", "service_unavailable"))
    given = request.headers.get("x-admin-token", "")
    import hmac
    if not hmac.compare_digest(given, GATEWAY_ADMIN_TOKEN):
        raise HTTPException(401, detail=openai_error("Invalid admin token"))


def client_allowed_models(client: dict) -> set[str]:
    return set(json.loads(client["allowed_models"])) & ALLOWED_MODELS


# ------------------------------------------------------------------ /v1 ---

@app.get("/healthz")
async def healthz():
    return {
        "ok": True,
        "upstream_keys": len(pool._keys),
        "models": sorted(ALLOWED_MODELS),
        "global_enabled": store.global_enabled(),
    }


@app.get("/v1/models")
async def list_models(request: Request):
    client = await authenticate(request)
    allowed = sorted(client_allowed_models(client))
    return {"object": "list", "data": [{"id": m, "object": "model", "owned_by": "opencode"} for m in allowed]}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    client = await authenticate(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, detail=openai_error("Invalid JSON body"))

    model = body.get("model")

    def reject(status: int, message: str, err_type: str):
        # Logged as its own status ("rejected"), distinct from "error" (an
        # upstream failure) — without this, a leaked key being throttled or a
        # misconfigured caller retrying a disallowed model is invisible in
        # /admin/stats even though the gateway is actively fielding it.
        store.log_request(client["id"], str(model), "rejected", status, None, None, None, 0, message)
        raise HTTPException(status, detail=openai_error(message, err_type))

    if model not in ALLOWED_MODELS:
        reject(403, f"Model '{model}' is not available through this gateway. "
                    f"Allowed: {', '.join(sorted(ALLOWED_MODELS))}.", "permission_error")
    if model not in client_allowed_models(client):
        reject(403, f"This key is not authorized for model '{model}'.", "permission_error")

    if not check_and_record(client["id"], client["rpm_limit"]):
        reject(429, "Rate limit exceeded for this key.", "rate_limit_error")

    used_today = store.tokens_used_today(client["id"])
    if used_today >= client["daily_token_limit"]:
        reject(429, f"Daily token budget ({client['daily_token_limit']}) exhausted for this key.",
               "rate_limit_error")

    forced = FORCED_REASONING.get(model)
    if forced:
        existing = body.get("reasoning_effort")
        if existing and existing != forced:
            log.info(f"{model}: overriding requested reasoning_effort={existing!r} with {forced!r}")
        body["reasoning_effort"] = forced

    if body.get("stream"):
        return await _stream(client, model, body)
    return await _non_stream(client, model, body)


async def _non_stream(client: dict, model: str, body: dict):
    t0 = time.time()
    order = pool.attempt_order()
    last_err, last_status = "no upstream keys available", 503

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as http:
        for key in order:
            try:
                r = await http.post(f"{UPSTREAM_BASE_URL}/chat/completions",
                                     headers={"Authorization": f"Bearer {key}"}, json=body)
            except httpx.RequestError as e:
                last_err, last_status = str(e), 502
                continue

            if r.status_code == 429:
                retry_after = r.headers.get("retry-after")
                pool.mark_429(key, float(retry_after) if retry_after else None)
                last_err, last_status = "upstream rate limited", 429
                continue
            if r.status_code in (401, 403):
                pool.mark_dead(key, f"{r.status_code}: {r.text[:200]}")
                last_err, last_status = "upstream credential rejected", r.status_code
                continue
            if r.status_code >= 500:
                last_err, last_status = f"upstream {r.status_code}", r.status_code
                continue

            pool.mark_ok(key)
            latency_ms = int((time.time() - t0) * 1000)
            try:
                data = r.json()
            except Exception:
                data = {}
            usage = data.get("usage", {})
            store.log_request(client["id"], model, "ok" if r.status_code < 400 else "error",
                               r.status_code, usage.get("prompt_tokens"), usage.get("completion_tokens"),
                               key[:10], latency_ms, None if r.status_code < 400 else r.text[:300])
            return JSONResponse(content=data, status_code=r.status_code)

    latency_ms = int((time.time() - t0) * 1000)
    store.log_request(client["id"], model, "error", last_status, None, None, None, latency_ms, last_err)
    raise HTTPException(502, detail=openai_error(
        f"All upstream keys exhausted or failing. Last error: {last_err}", "upstream_error"))


async def _stream(client: dict, model: str, body: dict):
    order = pool.attempt_order()
    if not order:
        raise HTTPException(503, detail=openai_error("No upstream keys available", "upstream_error"))

    async def gen():
        t0 = time.time()
        http = httpx.AsyncClient(timeout=REQUEST_TIMEOUT)
        last_err = "no upstream keys available"
        try:
            for key in order:
                req = http.build_request("POST", f"{UPSTREAM_BASE_URL}/chat/completions",
                                          headers={"Authorization": f"Bearer {key}"}, json=body)
                try:
                    r = await http.send(req, stream=True)
                except httpx.RequestError as e:
                    last_err = str(e)
                    continue

                if r.status_code == 429:
                    retry_after = r.headers.get("retry-after")
                    pool.mark_429(key, float(retry_after) if retry_after else None)
                    await r.aclose()
                    last_err = "upstream rate limited"
                    continue
                if r.status_code in (401, 403):
                    pool.mark_dead(key, f"{r.status_code}")
                    await r.aclose()
                    last_err = "upstream credential rejected"
                    continue
                if r.status_code >= 400:
                    last_err = f"upstream {r.status_code}"
                    await r.aclose()
                    continue

                # Committed: headers are good, we start relaying bytes. No
                # failover past this point — a key dying mid-stream surfaces
                # to the caller as a truncated response, not a silent retry.
                pool.mark_ok(key)
                store.log_request(client["id"], model, "ok", r.status_code, None, None,
                                   key[:10], int((time.time() - t0) * 1000), None)
                try:
                    async for chunk in r.aiter_bytes():
                        yield chunk
                finally:
                    await r.aclose()
                return

            store.log_request(client["id"], model, "error", 502, None, None, None,
                               int((time.time() - t0) * 1000), last_err)
            err = openai_error(f"All upstream keys exhausted or failing. Last error: {last_err}",
                                "upstream_error")
            yield f"data: {json.dumps(err)}\n\n".encode()
            yield b"data: [DONE]\n\n"
        finally:
            await http.aclose()

    return StreamingResponse(gen(), media_type="text/event-stream")


# --------------------------------------------------------------- /admin ---

@app.post("/admin/clients")
async def admin_create_client(request: Request):
    require_admin(request)
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, detail=openai_error("name is required"))

    requested = set(body.get("allowed_models") or ALLOWED_MODELS)
    allowed = sorted(requested & ALLOWED_MODELS)
    if not allowed:
        raise HTTPException(400, detail=openai_error(
            f"allowed_models must intersect {sorted(ALLOWED_MODELS)}"))

    from gateway.config import DEFAULT_DAILY_TOKEN_LIMIT, DEFAULT_RPM_LIMIT
    from gateway.auth import generate_token, hash_token

    rpm = int(body.get("rpm_limit") or DEFAULT_RPM_LIMIT)
    daily = int(body.get("daily_token_limit") or DEFAULT_DAILY_TOKEN_LIMIT)

    label = "".join(ch for ch in name.lower() if ch.isalnum())[:6] or "cli"
    token = generate_token(label)
    client_id = store.create_client(name, token_prefix(token), hash_token(token), allowed, rpm, daily)

    return {
        "id": client_id, "name": name, "token": token,
        "allowed_models": allowed, "rpm_limit": rpm, "daily_token_limit": daily,
        "warning": "This token is shown once. Store it now — it cannot be retrieved again.",
    }


@app.get("/admin/clients")
async def admin_list_clients(request: Request):
    require_admin(request)
    out = []
    for c in store.list_clients():
        c = dict(c)
        c.pop("token_hash", None)
        c["allowed_models"] = json.loads(c["allowed_models"])
        out.append(c)
    return out


@app.post("/admin/clients/{client_id}/enable")
async def admin_set_enabled(client_id: int, request: Request):
    require_admin(request)
    body = await request.json()
    ok = store.set_client_enabled(client_id, bool(body.get("enabled", True)))
    if not ok:
        raise HTTPException(404, detail=openai_error("No such client"))
    return {"id": client_id, "enabled": bool(body.get("enabled", True))}


@app.post("/admin/clients/{client_id}/revoke")
async def admin_revoke(client_id: int, request: Request):
    require_admin(request)
    ok = store.revoke_client(client_id)
    if not ok:
        raise HTTPException(404, detail=openai_error("No such client"))
    return {"id": client_id, "revoked": True}


@app.get("/admin/keys")
async def admin_keys(request: Request):
    require_admin(request)
    return pool.status()


@app.get("/admin/stats")
async def admin_stats(request: Request, hours: int = 24):
    require_admin(request)
    return store.usage_stats(hours)


@app.post("/admin/kill")
async def admin_kill(request: Request):
    require_admin(request)
    body = await request.json()
    enabled = bool(body.get("enabled", True))
    store.set_global_enabled(enabled)
    log.warning(f"global gateway enabled={enabled} (via /admin/kill)")
    return {"global_enabled": enabled}
