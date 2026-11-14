"""Render shipped Jinja and execute substituted examples in offline HA."""
import asyncio
import copy
from datetime import timedelta
from pathlib import Path
import tempfile

from conftest import have_homeassistant
from run import skip


ROOT = Path(__file__).resolve().parent.parent
PREFIX = "my_geely_ex5"


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


def _normal_states(hass):
    from homeassistant.util import dt as dt_util
    readings = {
        "binary_sensor.connected": "on", "lock.doors": "locked",
        "sensor.battery": "80", "sensor.12v_battery": "90",
        "sensor.days_to_service": "150", "sensor.distance_to_service": "5000",
        "cover.all_windows": "closed", "cover.sunroof": "closed",
        "sensor.last_updated": dt_util.utcnow().isoformat(),
        "sensor.car_reported_at": (dt_util.utcnow() - timedelta(seconds=20)).isoformat(),
    }
    for suffix in ("door_driver", "door_passenger", "door_rear_left",
                   "door_rear_right", "trunk", "hood"):
        readings[f"binary_sensor.{suffix}"] = "off"
    for suffix in ("tire_front_left", "tire_front_right", "tire_rear_left", "tire_rear_right"):
        readings[f"sensor.{suffix}"] = "240"
    for short, value in readings.items():
        domain, suffix = short.split(".")
        hass.states.async_set(f"{domain}.{PREFIX}_{suffix}", value,
                              {"unit_of_measurement": "kPa"} if suffix.startswith("tire_") else {})


def _render_widget(hass, source=None):
    from homeassistant.helpers.template import Template
    source = source or (ROOT / "android-widgets/attention.jinja").read_text()
    return Template(source, hass).async_render(parse_result=False)


def _automation(filename, automation_id):
    from homeassistant.util.yaml import loader
    return copy.deepcopy(next(a for a in loader.load_yaml(str(ROOT / "automations" / filename))
                              if a["id"] == automation_id))


async def _execute(hass, config, *, refresh=None):
    """Real HA schema/Script; replace remote services and long waits only."""
    from homeassistant.core import Context
    from homeassistant.components.automation.config import PLATFORM_SCHEMA
    from homeassistant.helpers.script import Script

    calls = []

    async def record(call):
        calls.append((call.domain, call.service, dict(call.data)))
        if call.domain == "button" and refresh:
            refresh(hass)

    for domain, service in (("button", "press"), ("notify", "mobile_app_your_phone"),
                            ("climate", "set_preset_mode"), ("climate", "set_temperature")):
        hass.services.async_register(domain, service, record)

    def shorten(node):
        if isinstance(node, dict):
            if "wait_template" in node:
                node["timeout"] = {"milliseconds": 1}
            if "delay" in node:
                node["delay"] = {"milliseconds": 1}
            for value in node.values():
                shorten(value)
        elif isinstance(node, list):
            for value in node:
                shorten(value)

    shorten(config)
    config = PLATFORM_SCHEMA(config)
    script = Script(hass, config["actions"], "audit example", "audit_example")
    await script.async_run(context=Context())
    return calls


def _messages(calls):
    return [data["message"] for domain, _, data in calls if domain == "notify"]


def test_hot_cabin_example_executes_only_ac_temperature_and_a_request_notification():
    async def check(hass):
        hass.states.async_set(f"sensor.{PREFIX}_interior_temperature", "38")
        calls = await _execute(hass, _automation("comfort-and-maintenance.yaml", "geely_precool_hot_cabin"))
        commands = [(d, s, data) for d, s, data in calls if d == "climate"]
        assert commands == [("climate", "set_temperature", {
            "entity_id": [f"climate.{PREFIX}_climate"], "temperature": 22})], commands
        assert "requested" in _messages(calls)[0].lower(), calls
    _hass_run(check)


