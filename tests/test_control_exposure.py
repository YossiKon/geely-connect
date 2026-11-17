"""Advertised actions reach the selected car, through real HA/Chromium paths.

No cloud calls: API clients record requests; all vehicle identities are fake.
"""
import asyncio
from datetime import timedelta
from pathlib import Path
import types

from conftest import load
from test_platform_commands import _bundle, _setup_buttons, _by_uid, _need_ha, RecorderApi
from test_zeekr_adapter import _make_adapter
from test_zeekr_capabilities import _row
from test_init_lifecycle import _mod, _service, _FakeApi, _Patched
from test_cards_in_a_browser import _evaluate
from test_audit_examples_safety import _hass_run

ROOT = Path(__file__).resolve().parent.parent


def _catalog(fid, **kwargs):
    return {"functionId": fid, "valueEnable": True, **kwargs}


def test_flash_requires_a_lights_only_declaration_and_sends_no_horn_parameter():
    _need_ha()
    caps = load("capabilities")
    for catalogue, expected in (
        ([], False),
        ([_catalog("honk_flash")], False),
        ([_catalog("honk_flash", valueEnum="honk_flash,honk")], False),
        ([_catalog("honk_flash", valueEnum="honk_flash,flash,honk")], True),
        ([{**_catalog("honk_flash", valueEnum="flash"), "valueEnable": False}], False),
    ):
        api = RecorderApi()
        _, entities = _setup_buttons(_bundle(api=api, caps=caps.parse(catalogue)))
        buttons = [b for b in entities if b.unique_id.endswith("_btn_flash_lights")]
        assert bool(buttons) == expected
        if expected:
            assert buttons[0].translation_key == "flash_lights"
            asyncio.run(buttons[0].async_press())
            assert api.calls == [("RHL", [{"key": "rhl", "value": "light-flash"}], "start", 0)]


def test_em_catalogue_to_button_to_wire_preserves_the_exact_captured_targets():
    _need_ha()
    ad, cap = load("zeekr_adapter"), load("capabilities")
    for flash_enabled in (False, True):
        rows = [_row("C_RDU_2_3"), _row("C_RDU_2_1"),
                _row("C_RHL_1"), _row("C_RHL_2", use="Y" if flash_enabled else "N")]
        view = cap.parse(ad.translate_capabilities(rows))
        api, client = _make_adapter()
        client.enc_vin = "fake-vehicle-token"
        sent = []
        client.control_new_resp = lambda sid, cmd, params=None: (
            sent.append((sid, cmd, params)) or {"code": "000000", "data": {}})
        _, entities = _setup_buttons(_bundle(api=api, caps=view))
        asyncio.run(_by_uid(entities, "btn_unlock_frunk").async_press())
        assert sent == [("RDU", "start", [{"key": "target", "value": "hood"}])]
        if flash_enabled:
            asyncio.run(_by_uid(entities, "btn_flash_lights").async_press())
            assert sent[-1] == ("RHL", "start", [{"key": "rhl", "value": "light-flash"}])
        else:
            assert not any(b.unique_id.endswith("_btn_flash_lights") for b in entities)
    for rows in ([], [_row("C_RDU_2_3")],
                 [_row("C_RDU_2_3"), _row("C_RDU_2_1", use="N")]):
        view = cap.parse(ad.translate_capabilities(rows))
        assert not view.get("frunk.enabled")


def test_an_e2_keeps_its_own_frunk_request_even_with_a_second_unlock_row():
    _need_ha()
    caps = load("capabilities").parse([
        _catalog("remote_lock_unlock", valueEnum="Front_trunk"),
        _catalog("remote_control_unlock_2", valueEnum="hood"),
    ])
    api = RecorderApi()
    _, entities = _setup_buttons(_bundle(api=api, caps=caps))
    asyncio.run(_by_uid(entities, "btn_unlock_frunk").async_press())
    assert api.calls == [("rlu", [{"key": "operation", "value": "1"},
                                 {"key": "rlu.target", "value": "1"}], "start", 0)]
    disabled = load("capabilities").parse([
        {**_catalog("remote_control_unlock_2", valueEnum="hood"), "valueEnable": False}])
    assert not disabled.get("frunk.enabled")


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


def test_new_card_actions_render_only_for_the_selected_vehicle_and_dispatch():
    got = _evaluate("""async () => {
      const hass = mkHass({});
      const add = (id) => { hass.states[id] = {entity_id:id, state:'unknown', attributes:{}}; };
      // Another car's buttons must not create controls on this car.
      add('button.other_flash_lights'); add('button.other_unlock_frunk');
      const el = document.createElement('geely-card');
      document.body.append(el); el.setConfig({prefix:'car', cooldown:0}); el.hass=hass;
      const count = () => el.shadowRoot.querySelectorAll('[data-act="flash"], [data-act="frunk"]').length;
      const absent=count();
      add('button.car_flash_lights'); add('button.car_unlock_frunk');
      el.hass={...hass}; // Entity appearance must itself trigger rendering.
      const present=count();
      el.shadowRoot.querySelector('[data-act="flash"]').click();
      await Promise.resolve();
      el.shadowRoot.querySelector('[data-act="frunk"]').click();
      const first=hass.serviceCalls.length;
      el.shadowRoot.querySelector('[data-act="frunk"]').click();
      await Promise.resolve();
      const calls=[...hass.serviceCalls];
      el.setConfig({prefix:'car', hide:['flash','frunk']});
      const hidden=count();
      const small=[];
      for (const tag of ['geely-card-compact','geely-card-mini','geely-card-strip']) {
        const c=document.createElement(tag); document.body.append(c);
        c.setConfig({prefix:'car', buttons:['flash','frunk']}); c.hass=hass;
        small.push(c.shadowRoot.querySelectorAll('[data-act="flash"], [data-act="frunk"]').length);
      }
      hass.states['sensor.car_speed'].state='25'; el.setConfig({prefix:'car'}); el.hass={...hass};
      const blocked=[...el.shadowRoot.querySelectorAll('[data-act="flash"], [data-act="frunk"]')].every(b=>b.disabled);
      return {absent,present,first,calls,hidden,small,blocked};
    }""")
    assert got == {"absent": 0, "present": 2, "first": 1,
                   "calls": [["button", "press", {"entity_id": "button.car_flash_lights"}],
                             ["button", "press", {"entity_id": "button.car_unlock_frunk"}]],
                   "hidden": 0, "small": [2, 2, 2], "blocked": True}, got


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
        hass.states.async_set("input_datetime.departure", future, {"has_date": True, "has_time": True})
        await run()
        assert calls == [{"entity_id": "sensor.renamed_car_b", "command": "start", "departure": future}]
        hass.states.async_set("input_datetime.departure", "unavailable")
        await run("stop")
        assert calls[-1] == {"entity_id": "sensor.renamed_car_b", "command": "stop"}
        for value, attrs, command in (("unavailable", {}, "start"),
                                      ("2000-01-01 07:00:00", {"has_date": True, "has_time": True}, "start"),
                                      (future, {"has_date": True, "has_time": False}, "start"),
                                      (future, {"has_date": True, "has_time": True}, "invalid")):
            hass.states.async_set("input_datetime.departure", value, attrs)
            await run(command)
            assert len(calls) == 2, "invalid departure/command sent a request"
        assert len(calls) == 2
    _hass_run(check)
