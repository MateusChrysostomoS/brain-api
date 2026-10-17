"""Real HTTP and loopback SMTP: restore reissues pending first-access e-mail."""

import asyncio
import json
import os
import re
import socketserver
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx

from tests import staff_local_server as captured
from tests.team_local_server import ROOT, seed

HERE = Path("C:/TECH/BRAIN/tasks/TASK-058")
checks = []


def check(label, value):
    checks.append({"check": label, "passed": bool(value)})
    if not value:
        raise AssertionError(label)


def bearer(raw):
    return {"Authorization": "Bearer " + raw}


def start_smtp():
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 8057), captured.SMTP)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def password_from_last_mail():
    found = re.search(r"\b(A7[A-Za-z0-9]{18})\b", captured.MAILS[-1].get_content())
    check("temporary password captured only locally", bool(found))
    return found.group(1)


def main():
    asyncio.run(seed())
    sink = start_smtp()
    with (HERE / "auto-email-local-api.log").open("w", encoding="utf-8") as log:
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
        try:
            with httpx.Client(base_url="http://127.0.0.1:8857", timeout=20) as api:
                for _ in range(40):
                    try:
                        if api.get("/health").status_code == 200:
                            break
                    except httpx.ConnectError:
                        pass
                    time.sleep(0.1)
                owner = api.post(
                    "/auth/token",
                    json={"email": "owner058@example.com", "password": "LocalFixture9"},
                )
                check("actual API manager login", owner.status_code == 200)
                headers = bearer(owner.json()["access_token"])
                for delivered in (True, False):
                    address = f"pending-auto-{int(delivered)}@example.com"
                    invite = api.post(
                        "/doctor/team/invites",
                        headers=headers,
                        json={"name": "Pending fixture", "email": address, "role": "secretary"},
                    )
                    check("pending invitation real API", invite.status_code == 201)
                    uid = invite.json()["member"]["user_id"]
                    first_mail = captured.MAILS[-1]
                    old_password = password_from_last_mail()
                    setup = api.post(
                        "/auth/account/login", json={"email": address, "password": old_password}
                    )
                    check(
                        "initial setup restricted",
                        setup.status_code == 200 and setup.json()["requires_password_change"],
                    )
                    old_setup = setup.json()["access_token"]
                    revoked = api.post(f"/doctor/team/{uid}/revoke", headers=headers)
                    check("revoke before personal password", revoked.status_code == 200)
                    before = len(captured.MAILS)
                    if not delivered:
                        sink.shutdown()
                        sink.server_close()
                        sink = None
                    restored = api.post(
                        f"/doctor/team/{uid}/restore",
                        headers=headers,
                        json={"expected_version": revoked.json()["access_version"]},
                    )
                    check(
                        "restore stays pending",
                        restored.status_code == 200
                        and restored.json()["access_status"] == "pending_first_access",
                    )
                    check(
                        "actual SMTP outcome recorded",
                        restored.json()["delivery_status"] == ("sent" if delivered else "failed"),
                    )
                    check(
                        "same first-access e-mail automatically sent"
                        if delivered
                        else "failed SMTP does not claim delivery",
                        len(captured.MAILS) == before + (1 if delivered else 0),
                    )
                    if delivered:
                        check(
                            "same recipient, sender and first-access subject",
                            all(
                                str(captured.MAILS[-1][field]) == str(first_mail[field])
                                for field in ("To", "From", "Subject")
                            ),
                        )
                    retry = api.post(
                        f"/doctor/team/{uid}/restore",
                        headers=headers,
                        json={"expected_version": revoked.json()["access_version"]},
                    )
                    check(
                        "retry is idempotent without duplicate e-mail",
                        retry.json() == restored.json()
                        and len(captured.MAILS) == before + (1 if delivered else 0),
                    )
                    check(
                        "old temporary password refused",
                        api.post(
                            "/auth/account/login", json={"email": address, "password": old_password}
                        ).status_code
                        == 401,
                    )
                    check(
                        "old setup proof refused",
                        api.post(
                            "/auth/account/first-access/complete",
                            headers=bearer(old_setup),
                            json={"new_password": "PersonalNewFixture9"},
                        ).status_code
                        == 401,
                    )
                    if not delivered:
                        sink = start_smtp()
                        resend = api.post(
                            f"/doctor/team/{uid}/first-access/resend", headers=headers
                        )
                        check(
                            "explicit retry after actual SMTP failure",
                            resend.status_code == 200
                            and resend.json()["delivery_status"] == "sent",
                        )
                    password = password_from_last_mail()
                    check("new temporary password issued", password != old_password)
                    setup = api.post(
                        "/auth/account/login", json={"email": address, "password": password}
                    )
                    check(
                        "new setup still restricted",
                        setup.status_code == 200 and setup.json()["requires_password_change"],
                    )
                    sh = bearer(setup.json()["access_token"])
                    check(
                        "temporary setup cannot operate clinic",
                        api.get("/auth/me", headers=sh).status_code == 401,
                    )
                    complete = api.post(
                        "/auth/account/first-access/complete",
                        headers=sh,
                        json={"new_password": "PersonalNewFixture9"},
                    )
                    check(
                        "personal first password completion",
                        complete.status_code == 200
                        and complete.json()["session_kind"] == "account",
                    )
                    ph = bearer(complete.json()["account_token"])
                    selected = api.post("/auth/account/select", headers=ph, json={"user_id": uid})
                    check("explicit selection opens restored clinic", selected.status_code == 200)
                    before = len(captured.MAILS)
                    revoked = api.post(f"/doctor/team/{uid}/revoke", headers=headers).json()
                    restored = api.post(
                        f"/doctor/team/{uid}/restore",
                        headers=headers,
                        json={"expected_version": revoked["access_version"]},
                    )
                    check(
                        "established personal credential not replaced",
                        restored.status_code == 200
                        and len(captured.MAILS) == before
                        and api.post(
                            "/auth/account/login",
                            json={"email": address, "password": "PersonalNewFixture9"},
                        ).status_code
                        == 200,
                    )
            HERE.joinpath("auto-email-local-acceptance.json").write_text(
                json.dumps(checks, indent=2), encoding="utf-8"
            )
            print(f"{len(checks)} actual HTTP/SMTP checks passed; loopback capture only")
        finally:
            child.terminate()
            child.wait(timeout=10)
            if sink:
                sink.shutdown()
                sink.server_close()


if __name__ == "__main__":
    main()
