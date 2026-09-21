"""
PIN hashing/verification, and session tokens. Deliberately uses only the
Python standard library (hashlib's PBKDF2) rather than adding bcrypt/
passlib as a new dependency — PBKDF2-HMAC-SHA256 with a high iteration
count is a legitimate, secure choice, and it means no requirements.txt
change is needed to ship this.

A PIN is only ever hashed here in response to someone typing it into the
app's own login screen — nothing else in this codebase should ever call
hash_pin() with a value that didn't come directly from that form.
"""
import hashlib
import hmac
import os
import secrets

PBKDF2_ITERATIONS = 260_000


def hash_pin(pin: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"{salt.hex()}${digest.hex()}"


def verify_pin(pin: str, stored: str) -> bool:
    try:
        salt_hex, digest_hex = stored.split("$", 1)
    except (ValueError, AttributeError):
        return False
    salt = bytes.fromhex(salt_hex)
    expected = bytes.fromhex(digest_hex)
    actual = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return hmac.compare_digest(actual, expected)  # constant-time, avoids timing attacks


def generate_token() -> str:
    return secrets.token_urlsafe(32)
