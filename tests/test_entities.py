"""Entity-level behaviour, against a real Home Assistant install.

Skipped when homeassistant is not importable, so the offline tests still run
in a bare checkout.
"""
import asyncio
import datetime as dt

from conftest import FAKE_VIN, have_homeassistant, load
from run import skip

STATUS = {
    "vehicleStatus": {
        "basicVehicleStatus": {"speed": "0", "engineStatus": "engine_running"},
        "additionalVehicleStatus": {
            "maintenanceStatus": {
                "odometer": "4646", "daysToService": "300", "distanceToService": "9000",
                "mainBatteryStatus": {"chargeLevel": "88", "voltage": "12.6"},
                "tyreStatusDriver": "240", "tyreStatusPassenger": "252",
                "tyreStatusDriverRear": "235", "tyreStatusPassengerRear": "238"},
            "electricVehicleStatus": {
                "chargeLevel": "84", "distanceToEmptyOnBatteryOnly": "349",
                "statusOfChargerConnection": "3", "timeToFullyCharged": "95",
                "averPowerConsumption": "16.4"},
            "climateStatus": {
                "interiorTemp": "24", "exteriorTemp": "31",
                "winStatusDriver": "2", "winStatusPassenger": "2",
                "winStatusDriverRear": "2", "winStatusPassengerRear": "2",
                "curtainOpenStatus": "1", "sunroofOpenStatus": "1"},
            "drivingSafetyStatus": {
                "electricParkBrakeStatus": "1", "doorOpenStatusDriver": "0",
                "centralLockingStatus": "1"},
            "runningStatus": {"tripMeter1": "12.4", "avgSpeed": "38"},
        },
    },
    "_state": {},
    "_scheduled_charging": {},
}


class _Coord:
    data = STATUS
    last_update_success = True

    def async_add_listener(self, cb, *a, **k):
        return lambda: None


class _Entry:
    entry_id = "e1"
    data = {"vin": FAKE_VIN, "pressure_unit": "psi"}
    options: dict = {}

    def async_on_unload(self, fn):
        return fn


class _Hass:
    def __init__(self):
        self.data = {}

    def async_create_task(self, coro, *a, **k):
        coro.close()
        return None


def _build_all(entry=None, **bundle_extra):
    """Set up every platform and return {platform: [entities]}.

    `bundle_extra` overrides entries in the hass.data bundle - that is how the
    propulsion verdict reaches the platforms, so it is how the gating is tested.
    `entry` overrides the config entry itself, for gates that read the entry
    rather than the bundle.
    """
    hass, entry = _Hass(), entry or _Entry()
    hass.data["geely_connect"] = {"e1": {
        "api": object(), "coordinator": _Coord(), "vin": FAKE_VIN,
        "device_name": "Geely EX5 (0000)", "capabilities": {}, **bundle_extra}}
    out = {}
    for name in ("sensor", "binary_sensor", "switch", "select", "cover",
                 "button", "lock", "climate", "device_tracker", "time"):
        mod = load(name)
        got = []
        asyncio.run(mod.async_setup_entry(hass, entry, lambda e, *a, **k: got.extend(list(e))))
        out[name] = got
    return out


def _keys(built):
    """Every unique_id suffix built, across all platforms."""
    return {getattr(e, "_attr_unique_id", "").rsplit(f"{FAKE_VIN}_", 1)[-1]
            for entities in built.values() for e in entities}


_FUEL_ONLY_KEYS = frozenset({
    "fuel_level", "fuel_level_pct", "fuel_consumption", "fuel_consumption_trip",
    "mileage_on_fuel", "mileage_on_battery", "engine_coolant_temp",
    "engine_speed", "engine_oil_health", "engine_hours_to_service",
    "fuel_range", "combined_range",
    "bs_tank_flap",   # binary_sensor unique_ids carry a bs_ prefix
})


def test_all_platforms_build_without_error():
    if not have_homeassistant():
        skip("homeassistant not installed")
    built = _build_all()
    total = sum(len(v) for v in built.values())
    assert total > 50, f"only {total} entities built"
    assert all(built.values()), [k for k, v in built.items() if not v]


