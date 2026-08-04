"""Display precision, and why rounding native_value is not enough.

A constant 15.38 km/kWh graphed as a spike between 15.379999999999999 and
15.380000000000003. The state was already `round(x, 2)`; the noise came from
Home Assistant's 5-minute statistics taking a float mean of N identical
samples, then the chart auto-scaling to that 1-ulp range and printing the full
repr on the axis. Only a display precision reaches statistics, so that is what
these tests pin - along with the trap that giving one to a sensor whose state is
a word makes Home Assistant demand a number and raise.
"""
from conftest import FAKE_VIN, have_homeassistant, load
from run import skip


def _sensor():
    if not have_homeassistant():
        skip("homeassistant not installed")
    return load("sensor")


# -------------------------------------------------------------- the rule ---

def test_efficiency_is_shown_to_two_decimals():
    s = _sensor()
    assert s._display_precision("km/kWh", "float") == 2


def test_a_word_valued_sensor_gets_no_precision():
    """Charger Connection reads "Disconnected". HA treats a precision as a
    promise the state is numeric and raises on a string, so a unitless sensor
    must come back None - not 0."""
    assert _sensor()._display_precision(None, "map") is None


def test_an_integer_reading_is_not_given_a_decimal():
    """Electric Range arrives as a whole number of km. The same unit on an
    odometer wants one decimal, so the coercion kind has to break the tie."""
    s = _sensor()
    assert s._display_precision("km", "int") == 0
    assert s._display_precision("km", "float") == 1


def test_an_unknown_unit_is_left_alone():
    """Better no opinion than a wrong one: an unmapped unit keeps HA's default
    rather than being forced to some house number."""
    assert _sensor()._display_precision("furlongs", "float") is None


def test_every_pressure_unit_has_a_precision_not_just_kpa():
    """The defect this caught: the four Tire Front/Rear sensors take their unit
    from the setup choice, so a psi or bar install landed on an unlisted unit
    and got no precision at all."""
    s = _sensor()
    for unit in ("psi", "bar", "kPa"):
        ha_unit = s._PRESSURE_UNIT_TO_HA[unit]
        assert s._display_precision(ha_unit, "float") is not None, unit


def test_pressure_precision_is_the_one_already_declared_beside_the_factor():
    """Derived, not restated: _PRESSURE_FROM_KPA owns these numbers because the
    rounding of the value uses them. A second copy could disagree with the
    rounding actually applied."""
    s = _sensor()
    for unit, (_factor, digits) in s._PRESSURE_FROM_KPA.items():
        assert s._display_precision(s._PRESSURE_UNIT_TO_HA[unit], "float") == digits, unit


def test_the_tire_corner_sensors_honour_the_chosen_unit():
    s = _sensor()
    for unit, expected in (("psi", 1), ("bar", 2), ("kPa", 0)):
        e = s.GeelyTireSensor(_Coord(), FAKE_VIN, "Geely (0000)",
                              "front_left", "Tire Front-Left",
                              ("tyreStatusDriver",), unit)
        assert e.suggested_display_precision == expected, unit


def test_the_curated_tire_sensors_honour_the_chosen_unit_too():
    """The corner sensors carry the chosen unit natively, but the curated four
    report native kPa and *display* the setup choice via the suggested unit.
    Home Assistant applies a suggested precision to the display unit, so
    deriving it from the native unit handed a bar install kPa's zero decimals
    - and "2 bar" cannot tell a flat tire from a full one."""
    s = _sensor()
    row = next(r for r in s.SENSOR_SPECS if r[0] == "tire_pressure_fl")
    for unit, expected in (("psi", 1), ("bar", 2), ("kPa", 0)):
        e = s.GeelySensor(_Coord(), FAKE_VIN, "Geely (0000)", *row,
                          pressure_unit=unit)
        assert e.suggested_display_precision == expected, unit


# ------------------------------------------------- reaches every consumer ---

