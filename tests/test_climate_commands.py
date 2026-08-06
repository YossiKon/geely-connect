"""The climate entity's write paths and restore logic.

Everything here talks to a recorder API - the assertions are on the exact
params the car would receive, because a wrong key or an unformatted
temperature is silently ignored by the gateway and the user just sees a
climate command that "did nothing".
"""
import asyncio
import time
import types

from conftest import FAKE_VIN, have_homeassistant, load
from run import skip


def _ha():
    if not have_homeassistant():
        skip("homeassistant not installed")


class _Coord:
    def __init__(self, data=None):
        self.data = data or {}
        self.last_update_success = True

    def async_add_listener(self, cb, *a, **k):
        return lambda: None

    async def async_request_refresh(self):
        pass


class _Api:
    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail

    def control(self, service_id, params, command, duration):
        self.calls.append((service_id, params, command, duration))
        if self.fail is not None:
            raise self.fail
        return {"success": True}

    def rapid_climate(self, **kw):
        self.calls.append(("rapid", kw))
        if self.fail is not None:
            raise self.fail
        return {"success": True}


class _Hass:
    def __init__(self):
        self.data = {}
        from homeassistant.const import UnitOfTemperature
        self.config = types.SimpleNamespace(units=types.SimpleNamespace(
            temperature_unit=UnitOfTemperature.CELSIUS))

    async def async_add_executor_job(self, fn, *args):
        return fn(*args)

    def async_create_task(self, coro):
        coro.close()


def _status(interior=None, pre=None, defrost=None, exterior=None):
    clim = {}
    if interior is not None:
        clim["interiorTemp"] = interior
    if pre is not None:
        clim["preClimateActive"] = pre
    if defrost is not None:
        clim["defrost"] = defrost
    if exterior is not None:
        clim["exteriorTemp"] = exterior
    return {"vehicleStatus": {"additionalVehicleStatus": {"climateStatus": clim}}}


def _entity(data=None, fail=None, caps=None):
    c = load("climate")
    hass = _Hass()
    api = _Api(fail=fail)
    e = c.GeelyClimate(hass, {
        "coordinator": _Coord(data), "api": api, "vin": FAKE_VIN,
        "capabilities": caps or {}, "device_name": "Geely EX5 (0000)"})
    e.hass = hass
    e.async_write_ha_state = lambda: None
    return e, api, c


def _quiet_refresh(c):
    """Silence the post-command refresh scheduling for the duration."""
    class _Ctx:
        def __enter__(self):
            self.orig = c.schedule_refresh
            c.schedule_refresh = lambda *a, **k: None
            return self

        def __exit__(self, *exc):
            c.schedule_refresh = self.orig
    return _Ctx()


# --------------------------------------------------------------------- setup ---

def test_a_trim_without_ac_gets_no_climate_entity():
    """The capability catalog is the authority: shipping a dead thermostat to
    a trim whose gateway rejects every AC command is worse than nothing."""
    _ha()
    c = load("climate")
    hass = _Hass()
    hass.data["geely_connect"] = {"e1": {
        "coordinator": _Coord(), "api": _Api(), "vin": FAKE_VIN,
        "capabilities": {"ac.enabled": False}, "device_name": "X"}}
    entry = types.SimpleNamespace(entry_id="e1")
    got = []
    asyncio.run(c.async_setup_entry(hass, entry, lambda e, *a, **k: got.extend(list(e))))
    assert got == []
    hass.data["geely_connect"]["e1"]["capabilities"] = {}
    asyncio.run(c.async_setup_entry(hass, entry, lambda e, *a, **k: got.extend(list(e))))
    assert len(got) == 1


# --------------------------------------------------------------- read state ---

def test_hvac_mode_follows_preclimate_then_defrost_then_off():
    _ha()
    from homeassistant.components.climate import HVACMode
    for status, expected in (
            (_status(pre="1"), HVACMode.HEAT_COOL),
            (_status(defrost="1"), HVACMode.HEAT_COOL),
            (_status(), HVACMode.OFF)):
        e, _, _ = _entity(status)
        assert e.hvac_mode == expected, status


