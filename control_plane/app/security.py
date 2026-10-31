from __future__ import annotations

import hashlib
import hmac
import os
import secrets


APP_SECRET = os.environ.get("APP_SECRET", "")
if len(APP_SECRET) < 32:
    raise RuntimeError("APP_SECRET must be set to at least 32 characters")


def random_token() -> str:
    return secrets.token_urlsafe(32)


def digest(value: str) -> str:
    return hmac.new(APP_SECRET.encode(), value.encode(), hashlib.sha256).hexdigest()


def hash_password(password: str) -> str:
    if len(password) < 12 or len(password) > 1024:
        raise ValueError("Password must be between 12 and 1024 characters")
    salt = secrets.token_bytes(16)
    result = hashlib.scrypt(password.encode(), salt=salt, n=2**15, r=8, p=1, maxmem=64 * 1024 * 1024)
    return f"scrypt${salt.hex()}${result.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, salt_hex, expected_hex = encoded.split("$", 2)
        if algorithm != "scrypt":
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(expected_hex)
        actual = hashlib.scrypt(password.encode(), salt=salt, n=2**15, r=8, p=1, maxmem=64 * 1024 * 1024)
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False
