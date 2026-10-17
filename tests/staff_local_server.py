"""Loopback-only API/SMTP/browser fixture; no production data or integrations."""

import asyncio
import email.parser
import email.policy
import http.server
import json
import os
import re
import socketserver
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = Path("C:/TECH/BRAIN")
os.environ.update(
    {
        "DATABASE_URL": "postgresql+asyncpg://postgres@127.0.0.1:55457/task057_browser",
        "SECRET_KEY": "disposable-task057-local-signing-key",
        "APP_ENV": "dev",
        "AUTH_RATE_LIMIT_PER_MIN": "0",
        "PASSWORD_RESET_EMAIL_RATE_LIMIT_PER_MIN": "0",
        "SMTP_HOST": "127.0.0.1",
        "SMTP_PORT": "8057",
        "SMTP_USE_TLS": "false",
        "SMTP_USERNAME": "",
        "SMTP_PASSWORD": "",
        "EMAIL_FROM_ADDRESS": "brain@example.com",
        "SECRETARIA_BASE_URL": "",
        "SECRETARIA_API_KEY": "",
        "SECRETARIA_ADMIN_TOKEN": "",
        "PRECHECK_BASE_URL": "",
        "PRECHECK_INTERNAL_TOKEN": "",
        "STRIPE_SECRET_KEY": "",
        "PRECHECK_API_KEY": "",
        "META_APP_ID": "",
        "META_ES_CONFIG_ID": "",
        "STAFF_MULTI_CLINIC_ENABLED": "true",
        "STAFF_ACCOUNT_CREDENTIALS_ENABLED": "true",
        "BRAIN_MESSAGE_PUBLIC_URL": "http://127.0.0.1:8957",
    }
)
sys.path.insert(0, str(ROOT / "src"))
MAILS = []


class SMTP(socketserver.StreamRequestHandler):
    def handle(self):
        self.wfile.write(b"220 local fixture SMTP\r\n")
        while True:
            line = self.rfile.readline()
            if not line:
                return
            command = line.split(b" ", 1)[0].strip().upper()
            if command == b"EHLO":
                self.wfile.write(b"250-local fixture\r\n250 SIZE 1048576\r\n")
            elif command == b"DATA":
                self.wfile.write(b"354 End with dot\r\n")
                rows = []
                while True:
                    row = self.rfile.readline()
                    if row == b".\r\n" or not row:
                        break
                    rows.append(row[1:] if row.startswith(b"..") else row)
                MAILS.append(
                    email.parser.BytesParser(policy=email.policy.default).parsebytes(b"".join(rows))
                )
                self.wfile.write(b"250 captured locally\r\n")
            elif command == b"QUIT":
                self.wfile.write(b"221 bye\r\n")
                return
            else:
                self.wfile.write(b"250 OK\r\n")


class Proxy(http.server.SimpleHTTPRequestHandler):
    directory_root = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(self.directory_root), **kwargs)

    def log_message(self, *args):
        pass

    def handle_request(self):
        if self.path == "/__qa/reset-code":
            code = None
            for mail in reversed(MAILS):
                match = re.search(r"\b([0-9]{6})\b", mail.get_content())
                if match:
                    code = match.group(1)
                    break
            body = json.dumps({"code": code}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        prefix = (
            "/api/brain"
            if self.path.startswith("/api/brain/")
            else ""
            if self.path.startswith("/auth/")
            else None
        )
        if prefix is None:
            if self.command == "GET":
                return super().do_GET()
            self.send_error(404)
            return
        size = int(self.headers.get("Content-Length", 0))
        headers = {
            k: v
            for k, v in self.headers.items()
            if k.lower() not in ("host", "connection", "content-length")
        }
        req = urllib.request.Request(
            "http://127.0.0.1:8857" + self.path[len(prefix) :],
            data=self.rfile.read(size) if size else None,
            headers=headers,
            method=self.command,
        )
        try:
            response = urllib.request.urlopen(req, timeout=15)
        except urllib.error.HTTPError as error:
            response = error
        body = response.read()
        self.send_response(response.status)
        for k, v in response.headers.items():
            if k.lower() not in ("transfer-encoding", "connection", "content-length"):
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = handle_request
    do_POST = handle_request
    do_PUT = handle_request


async def seed():
    from datetime import UTC, datetime

    from brain_api.core.database import async_session_factory, engine
    from brain_api.core.security import hash_password
    from brain_api.models import StaffAccount, Tenant, User

    async with async_session_factory() as session:
        aid = UUID("00000000-0000-4000-8000-000000000057")
        if await session.get(StaffAccount, aid):
            return
        account = StaffAccount(
            id=aid,
            email="staff057@example.com",
            name="Equipe fictícia",
            password_hash=hash_password("LocalFixture9"),
        )
        a = Tenant(clinic_name="Clínica fictícia A")
        b = Tenant(clinic_name="Clínica fictícia B")
        session.add_all([account, a, b])
        await session.flush()
        session.add_all(
            [
                User(
                    tenant_id=a.id,
                    account_id=aid,
                    email=account.email,
                    name=account.name,
                    role="doctor",
                    is_manager=True,
                    password_hash="!account-managed",
                    access_revoked_at=datetime.now(UTC),
                ),
                User(
                    tenant_id=b.id,
                    account_id=aid,
                    email=account.email,
                    name=account.name,
                    role="doctor",
                    is_manager=False,
                    password_hash="!account-managed",
                ),
            ]
        )
        await session.commit()
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(seed())
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    servers = []
    sink = socketserver.ThreadingTCPServer(("127.0.0.1", 8057), SMTP)
    servers.append(sink)
    for repo, port in (("Brain-Message-Frontend", 8957), ("brain-frontend", 8958)):
        handler = type(f"Proxy{port}", (Proxy,), {"directory_root": WORKSPACE / repo / "out"})
        servers.append(http.server.ThreadingHTTPServer(("127.0.0.1", port), handler))
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    log = open(WORKSPACE / "tasks/TASK-057/local-api.log", "w", encoding="utf-8")
    child = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "brain_api.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            "8857",
        ],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        stdout=log,
        stderr=log,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    print("Local API/SMTP and frontend fixtures started on loopback only", flush=True)
    try:
        child.wait()
    finally:
        child.terminate()
        for server in servers:
            server.shutdown()
            server.server_close()
