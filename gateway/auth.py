"""
Client token hashing and generation.

Same PBKDF2-SHA256 construction as app/login.py, reused rather than
re-invented. Tokens are never stored in plaintext — only the salted hash. The
first 12 characters (a fixed, non-secret prefix) are kept alongside the hash
so clients can be identified in logs and the admin UI without ever storing
anything that lets you reconstruct the secret.
"""
import hashlib
import hmac
import os
import secrets

ITERATIONS = 240_000
PREFIX_LEN = 12


def generate_token(label: str = "tal") -> str:
    """sk-<label>-<32 random url-safe chars>. label identifies the caller at a
    glance in logs (sk-tal-... vs sk-jsa-...) without decoding anything."""
    return f"sk-{label}-{secrets.token_urlsafe(24)}"


def hash_token(token: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", token.encode(), salt, ITERATIONS)
    return f"{salt.hex()}${dk.hex()}"


def verify_token(token: str, stored: str) -> bool:
    try:
        salt_hex, hash_hex = stored.split("$", 1)
    except ValueError:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", token.encode(), bytes.fromhex(salt_hex), ITERATIONS)
    return hmac.compare_digest(dk.hex(), hash_hex)


def token_prefix(token: str) -> str:
    return token[:PREFIX_LEN]
