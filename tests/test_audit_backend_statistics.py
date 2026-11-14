"""ARCH-8: resettable trip distance and electrical values are measurements."""
from conftest import FAKE_VIN, load
from test_computed_sensors import _Coord


def test_all_three_missing_statistics_sensors_are_measurements():
    sensor = load("sensor")
    for key in ("trip_meter_2", "discharge_current", "discharge_voltage"):
        entity = sensor.GeelySensor(_Coord({}), FAKE_VIN, "Fake car", key,
                                    "Fake reading", (), "km", None, "float")
        assert entity.state_class == sensor.SensorStateClass.MEASUREMENT, key
