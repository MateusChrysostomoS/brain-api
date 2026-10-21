"""Run TASK-062 against real local API/PostgreSQL/SMTP and a local Stripe simulator.

Only fictitious accounts. No service in this harness can contact an external host.
Run from this worktree with the repo's Python, after local migrations and frontend builds.
"""

import asyncio
import email.parser
import email.policy
import hashlib
import hmac
import http.server
import json
import os
import re
import socketserver
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT.parent
sys.path.insert(0, str(ROOT / "src"))
os.environ.update(
    {
        "DATABASE_URL": "postgresql+asyncpg://postgres@127.0.0.1:55462/task062_browser",
        "SECRET_KEY": "task062-disposable-local-signing-key",
        "APP_ENV": "dev",
        "AUTH_RATE_LIMIT_PER_MIN": "0",
        "PASSWORD_RESET_EMAIL_RATE_LIMIT_PER_MIN": "0",
        "SIGNUP_RATE_LIMIT_PER_MIN": "0",
        "SMTP_HOST": "127.0.0.1",
        "SMTP_PORT": "8062",
        "SMTP_USE_TLS": "false",
        "SMTP_USERNAME": "",
        "SMTP_PASSWORD": "",
        "EMAIL_FROM_ADDRESS": "brain@example.com",
        "SECRETARIA_BASE_URL": "http://127.0.0.1:8864",
        "SECRETARIA_API_KEY": "local-fixture",
        "SECRETARIA_ADMIN_TOKEN": "local-fixture",
        "PRECHECK_BASE_URL": "http://127.0.0.1:8865",
        "PRECHECK_API_KEY": "local-fixture",
        "PRECHECK_INTERNAL_TOKEN": "local-fixture",
        "STRIPE_SECRET_KEY": "sk_test_local_simulator_only",
        "STRIPE_WEBHOOK_SECRET": "whsec_local_task062",
        "STRIPE_PRICE_MAP": (
            '{"precheck_basic":"price_local_basic","precheck_start":"price_local_start"}'
        ),
        "META_APP_ID": "",
        "META_ES_CONFIG_ID": "",
        "STAFF_MULTI_CLINIC_ENABLED": "true",
        "STAFF_ACCOUNT_CREDENTIALS_ENABLED": "true",
        "FRONTEND_BASE_URL": "http://localhost:8963",
        "BRAIN_MESSAGE_PUBLIC_URL": "http://127.0.0.1:8962",
    }
)

MAILS = []
CHECKOUTS = {}
IDEMPOTENCY = {}
SUBSCRIPTIONS = {}
LOCK = threading.Lock()


class SMTP(socketserver.StreamRequestHandler):
    def handle(self):
        self.wfile.write(b"220 TASK-062 local SMTP\r\n")
        while line := self.rfile.readline():
            command = line.split(b" ", 1)[0].strip().upper()
            if command == b"EHLO":
                self.wfile.write(b"250-local\r\n250 SIZE 1048576\r\n")
            elif command == b"DATA":
                self.wfile.write(b"354 End with dot\r\n")
                rows = []
                while row := self.rfile.readline():
                    if row == b".\r\n":
                        break
                    rows.append(row)
                MAILS.append(
                    email.parser.BytesParser(policy=email.policy.default).parsebytes(b"".join(rows))
                )
                self.wfile.write(b"250 captured locally\r\n")
            elif command == b"QUIT":
                self.wfile.write(b"221 bye\r\n")
                return
            else:
                self.wfile.write(b"250 OK\r\n")


class QuietHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_json(self, value, status=200):
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


