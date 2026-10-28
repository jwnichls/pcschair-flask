"""Stress test: many attendees on websockets across venues, with chairs pushing updates.

Simulates a large meeting (e.g. CHI: ~22 parallel venues, ~1000 attendees).
Attendees load the venue page and hold a Socket.IO websocket; each venue's
chair periodically makes a new paper active the way the Chrome extension does.
Load is increased in steps, and each step reports whether attendees stayed
connected and how quickly chair updates reached all of them.

Attendees are spread over several worker processes so that the load generator,
not just the server, can keep up; chairs run in the coordinating process.
Latencies use wall-clock time, which all processes on this machine share.

Run it against staging, never production during a meeting:

    uv run --group stress python stress/stress.py \\
        --url https://pcschair-staging-e00ae72137b0.herokuapp.com \\
        --venues 22 --steps 100,250,500,1000
"""

import argparse
import asyncio
import collections
import json
import multiprocessing
import os
import random
import resource
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


async def watch_loop_lag(samples, stopped):
    """If a process falls behind, latencies measure the load generator, not the server."""
    while not stopped():
        started = time.monotonic()
        await asyncio.sleep(0.5)
        samples.append(time.monotonic() - started - 0.5)


# ---------------------------------------------------------------------------
# Attendee worker processes
# ---------------------------------------------------------------------------

class Attendee:
    def __init__(self, worker, venue_id):
        self.worker = worker
        self.venue_id = venue_id
        self.connected = False
        self.last_title = None
        # Reconnection is off so that a dropped connection is counted, not hidden.
        self.sio = socketio.AsyncClient(reconnection=False)
        self.sio.on("state", self.on_state)
        self.sio.on("disconnect", self.on_disconnect)

    async def on_state(self, state):
        title = state["venue"].get("paper_title")
        if title != self.last_title:
            self.last_title = title
            self.worker.receipts[title].append(time.time())

    async def on_disconnect(self, *args):
        if self.connected and not self.worker.closing:
            self.worker.drops += 1
        self.connected = False

    async def start(self, http, load_page):
        started = time.monotonic()
        try:
            if load_page:
                async with http.get(f"{self.worker.url}/venues/{self.venue_id}") as response:
                    response.raise_for_status()
                    await response.read()
            await self.sio.connect(self.worker.url, transports=["websocket"], wait_timeout=30)
            joined = await self.sio.call("join", {"venue_id": self.venue_id}, timeout=30)
            if "error" in joined:
                raise RuntimeError(joined["error"])
            self.last_title = joined["venue"].get("paper_title")
        except Exception as e:
            self.worker.connect_errors[type(e).__name__] += 1
            await self.stop()
            return
        self.connected = True
        self.worker.connect_times.append(time.monotonic() - started)

    async def ping(self):
        started = time.monotonic()
        try:
            await self.sio.call("time", timeout=10)
            self.worker.rtts.append(time.monotonic() - started)
        except Exception:
            self.worker.rtts.append(10.0)

    async def stop(self):
        try:
            await self.sio.disconnect()
        except Exception:
            pass


