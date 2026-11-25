"""Execute shipped climate examples against HA's real templates/service handler.

No live HA instance, cloud account or vehicle is contacted. The service adapter
records HA's native-Celsius result instead of sending a remote command.
"""
import asyncio
import re
from pathlib import Path
from tempfile import TemporaryDirectory

from conftest import have_homeassistant
from run import skip


ROOT = Path(__file__).resolve().parents[1]
CLIMATE = "climate.my_geely_ex5_climate"


def _ha():
    if not have_homeassistant():
        skip("homeassistant not installed")


def _blueprint(name, unit=None, target=None):
    from homeassistant.components.blueprint.models import Blueprint, BlueprintInputs
    from homeassistant.components.blueprint.schemas import BLUEPRINT_SCHEMA
    from homeassistant.util.yaml import loader

    path = ROOT / "blueprints" / "automation" / "geely_connect" / f"{name}.yaml"
    bp = Blueprint(loader.load_yaml(str(path)), expected_domain="automation",
                   path=str(path), schema=BLUEPRINT_SCHEMA)
    given = {"climate_entity": CLIMATE}
    if unit is not None:
        given["climate_temperature_unit"] = unit
    if name == "precondition":
        given["departure_time"] = "07:00:00"
    else:
        given["departure_datetime"] = "input_datetime.departure"
    if target is not None:
        given["target_temp"] = target
    inputs = BlueprintInputs(bp, {"use_blueprint": {"path": str(path), "input": given}})
    inputs.validate()
    return inputs.async_substitute()


def _comfort():
    from homeassistant.util.yaml import loader
    return {entry["id"]: entry for entry in loader.load_yaml(
        str(ROOT / "automations" / "comfort-and-maintenance.yaml"))}


def _temperature_service():
    """Home Assistant's own climate.set_temperature handler and schema.

    Up to 2026.9 both are in the climate package; from 2026.10 they are in
    its services module, where the handler is private."""
    try:
        from homeassistant.components.climate import (
            SET_TEMPERATURE_SCHEMA, async_service_temperature_set)
    except ImportError:
        from homeassistant.components.climate.services import (
            SET_TEMPERATURE_SCHEMA,
            _async_service_temperature_set as async_service_temperature_set)
    return async_service_temperature_set, SET_TEMPERATURE_SCHEMA


async def _with_hass(unit, check):
    from homeassistant.core import HomeAssistant
    from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM

    with TemporaryDirectory(prefix="geely-climate-units-") as directory:
        hass = HomeAssistant(directory)
        hass.config.units = METRIC_SYSTEM if unit == "°C" else US_CUSTOMARY_SYSTEM
        try:
            await check(hass)
        finally:
            await hass.async_stop(force=True)


async def _run_temperature_actions(hass, config, expected):
    from homeassistant.components.climate import ClimateEntity, ClimateEntityFeature
    from homeassistant.const import UnitOfTemperature
    from homeassistant.core import Context
    from homeassistant.helpers import config_validation as cv
    from homeassistant.helpers.script import Script
    from homeassistant.util.unit_conversion import TemperatureConverter
    async_service_temperature_set, _ = _temperature_service()

    class Recorder(ClimateEntity):
        # Both names of the unit, as GeelyClimate gives them.
        _attr_native_temperature_unit = UnitOfTemperature.CELSIUS
        _attr_temperature_unit = UnitOfTemperature.CELSIUS
        _attr_min_temp = 15.5
        _attr_max_temp = 28.5
        _attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE

        async def async_set_temperature(self, **kwargs):
            native.append(kwargs["temperature"])

    native, service_values = [], []
    entity = Recorder()
    entity.hass = hass

    async def set_temperature(call):
        service_values.append(call.data["temperature"])
        # Real HA handler converts display units and checks the vehicle range.
        await async_service_temperature_set(entity, call)

    hass.services.async_register("climate", "set_temperature", set_temperature)
    # Execute the shipped setpoint sequence. Presets, the 30-second vehicle
    # settling delay and phone delivery are outside this unit regression.
    actions = [step for step in config.get("actions", config.get("action", []))
               if (step.get("action") or step.get("service")) == "climate.set_temperature"]
    assert len(actions) == 1
    variables = cv.SCRIPT_VARIABLES_SCHEMA(
        config.get("variables", {})).async_render(hass, {})
    sequence = cv.SCRIPT_SCHEMA(actions)
    script = Script(hass, sequence, "shipped climate temperature", "automation")
    await script.async_run(variables, context=Context())
    assert len(native) == 1
    assert abs(native[0] - expected) < 1e-8, (service_values, native, expected)
    expected_service = TemperatureConverter.convert(
        expected, "°C", hass.config.units.temperature_unit)
    assert abs(service_values[0] - expected_service) < 1e-8, service_values


