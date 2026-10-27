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
    assert page.evaluate("document.activeElement && document.activeElement.id") == "cfg-run_every_minutes"
    assert page.input_value("#cfg-run_every_minutes") == "20"
    assert "Review change (1)" in page.inner_text("#view")
