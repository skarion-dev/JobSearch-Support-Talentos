"""
Set or rotate the app login.

Writes only a PBKDF2 hash into .env — the plaintext password is never stored
on disk, never committed, and never printed back.

Interactive:  python -m scripts.set_admin_password
Scripted:     python -m scripts.set_admin_password --user admin --password '...'
"""
import argparse
import getpass
import os
import re

from app.login import hash_password

ENV_PATH = os.path.join(os.path.dirname(__file__), "..", ".env")

WEAK_PATTERNS = [
    (re.compile(r"skarion", re.I), "contains the company name"),
    (re.compile(r"20\d\d"), "contains a year"),
    (re.compile(r"^.{0,11}$"), "shorter than 12 characters"),
]


def warn_if_weak(password: str) -> list[str]:
    return [why for pat, why in WEAK_PATTERNS if pat.search(password)]


def upsert_env(user: str, pw_hash: str):
    lines = []
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, encoding="utf-8") as f:
            lines = [
                ln for ln in f.read().splitlines()
                if not ln.startswith(("APP_ADMIN_USER=", "APP_ADMIN_PASSWORD_HASH="))
            ]
    while lines and not lines[-1].strip():
        lines.pop()
    lines += [
        "",
        "# App login (hash only — the plaintext password is never stored)",
        f"APP_ADMIN_USER={user}",
        f"APP_ADMIN_PASSWORD_HASH={pw_hash}",
    ]
    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", help="omit to be prompted without echo")
    a = ap.parse_args()

    password = a.password or getpass.getpass("Password: ")
    if not password:
        raise SystemExit("Empty password, aborting.")

    weaknesses = warn_if_weak(password)
    upsert_env(a.user, hash_password(password))

    print(f"Login set for user '{a.user}'. Only the hash was written to .env.")
    if weaknesses:
        print("\nNOTE — this password " + "; ".join(weaknesses) + ".")
        print("It is guessable for an app that is publicly reachable and writes")
        print("to production. Keep Cloudflare Access in front of it.")
    print("\nRestart the app for it to take effect.")


if __name__ == "__main__":
    main()