def test_blueprints_send_celsius_default_through_both_ha_service_units():
    _ha()

    async def check(hass):
        unit = hass.config.units.temperature_unit
        for name in ("precondition", "departure"):
            # Celsius users can retain every shipped default.
            await _run_temperature_actions(
                hass, _blueprint(name, None if unit == "°C" else unit), 22)

    for unit in ("°C", "°F"):
        asyncio.run(_with_hass(unit, check))


def test_readme_warmup_documented_units_reach_native_22_celsius():
    _ha()
    from homeassistant.util.yaml import loader

    section = (ROOT / "README.md").read_text(encoding="utf-8").split(
        "**Warm up the car on weekday mornings**", 1)[1].split(
        "**Auto-lock when you leave home**", 1)[0]
    snippet = section.split("```yaml\n", 1)[1].split("```", 1)[0]
    fahrenheit = float(re.search(
        r"change it to `([0-9.]+)` for Fahrenheit", section).group(1))

    async def check(hass):
        config = loader.parse_yaml(snippet)["automation"][0]
        if hass.config.units.temperature_unit == "°F":
            config["action"][0]["data"]["temperature"] = fahrenheit
        await _run_temperature_actions(hass, config, 22)

    for unit in ("°C", "°F"):
        asyncio.run(_with_hass(unit, check))


def test_blueprints_keep_custom_celsius_targets_and_range_endpoints():
    _ha()

    async def check(hass):
        for name in ("precondition", "departure"):
            for target in (15.5, 21.5, 28.5):
                await _run_temperature_actions(
                    hass, _blueprint(name, hass.config.units.temperature_unit, target), target)

    for unit in ("°C", "°F"):
        asyncio.run(_with_hass(unit, check))


def test_comfort_setpoints_use_ha_units_independently_of_sensor_units():
    _ha()

    async def check(hass):
        configs = _comfort()
        for name in ("geely_preheat_weekday_morning", "geely_precool_hot_cabin"):
            config = configs[name]
            # This is the documented user setting; targets remain Celsius.
            config["variables"]["climate_temperature_unit"] = hass.config.units.temperature_unit
            for sensor_unit, reading in (("°C", "5"), ("°F", "41")):
                hass.states.async_set("sensor.my_geely_ex5_exterior_temperature", reading,
                                      {"unit_of_measurement": sensor_unit})
                for target in (15.5, 22, 28.5):
                    config["variables"]["target_temp_c"] = target
                    await _run_temperature_actions(hass, config, target)

    for unit in ("°C", "°F"):
        asyncio.run(_with_hass(unit, check))


def test_comfort_celsius_thresholds_use_sensor_units_and_strict_boundaries():
    _ha()

    async def check(hass):
        from homeassistant.helpers import condition, config_validation as cv
        from homeassistant.util.unit_conversion import TemperatureConverter

        configs = _comfort()
        cold = next(c for c in configs["geely_preheat_weekday_morning"]["conditions"]
                    if c["condition"] == "numeric_state")
        hot, = configs["geely_precool_hot_cabin"]["triggers"]
        for config, samples in ((cold, ((7, True), (8, False), (9, False))),
                                (hot, ((34, False), (35, False), (36, True)))):
            for sensor_unit in ("°C", "°F"):
                for celsius, expected in samples:
                    reading = TemperatureConverter.convert(celsius, "°C", sensor_unit)
                    hass.states.async_set(config["entity_id"], str(reading),
                                          {"unit_of_measurement": sensor_unit})
                    value_template = (cv.template(config["value_template"])
                                      if "value_template" in config else None)
                    actual = condition.async_numeric_state(
                        hass, config["entity_id"], above=config.get("above"),
                        below=config.get("below"), value_template=value_template)
                    assert actual == expected, (config["entity_id"], sensor_unit, reading, actual)
            # Missing, non-numeric or unsupported-unit readings must not be
            # treated as a very cold cabin and fire a remote command.
            for sensor_unit, reading in (("°C", "unavailable"), ("°F", "unknown"),
                                         ("°F", "bad"), (None, "5"), ("K", "5")):
                hass.states.async_set(config["entity_id"], reading,
                                      {"unit_of_measurement": sensor_unit})
                assert not condition.async_numeric_state(
                    hass, config["entity_id"], above=config.get("above"),
                    below=config.get("below"), value_template=value_template)

    for unit in ("°C", "°F"):
        asyncio.run(_with_hass(unit, check))


