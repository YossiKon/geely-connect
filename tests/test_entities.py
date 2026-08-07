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


def _build_all(**bundle_extra):
    """Set up every platform and return {platform: [entities]}.

    `bundle_extra` overrides entries in the hass.data bundle - that is how the
    propulsion verdict reaches the platforms, so it is how the gating is tested.
    """
    hass, entry = _Hass(), _Entry()
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
    real car returned "operation succeed" and moved nothing - so it must appear
    as a sensor and NOT as a switch anyone could press."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    built = _build_all()
    keys = _keys(built)
    assert "bs_steering_wheel_heating" in keys, sorted(k for k in keys if "steer" in k)
    assert not any("steer" in k for k in keys if k.startswith(("sw_", "select_"))), (
        "a pressable steering-wheel control appeared, with no verified command"
    )
