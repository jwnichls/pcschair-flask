"""Browser scenarios that must behave identically on the Rails and Flask implementations.

Each scenario drives one target through pages and the extension's JSON API,
asserts the expected behaviour, and returns a dict of normalized observations.
test_parity then compares those observations across targets.
"""

import pytest

from parity.conftest import TARGETS
from parity.site import eventually, utc_in

SCENARIOS = {}


def scenario(fn):
    SCENARIOS[fn.__name__] = fn
    return fn


def visible(page, selector):
    locator = page.locator(selector)
    return locator.count() > 0 and locator.first.is_visible()


def set_active_paper(site, venue_id, pcs_id, title="A Paper", authors="Ada Lovelace - Analytical Engines"):
    """What the extension sends when a submission page is visited (PCSCHAIRsendPaperData)."""
    site.put_venue(venue_id, {
        "paper_title": title,
        "paper_authors": authors,
        "paper_pcs_id": str(pcs_id),  # the extension sends the id it scraped from the URL, as a string
        "active_paper": True,
        "timer": utc_in(5 * 60),
    })


def clear_active_paper(site, venue_id):
    """PCSCHAIRclearActivePaper in the extension."""
    site.put_venue(venue_id, {"paper_title": "", "paper_authors": "", "active_paper": False, "timer": None})


VENUE_FIELDS = [
    "name", "active_paper", "timer", "breaktime", "paper_title", "paper_authors", "paper_pcs_id",
    "pcs2_flag", "pcs2_venue_name", "allow_adds", "sub_committee", "sub_committee_id",
]


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

@scenario
def create_and_show(site):
    venue_id, notice = site.create_venue(name="Parity Venue", allow_adds=True)
    venue = site.venue_json(venue_id)
    page = site.open_attendee(venue_id)

    obs = {
        "notice": notice,
        "json": {field: venue[field] for field in VENUE_FIELDS},
        "heading": page.locator("h1").first.inner_text().strip(),
        "timer": site.timer(page),
        "paper_info": site.paper_info(page),
        "queue_count": page.locator("#queue-count").inner_text().strip(),
        "add_paper_visible": visible(page, "#newPaperId"),
        "break_visible": visible(page, "#break"),
        "volume_control": visible(page, "#volumeRange"),
    }
    assert obs["notice"] == "Venue was successfully created."
    assert obs["heading"] == "Parity Venue"
    assert obs["timer"] == "0:00"
    assert obs["paper_info"]["none"] == ["No Active Paper"]
    assert obs["add_paper_visible"] and not obs["break_visible"]
    return obs


@scenario
def json_api(site):
    venue_id, _ = site.create_venue()
    site.add_paper_api(venue_id, 11)
    obs = {
        "venue_keys": sorted(site.venue_json(venue_id)),
        "paper_keys": sorted(site.papers_json(venue_id)[0]),
    }

    # The extension's timer format (Date.toUTCString()) round-trips as ISO 8601 with milliseconds.
    site.put_venue(venue_id, {"timer": "Wed, 28 Oct 2026 00:05:00 GMT"})
    obs["timer_json"] = site.venue_json(venue_id)["timer"]

    # Flat JSON, without the "venue" wrapper (Rails' wrap_parameters accepts this).
    site.api.put(f"/venues/{venue_id}.json", data={"name": "Renamed Flat"},
                 headers={"Content-Type": "application/json"})
    obs["flat_json_name"] = site.venue_json(venue_id)["name"]

    # Form-encoded, as the Rails pages' jQuery sent it; an empty timer clears it.
    site.api.put(f"/venues/{venue_id}.json", form={"venue[timer]": "", "venue[breaktime]": "true"})
    venue = site.venue_json(venue_id)
    obs["form_encoded"] = {"timer": venue["timer"], "breaktime": venue["breaktime"]}

    assert obs["timer_json"] == "2026-10-28T00:05:00.000Z"
    assert obs["flat_json_name"] == "Renamed Flat"
    assert obs["form_encoded"] == {"timer": None, "breaktime": True}
    return obs


