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


def _hold_enter(page, repeats=3):
    """Send a real Chromium Enter press followed by auto-repeated keydowns."""
    cdp = page.context.new_cdp_session(page)
    try:
        base = {
            "key": "Enter",
            "code": "Enter",
            "windowsVirtualKeyCode": 13,
            "nativeVirtualKeyCode": 13,
        }
        cdp.send("Input.dispatchKeyEvent", {**base, "type": "rawKeyDown", "autoRepeat": False})
        cdp.send("Input.dispatchKeyEvent", {**base, "type": "char", "text": "\r", "autoRepeat": False})
        for _ in range(repeats):
            cdp.send("Input.dispatchKeyEvent", {**base, "type": "rawKeyDown", "autoRepeat": True})
            cdp.send("Input.dispatchKeyEvent", {**base, "type": "char", "text": "\r", "autoRepeat": True})
        cdp.send("Input.dispatchKeyEvent", {**base, "type": "keyUp", "autoRepeat": False})
    finally:
        cdp.detach()


def test_unlock_can_be_confirmed_with_two_keyboard_presses():
    for key in ("Enter", "Space"):
        with _keyboard_page() as page:
            page.evaluate("""() => {
              window.el = document.createElement('geely-card');
              document.body.appendChild(el); el.setConfig({});
              window.hass = window.mkHass({}); el.hass = hass;
            }""")
            unlock = page.locator("geely-card [data-act='unlock']").first
            unlock.focus()
            page.keyboard.press(key)
            assert page.evaluate("hass.serviceCalls.length") == 0
            page.keyboard.press(key)
            assert page.evaluate("hass.serviceCalls.map(c => [c[0],c[1],c[2].entity_id])") == [
                ["lock", "unlock", "lock.car_doors"]]


def test_mouse_click_unlock_does_not_confirm_on_space_or_after_disarm():
    for tag in ("geely-card", "geely-card-compact", "geely-card-mini"):
        with _keyboard_page() as page:
            page.clock.install()
            page.evaluate(f"""() => {{
              window.el = document.createElement('{tag}');
              document.body.appendChild(el); el.setConfig({{}});
              window.hass = window.mkHass({{}}); el.hass = hass;
            }}""")
            unlock = page.locator(f"{tag} [data-act='unlock']").first
            unlock.click()
            assert page.evaluate("el._armed") == "unlock", tag
            page.keyboard.press("Space")
            assert page.evaluate("hass.serviceCalls") == [], tag
            # Wait past the 3 s disarm timer, then press Space twice.
            page.clock.run_for(3100)
            assert page.evaluate("el._armed") is None, tag
            page.keyboard.press("Space")
            page.keyboard.press("Space")
            assert page.evaluate("hass.serviceCalls") == [], tag


def test_held_enter_repeat_cannot_arm_and_confirm_unlock_or_trunk():
    for action, expected in (
        ("unlock", ["lock", "unlock", "lock.car_doors"]),
        ("trunk", ["button", "press", "button.car_unlock_trunk"]),
    ):
        with _keyboard_page() as page:
            page.evaluate("""() => {
              window.el = document.createElement('geely-card');
              document.body.appendChild(el); el.setConfig({});
              window.hass = window.mkHass({}); el.hass = hass;
            }""")
            btn = page.locator(f"geely-card [data-act='{action}']").first
            btn.focus()
            _hold_enter(page)
            assert page.evaluate("el._armed") == action, action
            assert page.evaluate("hass.serviceCalls") == [], action
            # A second distinct Enter still confirms.
            page.keyboard.press("Enter")
            assert page.evaluate("hass.serviceCalls.map(c => [c[0],c[1],c[2].entity_id])") == [
                expected], action


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


