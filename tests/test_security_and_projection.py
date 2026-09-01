from __future__ import annotations

import base64
import hashlib
import json
import time
from datetime import UTC, datetime

import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from app.models import InboxItem, MentionType, SourceMessage, User
from app.projections import inbox_fields
from app.security import (
    OAuthStateSigner,
    SecurityError,
    TokenCipher,
    decrypt_feishu_event,
    verify_feishu_signature,
    verify_request_timestamp,
)


def test_token_cipher_round_trip_and_wrong_key_rejection() -> None:
    encrypted = TokenCipher("one-secret").encrypt("refresh-token")
    assert TokenCipher("one-secret").decrypt(encrypted) == "refresh-token"
    with pytest.raises(SecurityError):
        TokenCipher("another-secret").decrypt(encrypted)


def test_oauth_state_signature_and_expiry() -> None:
    signer = OAuthStateSigner("state-secret", ttl_seconds=60)
    state = signer.sign("nonce")
    assert signer.verify(state)["nonce"] == "nonce"
    with pytest.raises(SecurityError):
        signer.verify(f"{state}tampered")

    expired_signer = OAuthStateSigner("state-secret", ttl_seconds=-1)
    with pytest.raises(SecurityError):
        expired_signer.verify(expired_signer.sign("expired"))


def test_feishu_signature_matches_documented_concatenation() -> None:
    body = b'{"encrypt":"ciphertext"}'
    timestamp, nonce, key = "1787673600", "nonce", "encrypt-key"
    expected = hashlib.sha256(timestamp.encode() + nonce.encode() + key.encode() + body).hexdigest()
    assert verify_feishu_signature(
        timestamp=timestamp,
        nonce=nonce,
        encrypt_key=key,
        raw_body=body,
        signature=expected,
    )


def test_signed_request_timestamp_must_be_recent() -> None:
    now = time.time()
    assert verify_request_timestamp(str(int(now)), now=now)
    assert not verify_request_timestamp(str(int(now - 301)), now=now)
    assert not verify_request_timestamp("not-a-timestamp", now=now)


def test_feishu_event_decryption() -> None:
    encrypt_key = "encrypt-key"
    payload = {"header": {"event_type": "im.message.receive_v1"}, "event": {}}
    key = hashlib.sha256(encrypt_key.encode()).digest()
    iv = b"0123456789abcdef"
    padder = padding.PKCS7(128).padder()
    padded = padder.update(json.dumps(payload).encode()) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    encrypted = base64.b64encode(iv + encryptor.update(padded) + encryptor.finalize()).decode()
    assert decrypt_feishu_event(encrypted, encrypt_key) == payload


def test_projection_contains_target_member_and_independent_item_id() -> None:
    user = User(tenant_key="t", user_id="u1", open_id="ou_1", name="用户一")
    source = SourceMessage(
        tenant_key="t",
        message_id="om_1",
        chat_id="oc_1",
        chat_name="项目群",
        sender_id="sender",
        sender_name="发送者",
        message_type="text",
        content="请确认",
        sent_at=datetime.now(UTC),
    )
    item = InboxItem(
        source_message_id=source.id,
        target_user_id=user.id,
        mention_type=MentionType.DIRECT,
    )

    fields = inbox_fields(item, source, user)

    assert fields["目标用户"] == [{"id": "ou_1"}]
    assert fields["内部待办ID"] == str(item.id)
    assert fields["版本"] == 1
    assert fields["处理状态"] == "待处理"
