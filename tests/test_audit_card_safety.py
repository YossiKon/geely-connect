"""Synthetic browser regressions for targeting and delayed card commands."""
from test_cards_in_a_browser import _evaluate


_SETUP = """
const el = document.createElement('geely-card');
document.body.appendChild(el);
el.setConfig({prefix: 'car', cooldown: 0.05});
const hass = window.mkHass({});
// A real registry identifies each command entity, not only the battery.
for (const id in hass.states) {
  hass.entities[id] = {platform: 'geely_connect', device_id: 'd'};
}
el.hass = hass;
// Deterministic timers, including Date.now, without waiting on wall time.
let now = 100000, next = 0;
const timers = new Map();
Date.now = () => now;
window.setTimeout = (fn, ms) => { timers.set(++next, {fn, at: now + ms}); return next; };
window.clearTimeout = (id) => timers.delete(id);
const flush = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };
const tick = async (ms) => {
  const end = now + ms;
  while (true) {
    const due = [...timers].filter(([, t]) => t.at <= end).sort((a,b) => a[1].at-b[1].at)[0];
    if (!due) break;
    now = due[1].at; timers.delete(due[0]); due[1].fn(); await flush();
  }
  now = end; await flush();
};
"""


def _run(body):
    return _evaluate("""async () => {
      const realTimeout = window.setTimeout.bind(window);
    """ + _SETUP + """
      return await Promise.race([
        (async () => {""" + body + """})(),
        new Promise((_, reject) => realTimeout(() => reject(new Error('test did not settle')), 3000))
      ]);
    }""")


def test_unresolved_and_fully_renamed_prefixes_never_borrow_another_car():
    got = _run("""
      const other = window.mkHass({});
      hass.states = {}; hass.entities = {};
      for (const id in other.states) {
        const b = id.replace('.car_', '.other_');
        hass.states[b] = {...other.states[id], entity_id: b};
        hass.entities[b] = {platform:'geely_connect', device_id:'other-device'};
        const a = id.replace('.car_', '.renamed_');
        hass.states[a] = {...other.states[id], entity_id: a};
        hass.entities[a] = {platform:'geely_connect', device_id:'d'};
      }
      const results = [];
      for (const prefix of ['car', 'car_typo']) {
        el.setConfig({prefix, name:'Intended vehicle', cooldown:0}); el.hass = hass;
        el._onAction('unlock'); el._onAction('unlock'); await flush();
        results.push({calls:hass.serviceCalls.length,
          missing:el.shadowRoot.textContent.includes('No Geely vehicle found')});
      }
      return results;
    """)
    assert got == [{"calls": 0, "missing": True}] * 2, got


def test_registry_absence_keeps_the_requested_numeric_prefix_visible_without_actions():
    got = _run("""
      el.setConfig({prefix:2024});
      hass.entities={}; hass.devices={}; el.hass=hass;
      await el._call('lock','unlock','doors');
      return {title:el.shadowRoot.querySelector('.title').textContent,
        missing:el.shadowRoot.textContent.includes('No Geely vehicle found'),
        controls:el.shadowRoot.querySelectorAll('[data-act]').length, calls:hass.serviceCalls};
    """)
    assert got == {"title": "Geely Connect · 2024", "missing": True,
                   "controls": 0, "calls": []}, got


def test_strict_command_entity_must_belong_to_the_selected_device():
    got = _run("""
      hass.entities['lock.car_doors'] = {platform:'geely_connect', device_id:'other'};
      el.hass = hass;
      await el._call('lock', 'unlock', 'doors');
      return hass.serviceCalls;
    """)
    assert got == [], got


def test_valid_vehicle_anchor_does_not_authorize_an_unregistered_command_target():
    for remove_state in (False, True):
        got = _run("""
          delete hass.entities['lock.car_doors'];
          REMOVE_STATE;
          el.hass=hass;
          const result=await el._call('lock','unlock','doors');
          return {result, calls:hass.serviceCalls};
        """.replace("REMOVE_STATE", "delete hass.states['lock.car_doors']" if remove_state else ""))
        assert got == {"result": False, "calls": []}, got


