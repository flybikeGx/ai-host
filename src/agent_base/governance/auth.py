"""密码哈希与令牌签名。仅用标准库，不引入新依赖。

令牌为 HMAC-SHA256 签名的紧凑格式（``payload.sig``），claims 只放身份不放完整权限，
权限以数据库为权威（GOVERNANCE_DESIGN.md §4.1）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    iterations = 200_000
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, iterations, salt_hex, digest_hex = encoded.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                     bytes.fromhex(salt_hex), int(iterations))
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def issue_token(payload: dict, secret: str, *, ttl: int = 1200) -> str:
    body_payload = {**payload, "iat": int(time.time()), "exp": int(time.time()) + ttl}
    body = _b64(json.dumps(body_payload, separators=(",", ":")).encode("utf-8"))
    sig = _b64(hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{sig}"


def decode_token(token: str, secret: str) -> dict:
    try:
        body, sig = token.split(".", 1)
        expected = _b64(hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            raise ValueError("令牌签名无效")
        payload = json.loads(_unb64(body))
        if not isinstance(payload, dict) or not isinstance(payload.get("sub"), str) or not payload["sub"]:
            raise ValueError("令牌身份无效")
        expiry = payload.get("exp")
        if isinstance(expiry, bool) or not isinstance(expiry, (int, float)):
            raise ValueError("令牌有效期无效")
        import math
        if not math.isfinite(expiry):
            raise ValueError("令牌有效期无效")
        if expiry <= time.time():
            raise ValueError("令牌已过期")
        return payload
    except (ValueError, TypeError, UnicodeError) as error:
        if str(error) == "令牌已过期":
            raise ValueError("令牌已过期") from None
        raise ValueError("令牌无效") from None
