from __future__ import annotations

from uuid import uuid4

import pytest

from app.models import InboxStatus
from app.workers import BackgroundSupervisor, _coalesce_outbox_jobs


class ReconciliationRepository:
    def __init__(self) -> None:
        self.item_id = uuid4()
        self.updates: list[tuple] = []

    async def inbox_reconciliation_state(self):
        return {
            "rec_current": {
                "id": self.item_id,
                "status": InboxStatus.PENDING.value,
                "note": "",
                "version": 3,
            },
            "rec_stale": {
                "id": uuid4(),
                "status": InboxStatus.PENDING.value,
                "note": "",
                "version": 4,
            },
        }

    async def update_inbox_item(
        self, item_id, status, note, expected_version=None, changed_at=None
    ):
        self.updates.append((item_id, status, note, expected_version, changed_at))


class ReconciliationBitable:
    async def list_records(self, table_key: str):
        assert table_key == "inbox"
        return [
            {
                "record_id": "rec_current",
                "fields": {"处理状态": "已处理", "处理备注": "已确认", "版本": 3},
            },
            {
                "record_id": "rec_stale",
                "fields": {"处理状态": "已处理", "处理备注": "过期写入", "版本": 3},
            },
        ]


@pytest.mark.asyncio
async def test_reconciliation_pulls_current_base_change_and_ignores_stale_version() -> None:
    repository = ReconciliationRepository()
    supervisor = object.__new__(BackgroundSupervisor)
    supervisor.repository = repository  # type: ignore[assignment]
    supervisor.bitable = ReconciliationBitable()  # type: ignore[assignment]

    await supervisor._pull_bitable_changes()

    assert repository.updates == [(repository.item_id, InboxStatus.DONE, "已确认", 3, None)]


def test_outbox_coalescing_keeps_only_latest_projection_per_entity() -> None:
    first_id, second_id, other_id = uuid4(), uuid4(), uuid4()
    entity_id, other_entity_id = uuid4(), uuid4()
    jobs = [
        {
            "id": first_id,
            "entity_type": "inbox",
            "entity_id": entity_id,
            "table_key": "inbox",
        },
        {
            "id": second_id,
            "entity_type": "inbox",
            "entity_id": entity_id,
            "table_key": "inbox",
        },
        {
            "id": other_id,
            "entity_type": "inbox",
            "entity_id": other_entity_id,
            "table_key": "inbox",
        },
    ]

    latest, superseded = _coalesce_outbox_jobs(jobs)

    assert [job["id"] for job in latest] == [second_id, other_id]
    assert superseded == [first_id]
