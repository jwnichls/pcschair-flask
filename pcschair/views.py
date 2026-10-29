"""HTML pages plus the JSON API used by the pcschair Chrome extension.

The API keeps the Rails routes and payloads so the existing extension works
unchanged: GET/PUT /venues/<id>.json with {"venue": {...}} and
POST /venues/<id>/addpaper with {"pcs_id": ...}.
"""

import hmac
import re
from functools import wraps

from flask import (
    Blueprint, Response, abort, current_app, flash, jsonify, redirect, render_template, request, session, url_for,
)
from sqlalchemy import select

from . import services
from .extensions import db
from .models import Venue, find_venue

bp = Blueprint("main", __name__)

# Endpoints the extension calls from its service worker, which needs CORS.
CORS_ENDPOINTS = {"main.venue", "main.papers", "main.add_paper", "main.remove_paper", "main.move_paper"}


@bp.after_app_request
def add_cors_headers(response):
    if request.endpoint in CORS_ENDPOINTS:
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


@bp.app_errorhandler(services.ServiceError)
def handle_service_error(error):
    return jsonify(errors=[str(error)]), error.status


def get_venue_or_404(venue_id):
    venue = find_venue(venue_id)
    if venue is None:
        abort(404)
    return venue


def request_params():
    """Merge query, form and JSON params, like Rails' params hash."""
    params = request.args.to_dict()
    params.update(request.form.to_dict())
    body = request.get_json(silent=True)
    if isinstance(body, dict):
        params.update(body)
    return params


def venue_update_params():
    """Accepts {"venue": {...}} JSON, form-encoded venue[field]=..., or flat JSON."""
    params = request_params()
    if isinstance(params.get("venue"), dict):
        return params["venue"]
    nested = {k[len("venue["):-1]: v for k, v in params.items() if k.startswith("venue[") and k.endswith("]")}
    return nested or params


# PCS2 venue names are a few letters, a two-digit year and a letter, e.g. "chi27a".
PCS2_VENUE_NAME = re.compile(r"[a-z]+\d{2}[a-z]")
# Lets people paste any PCS URL for their venue instead of picking the name out of it.
PCS2_URL = re.compile(r"precisionconference\.com/+([^/?#\s]+)", re.IGNORECASE)


def normalize_pcs2_venue_name(value):
    value = value.strip()
    match = PCS2_URL.search(value)
    if match:
        value = match.group(1)
    return value.removeprefix("~").lower()


def venue_form_params(venue):
    """Validated attributes from the venue form, or a {field: message} dict of errors.

    Only PCS2 is supported, so pcs2_flag is always set. A subcommittee venue
    gets sub_committee_id -1, which makes the admin page ask the chair for it.
    """
    form = request.form
    name = form.get("name", "").strip()
    pcs2_venue_name = normalize_pcs2_venue_name(form.get("pcs2_venue_name", ""))
    sub_committee = "sub_committee" in form

    errors = {}
    if not name:
        errors["name"] = "Enter a name for your meeting."
    elif len(name) > 200:
        errors["name"] = "Use a name of 200 characters or fewer."
    if not pcs2_venue_name:
        errors["pcs2_venue_name"] = "Enter your PCS venue name."
    elif not PCS2_VENUE_NAME.fullmatch(pcs2_venue_name):
        errors["pcs2_venue_name"] = (
            f"\u201c{pcs2_venue_name}\u201d doesn't look like a PCS venue name. "
            "It's usually a few letters, two digits and a letter, like chi27a."
        )

    if sub_committee:
        # Keep an ID the chair already entered; otherwise ask for it on the admin page.
        sub_committee_id = venue.sub_committee_id if venue.sub_committee and (venue.sub_committee_id or 0) > 0 else -1
    else:
        sub_committee_id = 0

    attrs = {
        "name": name,
        "allow_adds": "allow_adds" in form,
        "pcs2_flag": True,
        "pcs2_venue_name": pcs2_venue_name,
        "sub_committee": sub_committee,
        "sub_committee_id": sub_committee_id,
    }
    return attrs, errors


def venue_list_credentials():
    config = current_app.config
    return config.get("VENUES_USERNAME"), config.get("VENUES_PASSWORD")


def require_venue_list_auth(view):
    """HTTP basic auth with the fixed username and password from the config.

    The list is disabled (404) unless both are configured.
    """
    @wraps(view)
    def wrapped(*args, **kwargs):
        username, password = venue_list_credentials()
        if not username or not password:
            abort(404)
        auth = request.authorization
        ok = (
            auth is not None
            and auth.type == "basic"
            and hmac.compare_digest((auth.username or "").encode(), username.encode())
            and hmac.compare_digest((auth.password or "").encode(), password.encode())
        )
        if not ok:
            return Response("Login required.", 401, {"WWW-Authenticate": 'Basic realm="PCS Chair venues"'})
        return view(*args, **kwargs)
    return wrapped


