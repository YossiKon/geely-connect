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


def test_a_request_that_never_settles_has_a_ceiling_above_normal_cooldown():
    with _keyboard_page() as page:
        page.clock.install()
        page.evaluate("""() => {
          window.el=document.createElement('geely-card'); document.body.append(el);
          el.setConfig({cooldown:1.5}); window.hass=mkHass({}); window.calls=0;
          hass.callService=()=>{ calls++; return new Promise(()=>{}); };
          el.hass=hass; el.shadowRoot.querySelector('[data-act="find"]').click();
        }""")
        page.clock.run_for(4000)
        assert page.locator("geely-card [data-act='find']").is_disabled()
        page.clock.run_for(26001)
        assert page.locator("geely-card [data-act='find']").is_enabled()
        page.locator("geely-card [data-act='find']").click()
        assert page.evaluate('calls') == 2


def test_slow_round_trips_hold_controls_through_rerenders_and_settlement():
    for outcome in ('success', 'busy', 'failure'):
        with _keyboard_page() as page:
            page.clock.install()
            page.evaluate("""() => {
              window.el=document.createElement('geely-card'); document.body.append(el);
              el.setConfig({cooldown:1.5}); window.hass=mkHass({}); window.calls=[];
              hass.callService=(d,s)=>{ calls.push(s); return new Promise((resolve,reject)=>{
                window.settle=resolve; window.refuse=reject;
              }); };
              el.hass=hass; el._call("lock","lock","doors").catch(()=>{});
            }""")
            page.clock.run_for(2000)
            page.evaluate("hass.states['sensor.car_battery'].state='62'; el.hass={...hass};")
            assert page.locator("geely-card [data-act='find']").is_disabled()
            page.locator("geely-card [data-act='find']").evaluate('(b)=>b.click()')
            assert page.evaluate('calls') == ['lock']
            page.clock.run_for(500)
            if outcome == 'success':
                page.evaluate('settle()')
            else:
                page.evaluate("refuse({code:8070})" if outcome == 'busy' else "refuse({message:'rejected'})")
            if outcome == 'busy':
                page.clock.run_for(3001)
                assert page.evaluate('calls') == ['lock', 'lock']
            else:
                assert page.locator("geely-card [data-act='find']").is_disabled()
                page.clock.run_for(1501)
                assert page.locator("geely-card [data-act='find']").is_enabled()


def test_same_vehicle_config_preserves_dial_confirmation_and_busy_retry():
    for changed in ({'prefix':'car','cooldown':0.1},
                    {'prefix':'car','title':'New title','cooldown':0.1},
                    {'prefix':'shortened','cooldown':0.1}):
        for action in ('dial', 'arm', 'retry'):
            with _keyboard_page() as page:
                page.clock.install()
                page.evaluate("""() => {
                  window.el=document.createElement('geely-card'); document.body.append(el);
                  el.setConfig({prefix:'car',cooldown:0.1}); window.hass=mkHass({});
                  window.calls=[];
                  // HA's entity registry includes the controls too; the battery-
                  // only fixture otherwise cannot resolve a renamed prefix.
                  for (const id of Object.keys(hass.states)) {
                    hass.entities[id]={platform:'geely_connect',device_id:'d'};
                  }
                  hass.callService=(d,s,data)=>{
                    calls.push([s,data]);
                    return calls.length===1 && window.busy ? Promise.reject({code:8070}):Promise.resolve();
                  }; el.hass=hass;
                }""")
                if action == 'dial':
                    page.evaluate("el._onAction('tempup')")
                elif action == 'arm':
                    page.evaluate("el._onAction('unlock')")
                else:
                    page.evaluate("window.busy=true; el._onAction('lock')")
                page.clock.run_for(50)
                page.evaluate('(cfg)=>el.setConfig(cfg)',changed)
                if action == 'arm':
                    page.evaluate("el._onAction('unlock')")
                page.clock.run_for(1500)
                calls=page.evaluate('calls')
                assert [c[0] for c in calls] == ({'dial':['set_temperature'], 'arm':['unlock'], 'retry':['lock','lock']}[action]), (changed, action, calls)
                assert all(c[1]['entity_id'].startswith(('climate.car_', 'lock.car_')) for c in calls)


def test_native_windows_chip_inherits_the_same_font_as_its_siblings():
    got=_evaluate("""() => {
      const el=document.createElement('geely-card-compact'); document.body.append(el);
      el.setConfig({}); el.hass=mkHass({covers:{windows:'open'}});
      return [...el.shadowRoot.querySelectorAll('.chip')].map(b=>getComputedStyle(b).fontFamily);
    }""")
    assert len(set(got)) == 1, got


def test_a_late_answer_after_the_ceiling_does_not_release_a_newer_request():
    for outcome in ('success', 'busy', 'failure'):
        with _keyboard_page() as page:
            page.clock.install()
            page.evaluate("""() => {
              window.el=document.createElement('geely-card'); document.body.append(el);
              el.setConfig({cooldown:1.5}); window.hass=mkHass({}); window.calls=[];
              window.answers=[];
              hass.callService=(d,s)=>{ calls.push(s); return new Promise((resolve,reject)=>{
                answers.push({resolve,reject});
              }); };
              el.hass=hass; el._call('lock','lock','doors').catch(()=>{});
            }""")
            page.clock.run_for(30001)
            page.locator("geely-card [data-act='find']").click()
            assert page.evaluate('calls') == ['lock', 'press']
            if outcome == 'success':
                page.evaluate('answers[0].resolve()')
            else:
                page.evaluate("answers[0].reject({code:8070})" if outcome == 'busy'
                              else "answers[0].reject({message:'rejected'})")
            page.clock.run_for(3001)
            assert page.locator("geely-card [data-act='find']").is_disabled()
            assert page.evaluate('calls') == ['lock', 'press']
            page.evaluate('answers[1].resolve()')
            page.clock.run_for(1501)
            assert page.locator("geely-card [data-act='find']").is_enabled()
