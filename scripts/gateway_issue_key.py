"""
Issue a new gateway client token.

Writes straight to gateway/gateway.db — the gateway process does not need to
be running. The token is printed once and is not recoverable afterward; if
it is lost, revoke the client and issue a new one.

    python -m scripts.gateway_issue_key --name jobsearch-app
    python -m scripts.gateway_issue_key --name talentos --rpm 200 --daily-tokens 3000000
    python -m scripts.gateway_issue_key --name talentos --models deepseek-v4-flash,deepseek-v4-pro
"""
import argparse

from gateway import store
from gateway.auth import generate_token, hash_token, token_prefix
from gateway.config import ALLOWED_MODELS, DEFAULT_DAILY_TOKEN_LIMIT, DEFAULT_RPM_LIMIT


def main(name: str, models: str | None, rpm: int, daily_tokens: int):
    if models:
        requested = {m.strip() for m in models.split(",") if m.strip()}
        allowed = sorted(requested & ALLOWED_MODELS)
        unknown = requested - ALLOWED_MODELS
        if unknown:
            print(f"Ignoring models not on the gateway allowlist: {sorted(unknown)}")
        if not allowed:
            print(f"No valid models given. Allowed: {sorted(ALLOWED_MODELS)}")
            return
    else:
        allowed = sorted(ALLOWED_MODELS)

    label = "".join(ch for ch in name.lower() if ch.isalnum())[:6] or "cli"
    token = generate_token(label)
    client_id = store.create_client(
        name, token_prefix(token), hash_token(token), allowed, rpm, daily_tokens
    )

    print(f"Client #{client_id} '{name}' created.")
    print(f"  models:            {allowed}")
    print(f"  rpm_limit:         {rpm}")
    print(f"  daily_token_limit: {daily_tokens}")
    print()
    print("TOKEN (shown once — save it now):")
    print(f"  {token}")
    print()
    print("Use as: base_url=http://<gateway host>:8787/v1, api_key=<token above>")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True, help="Identifies the caller in logs, e.g. 'jobsearch-app', 'talentos'")
    ap.add_argument("--models", default=None,
                    help="Comma-separated subset of the allowlist. Default: all allowed models.")
    ap.add_argument("--rpm", type=int, default=DEFAULT_RPM_LIMIT)
    ap.add_argument("--daily-tokens", type=int, default=DEFAULT_DAILY_TOKEN_LIMIT)
    a = ap.parse_args()
    main(a.name, a.models, a.rpm, a.daily_tokens)