def test_every_numeric_curated_sensor_declares_a_precision():
    """The point of deriving it from the unit: no spec row can be forgotten.
    A row with a unit but no precision is a graph waiting to show float noise.
    """
    s = _sensor()
    missing = [row[0] for row in (*s.SENSOR_SPECS, *s.HYBRID_SPECS)
               if row[3] is not None
               and s._display_precision(row[3], row[5]) is None]
    assert missing == [], missing


def test_the_mapped_sensors_are_the_only_ones_without_one():
    s = _sensor()
    unitless = {row[0] for row in (*s.SENSOR_SPECS, *s.HYBRID_SPECS)
                if row[3] is None}
    assert unitless == {"engine_state", "park_brake", "charger_connected"}, unitless


def test_derived_sensors_inherit_the_rule_rather_than_restating_it():
    """Each derived class carries the mixin, so none of them can drift from the
    table. Checking the class attribute is checking exactly that."""
    s = _sensor()
    for name in ("GeelyEfficiencySensor", "GeelyChargePowerSensor",
                 "GeelyChargeCurrentSensor", "GeelyChargeVoltageSensor",
                 "GeelyFuelRangeSensor", "GeelyCombinedRangeSensor",
                 "GeelyFullRangeSensor", "GeelyTireSensor",
                 "GeelyLastTripSensor", "GeelyTripInProgressSensor",
                 "GeelyPackPowerSensor"):
        assert issubclass(getattr(s, name), s._AutoPrecision), name


def test_no_class_sets_a_precision_by_hand():
    """A literal `_attr_suggested_display_precision` anywhere is the second way
    to do this, and the copy that drifts. There must be none."""
    import pathlib
    src = pathlib.Path(_sensor().__file__).read_text()
    assert "_attr_suggested_display_precision =" not in src


# ------------------------------------------------------- live sensor values ---

class _Coord:
    data = {"vehicleStatus": {"basicVehicleStatus": {"speed": "0.0"},
            "additionalVehicleStatus": {
                "electricVehicleStatus": {"averPowerConsumption": "6.5",
                                          "chargeLevel": "100",
                                          "statusOfChargerConnection": "0",
                                          "distanceToEmptyOnBatteryOnly": "136"},
                "maintenanceStatus": {}, "runningStatus": {},
                "climateStatus": {}, "drivingSafetyStatus": {}}}}
    last_update_success = True

    def async_add_listener(self, cb, *a, **k):
        return lambda: None


def test_the_efficiency_entity_reports_two_decimals_not_none():
    """End to end: the entity that showed the noise now carries a precision."""
    s = _sensor()
    e = s.GeelyEfficiencySensor(_Coord(), FAKE_VIN, "Geely (0000)")
    assert e.native_value == 15.38
    assert e.suggested_display_precision == 2


def test_the_range_entity_reports_whole_kilometres():
    s = _sensor()
    e = s.GeelyFuelRangeSensor(_Coord(), FAKE_VIN, "Geely (0000)")
    assert e.suggested_display_precision == 0


def test_a_spec_driven_sensor_takes_its_precision_from_its_row():
    """battery is a percentage -> 0 decimals, so 100 rather than 100.0."""
    s = _sensor()
    row = next(r for r in s.SENSOR_SPECS if r[0] == "battery")
    e = s.GeelySensor(_Coord(), FAKE_VIN, "Geely (0000)", *row)
    assert e.suggested_display_precision == 0
    assert e.native_value == 100.0


def test_a_mapped_spec_sensor_stays_non_numeric():
    """The regression that would take out three entities at once: a precision
    here turns "Disconnected" into a ValueError inside HA's state property."""
    s = _sensor()
    row = next(r for r in s.SENSOR_SPECS if r[0] == "charger_connected")
    e = s.GeelySensor(_Coord(), FAKE_VIN, "Geely (0000)", *row)
    assert e.suggested_display_precision is None
    assert e.native_value == "Disconnected"
