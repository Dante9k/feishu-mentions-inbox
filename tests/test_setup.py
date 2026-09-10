from __future__ import annotations

import json
import re
import threading
from pathlib import Path

import httpx
import pytest
from dotenv import dotenv_values

from app.local import initialize
from app.setup import credential_server, doctor, save_credentials
from tests.test_local_mode import local_settings


def test_credentials_preserve_encryption_key_and_reject_wrong_app(tmp_path: Path) -> None:
    path = tmp_path / ".env.local"
    initialize(path)
    original = dotenv_values(path)["TOKEN_ENCRYPTION_SECRET"]
    save_credentials(path, "cli_test", "S" * 32)
    save_credentials(path, "cli_test", "N" * 32)
    assert dotenv_values(path)["TOKEN_ENCRYPTION_SECRET"] == original
    assert dotenv_values(path)["FEISHU_APP_SECRET"] == "N" * 32
    with pytest.raises(ValueError):
        save_credentials(path, "cli_different", "X" * 32)


def test_one_use_credential_form_rejects_csrf_and_shuts_down(tmp_path: Path, capsys) -> None:
    path = tmp_path / ".env.local"
    initialize(path)
    # Port 0 is reserved by the OS; requests use the configured Host for this test.
    server = credential_server(path, port=0)
    route = capsys.readouterr().out.strip().split(":0", 1)[1]
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        with httpx.Client(
            base_url=f"http://127.0.0.1:{server.server_port}",
            trust_env=False,
            headers={"Host": "127.0.0.1:0"},
        ) as client:
            response = client.get(route)
            assert response.status_code == 200
            csrf = re.search(r'name="csrf" value="([^"]+)"', response.text).group(1)
            fields = {"csrf": csrf, "app_id": "cli_test", "app_secret": "S" * 32}
            assert client.post(route, data={**fields, "csrf": "bad"}).status_code == 403
            assert (
                client.post(route, data=fields, headers={"Origin": "https://evil.test"}).status_code
                == 403
            )
            assert client.post(route, data=fields).status_code == 200
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert dotenv_values(path)["FEISHU_APP_SECRET"] == "S" * 32
        assert "S" * 32 not in capsys.readouterr().out
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.asyncio
async def test_doctor_does_not_claim_delivery_or_print_secrets() -> None:
    settings = local_settings()
    result = await doctor(settings)
    assert not result["configuration_ready"]
    assert result["message_delivery"] == "not_verified"
    assert settings.admin_api_token not in json.dumps(result)
