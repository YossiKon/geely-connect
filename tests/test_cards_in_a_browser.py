"""The card's behaviour in a real browser, run by the normal test command.

Everything else in this suite is Python. These cases are not: they load
geely-card.js into Chromium, hand it a fake `hass`, and assert on what the card
actually renders and registers. That matters because every bug pinned here was
invisible to a Python test, and three of them were publicly described as
"pinned by a browser test" while nothing enforced them - one such script had no
assertion at all, another was never executed because the runner only collects
`test_*.py`, and a third did not exist.

Skips itself when playwright or its Chromium build is missing, so a contributor
without them still gets the Python suite. CI installs both, so these run there
- the whole point being that a fix nobody can enforce is a fix that comes back.
"""
import io
import json
import os

from conftest import PKG
from run import skip

HERE = os.path.dirname(os.path.abspath(__file__))
CARD = os.path.join(PKG, "geely-card.js").replace("\\", "/")
POLYFILL = os.path.join(HERE, "fixtures",
                        "scoped-custom-element-registry.js").replace("\\", "/")
CARD_TAGS = ("geely-card", "geely-card-compact", "geely-card-top",
             "geely-card-mini", "geely-card-strip")
# The mini card deliberately shows no bar and no percentage on any car: it is a
# range, a status and three buttons. Cards that do show them:
BAR_TAGS = ("geely-card", "geely-card-compact", "geely-card-top",
            "geely-card-strip")

# Builds a fake `hass` inside the page. Tire pressures differ per corner on
# purpose, so a mirrored pair is visible in the rendered numbers.
_HASS_FACTORY = """
window.mkHass = (opts) => {
  const S = {}, E = {};
  const P = "car";
  const put = (id, state, attrs = {}) => { S[id] = { entity_id: id, state, attributes: attrs }; };
  (opts.entityOrder || [`sensor.${P}_battery`]).forEach((id) => {
    E[id] = { platform: "geely_connect", device_id: "d" };
  });
  put(`sensor.${P}_12v_battery`, "99", { unit_of_measurement: "%" });
  put(`sensor.${P}_battery`, "61", { unit_of_measurement: "%" });
  put(`sensor.${P}_electric_range`, "256", { unit_of_measurement: "km" });
  put(`sensor.${P}_tire_front_left`, "220", { unit_of_measurement: "kPa" });
  put(`sensor.${P}_tire_front_right`, "240", { unit_of_measurement: "kPa" });
  put(`sensor.${P}_tire_rear_left`, "260", { unit_of_measurement: "kPa" });
  put(`sensor.${P}_tire_rear_right`, "280", { unit_of_measurement: "kPa" });
  put(`lock.${P}_doors`, "locked");
  put(`binary_sensor.${P}_door_driver`, opts.driverDoor || "off");
  put(`binary_sensor.${P}_door_passenger`, "off");
  put(`binary_sensor.${P}_door_rear_left`, "off");
  put(`binary_sensor.${P}_door_rear_right`, "off");
  put(`binary_sensor.${P}_trunk`, "off");
  put(`binary_sensor.${P}_hood`, "off");
  // The climate panel - and therefore every seat control inside it - renders
  // only when the climate entity exists, which is what the seat tests need.
  put(`climate.${P}_climate`, "off", { temperature: 22, min_temp: 15.5,
      max_temp: 28.5, target_temp_step: 0.5, preset_mode: "none" });
  put(`sensor.${P}_speed`, opts.speed === undefined ? "0" : opts.speed,
      { unit_of_measurement: "km/h" });
  // A car with a tank. The integration only creates these when the propulsion
  // verdict says the tank exists, so their presence is what the card reads.
  if (opts.fuel) {
    const f = opts.fuel;
    if (f.litres !== null) put(`sensor.${P}_fuel_level`, f.litres === undefined ? "31" : f.litres, { unit_of_measurement: "L" });
    if (f.pct !== null) put(`sensor.${P}_fuel_level_pct`, f.pct === undefined ? "62" : f.pct, { unit_of_measurement: "%" });
    if (f.range !== null) put(`sensor.${P}_fuel_range`, f.range === undefined ? "480" : f.range, { unit_of_measurement: "km" });
    if (f.combined !== null) put(`sensor.${P}_combined_range`, f.combined === undefined ? "736" : f.combined, { unit_of_measurement: "km" });
  }
  if (opts.noBattery) { delete S[`sensor.${P}_battery`]; }
  // The seat controls render only for the positions the car advertises, so a
  // trim without heated seats gets no row rather than a dead button.
  if (opts.seats) {
    const lvls = ["Off", "Low", "Medium", "High"];
    const seat = opts.seats;
    if (seat.heatDriver !== null) put(`select.${P}_seat_heat_driver`, seat.heatDriver || "Off", { options: lvls });
    if (seat.heatPassenger !== null) put(`select.${P}_seat_heat_passenger`, seat.heatPassenger || "Off", { options: lvls });
    if (seat.ventDriver !== null) put(`select.${P}_seat_vent_driver`, seat.ventDriver || "Off", { options: lvls });
    if (seat.ventPassenger !== null) put(`select.${P}_seat_vent_passenger`, seat.ventPassenger || "Off", { options: lvls });
  }
  if (opts.noElectric) { delete S[`sensor.${P}_electric_range`]; }
  if (opts.at) {
    S[`device_tracker.${P}_location`] = { entity_id: `device_tracker.${P}_location`,
      state: "not_home", attributes: { latitude: opts.at[0], longitude: opts.at[1] } };
  }
  put(`sensor.${P}_engine_state`, opts.engine || "Off");
  const served = [];
  return { states: S, entities: E, devices: { d: { name: "Geely EX5" } },
           config: { country: opts.country || "IL" },
           serviceCalls: served,
           callService: (d, s, data) => served.push([d, s, data]) };
};
"""


def _source(path):
    return io.open(path, encoding="utf-8").read()


def _load(page, with_polyfill):
    """Put the card into the page, and the polyfill after it if asked - that
    order being the one that used to break everything.

    The sources are injected rather than linked: a document created with
    set_content has an about:blank origin, and Chromium refuses to load
    file:// resources into it.
    """
    page.set_content("<!DOCTYPE html><html><head><meta charset='utf-8'>"
                     "</head><body></body></html>")
    page.add_script_tag(content=_HASS_FACTORY)
    page.add_script_tag(content=_source(CARD))
    if with_polyfill:
        page.add_script_tag(content=_source(POLYFILL))