class Worker:
    """Owns attendees with global index i where i % workers == index."""

    def __init__(self, url, venue_ids, index, workers, options):
        self.url = url
        self.venue_ids = venue_ids
        self.index = index
        self.workers = workers
        self.options = options
        self.attendees = []
        self.closing = False
        self.reset()

    def reset(self):
        self.receipts = collections.defaultdict(list)
        self.connect_times, self.rtts, self.loop_lag = [], [], []
        self.connect_errors = collections.Counter()
        self.drops = 0

    def live_by_venue(self):
        return dict(collections.Counter(a.venue_id for a in self.attendees if a.connected))

    async def ramp_to(self, http, total):
        mine = [i for i in range(total) if i % self.workers == self.index]
        new = [Attendee(self, self.venue_ids[i % len(self.venue_ids)]) for i in mine[len(self.attendees):]]
        semaphore = asyncio.Semaphore(self.options["connect_concurrency"])

        async def start(attendee):
            async with semaphore:
                await attendee.start(http, load_page=random.random() < self.options["page_load_fraction"])

        await asyncio.gather(*(start(a) for a in new))
        self.attendees.extend(new)

    def report(self):
        report = {
            "attempted": len(self.attendees),
            "connected": sum(a.connected for a in self.attendees),
            "receipts": dict(self.receipts),
            "connect_times": self.connect_times,
            "connect_errors": dict(self.connect_errors),
            "drops": self.drops,
            "rtts": self.rtts,
            "loop_lag_max": max(self.loop_lag, default=0.0),
        }
        self.reset()
        return report

    async def sample_rtts(self):
        while not self.closing:
            live = [a for a in self.attendees if a.connected]
            await asyncio.gather(*(a.ping() for a in random.sample(live, min(5, len(live)))))
            await asyncio.sleep(5)

    async def serve(self, conn):
        loop = asyncio.get_running_loop()
        background = [asyncio.create_task(self.sample_rtts()),
                      asyncio.create_task(watch_loop_lag(self.loop_lag, lambda: self.closing))]
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as http:
            while True:
                command, arg = await loop.run_in_executor(None, conn.recv)
                if command == "ramp":
                    await self.ramp_to(http, arg)
                    conn.send(self.live_by_venue())
                elif command == "clear":
                    # Drop ramp-period latency samples but keep this step's connection stats.
                    self.receipts.clear()
                    self.rtts, self.loop_lag = [], []
                    conn.send("cleared")
                elif command == "report":
                    conn.send(self.report())
                elif command == "stop":
                    self.closing = True
                    for task in background:
                        task.cancel()
                    await asyncio.gather(*(a.stop() for a in self.attendees), return_exceptions=True)
                    conn.send("stopped")
                    return


def worker_main(conn, url, venue_ids, index, workers, options):
    raise_fd_limit(options["fd_limit"])
    asyncio.run(Worker(url, venue_ids, index, workers, options).serve(conn))


# ---------------------------------------------------------------------------
# Coordinator: venues, chairs, steps, reporting
# ---------------------------------------------------------------------------

class Coordinator:
    def __init__(self, args):
        self.args = args
        self.url = args.url.rstrip("/")
        self.closing = False
        self.updates = {}  # title -> (venue_id, sent wall-clock time)
        self.put_latencies = []
        self.put_errors = collections.Counter()
        self.loop_lag = []

    async def create_venues(self, http, count):
        venue_ids = []
        for i in range(count):
            async with http.post(f"{self.url}/venues/new", data={"name": f"Stress test {i + 1}", "allow_adds": "on"},
                                 allow_redirects=False) as response:
                if response.status != 302:
                    sys.exit(f"Could not create venue ({response.status}); is --url a Flask pcschair server?")
                venue_ids.append(response.headers["Location"].rsplit("/", 1)[1])
        return venue_ids

    async def add_paper(self, http, venue_id, pcs_id):
        async with http.post(f"{self.url}/venues/{venue_id}/addpaper", json={"pcs_id": pcs_id}) as response:
            response.raise_for_status()

    async def seed_queue(self, http, venue_id):
        for pcs_id in range(1, QUEUE_LENGTH + 1):
            await self.add_paper(http, venue_id, pcs_id)

    async def chair(self, http, venue_id):
        """Every ~interval seconds, make the next queued paper active (as the extension does), then refill the queue."""
        interval = self.args.chair_interval
        seq = 0
        await asyncio.sleep(random.uniform(0, interval))
        while not self.closing:
            seq += 1
            title = f"Stress paper {uuid.uuid4().hex[:10]}"
            self.updates[title] = (venue_id, time.time())
            body = {"venue": {"paper_title": title, "paper_authors": "Load Tester - Somewhere",
                              "paper_pcs_id": str(seq), "active_paper": True,
                              "timer": time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(time.time() + 300))}}
            started = time.monotonic()
            try:
                async with http.put(f"{self.url}/venues/{venue_id}.json", json=body) as response:
                    response.raise_for_status()
                self.put_latencies.append(time.monotonic() - started)
                await self.add_paper(http, venue_id, QUEUE_LENGTH + seq)
            except Exception as e:
                self.put_errors[type(e).__name__] += 1
            await asyncio.sleep(interval * random.uniform(0.75, 1.25))

    def summarize(self, target, live_at_start, reports, window):
        receipts = collections.defaultdict(list)
        for report in reports:
            for title, times in report["receipts"].items():
                receipts[title].extend(times)

        expected = delivered = 0
        fanout = []
        updates = 0
        for title, (venue_id, sent) in self.updates.items():
            if not (window[0] <= sent <= window[1]) or not live_at_start.get(venue_id):
                continue
            updates += 1
            expected += live_at_start[venue_id]
            delivered += min(len(receipts[title]), live_at_start[venue_id])
            fanout.extend(received - sent for received in receipts[title])

        connect_errors = collections.Counter()
        for report in reports:
            connect_errors.update(report["connect_errors"])
        step = {
            "target": target,
            "connected": sum(r["connected"] for r in reports),
            "attempted": sum(r["attempted"] for r in reports),
            "connect_errors": dict(connect_errors),
            "drops": sum(r["drops"] for r in reports),
            "connect_p95": percentile([t for r in reports for t in r["connect_times"]], 95),
            "updates": updates,
            "delivery": delivered / expected if expected else None,
            "fanout_p50": percentile(fanout, 50),
            "fanout_p95": percentile(fanout, 95),
            "fanout_max": max(fanout, default=None),
            "chair_put_p95": percentile(self.put_latencies, 95),
            "chair_put_errors": dict(self.put_errors),
            "rtt_p95": percentile([t for r in reports for t in r["rtts"]], 95),
            "loop_lag_max": max([r["loop_lag_max"] for r in reports] + self.loop_lag, default=0.0),
        }
        self.put_latencies, self.loop_lag = [], []
        self.put_errors.clear()
        return step


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
    if step["loop_lag_max"] > 0.25:
        print(f"{'':>6} warning: load generator fell {step['loop_lag_max']:.2f}s behind; "
              f"latencies above may be inflated by this machine, not the server (try more --processes)")


