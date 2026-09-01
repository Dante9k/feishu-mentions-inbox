from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urljoin


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    return int(value) if value else default


@dataclass(frozen=True, slots=True)
class Settings:
    app_env: str = "development"
    host: str = "0.0.0.0"
    port: int = 8090
    public_base_url: str = "http://localhost:8090"
    database_url: str = "sqlite:///./data/mentions.db"
    run_background_workers: bool = True
    worker_poll_seconds: float = 1.0
    coverage_interval_seconds: int = 3600
    reconciliation_interval_seconds: int = 300
    retention_interval_seconds: int = 3600
    content_retention_days: int = 180

    admin_api_token: str = ""
    bitable_callback_token: str = ""
    oauth_state_secret: str = ""
    token_encryption_secret: str = ""

    feishu_base_url: str = "https://open.feishu.cn"
    feishu_accounts_url: str = "https://accounts.feishu.cn"
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_verification_token: str = ""
    feishu_encrypt_key: str = ""
    feishu_tenant_key: str = ""
    feishu_require_signature: bool = True
    feishu_oauth_scopes: str = "im:chat:readonly offline_access"

    bitable_app_token: str = ""
    bitable_inbox_table_id: str = ""
    bitable_settings_table_id: str = ""
    bitable_coverage_table_id: str = ""
    bitable_users_table_id: str = ""
    bitable_user_role_id: str = ""
    bitable_grant_document_access: bool = True

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            app_env=os.getenv("APP_ENV", "development"),
            host=os.getenv("APP_HOST", "0.0.0.0"),
            port=_env_int("APP_PORT", 8090),
            public_base_url=os.getenv("PUBLIC_BASE_URL", "http://localhost:8090").rstrip("/"),
            database_url=os.getenv(
                "DATABASE_URL",
                "sqlite:///./data/mentions.db",
            ),
            run_background_workers=_env_bool("RUN_BACKGROUND_WORKERS", True),
            worker_poll_seconds=float(os.getenv("WORKER_POLL_SECONDS", "1")),
            coverage_interval_seconds=_env_int("COVERAGE_INTERVAL_SECONDS", 3600),
            reconciliation_interval_seconds=_env_int("RECONCILIATION_INTERVAL_SECONDS", 300),
            retention_interval_seconds=_env_int("RETENTION_INTERVAL_SECONDS", 3600),
            content_retention_days=_env_int("CONTENT_RETENTION_DAYS", 180),
            admin_api_token=os.getenv("ADMIN_API_TOKEN", ""),
            bitable_callback_token=os.getenv("BITABLE_CALLBACK_TOKEN", ""),
            oauth_state_secret=os.getenv("OAUTH_STATE_SECRET", ""),
            token_encryption_secret=os.getenv("TOKEN_ENCRYPTION_SECRET", ""),
            feishu_base_url=os.getenv("FEISHU_BASE_URL", "https://open.feishu.cn").rstrip("/"),
            feishu_accounts_url=os.getenv(
                "FEISHU_ACCOUNTS_URL", "https://accounts.feishu.cn"
            ).rstrip("/"),
            feishu_app_id=os.getenv("FEISHU_APP_ID", ""),
            feishu_app_secret=os.getenv("FEISHU_APP_SECRET", ""),
            feishu_verification_token=os.getenv("FEISHU_VERIFICATION_TOKEN", ""),
            feishu_encrypt_key=os.getenv("FEISHU_ENCRYPT_KEY", ""),
            feishu_tenant_key=os.getenv("FEISHU_TENANT_KEY", ""),
            feishu_require_signature=_env_bool("FEISHU_REQUIRE_SIGNATURE", True),
            feishu_oauth_scopes=os.getenv("FEISHU_OAUTH_SCOPES", "im:chat:readonly offline_access"),
            bitable_app_token=os.getenv("BITABLE_APP_TOKEN", ""),
            bitable_inbox_table_id=os.getenv("BITABLE_INBOX_TABLE_ID", ""),
            bitable_settings_table_id=os.getenv("BITABLE_SETTINGS_TABLE_ID", ""),
            bitable_coverage_table_id=os.getenv("BITABLE_COVERAGE_TABLE_ID", ""),
            bitable_users_table_id=os.getenv("BITABLE_USERS_TABLE_ID", ""),
            bitable_user_role_id=os.getenv("BITABLE_USER_ROLE_ID", ""),
            bitable_grant_document_access=_env_bool("BITABLE_GRANT_DOCUMENT_ACCESS", True),
        )

    @property
    def oauth_redirect_uri(self) -> str:
        return urljoin(f"{self.public_base_url}/", "auth/feishu/callback")

    def validate_production(self) -> list[str]:
        required = {
            "ADMIN_API_TOKEN": self.admin_api_token,
            "BITABLE_CALLBACK_TOKEN": self.bitable_callback_token,
            "OAUTH_STATE_SECRET": self.oauth_state_secret,
            "TOKEN_ENCRYPTION_SECRET": self.token_encryption_secret,
            "FEISHU_APP_ID": self.feishu_app_id,
            "FEISHU_APP_SECRET": self.feishu_app_secret,
            "FEISHU_VERIFICATION_TOKEN": self.feishu_verification_token,
            "FEISHU_ENCRYPT_KEY": self.feishu_encrypt_key,
            "FEISHU_TENANT_KEY": self.feishu_tenant_key,
            "BITABLE_APP_TOKEN": self.bitable_app_token,
            "BITABLE_INBOX_TABLE_ID": self.bitable_inbox_table_id,
            "BITABLE_SETTINGS_TABLE_ID": self.bitable_settings_table_id,
            "BITABLE_COVERAGE_TABLE_ID": self.bitable_coverage_table_id,
            "BITABLE_USERS_TABLE_ID": self.bitable_users_table_id,
            "BITABLE_USER_ROLE_ID": self.bitable_user_role_id,
        }
        issues = [name for name, value in required.items() if not value]
        strong_secrets = {
            "ADMIN_API_TOKEN": self.admin_api_token,
            "BITABLE_CALLBACK_TOKEN": self.bitable_callback_token,
            "OAUTH_STATE_SECRET": self.oauth_state_secret,
            "TOKEN_ENCRYPTION_SECRET": self.token_encryption_secret,
        }
        issues.extend(
            f"{name} (must be at least 32 characters)"
            for name, value in strong_secrets.items()
            if value and len(value) < 32
        )
        if not self.feishu_require_signature:
            issues.append("FEISHU_REQUIRE_SIGNATURE (must be true in production)")
        if self.database_url == "sqlite:///:memory:":
            issues.append("DATABASE_URL (in-memory SQLite is not allowed in production)")
        if not self.database_url.startswith(("sqlite:///", "postgresql://", "postgres://")):
            issues.append("DATABASE_URL (must use sqlite:/// or postgresql://)")
        positive_values = {
            "WORKER_POLL_SECONDS": self.worker_poll_seconds,
            "COVERAGE_INTERVAL_SECONDS": self.coverage_interval_seconds,
            "RECONCILIATION_INTERVAL_SECONDS": self.reconciliation_interval_seconds,
            "RETENTION_INTERVAL_SECONDS": self.retention_interval_seconds,
            "CONTENT_RETENTION_DAYS": self.content_retention_days,
        }
        issues.extend(
            f"{name} (must be greater than zero)"
            for name, value in positive_values.items()
            if value <= 0
        )
        return issues
