# LLM Gateway

An OpenAI-compatible proxy in front of the OpenCode Go subscription, so
callers outside this repo (starting with Talentos) can use it without ever
holding the real OpenCode key.

```
Talentos ──► https://llm.skarion.com ──► gateway :8787 ──► OpenCode Go
             (Cloudflare Tunnel,          (rotates OPENCODE_API_KEY,
              NOT behind Access —          OPENCODE_KEY_1, OPENCODE_KEY_2, ...
              see "Exposure" below)        on 429/401)
JobSearch app ───────────────────────────────┘
```

**Status: built and tested locally. Not yet deployed to the spare PC, not
yet exposed on a tunnel, no key issued to Talentos.** That is deliberate —
see "Rollout" below.

---

## Why this exists

Before this, every agent in `app/agents/` built its own `OpenAI(...)` client
straight from `OPENCODE_API_KEY`. That key is the whole subscription: no
rotation if it hits a quota, no limit on what model gets called, no log of
who called what, and no way to hand it to Talentos without handing over
everything.

The gateway is the one thing standing between "a token" and "the whole
subscription":

- **Model allowlist, enforced in code.** `GET /v1/models` returns only
  `gpt-5.6-luna`, `deepseek-v4-flash`, `deepseek-v4-pro` — the three this
  system actually uses. `POST /v1/chat/completions` 403s anything else, no
  matter what a caller asks for. Same convention as `app/filters.py`'s
  location gates: rules live in code, not in a prompt or a gentleman's
  agreement.
- **Key rotation**, so a 429 on one OpenCode key fails over to the next
  instead of stalling whatever's running.
- **Per-client tokens**, independent of the upstream key, each with its own
  rate limit, daily token budget, and on/off switch — revocable without
  touching the real credential.
- **A kill switch** that stops all traffic instantly, and per-request
  logging (`gateway/gateway.db`) of who called what, when, and how much it
  cost.

## Reasoning effort

`gpt-5.6-luna` always runs at `reasoning_effort: "high"` — forced in
`gateway/config.py`'s `FORCED_REASONING`, applied in
`gateway/main.py::chat_completions` regardless of what the caller sends. If
a caller passes a different value it is overridden and logged, not silently
dropped:

```
gpt-5.6-luna: overriding requested reasoning_effort='low' with 'high'
```

Luna runs once a night (`app/agents/keyword_strategist.py`) to allocate the
whole night's API budget — the one call in this system where the strongest
available reasoning is worth paying for every time.

## Everything else routes here too

The rollout decision was **both**: Talentos AND this app's own agents call
the gateway, not OpenCode directly. `app/config.py` already has the
override wired in:

```python
GATEWAY_URL = os.getenv("GATEWAY_URL", "")
GATEWAY_KEY = os.getenv("GATEWAY_KEY", "")
# both set -> OPENCODE_API_KEY/OPENCODE_BASE_URL become the gateway URL and
# a gateway-issued token. Unset -> falls back to calling OpenCode directly,
# unchanged from before the gateway existed.
```

No agent file changes — `matcher_agent.py`, `description_agent.py`,
`analyst_agent.py`, `keyword_strategist.py`, `ceo_agent.py` all resolve
their client from `OPENCODE_API_KEY`/`OPENCODE_BASE_URL` (directly or via
`LLM_CONFIG`/`CEO_LLM_CONFIG`), so flipping the two `GATEWAY_*` vars in
`.env` is the entire migration. Model names sent (`deepseek-v4-flash`,
`deepseek-v4-pro`, `gpt-5.6-luna`) already match the gateway's allowlist
exactly.

This ordering matters: the nightly cycle running 100-wide through the
gateway is the load test that proves rotation and limits work under real
concurrency, before Talentos — which this repo does not control and cannot
quickly patch — ever depends on it.

## What "prepare the spare pc" means, concretely

1. Deploy `gateway/` to `192.168.1.193` (same repo, `git pull`).
2. Run it as its own process on `127.0.0.1:8787`, registered as a scheduled
   task like `JobSearchApp` and `CloudflareTunnel` already are.
3. Issue this app's own client token there (`jobsearch-app`), set
   `GATEWAY_URL=http://127.0.0.1:8787/v1` and `GATEWAY_KEY=<that token>` in
   the server's `.env`, redeploy the app.
4. Confirm a full nightly cycle runs clean through the gateway.
5. **Not yet done in this pass:** put it on the tunnel, issue Talentos a
   key, or touch Talentos' own config. That is the explicit next step, not
   part of this one.

---

## Running it

```bash
python -m uvicorn gateway.main:app --host 127.0.0.1 --port 8787 --workers 1
```

**`--workers 1` is not a tuning knob.** `gateway/keys.py`'s rotation and
cooldown state lives in process memory. A second worker process would have
its own independent view of which upstream keys are cooling down or dead —
two processes could both pick a key the other just got 429'd from, defeating
the entire point of rotation. If load ever demands more than one process,
that state needs to move into `gateway/gateway.db` first.

