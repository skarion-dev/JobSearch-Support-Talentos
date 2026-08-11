"""
Identity from Cloudflare Access.

Cloudflare Access authenticates the user before any request reaches this app
and forwards their verified identity in request headers. There is no login
form here and no password to store — if a request arrives, Access already
approved it.

Every authenticated user can push to Talentos (an operator decision), so the
Access allowlist is the only authorisation boundary. The compensating control
is attribution: whoever is signed in is recorded on every application and
event they create, so a bad push can always be traced to a person.

Local development has no Cloudflare in front of it, so it falls back to a
LOCAL_DEV_USER identity and the UI says so plainly.
"""
import os

import streamlit as st

# Set by Cloudflare Access on every proxied request.
EMAIL_HEADER = "Cf-Access-Authenticated-User-Email"
# Present only when a real Access session exists; absent for direct LAN hits.
JWT_HEADER = "Cf-Access-Jwt-Assertion"

LOCAL_DEV_USER = os.getenv("LOCAL_DEV_USER", "local-dev@skarion.com")


def _headers() -> dict:
    """Request headers, tolerant of Streamlit version differences."""
    try:
        return dict(st.context.headers or {})
    except Exception:
        return {}


def current_user() -> dict:
    """
    Returns {email, source, verified}.

      source='cloudflare'  a real Access session — verified identity
      source='local'       direct/LAN access with no Access in front
    """
    h = {k.lower(): v for k, v in _headers().items()}
    email = h.get(EMAIL_HEADER.lower())
    has_jwt = bool(h.get(JWT_HEADER.lower()))

    if email:
        return {"email": email, "source": "cloudflare", "verified": has_jwt}
    return {"email": LOCAL_DEV_USER, "source": "local", "verified": False}


def actor_label() -> str:
    """Short string stamped onto rows written to Talentos, for attribution."""
    u = current_user()
    prefix = "" if u["source"] == "cloudflare" else "local:"
    return f"{prefix}{u['email']}"[:80]


def render_identity_badge():
    """Small who-am-I indicator, plus a warning when access is unauthenticated."""
    u = current_user()
    if u["source"] == "cloudflare":
        st.sidebar.success(f"Signed in\n\n**{u['email']}**")
    else:
        st.sidebar.warning(
            f"**Unauthenticated ({u['email']})**\n\n"
            "No Cloudflare Access session — this is direct/LAN access. "
            "Writes are still attributed but not identity-verified."
        )
    return u