def test_comfort_notifications_label_the_actual_sensor_unit():
    _ha()

    async def check(hass):
        from homeassistant.helpers.template import Template
        from homeassistant.helpers import config_validation as cv

        configs = _comfort()
        for config, sensor in ((configs["geely_preheat_weekday_morning"], "exterior"),
                               (configs["geely_precool_hot_cabin"], "interior")):
            message = config["actions"][-1]["data"]["message"]
            for unit, reading in (("°C", "5"), ("°F", "41")):
                hass.states.async_set(f"sensor.my_geely_ex5_{sensor}_temperature", reading,
                                      {"unit_of_measurement": unit})
                variables = cv.SCRIPT_VARIABLES_SCHEMA(config["variables"]).async_render(hass, {})
                rendered = Template(message, hass).async_render(variables)
                assert f"{reading} {unit}" in rendered, rendered
                assert "22 C" in rendered, rendered

    asyncio.run(_with_hass("°F", check))


def test_shipped_climate_templates_validate_in_ha_automation_schemas():
    _ha()

    async def check(hass):
        from homeassistant.components.automation.config import PLATFORM_SCHEMA
        from homeassistant.components.homeassistant.triggers.numeric_state import (
            async_validate_trigger_config)

        configs = _comfort()
        for name in ("precondition", "departure"):
            PLATFORM_SCHEMA(_blueprint(name, hass.config.units.temperature_unit))
        for name in ("geely_preheat_weekday_morning", "geely_precool_hot_cabin"):
            PLATFORM_SCHEMA(configs[name])
        # The automation's top schema delegates trigger contents to platforms.
        hot = PLATFORM_SCHEMA(configs["geely_precool_hot_cabin"])
        await async_validate_trigger_config(hass, hot["triggers"][0])

    asyncio.run(_with_hass("°F", check))


def test_the_shipped_unit_is_celsius_and_the_blueprints_offer_what_the_templates_compare():
    """A paste left as shipped behaves as a Celsius install, and a blueprint can
    only offer a unit its own template recognises."""
    _ha()
    from homeassistant.util.yaml import loader

    configs = _comfort()
    for name in ("geely_preheat_weekday_morning", "geely_precool_hot_cabin"):
        assert configs[name]["variables"]["climate_temperature_unit"] == "°C", name
    for name in ("precondition", "departure"):
        path = ROOT / "blueprints" / "automation" / "geely_connect" / f"{name}.yaml"
        unit = loader.load_yaml(str(path))["blueprint"]["input"]["climate_temperature_unit"]
        assert unit["default"] == "°C", name
        assert unit["selector"]["select"]["options"] == ["°C", "°F"], name
        assert "climate_temperature_unit == '°F'" in path.read_text(encoding="utf-8"), name