def test_busy_retry_refuses_a_target_whose_registry_ownership_was_removed():
    got = _run("""
      let calls=0;
      hass.callService=()=>{calls++; return Promise.reject(new Error('8070'));};
      const pending=el._call('lock','unlock','doors'); await flush();
      delete hass.entities['lock.car_doors'];
      await tick(100); const result=await pending;
      return {calls,result};
    """)
    assert got == {"calls": 1, "result": False}, got


def test_all_command_domains_require_current_registry_ownership():
    got = _run("""
      el.setConfig({prefix:'car',cooldown:0}); el.hass=hass;
      for (const [domain,service,suffix] of [
        ['lock','unlock','doors'],['climate','set_temperature','climate'],
        ['cover','open_cover','all_windows'],['button','press','refresh_data'],
        ['switch','turn_on','charging'],['time','set_value','scheduled_charging_start'],
        ['select','select_option','seat_heat_driver']
      ]) {
        const id=`${domain}.car_${suffix}`;
        hass.states[id]={state:'off',attributes:{}}; delete hass.entities[id];
        await el._call(domain,service,suffix);
      }
      return hass.serviceCalls;
    """)
    assert got == [], got


def test_foreign_platform_strict_id_and_longer_prefix_cannot_supply_a_target():
    for change in (
        "hass.entities['lock.car_doors'] = {platform:'template', device_id:'other'}",
        "el.setConfig({prefix:'ca'});",
    ):
        got = _run("""
          CHANGE; el.hass=hass;
          await el._call('lock','unlock','doors');
          return hass.serviceCalls;
        """.replace("CHANGE", change))
        assert got == [], got


def test_same_device_rename_stays_usable():
    got = _run("""
      delete hass.states['lock.car_doors']; delete hass.entities['lock.car_doors'];
      hass.states['lock.renamed_doors'] = {state:'locked', attributes:{}};
      hass.entities['lock.renamed_doors'] = {platform:'geely_connect', device_id:'d'};
      el.hass = hass;
      await el._call('lock','unlock','doors');
      return hass.serviceCalls.map(c => c[2].entity_id);
    """)
    assert got == ["lock.renamed_doors"], got


def test_registry_replacement_cannot_silently_bind_the_card_to_another_device():
    got = _run("""
      for (const id in hass.entities) hass.entities[id] = {platform:'geely_connect', device_id:'other'};
      el.hass = hass;
      await el._call('lock','unlock','doors');
      return {calls:hass.serviceCalls, missing:el.shadowRoot.textContent.includes('No Geely vehicle found')};
    """)
    assert got == {"calls": [], "missing": True}, got


def test_expanded_card_keeps_the_parent_vehicle_when_registry_order_changes():
    got = _run("""
      el.setConfig({cooldown:0}); el.hass=hass;
      const otherStates={}, otherEntities={};
      for (const id in hass.states) {
        const other=id.replace('.car_', '.other_');
        otherStates[other]={...hass.states[id], entity_id:other};
        otherEntities[other]={platform:'geely_connect', device_id:'other'};
      }
      hass.states={...otherStates, ...hass.states};
      hass.entities={...otherEntities, ...hass.entities};
      el.hass=hass; el._openFullCard();
      await el._expandCard._call('lock','unlock','doors');
      return hass.serviceCalls.map(c => c[2].entity_id);
    """)
    assert got == ['lock.car_doors'], got


def test_reconfiguring_or_removing_parent_cancels_expanded_card_temperature():
    for operation in ("el.remove()", "el.setConfig({prefix:'other'})"):
        got = _run("""
          el._openFullCard();
          el._expandCard._onAction('tempup');
          OPERATION; await tick(1200);
          return {calls:hass.serviceCalls, dialogs:document.querySelectorAll('dialog').length};
        """.replace("OPERATION", operation))
        assert got == {"calls": [], "dialogs": 0}, got


