"""HTML pages plus the JSON API used by the pcschair Chrome extension.

The API keeps the Rails routes and payloads so the existing extension works
unchanged: GET/PUT /venues/<id>.json with {"venue": {...}} and
POST /venues/<id>/addpaper with {"pcs_id": ...}.
"""

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for

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


def venue_form_params():
    form = request.form
    return {
        "name": form.get("name", ""),
        "allow_adds": "allow_adds" in form,
        "pcs2_flag": "pcs2_flag" in form,
        "pcs2_venue_name": form.get("pcs2_venue_name", ""),
        "sub_committee": "sub_committee" in form,
        "sub_committee_id": form.get("sub_committee_id") or 0,
    }


@bp.get("/")
def index():
    return render_template("front.html")


@bp.get("/ntp")
def ntp():
    return str(services.now_ms()), 200, {"Content-Type": "text/plain"}


@bp.route("/venues/new", methods=["GET", "POST"])
def new_venue():
    venue = Venue(allow_adds=True, sub_committee=False, sub_committee_id=0)
    if request.method == "POST":
        try:
            for field, value in services.cast_venue_params(venue_form_params()).items():
                setattr(venue, field, value)
        except services.InvalidParams as e:
            return render_template("venue_form.html", venue=venue, error=str(e)), 422
        db.session.add(venue)
        db.session.commit()
        flash("Venue was successfully created.")
        return redirect(url_for("main.venue", venue_id=venue.id))
    return render_template("venue_form.html", venue=venue)


@bp.route("/venues/<venue_id>/edit", methods=["GET", "POST"])
def edit_venue(venue_id):
    venue = get_venue_or_404(venue_id)
    if request.method == "POST":
        try:
            services.update_venue(venue, venue_form_params())
        except services.InvalidParams as e:
            db.session.rollback()
            return render_template("venue_form.html", venue=venue, error=str(e)), 422
        flash("Venue was successfully updated.")
        return redirect(url_for("main.venue", venue_id=venue.id))
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
