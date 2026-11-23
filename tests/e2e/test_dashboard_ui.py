import os
from pathlib import Path

import pytest

from tests.e2e.conftest import Demo

pytestmark = pytest.mark.e2e
ROUTES = [
    "overview",
    "trading",
    "transactions",
    "history",
    "strategies",
    "agents",
    "research",
    "data",
    "activity",
]


def login(page, demo: Demo, key: str | None = None, route: str = "overview") -> None:  # type: ignore[no-untyped-def]
    page.goto(f"{demo.url}/dashboard#/{route}")
    page.fill("#key", key or demo.viewer_key)
    page.click("#login-form button[type=submit]")
    page.wait_for_selector("#view .card", timeout=15000)


def test_a_bad_key_is_rejected_and_nothing_is_stored_in_local_storage(new_page, demo: Demo) -> None:  # type: ignore[no-untyped-def]
    page = new_page()
    page.goto(f"{demo.url}/dashboard")
    page.fill("#key", "trk_wrong")
    page.click("#login-form button[type=submit]")
    page.wait_for_selector("#login-error:not(:empty)", timeout=10000)
    assert "rejected" in page.inner_text("#login-error")
    assert page.evaluate("sessionStorage.getItem('dashboard_api_key')") is None
    login(page, demo)
    assert (
        page.evaluate("localStorage.getItem('dashboard_api_key')") is None
    )  # the key never reaches localStorage
    assert page.evaluate("sessionStorage.getItem('dashboard_api_key')") is not None


def test_every_view_renders_without_console_errors_or_csp_violations(new_page, demo: Demo) -> None:  # type: ignore[no-untyped-def]
    page = new_page()
    login(page, demo)
    for route in ROUTES:
        page.click(f"#nav a[data-route={route}]")
        page.wait_for_function(
            "(r) => document.querySelector('#nav a[aria-current=page]')?.dataset.route === r",
            arg=route,
        )
        page.wait_for_selector("#view .card", timeout=10000)
        # A mounted card exists before the route's asynchronous fetch has populated it.
        page.wait_for_function(
            "(route) => { const v = document.querySelector('#view'); return v?.dataset.route === route && v.getAttribute('aria-busy') === 'false' && v.innerText.trim().length > 0; }",
            arg=route,
        )
        assert page.inner_text("#view").strip(), route
    bad = [p for p in page.problems if "503" not in p]  # no portfolio snapshot is an expected 503
    assert bad == []


def test_nothing_leaves_the_origin(new_page, demo: Demo) -> None:  # type: ignore[no-untyped-def]
    page = new_page()
    login(page, demo)
    page.click("#nav a[data-route=agents]")
    page.wait_for_selector("details.run")
    external = [
        u for u in page.requests if not u.startswith(demo.url) and not u.startswith("data:")
    ]
    assert external == []


def test_untrusted_agent_text_is_rendered_as_text_never_as_html(new_page, demo: Demo) -> None:  # type: ignore[no-untyped-def]
    page = new_page()
    login(page, demo)
    for route in ("overview", "agents", "research"):
        page.click(f"#nav a[data-route={route}]")
        page.wait_for_selector("#view .card")
        if route == "agents":
            for d in page.query_selector_all("details.run"):
                d.evaluate("(n) => n.setAttribute('open', '')")
        page.wait_for_timeout(300)
        assert page.evaluate("window.__pwned") is None, route
        assert page.query_selector("#view img, #view script") is None, route
    page.click("#nav a[data-route=agents]")
    page.wait_for_selector("details.run")
    assert "<img src=x" in page.inner_text("#view")  # visible as literal text


def test_the_overview_shows_alerts_kpis_and_an_equity_chart_with_keyboard_inspection(
    new_page, demo: Demo
) -> None:  # type: ignore[no-untyped-def]
    page = new_page()
    login(page, demo)
    page.wait_for_selector(".chart svg")
    assert page.query_selector(".kpi .value") is not None
    page.focus(".chart svg")
    page.keyboard.press("ArrowLeft")
    page.wait_for_selector(".chart.active .tip")
    first = page.inner_text(".chart .tip")
    page.keyboard.press("ArrowLeft")
    assert page.inner_text(".chart .tip") != first
    assert "vs open" in first
    page.keyboard.press("Home")
    assert "($0.00 vs open)" in page.inner_text(
        ".chart .tip"
    )  # the first point is the session baseline


