"""Stress test: many attendees on websockets across venues, with chairs pushing updates.

Simulates a large meeting (e.g. CHI: ~22 parallel venues, ~1000 attendees).
Attendees load the venue page and hold a Socket.IO websocket; each venue's
chair periodically makes a new paper active the way the Chrome extension does.
Load is increased in steps, and each step reports whether attendees stayed
connected and how quickly chair updates reached all of them.

Run it against staging, never production during a meeting:

    uv run --group stress python stress/stress.py \\
        --url https://pcschair-staging-e00ae72137b0.herokuapp.com \\
        --venues 22 --steps 100,250,500,1000
"""

import argparse
import asyncio
import collections
import json
import random
import resource
import statistics
import sys
import time
import uuid

import aiohttp
import socketio

QUEUE_LENGTH = 12


def percentile(values, pct):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(pct / 100 * (len(ordered) - 1))))]


def fmt_ms(seconds):
    return "-" if seconds is None else f"{seconds * 1000:.0f}"


def raise_fd_limit(needed):
    """Each websocket needs a file descriptor; macOS defaults to 256."""
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    target = needed if hard == resource.RLIM_INFINITY else min(needed, hard)
    if soft < target:
        resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
    return resource.getrlimit(resource.RLIMIT_NOFILE)[0]


class Run:
    """Shared state and measurements for one stress run."""

    def __init__(self, args):
        self.args = args
        self.url = args.url.rstrip("/")
        self.closing = False
        self.live = collections.Counter()  # venue_id -> connected attendees
        self.updates = {}  # title -> {"venue", "sent", "expected", "latencies"}
        self.put_latencies = []
        self.put_errors = collections.Counter()
        self.connect_errors = collections.Counter()
        self.connect_times = []
        self.drops = 0
        self.rtts = []
        self.loop_lag = []

    def reset_step(self):
        self.put_latencies, self.connect_times, self.rtts, self.loop_lag = [], [], [], []
        self.put_errors.clear()
        self.connect_errors.clear()
        self.drops = 0


class Attendee:
    def __init__(self, run, venue_id):
        self.run = run
        self.venue_id = venue_id
        self.connected = False
        self.last_title = None
        # Reconnection is off so that a dropped connection is counted, not hidden.
        self.sio = socketio.AsyncClient(reconnection=False)
        self.sio.on("state", self.on_state)
        self.sio.on("disconnect", self.on_disconnect)

    async def on_state(self, state):
        title = state["venue"].get("paper_title")
        if title == self.last_title:
            return
        self.last_title = title
        update = self.run.updates.get(title)
        if update is not None:
            update["latencies"].append(time.monotonic() - update["sent"])

    async def on_disconnect(self, *args):
        if self.connected and not self.run.closing:
            self.run.drops += 1
            self.run.live[self.venue_id] -= 1
        self.connected = False

    async def start(self, http, load_page):
        started = time.monotonic()
        try:
            if load_page:
                async with http.get(f"{self.run.url}/venues/{self.venue_id}") as response:
                    response.raise_for_status()
                    await response.read()
            await self.sio.connect(self.run.url, transports=["websocket"], wait_timeout=30)
            joined = await self.sio.call("join", {"venue_id": self.venue_id}, timeout=30)
            if "error" in joined:
                raise RuntimeError(joined["error"])
            self.last_title = joined["venue"].get("paper_title")
        except Exception as e:
            self.run.connect_errors[type(e).__name__] += 1
            await self.stop()
            return
        self.connected = True
        self.run.live[self.venue_id] += 1
        self.run.connect_times.append(time.monotonic() - started)

    async def ping(self):
        started = time.monotonic()
        try:
            await self.sio.call("time", timeout=10)
            self.run.rtts.append(time.monotonic() - started)
        except Exception:
            self.run.rtts.append(10.0)

    async def stop(self):
        try:
            await self.sio.disconnect()
        except Exception:
            pass


async def create_venues(run, http, count):
    venue_ids = []
    for i in range(count):
        async with http.post(f"{run.url}/venues/new", data={"name": f"Stress test {i + 1}", "allow_adds": "on"},
                             allow_redirects=False) as response:
            if response.status != 302:
                sys.exit(f"Could not create venue ({response.status}); is --url a Flask pcschair server?")
            venue_ids.append(response.headers["Location"].rsplit("/", 1)[1])
    return venue_ids


async def add_paper(run, http, venue_id, pcs_id):
    async with http.post(f"{run.url}/venues/{venue_id}/addpaper", json={"pcs_id": pcs_id}) as response:
        response.raise_for_status()


