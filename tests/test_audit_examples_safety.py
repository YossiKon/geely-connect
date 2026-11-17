"""The bedtime check and the attention widget, run in offline Home Assistant.

A default paste must do what it did before: the report-age test and the
widget's list of readings the car did not give are both off unless the owner
turns them on. The shipped YAML runs through HA's own Script engine and the
widget through its Template engine, with fake services and synthetic states.
"""
import asyncio
import copy
from datetime import timedelta
from pathlib import Path
import tempfile
import time

from conftest import have_homeassistant
from run import skip


ROOT = Path(__file__).resolve().parent.parent
PREFIX = "my_geely_ex5"
WIDGET = ROOT / "android-widgets/attention.jinja"


def _hass_run(check):
    if not have_homeassistant():
        skip("homeassistant not installed")
    from homeassistant.core import HomeAssistant

    async def run():
        with tempfile.TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            try:
                return await check(hass)
            finally:
                await hass.async_stop()
    return asyncio.run(run())


def _ago(**delta):
    from homeassistant.util import dt as dt_util
    return (dt_util.utcnow() - timedelta(**delta)).isoformat()


def _set(hass, short, value, attributes=None):
    domain, suffix = short.split(".")
    hass.states.async_set(f"{domain}.{PREFIX}_{suffix}", value, attributes or {})


def _tire(kpa):
    """The attributes GeelyTireSensor.extra_state_attributes always sets."""
    return {"unit_of_measurement": "kPa", "kPa": kpa,
            "psi": round(kpa * 0.1450377, 1), "bar": round(kpa / 100, 2)}


def _normal_states(hass, *, reported=None):
    """A parked car, everything shut and locked; its last report 9 h old."""
    readings = {
        "binary_sensor.connected": "on", "lock.doors": "locked",
        "sensor.battery": "80", "sensor.12v_battery": "90",
        "sensor.days_to_service": "150", "sensor.distance_to_service": "5000",
        "cover.all_windows": "closed", "cover.sunroof": "closed",
        "sensor.last_updated": _ago(seconds=30),
        "sensor.car_reported_at": reported or _ago(hours=9),
    }
    for suffix in ("door_driver", "door_passenger", "door_rear_left",
                   "door_rear_right", "trunk", "hood"):
        readings[f"binary_sensor.{suffix}"] = "off"
    for short, value in readings.items():
        _set(hass, short, value)
    for suffix in ("tire_front_left", "tire_front_right", "tire_rear_left", "tire_rear_right"):
        _set(hass, f"sensor.{suffix}", "240", _tire(240))


def _render_widget(hass, **knobs):
    from homeassistant.helpers.template import Template
    source = WIDGET.read_text(encoding="utf-8")
    for name, value in knobs.items():
        line = next(l for l in source.splitlines() if l.startswith(f"{{%- set {name} = "))
        source = source.replace(line, f"{{%- set {name} = {value!r} -%}}".replace("True", "true"))
    return Template(source, hass).async_render(parse_result=False)


def _bedtime(*, keep_lock_condition=True, **variables):
    from homeassistant.util.yaml import loader
    config = copy.deepcopy(next(
        a for a in loader.load_yaml(str(ROOT / "automations/security.yaml"))
        if a["id"] == "geely_ex5_security_bedtime_check"))
    config["actions"][0]["variables"].update(variables)
    if not keep_lock_condition:
        config["actions"] = [a for a in config["actions"]
                             if "lock." not in str(a.get("value_template", ""))]
    return config


async def _execute(hass, config, *, refresh=None, wait_s=0.001):
    """Real HA schema and Script; the services are fakes and the 120 s wait
    is cut to `wait_s`. Returns the service calls and the run time."""
    from homeassistant.core import Context
    from homeassistant.components.automation.config import PLATFORM_SCHEMA
    from homeassistant.helpers.script import Script

    calls = []

    async def record(call):
        calls.append((call.domain, call.service, dict(call.data)))
        if call.domain == "button" and refresh:
            refresh(hass)

    for domain, service in (("button", "press"), ("notify", "mobile_app_your_phone")):
        hass.services.async_register(domain, service, record)
    for action in config["actions"]:
        if "wait_template" in action:
            action["timeout"] = {"milliseconds": int(wait_s * 1000)}
    config = PLATFORM_SCHEMA(config)
    script = Script(hass, config["actions"], "bedtime", "bedtime")
    start = time.monotonic()
    await script.async_run(context=Context())
    return calls, time.monotonic() - start


def _messages(calls):
    return [data["message"] for domain, _, data in calls if domain == "notify"]