class Stripe(QuietHandler):
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/v1/checkout/sessions/"):
            sid = path.rsplit("/", 1)[1]
            row = CHECKOUTS[sid]
            return self.send_json({"id": sid, "status": row["status"], "url": row["url"]})
        if path.startswith("/v1/setup_intents/"):
            return self.send_json({"payment_method": "pm_local_fixture"})
        if path.startswith("/checkout/"):
            sid = path.rsplit("/", 1)[1]
            row = CHECKOUTS[sid]
            body = (
                '<html lang="pt-BR"><title>Pagamento simulado local</title>'
                "<h1>Pagamento simulado local</h1>"
                f'<p>Sem cobrança real.</p><form method="POST" action="/confirm/{sid}">'
                "<button>Confirmar pagamento simulado</button></form>"
                f'<a href="{row["data"]["cancel_url"]}">Cancelar e voltar à clínica</a></html>'
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        content = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if path == "/internal/tenants":
            return self.send_json({"ok": True}, 201)
        if path.startswith("/confirm/"):
            sid = path.rsplit("/", 1)[1]
            row = CHECKOUTS[sid]
            obj = {
                "id": sid,
                "mode": "setup",
                "customer": "cus_" + sid,
                "setup_intent": "seti_" + sid,
                "metadata": {
                    "kind": "signup_intent",
                    "signup_intent_id": row["data"]["metadata[signup_intent_id]"],
                },
            }
            event = {
                "id": "evt_" + sid,
                "type": "checkout.session.completed",
                "object": "event",
                "api_version": "2024-06-20",
                "data": {"object": obj},
            }
            raw = json.dumps(event).encode()
            timestamp = str(int(time.time()))
            sig = hmac.new(
                b"whsec_local_task062", timestamp.encode() + b"." + raw, hashlib.sha256
            ).hexdigest()
            # Real signature verification and real duplicate handling, HTTP to local API.
            for _ in range(2):
                request = urllib.request.Request(
                    "http://127.0.0.1:8862/webhooks/stripe",
                    data=raw,
                    headers={
                        "Content-Type": "application/json",
                        "Stripe-Signature": f"t={timestamp},v1={sig}",
                    },
                )
                with urllib.request.urlopen(request) as response:
                    assert response.status == 200
            row["status"] = "complete"
            self.send_response(303)
            self.send_header("Location", row["data"]["success_url"])
            self.end_headers()
            return
        data = {k: v[0] for k, v in urllib.parse.parse_qs(content.decode()).items()}
        key = self.headers.get("Idempotency-Key")
        with LOCK:
            if key and key in IDEMPOTENCY:
                return self.send_json(IDEMPOTENCY[key])
            if path == "/v1/checkout/sessions":
                sid = f"cs_local_{uuid4().hex}"
                url = f"http://localhost:8864/checkout/{sid}"
                CHECKOUTS[sid] = {"data": data, "url": url, "status": "open"}
                result = {"id": sid, "url": url}
            elif path == "/v1/subscriptions":
                sub = f"sub_local_{uuid4().hex}"
                SUBSCRIPTIONS[sub] = {
                    "customer": data["customer"],
                    "tenant_id": data["metadata[tenant_id]"],
                }
                result = {"id": sub, "status": "active"}
            else:
                return self.send_error(404)
            if key:
                IDEMPOTENCY[key] = result
            return self.send_json(result)


class Proxy(http.server.SimpleHTTPRequestHandler):
    root = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(self.root), **kwargs)

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
            return QuietHandler.send_json(self, {"code": code})
        if self.path == "/__qa/state":
            return QuietHandler.send_json(
                self,
                {
                    "checkouts": len(CHECKOUTS),
                    "subscriptions": len(SUBSCRIPTIONS),
                    "precheck_mode": "native-local-8865",
                },
            )
        prefix = (
            "/api/brain"
            if self.path.startswith("/api/brain/")
            else "/api"
            if self.path.startswith("/api/")
            else None
        )
        if prefix is None:
            if self.command == "GET":
                return super().do_GET()
            return self.send_error(404)
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        headers = {
            k: v
            for k, v in self.headers.items()
            if k.lower() not in ("host", "connection", "content-length")
        }
        request = urllib.request.Request(
            "http://127.0.0.1:8862" + self.path[len(prefix) :],
            data=body or None,
            headers=headers,
            method=self.command,
        )
        try:
            response = urllib.request.urlopen(request, timeout=30)
        except urllib.error.HTTPError as error:
            response = error
        raw = response.read()
        self.send_response(response.status)
        for key, value in response.headers.items():
            if key.lower() not in ("transfer-encoding", "content-length", "connection"):
                self.send_header(key, value)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_GET = handle_request
    do_POST = handle_request
    do_PUT = handle_request
    do_PATCH = handle_request


async def seed():
    from datetime import UTC, datetime

    from brain_api.core.database import async_session_factory, engine
    from brain_api.core.security import hash_password
    from brain_api.models import Entitlement, StaffAccount, Tenant, User

    async with async_session_factory() as db:
        account_id = UUID("00000000-0000-4000-8000-000000000062")
        if await db.get(StaffAccount, account_id):
            return
        account = StaffAccount(
            id=account_id,
            email="staff062@example.com",
            name="Pessoa fictícia 062",
            password_hash=hash_password("LocalFixture9"),
        )
        db.add(account)
        await db.flush()
        for suffix, clinic in (("a", "Clínica fictícia A"), ("b", "Clínica fictícia B")):
            tenant = Tenant(clinic_name=clinic)
            db.add(tenant)
            await db.flush()
            user = User(
                account_id=account_id,
                tenant_id=tenant.id,
                email=account.email,
                name=account.name,
                password_hash="!account-managed",
                role="doctor",
                is_manager=False,
                access_revoked_at=datetime.now(UTC),
                professional_id=UUID(f"00000000-0000-4000-8000-00000000006{suffix}"),
            )
            db.add(user)
            db.add(
                Entitlement(
                    tenant_id=tenant.id,
                    status="active",
                    plan="precheck_basic",
                    stripe_customer_id=f"cus_old_{suffix}",
                    stripe_subscription_id=f"sub_old_{suffix}",
                )
            )
        await db.commit()
    await engine.dispose()


def main():
    import httpx
    import uvicorn

    from brain_api.main import app

    # Exercise the real Stripe HTTP adapter with a simulator; refuse every external host.
    real_client = httpx.AsyncClient

    class LocalClient(real_client):
        def __init__(self, *args, **kwargs):
            base = str(kwargs.get("base_url", ""))
            if base == "https://api.stripe.com":
                kwargs["base_url"] = "http://127.0.0.1:8864"
            elif base and urllib.parse.urlparse(base).hostname not in ("127.0.0.1", "localhost"):
                raise RuntimeError("TASK-062 refuses non-local HTTP")
            super().__init__(*args, **kwargs)

        async def send(self, request, *args, **kwargs):
            if request.url.host not in ("127.0.0.1", "localhost"):
                raise RuntimeError("TASK-062 refuses non-local HTTP")
            return await super().send(request, *args, **kwargs)

    httpx.AsyncClient = LocalClient
    asyncio.run(seed())
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    servers = [
        socketserver.ThreadingTCPServer(("127.0.0.1", 8062), SMTP),
        http.server.ThreadingHTTPServer(("127.0.0.1", 8864), Stripe),
    ]
    for repo, port in (("Brain-Message-Frontend", 8962), ("brain-frontend", 8963)):
        handler = type(f"Proxy{port}", (Proxy,), {"root": TASK / repo / "out"})
        servers.append(http.server.ThreadingHTTPServer(("127.0.0.1", port), handler))
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    print("TASK-062 loopback API, frontends, SMTP and Stripe simulator ready", flush=True)
    try:
        uvicorn.run(app, host="127.0.0.1", port=8862, log_level="warning")
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    main()