def test_widget_missing_or_unavailable_readings_cannot_render_all_good():
    async def check(hass):
        for state in (None, "unknown", "unavailable"):
            _normal_states(hass)
            for entity in list(hass.states.async_entity_ids()):
                if state is None:
                    hass.states.async_remove(entity)
                else:
                    hass.states.async_set(entity, state)
            rendered = _render_widget(hass)
            assert "All good" not in rendered, (state, rendered)
            assert "Unable to check" in rendered, (state, rendered)
    _hass_run(check)


def test_widget_partial_missing_security_and_numeric_fields_are_explicit():
    async def check(hass):
        for entity, state in (("lock.doors", "locking"), ("binary_sensor.connected", "unknown"),
                              ("binary_sensor.hood", "unavailable"), ("sensor.battery", "bad"),
                              ("sensor.tire_front_left", "unavailable"), ("sensor.12v_battery", "0")):
            _normal_states(hass)
            domain, suffix = entity.split(".")
            hass.states.async_set(f"{domain}.{PREFIX}_{suffix}", state)
            rendered = _render_widget(hass)
            assert "All good" not in rendered, (entity, rendered)
            assert "Unable to check" in rendered, (entity, rendered)
    _hass_run(check)


def test_widget_rejects_stale_missing_and_future_vehicle_reports_but_keeps_warnings():
    async def check(hass):
        from homeassistant.util import dt as dt_util
        for timestamp in ("unavailable", (dt_util.utcnow()-timedelta(days=1)).isoformat(),
                          (dt_util.utcnow()+timedelta(hours=1)).isoformat()):
            _normal_states(hass)
            hass.states.async_set(f"sensor.{PREFIX}_car_reported_at", timestamp)
            hass.states.async_set(f"sensor.{PREFIX}_battery", "10")
            rendered = _render_widget(hass)
            assert "All good" not in rendered, rendered
            assert "Unable to check" in rendered and "worth plugging in" in rendered, rendered
    _hass_run(check)


def test_widget_complete_recent_readings_and_explicit_optional_exclusion():
    async def check(hass):
        _normal_states(hass)
        assert "All good" in _render_widget(hass)
        hass.states.async_remove(f"sensor.{PREFIX}_distance_to_service")
        assert "All good" not in _render_widget(hass)
        source = (ROOT / "android-widgets/attention.jinja").read_text().replace(
            "set optional_readings = []", f"set optional_readings = ['sensor.{PREFIX}_distance_to_service']")
        assert "All good" in _render_widget(hass, source)
    _hass_run(check)


def test_widget_tire_pressure_uses_actual_ha_units_and_marks_unreadable_tpms():
    async def check(hass):
        for unit, value in (("kPa", "180"), ("psi", "26"), ("bar", "1.8")):
            _normal_states(hass)
            hass.states.async_set(f"sensor.{PREFIX}_tire_front_left", value,
                                  {"unit_of_measurement": unit})
            rendered = _render_widget(hass)
            assert "Tire low" in rendered and "front left" in rendered, rendered
        for unit, value in ((None, "240"), ("kPa", "0"), ("kPa", "unknown")):
            _normal_states(hass)
            hass.states.async_set(f"sensor.{PREFIX}_tire_front_left", value,
                                  {"unit_of_measurement": unit})
            rendered = _render_widget(hass)
            assert "All good" not in rendered and "Unable to check" in rendered, rendered
    _hass_run(check)


def test_bedtime_cloud_poll_without_a_new_vehicle_report_is_not_clear():
    async def check(hass):
        from homeassistant.util import dt as dt_util
        _normal_states(hass)
        hass.states.async_set(f"sensor.{PREFIX}_car_reported_at",
                              (dt_util.utcnow()-timedelta(hours=5)).isoformat())
        def cloud_only(h):
            h.states.async_set(f"sensor.{PREFIX}_last_updated", dt_util.utcnow().isoformat())
        calls = await _execute(hass, _automation("security.yaml", "geely_ex5_security_bedtime_check"),
                               refresh=cloud_only)
        messages = _messages(calls)
        assert len(messages) == 1 and "Could not verify" in messages[0], calls
    _hass_run(check)


