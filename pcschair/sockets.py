"""Websocket events. Clients join a venue's room and receive a "state" event on every change."""

import functools

from flask_socketio import join_room

from . import services
from .extensions import socketio
from .models import find_venue


@socketio.on("join")
def on_join(data=None):
    venue = find_venue((data or {}).get("venue_id"))
    if venue is None:
        return {"error": "Venue not found"}
    join_room(services.room_for(venue))
    return services.venue_state(venue)


@socketio.on("time")
def on_time():
    """Server clock in epoch ms, used by clients for NTP-style clock sync."""
    return services.now_ms()


def venue_action(handler):
    @functools.wraps(handler)
    def wrapper(data=None):
        data = data or {}
        venue = find_venue(data.get("venue_id"))
        if venue is None:
            return {"error": "Venue not found"}
        try:
            handler(venue, data)
        except services.ServiceError as e:
            return {"error": str(e)}
        return {"ok": True}

    return wrapper


@socketio.on("update_venue")
@venue_action
def on_update_venue(venue, data):
    services.update_venue(venue, data.get("venue", {}))


@socketio.on("add_paper")
@venue_action
def on_add_paper(venue, data):
    services.add_paper(venue, data.get("pcs_id"))


@socketio.on("remove_paper")
@venue_action
def on_remove_paper(venue, data):
    services.remove_paper(venue, data.get("pcs_id"))


@socketio.on("move_paper")
@venue_action
def on_move_paper(venue, data):
    services.move_paper(venue, data.get("pcs_id"), up=data.get("dir") == "up")