def _evaluate(script, *, with_polyfill=False, wait_for_cards=True, timeout=4000,
              arg=None):
    """Load the card, wait until it has registered, then evaluate `script`.

    Any uncaught page error fails the test: a card that throws renders nothing,
    which is precisely the class of failure these cases exist to catch.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        skip("playwright not installed")
    pw = sync_playwright().start()
    try:
        browser = pw.chromium.launch(headless=True)
    except Exception as e:                       # no browser build present
        pw.stop()
        skip(f"chromium unavailable: {str(e)[:60]}")
    try:
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        _load(page, with_polyfill)
        if wait_for_cards:
            page.wait_for_function(
                "() => window.mkHass && !!customElements.get('geely-card')",
                timeout=timeout)
        else:
            page.wait_for_function("() => !!window.mkHass", timeout=timeout)
        value = page.evaluate(script, arg)
        assert not errors, f"the card threw: {errors}"
        return value
    finally:
        browser.close()
        pw.stop()


def _mount(tag, probe, *, cfg=None, **hass_opts):
    """Mount one card with a fake hass and evaluate `probe` against it.

    `probe` is a JavaScript expression with `el` in scope.
    """
    script = f"""() => {{
        const el = document.createElement({json.dumps(tag)});
        document.body.appendChild(el);
        el.setConfig({json.dumps(cfg or {})});
        el.hass = window.mkHass({json.dumps(hass_opts)});
        return ({probe});
    }}"""
    return _evaluate(script)


# ------------------------------------------------------ #16: the 12V impostor

def test_the_card_binds_to_the_pack_not_the_12v_battery():
    """sensor.<prefix>_12v_battery also ends in _battery, so whichever the
    entity registry listed first used to win - and a card bound to the 12V
    auxiliary looks entirely plausible, because a 12V battery sits at a
    rock-steady 97-100% while the real pack swings underneath (#16). The
    registry here lists the impostor FIRST."""
    got = _mount("geely-card", """{
            prefix: el._prefix,
            header: el.shadowRoot.querySelector(".head .micro").textContent.trim(),
        }""", entityOrder=["sensor.car_12v_battery", "sensor.car_battery"])
    assert got["prefix"] == "car", got
    assert got["header"] == "61%", got


# --------------------------------------------- #8: the scoped-registry wipe

def test_all_five_cards_register_on_load():
    got = _evaluate("() => %s.filter((n) => !customElements.get(n))"
                    % json.dumps(list(CARD_TAGS)))
    assert got == [], f"never registered: {got}"


def test_a_registry_polyfill_really_does_hide_earlier_registrations():
    """The premise behind the watchdog. If this ever stops being true, the
    recovery test below is measuring nothing, so assert the disease as well as
    the cure: several popular cards ship
    @webcomponents/scoped-custom-element-registry, and loading it makes
    customElements.get() forget everything registered before it (#8).

    The polyfill is injected and the registry read inside a single expression,
    because the watchdog is on a 50 ms timer and a timer cannot run in the
    middle of a synchronous task. Loading the polyfill as its own script tag and
    then evaluating separately was a race with the cure - it passed alone and
    failed once under the load of the full suite, which is the worst way for a
    test to behave.
    """
    got = _evaluate(
        """(polyfillSource) => {
             const s = document.createElement("script");
             s.textContent = polyfillSource;
             document.head.appendChild(s);   // inline scripts run synchronously
             return !!customElements.get("geely-card");
           }""",
        arg=_source(POLYFILL))
    assert got is False, (
        "the polyfill no longer wipes earlier registrations - re-check why the "
        "watchdog exists before deleting it"
    )


def test_the_watchdog_puts_every_card_back_after_the_wipe():
    """It has to beat the card picker's two-second lookup budget, or the tiles
    stay spinners for ever while window.customCards still lists them."""
    got = _evaluate(
        "() => new Promise((r) => setTimeout(() => r(%s.filter("
        "(n) => !customElements.get(n))), 200))" % json.dumps(list(CARD_TAGS)),
        with_polyfill=True, wait_for_cards=False, timeout=2000)
    assert got == [], f"still missing 200ms after the wipe: {got}"


# ------------------------------------------------ #14: boot versus trunk

def test_the_boot_is_called_a_boot_where_it_is_called_a_boot():
    """Asked for by an Australian owner, and the label is ours to choose (#14).
    Deliberately not the right-hand-drive list: India drives on the left and
    says boot, the Philippines drives on the right and says trunk."""
    label = '''el.shadowRoot.querySelector('[data-act="trunk"] span').textContent'''
    for country, want in (("AU", "Boot"), ("GB", "Boot"), ("IN", "Boot"),
                          ("NZ", "Boot"), ("ZA", "Boot"),
                          ("US", "Trunk"), ("IL", "Trunk"), ("PH", "Trunk"),
                          ("DE", "Trunk"), ("CA", "Trunk")):
        for tag in ("geely-card", "geely-card-compact", "geely-card-strip"):
            got = _mount(tag, label, country=country)
            assert got == want, (country, tag, got)


def test_the_boot_wording_can_be_overridden_per_card():
    label = '''el.shadowRoot.querySelector('[data-act="trunk"] span').textContent'''
    for cfg, country, want in (({"boot": True}, "US", "Boot"),
                               ({"boot": False}, "AU", "Trunk")):
        got = _mount("geely-card", label, cfg=cfg, country=country)
        assert got == want, (cfg, country, got)


def test_the_top_views_own_caption_follows_the_same_wording():
    caption = """[...el.shadowRoot.querySelectorAll('.cartop text')]
                   .map((t) => t.textContent)
                   .filter((s) => /BOOT|TRUNK/.test(s))"""
    assert _mount("geely-card-top", caption, country="AU") == ["BOOT"]
    assert _mount("geely-card-top", caption, country="US") == ["TRUNK"]


# ------------------------------------- #18: which side the driver sits on

def test_the_top_view_draws_the_drivers_door_on_the_drivers_side():
    """The car reports doors by role - driver, passenger - and the drawing has
    sides, so a right-hand-drive car had its open driver's door painted on the
    left (#18). The reporter opened his two left-hand doors and the card lit
    the front right."""
    lit = """[...el.shadowRoot.querySelectorAll('.cartop .ind.on')]
               .map((c) => Number(c.getAttribute('cx')) < 200 ? 'left' : 'right')"""
    for country, cfg, side in (("IL", {}, "left"), ("AU", {}, "right"),
                               ("AU", {"rhd": False}, "left"),
                               ("IL", {"rhd": True}, "right")):
        got = _mount("geely-card-top", lit, cfg=cfg, country=country,
                     driverDoor="on")
        assert got == [side], (country, cfg, got)


def test_the_top_view_mirrors_the_tires_with_the_doors():
    """The tire entities are named from the same driver/passenger fields under
    the same left-hand-drive assumption, so they have to mirror together - or
    the drawing puts the driver's door on the right and the driver's tire on
    the left. front_left reads 220 kPa here and front_right 240."""
    probe = """(() => {
        const at = (x) => [...el.shadowRoot.querySelectorAll('.cartop text.tv-val')]
          .filter((t) => Number(t.getAttribute('x')) === x)
          .map((t) => t.textContent);
        return { left: at(42), right: at(358) };
    })()"""
    lhd = _mount("geely-card-top", probe, country="IL")
    rhd = _mount("geely-card-top", probe, country="AU")
    assert lhd["left"][0] == "220" and lhd["right"][0] == "240", lhd
    assert rhd["left"][0] == "240" and rhd["right"][0] == "220", rhd


# --------------------------------- every card renders what it watches

def test_no_card_renders_an_entity_it_does_not_watch():
    """The render is skipped when the watched signature is unchanged, so an
    entity that is drawn but unwatched freezes on screen. Both small cards
    showed a door count and a charging label that could not update."""
    src = open(os.path.join(PKG, "geely-card.js"), encoding="utf-8").read()
    from_carstate = {"sensor.charger_connection", "sensor.battery",
                     "sensor.electric_range", "lock.doors", "climate.climate",
                     "binary_sensor.door_driver", "binary_sensor.door_passenger",
                     "binary_sensor.door_rear_left",
                     "binary_sensor.door_rear_right",
                     "binary_sensor.trunk", "binary_sensor.hood"}
    for cls in ("GeelyCard", "GeelyCardTop", "GeelyCardCompact",
                "GeelyCardMini", "GeelyCardStrip"):
        start = src.index(f"class {cls} extends")
        rest = src[start + 1:]
        end = start + 1 + rest.index("\n  class ") if "\n  class " in rest else len(src)
        watched = src[start:end]
        missing = sorted(e for e in from_carstate if f'"{e}"' not in watched)
        assert not missing, f"{cls} renders _carState() but does not watch {missing}"


# --------------------------------------- the car is moving: nothing to press

def _driving_probe():
    """One expression, evaluated against a mounted card."""
    return """{
        banner: !!el.shadowRoot.querySelector(".driving"),
        text: (el.shadowRoot.querySelector(".driving span") ||
               el.shadowRoot.querySelector(".status") || {}).textContent || "",
        live: [...el.shadowRoot.querySelectorAll("[data-act]")]
                .filter((b) => !b.hasAttribute("disabled"))
                .map((b) => b.dataset.act),
        timers: [...el.shadowRoot.querySelectorAll("input[data-time]")]
                .filter((i) => !i.hasAttribute("disabled")).length,
    }"""


def test_every_card_says_the_car_is_driving_and_offers_nothing_to_press():
    """Asked for directly: while the car is moving each card should say so and
    refuse to offer actions. The two one-row cards carry the words on their
    status line instead of a banner - a banner would double their height - so
    the assertion is on the wording, not on the element."""
    for tag in CARD_TAGS:
        got = _mount(tag, _driving_probe(), speed="63", engine="Running")
        assert "Driving" in got["text"], (tag, got)
        # Refresh is a read, not a command, and live data is the one thing worth
        # having mid-drive - everything else has to be disabled.
        assert got["live"] in ([], ["refresh"]), (tag, got["live"])
        assert got["timers"] == 0, (tag, got["timers"])


def test_a_parked_car_still_has_all_of_its_buttons():
    """The other half of the same promise. A lock that cannot be lifted is the
    worse bug of the two."""
    for tag in CARD_TAGS:
        got = _mount(tag, _driving_probe(), speed="0", engine="Off")
        assert got["banner"] is False, tag
        assert "Driving" not in got["text"], (tag, got["text"])
        assert got["live"], f"{tag} has no usable buttons while parked"


def test_a_car_stopped_at_a_light_is_still_driving():
    """Speed reads 0 at every red light. Keying the lock on speed alone would
    hand the buttons back at each stop and take them away again on pulling
    away - the ignition state is what stays put, which is the same reason the
    poller reads both (#21)."""
    for tag in ("geely-card", "geely-card-mini"):
        got = _mount(tag, _driving_probe(), speed="0", engine="Running")
        assert "Driving" in got["text"], (tag, got)
        assert got["live"] in ([], ["refresh"]), (tag, got["live"])


def test_a_trim_that_never_reports_an_engine_state_falls_back_to_speed():
    """`engine_state` is absent on some trims, where the test has to reduce to
    the old speed check rather than concluding "parked" for ever."""
    got = _mount("geely-card", _driving_probe(), speed="41", engine="unknown")
    assert "Driving" in got["text"], got


def test_no_command_is_sent_while_driving_even_if_a_button_is_reached():
    """A disabled attribute is a styling promise, not a lock: keyboard
    activation and anything that dispatches a click another way would still
    fire a command the car is about to refuse."""
    script = """() => {
        const el = document.createElement("geely-card");
        document.body.appendChild(el);
        el.setConfig({});
        const hass = window.mkHass({ speed: "63", engine: "Running" });
        el.hass = hass;
        // Straight at the handler, past the disabled attribute entirely.
        el._onAction("unlock"); el._onAction("unlock");
        el._onAction("climate"); el._onAction("trunk");
        const blocked = hass.serviceCalls.length;
        el.hass = window.mkHass({ speed: "0", engine: "Off" });
        el._onAction("lock");
        return { blocked, afterParking: el._hass.serviceCalls.length };
    }"""
    got = _evaluate(script)
    assert got["blocked"] == 0, f"{got['blocked']} commands escaped while driving"
    assert got["afterParking"] == 1, "the lock did not lift once parked"


def test_refresh_still_works_while_driving():
    """Deliberately exempt: it reads the car rather than commanding it, and a
    moving car is when fresh data is worth most."""
    script = """() => {
        const el = document.createElement("geely-card");
        document.body.appendChild(el);
        el.setConfig({});
        const hass = window.mkHass({ speed: "63", engine: "Running" });
        el.hass = hass;
        el._onAction("refresh");
        return hass.serviceCalls.map((c) => c[0] + "." + c[1]);
    }"""
    assert _evaluate(script) == ["button.press"]


def test_the_banner_appears_without_any_other_state_changing():
    """The render is skipped when the watched signature is unchanged, so if the
    driving entities are not in it the banner never appears - or worse, appears
    and then stays after the car parks. Nothing else moves between these two
    assignments."""
    script = """() => {
        const out = [];
        const el = document.createElement("geely-card-compact");
        document.body.appendChild(el);
        el.setConfig({});
        el.hass = window.mkHass({ speed: "0", engine: "Off" });
        out.push(!!el.shadowRoot.querySelector(".driving"));
        el.hass = window.mkHass({ speed: "0", engine: "Running" });
        out.push(!!el.shadowRoot.querySelector(".driving"));
        el.hass = window.mkHass({ speed: "0", engine: "Off" });
        out.push(!!el.shadowRoot.querySelector(".driving"));
        return out;
    }"""
    assert _evaluate(script) == [False, True, False]


def test_a_renamed_trip_average_is_not_mistaken_for_the_live_speed():
    """`avg_speed` and `engine_speed` both end in `_speed`. The suffix map is
    only consulted once a strict id is missing - i.e. after a rename - and a
    trip average above zero would then pin every card to "Driving" for good.
    Longest-suffix-first is what prevents it, the same rule that stops the 12V
    battery masquerading as the pack."""
    script = """() => {
        const el = document.createElement("geely-card");
        document.body.appendChild(el);
        el.setConfig({});
        const hass = window.mkHass({ speed: "0", engine: "Off" });
        // The owner renamed the live speed sensor away; the trip average, which
        // still reads 47 km/h from this morning's drive, is all that is left.
        delete hass.states["sensor.car_speed"];
        hass.states["sensor.car_average_speed"] =
          { entity_id: "sensor.car_average_speed", state: "47", attributes: {} };
        hass.entities["sensor.car_average_speed"] =
          { platform: "geely_connect", device_id: "d" };
        el.hass = hass;
        return {
            resolved: el._eid("sensor", "speed"),
            driving: el._isDriving(),
            banner: !!el.shadowRoot.querySelector(".driving"),
        };
    }"""
    got = _evaluate(script)
    assert got["resolved"] != "sensor.car_average_speed", got
    assert got["driving"] is False, got
    assert got["banner"] is False, got


# ------------------------------- a car with a tank is a different car to read

def _energy_probe():
    return """{
        headline: (el.shadowRoot.querySelector(".num.n") ||
                   el.shadowRoot.querySelector(".num.rng") || {}).textContent || "",
        labels: [...el.shadowRoot.querySelectorAll(".micro, .pct, .status")]
                  .map((n) => n.textContent.trim()),
        bars: el.shadowRoot.querySelectorAll(".bar").length,
        fuelBars: el.shadowRoot.querySelectorAll(".bar.fuel").length,
    }"""


def test_a_battery_only_car_shows_one_bar_and_calls_it_range():
    for tag in BAR_TAGS:
        got = _mount(tag, _energy_probe())
        assert got["fuelBars"] == 0, (tag, got)
        assert got["bars"] == 1, (tag, got)
        assert "256" in got["headline"], (tag, got["headline"])
        assert not any("fuel" in l.lower() for l in got["labels"]), (tag, got["labels"])


def test_a_car_with_a_tank_leads_with_the_combined_range():
    """The electric range alone understates a hybrid's reach by hundreds of
    kilometres, so it cannot be the headline. 736 is the combined figure here;
    256 is the electric one the battery-only card leads with."""
    for tag in CARD_TAGS:
        got = _mount(tag, _energy_probe(), fuel={})
        assert "736" in got["headline"], (tag, got["headline"])
        assert "256" not in got["headline"], (tag, got["headline"])


def test_a_car_with_a_tank_gets_a_second_bar_and_both_percentages():
    """One bar cannot say which tank it means, and 61% battery beside a 62% tank
    is a different situation from 61% alone."""
    for tag in BAR_TAGS:
        got = _mount(tag, _energy_probe(), fuel={})
        assert got["fuelBars"] == 1, (tag, got)
        assert got["bars"] == 2, (tag, got)
        assert any("62% fuel" in l for l in got["labels"]), (tag, got["labels"])


def test_the_big_cards_show_the_split_behind_the_combined_number():
    """A combined range does not tell a hybrid driver how far it gets before the
    engine starts. The two halves do."""
    for tag in ("geely-card", "geely-card-compact", "geely-card-top"):
        got = _mount(tag, _energy_probe(), fuel={})
        assert any("256 EV" in l and "480 fuel" in l for l in got["labels"]), (
            tag, got["labels"])
        assert any(l == "Combined range" for l in got["labels"]), (tag, got["labels"])


def test_a_hybrid_without_a_combined_figure_falls_back_and_says_so():
    """Some trims report the two halves and not the sum. Showing a dash there
    would be worse than showing the electric range under its own name."""
    got = _mount("geely-card", _energy_probe(), fuel={"combined": None})
    assert "256" in got["headline"], got["headline"]
    assert any(l == "Electric range" for l in got["labels"]), got["labels"]


def test_a_hybrid_reporting_only_a_fuel_range_still_has_a_headline():
    got = _mount("geely-card", _energy_probe(),
                 fuel={"combined": None, "range": "412"}, noElectric=True)
    assert "412" in got["headline"] or "256" in got["headline"], got["headline"]


def test_a_tank_with_no_traction_battery_draws_the_fuel_bar_alone():
    """An empty battery bar on a car that has no traction battery reads as "this
    car cannot move", which is the opposite of the truth."""
    got = _mount("geely-card", _energy_probe(), fuel={}, noBattery=True)
    assert got["bars"] == 1 and got["fuelBars"] == 1, got


def test_the_fuel_half_appears_without_any_other_state_changing():
    """The render is skipped when the watched signature is unchanged, so if the
    fuel entities are not in it a hybrid's bar and headline would freeze."""
    script = """() => {
        const el = document.createElement("geely-card");
        document.body.appendChild(el);
        el.setConfig({});
        el.hass = window.mkHass({});
        const before = el.shadowRoot.querySelectorAll(".bar.fuel").length;
        el.hass = window.mkHass({ fuel: {} });
        const after = el.shadowRoot.querySelectorAll(".bar.fuel").length;
        el.hass = window.mkHass({ fuel: { pct: "9" } });
        const moved = [...el.shadowRoot.querySelectorAll(".micro, .pct")]
                        .some((n) => n.textContent.includes("9% fuel"));
        return { before, after, moved };
    }"""
    got = _evaluate(script)
    assert got == {"before": 0, "after": 1, "moved": True}, got


def test_the_mini_card_says_it_with_the_number_because_it_has_nothing_else():
    """It shows no bar and no percentage on any car by design. So the whole
    propulsion difference has to land in the one number it does show - and it
    must not silently keep showing the electric range on a car with a tank."""
    ev = _mount("geely-card-mini", _energy_probe())
    hy = _mount("geely-card-mini", _energy_probe(), fuel={})
    assert ev["headline"].strip() == "256", ev
    assert hy["headline"].strip() == "736", hy
    assert ev["bars"] == 0 and hy["bars"] == 0, (ev, hy)


def test_the_fuel_section_reports_the_engine_and_not_the_headline_twice():
    """The headline already is the combined figure, so repeating it in the fuel
    section wastes a row. Whether the engine is running is the one hybrid fact
    these cards could not show anywhere else."""
    probe = """[...el.shadowRoot.querySelectorAll(".row")]
                 .map((r) => r.textContent.replace(/\s+/g, " ").trim())"""
    rows = _mount("geely-card", probe, fuel={}, engine="Running")
    assert any(r.startswith("Fuel level") for r in rows), rows
    assert any(r.startswith("Fuel range") for r in rows), rows
    # The label and value are adjacent elements, so the text runs together.
    assert any(r.startswith("Engine") and "Running" in r for r in rows), rows
    assert not any(r.startswith("Combined range") for r in rows), (
        "the combined range is the headline here and should not repeat", rows)
    # A trim that reports the halves but not the sum still gets it spelled out.
    rows = _mount("geely-card", probe, fuel={"combined": None})
    assert any(r.startswith("Fuel range") for r in rows), rows


# ------------------------------------- seat heating and cooling on the big cards

_SEAT_PROBE = """(() => {
    const label = (sel) => [...el.shadowRoot.querySelectorAll(sel)]
        .map((n) => n.textContent.replace(/\s+/g, " ").trim());
    return {
        headings: label(".csub"),
        buttons: [...el.shadowRoot.querySelectorAll('[data-act^="seat_"]')]
            .map((b) => ({ act: b.dataset.act,
                           text: b.textContent.replace(/\s+/g, " ").trim(),
                           lit: b.classList.contains("on") })),
    };
})()"""


def test_the_big_cards_offer_seat_heating_and_cooling_per_seat():
    """One button per seat per feature, each showing its own level. The card is
    where these are reachable in one tap - the entities themselves are selects
    buried in the device page."""
    for tag in ("geely-card", "geely-card-top"):
        got = _mount(tag, _SEAT_PROBE, seats={"heatDriver": "High",
                                              "ventPassenger": "Medium"})
        assert any("Seat heating" in h for h in got["headings"]), (tag, got)
        assert any("Seat cooling" in h for h in got["headings"]), (tag, got)
        acts = {b["act"]: b for b in got["buttons"]}
        assert set(acts) == {"seat_heat_driver", "seat_heat_passenger",
                             "seat_vent_driver", "seat_vent_passenger"}, (tag, acts)
        assert "High" in acts["seat_heat_driver"]["text"], (tag, acts)
        assert "Medium" in acts["seat_vent_passenger"]["text"], (tag, acts)
        # A seat that is actually on has to look different from one that is off.
        assert acts["seat_heat_driver"]["lit"] is True, (tag, acts)
        assert acts["seat_heat_passenger"]["lit"] is False, (tag, acts)


def test_a_trim_without_heated_seats_gets_no_row_at_all():
    """Better than a button that cannot do anything: the row is absent."""
    got = _mount("geely-card", _SEAT_PROBE)
    assert got["buttons"] == [], got
    assert not any("Seat" in h for h in got["headings"]), got


def test_only_the_advertised_seats_appear():
    got = _mount("geely-card", _SEAT_PROBE,
                 seats={"heatPassenger": None, "ventDriver": None,
                        "ventPassenger": None, "heatDriver": "Low"})
    assert [b["act"] for b in got["buttons"]] == ["seat_heat_driver"], got
    assert any("Seat heating" in h for h in got["headings"]), got
    assert not any("Seat cooling" in h for h in got["headings"]), got


def test_tapping_a_seat_steps_to_the_next_level_and_wraps():
    """Off -> Low -> Medium -> High -> Off, so one control reaches every level
    without a dropdown."""
    script = """() => {
        const el = document.createElement("geely-card");
        document.body.appendChild(el);
        el.setConfig({});
        // cooldown: 0 - the gate between commands is what this test is not
        // about, and two taps in a row is exactly what it holds back.
        el.setConfig({ cooldown: 0 });
        const hass = window.mkHass({ seats: { heatDriver: "Medium" } });
        el.hass = hass;
        el._onAction("seat_heat_driver");
        hass.states["select.car_seat_heat_driver"].state = "High";
        el._onAction("seat_heat_driver");
        return hass.serviceCalls.map((c) => [c[0], c[1], c[2].option]);
    }"""
    assert _evaluate(script) == [["select", "select_option", "High"],
                                ["select", "select_option", "Off"]]


def test_the_seat_buttons_are_locked_while_driving_like_everything_else():
    got = _mount("geely-card", """[...el.shadowRoot.querySelectorAll('[data-act^="seat_"]')]
                     .filter((b) => !b.hasAttribute("disabled")).length""",
                 seats={"heatDriver": "High"}, speed="63", engine="Running")
    assert got == 0, f"{got} seat buttons stayed live on a moving car"


# ---------------------------------------- navigating to where the car is parked

def test_the_big_cards_link_to_the_car_in_maps_and_waze():
    probe = """[...el.shadowRoot.querySelectorAll("a.nav")]
                 .map((a) => [a.textContent.trim(), a.getAttribute("href"),
                              a.getAttribute("target"), a.getAttribute("rel")])"""
    for tag in ("geely-card", "geely-card-top"):
        got = _mount(tag, probe, at=[32.0853, 34.7818])
        assert len(got) == 2, (tag, got)
        (m_label, m_href, m_t, m_rel), (w_label, w_href, _, _) = got
        assert m_label == "Maps" and w_label == "Waze", got
        assert "google.com/maps/dir/" in m_href and "32.085300,34.781800" in m_href, got
        assert "waze.com/ul?ll=32.085300,34.781800" in w_href and "navigate=yes" in w_href, got
        # Opening a map must not navigate the dashboard away, or leak the referrer.
        assert m_t == "_blank" and "noopener" in m_rel, got


def test_no_link_when_the_car_has_not_reported_where_it_is():
    """A link to 0,0 sends the owner to the Atlantic. No position, no button."""
    probe = 'el.shadowRoot.querySelectorAll("a.nav").length'
    assert _mount("geely-card", probe) == 0
    assert _mount("geely-card", probe, at=[0, 0]) == 0


def test_the_navigation_links_stay_usable_while_the_car_is_driving():
    """They open a map; they do not command the car. Someone whose car is being
    driven away is exactly who wants to see where it is."""
    probe = """[...el.shadowRoot.querySelectorAll("a.nav")]
                 .filter((a) => !a.hasAttribute("disabled")).length"""
    assert _mount("geely-card", probe, at=[32.1, 34.8],
                  speed="63", engine="Running") == 2


# ------------------------------------------ one command at a time, with a gap

def test_a_second_tap_during_the_wait_is_not_sent():
    """The car refuses a command that arrives while it is still executing the
    last one, and the refused command is dropped rather than queued - so the tap
    is lost and the owner gets an error toast. Holding it back here is the
    difference between one working command and one working plus one lost."""
    script = """() => {
        const el = document.createElement("geely-card");
        document.body.appendChild(el);
        el.setConfig({});
        const hass = window.mkHass({});
        el.hass = hass;
        el._onAction("find");
        el._onAction("find");
        el._onAction("defrost");
        return hass.serviceCalls.map((c) => c[0] + "." + c[1]);
    }"""
    assert _evaluate(script) == ["button.press"]


def test_cooldown_zero_turns_the_gate_off_for_anyone_who_wants_it_off():
    script = """() => {
        const el = document.createElement("geely-card");
        document.body.appendChild(el);
        el.setConfig({ cooldown: 0 });
        const hass = window.mkHass({});
        el.hass = hass;
        el._onAction("find");
        el._onAction("find");
        return hass.serviceCalls.length;
    }"""
    assert _evaluate(script) == 2


def test_the_stepper_moves_the_number_now_and_tells_the_car_once():
    """Three taps from 22 to 23.5 used to be three commands, two of them dropped
    with "the last request has not yet been executed". The display has to keep up
    with the finger, and the car has to hear one number."""
    script = """() => new Promise((done) => {
        const el = document.createElement("geely-card");
        document.body.appendChild(el);
        el.setConfig({});
        const hass = window.mkHass({});
        el.hass = hass;
        const shown = () => el.shadowRoot.querySelector(".tval").textContent.trim();
        el._onAction("tempup"); el._onAction("tempup"); el._onAction("tempup");
        const afterTaps = { shown: shown(), sent: hass.serviceCalls.length };
        setTimeout(() => done({ afterTaps,
            sent: hass.serviceCalls.map((c) => c[2].temperature) }), 1600);
    })"""
    got = _evaluate(script, timeout=8000)
    assert got["afterTaps"] == {"shown": "23.5\u00b0", "sent": 0}, got
    assert got["sent"] == [23.5], got


def test_a_command_the_car_refused_because_it_was_busy_is_retried_once():
    """A refused command never ran, so a retry is the only way it happens - and
    that is a different thing from the second command this project reverted in
    v1.21.5, which raced a first one that *was* running."""
    script = """() => new Promise((done) => {
        const el = document.createElement("geely-card");
        document.body.appendChild(el);
        el.setConfig({ cooldown: 0.05 });
        const hass = window.mkHass({});
        let attempts = 0;
        hass.callService = () => {
            attempts += 1;
            return attempts === 1
              ? Promise.reject({ message: "The last request has not yet been executed, please send the command later!" })
              : Promise.resolve();
        };
        el.hass = hass;
        el._onAction("find");
        setTimeout(() => done(attempts), 900);
    })"""
    assert _evaluate(script, timeout=8000) == 2


def test_a_real_refusal_is_not_retried_and_still_reaches_the_user():
    """Only "still busy" earns a retry. Anything else is the car saying no, and
    hiding it would leave the owner believing a command worked."""
    script = """() => new Promise((done) => {
        const el = document.createElement("geely-card");
        document.body.appendChild(el);
        el.setConfig({ cooldown: 0.05 });
        const hass = window.mkHass({});
        let attempts = 0, surfaced = null;
        hass.callService = () => {
            attempts += 1;
            return Promise.reject({ message: "feature not available on this vehicle" });
        };
        el.hass = hass;
        // The card raises its own toast, the way Home Assistant's cards do.
        el.addEventListener("hass-notification", (e) => { surfaced = e.detail.message; });
        el._onAction("find");
        addEventListener("unhandledrejection", (e) => e.preventDefault());
        setTimeout(() => done({ attempts, surfaced }), 700);
    })"""
    got = _evaluate(script, timeout=8000)
    assert got["attempts"] == 1, got
    assert got["surfaced"] == "Geely: feature not available on this vehicle", got


def test_no_card_says_parked_while_it_says_driving():
    """#25: the banner said Driving and the buttons were greyed out while the
    status line two lines above still read "Parked". The line was keyed on raw
    speed and the lock on the composite - and #21 showed how often those differ:
    that owner's speed field read 0 for twenty-five minutes of a drive."""
    probe = """[...el.shadowRoot.querySelectorAll(".status, .chip, .driving span")]
                 .map((n) => n.textContent.replace(/\s+/g, " ").trim()).join(" | ")"""
    for tag in CARD_TAGS:
        # Moving, and the speed field says 0 - the #21 backend behaviour.
        text = _mount(tag, probe, speed="0", engine="Running")
        assert "Parked" not in text, (tag, text)
        assert "Driving" in text, (tag, text)
        # And with a real speed the number is still worth showing.
        text = _mount(tag, probe, speed="63", engine="Running")
        assert "Parked" not in text, (tag, text)
    for tag in CARD_TAGS:
        text = _mount(tag, probe, speed="0", engine="Off")
        assert "Driving" not in text, (tag, text)


def test_the_speed_is_shown_when_the_car_reports_one():
    probe = 'el.shadowRoot.querySelector(".status").textContent.trim()'
    for tag in ("geely-card", "geely-card-top"):
        assert _mount(tag, probe, speed="63", engine="Running") == "Driving · 63 km/h"
        # No speed to show is not a reason to withhold the fact of driving.
        assert _mount(tag, probe, speed="0", engine="Running") == "Driving"


# --------------------------------- #26, #27: whichever map app the owner uses

def _nav_probe():
    return """[...el.shadowRoot.querySelectorAll("a.nav")]
                .map((a) => [a.textContent.trim(), a.getAttribute("href")])"""


def test_the_default_pair_is_unchanged_for_anyone_who_has_not_asked():
    got = _mount("geely-card", _nav_probe(), at=[32.0853, 34.7818])
    assert [g[0] for g in got] == ["Maps", "Waze"], got


def test_apple_maps_and_here_can_be_chosen():
    """#26 asked for Apple Maps, #27 for HERE WeGo - and supplied the URL its own
    user had already worked out, `share.here.com/r/lat,lon`."""
    got = _mount("geely-card", _nav_probe(), cfg={"nav": ["apple", "here"]},
                 at=[32.0853, 34.7818])
    labels = [g[0] for g in got]
    hrefs = dict(got)
    assert labels == ["Apple Maps", "HERE"], got
    assert hrefs["Apple Maps"] == "https://maps.apple.com/?daddr=32.085300,34.781800"
    assert hrefs["HERE"] == "https://share.here.com/r/32.085300,34.781800"


def test_all_four_at_once_and_in_the_order_asked_for():
    got = _mount("geely-card", _nav_probe(),
                 cfg={"nav": ["here", "apple", "waze", "maps"]}, at=[1.5, 2.5])
    assert [g[0] for g in got] == ["HERE", "Apple Maps", "Waze", "Maps"], got


def test_no_travel_mode_is_imposed_unless_it_is_asked_for():
    """Guessing is wrong both ways: you walk to a car parked round the corner and
    drive to one left at the airport. The app's own default is the better guess."""
    plain = dict(_mount("geely-card", _nav_probe(),
                        cfg={"nav": ["maps", "apple", "here"]}, at=[1.5, 2.5]))
    assert "travelmode" not in plain["Maps"], plain
    assert "dirflg" not in plain["Apple Maps"], plain
    assert "?" not in plain["HERE"].split("/r/")[1], plain

    walk = dict(_mount("geely-card", _nav_probe(),
                       cfg={"nav": ["maps", "apple", "here"],
                            "nav_travel": "walking"}, at=[1.5, 2.5]))
    assert "travelmode=walking" in walk["Maps"], walk
    assert "dirflg=w" in walk["Apple Maps"], walk
    assert walk["HERE"].endswith("?m=w"), walk


def test_an_unknown_app_name_is_skipped_rather_than_breaking_the_card():
    got = _mount("geely-card", _nav_probe(),
                 cfg={"nav": ["maps", "tomtom", "", 7]}, at=[1.5, 2.5])
    assert [g[0] for g in got] == ["Maps"], got


def test_an_empty_list_hides_the_row_entirely():
    """Someone who does not want the links should be able to say so."""
    got = _mount("geely-card", """{
        links: el.shadowRoot.querySelectorAll("a.nav").length,
        heading: [...el.shadowRoot.querySelectorAll(".csub")]
                   .some((n) => /Navigate/.test(n.textContent)),
    }""", cfg={"nav": []}, at=[1.5, 2.5])
    assert got == {"links": 0, "heading": False}, got


def test_the_driving_lock_can_be_switched_off_when_a_flag_sticks():
    """The failure mode is total - every control on every card, with no way back -
    and this integration already knows the engine flag can stick: the poller
    carries a guard for "a stuck flag, a driver sitting in the car with the
    ignition on for an hour". So there is a way out."""
    probe = """{
        banner: !!el.shadowRoot.querySelector(".driving"),
        live: [...el.shadowRoot.querySelectorAll("[data-act]")]
                .filter((b) => !b.hasAttribute("disabled")).length,
        status: (el.shadowRoot.querySelector(".status") || {}).textContent || "",
    }"""
    locked = _mount("geely-card", probe, speed="0", engine="Running")
    assert locked["banner"] is True and locked["live"] == 1, locked

    freed = _mount("geely-card", probe, cfg={"driving_lock": False},
                   speed="0", engine="Running")
    assert freed["banner"] is False, freed
    assert freed["live"] > 1, freed
    # And the line goes back to describing the car rather than the lock.
    assert "Driving" not in freed["status"], freed


# ------------------------------------- the wait must not make the card jump

def test_a_command_does_not_rebuild_the_card():
    """The gate used to be applied by re-rendering, which replaces the whole
    shadow root - the car drawing included. One press cost two full renders and
    the card visibly jumped. Toggling attributes in place costs none."""
    script = """() => new Promise((done) => {
        const el = document.createElement("geely-card");
        document.body.appendChild(el);
        el.setConfig({});
        el.hass = window.mkHass({});
        let renders = 0;
        const orig = el._render.bind(el);
        el._render = () => { renders += 1; return orig(); };
        const disabled = () => [...el.shadowRoot.querySelectorAll("[data-act]")]
            .filter((b) => b.hasAttribute("disabled")).length;
        el._onAction("find");
        const held = disabled();
        setTimeout(() => done({ renders, held, freed: disabled() }), 4200);
    })"""
    got = _evaluate(script, timeout=12000)
    assert got["renders"] == 0, f"the card rebuilt itself {got['renders']} times"
    assert got["held"] > 0, got
    assert got["freed"] == 0, "the controls never came back"


def test_the_stepper_never_shows_a_number_going_backwards():
    """Clearing the pending value when the command fired made the display fall
    back to the entity's old target for the length of the round trip - so the
    number the user had just dialled in jumped backwards, then forwards again."""
    script = """() => new Promise((done) => {
        const el = document.createElement("geely-card");
        document.body.appendChild(el);
        el.setConfig({});
        const hass = window.mkHass({});
        el.hass = hass;
        let renders = 0;
        const orig = el._render.bind(el);
        el._render = () => { renders += 1; return orig(); };
        const seen = [];
        const watch = setInterval(() => {
            const t = el.shadowRoot.querySelector(".tval");
            if (t) { const v = t.textContent.trim();
                     if (seen[seen.length - 1] !== v) seen.push(v); }
        }, 40);
        el._onAction("tempup"); el._onAction("tempup"); el._onAction("tempup");
        setTimeout(() => {
            clearInterval(watch);
            done({ seen, renders,
                   sent: hass.serviceCalls.map((c) => c[2].temperature) });
        }, 2600);
    })"""
    got = _evaluate(script, timeout=12000)
    numbers = [float(v.replace("\u00b0", "")) for v in got["seen"]]
    assert numbers == sorted(numbers), f"the number went backwards: {got['seen']}"
    assert numbers[-1] == 23.5, got
    assert got["sent"] == [23.5], got
    assert got["renders"] == 0, f"three taps rebuilt the card {got['renders']} times"


def test_the_pending_number_is_released_once_the_car_reports_it():
    """Otherwise the card would keep preferring a value that is no longer
    pending, and a later change made elsewhere would not show."""
    script = """() => new Promise((done) => {
        const el = document.createElement("geely-card");
        document.body.appendChild(el);
        el.setConfig({ cooldown: 0 });
        el.hass = window.mkHass({});
        el._onAction("tempup");
        setTimeout(() => {
            const before = el._pendingTemp;
            const hass = window.mkHass({});
            hass.states["climate.car_climate"].attributes.temperature = 22.5;
            el.hass = hass;
            done({ before, after: el._pendingTemp,
                   shown: el.shadowRoot.querySelector(".tval").textContent.trim() });
        }, 1500);
    })"""
    got = _evaluate(script, timeout=12000)
    assert got["before"] == 22.5 and got["after"] is None, got
    assert got["shown"] == "22.5\u00b0", got


def test_a_refused_temperature_does_not_stay_on_screen():
    """A number the car rejected must not sit there looking accepted."""
    script = """() => new Promise((done) => {
        const el = document.createElement("geely-card");
        document.body.appendChild(el);
        el.setConfig({ cooldown: 0.05 });
        const hass = window.mkHass({});
        hass.callService = () => Promise.reject({ message: "no" });
        el.hass = hass;
        addEventListener("unhandledrejection", (e) => e.preventDefault());
        el._onAction("tempup");
        setTimeout(() => done({ pending: el._pendingTemp,
            shown: el.shadowRoot.querySelector(".tval").textContent.trim() }), 1800);
    })"""
    got = _evaluate(script, timeout=12000)
    assert got["pending"] is None, got
    assert got["shown"] == "22\u00b0", got


def test_refresh_is_held_by_the_wait_though_not_by_the_driving_lock():
    """It reads rather than commands, so a moving car may still be polled - but
    the wait blocks every command including that one, and a button that looks
    live while being silently dropped is worse than one that looks held."""
    probe = """(() => {
        const b = el.shadowRoot.querySelector('[data-act="refresh"]');
        return b ? !b.hasAttribute("disabled") : null;
    })()"""
    assert _mount("geely-card", probe, speed="63", engine="Running") is True
    script = """() => {
        const el = document.createElement("geely-card");
        document.body.appendChild(el);
        el.setConfig({});
        el.hass = window.mkHass({});
        el._onAction("find");
        const b = el.shadowRoot.querySelector('[data-act="refresh"]');
        return !b.hasAttribute("disabled");
    }"""
    assert _evaluate(script) is False, "refresh stayed live during the wait"


def test_the_trunk_button_says_it_only_unlocks():
    """The button reads BOOT or TRUNK beside a tailgate icon, so anyone would
    expect it to open one. It releases the latch and the gate still has to be
    lifted - four owners across three trims have confirmed the official app does
    the same - so the control itself has to say so, not only the README."""
    probe = """(() => {
        const b = el.shadowRoot.querySelector('[data-act="trunk"]');
        return b ? { label: b.textContent.trim(), title: b.getAttribute("title") } : null;
    })()"""
    for tag, word in (("geely-card", "trunk"), ("geely-card-top", "trunk"),
                      ("geely-card-compact", "trunk"), ("geely-card-strip", "trunk")):
        got = _mount(tag, probe, country="US")
        assert got is not None, tag
        assert got["label"] == "Trunk", (tag, got)
        t = got["title"].lower()
        assert "does not open" in t and "electrically" in t, (tag, got["title"])
        assert "by hand" in t and "re-locks" in t, (tag, got["title"])
    # And it follows the local word, so an Australian owner is not told about a
    # trunk their car does not have.
    got = _mount("geely-card", probe, country="AU")
    assert got["label"] == "Boot", got
    assert "boot latch" in got["title"].lower(), got["title"]
    assert "trunk" not in got["title"].lower(), got["title"]
