from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from ..deps import get_session, publish_event_update, require_csrf, require_event_access, require_fixture_access, require_player
from ..models import Fixture, Match, MatchResult, Organizer, User
from ..schemas import (
    FixtureCreate, FixtureResponse, FixtureUpdate, MatchParticipantEntry, MatchParticipantResponse,
    MatchResultCreate, MatchResultResponse, MatchResultUpdate, PlayerFixtureResponse,
)
from ..services import (
    api_error, create_fixture, create_match_result, fixture_to_response, get_public_match, match_participants_response,
    match_result_to_response, player_fixtures_response, set_match_participants, update_fixture, update_match_result,
)

router = APIRouter()


@router.get("/api/admin/events/{match_id}/fixtures", response_model=list[FixtureResponse])
def admin_list_fixtures(match: Match = Depends(require_event_access), session: Session = Depends(get_session)):
    fixtures = session.scalars(
        select(Fixture).options(joinedload(Fixture.team_a), joinedload(Fixture.team_b))
        .where(Fixture.event_id == match.id).order_by(Fixture.sequence.is_(None), Fixture.sequence, Fixture.scheduled_at)
    ).unique().all()
    return [fixture_to_response(f) for f in fixtures]


@router.post("/api/admin/events/{match_id}/fixtures", response_model=FixtureResponse, status_code=201)
def admin_create_fixture(
    payload: FixtureCreate, request: Request, match: Match = Depends(require_event_access),
    organizer: Organizer = Depends(require_csrf), session: Session = Depends(get_session),
):
    fixture = create_fixture(session, match, payload, organizer)
    publish_event_update(request, session, match.id, "FIXTURE_CREATED")
    return fixture_to_response(fixture)


@router.patch("/api/admin/fixtures/{fixture_id}", response_model=FixtureResponse)
def admin_update_fixture(
    payload: FixtureUpdate, request: Request, fixture: Fixture = Depends(require_fixture_access),
    organizer: Organizer = Depends(require_csrf), session: Session = Depends(get_session),
):
    event_id = fixture.event_id
    fixture = update_fixture(session, fixture, payload, organizer)
    kind = "FIXTURE_STATUS_CHANGED" if payload.status is not None else "FIXTURE_UPDATED"
    publish_event_update(request, session, event_id, kind)
    return fixture_to_response(fixture)


@router.get("/api/events/{public_id}/fixtures", response_model=list[PlayerFixtureResponse])
def public_fixtures(public_id: str, session: Session = Depends(get_session), player: User = Depends(require_player)):
    match = get_public_match(session, public_id)
    return player_fixtures_response(session, match, player)


def _get_match_result_or_404(session: Session, fixture_id: int) -> MatchResult:
    result = session.scalar(select(MatchResult).where(MatchResult.fixture_id == fixture_id))
    if not result:
        raise api_error(404, "RESOURCE_NOT_FOUND", "No result has been recorded for this match yet")
    return result


@router.get("/api/admin/fixtures/{fixture_id}/result", response_model=MatchResultResponse)
def admin_get_match_result(fixture: Fixture = Depends(require_fixture_access), session: Session = Depends(get_session)):
    result = _get_match_result_or_404(session, fixture.id)
    return match_result_to_response(session, result)


@router.post("/api/admin/fixtures/{fixture_id}/result", response_model=MatchResultResponse, status_code=201)
def admin_create_match_result(
    payload: MatchResultCreate, request: Request, fixture: Fixture = Depends(require_fixture_access),
    organizer: Organizer = Depends(require_csrf), session: Session = Depends(get_session),
):
    result = create_match_result(session, fixture, payload, organizer)
    publish_event_update(request, session, fixture.event_id, "MATCH_RESULT_CREATED")
    return match_result_to_response(session, result)


@router.patch("/api/admin/fixtures/{fixture_id}/result", response_model=MatchResultResponse)
def admin_update_match_result(
    payload: MatchResultUpdate, request: Request, fixture: Fixture = Depends(require_fixture_access),
    organizer: Organizer = Depends(require_csrf), session: Session = Depends(get_session),
):
    result = _get_match_result_or_404(session, fixture.id)
    result = update_match_result(session, result, fixture, payload, organizer)
    publish_event_update(request, session, fixture.event_id, "MATCH_RESULT_UPDATED")
    return match_result_to_response(session, result)


@router.get("/api/admin/fixtures/{fixture_id}/participants", response_model=list[MatchParticipantResponse])
def admin_list_match_participants(fixture: Fixture = Depends(require_fixture_access), session: Session = Depends(get_session)):
    return match_participants_response(session, fixture)


@router.put("/api/admin/fixtures/{fixture_id}/participants", response_model=list[MatchParticipantResponse])
def admin_set_match_participants(
    entries: list[MatchParticipantEntry], request: Request, fixture: Fixture = Depends(require_fixture_access),
    organizer: Organizer = Depends(require_csrf), session: Session = Depends(get_session),
):
    response = set_match_participants(session, fixture, entries, organizer)
    publish_event_update(request, session, fixture.event_id, "MATCH_PARTICIPATION_UPDATED")
    return response
