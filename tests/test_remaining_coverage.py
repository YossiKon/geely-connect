"""The corners the rest of the suite walks past.

Each test here exists because a coverage run showed the line had never
executed - which means a regression there would ship silently. The behaviors
are small (a fallback name, a truncated raw value, a restore conversion), but
every one is user-visible when it breaks.
"""
import asyncio
import types

from conftest import FAKE_VIN, have_homeassistant, load
from run import skip


def _ha():
    if not have_homeassistant():
        skip("homeassistant not installed")


class _Coord:
    def __init__(self, data=None, success=True):
        self.data = data or {}
        self.last_update_success = success
        self.refreshes = 0

    def async_add_listener(self, cb, *a, **k):
        return lambda: None

    async def async_request_refresh(self):
        self.refreshes += 1


# ---------------------------------------------------------------- propulsion ---

def test_is_hybrid_is_the_kind_question_not_the_plug_one():
    """A PHEV and an HEV are both hybrids; a BEV is not. The property is
    consumed by humans reading diagnostics, so it must track `kind` alone."""
    _ha()
    p = load("propulsion")
    assert p.classify("混动", {}).is_hybrid is True
    assert p.classify("纯电动", {}).is_hybrid is False


# ------------------------------------------------------------- binary sensor ---

def test_connectivity_is_available_even_when_the_cloud_is_not():
    """The whole point of the Connected sensor is to say "down" - if it went
    unavailable together with the cloud it could never report the outage."""
    _ha()
    bs = load("binary_sensor")
    e = bs.GeelyConnectivity(_Coord(success=False), FAKE_VIN, "Geely (0000)")
    assert e.available is True
    assert e.is_on is False


def test_a_present_binary_value_maps_through_the_on_set():
    _ha()
    bs = load("binary_sensor")
    row = next(s for s in bs.SPECS if s[0] == "charger_plugged_in")
    data = {"vehicleStatus": {"additionalVehicleStatus": {
        "electricVehicleStatus": {"statusOfChargerConnection": "3"}}}}
    e = bs.GeelyBinarySensor(_Coord(data), FAKE_VIN, "Geely (0000)", *row)
    assert e.is_on is True
    data["vehicleStatus"]["additionalVehicleStatus"]["electricVehicleStatus"]["statusOfChargerConnection"] = "0"
    assert e.is_on is False


# ------------------------------------------------------------------- helpers ---

def test_walk_stops_at_a_non_dict_midway():
    """A server that sends a string where a branch should be must read as
    absent, not raise AttributeError inside a state write."""
    _ha()
    h = load("helpers")
    assert h.walk({"a": "flat"}, ("a", "b")) is None


def test_device_info_falls_back_to_a_generic_name():
    _ha()
    h = load("helpers")
    info = h.device_info(FAKE_VIN)
    assert info["name"] == f"Geely ({FAKE_VIN})"
    named = h.device_info(FAKE_VIN, "My Car (0000)")
    assert named["name"] == "My Car (0000)"


def _run_scheduled(h, coordinator, *delays, after=None, raise_from_refresh=None):
    """Drive schedule_refresh's background task to completion synchronously."""
    captured = {}

    class _Hass:
        def async_create_task(self, coro):
            captured["coro"] = coro

    if raise_from_refresh is not None:
        async def _boom():
            raise raise_from_refresh
        coordinator.async_request_refresh = _boom
    h.schedule_refresh(_Hass(), coordinator, *delays, after=after)
    asyncio.run(captured["coro"])


def test_schedule_refresh_polls_after_each_delay_then_releases():
    """The `after` hook is what drops an optimistic override - it must run
    once the real state is available, and only then."""
    _ha()
    h = load("helpers")
    c = _Coord()
    seen = []
    _run_scheduled(h, c, 0, 0, after=lambda: seen.append("after"))
    assert c.refreshes == 2
    assert seen == ["after"]


def test_a_failed_refresh_still_releases_the_optimistic_state():
    """`after` must run even when the poll dies, or the entity stays pinned
    to a guessed state until the next command."""
    _ha()
    h = load("helpers")
    seen = []
    _run_scheduled(h, _Coord(), 0, after=lambda: seen.append("after"),
                   raise_from_refresh=ValueError("cloud hiccup"))
    assert seen == ["after"]