def test_the_optimistic_mode_wins_only_inside_its_window():
    """After a command the cloud takes ~30 s to reflect it; the UI must not
    flash Off in between, and must stop guessing once the window closes."""
    _ha()
    from homeassistant.components.climate import HVACMode
    e, _, _ = _entity(_status())
    e._optimistic_hvac_mode = HVACMode.HEAT_COOL
    e._optimistic_until = time.time() + 30
    assert e.hvac_mode == HVACMode.HEAT_COOL
    e._optimistic_until = time.time() - 1
    assert e.hvac_mode == HVACMode.OFF


def test_hvac_action_prefers_defrost_and_uses_the_dead_band():
    _ha()
    from homeassistant.components.climate import HVACAction
    e, _, _ = _entity(_status())
    assert e.hvac_action == HVACAction.OFF
    e, _, _ = _entity(_status(pre="1", defrost="1"))
    assert e.hvac_action == HVACAction.DEFROSTING
    e, _, _ = _entity(_status(pre="1", interior="18"))
    e._cached_target_temp = 25.0
    assert e.hvac_action == HVACAction.HEATING
    e._cached_target_temp = 15.5
    assert e.hvac_action == HVACAction.COOLING
    e._cached_target_temp = 18.5
    assert e.hvac_action == HVACAction.IDLE


def test_the_optimistic_action_bridges_the_dead_band_recompute():
    _ha()
    from homeassistant.components.climate import HVACAction
    e, _, _ = _entity(_status(pre="1", interior="22"))
    e._optimistic_action = HVACAction.COOLING
    e._optimistic_action_until = time.time() + 60
    assert e.hvac_action == HVACAction.COOLING


def test_the_rapid_preset_shows_briefly_then_reverts_to_none():
    _ha()
    c = load("climate")
    e, _, _ = _entity(_status())
    e._optimistic_preset = c.PRESET_RAPID_WARMING
    e._optimistic_preset_until = time.time() + 30
    assert e.preset_mode == c.PRESET_RAPID_WARMING
    e._optimistic_preset_until = time.time() - 1
    assert e.preset_mode == c.PRESET_NONE


def test_the_exterior_temperature_rides_along_as_an_attribute():
    _ha()
    e, _, _ = _entity(_status(exterior="31.5"))
    assert e.extra_state_attributes == {"exterior_temperature": 31.5}


# ------------------------------------------------------------------- restore ---

def _restored(e, extra=None, last_state=None):
    from homeassistant.helpers.restore_state import RestoreEntity

    async def _extra():
        return extra

    async def _last():
        return last_state

    e.async_get_last_extra_data = _extra
    e.async_get_last_state = _last
    orig = RestoreEntity.async_added_to_hass

    async def _noop(self):
        return None

    RestoreEntity.async_added_to_hass = _noop
    try:
        asyncio.run(e.async_added_to_hass())
    finally:
        RestoreEntity.async_added_to_hass = orig
    return e


def test_restore_prefers_the_native_celsius_extra_data():
    _ha()
    e, _, _ = _entity(_status())
    _restored(e, extra=types.SimpleNamespace(as_dict=lambda: {"target_temp_c": "24.5"}))
    assert e.target_temperature == 24.5
    assert e.extra_restore_state_data.as_dict() == {"target_temp_c": 24.5}


def test_restore_converts_the_legacy_display_attribute_back():
    """Pre-native entries only stored the display-unit attribute. 72 on a
    Fahrenheit install is 22.2 C - restoring it verbatim would set 28.5."""
    _ha()
    from homeassistant.const import UnitOfTemperature
    e, _, _ = _entity(_status())
    e.hass.config.units.temperature_unit = UnitOfTemperature.FAHRENHEIT
    _restored(e, extra=types.SimpleNamespace(as_dict=lambda: {"target_temp_c": "junk"}),
              last_state=types.SimpleNamespace(attributes={"temperature": 72}))
    assert abs(e.target_temperature - 22.2) < 0.05