def test_unique_ids_are_unique_across_every_platform():
    if not have_homeassistant():
        skip("homeassistant not installed")
    seen = {}
    for platform, entities in _build_all().items():
        for e in entities:
            uid = getattr(e, "_attr_unique_id", None)
            assert uid, f"{platform}/{type(e).__name__} has no unique_id"
            assert uid not in seen, f"{uid} used by both {seen.get(uid)} and {platform}"
            seen[uid] = platform


def test_every_unique_id_is_namespaced_by_vin():
    if not have_homeassistant():
        skip("homeassistant not installed")
    for entities in _build_all().values():
        for e in entities:
            uid = getattr(e, "_attr_unique_id", "")
            assert FAKE_VIN in uid, f"{uid} is not vehicle-specific"


def test_the_climate_object_id_stays_climate_despite_the_display_name():
    """The display name is "Remote Pre-Conditioning", but every shipped
    dashboard and the README adaptation procedure reference
    climate.<device>_climate, and existing installs already have that id in
    the registry. New installs must generate the same one."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    (entity,) = _build_all()["climate"]
    assert entity.entity_id == "climate.geely_ex5_0000_climate", entity.entity_id
    assert entity._attr_name == "Remote Pre-Conditioning"


# ------------------------------------------------- propulsion gating ---
# STATUS above is a BEV payload: no fuelStatus, no engine fields. What a BEV
# owner must never see is a row of fuel tiles that can only read `unavailable`.

def test_a_bev_gets_no_fuel_or_engine_entities():
    if not have_homeassistant():
        skip("homeassistant not installed")
    p = load("propulsion")
    verdict = p.classify("\u7eaf\u7535\u52a8", _Coord.data)
    assert verdict.has_tank is False, "fixture is not a BEV any more"
    leaked = _keys(_build_all(propulsion=verdict)) & _FUEL_ONLY_KEYS
    assert leaked == set(), leaked


def test_an_entry_with_no_verdict_at_all_behaves_like_a_bev():
    """Belt and braces: if the verdict is ever missing from the bundle, the
    platforms must fall back to the entity set every install had before."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    leaked = _keys(_build_all()) & _FUEL_ONLY_KEYS
    assert leaked == set(), leaked


_CHARGING_KEYS = frozenset({
    "charger_connected", "time_to_full_min", "charge_complete",
    "charge_power", "charge_current", "charge_voltage",
    "bs_charger_plugged_in", "sw_charging", "sw_scheduled_charging",
    "time_scheduled_charging_start", "time_scheduled_charging_end",
})


def test_a_socketless_car_gets_no_charging_entities():
    """The other half of the propulsion gate: an HEV or a petrol car must not
    carry charging tiles that can only read unavailable, or charging commands
    that can only fail."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    p = load("propulsion")
    for kind in (p.Propulsion.HYBRID, p.Propulsion.FUEL):
        verdict = p.Verdict(kind=kind, has_tank=True, has_plug=False,
                            source="declared", declared_raw="x")
        leaked = _keys(_build_all(propulsion=verdict)) & _CHARGING_KEYS
        assert leaked == set(), (kind, leaked)


def test_a_missing_or_unknown_verdict_keeps_the_charging_entities():
    """A BEV with an unreadable first payload must not lose its charging
    tiles - the gate closes on positive no-plug evidence only."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    p = load("propulsion")
    for extra in ({}, {"propulsion": p.classify(None, None)}):
        got = _keys(_build_all(**extra))
        assert _CHARGING_KEYS <= got, _CHARGING_KEYS - got


_NEW_PLATFORM_KEYS = frozenset({"bs_is_charging", "bs_is_plugged_in"})

# Added by the same change as the two above, and present in BOTH platforms'
# payloads - a gate that swept these up would delete six live entities from
# every existing install.
_BOTH_PLATFORM_KEYS = frozenset({
    "bs_door_lock_driver", "bs_door_lock_passenger", "bs_door_lock_rear_left",
    "bs_door_lock_rear_right", "trip_meter_2", "discharge_current",
    "discharge_voltage",
})


def _new_platform_entry():
    e = _Entry()
    e.data = {"vin": FAKE_VIN, "pressure_unit": "psi", "platform": "zeekr"}
    e.options = {"zeekr_enc_vin": "1eNc0d3d="}
    return e