async def seed_queue(run, http, venue_id):
    for pcs_id in range(1, QUEUE_LENGTH + 1):
        await add_paper(run, http, venue_id, pcs_id)


async def chair(run, http, venue_id, interval):
    """Every ~interval seconds, make the next queued paper active (as the extension does), then refill the queue."""
    seq = 0
    await asyncio.sleep(random.uniform(0, interval))
    while not run.closing:
        seq += 1
        title = f"Stress paper {uuid.uuid4().hex[:10]}"
        run.updates[title] = {"venue": venue_id, "sent": time.monotonic(),
                              "expected": run.live[venue_id], "latencies": []}
        body = {"venue": {"paper_title": title, "paper_authors": "Load Tester - Somewhere",
                          "paper_pcs_id": str(seq), "active_paper": True,
                          "timer": time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(time.time() + 300))}}
        started = time.monotonic()
        try:
            async with http.put(f"{run.url}/venues/{venue_id}.json", json=body) as response:
                response.raise_for_status()
            run.put_latencies.append(time.monotonic() - started)
            await add_paper(run, http, venue_id, QUEUE_LENGTH + seq)
        except Exception as e:
            run.put_errors[type(e).__name__] += 1
        await asyncio.sleep(interval * random.uniform(0.75, 1.25))


async def sample_rtts(run, attendees):
    while not run.closing:
        live = [a for a in attendees if a.connected]
        await asyncio.gather(*(a.ping() for a in random.sample(live, min(25, len(live)))))
        await asyncio.sleep(5)


async def watch_loop_lag(run):
    """If this process falls behind, latencies measure the load generator, not the server."""
    while not run.closing:
        started = time.monotonic()
        await asyncio.sleep(0.5)
        run.loop_lag.append(time.monotonic() - started - 0.5)


async def ramp_to(run, http, attendees, venue_ids, target):
    semaphore = asyncio.Semaphore(run.args.connect_concurrency)
    new = []
    while len(attendees) + len(new) < target:
        new.append(Attendee(run, venue_ids[(len(attendees) + len(new)) % len(venue_ids)]))

    async def start(attendee):
        async with semaphore:
            await attendee.start(http, load_page=random.random() < run.args.page_load_fraction)

    await asyncio.gather(*(start(a) for a in new))
    attendees.extend(new)


def summarize_step(run, target, attendees, window_start, window_end):
    updates = [u for u in run.updates.values() if window_start <= u["sent"] <= window_end and u["expected"] > 0]
    expected = sum(u["expected"] for u in updates)
    delivered = sum(min(len(u["latencies"]), u["expected"]) for u in updates)
    fanout = [latency for u in updates for latency in u["latencies"]]
    connected = sum(a.connected for a in attendees)
    attempted = len(attendees)
    return {
        "target": target,
        "connected": connected,
        "attempted": attempted,
        "connect_errors": dict(run.connect_errors),
        "drops": run.drops,
        "connect_p95": percentile(run.connect_times, 95),
        "updates": len(updates),
        "delivery": delivered / expected if expected else None,
        "fanout_p50": percentile(fanout, 50),
        "fanout_p95": percentile(fanout, 95),
        "fanout_max": max(fanout) if fanout else None,
        "chair_put_p95": percentile(run.put_latencies, 95),
        "chair_put_errors": dict(run.put_errors),
        "rtt_p95": percentile(run.rtts, 95),
        "loop_lag_max": max(run.loop_lag) if run.loop_lag else None,
    }


def evaluate(step, args):
    problems = []
    if step["connected"] < step["attempted"] * args.min_connected:
        problems.append(f"only {step['connected']}/{step['attempted']} connected")
    if step["delivery"] is not None and step["delivery"] < args.min_delivery:
        problems.append(f"delivery {step['delivery']:.1%}")
    if step["fanout_p95"] is not None and step["fanout_p95"] > args.max_fanout_p95:
        problems.append(f"p95 update latency {step['fanout_p95']:.1f}s")
    if step["chair_put_errors"]:
        problems.append(f"chair update errors {step['chair_put_errors']}")
    return problems


def print_header():
    print(f"\n{'users':>6} {'connected':>10} {'drops':>6} {'conn p95':>9} {'updates':>8} {'delivered':>10} "
          f"{'p50 ms':>7} {'p95 ms':>7} {'max ms':>7} {'chair p95':>10} {'rtt p95':>8}  result")