@bp.get("/")
def index():
    return render_template("front.html")


@bp.get("/ntp")
def ntp():
    return str(services.now_ms()), 200, {"Content-Type": "text/plain"}


@bp.get("/venues", strict_slashes=False)
@require_venue_list_auth
def venues():
    venues = db.session.scalars(select(Venue).order_by(Venue.created_at.desc())).all()
    return render_template("venue_list.html", venues=venues)


@bp.post("/venues/<venue_id>/delete")
@require_venue_list_auth
def delete_venue(venue_id):
    venue = get_venue_or_404(venue_id)
    name = venue.name
    db.session.delete(venue)
    db.session.commit()
    flash(f'Deleted "{name}".')
    return redirect(url_for("main.venues"))


def sponsor_answers():
    """Accepted answers to the sponsor question, lowercased; empty means no question is asked."""
    raw = current_app.config.get("VENUE_SPONSOR_ANSWERS") or ""
    return {answer.strip().lower() for answer in raw.split(",") if answer.strip()}


def may_create_venue():
    return not sponsor_answers() or session.get("sponsor_answered")


@bp.post("/venues/new/check")
def check_sponsor_answer():
    """Answers are single words, so accept a reply containing one, e.g. "the ACM"."""
    words = re.findall(r"\w+", request.form.get("answer", "").lower())
    if sponsor_answers() & set(words):
        session["sponsor_answered"] = True
        return redirect(url_for("main.new_venue"))
    return render_template("venue_gate.html", answer=request.form.get("answer", ""), wrong=True), 403


@bp.route("/venues/new", methods=["GET", "POST"])
def new_venue():
    if not may_create_venue():
        if request.method == "POST":
            abort(403)
        return render_template("venue_gate.html")
    venue = Venue(allow_adds=True, sub_committee=False, sub_committee_id=0)
    if request.method == "POST":
        attrs, errors = venue_form_params(venue)
        if errors:
            return render_template("venue_form.html", venue=venue, form=request.form, errors=errors), 422
        for field, value in attrs.items():
            setattr(venue, field, value)
        db.session.add(venue)
        db.session.commit()
        flash("Venue was successfully created.")
        return redirect(url_for("main.admin", venue_id=venue.id))
    return render_template("venue_form.html", venue=venue)


@bp.route("/venues/<venue_id>/edit", methods=["GET", "POST"])
def edit_venue(venue_id):
    venue = get_venue_or_404(venue_id)
    if request.method == "POST":
        attrs, errors = venue_form_params(venue)
        if errors:
            return render_template("venue_form.html", venue=venue, form=request.form, errors=errors), 422
        services.update_venue(venue, attrs)
        flash("Venue was successfully updated.")
        return redirect(url_for("main.admin", venue_id=venue.id))
    return render_template("venue_form.html", venue=venue)


@bp.route("/venues/<venue_id>", methods=["GET", "PUT", "PATCH"])
def venue(venue_id):
    as_json = venue_id.endswith(".json")
    venue = get_venue_or_404(venue_id.removesuffix(".json"))

    if request.method == "GET":
        if as_json:
            return jsonify(venue.to_json())
        return render_template("venue_show.html", venue=venue, admin=False, state=services.venue_state(venue))

    services.update_venue(venue, venue_update_params())
    return "", 204


@bp.get("/venues/<venue_id>/admin")
def admin(venue_id):
    venue = get_venue_or_404(venue_id)
    return render_template("venue_admin.html", venue=venue, admin=True, state=services.venue_state(venue))


@bp.get("/venues/<venue_id>/papers")
def papers(venue_id):
    venue = get_venue_or_404(venue_id)
    return jsonify([p.to_json() for p in venue.papers])


@bp.post("/venues/<venue_id>/addpaper")
def add_paper(venue_id):
    services.add_paper(get_venue_or_404(venue_id), request_params().get("pcs_id"))
    return "", 204


@bp.delete("/venues/<venue_id>/removepaper")
def remove_paper(venue_id):
    services.remove_paper(get_venue_or_404(venue_id), request_params().get("pcs_id"))
    return "", 204


@bp.put("/venues/<venue_id>/movepaper")
def move_paper(venue_id):
    params = request_params()
    up = str(params.get("dir")).lower() in ("true", "up")
    services.move_paper(get_venue_or_404(venue_id), params.get("pcs_id"), up=up)
    return "", 204