def test_held_enter_on_windows_chip_opens_menu_without_triggering_open_cover():
    with _keyboard_page() as page:
        page.evaluate("""() => {
          window.el = document.createElement('geely-card-compact');
          document.body.appendChild(el); el.setConfig({});
          window.hass = window.mkHass({covers:{windows:'open', windowsPos:50}});
          el.hass = hass;
        }""")
        page.locator("geely-card-compact [data-act='windows_menu']").focus()
        _hold_enter(page)
        assert page.locator("dialog[open] .win-menu").count() == 1
        assert page.evaluate("hass.serviceCalls") == []
        # A subsequent distinct Enter activates the focused Open button.
        page.keyboard.press("Enter")
        assert page.evaluate("hass.serviceCalls.map(c => [c[0],c[1],c[2].entity_id])") == [
            ["cover", "open_cover", "cover.car_all_windows"]]


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
    got = _evaluate("""() => {
      const el = document.createElement('geely-card-compact');
      el.style.letterSpacing = '0.4px';
      el.style.lineHeight = '1.4';
      document.body.append(el);
      el.setConfig({}); el.hass = mkHass({covers:{windows:'open', windowsPos:50}});
      const chips = [...el.shadowRoot.querySelectorAll('.chip')];
      const styles = chips.map(b => {
        const cs = getComputedStyle(b);
        return [cs.fontFamily, cs.letterSpacing, cs.lineHeight];
      });
      const btn = el.shadowRoot.querySelector('button[data-act="windows_menu"]');
      const probe = document.createElement('span');
      probe.className = btn.className;
      probe.innerHTML = btn.innerHTML;
      btn.parentNode.appendChild(probe);
      const widths = [btn.getBoundingClientRect().width, probe.getBoundingClientRect().width];
      probe.remove();
      return { styles, widths };
    }""")
    assert len({tuple(s) for s in got["styles"]}) == 1, got
    assert abs(got["widths"][0] - got["widths"][1]) < 0.01, got


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


def test_earlier_hold_timer_cannot_release_a_later_in_flight_command():
    """Real-path stale-_holdTimer regression: with cooldown: 1.5, a temperature
    refused busy at 1.2 s arms a cooldown timer for 2.7 s; Lock dispatched at
    1.7 s (answering at 3.5 s) must clear that earlier timer so Find at 2.8 s
    stays held until Lock answers plus its own 1.5 s cooldown (5.0 s)."""
    with _keyboard_page() as page:
        page.clock.install()
        page.evaluate("""() => {
          window.el = document.createElement('geely-card');
          document.body.append(el);
          el.setConfig({ cooldown: 1.5 });
          window.hass = mkHass({});
          window.calls = [];
          hass.callService = (d, s) => {
            calls.push(s);
            if (s === 'set_temperature') {
              return new Promise((_, reject) => setTimeout(() => reject({ code: 8070 }), 100));
            }
            if (s === 'lock') {
              return new Promise((resolve) => setTimeout(resolve, 1800));
            }
            return Promise.resolve();
          };
          el.hass = hass;
          el._onAction('tempup');
        }""")
        # 1.1 s settle + 0.1 s round trip -> busy refusal at 1.2 s (old release
        # timer would fire at 2.7 s). Lock dispatches at 1.7 s and answers at 3.5 s.
        page.clock.run_for(1700)
        assert page.evaluate("calls") == ["set_temperature"]
        page.evaluate("el._onAction('lock')")
        assert page.evaluate("calls") == ["set_temperature", "lock"]
        assert page.locator("geely-card [data-act='find']").is_disabled()
        # At 2.8 s (past 2.7 s, while Lock is still in flight until 3.5 s):
        # controls must stay held and Find must not go out.
        page.clock.run_for(1100)
        assert page.locator("geely-card [data-act='find']").is_disabled()
        page.locator("geely-card [data-act='find']").evaluate("(b) => b.click()")
        page.evaluate("el._onAction('find')")
        assert page.evaluate("calls") == ["set_temperature", "lock"]
        # Lock answers at 3.5 s and holds through its 1.5 s cooldown (5.0 s).
        page.clock.run_for(2100)
        assert page.locator("geely-card [data-act='find']").is_disabled()
        page.clock.run_for(101)
        assert page.locator("geely-card [data-act='find']").is_enabled()
        page.locator("geely-card [data-act='find']").click()
        assert page.evaluate("calls") == ["set_temperature", "lock", "press"]


