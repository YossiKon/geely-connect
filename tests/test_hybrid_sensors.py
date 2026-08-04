"""Fuel and engine sensors, and the arithmetic behind the two derived ranges.

The scaling test is the load-bearing one: `odometerOnFuelOnly` arrives in
hectometres, so publishing it raw overstates every distance by 10x. The proof is
in the payload itself - 630 + 332 = 962 against a `tripMeter1` of 96.2 km.
"""
from conftest import FAKE_VIN, have_homeassistant, load
from run import skip


def _status(fuel=None, ev=None, running=None):
    add = {
        "runningStatus": {"fuelLevel": "35.8", "fuelLevelPct": "71",
                          "aveFuelConsumption": "7.1",
                          "aveTraFuelConsumption": "4.9",
                          "engineCoolantTemperature": "17.0",
                          "tripMeter1": "96.2",
                          **(running or {})},
        "fuelStatus": {"odometerOnFuelOnly": "630", **(fuel or {})},
        "electricVehicleStatus": {"odometerOnBatteryOnly": "332",
                                  "distanceToEmptyOnBatteryOnly": "136",
                                  "averTraPowerConsumption": "1.4",
                                  **(ev or {})},
        "drivingBehaviourStatus": {"engineSpeed": "0.000"},
        "maintenanceStatus": {"engineOilHealthLevel": "87",
                              "engineHrsToService": "1425",
                              "odometer": "96.000"},
        "climateStatus": {}, "drivingSafetyStatus": {},
    }
    return {"vehicleStatus": {"basicVehicleStatus": {}, "additionalVehicleStatus": add},
            "_state": {}, "_scheduled_charging": {}}


class _Coord:
    def __init__(self, data):
        self.data = data
        self.last_update_success = True

    def async_add_listener(self, cb, *a, **k):
        return lambda: None


def _sensor_mod():
    if not have_homeassistant():
        skip("homeassistant not installed")
    return load("sensor")


def _spec_sensor(key, data):
    """Build the spec-driven sensor for `key` out of HYBRID_SPECS/SENSOR_SPECS."""
    sensor = _sensor_mod()
    for spec in (*sensor.SENSOR_SPECS, *sensor.HYBRID_SPECS):
        if spec[0] == key:
            return sensor.GeelySensor(_Coord(data), FAKE_VIN, "Geely (0000)", *spec)
    raise AssertionError(f"no spec declares {key!r}")


def _derived(cls_name, data):
    sensor = _sensor_mod()
    return getattr(sensor, cls_name)(_Coord(data), FAKE_VIN, "Geely (0000)")


# ------------------------------------------------------------- scaling ---

def test_the_split_odometers_are_hectometres_and_get_scaled():
    """630 + 332 = 962 -> 96.2 km, exactly what tripMeter1 reads. Published raw
    they claim 630 km on petrol in a car whose odometer reads 96."""
    assert _spec_sensor("mileage_on_fuel", _status()).native_value == 63.0
    assert _spec_sensor("mileage_on_battery", _status()).native_value == 33.2


def test_the_two_halves_add_up_to_the_odometer():
    """The invariant that fixes the scale, and the reason these are lifetime
    totals rather than trip figures: every kilometre was driven either on petrol
    or on the battery, so the split must reconstruct the odometer. A wrong scale
    factor on either half breaks this; matching two hardcoded numbers would not.
    """
    data = _status()
    halves = (_spec_sensor("mileage_on_fuel", data).native_value
              + _spec_sensor("mileage_on_battery", data).native_value)
    odo = _spec_sensor("total_mileage", data).native_value
    assert abs(halves - odo) < 0.5, f"{halves} vs odometer {odo}"


def test_scaling_survives_the_junk_the_api_sends():
    for junk in ("", None, "abc", {}):
        s = _spec_sensor("mileage_on_fuel", _status(fuel={"odometerOnFuelOnly": junk}))
        assert s.native_value is None, junk


def test_zero_distance_is_a_reading_not_an_absence():
    """A car that has never run on petrol reports 0, and 0 is the truth."""
    s = _spec_sensor("mileage_on_fuel", _status(fuel={"odometerOnFuelOnly": "0"}))
    assert s.native_value == 0.0


# -------------------------------------------------- plain fuel readings ---

