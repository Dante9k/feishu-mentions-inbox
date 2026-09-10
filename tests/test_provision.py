from pathlib import Path
from typing import Any

import pytest

from app.local import initialize
from app.provision import TABLES, Provisioner
from tests.test_local_mode import local_settings


class FakeClient:
    def __init__(self) -> None:
        self.tables: list[dict[str, Any]] = []
        self.fields: dict[str, list] = {}
        self.writes = 0

    async def tenant_access_token(self) -> str:
        return "synthetic-token"

    async def _request(self, method, path, params=None, json_body=None):
        if path.endswith("/apps"):
            self.writes += 1
            return {"app": {"app_token": "base_test", "url": "https://example.test/base"}}
        if path.endswith("/tables"):
            if method == "GET":
                return {"items": self.tables}
            self.writes += 1
            table = json_body["table"]
            table_id = f"tbl_{len(self.tables)}"
            self.tables.append({"name": table["name"], "table_id": table_id})
            self.fields[table_id] = table["fields"]
            return {"table_id": table_id}
        if path.endswith("/fields"):
            return {"items": self.fields[path.split("/")[-2]]}
        raise AssertionError("unexpected endpoint")


@pytest.mark.asyncio
async def test_structure_provisioning_is_resumable_without_granting_access(tmp_path: Path) -> None:
    path = tmp_path / ".env.local"
    initialize(path)
    client = FakeClient()
    first = await Provisioner(path, local_settings(), client).run()
    assert first["structure_ready"]
    assert first["employee_access_granted"] is False
    assert client.writes == 1 + len(TABLES)
    await Provisioner(path, local_settings(), client).run()
    assert client.writes == 1 + len(TABLES)
