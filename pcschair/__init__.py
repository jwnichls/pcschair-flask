import os

from flask import Flask

from .extensions import db, socketio


def database_url():
    url = os.environ.get("DATABASE_URL", "sqlite:///pcschair.db")
    # Heroku-style URLs name the scheme "postgres"; SQLAlchemy needs an explicit driver.
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def socketio_async_mode():
    """gevent under gunicorn's gevent worker (production); threads for the dev server and tests.

    The threaded mode holds OS threads per connection, which hits Heroku's
    per-dyno thread limit at about 100 attendees; gevent handles thousands.
    """
    try:
        from gevent import monkey
    except ImportError:
        return "threading"
    return "gevent" if monkey.is_module_patched("socket") else "threading"


def create_app(config=None):
    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev"),
        SQLALCHEMY_DATABASE_URI=database_url(),
        # Set to a Redis URL to broadcast across multiple server processes.
        SOCKETIO_MESSAGE_QUEUE=os.environ.get("SOCKETIO_MESSAGE_QUEUE"),
        # Login for the venue list at /venues; the list is disabled unless both are set.
        VENUES_USERNAME=os.environ.get("VENUES_USERNAME"),
        VENUES_PASSWORD=os.environ.get("VENUES_PASSWORD"),
        # Comma-separated one-word answers to the question asked before creating a venue.
        # Unset, anyone can create venues.
        VENUE_SPONSOR_ANSWERS=os.environ.get("VENUE_SPONSOR_ANSWERS"),
    )
    if config:
        app.config.update(config)

    # Socket handlers must be registered before socketio.init_app runs.
    from . import sockets, views  # noqa: F401

    db.init_app(app)
    socketio.init_app(
        app,
        async_mode=socketio_async_mode(),
        message_queue=app.config["SOCKETIO_MESSAGE_QUEUE"],
    )
    app.register_blueprint(views.bp)

    # Creates missing tables only; an existing database from the Rails app is used as-is.
    with app.app_context():
        db.create_all()

    return app