def test_dismissing_expansion_cancels_pending_temperature_and_cleans_up_menus():
    got = _run("""
      el._openFullCard(); el._expandCard._onAction('tempup');
      const closed = new Promise(r => el._expandDialog.addEventListener('close', r, {once:true}));
      el._expandDialog.close();
      // Synchronize with the browser's actual close event before advancing
      // fake time; a zero-delay timer need not run after that event.
      await closed;
      await tick(1200);
      el._openWindowsMenu(); el.setConfig({prefix:'other'});
      return {calls:hass.serviceCalls, dialogs:document.querySelectorAll('dialog').length};
    """)
    assert got == {"calls": [], "dialogs": 0}, got


def test_request_lifetime_and_post_completion_cooldown_are_separate():
    for cooldown in (0, 0.05):
        got = _run("""
          el.setConfig({prefix:'car', cooldown:COOLDOWN});
          const completions = []; let calls = 0;
          hass.callService = () => { calls++; return new Promise(r => completions.push(r)); };
          const pending = el._call('lock','lock','doors');
          await tick(10000); el._safeRender();
          const gated = el.shadowRoot.querySelector('[data-act="unlock"]').disabled;
          const second = el._call('lock','lock','doors');
          const during = calls;
          completions.forEach(r => r()); await Promise.all([pending, second]);
          const after = el._waiting();
          await tick(100); const released = !el._waiting();
          return {during, gated, after, released};
        """.replace("COOLDOWN", str(cooldown)))
        assert got == {"during": 1, "gated": True,
                       "after": cooldown > 0, "released": True}, got


def test_rejected_and_synchronously_thrown_calls_release_and_notify_once():
    got = _run("""
      const notices = [], unhandled = [];
      el.addEventListener('hass-notification', e => notices.push(e.detail.message));
      window.addEventListener('unhandledrejection', e => {unhandled.push(e.reason); e.preventDefault();});
      hass.callService = () => Promise.reject(new Error('synthetic refusal'));
      el._onAction('lock'); await flush(); await tick(100);
      const released = !el._waiting();
      hass.callService = () => {throw new Error('synthetic synchronous refusal');};
      let threw = false;
      try { el._onAction('lock'); } catch(e) { threw=true; }
      await flush(); await tick(100);
      await new Promise(r => realTimeout(r, 0));
      return {released, threw, notices, unhandled, waiting:el._waiting()};
    """)
    assert got == {"released": True, "threw": False, "notices": [
        "Geely: synthetic refusal", "Geely: synthetic synchronous refusal"],
        "unhandled": [], "waiting": False}, got


def test_busy_retry_is_held_bounded_and_rechecks_driving_at_dispatch():
    got = _run("""
      let attempts = 0;
      hass.callService = () => {attempts++; return Promise.reject(new Error('8070'));};
      const first = el._call('lock','lock','doors').catch(() => {});
      await flush();
      const second = el._call('lock','lock','doors').catch(() => {});
      const held = attempts;
      hass.states['sensor.car_speed'].state = '40'; el.hass = hass;
      await tick(101); await Promise.all([first, second]);
      return {held, attempts, waiting:el._waiting()};
    """)
    assert got == {"held": 1, "attempts": 1, "waiting": True}, got


def test_busy_retry_succeeds_once_or_surfaces_the_second_refusal():
    for outcome in ("Promise.resolve()", "Promise.reject(new Error('8070'))"):
        got = _run("""
          let attempts=0, settled=false; const notices=[];
          el.addEventListener('hass-notification', e => notices.push(e.detail.message));
          hass.callService = () => {attempts++; return attempts === 1
            ? Promise.reject(new Error('8070')) : OUTCOME;};
          el._call('lock','lock','doors').then(() => settled=true, () => settled=true);
          await flush(); await tick(99);
          const before=attempts; await tick(1);
          const after=attempts; await tick(10000);
          return {before, after, attempts, settled, notices, waiting:el._waiting()};
        """.replace("OUTCOME", outcome))
        assert got == {"before": 1, "after": 2, "attempts": 2, "settled": True,
                       "notices": [] if "resolve" in outcome else ["Geely: 8070"],
                       "waiting": False}, got


