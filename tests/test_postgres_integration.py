from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.models import Chat, InboxStatus, OAuthTokens, OAuthUserInfo, User
from app.postgres import PostgresRepository
from app.security import TokenCipher
from app.service import MentionProcessor, StatusService


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_migration_deduplication_and_status_isolation() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is not configured")

    repo = PostgresRepository(database_url, TokenCipher("integration-test-cipher-secret"))
    await repo.open()
    try:
        suffix = uuid4().hex
        tenant = f"tenant-integration-{suffix}"
        active_users = []
        for index in (1, 2):
            user_id = f"integration-user-{suffix}-{index}"
            await repo.enable_user(
                User(
                    tenant_key=tenant,
                    user_id=user_id,
                    open_id=f"integration-open-{suffix}-{index}",
                    name=f"Integration User {index}",
                )
            )
            active = await repo.activate_user(
                OAuthUserInfo(
                    tenant_key=tenant,
                    user_id=user_id,
                    open_id=f"integration-open-{suffix}-{index}",
                    name=f"Integration User {index}",
                ),
                OAuthTokens(
                    access_token=f"access-{index}",
                    refresh_token=f"refresh-{index}",
                    expires_in=3600,
                    refresh_expires_in=7200,
                ),
            )
            assert active is not None
            active_users.append(active)

        await repo.upsert_chat(
            Chat(
                tenant_key=tenant,
                chat_id=f"integration-chat-{suffix}",
                name="Integration Chat",
                bot_present=True,
            )
        )
        payload = {
            "header": {
                "tenant_key": tenant,
                "event_id": "integration-event",
                "event_type": "im.message.receive_v1",
            },
            "event": {
                "sender": {
                    "sender_id": {"user_id": "integration-sender"},
                    "name": "Integration Sender",
                },
                "message": {
                    "message_id": f"integration-message-{suffix}",
                    "chat_id": f"integration-chat-{suffix}",
                    "chat_type": "group",
                    "message_type": "text",
                    "content": json.dumps({"text": "Please review"}),
                    "create_time": str(int(datetime.now(UTC).timestamp() * 1000)),
                    "mentions": [
                        {
                            "key": f"@_user_{index}",
                            "name": f"Integration User {index}",
                            "id": {
                                "user_id": f"integration-user-{suffix}-{index}",
                                "open_id": f"integration-open-{suffix}-{index}",
                            },
                        }
                        for index in (1, 2)
                    ],
                },
            },
        }

        processor = MentionProcessor(repo, allowed_tenant_key=tenant)
        created = await processor.process_receive_event(payload)
        duplicated = await processor.process_receive_event(payload)

        assert len(created) == 2
        assert duplicated == []

        first = next(item for item in created if item.target_user_id == active_users[0].id)
        second = next(item for item in created if item.target_user_id == active_users[1].id)
        updated = await StatusService(repo).update_from_bitable(
            item_id=first.id,
            status=InboxStatus.DONE,
            note="handled",
            expected_version=first.version,
        )
        second_context = await repo.get_inbox_context(second.id)

        assert updated is not None and updated.status == InboxStatus.DONE
        assert second_context is not None
        assert second_context[0].status == InboxStatus.PENDING
    finally:
        await repo.close()