def test_cancellation_skips_the_after_hook():
    """Unload/shutdown is the one case `after` must NOT run - there is no
    entity left to write the released state to."""
    _ha()
    h = load("helpers")
    seen = []
    try:
        _run_scheduled(h, _Coord(), 0, after=lambda: seen.append("after"),
                       raise_from_refresh=asyncio.CancelledError())
    except asyncio.CancelledError:
        pass
    assert seen == []


def test_control_errors_become_toasts_with_the_server_message():
    _ha()
    h = load("helpers")
    api = load("api")
    from homeassistant.exceptions import HomeAssistantError
    import logging
    try:
        with h.translate_control_errors(logging.getLogger("t"), "lock"):
            raise api.GeelyControlError("8070", "still pending")
    except HomeAssistantError as e:
        assert "lock" in str(e) and "still pending" in str(e)
    else:
        raise AssertionError("GeelyControlError passed through unwrapped")


def test_unexpected_errors_are_wrapped_and_logged_too():
    """A KeyError from a malformed response must reach the user as a toast,
    not vanish as an unhandled exception in a service call."""
    _ha()
    h = load("helpers")
    from homeassistant.exceptions import HomeAssistantError
    import logging
    try:
        with h.translate_control_errors(logging.getLogger("t"), "trunk",
                                        "trunk command blew up"):
            raise KeyError("data")
    except HomeAssistantError as e:
        assert "trunk" in str(e)
    else:
        raise AssertionError("the generic error passed through unwrapped")


# -------------------------------------------------------------------- sensor ---

def test_int_coercion_truncates_a_decimal_string():
    """The server sends "96.0" for whole-number fields; int() alone raises on
    it, so the coercion goes through float first."""
    _ha()
    s = load("sensor")
    assert s._coerce("96.4", "int", None) == 96


def test_a_map_kind_without_a_map_passes_the_value_through():
    _ha()
    s = load("sensor")
    assert s._coerce("7", "map", None) == "7"
    assert s._coerce("7", "no-such-kind", None) == "7"


def test_flatten_gives_up_below_the_depth_cap():
    """A pathologically nested payload must cost a truncated listing, not a
    recursion error inside entity setup."""
    _ha()
    s = load("sensor")
    bomb = current = {}
    for _ in range(s._MAX_FLATTEN_DEPTH + 2):
        current["d"] = {}
        current = current["d"]
    current["leaf"] = "1"
    flat = s._flatten(bomb)
    assert all(k.count(".") <= s._MAX_FLATTEN_DEPTH for k in flat)


def test_flatten_indexes_lists_and_caps_them():
    _ha()
    s = load("sensor")
    flat = s._flatten({"a": ["x", "y"]})
    assert flat == {"a.0": "x", "a.1": "y"}
    capped = s._flatten({"a": list(map(str, range(s._MAX_LIST_ITEMS + 10)))})
    assert len(capped) == s._MAX_LIST_ITEMS


def _raw(path, data):
    s = load("sensor")
    return s.GeelyRawSensor(_Coord(data), FAKE_VIN, "Geely (0000)", path)


def test_raw_sensor_walks_lists_and_coerces_numbers():
    _ha()
    data = {"a": [{"b": "42"}, {"b": "3.5"}, {"b": "word"}]}
    assert _raw("a.0.b", data).native_value == 42
    assert _raw("a.1.b", data).native_value == 3.5
    assert _raw("a.2.b", data).native_value == "word"
    assert _raw("a.0.b", data).extra_state_attributes == {"field_path": "a.0.b"}


def test_raw_sensor_reads_absent_for_every_broken_path():
    """Bad index, non-container midway, missing key - all unknown, never a
    crash in a state write."""
    _ha()
    data = {"a": [{"b": "1"}], "s": "flat", "n": None}
    for path in ("a.9.b", "a.x.b", "s.deeper", "missing", "n.deeper"):
        assert _raw(path, data).native_value is None, path


def test_raw_sensor_truncates_unbounded_strings_and_stringifies_containers():
    """HA rejects states over 255 chars; a raw dump must not take the entity
    down with a too-long state."""
    _ha()
    long = "x" * 300
    assert _raw("k", {"k": long}).native_value == "x" * 255
    assert _raw("k", {"k": True}).native_value is True
    v = _raw("k", {"k": {"nested": "y"}}).native_value
    assert isinstance(v, str) and len(v) <= 255