def test_the_fuel_readings_pass_through_unscaled():
    assert _spec_sensor("fuel_level", _status()).native_value == 35.8
    assert _spec_sensor("fuel_level_pct", _status()).native_value == 71
    assert _spec_sensor("fuel_consumption", _status()).native_value == 7.1
    assert _spec_sensor("fuel_consumption_trip", _status()).native_value == 4.9


def test_the_trip_power_consumption_reads_the_trip_field_not_the_lifetime_one():
    """`averPowerConsumption` and `averTraPowerConsumption` differ by a factor
    of ten here; reading the wrong one is invisible without this."""
    s = _spec_sensor("power_consumption_trip",
                     _status(ev={"averPowerConsumption": "16.4",
                                 "averTraPowerConsumption": "1.4"}))
    assert s.native_value == 1.4


# --------------------------------------------------------- fuel range ---

def test_fuel_range_is_litres_over_consumption():
    """The API has no distanceToEmptyOnFuel field at all, so this is the only
    answer to "how far on the tank". 35.8 L at 7.1 L/100km -> 504 km."""
    v = _derived("GeelyFuelRangeSensor", _status()).native_value
    assert abs(v - 504.2) < 1.0, v


def test_fuel_range_refuses_to_divide_by_zero():
    for junk in ("0", "0.0", "", None, "abc", "-1"):
        s = _derived("GeelyFuelRangeSensor", _status(running={"aveFuelConsumption": junk}))
        assert s.native_value is None, junk


def test_fuel_range_is_absent_without_a_level():
    s = _derived("GeelyFuelRangeSensor", _status(running={"fuelLevel": None}))
    assert s.native_value is None


def test_an_empty_tank_is_zero_kilometres_not_unknown():
    """fuelLevel 0 with a known consumption is a *reading*: the driver ran the
    tank down. Hiding it would blank Combined Range exactly when someone is
    running on the last of both - and a combined_range < X automation would
    never fire. Distinct from the never-fueled car, which has no consumption
    average and stays absent."""
    s = _derived("GeelyFuelRangeSensor", _status(running={"fuelLevel": "0"}))
    assert s.native_value == 0.0
    combined = _derived("GeelyCombinedRangeSensor",
                        _status(running={"fuelLevel": "0"}))
    assert abs(combined.native_value - 136.0) < 0.1, combined.native_value


# ------------------------------------------------------ combined range ---

def test_combined_range_adds_both_halves():
    """136 km electric + 504 km fuel."""
    v = _derived("GeelyCombinedRangeSensor", _status()).native_value
    assert abs(v - 640.2) < 1.5, v


def test_combined_range_is_absent_rather_than_half_an_answer():
    """Reporting the electric half alone as "combined" would read 136 km to a
    driver with a full tank - worse than no entity."""
    no_fuel = _derived("GeelyCombinedRangeSensor",
                       _status(running={"aveFuelConsumption": "0"}))
    assert no_fuel.native_value is None
    no_ev = _derived("GeelyCombinedRangeSensor",
                     _status(ev={"distanceToEmptyOnBatteryOnly": None}))
    assert no_ev.native_value is None


# ------------------------------------------------------------ metadata ---

def test_every_hybrid_key_declares_a_state_class():
    """CONTRIBUTING.md: a numeric sensor without one records no long-term
    statistics, which is only noticed months later when the history is empty."""
    sensor = _sensor_mod()
    for spec in sensor.HYBRID_SPECS:
        key = spec[0]
        assert key in sensor._MEASUREMENT_KEYS or key in sensor._TOTAL_INCREASING_KEYS, key


def test_the_hybrid_keys_do_not_collide_with_the_base_ones():
    sensor = _sensor_mod()
    base = {s[0] for s in sensor.SENSOR_SPECS}
    extra = {s[0] for s in sensor.HYBRID_SPECS}
    assert base & extra == set()


def test_the_cumulative_odometers_are_total_increasing_not_measurement():
    """A measurement state_class on a running total gives a useless statistic."""
    sensor = _sensor_mod()
    for key in ("mileage_on_fuel", "mileage_on_battery"):
        assert key in sensor._TOTAL_INCREASING_KEYS, key
        assert key not in sensor._MEASUREMENT_KEYS, key