def test_the_reported_charging_booleans_are_new_platform_only():
    """`isCharging` / `isPluggedIn` are in the new platform's payload only, so
    an old-platform car must not be given two tiles that can never say
    anything."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    leaked = _keys(_build_all()) & _NEW_PLATFORM_KEYS
    assert leaked == set(), leaked
    got = _keys(_build_all(entry=_new_platform_entry()))
    assert _NEW_PLATFORM_KEYS <= got, _NEW_PLATFORM_KEYS - got


def test_the_gate_is_the_token_not_the_platform_name():
    """A zeekr entry with no x-vin token still polls the legacy status path and
    gets the legacy fields, so the token is what the gate has to read."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    e = _Entry()
    e.data = {"vin": FAKE_VIN, "pressure_unit": "psi", "platform": "zeekr"}
    leaked = _keys(_build_all(entry=e)) & _NEW_PLATFORM_KEYS
    assert leaked == set(), leaked


def test_the_platform_gate_takes_only_those_two_entities():
    """The seven other fields promoted alongside them are on both platforms."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    got = _keys(_build_all())          # the default entry is an old-platform one
    assert _BOTH_PLATFORM_KEYS <= got, _BOTH_PLATFORM_KEYS - got


def test_full_exposure_keeps_fields_whose_curated_twin_was_not_built():
    """A path is only skipped by the raw pass when the curated entity that
    owns it was actually created. On a BEV the fuel rows do not exist, so a
    stray fuel field must still surface as a raw sensor - suppressed there it
    would be visible nowhere. The charge-leg fields stay suppressed because
    their computed owners ARE built."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    import copy

    class _FullEntry(_Entry):
        data = {"vin": FAKE_VIN, "pressure_unit": "psi", "full_exposure": True}

    class _RichCoord(_Coord):
        data = copy.deepcopy(STATUS)

    add = _RichCoord.data["vehicleStatus"]["additionalVehicleStatus"]
    add["runningStatus"]["fuelLevel"] = "0"
    add["electricVehicleStatus"]["chargeUAct"] = "0.0"

    hass, entry = _Hass(), _FullEntry()
    hass.data["geely_connect"] = {"e1": {
        "api": object(), "coordinator": _RichCoord(), "vin": FAKE_VIN,
        "device_name": "Geely EX5 (0000)", "capabilities": {}}}
    got = []
    asyncio.run(load("sensor").async_setup_entry(
        hass, entry, lambda e, *a, **k: got.extend(list(e))))
    raw = {getattr(e, "_attr_unique_id", "") for e in got
           if type(e).__name__ == "GeelyRawSensor"}
    assert any(uid.endswith("runningStatus.fuelLevel") for uid in raw), \
        "the unowned fuel field vanished from full exposure"
    assert not any(uid.endswith("electricVehicleStatus.chargeUAct") for uid in raw), \
        "the charge leg grew a raw twin despite its computed owner"


def test_a_hybrid_gets_the_whole_fuel_set_and_keeps_the_electric_one():
    if not have_homeassistant():
        skip("homeassistant not installed")
    p = load("propulsion")
    verdict = p.Verdict(kind=p.Propulsion.HYBRID, has_tank=True, has_plug=True,
                        source="declared", declared_raw="\u6df7\u52a8",
                        propulsion_type="3", fuel_type="2")
    built = _build_all(propulsion=verdict)
    got = _keys(built)
    assert _FUEL_ONLY_KEYS <= got, _FUEL_ONLY_KEYS - got
    # The electric half does not go away on a PHEV.
    assert {"battery", "range", "sw_charging"} <= got, sorted(got)


