import os

from pcschair import create_app, socketio

app = create_app()

if __name__ == "__main__":
    # Development server only; use gunicorn in production (see Procfile).
    # Port 3000 matches the Chrome extension's localhost content-script match.
    socketio.run(
        app,
        host="127.0.0.1",
        port=int(os.environ.get("PORT", 3000)),
        debug=True,
        allow_unsafe_werkzeug=True,
    )