def test_restore_survives_having_nothing_to_restore():
    _ha()
    e, _, _ = _entity(_status())
    before = e.target_temperature
    _restored(e)
    assert e.target_temperature == before
    _restored(e, last_state=types.SimpleNamespace(attributes={}))
    assert e.target_temperature == before
    _restored(e, last_state=types.SimpleNamespace(attributes={"temperature": "junk"}))
    assert e.target_temperature == before


# -------------------------------------------------------------------- writes ---

def test_turning_on_sends_ac_with_the_cached_setpoint():
    _ha()
    const = load("const")
    e, api, c = _entity(_status())
    e._cached_target_temp = 23.5
    with _quiet_refresh(c):
        asyncio.run(e.async_turn_on())
    service, params, command, duration = api.calls[0]
    assert command == "start" and duration == const.RCE_AC_DURATION_SEC
    assert {"key": const.RCE_KEY_TEMP, "value": "23.5"} in params
    from homeassistant.components.climate import HVACMode
    assert e.hvac_mode == HVACMode.HEAT_COOL, "optimistic state must follow"


def test_turning_off_a_defrosting_car_stops_both_circuits():
    """Stopping only the AC leaves defrost running, and the entity flips back
    to on when the optimistic window closes - the user pressed Off and the
    car must actually go off."""
    _ha()
    const = load("const")
    e, api, c = _entity(_status(defrost="1"))
    with _quiet_refresh(c):
        asyncio.run(e.async_turn_off())
    assert len(api.calls) == 2
    assert api.calls[0][2] == "stop"
    assert api.calls[1][1] == [{"key": const.RCE_KEY_CONDITIONER,
                               "value": const.RCE_VAL_DEFROST}]
    e2, api2, _ = _entity(_status())
    with _quiet_refresh(c):
        asyncio.run(e2.async_turn_off())
    assert len(api2.calls) == 1, "no defrost running -> no second command"


def test_set_temperature_clamps_fires_and_only_then_caches():
    _ha()
    e, api, c = _entity(_status(interior="18"))
    with _quiet_refresh(c):
        asyncio.run(e.async_set_temperature(temperature=40))
    assert e.target_temperature == e.max_temp, "40 must clamp to the cap"
    assert any(p["value"] == f"{e.max_temp:.1f}"
               for p in api.calls[0][1] if "value" in p)
    from homeassistant.components.climate import HVACAction
    assert e._optimistic_action == HVACAction.HEATING


def test_set_temperature_picks_the_matching_optimistic_action():
    """Above the band heats, below cools, inside idles - same rule the live
    dead-band uses, so the UI never contradicts itself."""
    _ha()
    from homeassistant.components.climate import HVACAction
    for interior, target, expected in (
            ("30", 16.0, HVACAction.COOLING),
            ("22", 22.5, HVACAction.IDLE)):
        e, _, c = _entity(_status(interior=interior))
        with _quiet_refresh(c):
            asyncio.run(e.async_set_temperature(temperature=target))
        assert e._optimistic_action == expected, (interior, target)


def test_a_garbled_interior_temperature_reads_absent():
    """float("junk") must become None, not a crash inside the thermostat."""
    _ha()
    e, _, _ = _entity(_status(interior="junk"))
    assert e.current_temperature is None


def test_set_temperature_without_a_temperature_is_a_no_op():
    _ha()
    e, api, _ = _entity(_status())
    asyncio.run(e.async_set_temperature())
    assert api.calls == []


def test_a_rejected_temperature_never_reaches_the_cache():
    """Fire first, cache second: the UI must not show a setpoint the server
    refused."""
    _ha()
    api_mod = load("api")
    from homeassistant.exceptions import HomeAssistantError
    e, api, c = _entity(_status(),
                        fail=api_mod.GeelyControlError("8070", "pending"))
    before = e.target_temperature
    with _quiet_refresh(c):
        try:
            asyncio.run(e.async_set_temperature(temperature=27))
        except HomeAssistantError as err:
            assert "pending" in str(err)
        else:
            raise AssertionError("the rejection vanished")
    assert e.target_temperature == before