def test_the_hybrid_entities_carry_unique_namespaced_ids_too():
    """The uniqueness and VIN checks above only cover the BEV set."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    p = load("propulsion")
    verdict = p.Verdict(kind=p.Propulsion.HYBRID, has_tank=True, has_plug=True,
                        source="declared", declared_raw="", propulsion_type="",
                        fuel_type="")
    seen = {}
    for platform, entities in _build_all(propulsion=verdict).items():
        for e in entities:
            uid = getattr(e, "_attr_unique_id", None)
            assert uid, f"{platform}/{type(e).__name__} has no unique_id"
            assert FAKE_VIN in uid, f"{uid} is not vehicle-specific"
            assert uid not in seen, f"{uid} used by both {seen[uid]} and {platform}"
            seen[uid] = platform


def test_no_property_raises_on_a_sparse_payload():
    """A trim that reports almost nothing must not take the platform down."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    sparse = {"vehicleStatus": {"basicVehicleStatus": {},
                                "additionalVehicleStatus": {}},
              "_state": {}, "_scheduled_charging": {}}
    # Only the properties this integration implements. `state` belongs to Home
    # Assistant's base classes and needs a live hass, which a stub cannot give.
    OURS = ("native_value", "is_on", "is_closed", "is_locked", "hvac_mode",
            "hvac_action", "current_option", "extra_state_attributes",
            "latitude", "longitude", "target_temperature", "current_temperature")
    original = _Coord.data
    try:
        _Coord.data = sparse
        for platform, entities in _build_all().items():
            for e in entities:
                for prop in OURS:
                    # defined by us, not inherited from the HA base class
                    if any(prop in k.__dict__ for k in type(e).__mro__
                           if k.__module__.startswith("gc.")):
                        getattr(e, prop)      # must not raise
    finally:
        _Coord.data = original


def test_windows_cover_reports_unknown_when_the_car_says_nothing():
    """Not 'open'. Otherwise every 'window left open' automation fires forever."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    helpers = load("helpers")
    assert helpers.windows_open({"vehicleStatus": {"additionalVehicleStatus": {
        "climateStatus": {}}}}) is None
    closed = {"vehicleStatus": {"additionalVehicleStatus": {"climateStatus": {
        "winStatusDriver": "2", "winStatusPassenger": "2",
        "winStatusDriverRear": "2", "winStatusPassengerRear": "2"}}}}
    assert helpers.windows_open(closed) is False
    one_open = dict(closed)
    one_open["vehicleStatus"]["additionalVehicleStatus"]["climateStatus"]["winStatusDriver"] = "1"
    assert helpers.windows_open(one_open) is True


def test_a_failed_poll_does_not_strand_an_optimistic_state():
    """`after` releases the optimistic override, so it must run even on failure -
    but not when the task is cancelled, because the entity is gone by then."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    helpers = load("helpers")

    class Hass:
        def __init__(self):
            self.tasks = []

        def async_create_task(self, coro, *a, **k):
            t = asyncio.ensure_future(coro)
            self.tasks.append(t)
            return t

    class Coord:
        def __init__(self, fail):
            self.n, self.fail = 0, fail

        async def async_request_refresh(self):
            self.n += 1
            if self.fail:
                raise RuntimeError("network down")

    async def run(fail):
        h, c, ran = Hass(), Coord(fail), []
        real = asyncio.sleep

        async def fast(d, *a, **k):
            return await real(0)

        asyncio.sleep = fast
        try:
            helpers.schedule_refresh(h, c, 1, 2, after=lambda: ran.append(1))
            await asyncio.gather(*h.tasks)
        finally:
            asyncio.sleep = real
        return bool(ran)

    assert asyncio.run(run(False)) is True, "after() skipped on the happy path"
    assert asyncio.run(run(True)) is True, "a failed refresh stranded the entity"


# ------------------------------- #4: steering-wheel heat, read-only ---------

def _steering(value):
    """One steering-wheel binary sensor over a climateStatus with `value`."""
    import copy
    data = copy.deepcopy(STATUS)
    clim = data["vehicleStatus"]["additionalVehicleStatus"]["climateStatus"]
    if value is None:
        clim.pop("steerWhlHeatingSts", None)
    else:
        clim["steerWhlHeatingSts"] = value

    class C(_Coord):
        pass
    C.data = data
    bs = load("binary_sensor")
    spec = next(s for s in bs.SPECS if s[0] == "steering_wheel_heating")
    return bs.GeelyBinarySensor(C(), FAKE_VIN, "Geely EX5 (0000)", *spec)


