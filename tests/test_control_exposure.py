"""Native departure actions reach the selected car through real HA paths.

No cloud calls: API clients record requests; all vehicle identities are fake.
"""
from datetime import timedelta
from pathlib import Path
import types

from test_platform_commands import _need_ha
from test_init_lifecycle import _mod, _service, _FakeApi, _Patched
from test_audit_examples_safety import _hass_run

ROOT = Path(__file__).resolve().parent.parent


def test_native_trip_entity_target_is_scoped_and_conflicts_never_send():
    _need_ha()
    from homeassistant.exceptions import ServiceValidationError
    m = _mod()
    for entity, vin, accepted in (
        (types.SimpleNamespace(platform="geely_connect", config_entry_id="b"), None, True),
        (types.SimpleNamespace(platform="geely_connect", config_entry_id="b"), "B", True),
        (types.SimpleNamespace(platform="geely_connect", config_entry_id="b"), "A", False),
        (types.SimpleNamespace(platform="other", config_entry_id="b"), None, False),
        (types.SimpleNamespace(platform="geely_connect", config_entry_id="unloaded"), None, False),
        (None, None, False),
    ):
        first, second = _FakeApi(), _FakeApi()
        bundles = {"a": {"api": first, "vin": "A"}, "b": {"api": second, "vin": "B"}}
        data = {"command": "stop", "entity_id": "sensor.renamed_car_b"}
        if vin:
            data["vin"] = vin
        _, run = _service(m, bundles, data, name="set_scheduled_trip")
        with _Patched(m, er=types.SimpleNamespace(async_get=lambda h:
                      types.SimpleNamespace(async_get=lambda eid: entity))):
            try:
                run()
            except ServiceValidationError:
                assert not accepted
            else:
                assert accepted
        assert first.calls == []
        assert bool(second.calls) == accepted


def test_native_departure_blueprint_executes_real_ha_start_stop_and_refusals():
    from homeassistant.components.blueprint.models import Blueprint, BlueprintInputs
    from homeassistant.components.blueprint.schemas import BLUEPRINT_SCHEMA
    from homeassistant.components.script.config import SCRIPT_ENTITY_SCHEMA
    from homeassistant.core import Context
    from homeassistant.helpers.script import Script
    from homeassistant.util import dt as dt_util
    from homeassistant.util.yaml import loader

    async def check(hass):
        path = str(ROOT / "blueprints/script/geely_connect/native_departure.yaml")
        bp = Blueprint(loader.load_yaml(path), expected_domain="script", path=path,
                       schema=BLUEPRINT_SCHEMA)
        inputs = BlueprintInputs(bp, {"use_blueprint": {"path": path, "input": {
            "vehicle": "sensor.renamed_car_b", "departure_helper": "input_datetime.departure"}}})
        inputs.validate()
        config = SCRIPT_ENTITY_SCHEMA(inputs.async_substitute())
        calls = []
        hass.services.async_register("geely_connect", "set_scheduled_trip",
                                     lambda call: calls.append(dict(call.data)))
        future = (dt_util.now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        async def run(command=None):
            script = Script(hass, config["sequence"], "Departure", "script",
                            variables=config["variables"])
            return await script.async_run({} if command is None else {"command": command}, Context())
        hass.states.async_set("input_datetime.departure", future, {"has_date": True, "has_time": True,
                              "timestamp": (dt_util.now() + timedelta(days=1)).timestamp()})
        await run()
        assert calls == [{"entity_id": "sensor.renamed_car_b", "command": "start", "departure": future}]
        hass.states.async_set("input_datetime.departure", "unavailable")
        await run("stop")
        assert calls[-1] == {"entity_id": "sensor.renamed_car_b", "command": "stop"}
        for value, attrs, command in (("unavailable", {}, "start"),
                                      ("2000-01-01 07:00:00", {"has_date": True, "has_time": True}, "start"),
                                      (future, {"has_date": True, "has_time": False}, "start"),
                                      (future, {"has_date": True, "has_time": True, "timestamp": (dt_util.now() + timedelta(days=1)).timestamp()}, "invalid")):
            hass.states.async_set("input_datetime.departure", value, attrs)
            await run(command)
            assert len(calls) == 2, "invalid departure/command sent a request"
        assert len(calls) == 2
    _hass_run(check)


def test_departure_guard_uses_the_helpers_ha_zone_when_host_is_utc():
    from homeassistant.components.blueprint.models import Blueprint, BlueprintInputs
    from homeassistant.components.blueprint.schemas import BLUEPRINT_SCHEMA
    from homeassistant.components.script.config import SCRIPT_ENTITY_SCHEMA
    from homeassistant.core import Context
    from homeassistant.helpers.script import Script
    from homeassistant.util import dt as dt_util
    from homeassistant.util.yaml import loader
    from zoneinfo import ZoneInfo

    async def check(hass):
        path = str(ROOT / "blueprints/script/geely_connect/native_departure.yaml")
        bp = Blueprint(loader.load_yaml(path), expected_domain="script", path=path, schema=BLUEPRINT_SCHEMA)
        config = SCRIPT_ENTITY_SCHEMA(BlueprintInputs(bp, {"use_blueprint": {"path": path, "input": {
            "vehicle": "sensor.car_b_battery", "departure_helper": "input_datetime.departure"}}}).async_substitute())
        calls = []
        hass.services.async_register("geely_connect", "set_scheduled_trip", lambda c: calls.append(dict(c.data)))
        previous = dt_util.DEFAULT_TIME_ZONE
        try:
            for zone, minutes, accepted in (("Australia/Sydney", -30, False),
                                             ("America/Los_Angeles", 30, True)):
                dt_util.set_default_time_zone(ZoneInfo(zone))
                departure = dt_util.now() + timedelta(minutes=minutes)
                text = departure.strftime("%Y-%m-%d %H:%M:%S")
                hass.states.async_set("input_datetime.departure", text, {
                    "has_date": True, "has_time": True, "timestamp": departure.timestamp()})
                before = len(calls)
                await Script(hass, config["sequence"], "Departure", "script",
                             variables=config["variables"]).async_run({}, Context())
                assert (len(calls) == before + 1) == accepted, (zone, calls)
                if accepted:
                    assert calls[-1]["departure"] == text
        finally:
            dt_util.set_default_time_zone(previous)
    _hass_run(check)
