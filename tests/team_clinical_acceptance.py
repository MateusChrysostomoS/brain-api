"""Real local brain API transitions preserve the actual SecretarIA database rows."""

import asyncio
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

os.environ.update(
    DATABASE_URL="postgresql+asyncpg://postgres@127.0.0.1:55458/task058_secretaria", APP_ENV="dev"
)
sys.path.insert(0, "C:/TECH/BRAIN/secretarIA/src")
from secretaria.core.database import Base
from secretaria.models import (
    Appointment,
    AppointmentStatus,
    Conversation,
    Message,
    MessageDirection,
    MessageSender,
    Patient,
    Professional,
    Tenant,
)

HERE = Path("C:/TECH/BRAIN/tasks/TASK-058")
MODELS = (Tenant, Patient, Professional, Conversation, Message, Appointment)
checks = []


def uid(n):
    return UUID(f"00000000-0000-4000-8000-{n:012d}")


def check(label, value):
    checks.append({"check": label, "passed": bool(value)})
    if not value:
        raise AssertionError(label)


def bearer(raw):
    return {"Authorization": "Bearer " + raw}


async def main():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as db:
        await db.run_sync(Base.metadata.create_all)
    async with factory() as db:
        for i, (tid, pid) in enumerate(((5810, 5830), (5820, 5840))):
            if await db.get(Tenant, uid(tid)):
                continue
            tenant = Tenant(
                id=uid(tid),
                clinic_name=f"Synthetic clinic {i}",
                is_active=True,
                google_calendar_mode="per_professional",
                google_calendar_id=f"fixture-clinic-calendar-{i}",
                business_hours={"mon": [{"start": "08:00", "end": "18:00"}]},
            )
            db.add(tenant)
            await db.flush()
            prof = Professional(
                id=uid(pid),
                tenant_id=tenant.id,
                name=f"Synthetic doctor {i}",
                is_active=True,
                google_calendar_id=f"fixture-existing-subcalendar-{i}",
                business_hours={"tue": [{"start": "09:00", "end": "12:00"}]},
                appointment_types=[{"name": "Synthetic consultation", "duration_minutes": 30}],
            )
            patient = Patient(
                id=uid(5900 + i),
                tenant_id=tenant.id,
                channel="brain_message",
                external_id=f"synthetic-patient-{i}",
                name=f"Synthetic person {i}",
            )
            db.add_all([prof, patient])
            await db.flush()
            conversation = Conversation(
                id=uid(5910 + i), tenant_id=tenant.id, patient_id=patient.id
            )
            db.add(conversation)
            await db.flush()
            db.add(
                Message(
                    id=uid(5920 + i),
                    conversation_id=conversation.id,
                    direction=MessageDirection.INBOUND,
                    sender=MessageSender.PATIENT,
                    body="Synthetic historical message, no clinical data",
                )
            )
            for j, state in enumerate((AppointmentStatus.CONFIRMED, AppointmentStatus.ATTENDED)):
                start = datetime(2026, 10, 14, 12, tzinfo=UTC) + timedelta(days=j)
                db.add(
                    Appointment(
                        id=uid(5930 + i * 10 + j),
                        tenant_id=tenant.id,
                        professional_id=prof.id,
                        patient_id=patient.id,
                        conversation_id=conversation.id,
                        google_event_id=f"fixture-event-{i}-{j}",
                        google_calendar_source="professional",
                        appointment_type="Synthetic consultation",
                        start_at=start,
                        end_at=start + timedelta(minutes=30),
                        status=state,
                    )
                )
        await db.commit()

    async def snapshot():
        async with factory() as db:
            result = {}
            for model in MODELS:
                rows = (
                    (await db.execute(select(model.__table__).order_by(model.id))).mappings().all()
                )
                result[model.__tablename__] = json.dumps(
                    [dict(r) for r in rows], sort_keys=True, default=str
                )
            return result

    baseline = await snapshot()
    check(
        "nonempty clinical fixture",
        {name: len(json.loads(value)) for name, value in baseline.items()}
        == {
            "tenants": 2,
            "patients": 2,
            "professionals": 2,
            "conversations": 2,
            "messages": 2,
            "appointments": 4,
        },
    )
    professionals = json.loads(baseline["professionals"])
    check(
        "existing active professionals and subcalendars",
        all(
            p["is_active"]
            and p["business_hours"]
            and p["google_calendar_id"].startswith("fixture-existing-subcalendar-")
            for p in professionals
        ),
    )
    async with (
        httpx.AsyncClient(base_url="http://127.0.0.1:8857") as api,
        httpx.AsyncClient(base_url="http://127.0.0.1:8860") as clinical,
    ):
        clinical_headers = {"X-Internal-Api-Key": "task058-local-only"}
        for tid, pid in ((5810, 5830), (5820, 5840)):
            roster = await clinical.get(
                f"/internal/tenants/{uid(tid)}/professionals", headers=clinical_headers
            )
            check(
                f"actual SecretarIA roster {tid}",
                roster.status_code == 200 and any(p["id"] == str(uid(pid)) for p in roster.json()),
            )
        owner = await api.post(
            "/auth/token", json={"email": "owner058@example.com", "password": "LocalFixture9"}
        )
        check("real owner authentication", owner.status_code == 200)
        headers = bearer(owner.json()["access_token"])
        target = uid(5802)

        async def unchanged(label):
            check(label, baseline == await snapshot())
            check(
                label + " no clinical mutation request",
                (await clinical.get("/__qa/counts")).json()["mutations"] == 0,
            )

        await unchanged("initial linked clinical state")
        for enabled in (False, True):
            result = await api.patch(
                f"/doctor/team/{target}/management", headers=headers, json={"is_manager": enabled}
            )
            check(f"management {enabled}", result.status_code == 200)
            await unchanged(f"after management {enabled}")
        revoked = await api.post(f"/doctor/team/{target}/revoke", headers=headers)
        check("revoke real API", revoked.status_code == 200)
        await unchanged("while A revoked")
        restored = await api.post(
            f"/doctor/team/{target}/restore",
            headers=headers,
            json={"expected_version": revoked.json()["access_version"]},
        )
        check(
            "restore real API without management",
            restored.status_code == 200 and not restored.json()["is_manager"],
        )
        check("same professional link", restored.json()["professional_id"] == str(uid(5830)))
        await unchanged("after restore A")
        retry = await api.post(
            f"/doctor/team/{target}/restore",
            headers=headers,
            json={"expected_version": revoked.json()["access_version"]},
        )
        check("idempotent restoration", retry.json() == restored.json())
        await unchanged("after retry A")
        login = await api.post(
            "/auth/account/login",
            json={"email": "staff058@example.com", "password": "LocalFixture9"},
        )
        check("personal password preserved", login.status_code == 200)
        proof = bearer(login.json()["account_token"])
        for u in (5802, 5803):
            selected = await api.post(
                "/auth/account/select", headers=proof, json={"user_id": str(uid(u))}
            )
            check(f"new clinical login {u}", selected.status_code == 200)
        await unchanged("after A and B selection")
    HERE.joinpath("clinical-local-acceptance.json").write_text(
        json.dumps(
            {
                "secretaria_head": "59f260a067388b6aad2080e4cacd4c41b90dc3a0",
                "models": "actual ORM, schema created from current metadata; no migration claim",
                "checks": checks,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    await engine.dispose()
    print(
        f"{len(checks)} local clinical persistence checks passed; no external Google/WhatsApp/SMTP"
    )


if __name__ == "__main__":
    asyncio.run(main())
