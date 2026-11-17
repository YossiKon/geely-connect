"""Command context and keyboard regressions in Chromium, with fake HA services."""
from contextlib import contextmanager

from run import skip
from test_cards_in_a_browser import _evaluate, _load, _two_more_cars


def test_a_settled_temperature_rechecks_driving_and_returns_to_the_cars_number():
    got = _evaluate("""() => new Promise(done => {
      const el = document.createElement('geely-card');
      document.body.appendChild(el); el.setConfig({});
      const hass = window.mkHass({}); el.hass = hass;
      el._onAction('tempup');
      hass.states['sensor.car_speed'].state = '40'; el.hass = hass;
      setTimeout(() => done({calls: hass.serviceCalls,
        shown: el.shadowRoot.querySelector('.tval').textContent}), 1400);
    })""")
    assert got == {"calls": [], "shown": "22°"}, got


def test_reconfiguring_the_card_does_not_send_a_pending_temperature_to_another_car():
    got = _evaluate("""() => new Promise(done => {
      const el = document.createElement('geely-card');
      document.body.appendChild(el); el.setConfig({prefix: 'car'});
      const hass = window.mkHass({});
    """ + _two_more_cars() + """
      el.hass = hass; el._onAction('tempup'); el._onAction('unlock');
      el.setConfig({prefix: 'fleet_b'}); el._onAction('unlock');
      setTimeout(() => done(hass.serviceCalls), 1400);
    })""")
    assert got == [], got


def test_busy_retries_recheck_driving_and_reconfiguration():
    for change in ("hass.states['sensor.car_speed'].state = '40'; el.hass = hass;",
                   "el.setConfig({prefix: 'fleet_b', cooldown: 0.05});"):
        got = _evaluate("""() => new Promise(done => {
          const el = document.createElement('geely-card');
          document.body.appendChild(el); el.setConfig({prefix:'car', cooldown:0.05});
          const hass = window.mkHass({});
        """ + _two_more_cars() + """
          let calls = [];
          hass.callService = (d, s, data) => {
            calls.push([d, s, data.entity_id]);
            return calls.length === 1 ? Promise.reject({code:8070}) : Promise.resolve();
          };
          el.hass = hass; el._onAction('unlock'); el._onAction('unlock');
          """ + change + """
          setTimeout(() => done(calls), 250);
        })""")
        assert got == [["lock", "unlock", "lock.car_doors"]], got


def test_closing_the_popup_still_sends_the_temperature_the_owner_dialled():
    got = _evaluate("""() => new Promise(done => {
      const el = document.createElement('geely-card');
      document.body.appendChild(el); el.setConfig({});
      const hass = window.mkHass({}); el.hass = hass;
      el._onAction('tempup'); el.remove();
      setTimeout(() => done(hass.serviceCalls.map(c => c[2].temperature)), 1400);
    })""")
    assert got == [22.5], got


@contextmanager
def _keyboard_page():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        skip("playwright not installed")
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception as err:
            skip(f"chromium unavailable: {str(err)[:60]}")
        try:
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda err: errors.append(str(err)))
            _load(page, False)
            page.wait_for_function("() => !!customElements.get('geely-card')")
            yield page
            assert not errors, errors
        finally:
            browser.close()


def test_unlock_can_be_confirmed_with_two_keyboard_presses():
    with _keyboard_page() as page:
        page.evaluate("""() => {
          window.el = document.createElement('geely-card');
          document.body.appendChild(el); el.setConfig({});
          window.hass = window.mkHass({}); el.hass = hass;
        }""")
        unlock = page.locator("geely-card [data-act='unlock']").first
        unlock.focus()
        page.keyboard.press("Enter")
        assert page.evaluate("hass.serviceCalls.length") == 0
        page.keyboard.press("Enter")
        assert page.evaluate("hass.serviceCalls.map(c => [c[0],c[1],c[2].entity_id])") == [
            ["lock", "unlock", "lock.car_doors"]]


def test_the_windows_chip_opens_its_menu_with_enter_or_space():
    for key in ("Enter", "Space"):
        with _keyboard_page() as page:
            page.evaluate("""() => {
              const el = document.createElement('geely-card-compact');
              document.body.appendChild(el); el.setConfig({});
              el.hass = window.mkHass({covers:{windows:'open', windowsPos:50}});
            }""")
            page.locator("geely-card-compact [data-act='windows_menu']").focus()
            page.keyboard.press(key)
            assert page.locator("dialog[open] .win-menu").count() == 1


def test_a_request_that_never_settles_does_not_leave_the_buttons_disabled():
    got = _evaluate("""() => new Promise(done => {
      const el = document.createElement('geely-card');
      document.body.appendChild(el); el.setConfig({cooldown:0.05});
      const hass = window.mkHass({}); let calls = 0;
      hass.callService = () => { calls++; return new Promise(() => {}); };
      el.hass = hass;
      const find = () => el.shadowRoot.querySelector('[data-act="find"]');
      find().click(); const held = find().disabled;
      setTimeout(() => {
        const released = !find().disabled; find().click();
        done({held, released, calls});
      }, 120);
    })""")
    assert got == {"held": True, "released": True, "calls": 2}, got