def test_full_exposure_suppresses_hybrid_paths_only_when_the_entities_exist():
    """The mirror of the BEV case already tested: on a hybrid the curated fuel
    entities DO exist, so full exposure must not hand back raw twins."""
    _ha()
    import copy
    from test_entities import STATUS, _Entry, _Hass
    p = load("propulsion")
    verdict = p.Verdict(kind=p.Propulsion.HYBRID, has_tank=True, has_plug=True,
                        source="declared", declared_raw="混动")

    class _FullEntry(_Entry):
        data = {"vin": FAKE_VIN, "pressure_unit": "psi", "full_exposure": True}

    data = copy.deepcopy(STATUS)
    data["vehicleStatus"]["additionalVehicleStatus"]["runningStatus"]["fuelLevel"] = "35.8"
    hass, entry = _Hass(), _FullEntry()
    hass.data["geely_connect"] = {"e1": {
        "api": object(), "coordinator": _Coord(data), "vin": FAKE_VIN,
        "device_name": "Geely EX5 (0000)", "capabilities": {},
        "propulsion": verdict}}
    got = []
    asyncio.run(load("sensor").async_setup_entry(
        hass, entry, lambda e, *a, **k: got.extend(list(e))))
    raw = {e._attr_unique_id for e in got if type(e).__name__ == "GeelyRawSensor"}
    assert not any(uid.endswith("runningStatus.fuelLevel") for uid in raw), \
        "full exposure duplicated a curated hybrid entity"


def test_last_updated_stamps_on_success_only():
    _ha()
    s = load("sensor")
    e = s.GeelyLastUpdatedSensor(_Coord(success=True), FAKE_VIN, "Geely (0000)")
    e.async_write_ha_state = lambda: None
    asyncio.run(e.async_added_to_hass())
    first = e.native_value
    assert first is not None
    e.coordinator.last_update_success = False
    e._handle_coordinator_update()
    assert e.native_value == first, "a failed poll must not move Last Updated"
    e.coordinator.last_update_success = True
    e._handle_coordinator_update()
    assert e.native_value >= first


def test_combined_range_rejects_a_negative_electric_reading():
    _ha()
    s = load("sensor")
    data = {"vehicleStatus": {"additionalVehicleStatus": {
        "electricVehicleStatus": {"distanceToEmptyOnBatteryOnly": "-5"},
        "runningStatus": {"fuelLevel": "35.8", "aveFuelConsumption": "7.1"}}}}
    e = s.GeelyCombinedRangeSensor(_Coord(data), FAKE_VIN, "Geely (0000)")
    assert e.native_value is None


def test_engine_running_normalises_the_firmware_spellings():
    """Different firmware sends engine_running / RUNNING / 1; unknown words
    must read as "has not said", not as off."""
    _ha()
    s = load("sensor")
    def status(raw):
        return {"vehicleStatus": {"basicVehicleStatus": {"engineStatus": raw}}}
    assert s._engine_running(status("engine_running")) is True
    assert s._engine_running(status("  RUNNING ")) is True
    assert s._engine_running(status("mystery_state")) is None
    assert s._engine_running({}) is None


def _restorable(cls_name, data, last_state=None, last_sensor=None):
    """Build a Restore sensor whose HA-side restore plumbing is stubbed out."""
    s = load("sensor")
    e = getattr(s, cls_name)(_Coord(data), FAKE_VIN, "Geely (0000)")

    async def _last_state():
        return last_state

    async def _last_sensor():
        return last_sensor

    e.async_get_last_state = _last_state
    e.async_get_last_sensor_data = _last_sensor
    from homeassistant.helpers.restore_state import RestoreEntity
    orig = RestoreEntity.async_added_to_hass

    async def _noop(self):
        return None

    RestoreEntity.async_added_to_hass = _noop
    try:
        asyncio.run(e.async_added_to_hass())
    finally:
        RestoreEntity.async_added_to_hass = orig
    return e


def test_last_trip_restores_miles_back_into_kilometres():
    """A DISTANCE sensor's state is stored in the display unit. Restoring 10
    miles as 10 km shrank every trip by 1.609 on a miles install - the fix
    reads it through the sensor channel and converts."""
    _ha()
    data = {"vehicleStatus": {"basicVehicleStatus": {"engineStatus": "engine_off"},
                              "additionalVehicleStatus": {"maintenanceStatus": {"odometer": "100"}}}}
    e = _restorable(
        "GeelyLastTripSensor", data,
        last_state=types.SimpleNamespace(attributes={
            "trip_start_odometer": 90.0, "engine_was_running": False}),
        last_sensor=types.SimpleNamespace(native_value="10",
                                          native_unit_of_measurement="mi"))
    assert abs(e._last_trip - 16.09) < 0.01
    assert e._start_km == 90.0


