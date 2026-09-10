"""Time-limited connection setup probe; never acknowledges business events."""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any

from dotenv import dotenv_values


class SafeConnectionLog(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        # The SDK log contains credential-bearing WebSocket URLs. Do not format,
        # forward or persist the original message, arguments, or exception.
        if "connected to " in str(record.msg):
            print('{"websocket_connected": true, "collection_enabled": false}', flush=True)
        elif record.levelno >= logging.ERROR:
            print('{"connection_error": true}', flush=True)


def main() -> None:
    import lark_oapi as lark
    from lark_oapi.core.log import logger

    config = dotenv_values(Path.cwd() / ".env.local", interpolate=False)
    if not config.get("FEISHU_APP_ID") or not config.get("FEISHU_APP_SECRET"):
        raise SystemExit("Configure credentials first")

    def reject_event(event: Any) -> None:
        raise RuntimeError("setup probe cannot acknowledge business events")

    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(reject_event)
        .register_p2_im_message_recalled_v1(reject_event)
        .register_p2_im_chat_member_bot_added_v1(reject_event)
        .register_p2_im_chat_member_bot_deleted_v1(reject_event)
        .register_p2_im_chat_disbanded_v1(reject_event)
        .build()
    )
    client = lark.ws.Client(
        config["FEISHU_APP_ID"], config["FEISHU_APP_SECRET"], event_handler=handler
    )
    logger.handlers.clear()
    logger.addHandler(SafeConnectionLog())
    logger.propagate = False
    timer = threading.Timer(600, lambda: os._exit(0))
    timer.daemon = True
    timer.start()
    print('{"setup_probe": true, "expires_in_seconds": 600}', flush=True)
    try:
        client.start()
    except Exception:
        print('{"connection_error": true}', flush=True)
    finally:
        timer.cancel()


if __name__ == "__main__":
    main()