def test_theme_toggle_switches_and_persists(new_page, demo: Demo) -> None:  # type: ignore[no-untyped-def]
    page = new_page(color_scheme="dark")
    login(page, demo)
    assert page.get_attribute("html", "data-theme") == "dark"
    page.click("#theme")
    assert page.get_attribute("html", "data-theme") == "light"
    page.reload()
    page.wait_for_selector("#view .card")
    assert page.get_attribute("html", "data-theme") == "light"


def test_the_layout_never_scrolls_sideways_on_a_phone(new_page, demo: Demo) -> None:  # type: ignore[no-untyped-def]
    page = new_page(viewport={"width": 375, "height": 800})
    login(page, demo)
    for route in ROUTES:
        page.evaluate(f"location.hash = '#/{route}'")
        page.wait_for_function(
            "(r) => document.querySelector('#nav a[aria-current=page]')?.dataset.route === r",
            arg=route,
        )
        page.wait_for_selector("#view .card")
        page.wait_for_timeout(250)
        overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
        assert overflow <= 1, f"{route} overflows by {overflow}px"


@pytest.mark.parametrize("demo", [True, False], indirect=True, ids=["enabled", "disabled"])
def test_paper_shorts_on_a_phone(new_page, demo: Demo, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    from playwright.sync_api import expect

    color = "light" if demo.allow_short else "dark"
    page = new_page(viewport={"width": 375, "height": 800}, color_scheme=color, reduced_motion="reduce")
    login(page, demo)

    overview = page.locator(".position-summary")
    state = "enabled" if demo.allow_short else "disabled"
    expect(overview.get_by_text(f"Paper shorts {state}", exact=True)).to_be_visible()
    expect(overview.get_by_text("Borrow fees are omitted.", exact=False)).to_be_visible()
    for ticker, direction, size, pnl in (
        ("QQQ", "SHORT", "2", "-$8.00"),
        ("SPY", "LONG", "1", "+$2.00"),
    ):
        row = overview.get_by_role("row").filter(has_text=ticker)
        for value in (direction, size, "$100.00", pnl):
            expect(row.get_by_text(value, exact=True)).to_be_visible()
    for label in ("Short liability", "Short collateral", "Restricted proceeds"):
        expect(overview.get_by_text(label, exact=True)).to_be_visible()
    expect(overview.get_by_text("$208.00", exact=True)).to_have_count(2)
    expect(overview.get_by_text("$200.00", exact=True)).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth") <= 376
    assert overview.locator(".tablewrap").evaluate("(e) => e.scrollWidth <= e.clientWidth + 1")
    captures = Path(os.environ.get("PAPER_SHORT_SCREENSHOT_DIR", str(tmp_path)))
    captures.mkdir(parents=True, exist_ok=True)
    overview.screenshot(path=captures / f"paper-shorts-mobile-{state}.png", animations="disabled")

    page.click("#nav a[data-route=trading]")
    for table_name, actions in (
        ("Orders", ["SELL to open", "BUY to cover", "BUY to open", "SELL to close"]),
        ("Signals", ["SELL to open"]),
    ):
        rows = page.get_by_role("table", name=table_name, exact=True)
        for action in actions:
            expect(rows.get_by_text(action, exact=True).first).to_be_visible()
    page.click("#nav a[data-route=transactions]")
    round_trips = page.get_by_role("table", name="Round trips", exact=True)
    closed_short = round_trips.get_by_role("row").filter(has_text="AAPL")
    for value in ("SHORT", "CLOSED", "SELL to open", "BUY to cover"):
        expect(closed_short.get_by_text(value, exact=True)).to_be_visible()
    expect(closed_short.get_by_text("-$4.00")).to_be_visible()
    fills = page.get_by_role("table", name="Fills", exact=True)
    for action in ("SELL to open", "BUY to cover"):
        expect(fills.get_by_text(action, exact=True).first).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth") <= 376
    assert page.problems == []


def test_reduced_motion_never_hides_content(new_page, demo: Demo) -> None:  # type: ignore[no-untyped-def]
    page = new_page(reduced_motion="reduce")
    login(page, demo)
    page.wait_for_timeout(200)
    opacities = page.evaluate(
        "[...document.querySelectorAll('#view .grid > *')].map(e => getComputedStyle(e).opacity)"
    )
    assert opacities and all(o == "1" for o in opacities)
    assert (
        page.evaluate("getComputedStyle(document.querySelector('.badge.live')).animationName")
        == "none"
    )


def test_accessibility_basics(new_page, demo: Demo) -> None:  # type: ignore[no-untyped-def]
    page = new_page()
    login(page, demo)
    assert page.query_selector("a.skip") is not None
    page.keyboard.press("Tab")
    assert page.evaluate("document.activeElement.classList.contains('skip')")
    assert page.query_selector("main#view, #view[tabindex]") is not None
    for route in ROUTES:
        page.evaluate(f"location.hash = '#/{route}'")
        page.wait_for_selector("#view .card")
        page.wait_for_timeout(200)
        missing = page.evaluate(
            "[...document.querySelectorAll('#view table')].filter(t => !t.querySelector('caption')).length"
        )
        assert missing == 0, route
        unnamed = page.evaluate(
            "[...document.querySelectorAll('button')].filter(b => !(b.textContent.trim() || b.getAttribute('aria-label') || b.title)).length"
        )
        assert unnamed == 0, route
        assert page.evaluate("document.querySelectorAll('h1,h2').length") > 0
    assert page.get_attribute("html", "lang") == "en"


def test_pause_needs_an_operator_and_a_confirmation(new_page, demo: Demo) -> None:  # type: ignore[no-untyped-def]
    # status reports PAPER_RUNNING only if the seeded system state is running; set it directly
    page = new_page()
    login(page, demo, demo.viewer_key)
    page.wait_for_selector("#badges .badge")
    if page.is_hidden("#pause"):
        pytest.skip("system is not in PAPER_RUNNING in this fixture")
    page.click("#pause")
    page.wait_for_selector("dialog[open]")
    page.click("#dialog-cancel")
    page.wait_for_selector("dialog:not([open])", state="attached")
    page.click("#pause")
    page.wait_for_selector("dialog[open]")
    page.click("#dialog-ok")
    page.wait_for_selector(".toast.bad", timeout=10000)
    assert "operator" in page.inner_text(".toast.bad")


def test_a_second_operator_can_approve_a_pending_agent_proposal(new_page, demo: Demo) -> None:  # type: ignore[no-untyped-def]
    page = new_page()
    login(page, demo, demo.reviewer_key, "research")
    page.wait_for_selector("#view button:has-text('Approve')")
    assert "target_r" in page.inner_text("#view")
    assert "2.0" in page.inner_text("#view")
    page.click("#view button:has-text('Approve')")
    page.wait_for_selector("dialog[open]")
    page.fill("dialog textarea", "looks right")
    page.click("#dialog-ok")
    page.wait_for_selector(".toast:not(.bad)", timeout=10000)
    page.wait_for_selector(".chip:has-text('APPROVED')", timeout=20000)
    assert page.query_selector("#view button:has-text('Approve')") is None


def test_picking_a_weekend_date_shows_empty_states_without_errors(new_page, demo: Demo) -> None:  # type: ignore[no-untyped-def]
    page = new_page()
    login(page, demo, route="data")
    page.fill("#date", "2026-10-17")
    page.dispatch_event("#date", "change")
    page.wait_for_selector("text=no session on this date", timeout=10000)
    assert [p for p in page.problems if "503" not in p] == []
    page.click("#live")
    page.wait_for_selector("table.heat", timeout=10000)


def test_transactions_show_round_trips_fills_and_the_cash_ledger(new_page, demo: Demo) -> None:  # type: ignore[no-untyped-def]
    page = new_page()
    login(page, demo, route="transactions")
    page.wait_for_selector("text=Round trips")
    page.wait_for_selector("#view table")
    text = page.inner_text("#view")
    assert "orb_v1" in text and "SPY" in text
    assert "+$2.98" in text  # 3.00 gross less two 1-cent fees
    assert "1 trade" in text and "100% winners" in text
    assert "103" in text and "100" in text  # entry and exit prices
    page.wait_for_selector("table caption:has-text('Fills')", state="attached")
    assert (
        page.query_selector(
            "table caption:has-text('Cash ledger')",
        )
        is not None
    )
    assert "DEPOSIT" in text or "FILL" in text  # cash movements are listed


def test_history_shows_daily_pnl_bars_and_a_sessions_table(new_page, demo: Demo) -> None:  # type: ignore[no-untyped-def]
    page = new_page()
    login(page, demo, route="history")
    page.wait_for_selector(".chart svg rect.bar")
    assert page.locator(".chart svg rect.bar").count() >= 3
    text = page.inner_text("#view")
    assert "+$3.50" in text and "-$2.30" in text
    assert (
        page.query_selector("rect.bar.up") is not None
        and page.query_selector("rect.bar.down") is not None
    )


def test_typing_in_an_agent_setting_survives_a_background_refresh(new_page, demo: Demo) -> None:  # type: ignore[no-untyped-def]
    page = new_page()
    login(page, demo, route="agents")
    page.wait_for_selector("#cfg-run_every_minutes")
    field = page.locator("#cfg-run_every_minutes")
    field.click()
    field.fill("20")
    page.dispatch_event("body", "keydown")  # no-op; the refresh below is what matters
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")  # forces a refresh
    page.wait_for_timeout(1500)
    assert (
        page.evaluate("document.activeElement && document.activeElement.id")
        == "cfg-run_every_minutes"
    )
    assert page.input_value("#cfg-run_every_minutes") == "20"
    assert "Review change (1)" in page.inner_text("#view")


def test_overview_uses_selected_session_values_and_honest_errors(new_page, demo: Demo) -> None:
    page = new_page(color_scheme="dark")
    report = {
        "equity": {
            "end": "812.34",
            "pnl": "-7.66",
            "realized_pnl_change": "-2.00",
            "unrealized_pnl_change": "-5.66",
        },
        "benchmark": {},
        "costs": {"fees": "0.12", "slippage_estimate": "0.03"},
        "strategies": {},
        "diagnostic_counts": {
            "sampled_evaluations": 4,
            "by_strategy": {"orb_v1": {"NO_BREAKOUT": 4}},
        },
    }
    page.route("**/v1/report/daily?*", lambda route: route.fulfill(json=report))
    login(page, demo)
    page.wait_for_function("document.querySelector('#view').getAttribute('aria-busy') === 'false'")
    assert page.locator(".kpi").nth(0).inner_text().find("$812.34") >= 0
    assert "-$2.00" in page.locator(".kpi").nth(4).inner_text()
    assert "NO_BREAKOUT" in page.inner_text("#view")
    assert page.locator("#view .grid > .card").first.inner_text().lower().startswith("attention")
    page.fill("#date", "2026-10-16")
    page.dispatch_event("#date", "change")
    page.wait_for_function("document.querySelector('#view').getAttribute('aria-busy') === 'false'")
    page.wait_for_selector("text=unavailable for historical date")
    assert "unavailable for historical date" in page.locator(".kpi").nth(3).inner_text()
    page.route(
        "**/v1/report/daily?*", lambda route: route.fulfill(status=503, json={"detail": "offline"})
    )
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    page.wait_for_selector("text=Report unavailable")
    assert "All clear" not in page.inner_text("#view")
    assert page.get_by_role("button", name="Retry unavailable sections").count() == 1


def test_equity_chart_has_table_and_pointer_is_silent_after_keyboard(new_page, demo: Demo) -> None:
    page = new_page()
    login(page, demo)
    page.wait_for_selector(".chart svg")
    assert page.get_by_role("table", name="Session equity points", include_hidden=True).count() == 1
    page.focus(".chart svg")
    page.keyboard.press("ArrowLeft")
    assert page.locator(".chart .chart-announcement").inner_text()
    previous = page.locator(".chart .chart-announcement").inner_text()
    page.locator(".chart svg").hover(position={"x": 100, "y": 50})
    assert page.locator(".chart .chart-announcement").inner_text() == previous
    assert page.locator(".chart .tip").get_attribute("role") is None


def test_overview_lists_all_agents_and_retains_mobile_kpi_hierarchy(new_page, demo: Demo) -> None:
    page = new_page(color_scheme="dark")
    login(page, demo)
    page.wait_for_function("document.querySelector('#view').getAttribute('aria-busy') === 'false'")
    for name in (
        "macro",
        "fundamental",
        "technical",
        "consensus",
        "evaluator",
        "data_quality",
        "execution_quality",
        "opportunities",
    ):
        assert (
            name.replace("_", " ")
            in page.get_by_role("table", name="Current agent health and latest runs").inner_text()
        )
    assert any("latest_per_agent=true" in url for url in page.requests)
    assert page.get_by_role("heading", name="Trading operations").count() == 1
    assert page.locator("#nav").bounding_box()["width"] == 220
    page.screenshot(path="/tmp/dashboard-data-desktop.png", full_page=True)  # noqa: S108 - seeded visual QA
    page.set_viewport_size({"width": 375, "height": 800})
    assert page.evaluate("getComputedStyle(document.querySelector('.kpi')).gridColumn") == "span 6"
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    page.screenshot(path="/tmp/dashboard-data-mobile.png", full_page=True)  # noqa: S108 - seeded visual QA
    page.click("#theme")
    page.screenshot(path="/tmp/dashboard-redesign-mobile-light.png", full_page=True)  # noqa: S108 - seeded visual QA
    page.set_viewport_size({"width": 1280, "height": 900})
    page.screenshot(path="/tmp/dashboard-redesign-desktop-light.png", full_page=True)  # noqa: S108 - seeded visual QA


@pytest.mark.parametrize("name", ["data_quality", "execution_quality", "opportunities"])
def test_advisory_run_results_are_readable_safe_text(new_page, demo: Demo, name: str) -> None:
    page = new_page()
    hostile = "<img src=x onerror=window.__pwned=1>"
    run = {
        "agent_name": name,
        "status": "COMPLETED",
        "model": "deterministic",
        "prompt_version": "v1",
        "started_at": "2026-10-19T14:00:00Z",
        "result": {
            "conclusion": "Review complete",
            "coverage": {"SPY": {"expected": 30, "bars": 28}},
            "evidence": [hostile],
            "limitations": ["Configured universe only"],
        },
    }
    page.route("**/v1/agents/runs?*", lambda route: route.fulfill(json={"items": [run]}))
    login(page, demo, route="agents")
    page.get_by_role("button", name=name, exact=True).click()
    page.wait_for_selector("text=Review complete")
    page.locator("details.run").evaluate_all("nodes => nodes.forEach(n => n.open = true)")
    assert hostile in page.inner_text("#view")
    assert "Configured universe only" in page.inner_text("#view")
    assert page.query_selector("#view img") is None
    assert page.evaluate("window.__pwned") is None


def test_attention_includes_enabled_agent_failures(new_page, demo: Demo) -> None:
    page = new_page()
    page.route(
        "**/v1/agents",
        lambda route: route.fulfill(
            json={
                "items": [
                    {
                        "name": "data_quality",
                        "enabled": True,
                        "health": "failing",
                        "schedule_state": "overdue",
                        "last_skip_reason": "no_market_data",
                    }
                ]
            }
        ),
    )
    login(page, demo)
    page.wait_for_function("document.querySelector('#view').getAttribute('aria-busy') === 'false'")
    assert "Market data incomplete" in page.locator(".attention-panel").inner_text()
    assert "data_quality needs attention" in page.locator(".attention-panel").inner_text()
    assert "no market data" in page.locator(".attention-panel").inner_text()
    assert page.locator(".attention-panel .alert.bad").filter(has_text="data_quality").count() == 1


def test_agent_scorecards_separate_unknown_quality_and_trade_contribution(
    new_page, demo: Demo
) -> None:  # type: ignore[no-untyped-def]
    page = new_page()
    login(page, demo, route="agents")
    page.wait_for_selector(".agent-scorecard", timeout=10000)
    assert page.locator(".agent-scorecard").count() == 8
    technical = page.locator('.agent-scorecard[data-agent="technical"]')
    assert "No observations" in technical.inner_text()
    assert "Not measured" in technical.inner_text()
    assert "Current version" in technical.inner_text()
    assert "Recorded-run completion" in page.inner_text("#view")
    assert "Overall score" not in page.inner_text("#view")
    page.locator(".evidence-cards").screenshot(
        path=".superpowers/sdd/2026-11-14-agent-performance/scorecards-desktop.png"
    )
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.locator(".agent-scorecard[data-agent=macro]").screenshot(
        path=".superpowers/sdd/2026-11-14-agent-performance/scorecard-mobile.png"
    )


def test_scorecard_failure_is_visible_and_retry_restores_cards(new_page, demo: Demo) -> None:  # type: ignore[no-untyped-def]
    page = new_page()

    def fail(route):  # type: ignore[no-untyped-def]
        route.fulfill(status=503, content_type="application/json", body='{"detail":"not ready"}')

    page.route("**/v1/agents/performance?*", fail)
    login(page, demo, route="agents")
    page.wait_for_selector("text=Performance evidence unavailable")
    page.unroute("**/v1/agents/performance?*", fail)
    page.get_by_role("button", name="Retry performance evidence").click()
    page.wait_for_selector(".agent-scorecard", timeout=10000)
    assert page.locator(".agent-scorecard").count() == 8


@pytest.mark.parametrize("before_open", [True, False])
def test_missing_session_data_distinguishes_premarket_from_open_market(
    new_page, demo: Demo, before_open: bool
) -> None:  # type: ignore[no-untyped-def]
    import json
    from datetime import UTC, datetime

    from playwright.sync_api import expect

    page = new_page(viewport={"width": 375, "height": 800})
    page.clock.install(time=datetime(2026, 10, 19, 12 if before_open else 15, tzinfo=UTC))

    headers = {"Authorization": "Bearer " + demo.viewer_key}
    status = page.request.get(demo.url + "/v1/system/status", headers=headers).json()
    status["session"]["is_open"] = not before_open
    report = page.request.get(demo.url + "/v1/report/daily?date=2026-10-19", headers=headers).json()
    report["ticks"] = 0
    report["equity"] = dict.fromkeys(report["equity"])
    report["benchmark"]["return_pct"] = None
    page.route("**/v1/system/status", lambda route: route.fulfill(json=status))
    page.route("**/v1/report/daily?*", lambda route: route.fulfill(json=report))
    page.route(
        "**/v1/equity?*",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps({"points": []})
        ),
    )
    login(page, demo)
    page.wait_for_function(
        "() => document.querySelector('#view').getAttribute('aria-busy') === 'false'"
    )
    cards = page.locator(".kpi")
    if before_open:
        assert "Market opens" in page.inner_text(".attention-panel")
        assert "Latest account snapshot" in cards.nth(0).inner_text()
        assert "$" in cards.nth(0).locator(".value").inner_text()
        assert cards.nth(1).locator(".value").inner_text() == "Awaiting open"
        assert "Awaiting the first market tick" in page.inner_text("#view")
        # Selecting a date must never substitute a current account value for missing history.
        page.locator("#date").evaluate(
            "(el) => { el.value = '2026-10-19'; el.dispatchEvent(new Event('change', {bubbles:true})); }"
        )
        expect(page.locator(".session-label")).to_contain_text("Selected session")
        expect(cards.nth(0).locator(".value")).to_have_text("Unavailable")
    else:
        assert cards.nth(0).locator(".value").inner_text() == "Unavailable"
        assert "Market is open but no ticks yet" in page.inner_text(".attention-panel")
        assert "Awaiting open" not in page.inner_text("#view")
    assert page.evaluate("document.documentElement.scrollWidth - innerWidth") <= 1