def test_bedtime_timeout_missing_and_future_report_or_lock_always_notify():
    async def check(hass):
        from homeassistant.util import dt as dt_util
        for report, lock in (("unavailable", "locked"), ("unknown", "unavailable"),
                             ((dt_util.utcnow()+timedelta(hours=1)).isoformat(), "locked")):
            _normal_states(hass)
            hass.states.async_set(f"sensor.{PREFIX}_car_reported_at", report)
            hass.states.async_set(f"lock.{PREFIX}_doors", lock)
            calls = await _execute(hass, _automation("security.yaml", "geely_ex5_security_bedtime_check"))
            messages = _messages(calls)
            assert len(messages) == 1 and "Could not verify" in messages[0], calls
    _hass_run(check)


def test_bedtime_changed_but_stale_report_and_unchanged_recent_report_are_not_clear():
    async def check(hass):
        from homeassistant.util import dt as dt_util
        for advance in (False, True):
            _normal_states(hass)
            if advance:
                hass.states.async_set(f"sensor.{PREFIX}_car_reported_at",
                    (dt_util.utcnow()-timedelta(days=1)).isoformat())
            def refresh(h):
                if advance:
                    h.states.async_set(f"sensor.{PREFIX}_car_reported_at",
                        (dt_util.utcnow()-timedelta(hours=1)).isoformat())
            calls = await _execute(hass, _automation("security.yaml", "geely_ex5_security_bedtime_check"),
                                   refresh=refresh)
            assert len(_messages(calls)) == 1 and "Could not verify" in _messages(calls)[0], calls
    _hass_run(check)


def test_bedtime_no_wake_option_preserves_the_configured_freshness_policy():
    async def check(hass):
        for max_age in (300, 5):
            _normal_states(hass)
            config = _automation("security.yaml", "geely_ex5_security_bedtime_check")
            config["actions"][0]["variables"].update(request_refresh=False, report_max_age_seconds=max_age)
            calls = await _execute(hass, config)
            assert not any(domain == "button" for domain, _, _ in calls), calls
            if max_age == 300:
                assert _messages(calls) == [], calls
            else:
                assert "Could not verify" in _messages(calls)[0], calls
    _hass_run(check)


def test_bedtime_new_recent_vehicle_report_can_be_quiet_only_with_complete_readings():
    async def check(hass):
        from homeassistant.util import dt as dt_util
        def new_report(h):
            h.states.async_set(f"sensor.{PREFIX}_car_reported_at", dt_util.utcnow().isoformat())
        for missing in (None, "lock", "hood"):
            _normal_states(hass)
            if missing:
                entity = f"lock.{PREFIX}_doors" if missing == "lock" else f"binary_sensor.{PREFIX}_hood"
                hass.states.async_set(entity, "unavailable")
            calls = await _execute(hass, _automation("security.yaml", "geely_ex5_security_bedtime_check"),
                                   refresh=new_report)
            messages = _messages(calls)
            if missing:
                assert len(messages) == 1 and "No reading" in messages[0], calls
            else:
                assert messages == [], calls
    _hass_run(check)


def test_bedtime_reports_observed_problems_without_claiming_physical_confirmation():
    async def check(hass):
        from homeassistant.util import dt as dt_util
        _normal_states(hass)
        hass.states.async_set(f"lock.{PREFIX}_doors", "unlocked")
        calls = await _execute(hass, _automation("security.yaml", "geely_ex5_security_bedtime_check"),
            refresh=lambda h: h.states.async_set(f"sensor.{PREFIX}_car_reported_at", dt_util.utcnow().isoformat()))
        messages = _messages(calls)
        assert len(messages) == 1 and "doors are unlocked" in messages[0], calls
        assert "reported" in messages[0].lower(), messages
    _hass_run(check)