def test_the_steering_wheel_reads_one_as_on_and_two_as_off():
    """Inverted relative to every other flag in that table, and measured on a
    real car: 1 while heating at any level, 2 while off (#4). Guessing the usual
    way round would have shown "heating" on a cold wheel, permanently."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    assert _steering("1").is_on is True
    assert _steering("2").is_on is False
    # Levels are not reported separately - any level reads 1.
    assert _steering(1).is_on is True


def test_a_car_without_a_heated_wheel_says_unknown_not_off():
    """The feature is absent on most trims. "Off" would be a claim about
    hardware that is not there; unknown is the truth."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    assert _steering(None).is_on is None


def test_the_steering_wheel_sensor_exists_and_is_read_only():
    """No command for it has ever been verified - every candidate fired at a
    real car returned "operation succeed" and nothing moved - so it must appear
    as a sensor and NOT as a switch anyone could press."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    built = _build_all()
    keys = _keys(built)
    assert "bs_steering_wheel_heating" in keys, sorted(k for k in keys if "steer" in k)
    assert not any("steer" in k for k in keys if k.startswith(("sw_", "select_"))), (
        "a pressable steering-wheel control appeared, with no verified command"
    )


# ------------------------------- #44: speedValidity -------------------------

def _speed(speed, validity=None, **basic_extra):
    """The speed sensor over a basicVehicleStatus with `speed` and a
    `speedValidity` flag. `validity=None` means the flag is absent (a trim
    that never reports it); pass a real value to set it."""
    import copy
    data = copy.deepcopy(STATUS)
    basic = data["vehicleStatus"]["basicVehicleStatus"]
    basic["speed"] = speed
    for k, v in basic_extra.items():
        basic[k] = v
    if validity is not None:
        basic["speedValidity"] = validity
    else:
        basic.pop("speedValidity", None)

    class C(_Coord):
        pass
    C.data = data
    sensor = load("sensor")
    spec = next(s for s in sensor.SENSOR_SPECS if s[0] == "speed")
    return sensor.GeelySensor(C(), FAKE_VIN, "Geely EX5 (0000)", *spec,
                              pressure_unit="psi")


def test_speed_is_unknown_when_speed_validity_is_false():
    """A parked EX5 sends `speed` = 0 with `speedValidity` = false; the two
    agree by coincidence. The flag going false is the car saying the value is
    stale, so the sensor must publish unknown rather than a confident zero."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    assert _speed("0", False).native_value is None
    assert _speed("0", "false").native_value is None


def test_a_stale_nonzero_speed_is_unknown_not_real_motion():
    """The case that actually bites: flag false but the last speed was
    non-zero. Without the guard this is published as real motion, which can
    hold the driving lock and keep polling fast while the car is parked."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    assert _speed("50", False).native_value is None
    assert _speed("50", "false").native_value is None


def test_speed_is_published_when_speed_validity_is_true():
    """The flag true means the value is live, so it passes through unchanged."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    assert _speed("50", True).native_value == 50.0
    assert _speed("50", "true").native_value == 50.0


def test_speed_is_published_when_speed_validity_is_absent():
    """A trim that never reports the flag must keep the old behaviour - the
    guard only fires on an explicit falsy value, not on a missing one."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    assert _speed("50").native_value == 50.0


def test_the_documented_entity_counts_are_the_real_ones():
    """The README promises specific numbers on the first screen, and a new entity
    silently makes them wrong - which is a small lie in the most-read paragraph
    in the repository."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    import io
    import os
    import types
    prop = load("propulsion")
    bev = sum(len(v) for v in _build_all().values())
    hybrid_verdict = types.SimpleNamespace(
        has_tank=True, has_plug=True, charges=True,
        kind=prop.Propulsion.HYBRID, source="declared", declared_raw="phev")
    hybrid = sum(len(v) for v in _build_all(propulsion=hybrid_verdict).values())
    readme = io.open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "README.md"), encoding="utf-8").read()
    assert f"all {bev} entities are on from the start ({hybrid} on a" in readme, (
        f"the README's counts are stale: a battery-only car builds {bev} entities "
        f"and a hybrid {hybrid}")
    assert f"{bev} entities and a PHEV gets {hybrid}." in readme


