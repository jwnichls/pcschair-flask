"""Drives one running pcschair implementation (Flask or Rails) through a real browser.

Everything here goes through the public surface both implementations share: the
HTML pages (located by the element ids the Chrome extension also relies on) and
the JSON API the extension calls. Nothing depends on how state reaches the page,
so the same steps work against Rails' 5-second polling and Flask's websockets.
"""

import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

# Rails polls every 5 seconds, so allow a couple of poll cycles for changes to show.
PROPAGATION_TIMEOUT = 12.0

# Records every audio play() call, by file name, in window.__plays.
PLAY_SPY = """
window.__plays = [];
const originalPlay = HTMLMediaElement.prototype.play;
HTMLMediaElement.prototype.play = function () {
  window.__plays.push(this.src.split('/').pop().split('?')[0]);
  try { return originalPlay.call(this).catch(() => {}); } catch (e) { return Promise.resolve(); }
};
"""

# Both implementations render queue controls with these classes, in DOM order.
CONTROL_CLASSES_JS = """li => [...li.querySelectorAll('.upimg, .downimg, .clearimg, .removeimg')]
  .map(el => ['upimg', 'downimg', 'clearimg', 'removeimg'].find(c => el.classList.contains(c)))"""


def http_date(dt):
    """What the extension sends: JavaScript's Date.toUTCString()."""
    return format_datetime(dt.astimezone(timezone.utc), usegmt=True)


def utc_in(seconds):
    return http_date(datetime.now(timezone.utc) + timedelta(seconds=seconds))


def eventually(read, expected, timeout=PROPAGATION_TIMEOUT, interval=0.2):
    """Poll read() until it returns expected (or satisfies it, if callable); return the last value."""
    matches = expected if callable(expected) else (lambda value: value == expected)
    deadline = time.monotonic() + timeout
    value = read()
    while not matches(value) and time.monotonic() < deadline:
        time.sleep(interval)
        value = read()
    assert matches(value), f"timed out waiting for {expected!r}; last value {value!r}"
    return value


class Site:
    def __init__(self, name, base_url, browser, playwright):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.browser = browser
        # The extension doesn't follow redirects meaningfully; neither do we.
        self.api = playwright.request.new_context(base_url=self.base_url, max_redirects=0)

    # ---- Pages ----

    def new_page(self):
        context = self.browser.new_context(base_url=self.base_url)
        context.add_init_script(PLAY_SPY)
        return context.new_page()

    def open_attendee(self, venue_id):
        page = self.new_page()
        page.goto(f"/venues/{venue_id}")
        self.wait_until_loaded(page)
        return page

    def open_admin(self, venue_id):
        page = self.new_page()
        page.goto(f"/venues/{venue_id}/admin")
        self.wait_until_loaded(page)
        return page

    @staticmethod
    def wait_until_loaded(page):
        # Both implementations replace the server-rendered paper info with
        # JS-rendered markup once state arrives; wait for that to settle.
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(500)

    # ---- Venue setup through the HTML form ----

    @staticmethod
    def _field(page, name):
        # Rails names fields venue[name] (plus hidden inputs for checkboxes); Flask uses bare names.
        return page.locator(f'input[name="venue[{name}]"]:not([type=hidden]), input[name="{name}"]')

    def create_venue(self, name=None, allow_adds=True, pcs2_flag=False, pcs2_venue_name="",
                     sub_committee=False):
        """Create a venue through /venues/new. Returns (venue_id, notice text)."""
        name = name or f"Parity {uuid.uuid4().hex[:8]}"
        page = self.new_page()
        page.goto("/venues/new")
        self._field(page, "name").fill(name)
        self._field(page, "allow_adds").set_checked(allow_adds)
        self._field(page, "pcs2_flag").set_checked(pcs2_flag)
        self._field(page, "pcs2_venue_name").fill(pcs2_venue_name)
        self._field(page, "sub_committee").set_checked(sub_committee)
        page.get_by_role("button", name="Create Venue").click()
        page.wait_for_url(re.compile(r"/venues/[0-9a-f-]{36}$"))
        venue_id = page.url.rsplit("/", 1)[1]
        notice = page.locator("#notice").inner_text().strip()
        page.context.close()
        return venue_id, notice

    # ---- JSON API, called exactly as the extension's background.js does ----

    def venue_json(self, venue_id):
        response = self.api.get(f"/venues/{venue_id}.json")
        assert response.ok, f"GET venue failed: {response.status}"
        return response.json()

    def papers_json(self, venue_id):
        response = self.api.get(f"/venues/{venue_id}/papers")
        assert response.ok, f"GET papers failed: {response.status}"
        return response.json()

    def queued_ids_json(self, venue_id):
        return [p["pcs_paper_id"] for p in self.papers_json(venue_id)]

    def put_venue(self, venue_id, attrs):
        response = self.api.put(f"/venues/{venue_id}.json", data={"venue": attrs},
                                headers={"Content-Type": "application/json"})
        assert response.status < 400, f"PUT venue failed: {response.status}"
        return response

    def add_paper_api(self, venue_id, pcs_id):
        response = self.api.post(f"/venues/{venue_id}/addpaper", data={"pcs_id": pcs_id},
                                 headers={"Content-Type": "application/json"})
        # Rails answers the extension's request with a redirect; only the effect matters.
        assert response.status < 400, f"addpaper failed: {response.status}"
        eventually(lambda: pcs_id in self.queued_ids_json(venue_id), True)

    # ---- Reading pages ----

    @staticmethod
    def queue(page):
        texts = page.locator("#paper-queue li").all_inner_texts()
        return [int(re.search(r"\d+", text).group()) for text in texts]

    @staticmethod
    def queue_item(page, pcs_id):
        return page.locator("#paper-queue li").filter(has_text=re.compile(rf"^\s*{pcs_id}\s*$"))

    @staticmethod
    def queue_controls(page):
        return [li.evaluate(CONTROL_CLASSES_JS) for li in page.locator("#paper-queue li").all()]

    @staticmethod
    def timer(page):
        return page.locator("#timer").inner_text().strip()

    @staticmethod
    def paper_info(page):
        info = page.locator("#paper-info")
        return {
            "id": info.locator(".paper-id").all_inner_texts(),
            "title": info.locator(".paper-title").all_inner_texts(),
            "authors": info.locator("li.author").all_inner_texts(),
            "none": info.locator(".no-active-paper").all_inner_texts(),
        }

    @staticmethod
    def plays(page):
        return page.evaluate("window.__plays.slice()")

    # ---- Admin actions through the UI ----

    def admin_add_paper(self, page, pcs_id):
        page.fill("#newPaperId", str(pcs_id))
        page.click("#addNewPaper")
        # Wait for the add to land before the next one: Rails assigns list order
        # from the current maximum, so overlapping adds can race.
        eventually(lambda: pcs_id in self.queue(page), True)

    def admin_move(self, page, pcs_id, up):
        before = self.queue(page)
        self.queue_item(page, pcs_id).locator(".upimg" if up else ".downimg").click()
        eventually(lambda: self.queue(page) != before, True)

    def admin_remove(self, page, pcs_id):
        self.queue_item(page, pcs_id).locator(".removeimg").click()
        eventually(lambda: pcs_id not in self.queue(page), True)
