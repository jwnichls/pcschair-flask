# PCS Chair Website (Flask)

A Flask port of [pcschair](https://github.com/jwnichls/pcschair), the website at
[http://www.pcschair.org/](http://www.pcschair.org/). It shows a live timer, the active
paper, and the upcoming-paper queue for program committee meetings.

For how to use the site, [see the usage doc](https://docs.google.com/document/d/1kxKCf4G7U_HD18bHi3A5mgn_nUNakMKEK3JX-WXRT-M/edit).
Admin use requires the [Chrome extension](https://github.com/jwnichls/pcschair-extension).

## What changed from the Rails version

- **Websockets instead of polling.** Pages no longer fetch the venue every 5 seconds.
  They join the venue's room over Socket.IO (websocket transport only), and the server
  pushes the full venue state whenever anything changes, whether the change came from
  the admin page, an attendee adding a paper, or the extension.
- Admin and attendee actions are sent over the socket. Clock sync (for the countdown)
  uses socket round trips instead of `/ntp`, although `/ntp` still exists.
- The Rails JSON API is kept unchanged, so the existing extension works as-is:
  `GET/PUT /venues/<id>.json`, `GET /venues/<id>/papers`, `POST /venues/<id>/addpaper`,
  `DELETE /venues/<id>/removepaper`, `PUT /venues/<id>/movepaper`. These endpoints send
  CORS headers so the extension's service worker can call them.
- The schema is the same as Rails (`venues` and `papers` with UUID keys), so you can point
  `DATABASE_URL` at the existing Postgres database.

## Running locally

```sh
uv sync --group dev
uv run python app.py        # http://localhost:3000 (the port the extension matches)
uv run pytest
```

Create a venue at `/venues/new`. Attendees use `/venues/<id>`, and the chair uses `/venues/<id>/admin`.

SQLite (`instance/pcschair.db`) is used by default.

## Parity tests against the Rails app

`parity/` holds headless-browser (Playwright) scenarios that drive the pages and the
extension's JSON API identically on both implementations. Each scenario checks the
expected behavior. With two targets configured, `test_parity` also checks that both
implementations made identical observations.

```sh
uv sync --group parity && uv run playwright install chromium

# Rails in Docker on :3001 (first start builds gems, which takes a few minutes)
colima start
docker-compose -f parity/rails/docker-compose.yml up -d

uv run python app.py &   # Flask on :3000
PCSCHAIR_TARGETS="flask=http://localhost:3000,rails=http://localhost:3001" \
  uv run --group parity pytest parity
```

`PCSCHAIR_TARGETS` defaults to Flask alone. Set `HEADED=1` to watch the browser.

## Stress testing

`stress/stress.py` simulates a large meeting to size the Heroku resources.
Simulated attendees are spread across venues; each loads the venue page and holds
a websocket. Each venue's chair makes a new paper active every ~20 seconds, the way
the extension does. Load increases in steps, and each step reports:
- how many attendees stayed connected
- the share of chair updates that reached them, and how quickly (p50/p95/max)
- how long the chair's update request took
- socket round-trip time

```sh
uv run --group stress python stress/stress.py \
  --url https://pcschair-staging-e00ae72137b0.herokuapp.com \
  --venues 22 --steps 100,250,500,750,1000,1500 --output results.json
```

Run it against staging, scaled to the dyno type being evaluated, and never against
production during a meeting. The tool creates "Stress test N" venues on the target
(or reuses them via `--venue-ids`). A warning about the load generator falling
behind means the measuring machine, not the server, was the bottleneck.

## Configuration

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` | Database URL, e.g. `postgres://...` |
| `SECRET_KEY` | Flask session secret (used for flash messages) |
| `SOCKETIO_MESSAGE_QUEUE` | Redis URL, needed only when running more than one server process (`--extra redis`) |

## Deployment

```sh
gunicorn --worker-class gevent --workers 1 --worker-connections 3000 --bind 0.0.0.0:$PORT app:app
```

One gevent worker holds thousands of websockets in a single process (the threaded worker hit Heroku's per-dyno thread limit at about 100 users). To run
multiple workers or dynos, set `SOCKETIO_MESSAGE_QUEUE` to a Redis URL so broadcasts
reach every process. The client uses only the websocket transport, so sticky sessions
aren't required.