@scenario
def admin_queue(site):
    venue_id, _ = site.create_venue()
    admin = site.open_admin(venue_id)
    attendee = site.open_attendee(venue_id)

    for pcs_id in (101, 102, 103):
        site.admin_add_paper(admin, pcs_id)
    obs = {"added": site.queue(admin)}

    site.admin_move(admin, 103, up=True)
    obs["moved_103_up"] = site.queue(admin)
    site.admin_move(admin, 101, up=False)
    obs["moved_101_down"] = site.queue(admin)
    obs["admin_controls"] = site.queue_controls(admin)

    site.admin_remove(admin, 102)
    obs["removed_102"] = site.queue(admin)
    obs["attendee_queue"] = eventually(lambda: site.queue(attendee), obs["removed_102"])
    obs["attendee_count"] = attendee.locator("#queue-count").inner_text().strip()
    obs["attendee_controls"] = site.queue_controls(attendee)
    obs["json_queue"] = site.queued_ids_json(venue_id)

    assert obs["added"] == [101, 102, 103]
    assert obs["moved_103_up"] == [101, 103, 102]
    assert obs["moved_101_down"] == [103, 101, 102]
    assert obs["admin_controls"] == [
        ["clearimg", "downimg", "removeimg"],
        ["upimg", "downimg", "removeimg"],
        ["upimg", "clearimg", "removeimg"],
    ]
    assert obs["removed_102"] == obs["json_queue"] == [103, 101]
    assert obs["attendee_count"] == "2"
    assert obs["attendee_controls"] == [[], []]
    return obs


@scenario
def allow_adds(site):
    venue_id, _ = site.create_venue(allow_adds=True)
    admin = site.open_admin(venue_id)
    attendee = site.open_attendee(venue_id)

    attendee.fill("#newPaperId", "201")
    attendee.click("#addNewPaper")
    obs = {"admin_queue_after_attendee_add": eventually(lambda: site.queue(admin), [201])}

    admin.uncheck("#allowUserAdds")
    obs["json_after_uncheck"] = eventually(lambda: site.venue_json(venue_id)["allow_adds"], False)
    attendee.reload()
    site.wait_until_loaded(attendee)
    obs["attendee_add_visible_when_disallowed"] = visible(attendee, "#newPaperId")
    admin.reload()
    site.wait_until_loaded(admin)
    obs["admin_checkbox_after_reload"] = admin.is_checked("#allowUserAdds")

    admin.check("#allowUserAdds")
    eventually(lambda: site.venue_json(venue_id)["allow_adds"], True)
    attendee.reload()
    site.wait_until_loaded(attendee)
    obs["attendee_add_visible_when_allowed"] = visible(attendee, "#newPaperId")

    assert obs["attendee_add_visible_when_disallowed"] is False
    assert obs["admin_checkbox_after_reload"] is False
    assert obs["attendee_add_visible_when_allowed"] is True
    return obs


@scenario
def extension_paper_flow(site):
    venue_id, _ = site.create_venue()
    for pcs_id in (301, 302):
        site.add_paper_api(venue_id, pcs_id)
    attendee = site.open_attendee(venue_id)

    title = "Websockets Considered Helpful"
    set_active_paper(site, venue_id, 301, title=title,
                     authors="Ada Lovelace - Analytical Engines\nGrace Hopper - Navy")
    obs = {
        "paper_info": eventually(lambda: site.paper_info(attendee), lambda info: info["title"] == [title]),
        "queue": eventually(lambda: site.queue(attendee), [302]),
        # Rails redraws the countdown on its own 1-second tick, so it can lag the paper info briefly.
        "timer_minutes": eventually(lambda: site.timer(attendee), lambda t: t.startswith("4:")).split(":")[0],
    }
    venue = site.venue_json(venue_id)
    obs["json"] = {field: venue[field] for field in ("active_paper", "paper_pcs_id", "paper_title", "paper_authors")}

    clear_active_paper(site, venue_id)
    obs["cleared_info"] = eventually(lambda: site.paper_info(attendee),
                                     lambda info: info["none"] == ["No Active Paper"])
    obs["cleared_timer"] = eventually(lambda: site.timer(attendee), "0:00")

    assert obs["paper_info"]["id"] == ["Paper #301"]
    assert obs["paper_info"]["authors"] == ["Ada Lovelace - Analytical Engines", "Grace Hopper - Navy"]
    assert obs["timer_minutes"] == "4"
    assert obs["json"]["paper_pcs_id"] == 301
    return obs


