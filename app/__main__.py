"""Run the API server with settings sourced from the environment."""

import uvicorn

from .config import Settings


def main() -> None:
    settings = Settings.from_env()
    issues = settings.validate_runtime()
    if issues:
        raise RuntimeError("invalid settings: " + ", ".join(issues))
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        proxy_headers=not settings.local_only,
        access_log=not settings.local_only,
    )


if __name__ == "__main__":
    main()
