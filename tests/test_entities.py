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
