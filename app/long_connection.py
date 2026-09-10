"""Official SDK receiver isolated from the API's asyncio loop.

The child's stdout/stdin are private pipes, never a log destination. An SDK
callback succeeds only after the parent commits the event to its durable queue.
The SDK owns reconnects; the parent restarts a crashed child with bounded backoff.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
from contextlib import suppress
from typing import Any

from .config import Settings
from .repository import Repository
from .service import event_key

logger = logging.getLogger(__name__)
MAX_EVENT_BYTES = 2 * 1024 * 1024
EVENT_TYPES = frozenset(
    {
        "im.message.receive_v1",
        "im.message.recalled_v1",
        "im.chat.member.bot.added_v1",
        "im.chat.member.bot.deleted_v1",
        "im.chat.disbanded_v1",
    }
)


async def persist_event(repository: Repository, raw: bytes, tenant_key: str) -> None:
    if len(raw) > MAX_EVENT_BYTES:
        raise ValueError("event is too large")
    payload = json.loads(raw)
    if not isinstance(payload, dict) or not isinstance(payload.get("header"), dict):
        raise ValueError("invalid event envelope")
    header = payload["header"]
    if not tenant_key or header.get("tenant_key") != tenant_key:
        raise ValueError("event tenant mismatch")
    key, event_type = event_key(payload)
    if event_type not in EVENT_TYPES or key.endswith(":"):
        raise ValueError("unsupported event or missing event identity")
    # Do not retain verification credentials from the SDK envelope.
    header.pop("token", None)
    await repository.enqueue_event(f"{tenant_key}:{key}", event_type, payload)


class LongConnectionReceiver:
    def __init__(self, settings: Settings, repository: Repository):
        self.settings = settings
        self.repository = repository
        self.process: asyncio.subprocess.Process | None = None
        self.task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.returncode is None

    async def start(self) -> None:
        from importlib.util import find_spec

        if find_spec("lark_oapi") is None:
            raise RuntimeError('long_connection requires pip install ".[local]"')
        self.task = asyncio.create_task(self._run(), name="feishu-long-connection")

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    async def _run(self) -> None:
        while True:
            try:
                env = os.environ.copy()
                env.update(
                    FEISHU_APP_ID=self.settings.feishu_app_id,
                    FEISHU_APP_SECRET=self.settings.feishu_app_secret,
                    FEISHU_BASE_URL=self.settings.feishu_base_url,
                    PYTHONIOENCODING="utf-8",
                )
                creationflags = (
                    int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0
                )
                self.process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "app.long_connection",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    # SDK errors can embed payloads, credentials or connection URLs.
                    stderr=asyncio.subprocess.DEVNULL,
                    limit=MAX_EVENT_BYTES + 1,
                    env=env,
                    creationflags=creationflags,
                )
                assert self.process.stdout is not None
                assert self.process.stdin is not None
                while raw := await self.process.stdout.readline():
                    reply = b"retry\n"
                    try:
                        async with asyncio.timeout(2):
                            await persist_event(
                                self.repository, raw, self.settings.feishu_tenant_key
                            )
                        reply = b"ok\n"
                    except Exception:
                        logger.warning(
                            "long-connection event not acknowledged; awaiting redelivery"
                        )
                    self.process.stdin.write(reply)
                    await self.process.stdin.drain()
                logger.warning("long-connection receiver exited; restarting in 10 seconds")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("long-connection receiver failed; restarting in 10 seconds")
            finally:
                if self.process and self.process.returncode is None:
                    with suppress(ProcessLookupError):
                        self.process.terminate()
                    try:
                        await asyncio.wait_for(self.process.wait(), timeout=5)
                    except TimeoutError:
                        self.process.kill()
                        await self.process.wait()
                self.process = None
            await asyncio.sleep(10)


def main() -> None:
    import lark_oapi as lark

    # Suppress even SDK DEBUG logs: these may contain full message bodies.
    logging.disable(logging.CRITICAL)

    def receive(event: Any) -> None:
        raw = lark.JSON.marshal(event).encode("utf-8")
        if len(raw) > MAX_EVENT_BYTES - 1:
            raise ValueError("event is too large")
        sys.stdout.buffer.write(raw + b"\n")
        sys.stdout.buffer.flush()
        reply = sys.stdin.buffer.readline()
        if not reply:
            raise SystemExit(1)
        if reply != b"ok\n":
            raise RuntimeError("event was not persisted")

    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(receive)
        .register_p2_im_message_recalled_v1(receive)
        .register_p2_im_chat_member_bot_added_v1(receive)
        .register_p2_im_chat_member_bot_deleted_v1(receive)
        .register_p2_im_chat_disbanded_v1(receive)
        .build()
    )
    client = lark.ws.Client(
        os.environ["FEISHU_APP_ID"],
        os.environ["FEISHU_APP_SECRET"],
        event_handler=handler,
        domain=os.environ.get("FEISHU_BASE_URL", "https://open.feishu.cn"),
    )
    client.start()


if __name__ == "__main__":
    main()
