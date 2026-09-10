"""Local setup commands with redacted output and explicit, resumable steps."""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import re
import secrets
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
from dotenv import dotenv_values

from .config import Settings
from .feishu import FeishuAPIError, FeishuClient


def update_config(path: Path, updates: dict[str, str]) -> None:
    """Atomically update private config without emitting values or rotating keys."""
    lines = path.read_text(encoding="utf-8").splitlines()
    pending = dict(updates)
    result = []
    for line in lines:
        name = line.partition("=")[0].strip()
        if name in updates:
            if name in pending:
                result.append(f"{name}={json.dumps(pending.pop(name), ensure_ascii=False)}")
        else:
            result.append(line)
    result.extend(
        f"{name}={json.dumps(value, ensure_ascii=False)}" for name, value in pending.items()
    )
    fd, temporary = tempfile.mkstemp(prefix=".env.local.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write("\n".join(result) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def save_credentials(path: Path, app_id: str, secret: str) -> None:
    if not re.fullmatch(r"cli_[A-Za-z0-9]+", app_id):
        raise ValueError("invalid app ID format")
    if not re.fullmatch(r"[A-Za-z0-9_-]{24,128}", secret):
        raise ValueError("invalid app secret format")
    current = dotenv_values(path, interpolate=False)
    if current.get("FEISHU_APP_ID") not in {None, "", app_id}:
        raise ValueError("refusing to replace a different application's identity")
    update_config(path, {"FEISHU_APP_ID": app_id, "FEISHU_APP_SECRET": secret})


def credential_server(path: Path, port: int = 8091) -> HTTPServer:
    route = "/setup/" + secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    expected_host = f"127.0.0.1:{port}"
    origin = f"http://{expected_host}"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            # Never log URLs, headers, field values or raw request bodies.
            pass

        def reply(self, code: int, body: str) -> None:
            raw = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; form-action 'self'; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(raw)

        def allowed(self) -> bool:
            return (
                self.client_address[0] == "127.0.0.1"
                and self.headers.get("Host") == expected_host
                and secrets.compare_digest(self.path, route)
            )

        def do_GET(self) -> None:
            if not self.allowed():
                self.reply(404, "Not found")
                return
            self.reply(
                200,
                '<!doctype html><meta charset="utf-8"><title>Local setup</title>'
                "<h1>本机凭证配置</h1><p>仅保存到 .env.local，保存成功后入口自动关闭。</p>"
                f'<form method="post" action="{html.escape(route)}">'
                f'<input type="hidden" name="csrf" value="{csrf}">'
                '<label>App ID<input name="app_id" autocomplete="off" required></label><br>'
                '<label>App Secret<input type="password" name="app_secret" autocomplete="off" required></label><br>'
                '<button type="submit">保存凭证</button></form>',
            )

        def do_POST(self) -> None:
            # Some in-app browsers omit Origin. Two independent unpredictable
            # capabilities (URL and form token) protect this one-use intake.
            if not self.allowed() or self.headers.get("Origin") not in {None, "null", origin}:
                self.reply(403, "Invalid origin or setup URL")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 8192:
                    raise ValueError
                values = parse_qs(self.rfile.read(length).decode("utf-8"), strict_parsing=True)
                if not secrets.compare_digest(values.get("csrf", [""])[0], csrf):
                    self.reply(403, "Invalid one-time form token")
                    return
                save_credentials(path, values["app_id"][0].strip(), values["app_secret"][0].strip())
            except (ValueError, KeyError, UnicodeError):
                self.reply(400, "Invalid credentials; nothing was saved")
                return
            except OSError:
                self.reply(500, "Unable to save private configuration")
                return
            self.reply(
                200, "<h1>凭证已保存到本机</h1><p>入口已关闭。请运行 doctor 检查接入状态。</p>"
            )
            threading.Thread(target=self.server.shutdown, daemon=True).start()

    server = HTTPServer(("127.0.0.1", port), Handler)
    # This is an ephemeral setup capability, never an application credential.
    print(origin + route, flush=True)
    return server


async def doctor(settings: Settings, online: bool = False) -> dict[str, Any]:
    issues = sorted(set(settings.validate_runtime() + settings.validate_production()))
    report: dict[str, Any] = {
        "local_only": settings.local_only,
        "configuration_ready": not issues,
        "issues": issues,
        "credentials_valid": "not_checked",
        "message_delivery": "not_verified",
        "row_isolation": "requires_three_account_acceptance",
    }
    if online and settings.feishu_app_id and settings.feishu_app_secret:
        if settings.feishu_base_url not in {"https://open.feishu.cn", "https://open.larksuite.com"}:
            report["credentials_valid"] = "refused_untrusted_api_origin"
            return report
        client = FeishuClient(settings)
        try:
            await client.tenant_access_token()
            report["credentials_valid"] = True
        except FeishuAPIError as exc:
            report["credentials_valid"] = False
            report["api_error_code"] = exc.code
            report["http_status"] = exc.status_code
        except httpx.HTTPError:
            report["credentials_valid"] = "network_error"
        finally:
            await client.close()
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Local setup and redacted preflight")
    parser.add_argument("command", choices=["credentials", "doctor"])
    parser.add_argument("--online", action="store_true", help="validate credentials with Feishu")
    args = parser.parse_args()
    path = Path.cwd() / ".env.local"
    if not path.is_file():
        parser.error("run python -m app.local --init first")
    if args.command == "credentials":
        server = credential_server(path)
        # A forgotten setup page cannot stay open indefinitely.
        timer = threading.Timer(900, server.shutdown)
        timer.daemon = True
        timer.start()
        try:
            server.serve_forever()
        finally:
            timer.cancel()
            server.server_close()
        return
    os.environ.update(
        {k: v for k, v in dotenv_values(path, interpolate=False).items() if v is not None}
    )
    report = asyncio.run(doctor(Settings.from_env(), args.online))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
