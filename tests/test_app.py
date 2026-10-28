import pytest

from pcschair import create_app, socketio
from pcschair.extensions import db
from pcschair.models import Venue


@pytest.fixture
def app():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
    yield app
    with app.app_context():
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def venue_id(app):
    with app.app_context():
        venue = Venue(name="CHI PC", allow_adds=True, pcs2_flag=True, pcs2_venue_name="chi27a")
        db.session.add(venue)
        db.session.commit()
        return str(venue.id)


@pytest.fixture
def watcher(app, client, venue_id):
    """A socket client that has joined the venue's room."""
    sock = socketio.test_client(app, flask_test_client=client)
    sock.emit("join", {"venue_id": venue_id}, callback=True)
    sock.get_received()
    return sock


def queue(client, venue_id):
    return [p["pcs_paper_id"] for p in client.get(f"/venues/{venue_id}/papers").get_json()]


def last_state(sock):
    states = [m["args"][0] for m in sock.get_received() if m["name"] == "state"]
    assert states, "expected a state broadcast"
    return states[-1]


def test_pages_render(client, venue_id):
    assert client.get("/").status_code == 200
    assert client.get("/venues/new").status_code == 200
    assert client.get(f"/venues/{venue_id}").status_code == 200
    assert b'id="setExtensionIdButton"' in client.get(f"/venues/{venue_id}/admin").data
    assert client.get(f"/venues/{venue_id}/edit").status_code == 200
    assert client.get("/venues/not-a-uuid").status_code == 404
    assert client.get("/ntp").data.isdigit()


def test_create_and_edit_venue(client):
    resp = client.post("/venues/new", data={"name": "UIST", "allow_adds": "on", "sub_committee_id": "3"})
    assert resp.status_code == 302
    venue_id = resp.headers["Location"].rsplit("/", 1)[1]
    data = client.get(f"/venues/{venue_id}.json").get_json()
    assert data["name"] == "UIST"
    assert data["allow_adds"] is True
    assert data["pcs2_flag"] is False
    assert data["sub_committee_id"] == 3

    client.post(f"/venues/{venue_id}/edit", data={"name": "UIST 2027", "pcs2_flag": "on"})
    data = client.get(f"/venues/{venue_id}.json").get_json()
    assert data["name"] == "UIST 2027"
    assert data["pcs2_flag"] is True
    assert data["allow_adds"] is False


def test_venue_json_matches_rails_shape(client, venue_id):
    data = client.get(f"/venues/{venue_id}.json").get_json()
    assert set(data) == {
        "id", "name", "active_paper", "timer", "breaktime", "paper_title", "paper_authors",
        "paper_pcs_id", "created_at", "updated_at", "pcs2_flag", "pcs2_venue_name",
        "allow_adds", "sub_committee", "sub_committee_id",
    }
    assert data["created_at"].endswith("Z")


def test_extension_update_broadcasts_and_dequeues_active_paper(client, venue_id, watcher):
    client.post(f"/venues/{venue_id}/addpaper", json={"pcs_id": 101})
    client.post(f"/venues/{venue_id}/addpaper", json={"pcs_id": 102})
    watcher.get_received()

    # Exactly what the extension's background.js sends.
    resp = client.put(f"/venues/{venue_id}.json", json={"venue": {
        "paper_title": "A Paper",
        "paper_authors": "Ada - Somewhere\nGrace - Elsewhere",
        "paper_pcs_id": "101",
        "active_paper": True,
        "timer": "Wed, 28 Oct 2026 00:05:00 GMT",
    }})
    assert resp.status_code == 204
    assert resp.headers["Access-Control-Allow-Origin"] == "*"

    state = last_state(watcher)
    assert state["venue"]["active_paper"] is True
    assert state["venue"]["paper_pcs_id"] == 101
    assert state["venue"]["timer"] == "2026-10-28T00:05:00.000Z"
    assert [p["pcs_paper_id"] for p in state["papers"]] == [102]


def test_form_encoded_update_like_rails_jquery(client, venue_id):
    client.put(f"/venues/{venue_id}.json", data={"venue[timer]": "2026-10-28T00:05:00.000Z"})
    client.put(f"/venues/{venue_id}.json", data={"venue[timer]": "", "venue[breaktime]": "true"})
    data = client.get(f"/venues/{venue_id}.json").get_json()
    assert data["timer"] is None
    assert data["breaktime"] is True


def test_invalid_update_rejected(client, venue_id):
    resp = client.put(f"/venues/{venue_id}.json", json={"venue": {"timer": "not a date"}})
    assert resp.status_code == 422


def test_cors_preflight(client, venue_id):
    resp = client.options(f"/venues/{venue_id}.json")
    assert resp.status_code == 200
    assert "PUT" in resp.headers["Access-Control-Allow-Methods"]


def test_rest_queue_operations(client, venue_id):
    for pcs_id in (1, 2, 3, 4):
        client.post(f"/venues/{venue_id}/addpaper", json={"pcs_id": pcs_id})
    assert queue(client, venue_id) == [1, 2, 3, 4]

    client.put(f"/venues/{venue_id}/movepaper", data={"pcs_id": "3", "dir": "true"})
    assert queue(client, venue_id) == [1, 3, 2, 4]
    client.put(f"/venues/{venue_id}/movepaper", data={"pcs_id": "1", "dir": "false"})
    assert queue(client, venue_id) == [3, 1, 2, 4]
    client.put(f"/venues/{venue_id}/movepaper", data={"pcs_id": "4", "dir": "false"})
    assert queue(client, venue_id) == [3, 1, 2, 4]

    client.delete(f"/venues/{venue_id}/removepaper", data={"pcs_id": "2"})
    assert queue(client, venue_id) == [3, 1, 4]
    assert client.delete(f"/venues/{venue_id}/removepaper", data={"pcs_id": "99"}).status_code == 404
    assert client.post(f"/venues/{venue_id}/addpaper", json={"pcs_id": "abc"}).status_code == 422


def test_socket_join_and_actions(app, client, venue_id, watcher):
    other = socketio.test_client(app, flask_test_client=client)
    joined = other.emit("join", {"venue_id": venue_id}, callback=True)
    assert joined["venue"]["name"] == "CHI PC"

    assert other.emit("add_paper", {"venue_id": venue_id, "pcs_id": 7}, callback=True) == {"ok": True}
    assert other.emit("add_paper", {"venue_id": venue_id, "pcs_id": 8}, callback=True) == {"ok": True}
    other.emit("move_paper", {"venue_id": venue_id, "pcs_id": 8, "dir": "up"}, callback=True)
    assert [p["pcs_paper_id"] for p in last_state(watcher)["papers"]] == [8, 7]

    other.emit("update_venue", {"venue_id": venue_id, "venue": {"breaktime": True}}, callback=True)
    assert last_state(watcher)["venue"]["breaktime"] is True

    other.emit("remove_paper", {"venue_id": venue_id, "pcs_id": 8}, callback=True)
    assert [p["pcs_paper_id"] for p in last_state(watcher)["papers"]] == [7]

    assert "error" in other.emit("remove_paper", {"venue_id": venue_id, "pcs_id": 99}, callback=True)
    assert "error" in other.emit("join", {"venue_id": "nope"}, callback=True)
    assert isinstance(other.emit("time", callback=True), int)


def test_broadcast_is_scoped_to_venue(app, client, venue_id, watcher):
    with app.app_context():
        other_venue = Venue(name="Other")
        db.session.add(other_venue)
        db.session.commit()
        other_id = str(other_venue.id)

    client.post(f"/venues/{other_id}/addpaper", json={"pcs_id": 5})
    assert watcher.get_received() == []