def print_step(step, problems):
    delivery = "-" if step["delivery"] is None else f"{step['delivery']:.1%}"
    print(f"{step['target']:>6} {step['connected']:>5}/{step['attempted']:<4} {step['drops']:>6} "
          f"{fmt_ms(step['connect_p95']):>9} {step['updates']:>8} {delivery:>10} "
          f"{fmt_ms(step['fanout_p50']):>7} {fmt_ms(step['fanout_p95']):>7} {fmt_ms(step['fanout_max']):>7} "
          f"{fmt_ms(step['chair_put_p95']):>10} {fmt_ms(step['rtt_p95']):>8}  "
          f"{'PASS' if not problems else 'FAIL: ' + '; '.join(problems)}", flush=True)
    if step["connect_errors"]:
        print(f"{'':>6} connect errors: {step['connect_errors']}")
    if step["loop_lag_max"] is not None and step["loop_lag_max"] > 0.25:
        print(f"{'':>6} warning: load generator fell {step['loop_lag_max']:.2f}s behind; "
              f"latencies above may be inflated by this machine, not the server")


async def main(args):
    steps = [int(s) for s in args.steps.split(",")]
    print(f"File descriptor limit: {raise_fd_limit(max(steps) * 3 + 1024)}")
    run = Run(args)
    attendees = []
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout, connector=aiohttp.TCPConnector(limit=200)) as http:
        venue_ids = args.venue_ids.split(",") if args.venue_ids else await create_venues(run, http, args.venues)
        print(f"Target {run.url}: {len(venue_ids)} venues, chair update every ~{args.chair_interval}s per venue")
        await asyncio.gather(*(seed_queue(run, http, v) for v in venue_ids))

        background = [asyncio.create_task(chair(run, http, v, args.chair_interval)) for v in venue_ids]
        background += [asyncio.create_task(sample_rtts(run, attendees)), asyncio.create_task(watch_loop_lag(run))]

        results = []
        print_header()
        try:
            for target in steps:
                run.reset_step()
                await ramp_to(run, http, attendees, venue_ids, target)
                window_start = time.monotonic()
                await asyncio.sleep(args.hold)
                window_end = time.monotonic()
                await asyncio.sleep(args.grace)  # let in-flight updates arrive before counting
                step = summarize_step(run, target, attendees, window_start, window_end)
                problems = evaluate(step, args)
                step["problems"] = problems
                results.append(step)
                print_step(step, problems)
                if problems and not args.keep_going:
                    print("\nStopping at the first failing step (use --keep-going to continue).")
                    break
        finally:
            run.closing = True
            for task in background:
                task.cancel()
            await asyncio.gather(*(a.stop() for a in attendees), return_exceptions=True)

    if args.output:
        with open(args.output, "w") as f:
            json.dump({"args": vars(args), "venue_ids": venue_ids, "steps": results}, f, indent=2)
        print(f"\nResults written to {args.output}")
    passed = [s["target"] for s in results if not s["problems"]]
    print(f"\nHighest passing step: {max(passed) if passed else 'none'} users")


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", required=True, help="Base URL of a Flask pcschair server (use staging)")
    parser.add_argument("--venues", type=int, default=22, help="Venues to create (default: 22, like CHI)")
    parser.add_argument("--venue-ids", help="Comma-separated existing venue ids to use instead of creating venues")
    parser.add_argument("--steps", default="100,250,500,750,1000,1500", help="Total attendees at each step")
    parser.add_argument("--hold", type=float, default=60, help="Seconds to hold each step (default: 60)")
    parser.add_argument("--grace", type=float, default=5, help="Seconds to wait for in-flight updates (default: 5)")
    parser.add_argument("--chair-interval", type=float, default=20,
                        help="Average seconds between a venue's chair updates (default: 20)")
    parser.add_argument("--connect-concurrency", type=int, default=50,
                        help="Attendees connecting at once while ramping (default: 50)")
    parser.add_argument("--page-load-fraction", type=float, default=1.0,
                        help="Fraction of attendees that load the venue page before connecting (default: 1)")
    parser.add_argument("--min-connected", type=float, default=0.99, help="Pass threshold (default: 0.99)")
    parser.add_argument("--min-delivery", type=float, default=0.99, help="Pass threshold (default: 0.99)")
    parser.add_argument("--max-fanout-p95", type=float, default=2.0,
                        help="Pass threshold for p95 update latency, seconds (default: 2)")
    parser.add_argument("--keep-going", action="store_true", help="Continue after a failing step")
    parser.add_argument("--output", help="Write per-step results as JSON")
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(main(parse_args()))