def test_last_trip_survives_junk_in_the_restored_value():
    _ha()
    data = {"vehicleStatus": {}}
    e = _restorable(
        "GeelyLastTripSensor", data,
        last_state=types.SimpleNamespace(attributes={}),
        last_sensor=types.SimpleNamespace(native_value="not-a-number",
                                          native_unit_of_measurement="km"))
    assert e._last_trip is None


def test_trip_in_progress_measures_from_the_engine_on_odometer():
    _ha()
    s = load("sensor")
    data = {"vehicleStatus": {"basicVehicleStatus": {"engineStatus": "engine_running"},
                              "additionalVehicleStatus": {"maintenanceStatus": {"odometer": "104.6"}}}}
    e = s.GeelyTripInProgressSensor(_Coord(data), FAKE_VIN, "Geely (0000)")
    e._was_running, e._start_km = True, 100.0
    assert e.native_value == 4.6
    e.coordinator.data = {"vehicleStatus": {"basicVehicleStatus": {"engineStatus": "engine_running"}}}
    assert e.native_value is None, "no odometer must read unknown, not a guess"


# -------------------------------------------------------------- capabilities ---

def _entry(fid, enable=True, params=None, **extra):
    e = {"functionId": fid, "valueEnable": enable}
    if params:
        e["paramsJson"] = [{"nameKey": k, "config": v} for k, v in params.items()]
    e.update(extra)
    return e


def test_the_catalog_parses_a_fully_loaded_climate_entry():
    """One entry carrying everything the EX5 catalog spreads across params:
    seats, defrost, steering wheel, window ventilation with a duration."""
    _ha()
    cap = load("capabilities")
    out = cap.parse([_entry("combined_climate_control", True, params={
        "ad_temp_range": "16|30", "AC_step": "0.5",
        "dpt_heat_loc": "driver, passenger", "dpt_vent_loc": "driver",
        "alone_select": "support",
        "climate_devices": "defrost;AC;heater",
        "steel_wheel_heating": "true",
        "window_ventilation": "true", "window_ventilation_duration": "90",
    })])
    assert out["ac.enabled"] is True
    assert (out["ac.min"], out["ac.max"], out["ac.step"]) == (16.0, 30.0, 0.5)
    assert out["seat.heat.positions"] == ["driver", "passenger"]
    assert out["seat.vent.positions"] == ["driver"]
    assert out["seat.alone_select"] is True
    assert out["defrost.enabled"] is True
    assert out["steering_wheel_heat.enabled"] is True
    assert out["window_vent.enabled"] is True
    assert out["window_vent.duration_s"] == 90


def test_garbled_numbers_in_the_catalog_degrade_to_defaults():
    """Geely's catalog is server-controlled free text; a typo there must cost
    a default, never the climate entity."""
    _ha()
    cap = load("capabilities")
    out = cap.parse([_entry("remote_climate_control_2", True,
                            valueRange="a|b", showType="fast",
                            params={"ad_temp_range": "x|y", "AC_step": "quick",
                                    "window_ventilation": "true",
                                    "window_ventilation_duration": "soon"})])
    assert out["ac.enabled"] is True
    assert "ac.min" not in out and "ac.step" not in out
    assert "window_vent.duration_s" not in out


def test_showtype_supplies_the_step_when_params_do_not():
    _ha()
    cap = load("capabilities")
    out = cap.parse([_entry("remote_climate_control_2", True,
                            valueRange="15.5|28.5", showType="0.5")])
    assert out["ac.step"] == 0.5


def test_every_bool_feature_flag_maps_to_its_entity_switch():
    """One flag per remote control - a missing mapping strands a real car
    control behind a default."""
    _ha()
    cap = load("capabilities")
    fids = ["remote_purification", "honk_flash", "remote_control_lock_2",
            "remote_control_unlock_2", "remote_control_open_2",
            "remote_control_skylight_2", "remote_control_curtain_2",
            "remote_control_window_2", "remote_control_ventilate_2",
            "remote_charge_2", "parking_comfortable_2",
            "remote_appointment_charging"]
    out = cap.parse([_entry(f) for f in fids])
    for key in ("gclean.enabled", "find_car.enabled", "lock.enabled",
                "unlock.enabled", "tailgate.enabled", "sunroof.enabled",
                "sunshade.enabled", "windows.enabled", "window_vent.enabled",
                "charging.enabled", "parking_comfort.enabled",
                "scheduled_charging.enabled"):
        assert out.get(key) is True, key
