"""Explicit, loopback-only launcher. Never implicitly loads production .env."""

from __future__ import annotations

import argparse
import os
import secrets
from pathlib import Path

from .config import Settings


def initialize(path: Path) -> None:
    values = {
        "APP_ENV": "production",
        "LOCAL_ONLY": "true",
        "LOCAL_SINGLE_USER_PILOT": "true",
        "APP_HOST": "127.0.0.1",
        "APP_PORT": "8090",
        "PUBLIC_BASE_URL": "http://127.0.0.1:8090",
        "DATABASE_URL": "sqlite:///./data/mentions-local.db",
        "FEISHU_EVENT_TRANSPORT": "long_connection",
        "RUN_BACKGROUND_WORKERS": "true",
        "RECONCILIATION_INTERVAL_SECONDS": "30",
        "COVERAGE_INTERVAL_SECONDS": "3600",
        "ADMIN_API_TOKEN": secrets.token_urlsafe(48),
        "OAUTH_STATE_SECRET": secrets.token_urlsafe(48),
        "TOKEN_ENCRYPTION_SECRET": secrets.token_urlsafe(48),
        "FEISHU_APP_ID": "",
        "FEISHU_APP_SECRET": "",
        "FEISHU_TENANT_KEY": "",
        "FEISHU_OAUTH_SCOPES": (
            "im:chat:readonly contact:user.employee_id:readonly offline_access"
        ),
        "BITABLE_APP_TOKEN": "",
        "BITABLE_INBOX_TABLE_ID": "",
        "BITABLE_SETTINGS_TABLE_ID": "",
        "BITABLE_COVERAGE_TABLE_ID": "",
        "BITABLE_USERS_TABLE_ID": "",
        "BITABLE_USER_ROLE_ID": "",
        "BITABLE_GRANT_DOCUMENT_ACCESS": "false",
    }
    # Exclusive creation: never rotate an existing database's encryption key.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
        stream.write("# Private local configuration. Never commit or share this file.\n")
        stream.writelines(f"{key}={value}\n" for key, value in values.items())


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the inbox on this computer only")
    parser.add_argument("--init", action="store_true", help="create .env.local without overwriting")
    parser.add_argument(
        "--offline", action="store_true", help="local smoke test; no background jobs"
    )
    parser.add_argument(
        "--bootstrap-first-user",
        action="store_true",
        help="one-time local OAuth identity bootstrap; requires --offline",
    )
    args = parser.parse_args()
    path = Path.cwd() / ".env.local"
    if args.init:
        initialize(path)
        print(
            "Created .env.local with random secrets. Keep it private; fill in integration settings."
        )
        return
    if not path.is_file():
        parser.error("run python -m app.local --init from the project directory first")
    from dotenv import dotenv_values

    values = dotenv_values(path, interpolate=False)
    os.environ.update({key: value for key, value in values.items() if value is not None})
    os.environ["LOCAL_CONFIG_PATH"] = str(path.resolve())
    # The explicit local launcher cannot be repurposed to expose the service.
    os.environ["LOCAL_ONLY"] = "true"
    if args.offline:
        os.environ.update(APP_ENV="development", RUN_BACKGROUND_WORKERS="false")
        print("OFFLINE TEST: Feishu collection and Bitable synchronization are disabled.")
    if args.bootstrap_first_user:
        if not args.offline:
            parser.error("--bootstrap-first-user requires --offline")
        os.environ["LOCAL_BOOTSTRAP_FIRST_USER"] = "true"
        # Enrollment is a separate, explicit, offline-only mode. The persisted
        # configuration remains in strict single-user pilot mode for normal runs.
        os.environ["LOCAL_SINGLE_USER_PILOT"] = "false"
        print("FIRST-USER BOOTSTRAP: only the first local OAuth identity can be initialized.")
    settings = Settings.from_env()
    issues = settings.validate_runtime()
    if not args.offline:
        issues.extend(settings.validate_production())
    if issues:
        parser.error("invalid local configuration: " + ", ".join(issues))
    from .__main__ import main as serve

    serve()


if __name__ == "__main__":
    main()