async def main(args):
    steps = [int(s) for s in args.steps.split(",")]
    coordinator = Coordinator(args)
    options = {
        "connect_concurrency": max(1, args.connect_concurrency // args.processes),
        "page_load_fraction": args.page_load_fraction,
        "fd_limit": max(steps) // args.processes * 3 + 1024,
    }
    loop = asyncio.get_running_loop()

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as http:
        venue_ids = args.venue_ids.split(",") if args.venue_ids else await coordinator.create_venues(http, args.venues)
        print(f"Target {coordinator.url}: {len(venue_ids)} venues, chair update every ~{args.chair_interval}s "
              f"per venue, attendees spread over {args.processes} processes")
        await asyncio.gather(*(coordinator.seed_queue(http, v) for v in venue_ids))

        context = multiprocessing.get_context("spawn")
        pipes, processes = [], []
        for index in range(args.processes):
            parent, child = context.Pipe()
            process = context.Process(target=worker_main,
                                      args=(child, coordinator.url, venue_ids, index, args.processes, options),
                                      daemon=True)
            process.start()
            pipes.append(parent)
            processes.append(process)

        def ask(conn, command, arg=None):
            conn.send((command, arg))
            return conn.recv()

        async def ask_all(command, arg=None):
            return await asyncio.gather(*(loop.run_in_executor(None, ask, conn, command, arg) for conn in pipes))

        background = [asyncio.create_task(coordinator.chair(http, v)) for v in venue_ids]
        background.append(asyncio.create_task(watch_loop_lag(coordinator.loop_lag, lambda: coordinator.closing)))

        results = []
        print_header()
        try:
            for target in steps:
                live_at_start = collections.Counter()
                for live in await ask_all("ramp", target):
                    live_at_start.update(live)
                await ask_all("clear")
                coordinator.loop_lag = []
                window = (time.time(), None)
                await asyncio.sleep(args.hold)
                window = (window[0], time.time())
                await asyncio.sleep(args.grace)  # let in-flight updates arrive before counting
                step = coordinator.summarize(target, live_at_start, await ask_all("report"), window)
                problems = evaluate(step, args)
                step["problems"] = problems
                results.append(step)
                print_step(step, problems)
                if problems and not args.keep_going:
                    print("\nStopping at the first failing step (use --keep-going to continue).")
                    break
        finally:
            coordinator.closing = True
            for task in background:
                task.cancel()
            await ask_all("stop")
            for process in processes:
                process.join(timeout=10)

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
    parser.add_argument("--processes", type=int, default=min(8, os.cpu_count() or 1),
                        help="Attendee worker processes (default: up to 8)")
    parser.add_argument("--connect-concurrency", type=int, default=50,
                        help="Attendees connecting at once while ramping, across all processes (default: 50)")
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