On the server this is `deploy/gateway_launch.ps1` (matches the existing
`launch_app.ps1` pattern) registered as a scheduled task:

```powershell
schtasks /Create /TN LLMGateway /TR "powershell -ExecutionPolicy Bypass -File C:\JobSearch-Support-Talentos\deploy\gateway_launch.ps1" /SC ONSTART /RU <user> /F
schtasks /Run /TN LLMGateway
```

### ⚠ `deploy.ps1` will kill this process too

The app's existing `deploy.ps1` restarts the app by matching **every**
`python.exe` under the `Python312` install path and force-stopping it —
that is a match by install path, not by role, so it will also kill the
gateway (and would kill a nightly cycle in flight, though that one just
re-runs tomorrow). Until `deploy.ps1` is made role-aware:

**After running `deploy.ps1`, always follow it with:**

```bash
ssh saki-@192.168.1.193 "schtasks /Run /TN LLMGateway"
```

To redeploy the gateway on its own, without touching the app, use
`deploy/gateway_deploy.ps1` instead — it matches only the gateway's own
`uvicorn ... gateway.main:app` command line, so it never touches
`JobSearchApp` or `JobSearchNightly`.

---

## Issuing a client token

Writes straight to `gateway/gateway.db` — the gateway process doesn't need
to be running.

```bash
python -m scripts.gateway_issue_key --name jobsearch-app
python -m scripts.gateway_issue_key --name talentos --rpm 200 --daily-tokens 3000000
python -m scripts.gateway_issue_key --name talentos --models deepseek-v4-flash,deepseek-v4-pro
```

The token is printed once. It cannot be recovered — only the salted
PBKDF2-SHA256 hash is stored (same construction as `app/login.py`). If it's
lost, `revoke` the client and issue a new one.

## Operating it

```bash
python -m scripts.gateway_admin list                 # clients, their limits, enabled state
python -m scripts.gateway_admin keys                  # upstream OpenCode key pool: ok / cooling / dead
python -m scripts.gateway_admin stats --hours 24       # requests, tokens, errors, rejections per client+model
python -m scripts.gateway_admin disable --id 2         # turn off one client without deleting it
python -m scripts.gateway_admin enable  --id 2
python -m scripts.gateway_admin revoke  --id 2          # permanent — issue a new token to replace it
python -m scripts.gateway_admin kill-switch --off       # stop ALL traffic through the gateway
python -m scripts.gateway_admin kill-switch --on
```

The same operations exist over HTTP under `/admin/*`, gated by
`X-Admin-Token: <GATEWAY_ADMIN_TOKEN>` (a secret separate from any client
token — never issue it to a caller). `POST /admin/clients` is the HTTP
equivalent of `gateway_issue_key.py`, for issuing keys without shell access
to the server.

## What gets logged, and what doesn't

Every request — success, upstream failure, or local rejection (bad model,
rate limit, daily cap) — writes one row to `gateway_requests`, tagged
`ok` / `error` / `rejected`. Rejections are logged deliberately: a client
hammering a disallowed model or blowing through its rate limit is exactly
the signal a leaked or misused token looks like, and it would be invisible
if only successful calls were recorded.

**Not logged:** token counts for streamed responses. OpenCode's streaming
response doesn't include a usage block by default, and this gateway does
not parse SSE to reconstruct one — a streamed call's row has `prompt_tokens`
and `completion_tokens` as `NULL`. Streaming also never fails over
mid-response: the key is picked before the first byte is relayed, and a key
dying after that surfaces to the caller as a truncated response, not a
retry. **No caller in this system streams today** — the app's own agents
all call `chat.completions.create(...)` without `stream=True`. This only
matters once/if Talentos is wired up as a streaming caller.

## Error shape

Every rejection returns the exact shape OpenAI SDKs expect at the top
level — `{"error": {"message", "type", "code"}}` — so a rejected call
surfaces to a caller's SDK as a normal, readable exception
(`openai.PermissionDeniedError`, `openai.RateLimitError`, etc.) instead of
an unparseable body. FastAPI's default exception handler wraps
`HTTPException.detail` under a `"detail"` key; `gateway/main.py` overrides
that handler specifically so this contract holds — do not remove
`openai_shaped_http_exception` when touching error handling.

## Exposure

Planned hostname: `llm.skarion.com`, same Cloudflare Tunnel machine as
`jobs.skarion.com`, but **Cloudflare Access must NOT cover this hostname.**
Access expects an interactive browser login; a bearer-token API call from
Talentos' backend has no browser to complete that flow with. The gateway's
own token auth is the access control for this hostname — Access still
belongs on `jobs.skarion.com` (and per `docs/RUNBOOK.md`, still isn't
configured there either).

## Adding fallback OpenCode keys

```
OPENCODE_API_KEY=...      # primary
OPENCODE_KEY_1=...        # additional pool members
OPENCODE_KEY_2=...
```

`.env` lines only — `gateway/config.py` reads them at process start, no
code change. `gateway/keys.py` rotates round-robin across whatever's not
currently cooling down (429, 60s default) or marked dead (401/403,
permanent until restart).
