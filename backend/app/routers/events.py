from __future__ import annotations

import json
import queue

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..deps import get_session
from ..models import Match
from ..schemas import EventSummary, MatchResponse
from ..services import event_summary, get_public_match, match_to_response

router = APIRouter()


@router.get("/api/events", response_model=list[MatchResponse])
def public_events(session: Session = Depends(get_session)):
    return [match_to_response(session, item) for item in session.scalars(select(Match).where(Match.status.in_(("OPEN", "FULL", "ONGOING"))).order_by(Match.date, Match.start_time)).all()]


@router.get("/api/events/{public_id}", response_model=MatchResponse)
@router.get("/api/matches/{public_id}", response_model=MatchResponse)
def public_event(public_id: str, session: Session = Depends(get_session)): return match_to_response(session, get_public_match(session, public_id))


@router.get("/api/events/{public_id}/summary", response_model=EventSummary)
def public_summary(public_id: str, session: Session = Depends(get_session)): return event_summary(session, get_public_match(session, public_id))


@router.get("/api/events/{public_id}/stream")
def event_stream(public_id: str, request: Request, session: Session = Depends(get_session)):
    initial_summary = event_summary(session, get_public_match(session, public_id)); channel = request.app.state.broadcaster.subscribe(public_id)
    def stream():
        try:
            yield f"event: summary\ndata: {json.dumps(initial_summary, default=str)}\n\n"
            while True:
                try: item = channel.get(timeout=15); yield f"event: {item['type']}\ndata: {json.dumps(item['summary'], default=str)}\n\n"
                except queue.Empty: yield ": keepalive\n\n"
        finally: request.app.state.broadcaster.unsubscribe(public_id, channel)
    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
