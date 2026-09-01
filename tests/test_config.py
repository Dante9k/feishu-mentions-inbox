from __future__ import annotations

from app.config import Settings


def test_production_validation_requires_signature_and_positive_intervals() -> None:
    issues = Settings(
        app_env="production",
        feishu_require_signature=False,
        content_retention_days=0,
        worker_poll_seconds=0,
    ).validate_production()

    assert "FEISHU_REQUIRE_SIGNATURE (must be true in production)" in issues
    assert "CONTENT_RETENTION_DAYS (must be greater than zero)" in issues
    assert "WORKER_POLL_SECONDS (must be greater than zero)" in issues


def test_production_validation_rejects_ephemeral_or_unknown_database_urls() -> None:
    memory_issues = Settings(
        app_env="production", database_url="sqlite:///:memory:"
    ).validate_production()
    unknown_issues = Settings(
        app_env="production", database_url="mysql://db/app"
    ).validate_production()

    assert "DATABASE_URL (in-memory SQLite is not allowed in production)" in memory_issues
    assert "DATABASE_URL (must use sqlite:/// or postgresql://)" in unknown_issues
