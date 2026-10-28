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


def create_app(config=None):
    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev"),
        SQLALCHEMY_DATABASE_URI=database_url(),
        # Set to a Redis URL to broadcast across multiple server processes.
        SOCKETIO_MESSAGE_QUEUE=os.environ.get("SOCKETIO_MESSAGE_QUEUE"),
    )
    if config:
        app.config.update(config)

    # Socket handlers must be registered before socketio.init_app runs.
    from . import sockets, views  # noqa: F401

    db.init_app(app)
    socketio.init_app(
        app,
        async_mode="threading",
        message_queue=app.config["SOCKETIO_MESSAGE_QUEUE"],
    )
    app.register_blueprint(views.bp)

    # Creates missing tables only; an existing database from the Rails app is used as-is.
    with app.app_context():
        db.create_all()

    return app