@scenario
def break_flow(site):
    venue_id, _ = site.create_venue()
    set_active_paper(site, venue_id, 601)
    admin = site.open_admin(venue_id)
    attendee = site.open_attendee(venue_id)

    admin.click("#takebreak")
    obs = {
        "attendee_break_visible": eventually(lambda: visible(attendee, "#break"), True),
        "attendee_main_visible": visible(attendee, "#main"),
        "admin_break_visible": eventually(lambda: visible(admin, "#break"), True),
        "admin_takebreak_visible": visible(admin, "#takebreak"),
        "json_breaktime": site.venue_json(venue_id)["breaktime"],
    }

    admin.click("#endbreak")
    obs["attendee_break_after_end"] = eventually(lambda: visible(attendee, "#break"), False)
    obs["attendee_info_after_end"] = eventually(lambda: site.paper_info(attendee)["none"], ["No Active Paper"])
    obs["attendee_timer_after_end"] = site.timer(attendee)
    venue = site.venue_json(venue_id)
    obs["json_after_end"] = {f: venue[f] for f in ("breaktime", "timer", "active_paper", "paper_pcs_id")}
    attendee.wait_for_timeout(1000)
    obs["attendee_sounds"] = site.plays(attendee)
    obs["admin_sounds"] = site.plays(admin)

    assert obs["attendee_break_visible"] and not obs["attendee_main_visible"]
    assert obs["json_breaktime"] is True
    assert obs["json_after_end"] == {"breaktime": False, "timer": None, "active_paper": False, "paper_pcs_id": 0}
    assert "alert.mp3" in obs["attendee_sounds"]
    return obs


@scenario
def timer_flow(site):
    venue_id, _ = site.create_venue()
    admin = site.open_admin(venue_id)
    attendee = site.open_attendee(venue_id)

    admin.fill("#timerNum", "3")
    admin.click("#setTimer")
    obs = {"after_set_3": eventually(lambda: site.timer(attendee), lambda t: t.startswith("2:5"))[:3]}

    admin.click("#clearTimer")
    obs["after_clear"] = eventually(lambda: site.timer(attendee), "0:00")

    site.put_venue(venue_id, {"timer": utc_in(-90)})
    obs["expired_prefix"] = eventually(lambda: site.timer(attendee), lambda t: t.startswith("-1:"))[:3]
    obs["expired_class"] = "expired" in (attendee.locator("#timer").get_attribute("class") or "")

    admin.click("#clearActivePaper")
    obs["after_clear_active"] = eventually(lambda: site.timer(attendee), "0:00")
    obs["expired_class_after_clear"] = "expired" in (attendee.locator("#timer").get_attribute("class") or "")

    assert obs["expired_class"] is True
    assert obs["expired_class_after_clear"] is False
    return obs


@scenario
def pcs2_links(site):
    venue_id, _ = site.create_venue(pcs2_flag=True, pcs2_venue_name="chi27a")
    for pcs_id in (401, 402):
        site.add_paper_api(venue_id, pcs_id)
    set_active_paper(site, venue_id, 401)
    attendee = site.open_attendee(venue_id)
    admin = site.open_admin(venue_id)

    def links(page):
        return {
            "queue": page.locator("#paper-queue a.paperLink[href]").evaluate_all(
                "els => els.map(e => [e.textContent.trim(), e.getAttribute('href'), e.getAttribute('target')])"),
            "active": page.locator("#paper-info .paper-id a").evaluate_all(
                "els => els.map(e => [e.textContent.trim(), e.getAttribute('href'), e.getAttribute('target')])"),
        }

    obs = {"attendee": links(attendee), "admin_chair": links(admin)}

    site.put_venue(venue_id, {"sub_committee": True, "sub_committee_id": 16})
    obs["admin_subchair"] = eventually(lambda: links(admin), lambda l: "subchair16" in str(l["queue"]))

    base = "https://new.precisionconference.com/chi27a"
    assert obs["attendee"]["queue"] == [["402", f"{base}/committee/subs/402", "_pcs_paper"]]
    assert obs["attendee"]["active"] == [["Paper #401", f"{base}/committee/subs/401", "_pcs_paper"]]
    assert obs["admin_chair"]["queue"] == [["402", f"{base}/chair/subs/402", "_pcs_paper"]]
    assert obs["admin_subchair"]["active"] == [["Paper #401", f"{base}/subchair16/subs/401", "_pcs_paper"]]
    return obs