def test_a_nonsense_pack_size_in_the_options_does_not_break_setup():
    """The field is a number in the UI, but options survive round trips and
    hand-edited storage - and a car that will not load because someone typed
    "60,22" is a far worse outcome than a range figure falling back."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    import types
    sensor = load("sensor")
    for junk in ("60,22", "abc", [], {}):
        hass, entry = _Hass(), _Entry()
        entry.options = {"battery_capacity_kwh": junk}
        hass.data["geely_connect"] = {"e1": {
            "api": object(), "coordinator": _Coord(), "vin": FAKE_VIN,
            "device_name": "Geely EX5 (0000)", "capabilities": {}}}
        got = []
        asyncio.run(sensor.async_setup_entry(
            hass, entry, lambda e, *a, **k: got.extend(list(e))))
        full = next(e for e in got
                    if getattr(e, "_attr_unique_id", "").endswith("_full_range"))
        assert full._capacity_kwh == 0.0, junk
        # And it still reports, by the other method.
        assert full.extra_state_attributes["method"] == "car estimate scaled to 100%"


def test_a_car_without_a_heated_wheel_does_not_claim_to_have_one():
    """v1.27.0 shipped this reading 1=on / 2=off, from an EX5 that has the
    feature. Three Starray payloads then showed a THIRD value: 0, on cars whose
    capability catalogue does not advertise a heated wheel at all. Reported as
    Off, that told most owners their car had one, switched off."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    assert _steering("1").is_on is True, "heating at any level"
    assert _steering("2").is_on is False, "fitted and off"
    assert _steering("0").is_on is None, "0 means not fitted, not off"
    assert _steering(0).is_on is None
    assert _steering(None).is_on is None, "absent means not fitted either"


