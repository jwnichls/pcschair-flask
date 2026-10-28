"""Venue mutations shared by the HTTP API and the websocket handlers.

Every mutation commits and then pushes the venue's full state to all
clients watching it, which replaces the polling the Rails app did.
"""

import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from .extensions import db, socketio
from .models import Paper


class ServiceError(Exception):
    status = 400


class InvalidParams(ServiceError):
    status = 422


class PaperNotFound(ServiceError):
    status = 404


def now_ms():
    return int(time.time() * 1000)


def room_for(venue):
    return f"venue:{venue.id}"


def venue_state(venue):
    return {"venue": venue.to_json(), "papers": [p.to_json() for p in venue.papers]}


def broadcast(venue):
    socketio.emit("state", venue_state(venue), to=room_for(venue))


# --- Parameter casting (mirrors ActiveRecord's type casting of form/JSON input) ---

_FALSE_STRINGS = {"0", "f", "false", "off", "no"}


def _to_bool(value):
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    value = str(value).strip().lower()
    if value == "":
        return None
    return value not in _FALSE_STRINGS


def _to_int(value):
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("expected an integer")
    if isinstance(value, (int, float)):
        return int(value)
    value = str(value).strip()
    return None if value == "" else int(value)


def _to_str(value):
    return None if value is None else str(value)


def _to_datetime(value):
    """Accepts ISO 8601 or RFC 2822 (what Date.toUTCString() produces); returns naive UTC."""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("expected a date string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        parsed = parsedate_to_datetime(value)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


VENUE_FIELDS = {
    "name": _to_str,
    "active_paper": _to_bool,
    "timer": _to_datetime,
    "breaktime": _to_bool,
    "paper_title": _to_str,
    "paper_authors": _to_str,
    "paper_pcs_id": _to_int,
    "pcs2_flag": _to_bool,
    "pcs2_venue_name": _to_str,
    "allow_adds": _to_bool,
    "sub_committee": _to_bool,
    "sub_committee_id": _to_int,
}


def cast_venue_params(raw):
    """Keep only permitted venue fields, cast to column types. Unknown keys are dropped."""
    if not isinstance(raw, dict):
        raise InvalidParams("venue parameters must be an object")
    attrs = {}
    for field, cast in VENUE_FIELDS.items():
        if field in raw:
            try:
                attrs[field] = cast(raw[field])
            except (TypeError, ValueError):
                raise InvalidParams(f"invalid value for {field}: {raw[field]!r}") from None
    return attrs


def cast_pcs_id(value):
    try:
        pcs_id = _to_int(value)
    except (TypeError, ValueError):
        pcs_id = None
    if pcs_id is None:
        raise InvalidParams(f"invalid paper id: {value!r}")
    return pcs_id


# --- Mutations ---

def update_venue(venue, raw):
    for field, value in cast_venue_params(raw).items():
        setattr(venue, field, value)

    # A paper that just became active comes off the upcoming queue.
    if venue.paper_pcs_id is not None and not venue.breaktime:
        queued = venue.queued_paper(venue.paper_pcs_id)
        if queued is not None:
            venue.papers.remove(queued)

    db.session.commit()
    broadcast(venue)


def add_paper(venue, raw_pcs_id):
    paper = Paper(pcs_paper_id=cast_pcs_id(raw_pcs_id), listorder=Paper.next_listorder(venue.id))
    venue.papers.append(paper)
    db.session.commit()
    broadcast(venue)


def remove_paper(venue, raw_pcs_id):
    paper = _find_paper(venue, raw_pcs_id)
    venue.papers.remove(paper)
    db.session.commit()
    broadcast(venue)


def move_paper(venue, raw_pcs_id, up):
    paper = _find_paper(venue, raw_pcs_id)
    if up:
        paper.move_up()
    else:
        paper.move_down()
    db.session.commit()
    # The relationship was loaded in the old order; reload it before broadcasting.
    db.session.expire(venue, ["papers"])
    broadcast(venue)


def _find_paper(venue, raw_pcs_id):
    paper = venue.queued_paper(cast_pcs_id(raw_pcs_id))
    if paper is None:
        raise PaperNotFound(f"paper {raw_pcs_id} is not in the queue")
    return paper
