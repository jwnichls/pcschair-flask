"""Configuration for the cross-implementation browser tests.

Targets come from PCSCHAIR_TARGETS, a comma-separated list of name=url pairs:

    PCSCHAIR_TARGETS="flask=http://localhost:3000,rails=http://localhost:3001"

Every scenario runs against every target. When two or more targets are given,
test_parity additionally checks that each scenario observed identical results.
"""

import os

import pytest
from playwright.sync_api import sync_playwright

from parity.site import Site


def parse_targets():
    raw = os.environ.get("PCSCHAIR_TARGETS", "flask=http://localhost:3000")
    targets = {}
    for entry in filter(None, (part.strip() for part in raw.split(","))):
        name, _, url = entry.partition("=")
        targets[name.strip()] = url.strip()
    return targets


TARGETS = parse_targets()


@pytest.fixture(scope="session")
def playwright():
    with sync_playwright() as p:
        yield p


@pytest.fixture(scope="session")
def browser(playwright):
    browser = playwright.chromium.launch(
        headless=os.environ.get("HEADED") is None,
        args=["--autoplay-policy=no-user-gesture-required"],
    )
    yield browser
    browser.close()


@pytest.fixture(scope="session")
def sites(browser, playwright):
    return {name: Site(name, url, browser, playwright) for name, url in TARGETS.items()}


@pytest.fixture(scope="session")
def observations():
    """(scenario name, target name) -> what that scenario observed on that target."""
    return {}
