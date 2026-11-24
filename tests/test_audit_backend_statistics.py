"""ARCH-8: resettable trip distance and electrical values are measurements."""
from conftest import FAKE_VIN, have_homeassistant, load
from run import skip
from test_computed_sensors import _Coord


def test_all_three_missing_statistics_sensors_are_measurements():
    if not have_homeassistant():
        skip("homeassistant not installed")
    sensor = load("sensor")
    specs = {spec[0]: spec for spec in sensor.SENSOR_SPECS}
    expected_units = {
        "trip_meter_2": "km",
        "discharge_current": "A",
        "discharge_voltage": "V",
    }
    for key, unit in expected_units.items():
        entity = sensor.GeelySensor(_Coord({}), FAKE_VIN, "Fake car", *specs[key])
        assert entity.state_class == sensor.SensorStateClass.MEASUREMENT, key
        assert entity.native_unit_of_measurement == unit, (key, entity.native_unit_of_measurement)
