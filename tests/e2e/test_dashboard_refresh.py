"""Refreshes must not commit obsolete routes or disguise stale health."""

import pytest

from tests.e2e.test_dashboard_ui import login

pytestmark = pytest.mark.e2e


def hold_overview_report(page):  # type: ignore[no-untyped-def]
    held = []
    page.route(
        "**/v1/report/daily?*", lambda route: route.continue_() if held else held.append(route)
    )
    with page.expect_request("**/v1/report/daily?*"):
        page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    page.wait_for_timeout(100)
    assert held
    return held


def test_navigation_during_refresh_discards_old_route_and_loads_new(new_page, demo):  # type: ignore[no-untyped-def]
    page = new_page()
    login(page, demo)
    page.wait_for_function(
        "() => document.querySelector('#view').getAttribute('aria-busy') === 'false'"
    )
    held = hold_overview_report(page)
    page.click("#nav a[data-route=history]")
    held[0].continue_()
    page.wait_for_function(
        "() => document.querySelector('#view').dataset.route === 'history' && document.querySelector('#view').getAttribute('aria-busy') === 'false'",
        timeout=5000,
    )
    assert page.locator("#nav a[aria-current=page]").get_attribute("data-route") == "history"


def test_date_change_during_refresh_loads_the_selected_session(new_page, demo):  # type: ignore[no-untyped-def]
    page = new_page()
    login(page, demo)
    page.wait_for_function(
        "() => document.querySelector('#view').getAttribute('aria-busy') === 'false'"
    )
    held = hold_overview_report(page)
    page.fill("#date", "2026-10-16")
    page.locator("#date").dispatch_event("change")
    with page.expect_request("**/v1/report/daily?date=2026-10-16", timeout=5000):
        held[0].continue_()
    page.wait_for_function(
        "() => document.querySelector('#view').getAttribute('aria-busy') === 'false'"
    )
    assert page.locator("#date").input_value() == "2026-10-16"


def test_failed_health_refresh_is_prominent_and_retry_clears_stale_state(new_page, demo):  # type: ignore[no-untyped-def]
    page = new_page()
    login(page, demo)
    page.wait_for_function(
        "() => document.querySelector('#view').getAttribute('aria-busy') === 'false'"
    )
    page.route(
        "**/v1/system/status",
        lambda route: route.fulfill(status=503, json={"detail": "temporary outage"}),
    )
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    page.wait_for_selector("#refresh-error[role=alert]:not([hidden])", timeout=5000)
    assert page.locator("#view").get_attribute("data-stale") == "true"
    assert "stale" in page.locator("#refresh-error").inner_text().lower()
    page.unroute("**/v1/system/status")
    page.locator("#refresh-error button").click()
    page.wait_for_selector("#refresh-error[hidden]", state="attached")
    assert page.locator("#view").get_attribute("data-stale") == "false"