def test_a_generic_transport_error_is_wrapped_not_leaked():
    _ha()
    from homeassistant.exceptions import HomeAssistantError
    e, api, c = _entity(_status(), fail=OSError("socket closed"))
    with _quiet_refresh(c):
        try:
            asyncio.run(e.async_turn_on())
        except HomeAssistantError as err:
            assert "failure" in str(err)
        else:
            raise AssertionError("OSError escaped raw")


def test_rapid_warming_bundles_heat_and_rapid_cooling_bundles_vent():
    _ha()
    c = load("climate")
    e, api, _ = _entity(_status())
    with _quiet_refresh(c):
        asyncio.run(e.async_set_preset_mode(c.PRESET_RAPID_WARMING))
    kind, kw = api.calls[0]
    assert kind == "rapid"
    assert kw["heat_seats"] == ["11", "19"] and kw["vent_seats"] is None
    assert kw["temp"] == f"{e.max_temp:.1f}" and kw["vlt"] is False
    assert e.preset_mode == c.PRESET_RAPID_WARMING
    e2, api2, _ = _entity(_status())
    with _quiet_refresh(c):
        asyncio.run(e2.async_set_preset_mode(c.PRESET_RAPID_COOLING))
    _, kw2 = api2.calls[0]
    assert kw2["vent_seats"] == ["11", "19"] and kw2["heat_seats"] is None
    assert kw2["temp"] == f"{e2.min_temp:.1f}" and kw2["vlt"] is True


def test_choosing_none_clears_the_optimistic_preset_immediately():
    _ha()
    c = load("climate")
    e, api, _ = _entity(_status())
    e._optimistic_preset = c.PRESET_RAPID_WARMING
    e._optimistic_preset_until = time.time() + 30
    asyncio.run(e.async_set_preset_mode(c.PRESET_NONE))
    assert e.preset_mode == c.PRESET_NONE
    assert api.calls == [], "none is a UI reset, not a car command"


def test_a_rejected_rapid_leaves_no_optimistic_trace():
    _ha()
    api_mod = load("api")
    c = load("climate")
    from homeassistant.exceptions import HomeAssistantError
    e, api, _ = _entity(_status(),
                        fail=api_mod.GeelyControlError("failure", "no seats"))
    with _quiet_refresh(c):
        try:
            asyncio.run(e.async_set_preset_mode(c.PRESET_RAPID_COOLING))
        except HomeAssistantError as err:
            assert "no seats" in str(err)
        else:
            raise AssertionError("the rejection vanished")
    assert e.preset_mode == c.PRESET_NONE
    from homeassistant.components.climate import HVACMode
    assert e.hvac_mode == HVACMode.OFF


def test_a_crashed_rapid_is_wrapped_too():
    _ha()
    c = load("climate")
    from homeassistant.exceptions import HomeAssistantError
    e, api, _ = _entity(_status(), fail=OSError("tls reset"))
    with _quiet_refresh(c):
        try:
            asyncio.run(e.async_set_preset_mode(c.PRESET_RAPID_WARMING))
        except HomeAssistantError:
            pass
        else:
            raise AssertionError("OSError escaped raw")


def test_temperatures_are_formatted_the_way_the_gateway_expects():
    """The gateway silently ignores '22' - it wants '22.0'."""
    _ha()
    c = load("climate")
    assert c._fmt_temp(22) == "22.0"
    assert c._fmt_temp(15.5) == "15.5"


# ------------------------------ rapid presets and the seats that ignore them ---
# A 2025 EX5 Inspire runs the AC on Rapid Warming and ignores the compound
# bundle's seat block entirely, while its individual seat entities work
# (#19). So the presets follow up on RCE_2 - the channel that car proves
# works - for the front positions the car advertises.