def test_busy_refusal_keeps_controls_held_through_cooldown():
    """With default cooldown (3 s), Lock refused busy at 0.3 s keeps Find and
    Climate visibly held at 1.0 s, so the Lock retry at 6.3 s runs cleanly."""
    for other, busy_other in (("find", False), ("climate", True)):
        with _keyboard_page() as page:
            page.clock.install()
            page.evaluate(f"""() => {{
              window.el = document.createElement('geely-card');
              document.body.append(el);
              el.setConfig({{}});
              window.hass = mkHass({{}});
              window.calls = [];
              let locks = 0;
              hass.callService = (d, s) => {{
                calls.push(s);
                if (s === 'lock' && ++locks === 1) {{
                  return new Promise((_, reject) => setTimeout(() => reject({{ code: 8070 }}), 300));
                }}
                if (s === 'press') {{
                  return new Promise((resolve) => setTimeout(resolve, 2500));
                }}
                if ({'true' if busy_other else 'false'} && d === 'climate') {{
                  return Promise.reject({{ code: 8070 }});
                }}
                return Promise.resolve();
              }};
              el.hass = hass;
              el.shadowRoot.querySelector('[data-act="lock"]').click();
            }}""")
            page.clock.run_for(1000)
            assert page.locator(f"geely-card [data-act='{other}']").is_disabled(), other
            page.locator(f"geely-card [data-act='{other}']").evaluate("(b) => b.click()")
            assert page.evaluate("calls") == ["lock"], other
            page.clock.run_for(2301)
            assert page.locator(f"geely-card [data-act='{other}']").is_enabled(), other
            page.clock.run_for(3000)
            assert page.evaluate("calls") == ["lock", "lock"], other


def test_busy_retry_waits_for_active_hold_instead_of_dropping_silently():
    """Default cooldown: Lock refused busy at 0.3 s (retry due at 6.3 s), then
    Find tapped at 3.5 s with a 2.5 s call (answers at 6.0 s, holds until
    9.0 s). Lock's retry must wait for Find's hold to end and fire at 9.0 s."""
    with _keyboard_page() as page:
        page.clock.install()
        page.evaluate("""() => {
          window.el = document.createElement('geely-card');
          document.body.append(el);
          el.setConfig({});
          window.hass = mkHass({});
          window.calls = [];
          let locks = 0;
          hass.callService = (d, s) => {
            calls.push(s);
            if (s === 'lock' && ++locks === 1) {
              return new Promise((_, reject) => setTimeout(() => reject({ code: 8070 }), 300));
            }
            if (s === 'press') {
              return new Promise((resolve) => setTimeout(resolve, 2500));
            }
            return Promise.resolve();
          };
          el.hass = hass;
          el.shadowRoot.querySelector('[data-act="lock"]').click();
        }""")
        # Controls re-enable at 3.3 s; tap Find at 3.5 s.
        page.clock.run_for(3500)
        assert page.locator("geely-card [data-act='find']").is_enabled()
        page.locator("geely-card [data-act='find']").click()
        assert page.evaluate("calls") == ["lock", "press"]
        # At 6.3 s Lock's retry comes due while Find's cooldown runs to 9.0 s.
        page.clock.run_for(5400)  # t = 8.9 s
        assert page.evaluate("calls") == ["lock", "press"]
        assert page.locator("geely-card [data-act='find']").is_disabled()
        # At 9.0 s Find's hold ends and Lock's retry fires immediately.
        page.clock.run_for(101)  # t = 9.001 s
        assert page.evaluate("calls") == ["lock", "press", "lock"]


def test_waiting_busy_retry_cancels_on_driving_car_change_or_disconnect():
    """A busy retry waiting for another command's hold to end must still abort
    if the car starts moving, the card switches cars, or the card disconnects."""
    for interrupt in (
        "hass.states['sensor.car_speed'].state = '40'; el.hass = {...hass};",
        "el.setConfig({ prefix: 'fleet_b' });",
        "el.remove();",
    ):
        with _keyboard_page() as page:
            page.clock.install()
            page.evaluate("""() => {
              window.el = document.createElement('geely-card');
              document.body.append(el);
              el.setConfig({ prefix: 'car' });
              window.hass = mkHass({});
            """ + _two_more_cars() + """
              window.calls = [];
              let locks = 0;
              hass.callService = (d, s) => {
                calls.push(s);
                if (s === 'lock' && ++locks === 1) {
                  return new Promise((_, reject) => setTimeout(() => reject({ code: 8070 }), 300));
                }
                if (s === 'press') {
                  return new Promise((resolve) => setTimeout(resolve, 2500));
                }
                return Promise.resolve();
              };
              el.hass = hass;
              el.shadowRoot.querySelector('[data-act="lock"]').click();
            }""")
            page.clock.run_for(3500)
            page.locator("geely-card [data-act='find']").click()
            page.clock.run_for(3000)  # t = 6.5 s (Lock retry queued on Find's hold)
            page.evaluate(interrupt)
            page.clock.run_for(4000)  # well past Find's 9.0 s hold end
            assert page.evaluate("calls") == ["lock", "press"], interrupt