def _later(hass, seconds, short, value):
    hass.loop.call_later(seconds, lambda: _set(hass, short, value))


# ------------------------------------------------------------------ widget

def test_a_default_widget_shows_the_tick_wherever_it_did_before():
    """#118 review: locking/unlocking after a command from HA, a trim with no
    battery, lock or hood reading, a sleeping TPMS and an old report all left
    main's tick alone. With both checks off they still do."""
    cases = (("lock.doors", "locking"), ("lock.doors", "unlocking"),
             ("lock.doors", "unavailable"), ("binary_sensor.hood", "unavailable"),
             ("sensor.battery", None), ("sensor.tire_front_left", "unknown"),
             ("sensor.car_reported_at", "unavailable"))

    async def check(hass):
        for short, value in cases:
            _normal_states(hass)
            if value is None:
                domain, suffix = short.split(".")
                hass.states.async_remove(f"{domain}.{PREFIX}_{suffix}")
            else:
                _set(hass, short, value)
            rendered = _render_widget(hass)
            assert "All good" in rendered and "Unable to check" not in rendered, (short, rendered)
    _hass_run(check)


def test_a_widget_with_check_readings_names_what_the_car_did_not_give():
    async def check(hass):
        for short, value in (("binary_sensor.hood", "unavailable"), ("lock.doors", "unknown"),
                             ("binary_sensor.connected", "unknown"), ("sensor.battery", "bad")):
            _normal_states(hass)
            _set(hass, short, value)
            rendered = _render_widget(hass, check_readings=True)
            assert "Unable to check" in rendered and "All good" not in rendered, (short, rendered)
        for lock in ("locking", "unlocking"):
            _normal_states(hass)
            _set(hass, "lock.doors", lock)
            assert "All good" in _render_widget(hass, check_readings=True), lock
        _normal_states(hass)
        _set(hass, "binary_sensor.hood", "unavailable")
        assert "All good" in _render_widget(
            hass, check_readings=True, optional_readings=[f"binary_sensor.{PREFIX}_hood"])
    _hass_run(check)


def test_a_widget_with_an_age_set_rejects_stale_missing_and_future_reports():
    async def check(hass):
        from homeassistant.util import dt as dt_util
        for reported in ("unavailable", _ago(days=1),
                         (dt_util.utcnow() + timedelta(hours=1)).isoformat()):
            _normal_states(hass, reported=reported)
            _set(hass, "sensor.battery", "10")
            rendered = _render_widget(hass, report_max_age_seconds=3600)
            assert "Unable to check" in rendered and "worth plugging in" in rendered, rendered
        _normal_states(hass, reported=_ago(minutes=5))
        assert "All good" in _render_widget(hass, report_max_age_seconds=3600)
        # A quoted age works here as it does in the bedtime check.
        _normal_states(hass, reported=_ago(days=1))
        assert "Unable to check" in _render_widget(hass, report_max_age_seconds="3600")
        _normal_states(hass, reported=_ago(minutes=5))
        assert "All good" in _render_widget(hass, report_max_age_seconds="3600")
    _hass_run(check)


def test_the_tire_warning_still_reads_the_sensors_psi_attribute():
    async def check(hass):
        for shown_unit in ("kPa", "psi", "bar"):
            _normal_states(hass)
            attributes = {**_tire(180), "unit_of_measurement": shown_unit}
            _set(hass, "sensor.tire_front_left", "180", attributes)
            rendered = _render_widget(hass)
            assert "Tire low" in rendered and "front left 26.1" in rendered, (shown_unit, rendered)
    _hass_run(check)


# ----------------------------------------------------------------- bedtime

def test_a_default_bedtime_check_ends_its_wait_on_last_updated():
    """#118 review: the Refresh button awaits its own poll, so Last Updated
    moves as soon as that poll returns; waiting on Car Reported At instead
    ran the whole wait on any night the car sent nothing new."""
    async def check(hass):
        _normal_states(hass)
        calls, took = await _execute(
            hass, _bedtime(), wait_s=3,
            refresh=lambda h: _later(h, 0.2, "sensor.last_updated", _ago(seconds=0)))
        assert _messages(calls) == [] and took < 1.5, (calls, took)
    _hass_run(check)


def test_a_default_bedtime_check_says_what_main_says():
    async def check(hass):
        _normal_states(hass)
        _set(hass, "lock.doors", "unlocked")
        calls, _ = await _execute(hass, _bedtime())
        assert _messages(calls) == ["Before you turn in: the doors are unlocked."], calls
    _hass_run(check)