def test_disconnect_and_reconfigure_cancel_retry_without_unsettled_promise():
    for operation in ("el.remove()", "el.setConfig({prefix:'other'})"):
        got = _run("""
          let attempts=0, settled=false;
          hass.callService = () => {attempts++; return Promise.reject(new Error('8070'));};
          el._call('lock','lock','doors').then(() => settled=true, () => settled=true);
          await flush(); OPERATION; await flush(); await tick(1000);
          return {attempts, settled, timers:timers.size};
        """.replace("OPERATION", operation))
        assert got == {"attempts": 1, "settled": True, "timers": 0}, got


def test_disconnect_does_not_release_a_request_still_in_flight_on_reconnect():
    got = _run("""
      const completions=[]; let calls=0;
      hass.callService = () => {calls++; return new Promise(r => completions.push(r));};
      const pending = el._call('lock','lock','doors');
      el.remove(); document.body.appendChild(el); await tick(10000);
      const second=el._call('lock','lock','doors');
      const during=calls; completions.forEach(r => r());
      await Promise.all([pending, second]); await tick(100);
      return {during, waiting:el._waiting(), timers:timers.size,
        disabled:el.shadowRoot.querySelector('[data-act="lock"]').hasAttribute('disabled')};
    """)
    assert got == {"during": 1, "waiting": False, "timers": 0, "disabled": False}, got


def test_obsolete_request_settlement_restores_current_controls_without_a_new_ha_update():
    for operation in ("el.remove(); document.body.appendChild(el)",
                      "el.setConfig({prefix:'car',cooldown:0})"):
        for rejected in (False, True):
            got = _run("""
              el.setConfig({prefix:'car',cooldown:0}); el.hass=hass;
              let finish; const notices=[];
              el.addEventListener('hass-notification',e=>notices.push(e.detail.message));
              hass.callService=()=>new Promise((resolve,reject)=>{finish=()=>SETTLE;});
              const pending=el._call('lock','unlock','doors').catch(()=>{});
              OPERATION;
              const during=el.shadowRoot.querySelector('[data-act="unlock"]').hasAttribute('disabled');
              finish(); await pending; await flush();
              return {during,disabled:el.shadowRoot.querySelector('[data-act="unlock"]').hasAttribute('disabled'),
                waiting:el._waiting(),timers:timers.size,notices};
            """.replace("OPERATION", operation).replace(
                "SETTLE", "reject(new Error('8070'))" if rejected else "resolve()"))
            assert got == {"during": True, "disabled": False, "waiting": False,
                           "timers": 0, "notices": []}, got


def test_historical_odometer_and_door_ids_resolve_within_the_pinned_vehicle():
    got = _run("""
      const aliases={
        'sensor.total_mileage':'sensor.car_odometer',
        'binary_sensor.door_driver':'binary_sensor.car_driver_door_open',
        'binary_sensor.door_passenger':'binary_sensor.car_passenger_door_open',
        'binary_sensor.door_rear_left':'binary_sensor.car_rear_left_door_open',
        'binary_sensor.door_rear_right':'binary_sensor.car_rear_right_door_open'
      };
      for (const [semantic,id] of Object.entries(aliases)) {
        const current=semantic.replace('.', '.car_');
        hass.states[id]={state:semantic.startsWith('sensor.')?'12345':'on',attributes:{}};
        hass.entities[id]={platform:'geely_connect',device_id:'d'};
        delete hass.states[current]; delete hass.entities[current];
      }
      el.hass=hass;
      return Object.keys(aliases).map(key=>el._st(key)?.state??null);
    """)
    assert got == ["12345", "on", "on", "on", "on"], got


