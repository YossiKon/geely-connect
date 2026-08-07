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
  return { states: S, entities: E, devices: { d: { name: "Geely EX5" } },
           config: { country: opts.country || "IL" }, callService: () => {} };
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


def _evaluate(script, *, with_polyfill=False, wait_for_cards=True, timeout=4000):
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
        value = page.evaluate(script)
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
    customElements.get() forget everything registered before it (#8)."""
    got = _evaluate("() => !!customElements.get('geely-card')",
                    with_polyfill=True, wait_for_cards=False, timeout=2000)
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
