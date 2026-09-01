from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any, cast

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


class SecurityError(ValueError):
    pass


class TokenCipher:
    def __init__(self, secret: str):
        if not secret:
            raise SecurityError("TOKEN_ENCRYPTION_SECRET is required")
        key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
        self._fernet = Fernet(key)

    def encrypt(self, value: str) -> str:
        if not value:
            return ""
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        if not value:
            return ""
        try:
            return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise SecurityError("encrypted token is invalid") from exc


class OAuthStateSigner:
    def __init__(self, secret: str, ttl_seconds: int = 600):
        if not secret:
            raise SecurityError("OAUTH_STATE_SECRET is required")
        self._secret = secret.encode("utf-8")
        self._ttl_seconds = ttl_seconds

    def sign(self, nonce: str) -> str:
        payload = {"nonce": nonce, "iat": int(time.time())}
        encoded = (
            base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
            .decode("ascii")
            .rstrip("=")
        )
        signature = hmac.new(self._secret, encoded.encode("ascii"), hashlib.sha256).hexdigest()
        return f"{encoded}.{signature}"

    def verify(self, value: str) -> dict[str, Any]:
        try:
            encoded, signature = value.rsplit(".", 1)
        except ValueError as exc:
            raise SecurityError("OAuth state is malformed") from exc
        expected = hmac.new(self._secret, encoded.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise SecurityError("OAuth state signature is invalid")
        padded = encoded + "=" * (-len(encoded) % 4)
        try:
            raw_payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise SecurityError("OAuth state payload is invalid") from exc
        if not isinstance(raw_payload, dict):
            raise SecurityError("OAuth state payload is invalid")
        payload = cast(dict[str, Any], raw_payload)
        issued_at = int(payload.get("iat", 0))
        if issued_at <= 0 or time.time() - issued_at > self._ttl_seconds:
            raise SecurityError("OAuth state has expired")
        return payload


def verify_feishu_signature(
    *, timestamp: str, nonce: str, encrypt_key: str, raw_body: bytes, signature: str
) -> bool:
    digest = hashlib.sha256()
    digest.update(timestamp.encode("utf-8"))
    digest.update(nonce.encode("utf-8"))
    digest.update(encrypt_key.encode("utf-8"))
    digest.update(raw_body)
    return hmac.compare_digest(digest.hexdigest(), signature)


def verify_request_timestamp(
    timestamp: str,
    *,
    now: float | None = None,
    tolerance_seconds: int = 300,
) -> bool:
    """Reject malformed or stale signed requests to limit replay attacks."""
    try:
        request_time = int(timestamp)
    except (TypeError, ValueError):
        return False
    current_time = time.time() if now is None else now
    return abs(current_time - request_time) <= tolerance_seconds


def decrypt_feishu_event(encrypted: str, encrypt_key: str) -> dict[str, Any]:
    try:
        ciphertext = base64.b64decode(encrypted)
        key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()
        iv, body = ciphertext[:16], ciphertext[16:]
        decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        padded = decryptor.update(body) + decryptor.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        plaintext = unpadder.update(padded) + unpadder.finalize()
        raw_payload = json.loads(plaintext.decode("utf-8"))
        if not isinstance(raw_payload, dict):
            raise SecurityError("decrypted Feishu event is not an object")
        return cast(dict[str, Any], raw_payload)
    except Exception as exc:  # crypto/parsing failures intentionally share one public error
        raise SecurityError("unable to decrypt Feishu event") from exc


def require_bearer(value: str | None, expected_token: str) -> None:
    if not expected_token:
        raise SecurityError("server bearer token is not configured")
    expected = f"Bearer {expected_token}"
    if value is None or not hmac.compare_digest(value, expected):
        raise SecurityError("invalid bearer token")