def test_rapid_warming_follows_up_with_seat_heat_on_the_verified_channel():
    _ha()
    c = load("climate")
    e, api, _ = _entity(_status())
    with _quiet_refresh(c):
        asyncio.run(e.async_set_preset_mode(c.PRESET_RAPID_WARMING))
    assert api.calls[0][0] == "rapid", "the compound bundle still fires first"
    seat_calls = [x for x in api.calls if x[0] == c.SERVICE_CLIMATE]
    assert len(seat_calls) == 2, api.calls
    for _sid, params, command, _dur in seat_calls:
        flat = {p["key"]: p["value"] for p in params}
        assert flat[c.RCE_KEY_LEVEL] == "3"
        assert c.RCE_KEY_HEAT in flat and c.RCE_KEY_VENT not in flat
        assert command == "start"
    seats = {
        p["value"] for _s, params, _c, _d in seat_calls for p in params
        if p["key"] == c.RCE_KEY_HEAT
    }
    assert seats == {c.SEAT_FRONT_LEFT, c.SEAT_FRONT_RIGHT}


def test_rapid_cooling_follows_up_with_seat_ventilation():
    _ha()
    c = load("climate")
    e, api, _ = _entity(_status())
    with _quiet_refresh(c):
        asyncio.run(e.async_set_preset_mode(c.PRESET_RAPID_COOLING))
    seat_calls = [x for x in api.calls if x[0] == c.SERVICE_CLIMATE]
    assert len(seat_calls) == 2
    for _sid, params, _cmd, _dur in seat_calls:
        flat = {p["key"]: p["value"] for p in params}
        assert c.RCE_KEY_VENT in flat and c.RCE_KEY_HEAT not in flat


def test_a_car_without_heated_seats_gets_no_seat_follow_up():
    _ha()
    c = load("climate")
    e, api, _ = _entity(_status(), caps={"seat.heat.enabled": False})
    with _quiet_refresh(c):
        asyncio.run(e.async_set_preset_mode(c.PRESET_RAPID_WARMING))
    assert [x for x in api.calls if x[0] == c.SERVICE_CLIMATE] == []


def test_only_the_advertised_front_positions_are_driven():
    """A car advertising rear seats keeps them out of a rapid preset - the
    presets are about the two front seats, as in the official app."""
    _ha()
    c = load("climate")
    e, api, _ = _entity(_status(), caps={
        "seat.heat.positions": [c.SEAT_FRONT_LEFT, "rear-left", "rear-right"]})
    with _quiet_refresh(c):
        asyncio.run(e.async_set_preset_mode(c.PRESET_RAPID_WARMING))
    seat_calls = [x for x in api.calls if x[0] == c.SERVICE_CLIMATE]
    assert len(seat_calls) == 1
    flat = {p["key"]: p["value"] for p in seat_calls[0][1]}
    assert flat[c.RCE_KEY_HEAT] == c.SEAT_FRONT_LEFT


def test_a_refused_seat_does_not_fail_a_rapid_that_already_worked():
    """The cabin is warming; a car that rejects the seat command must not
    turn that success into an error toast."""
    _ha()
    c = load("climate")
    calls = []

    class _PartialApi(_Api):
        def control(self, *a, **k):
            calls.append(a)
            raise RuntimeError("seat says no")

    hass = _Hass()
    api = _PartialApi()
    e = c.GeelyClimate(hass, {
        "coordinator": _Coord(_status()), "api": api, "vin": FAKE_VIN,
        "capabilities": {}, "device_name": "Geely EX5 (0000)"})
    e.hass = hass
    e.async_write_ha_state = lambda: None
    with _quiet_refresh(c):
        asyncio.run(e.async_set_preset_mode(c.PRESET_RAPID_WARMING))
    assert len(calls) == 2, "both seats were attempted"
    assert e.preset_mode == c.PRESET_RAPID_WARMING, "the rapid action stands"
