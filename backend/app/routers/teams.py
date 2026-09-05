from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..deps import (
    get_session, publish_event_update, require_csrf, require_event_access, require_player,
    require_team_access, require_team_member_access,
)
from ..models import Match, Organizer, Registration, Team, TeamMember, User
from ..schemas import PlayerTeamResponse, TeamCreate, TeamMemberAssign, TeamMemberMove, TeamResponse, TeamRosterResponse, TeamUpdate
from ..services import (
    api_error, assign_team_member, create_team, delete_team, get_public_match, move_team_member,
    player_team_response, remove_team_member, team_roster_response, team_to_response, update_team,
)

router = APIRouter()


@router.get("/api/admin/events/{match_id}/teams", response_model=list[TeamRosterResponse])
def admin_list_teams(match: Match = Depends(require_event_access), session: Session = Depends(get_session)):
    teams = session.scalars(select(Team).where(Team.event_id == match.id).order_by(Team.created_at)).all()
    return [team_roster_response(session, team) for team in teams]


@router.post("/api/admin/events/{match_id}/teams", response_model=TeamResponse, status_code=201)
def admin_create_team(
    payload: TeamCreate, request: Request, match: Match = Depends(require_event_access),
    organizer: Organizer = Depends(require_csrf), session: Session = Depends(get_session),
):
    team = create_team(session, match, payload, organizer)
    publish_event_update(request, session, match.id, "TEAM_CREATED")
    return team_to_response(session, team)


@router.patch("/api/admin/teams/{team_id}", response_model=TeamResponse)
def admin_update_team(
    payload: TeamUpdate, request: Request, team: Team = Depends(require_team_access),
    organizer: Organizer = Depends(require_csrf), session: Session = Depends(get_session),
):
    team = update_team(session, team, payload, organizer)
    publish_event_update(request, session, team.event_id, "TEAM_UPDATED")
    return team_to_response(session, team)


@router.delete("/api/admin/teams/{team_id}", status_code=204)
def admin_delete_team(
    request: Request, team: Team = Depends(require_team_access),
    organizer: Organizer = Depends(require_csrf), session: Session = Depends(get_session),
):
    event_id = team.event_id
    delete_team(session, team, organizer)
    publish_event_update(request, session, event_id, "TEAM_REMOVED")


@router.post("/api/admin/teams/{team_id}/members", response_model=TeamRosterResponse, status_code=201)
def admin_assign_team_member(
    payload: TeamMemberAssign, request: Request, team: Team = Depends(require_team_access),
    organizer: Organizer = Depends(require_csrf), session: Session = Depends(get_session),
):
    registration = session.get(Registration, payload.registration_id)
    if not registration:
        raise api_error(404, "RESOURCE_NOT_FOUND", "Registration not found")
    assign_team_member(session, team, registration, organizer)
    publish_event_update(request, session, team.event_id, "TEAM_MEMBER_ASSIGNED")
    return team_roster_response(session, team)


@router.patch("/api/admin/team-members/{team_member_id}", response_model=TeamRosterResponse)
def admin_move_team_member(
    payload: TeamMemberMove, request: Request, member: TeamMember = Depends(require_team_member_access),
    organizer: Organizer = Depends(require_csrf), session: Session = Depends(get_session),
):
    new_team = session.scalar(select(Team).where(Team.id == payload.team_id, Team.event_id == member.event_id))
    if not new_team:
        raise api_error(422, "VALIDATION_ERROR", "Team does not belong to this event")
    move_team_member(session, member, new_team, organizer)
    publish_event_update(request, session, member.event_id, "TEAM_MEMBER_MOVED")
    return team_roster_response(session, new_team)


@router.delete("/api/admin/team-members/{team_member_id}", status_code=204)
def admin_remove_team_member(
    request: Request, member: TeamMember = Depends(require_team_member_access),
    organizer: Organizer = Depends(require_csrf), session: Session = Depends(get_session),
):
    event_id = member.event_id
    remove_team_member(session, member, organizer)
    publish_event_update(request, session, event_id, "TEAM_MEMBER_REMOVED")


@router.get("/api/events/{public_id}/my-team", response_model=PlayerTeamResponse)
def my_team(public_id: str, session: Session = Depends(get_session), player: User = Depends(require_player)):
    match = get_public_match(session, public_id)
    return player_team_response(session, match, player)