def test_the_trunk_lock_is_readable_at_all():
    """`trunkLockStatus` sat beside the open/closed sensor in every payload,
    unread. It is the only observable signal that the Unlock Trunk button did
    anything on the cars in #20, where the latch releases without the gate
    moving - "the indicators flashed" was all anyone had to go on."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    import copy
    bs = load("binary_sensor")
    spec = next(s for s in bs.SPECS if s[0] == "trunk_unlocked")

    def lock(value):
        data = copy.deepcopy(STATUS)
        safe = data["vehicleStatus"]["additionalVehicleStatus"]["drivingSafetyStatus"]
        if value is None:
            safe.pop("trunkLockStatus", None)
        else:
            safe["trunkLockStatus"] = value

        class C(_Coord):
            pass
        C.data = data
        return bs.GeelyBinarySensor(C(), FAKE_VIN, "Geely", *spec)

    # device_class lock: is_on True means UNLOCKED. 0 is the unlocked code, and
    # that is measured rather than assumed: across three real payloads this field
    # matched centralLockingStatus exactly - both 1 on a locked car, both 0 on an
    # unlocked one - and lock.py documents that field as 1/2 locked, 0 unlocked.
    assert lock("0").is_on is True
    assert lock("1").is_on is False
    assert lock("2").is_on is False, "double-locked is still locked"
    assert lock(None).is_on is None


def test_every_panel_that_opens_says_closed_rather_than_off():
    """A binary sensor with no device class has no labels to show, so Home
    Assistant falls back to On/Off. The hood was the one opening without a
    class, and it read "Off" in a row of doors reading "Closed" (#40).

    Deliberately a rule over all of them rather than a check of the one that
    was wrong, because the next panel added would land in the same trap - the
    seatbelt is excluded because it is not an opening and On/Off is honest
    for it."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    bs = load("binary_sensor")
    openings = {"door_driver", "door_passenger", "door_rear_left",
                "door_rear_right", "trunk_open", "hood_open"}
    for spec in bs.SPECS:
        if spec[0] not in openings:
            continue
        assert spec[3] is not None, f"{spec[0]} has no device class, so it reads On/Off"


def test_the_park_brake_reads_the_codes_real_cars_send():
    """The map shipped with 0/1 and no car has ever been seen sending either.

    Two EX5s send 3 and 9 instead (#41): an owner watched the brake and read 3
    engaged, 9 released, and a second car's diagnostics attached to #20 shows
    `electricParkBrakeStatus: "3"` while parked with the engine off - which is
    the state a parked car is in. Before this, both of them saw a bare "3"
    where the entity promises a word."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    sensor = load("sensor")
    m = sensor._PARK_BRAKE_MAP
    assert m["3"] == m[3] == "Engaged"
    assert m["9"] == m[9] == "Released"
    # The originals stay: they are unproven, not disproven, and dropping them
    # on an absence would break any car that does use them.
    assert m["0"] == "Released" and m["1"] == "Engaged"


def test_an_unknown_park_brake_code_stays_visible_as_itself():
    """Two codes are known out of a set nobody has enumerated, so a third has
    to arrive as a number somebody can report - not as a guessed label, and not
    as unknown, which would hide it."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    sensor = load("sensor")
    assert sensor._coerce("7", "map", sensor._PARK_BRAKE_MAP) == "7"


def _park_brake(value):
    """The park-brake binary sensor reading one raw code."""
    import copy
    bs = load("binary_sensor")
    spec = next(s for s in bs.SPECS if s[0] == "park_brake_engaged")
    data = copy.deepcopy(STATUS)
    safe = data["vehicleStatus"]["additionalVehicleStatus"]["drivingSafetyStatus"]
    if value is None:
        safe.pop("electricParkBrakeStatus", None)
    else:
        safe["electricParkBrakeStatus"] = value

    class C(_Coord):
        pass
    C.data = data
    return bs.GeelyBinarySensor(C(), FAKE_VIN, "Geely", *spec)


def test_the_park_brake_binary_reads_the_two_measured_codes():
    if not have_homeassistant():
        skip("homeassistant not installed")
    assert _park_brake("3").is_on is True
    assert _park_brake("9").is_on is False


def test_an_unrecognised_park_brake_code_is_unknown_not_released():
    """The point of the entity, and the reporter's own design (#41): 3 and 9
    are the only codes any car has been seen sending, out of a set nobody has
    enumerated. A fourth code is most likely a fault state, and reporting a
    car whose brake state cannot be determined as *released* is the failure
    that matters - it is the direction that reads safe when it is not."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    for code in ("7", 7, "0x3", "", "engaged"):
        assert _park_brake(code).is_on is None, code
    assert _park_brake(None).is_on is None, "a missing field must not read off"


def test_every_other_binary_sensor_still_treats_unknown_as_off():
    """off_values is opt-in. Adding it must not have changed an entity that can
    say what off is - a door that is not open is closed, and always was."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    import copy
    bs = load("binary_sensor")
    spec = next(s for s in bs.SPECS if s[0] == "door_driver")
    data = copy.deepcopy(STATUS)
    data["vehicleStatus"]["additionalVehicleStatus"]["drivingSafetyStatus"][
        "doorOpenStatusDriver"] = "0"

    class C(_Coord):
        pass
    C.data = data
    assert bs.GeelyBinarySensor(C(), FAKE_VIN, "Geely", *spec).is_on is False


def _btm(value):
    """The battery-temperature-maintenance sensor reading one raw value."""
    import copy
    bs = load("binary_sensor")
    spec = next(s for s in bs.SPECS if s[0] == "battery_temp_maintenance")
    data = copy.deepcopy(STATUS)
    data.setdefault("_state", {})
    if value is None:
        data["_state"].pop("btTempActive", None)
    else:
        data["_state"]["btTempActive"] = value

    class C(_Coord):
        pass
    C.data = data
    return bs.GeelyBinarySensor(C(), FAKE_VIN, "Geely", *spec)


def test_battery_temperature_maintenance_reads_the_flag_three_sources_agree_on():
    """The half of #4 open since 4 August, and it needed no new request - the
    field was already in the secondary status block.

    On one EX5 the app shows the toggle on, `_state.btTempActive` reads 1, and
    the vendor's schedule endpoint returns `btTempActive: "true"` beside a
    `scheduledTime` that decodes to exactly the 22:30 the app displays. A
    Starray reads 0, which is the off half."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    assert _btm(1).is_on is True
    assert _btm("1").is_on is True
    assert _btm("true").is_on is True
    assert _btm(0).is_on is False
    assert _btm("false").is_on is False


def test_a_car_that_never_reports_the_flag_says_unknown_not_off():
    """A trim without the feature, or a payload fetched before the secondary
    block arrives, must not claim the maintenance is switched off."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    assert _btm(None).is_on is None