async def _run_preheat(hass, unit_setting, target=22, low=15.5, high=28.5):
    """The pre-heat actions, delays left out, against Home Assistant's own
    climate service handler and a climate state shown the way HA shows it:
    a climate entity whose native range is `low`-`high` C. A setpoint the
    handler refuses ends the run, recorded as REFUSED after what went out."""
    from homeassistant.components.climate import ClimateEntity, ClimateEntityFeature
    from homeassistant.const import PRECISION_TENTHS, PRECISION_WHOLE, UnitOfTemperature
    from homeassistant.core import Context
    from homeassistant.helpers import config_validation as cv
    from homeassistant.helpers.script import Script
    from homeassistant.helpers.temperature import display_temp
    async_service_temperature_set, SET_TEMPERATURE_SCHEMA = _temperature_service()

    class Recorder(ClimateEntity):
        # Both names of the unit, as GeelyClimate gives them.
        _attr_native_temperature_unit = UnitOfTemperature.CELSIUS
        _attr_temperature_unit = UnitOfTemperature.CELSIUS
        _attr_min_temp = low
        _attr_max_temp = high
        _attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE

        async def async_set_temperature(self, **kwargs):
            sent.append(("setpoint", round(kwargs["temperature"], 6)))

    sent = []
    entity = Recorder()
    entity.hass = hass
    precision = (PRECISION_TENTHS if hass.config.units.temperature_unit == "°C"
                 else PRECISION_WHOLE)
    hass.states.async_set(CLIMATE, "off", {
        "min_temp": display_temp(hass, low, "°C", precision),
        "max_temp": display_temp(hass, high, "°C", precision)})

    async def set_temperature(call):
        await async_service_temperature_set(entity, call)

    async def record(call):
        sent.append((call.service, call.data.get("preset_mode")))

    for service in ("set_temperature", "set_preset_mode"):
        if hass.services.has_service("climate", service):
            hass.services.async_remove("climate", service)
    hass.services.async_register("climate", "set_temperature", set_temperature,
                                 schema=SET_TEMPERATURE_SCHEMA)
    hass.services.async_register("climate", "set_preset_mode", record)
    if not hass.services.has_service("notify", "mobile_app_your_phone"):
        hass.services.async_register("notify", "mobile_app_your_phone", lambda call: None)
    hass.states.async_set("sensor.my_geely_ex5_exterior_temperature", "5",
                          {"unit_of_measurement": "°C"})

    config = _comfort()["geely_preheat_weekday_morning"]
    config["variables"]["climate_temperature_unit"] = unit_setting
    config["variables"]["target_temp_c"] = target
    steps = [step for step in config["actions"] if "delay" not in step]
    variables = cv.SCRIPT_VARIABLES_SCHEMA(config["variables"]).async_render(hass, {})
    script = Script(hass, cv.SCRIPT_SCHEMA(steps), "shipped pre-heat", "automation")
    try:
        await script.async_run(variables, context=Context())
    except Exception as err:  # noqa: BLE001 - what went out before a refusal is the point
        sent.append(("REFUSED", type(err).__name__))
    return sent


def test_preheat_sends_no_rapid_warming_when_the_unit_setting_does_not_match():
    """Rapid Warming goes out before the setpoint. With the unit left at °C on
    a Fahrenheit install, or set to °F on a Celsius one, the setpoint is
    refused; the run used to send the preset first and stop on the refusal.
    It now stops before the preset. Set right, both go out."""
    _ha()
    want = [("set_preset_mode", "Rapid Warming"), ("setpoint", 22.0)]
    for install, setting, expected in (("°F", "°C", []), ("°F", "°F", want),
                                       ("°C", "°F", []), ("°C", "°C", want)):
        async def check(hass, setting=setting, expected=expected):
            got = await _run_preheat(hass, setting)
            assert got == expected, (hass.config.units.temperature_unit, setting, got)

        asyncio.run(_with_hass(install, check))


def test_preheat_reaches_both_ends_of_the_range_on_fahrenheit():
    """Home Assistant shows the ends of the range in whole degrees on a
    Fahrenheit install, and converts the target back to Celsius on its way
    in: both ends of the shipped 15.5-28.5 range, and of a 16-28 entity (the
    whole-degree request's), get through the guard and the range check."""
    _ha()
    for target, low, high in ((15.5, 15.5, 28.5), (28.5, 15.5, 28.5),
                              (16, 16.0, 28.0), (28, 16.0, 28.0)):
        async def check(hass, target=target, low=low, high=high):
            got = await _run_preheat(hass, "°F", target, low, high)
            assert got == [("set_preset_mode", "Rapid Warming"),
                           ("setpoint", float(target))], (target, low, high, got)

        asyncio.run(_with_hass("°F", check))


def test_preheat_sends_nothing_for_a_target_outside_the_range_on_celsius():
    """On a Celsius install the range is shown as it is, so the guard takes no
    slack: half a degree outside it sends neither the preset nor the target."""
    _ha()
    for target, low, high in ((29, 15.5, 28.5), (15, 15.5, 28.5),
                              (15.5, 16.0, 28.0), (28.5, 16.0, 28.0)):
        async def check(hass, target=target, low=low, high=high):
            got = await _run_preheat(hass, "°C", target, low, high)
            assert got == [], (target, low, high, got)

        asyncio.run(_with_hass("°C", check))