@scenario
def ac_papers(site):
    venue_id, _ = site.create_venue()
    for pcs_id in (501, 502, 503):
        site.add_paper_api(venue_id, pcs_id)
    attendee = site.open_attendee(venue_id)

    def ac_state():
        return {
            "list": [t.strip() for t in attendee.locator("#acPaperList li").all_inner_texts()],
            "highlighted": [
                int(t.strip()) for t in attendee.locator("#paper-queue li.acPaper").all_inner_texts()
            ],
        }

    for pcs_id in ("502", "503", "502"):  # the duplicate is ignored
        attendee.fill("#acPaperId", pcs_id)
        attendee.click("#addACPaper")
    obs = {"added": eventually(ac_state, lambda s: s["highlighted"] == [502, 503])}

    attendee.reload()
    site.wait_until_loaded(attendee)
    obs["after_reload"] = eventually(ac_state, lambda s: s["highlighted"] == [502, 503])

    attendee.click("#clearAllACPapers")
    obs["after_clear"] = eventually(ac_state, {"list": [], "highlighted": []})

    assert obs["added"]["list"] == ["502", "503"]
    return obs


@scenario
def subcommittee_alert(site):
    venue_id, _ = site.create_venue(sub_committee=True)
    site.put_venue(venue_id, {"sub_committee_id": -1})
    admin = site.open_admin(venue_id)
    obs = {"shown_when_unset": visible(admin, "#scalert")}

    admin.fill("#subcommitteeInput", "abc")
    admin.click("#submitSCID")
    obs["invalid"] = {
        "value": admin.input_value("#subcommitteeInput"),
        "placeholder": admin.get_attribute("#subcommitteeInput", "placeholder"),
        "shown": visible(admin, "#scalert"),
    }

    admin.fill("#subcommitteeInput", "7")
    admin.click("#submitSCID")
    obs["shown_after_submit"] = eventually(lambda: visible(admin, "#scalert"), False)
    obs["json_sub_committee_id"] = eventually(lambda: site.venue_json(venue_id)["sub_committee_id"], 7)

    assert obs["shown_when_unset"] is True
    assert obs["invalid"] == {"value": "", "placeholder": "Must be a number", "shown": True}
    return obs


@scenario
def sounds(site):
    venue_id, _ = site.create_venue()
    attendee = site.open_attendee(venue_id)

    set_active_paper(site, venue_id, 701, title="First")
    eventually(lambda: site.paper_info(attendee)["title"], ["First"])
    obs = {"first_paper": site.plays(attendee)}

    set_active_paper(site, venue_id, 702, title="Second")
    eventually(lambda: site.paper_info(attendee)["title"], ["Second"])
    obs["switched_paper"] = site.plays(attendee)

    clear_active_paper(site, venue_id)
    eventually(lambda: site.paper_info(attendee)["none"], ["No Active Paper"])
    obs["cleared_paper"] = site.plays(attendee)

    site.put_venue(venue_id, {"timer": utc_in(8)})
    obs["timer_expired"] = eventually(lambda: site.plays(attendee), lambda p: "roar1.m4a" in p, timeout=20)

    assert obs["first_paper"] == []
    assert obs["switched_paper"] == ["alert.mp3"]
    assert obs["cleared_paper"] == ["alert.mp3", "alert.mp3"]
    assert obs["timer_expired"] == ["alert.mp3", "alert.mp3", "roar1.m4a"]
    return obs


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("target", list(TARGETS))
@pytest.mark.parametrize("name", list(SCENARIOS))
def test_scenario(name, target, sites, observations):
    observations[(name, target)] = SCENARIOS[name](sites[target])


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_parity(name, observations):
    if len(TARGETS) < 2:
        pytest.skip("parity needs two targets in PCSCHAIR_TARGETS")
    results = {target: observations.get((name, target)) for target in TARGETS}
    missing = [target for target, obs in results.items() if obs is None]
    if missing:
        pytest.fail(f"scenario failed on {', '.join(missing)}; see test_scenario")
    reference_target, reference = next(iter(results.items()))
    for target, obs in results.items():
        assert obs == reference, f"{target} differs from {reference_target}"