def test_an_unreported_lock_is_silent_by_default_and_named_without_the_condition():
    """On a cloud-down night every entity goes unavailable together. Main
    stays quiet; an owner who deletes that condition is told what is missing."""
    async def check(hass):
        _normal_states(hass)
        _set(hass, "lock.doors", "unavailable")
        calls, _ = await _execute(hass, _bedtime())
        assert _messages(calls) == [], calls
        calls, _ = await _execute(hass, _bedtime(keep_lock_condition=False))
        assert len(_messages(calls)) == 1 and "No reading from the car for" in _messages(calls)[0]
        assert f"lock.{PREFIX}_doors" in _messages(calls)[0], calls
    _hass_run(check)


def test_removing_the_three_polling_steps_leaves_the_check_as_it_was():
    """The file invites an owner who would rather not poll the car at night to
    delete three steps: the poll_before variables, the button press and the
    wait. The age setting sits in a step of its own, so the report still finds
    both of its variables: the message is the one it always was, and Home
    Assistant has no undefined variable to warn about - a warning that, on a
    default install, would land in the log every night."""
    import logging

    class Seen(logging.Handler):
        def __init__(self):
            super().__init__(logging.WARNING)
            self.lines = []

        def emit(self, record):
            self.lines.append(record.getMessage())

    def without_polling(*, and_the_setting=False):
        config = _bedtime()
        kept = [a for a in config["actions"]
                if "poll_before" not in a.get("variables", {})
                and a.get("action") != "button.press" and "wait_template" not in a]
        assert len(config["actions"]) - len(kept) == 3, config["actions"]
        if and_the_setting:
            kept = [a for a in kept if "age_check" not in a.get("variables", {})]
        config["actions"] = kept
        return config

    async def check(hass):
        log = logging.getLogger("homeassistant.helpers.template")
        seen = Seen()
        log.addHandler(seen)
        try:
            _normal_states(hass)
            _set(hass, "lock.doors", "unlocked")
            calls, _ = await _execute(hass, without_polling())
            assert _messages(calls) == ["Before you turn in: the doors are unlocked."], calls
            assert not any(domain == "button" for domain, _, _ in calls), calls
            assert seen.lines == [], seen.lines
            # The capture is live: with the setting's own step deleted as well,
            # the warning this guards against does appear.
            await _execute(hass, without_polling(and_the_setting=True))
            assert any("age_check" in line for line in seen.lines), seen.lines
        finally:
            log.removeHandler(seen)
    _hass_run(check)


def test_with_an_age_set_the_wait_is_for_a_report_from_the_car():
    """Last Updated moves at once; the car's own report arrives later. Waiting
    on Last Updated would judge the 9 h old report and warn."""
    async def check(hass):
        _normal_states(hass)

        def poll_then_report(h):
            _set(h, "sensor.last_updated", _ago(seconds=0))
            _later(h, 0.3, "sensor.car_reported_at", _ago(seconds=0))
        calls, _ = await _execute(hass, _bedtime(report_max_age_seconds=3600),
                                  refresh=poll_then_report, wait_s=3)
        assert _messages(calls) == [], calls
    _hass_run(check)


def test_with_an_age_set_a_report_already_inside_it_needs_no_newer_one():
    async def check(hass):
        _normal_states(hass, reported=_ago(minutes=10))
        calls, took = await _execute(hass, _bedtime(report_max_age_seconds=3600), wait_s=3)
        assert _messages(calls) == [] and took < 1.5, (calls, took)
    _hass_run(check)


def test_with_an_age_set_a_stale_missing_or_future_report_is_named_with_both_times():
    async def check(hass):
        from homeassistant.util import dt as dt_util
        for reported in ("unavailable", _ago(hours=9),
                         (dt_util.utcnow() + timedelta(hours=1)).isoformat()):
            _normal_states(hass, reported=reported)
            calls, _ = await _execute(hass, _bedtime(report_max_age_seconds=3600))
            messages = _messages(calls)
            assert len(messages) == 1, calls
            assert messages[0].startswith("Could not verify: no report from the car within 3600 s")
            assert "Car report:" in messages[0] and "cloud contact:" in messages[0], messages
    _hass_run(check)


def test_a_quoted_age_works_like_a_number():
    async def check(hass):
        for reported, warned in ((_ago(minutes=10), False), (_ago(hours=9), True)):
            _normal_states(hass, reported=reported)
            calls, _ = await _execute(hass, _bedtime(report_max_age_seconds="3600"))
            assert bool(_messages(calls)) is warned, (reported, calls)
    _hass_run(check)
