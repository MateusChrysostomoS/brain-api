"""Managers administer only the clinic resolved from their live session."""

from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain_api.api.deps import Principal, require_team_manager
from brain_api.core.database import get_session
from brain_api.models import User
from brain_api.schemas.team import (
    TeamInviteInput,
    TeamInviteResult,
    TeamManagementInput,
    TeamMember,
)
from brain_api.services import team

router = APIRouter(prefix="/doctor/team", tags=["team"])


@router.get("", response_model=list[TeamMember])
async def list_team(
    principal: Principal = Depends(require_team_manager),
    session: AsyncSession = Depends(get_session),
):
    rows = await session.scalars(
        select(User).where(User.tenant_id == principal.tenant_id).order_by(User.created_at, User.id)
    )
    return [team.member_out(user) for user in rows]


@router.post("/invites", response_model=TeamInviteResult, status_code=201)
async def invite(
    payload: TeamInviteInput,
    principal: Principal = Depends(require_team_manager),
    session: AsyncSession = Depends(get_session),
):
    return await team.invite_team_member(session, principal, payload)


@router.patch("/{user_id}/management", response_model=TeamMember)
async def management(
    user_id: UUID,
    payload: TeamManagementInput,
    principal: Principal = Depends(require_team_manager),
    session: AsyncSession = Depends(get_session),
):
    return await team.set_member_management(session, principal, user_id, payload.is_manager)


@router.post("/{user_id}/first-access/resend", response_model=TeamInviteResult)
async def resend(
    user_id: UUID,
    principal: Principal = Depends(require_team_manager),
    session: AsyncSession = Depends(get_session),
):
    return await team.resend_member_first_access(session, principal, user_id)


@router.post("/{user_id}/revoke", response_model=TeamMember)
async def revoke(
    user_id: UUID,
    principal: Principal = Depends(require_team_manager),
    session: AsyncSession = Depends(get_session),
):
    return await team.revoke_member_access(session, principal, user_id)