def test_legacy_aliases_can_anchor_a_vehicle_but_never_borrow_a_sibling_reading():
    got = _run("""
      el.setConfig({prefix:'car'});
      hass.states={
        'sensor.car_odometer':{state:'12345',attributes:{}},
        'binary_sensor.other_driver_door_open':{state:'on',attributes:{}}
      };
      hass.entities={
        'sensor.car_odometer':{platform:'geely_connect',device_id:'d'},
        'binary_sensor.other_driver_door_open':{platform:'geely_connect',device_id:'other'}
      };
      el.hass=hass;
      return {device:el._vehicleDevice(),mileage:el._st('sensor.total_mileage')?.state,
        door:el._st('binary_sensor.door_driver')?.state??null};
    """)
    assert got == {"device": "d", "mileage": "12345", "door": None}, got


def test_current_entity_spelling_takes_precedence_over_a_retained_alias():
    got = _run("""
      hass.states['binary_sensor.old_driver_door_open']={state:'on',attributes:{}};
      hass.entities={'binary_sensor.old_driver_door_open':{platform:'geely_connect',device_id:'d'},
        ...hass.entities};
      el.hass=hass;
      return el._st('binary_sensor.door_driver')?.state;
    """)
    assert got == "off", got


def test_rejection_after_teardown_has_no_late_toast_or_timer():
    got = _run("""
      let fail; const notices=[];
      el.addEventListener('hass-notification', e => notices.push(e.detail.message));
      hass.callService=() => new Promise((_, reject) => fail=reject);
      const pending=el._call('lock','lock','doors').catch(() => {});
      el.remove(); fail(new Error('8070')); await pending; await tick(10000);
      return {notices, timers:timers.size, waiting:el._waiting()};
    """)
    assert got == {"notices": [], "timers": 0, "waiting": False}, got


def test_debounced_temperature_rechecks_motion_and_drops_optimistic_target():
    got = _run("""
      el._onAction('tempup');
      hass.states['sensor.car_speed'].state = '40'; el.hass = hass;
      await tick(1200);
      return {calls:hass.serviceCalls, pending:el._pendingTemp,
        shown:el.shadowRoot.querySelector('.tval').textContent};
    """)
    assert got == {"calls": [], "pending": None, "shown": "22°"}, got


def test_debounced_temperature_drops_changed_identity_and_removed_entity():
    for change in ("hass.entities['climate.car_climate'].device_id='other'",
                   "delete hass.entities['climate.car_climate']",
                   "delete hass.states['climate.car_climate']"):
        got = _run("""
          el._onAction('tempup'); CHANGE; el.hass=hass;
          await tick(1200);
          return {calls:hass.serviceCalls, pending:el._pendingTemp};
        """.replace("CHANGE", change))
        assert got == {"calls": [], "pending": None}, got


def test_temperature_timer_is_canceled_on_disconnect_or_config_change():
    for operation in ("el.remove()", "el.setConfig({prefix:'other'})"):
        got = _run("""
          el._onAction('tempup'); OPERATION; await tick(1200);
          return {calls:hass.serviceCalls, pending:el._pendingTemp, timers:timers.size};
        """.replace("OPERATION", operation))
        assert got == {"calls": [], "pending": None, "timers": 0}, got


def test_temperature_debounce_still_combines_taps_and_respects_driving_override():
    got = _run("""
      el.setConfig({prefix:'car', driving_lock:false});
      hass.states['sensor.car_speed'].state = '40'; el.hass = hass;
      el._onAction('tempup'); el._onAction('tempup'); el._onAction('tempup');
      await tick(1200);
      return hass.serviceCalls.map(c => c[2]);
    """)
    assert got == [{"entity_id": "climate.car_climate", "temperature": 23.5}], got
