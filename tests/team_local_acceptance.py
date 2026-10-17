"""Network acceptance against team_local_server; only boolean sanitized evidence."""

import json
import time
from pathlib import Path

import httpx

HERE = Path("C:/TECH/BRAIN/tasks/TASK-058")
A = "00000000-0000-4000-8000-000000005802"
B = "00000000-0000-4000-8000-000000005803"
D = "00000000-0000-4000-8000-000000005804"
checks = []


def check(label, value):
    checks.append({"check": label, "passed": bool(value)})
    if not value:
        raise AssertionError(label)


def bearer(token):
    return {"Authorization": "Bearer " + token}


owner = httpx.Client(base_url="http://127.0.0.1:8857")
c = httpx.Client(base_url="http://127.0.0.1:8857")
o = owner.post("/auth/token", json={"email": "owner058@example.com", "password": "LocalFixture9"})
check("owner real login", o.status_code == 200)
oh = bearer(o.json()["access_token"])
login = c.post(
    "/auth/account/login", json={"email": "staff058@example.com", "password": "LocalFixture9"}
).json()
ph = bearer(login["account_token"])


def select(uid, proof=ph):
    return c.post("/auth/account/select", headers=proof, json={"user_id": uid})


a = select(A).json()
b = select(B).json()
check("initial A and B real API", bool(a.get("access_token")) and bool(b.get("access_token")))
r = owner.post(f"/doctor/team/{A}/revoke", headers=oh)
check("revocation", r.status_code == 200)
check(
    "old A token blocked", c.get("/auth/me", headers=bearer(a["access_token"])).status_code == 401
)
check(
    "B unaffected by A revocation",
    c.get("/auth/me", headers=bearer(b["access_token"])).status_code == 200,
)
r = c.post("/auth/password-reset/request", json={"email": "staff058@example.com"})
check("reset request accepted", r.status_code == 200)
code = None
for _ in range(20):
    code = httpx.get("http://127.0.0.1:8957/__qa/reset-code").json()["code"]
    if code:
        break
    time.sleep(0.1)
check("SMTP captured local code", bool(code))
v = c.post("/auth/password-reset/verify-code", json={"email": "staff058@example.com", "code": code})
check("reset verify-code", v.status_code == 200)
reset_token = v.json().get("reset_token") or v.json().get("token")
check(
    "reset confirm",
    c.post(
        "/auth/password-reset/confirm",
        json={"token": reset_token, "new_password": "NewLocalFixture9"},
    ).status_code
    == 200,
)
new = c.post(
    "/auth/account/login", json={"email": "staff058@example.com", "password": "NewLocalFixture9"}
)
check("new personal password login", new.status_code == 200)
ph = bearer(new.json()["account_token"])
check(
    "revocation survives personal recovery",
    next(m for m in new.json()["memberships"] if m["user_id"] == A)["status"] == "revoked",
)
check("A selection remains denied after recovery", select(A, ph).status_code == 403)
b = select(B, ph).json()
rows = owner.get("/doctor/team", headers=oh).json()
version = next(m for m in rows if m["user_id"] == A)["access_version"]
r = owner.post(f"/doctor/team/{A}/restore", headers=oh, json={"expected_version": version})
check("restore real API without management", r.status_code == 200 and not r.json()["is_manager"])
check(
    "B session still live after restore",
    c.get("/auth/me", headers=bearer(b["access_token"])).status_code == 200,
)
check(
    "old A token still invalid after restore",
    c.get("/auth/me", headers=bearer(a["access_token"])).status_code == 401,
)
fresh = select(A, ph)
check("new A login authorized", fresh.status_code == 200)
check(
    "restored doctor cannot manage team",
    c.get("/doctor/team", headers=bearer(fresh.json()["access_token"])).status_code == 403,
)
check(
    "explicit management grant",
    owner.patch(f"/doctor/team/{A}/management", headers=oh, json={"is_manager": True}).status_code
    == 200,
)
# Legacy refresh leg also remains dead after restoration.
d = owner.post(
    "/auth/token", json={"email": "desk058@example.com", "password": "LocalFixture9"}
).json()
r = owner.post(f"/doctor/team/{D}/revoke", headers=oh).json()
check(
    "secretary restored without clinical profile",
    owner.post(
        f"/doctor/team/{D}/restore", headers=oh, json={"expected_version": r["access_version"]}
    ).json()["professional_id"]
    is None,
)
c.cookies.clear()
check(
    "old legacy refresh remains invalid",
    c.post("/auth/refresh", json={"refresh_token": d["refresh_token"]}).status_code == 401,
)
HERE.joinpath("api-local-acceptance.json").write_text(
    json.dumps(checks, indent=2), encoding="utf-8"
)
print(f"{len(checks)} real local API checks passed; SMTP capture only, no external delivery")
