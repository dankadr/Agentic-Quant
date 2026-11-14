"""Browser regressions for deliberate, interruptible dashboard motion."""

import pytest

from tests.e2e.test_dashboard_ui import login

pytestmark = pytest.mark.e2e


def test_kpi_change_feedback_survives_the_render(new_page, demo):  # type: ignore[no-untyped-def]
    page = new_page()
    login(page, demo)
    result = page.evaluate("""async () => {
      const { VIEWS } = await import('/dashboard/views.js');
      const root = document.createElement('div'); document.body.append(root);
      const update = VIEWS.overview.mount(root, {actions:{}, explicit:false});
      update({report:{equity:{end:'100',pnl:'1'}}});
      update({report:{equity:{end:'101',pnl:'2'}}});
      return root.querySelector('.kpi .value').classList.contains('flash-up');
    }""")
    assert result


def test_routes_and_dates_do_not_replay_panel_entrances(new_page, demo):  # type: ignore[no-untyped-def]
    page = new_page()
    login(page, demo)
    page.click("#nav a[data-route=history]")
    page.wait_for_function("() => document.querySelector('#view')?.dataset.route === 'history'")
    assert page.locator("#view .enter").count() == 0
    page.fill("#date", "2026-10-16")
    page.locator("#date").dispatch_event("change")
    page.wait_for_function(
        "() => document.querySelector('#view').getAttribute('aria-busy') === 'false'"
    )
    assert page.locator("#view .enter").count() == 0


def test_reduced_motion_press_keeps_buttons_still(new_page, demo):  # type: ignore[no-untyped-def]
    page = new_page(reduced_motion="reduce")
    login(page, demo)
    button = page.locator("#theme")
    button.hover()
    page.mouse.down()
    try:
        assert button.evaluate("(el) => getComputedStyle(el).transform") == "none"
    finally:
        page.mouse.up()


def test_dialog_closes_on_its_own_transition_and_restores_focus(new_page, demo):  # type: ignore[no-untyped-def]
    page = new_page()
    login(page, demo)
    page.click("#pause")
    page.wait_for_selector("dialog[open]")
    # Slow the actual transition: a fixed timeout must not close the dialog early.
    page.locator("dialog").evaluate("el => el.style.transitionDuration = '400ms'")
    page.wait_for_timeout(450)
    page.click("#dialog-cancel")
    page.wait_for_timeout(230)
    assert page.locator("dialog").evaluate("el => el.open")
    page.wait_for_function("() => !document.querySelector('dialog').open")
    assert page.evaluate("document.activeElement.id") == "pause"


def test_macro_meter_survives_changed_run_details_and_retargets(new_page, demo):  # type: ignore[no-untyped-def]
    page = new_page()
    login(page, demo)
    result = page.evaluate("""async () => {
      const { VIEWS } = await import('/dashboard/views.js');
      const { agentState } = await import('/dashboard/agentpanel.js');
      agentState.selected = 'macro';
      const root = document.createElement('div'); document.body.append(root);
      const update = VIEWS.agents.mount(root, {actions:{}});
      const run = {id:'same-run',agent_name:'macro',status:'COMPLETED',model:'test',prompt_version:'v1',started_at:'2026-10-19T14:00:00Z',result:{regime:'RANGING',risk_multiplier:'0.7'}};
      update({runs:{items:[run]}});
      const original = root.querySelector('.meter');
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      run.result.confidence = 0.8;
      update({runs:{items:[run]}});
      const unchanged = original === root.querySelector('.meter');
      run.result.risk_multiplier = '0.9';
      update({runs:{items:[run]}});
      const changed = root.querySelector('.meter');
      return {unchanged, sameAfterChange:original===changed, value:changed.getAttribute('aria-valuenow'), transform:changed.firstChild.style.transform, duration:getComputedStyle(changed.firstChild).transitionDuration};
    }""")
    assert result == {
        "unchanged": True,
        "sameAfterChange": True,
        "value": "0.9",
        "transform": "scaleX(0.9)",
        "duration": "0.2s",
    }


def test_dialog_entry_has_a_starting_transform_and_reduced_motion_skips_it(new_page, demo):  # type: ignore[no-untyped-def]
    for reduced in (False, True):
        page = new_page(reduced_motion="reduce" if reduced else "no-preference")
        login(page, demo)
        transforms = page.evaluate("""async () => {
          document.querySelector('#pause').click();
          await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
          return document.querySelector('dialog').getAnimations().flatMap(a => a.effect.getKeyframes().map(f => f.transform)).filter(Boolean);
        }""")
        if reduced:
            assert not transforms or all(t == "none" for t in transforms)
            page.keyboard.press("Escape")
            assert not page.locator("dialog").evaluate("el => el.open")
        else:
            assert any("0.96" in t for t in transforms)
            page.keyboard.press("Escape")
            page.wait_for_function("() => !document.querySelector('dialog').open")
