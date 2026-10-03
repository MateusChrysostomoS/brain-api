"""Platform-admin-only preview and execution of patient cleanup for ONE clinic."""

from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from brain_api.api.deps import Principal, get_current_principal, require_role
from brain_api.core.database import get_session
from brain_api.schemas.tenant_patient_cleanup import CleanupRequest, CleanupResult
from brain_api.services.tenant_patient_cleanup import cleanup_patients

router = APIRouter(
    prefix="/admin/tenants/{tenant_id}/patient-cleanup",
    tags=["admin-patient-cleanup"],
    dependencies=[Depends(require_role("admin"))],
)


@router.get(
    "/preview",
    response_model=CleanupResult,
    summary="Prévia da limpeza de pacientes de uma clínica",
    description="Contagens nos três produtos. Não apaga dados. Resolver todos os "
    "impedimentos antes de executar; agendamentos/pagamentos são preservados.",
)
async def preview(tenant_id: UUID, session: AsyncSession = Depends(get_session)) -> CleanupResult:
    return await cleanup_patients(session, tenant_id)


@router.post(
    "",
    response_model=CleanupResult,
    summary="Apagar pacientes e conversas de uma clínica (DEFINITIVO)",
    description="Exige login admin, confirm=true e nome exato da clínica. Inclui os dois "
    "bancos PreCheck e anexos vinculados. Preserva equipe, configurações, "
    "agenda, pagamentos e clínicas externas. Confira status e produtos: "
    "partial exige repetir. Eventos Google/Asaas e relatórios externos ficam.",
    responses={
        400: {"description": "Confirmation required."},
        409: {"description": "Clinic name mismatch."},
    },
)
async def execute(
    tenant_id: UUID,
    payload: CleanupRequest,
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
) -> CleanupResult:
    return await cleanup_patients(session, tenant_id, payload=payload, actor_id=principal.user_id)
