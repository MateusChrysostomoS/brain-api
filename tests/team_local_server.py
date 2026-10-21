"""Loopback team fixture using the actual API and a disposable PostgreSQL database."""

import asyncio
import http.server
import os
import socketserver
import subprocess
import sys
import threading
from pathlib import Path
from uuid import UUID

from tests.staff_local_server import SMTP, Proxy

os.environ.update(
    {
        "DATABASE_URL": "postgresql+asyncpg://postgres@127.0.0.1:55458/task058_team",
        "SECRET_KEY": "disposable-task058-key",
        "BRAIN_MESSAGE_PUBLIC_URL": "http://127.0.0.1:8957",
    }
)
if os.environ.get("TEAM_CLINICAL_UPSTREAM"):
    assert os.environ["TEAM_CLINICAL_UPSTREAM"] == "http://127.0.0.1:8860"
    os.environ.update(
        SECRETARIA_BASE_URL="http://127.0.0.1:8860",
        SECRETARIA_API_KEY="task058-local-only",
    )
ROOT = Path(__file__).resolve().parents[1]
HERE = Path("C:/TECH/BRAIN/tasks/TASK-058")


def uid(n):
    return UUID(f"00000000-0000-4000-8000-{n:012d}")


async def seed():
    from brain_api.core.database import async_session_factory, engine
    from brain_api.core.security import hash_password
    from brain_api.models import Entitlement, StaffAccount, Tenant, User

    async with async_session_factory() as db:
        if await db.get(User, uid(5801)):
            return
        a, b = (
            Tenant(id=uid(5810), clinic_name="Clinica ficticia A"),
            Tenant(id=uid(5820), clinic_name="Clinica ficticia B"),
        )
        db.add_all([a, b])
        await db.flush()
        account = StaffAccount(
            id=uid(5802),
            email="staff058@example.com",
            name="Pessoa ficticia",
            password_hash=hash_password("LocalFixture9"),
        )
        db.add(account)
        await db.flush()
        db.add_all(
            [
                User(
                    id=uid(5801),
                    tenant_id=a.id,
                    name="Titular ficticio",
                    email="owner058@example.com",
                    password_hash=hash_password("LocalFixture9"),
                    role="doctor",
                    is_owner=True,
                    is_manager=True,
                ),
                User(
                    id=uid(5802),
                    account_id=account.id,
                    tenant_id=a.id,
                    name="Medica ficticia",
                    email=account.email,
                    password_hash="!account-managed",
                    role="doctor",
                    is_manager=True,
                    professional_id=uid(5830),
                ),
                User(
                    id=uid(5803),
                    account_id=account.id,
                    tenant_id=b.id,
                    name="Medica ficticia B",
                    email=account.email,
                    password_hash="!account-managed",
                    role="doctor",
                    is_manager=True,
                    professional_id=uid(5840),
                ),
                User(
                    id=uid(5804),
                    tenant_id=a.id,
                    name="Recepcao ficticia",
                    email="desk058@example.com",
                    password_hash=hash_password("LocalFixture9"),
                    role="secretary",
                ),
            ]
        )
        for t in (a, b):
            db.add(
                Entitlement(
                    tenant_id=t.id,
                    status="active",
                    plan="complete_clinic_combo",
                    secretaria_enabled=True,
                    precheck_enabled=True,
                    addons={},
                    limits={},
                )
            )
        await db.commit()
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(seed())
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    sink = socketserver.ThreadingTCPServer(("127.0.0.1", 8057), SMTP)
    handler = type(
        "TeamProxy",
        (Proxy,),
        {"directory_root": HERE / "browser", "do_PATCH": Proxy.handle_request},
    )
    proxy = http.server.ThreadingHTTPServer(("127.0.0.1", 8957), handler)
    for server in (sink, proxy):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    log = open(HERE / "local-api.log", "w", encoding="utf-8")
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
    print("Team local API/SMTP/browser fixture started", flush=True)
    try:
        child.wait()
    finally:
        child.terminate()
        for server in (sink, proxy):
            server.shutdown()
            server.server_close()
