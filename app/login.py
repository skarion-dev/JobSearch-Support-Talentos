"""
Application login.

This is defence in depth, not the primary boundary. Cloudflare Access should
still gate jobs.skarion.com — this exists so that a misconfigured or
not-yet-configured Access policy does not leave the app wide open, and so a
shared link cannot be used by whoever receives it.

The password is never stored in plaintext or in the repo. Only a salted
PBKDF2-SHA256 hash lives in .env (gitignored), and it is compared in constant
time.

Set or rotate the password with:
    python -m scripts.set_admin_password
"""
import hmac
import hashlib
import os

import streamlit as st

ITERATIONS = 240_000
SESSION_KEY = "_authed_user"


def hash_password(password: str, salt: bytes | None = None) -> str:
    """Returns 'salt_hex$hash_hex' for storage in .env."""
    salt = salt or os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return f"{salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, hash_hex = stored.split("$", 1)
    except ValueError:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), ITERATIONS)
    # constant time, so a wrong password cannot be found by timing
    return hmac.compare_digest(dk.hex(), hash_hex)


def _configured() -> tuple[str, str]:
    return os.getenv("APP_ADMIN_USER", ""), os.getenv("APP_ADMIN_PASSWORD_HASH", "")


def require_login() -> str | None:
    """
    Blocks the page until the user authenticates. Returns the username.

    If no password is configured the app stays open — otherwise a bad deploy
    would lock everyone out — but it says so loudly rather than failing quietly.
    """
    user, pw_hash = _configured()
    if not user or not pw_hash:
        st.warning(
            "**No app password configured.** Anyone who can reach this URL can use it. "
            "Set one with `python -m scripts.set_admin_password`."
        )
        return None

    if st.session_state.get(SESSION_KEY):
        return st.session_state[SESSION_KEY]

    st.markdown("### Sign in")
    st.caption("Talentos JobSearch Support")

    with st.form("login", clear_on_submit=False):
        u = st.text_input("Username")
        p = st.text_input("Password", type="password")
        ok = st.form_submit_button("Sign in", type="primary")

    if ok:
        if hmac.compare_digest(u.strip(), user) and verify_password(p, pw_hash):
            st.session_state[SESSION_KEY] = u.strip()
            st.rerun()
        else:
            # deliberately does not say which field was wrong
            st.error("Incorrect username or password.")

    st.stop()


def logout_button():
    if st.session_state.get(SESSION_KEY):
        if st.sidebar.button("Sign out", use_container_width=True):
            st.session_state.pop(SESSION_KEY, None)
            st.rerun()
