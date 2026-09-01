"""Run the API server with settings sourced from the environment."""

import uvicorn

from .config import Settings


def main() -> None:
    settings = Settings.from_env()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        proxy_headers=True,
    )


if __name__ == "__main__":
    main()
