"""
Gateway configuration.

Reads the REAL OpenCode Go credentials directly from the environment — this
is deliberately independent of app/config.py. Once the JobSearch app itself
is switched to call the gateway (see app/config.py's GATEWAY_URL / GATEWAY_KEY
override), app/config.py's OPENCODE_API_KEY stops being an upstream secret at
all and becomes a gateway-issued client token. The gateway process must not
go through that indirection for its own pool, or it would try to authenticate
to OpenCode with a token OpenCode has never seen.

Upstream key pool: OPENCODE_API_KEY plus OPENCODE_KEY_1..N, in that order.
Adding a fallback key later is a .env line — no code change and no restart
logic beyond the existing one (env is re-read at process start).
"""
import os

from dotenv import load_dotenv

load_dotenv()

UPSTREAM_BASE_URL = os.getenv("OPENCODE_BASE_URL", "https://opencode.ai/zen/go/v1")


def _load_upstream_keys() -> list[str]:
    keys = []
    primary = os.getenv("OPENCODE_API_KEY", "")
    if primary:
        keys.append(primary)
    i = 1
    while True:
        k = os.getenv(f"OPENCODE_KEY_{i}", "")
        if not k:
            break
        keys.append(k)
        i += 1
    seen, out = set(), []
    for k in keys:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


UPSTREAM_KEYS = _load_upstream_keys()

# The models this gateway will forward. Enforced in code (GET /v1/models
# reports only these; POST /v1/chat/completions 403s on anything else) rather
# than trusted to whatever a caller asks for — see CONTRIBUTING.md's "rules
# go in code, not in prompts". This is still a real boundary even though it
# is now the full catalogue: it is what stands between "a gateway token" and
# "the whole subscription plus every future model OpenCode adds without
# anyone here deciding to allow it."
#
# A hardcoded snapshot, not a live fetch from OpenCode at startup — a gateway
# whose allowlist depends on an upstream HTTP call succeeding at boot is a
# new failure mode for a small, rarely-changing list. Refresh it by re-running
# the same probe that produced this list and diffing:
#
#   python -c "import httpx,os; from dotenv import load_dotenv; load_dotenv(); \
#     k=os.getenv('OPENCODE_API_KEY'); b=os.getenv('OPENCODE_BASE_URL'); \
#     r=httpx.get(f'{b}/models', headers={'Authorization':f'Bearer {k}'}); \
#     print(sorted(m['id'] for m in r.json().get('data', r.json())))"
#
# Snapshot taken 2026-08-12. Widening this alone does not widen what an
# already-issued client token can call — each token's own allowed_models in
# gateway.db is intersected against this set, so an existing client stays
# capped at whatever it was issued with until updated via
# `scripts.gateway_admin set-models --id <n> --models all`.
ALLOWED_MODELS = {
    "deepseek-v4-flash", "deepseek-v4-pro",
    "glm-5", "glm-5.1", "glm-5.2",
    "gpt-5.6-luna",
    "grok-4.5",
    "hy3", "hy3-preview",
    "kimi-k2.5", "kimi-k2.6", "kimi-k2.7-code", "kimi-k3",
    "mimo-v2-omni", "mimo-v2-pro", "mimo-v2.5", "mimo-v2.5-pro",
    "minimax-m2.5", "minimax-m2.7", "minimax-m3",
    "qwen3.5-plus", "qwen3.6-plus", "qwen3.7-max", "qwen3.7-plus", "qwen3.8-max",
}

# The keyword strategist's model always runs at reasoningEffort "high" — this
# is the one nightly decision (keyword selection) where being wrong wastes
# the whole night's API budget, so the strongest available reasoning is
# affordable for it. Forced here, in code, so no caller can silently
# downgrade it by omission. Both the primary and its fallback are covered,
# since either may end up carrying the decision on a given night.
FORCED_REASONING = {"kimi-k2.7-code": "high", "minimax-m3": "high"}

GATEWAY_DB_PATH = os.getenv("GATEWAY_DB_PATH", os.path.join(
    os.path.dirname(__file__), "gateway.db"))
GATEWAY_PORT = int(os.getenv("GATEWAY_PORT", "8787"))
GATEWAY_HOST = os.getenv("GATEWAY_HOST", "127.0.0.1")

# Separate secret from client tokens — gates /admin/* only. Never logged, never
# issued to a caller. Required to start; a gateway with no admin boundary is
# not administrable safely once it is on a tunnel.
GATEWAY_ADMIN_TOKEN = os.getenv("GATEWAY_ADMIN_TOKEN", "")

DEFAULT_RPM_LIMIT = int(os.getenv("GATEWAY_DEFAULT_RPM", "120"))
DEFAULT_DAILY_TOKEN_LIMIT = int(os.getenv("GATEWAY_DEFAULT_DAILY_TOKENS", "2000000"))

# How long a key that returned 429 sits out before being retried.
COOLDOWN_SECONDS = int(os.getenv("GATEWAY_COOLDOWN_SECONDS", "60"))

REQUEST_TIMEOUT = float(os.getenv("GATEWAY_UPSTREAM_TIMEOUT", "120"))
