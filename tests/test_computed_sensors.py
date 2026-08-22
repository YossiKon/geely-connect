"""The sensors the car does not report - we derive them.

These are the ones users notice, and the ones with real arithmetic in them:
division by zero, None operands, unit handling and values that must reset.
"""
from conftest import FAKE_VIN, have_homeassistant, load
from run import skip


def _status(**over):
    ev = {"chargeLevel": "84", "distanceToEmptyOnBatteryOnly": "349",
          "averPowerConsumption": "16.4", "timeToFullyCharged": "95",
          "statusOfChargerConnection": "3"}
    maint = {"odometer": "4646",
             "tyreStatusDriver": "240", "tyreStatusPassenger": "252",
             "tyreStatusDriverRear": "235", "tyreStatusPassengerRear": "238"}
    basic = {"speed": "0", "engineStatus": "engine_off"}
    clim = {}
    ev.update(over.pop("ev", {}))
    maint.update(over.pop("maint", {}))
    basic.update(over.pop("basic", {}))
    clim.update(over.pop("clim", {}))
    return {"vehicleStatus": {
        "basicVehicleStatus": basic,
        "additionalVehicleStatus": {"electricVehicleStatus": ev,
                                    "maintenanceStatus": maint,
                                    "climateStatus": clim, "drivingSafetyStatus": {},
                                    "runningStatus": {}}},
        "_state": {}, "_scheduled_charging": {}}


class _Coord:
    def __init__(self, data):
        self.data = data
        self.last_update_success = True

    def async_add_listener(self, cb, *a, **k):
        return lambda: None


def _make(cls_name, data, *args):
    if not have_homeassistant():
        skip("homeassistant not installed")
    sensor = load("sensor")
    cls = getattr(sensor, cls_name)
    return cls(_Coord(data), FAKE_VIN, "Geely EX5 (0000)", *args)


# ----------------------------------------------------------- efficiency ---

def test_efficiency_is_km_per_kwh():
    s = _make("GeelyEfficiencySensor", _status(ev={"averPowerConsumption": "20.0"}))
    # 20 kWh/100km -> 5 km/kWh
    assert abs(s.native_value - 5.0) < 0.01, s.native_value


def test_efficiency_does_not_divide_by_zero():
    for junk in ("0", "0.0", "", None, "abc", "-1"):
        s = _make("GeelyEfficiencySensor", _status(ev={"averPowerConsumption": junk}))
        v = s.native_value
        assert v is None or v > 0, f"{junk!r} produced {v!r}"


def test_efficiency_is_none_when_the_field_is_absent():
    data = _status()
    del data["vehicleStatus"]["additionalVehicleStatus"]["electricVehicleStatus"]["averPowerConsumption"]
    assert _make("GeelyEfficiencySensor", data).native_value is None


def test_energy_per_distance_sensors_can_be_switched_to_miles():
    """A unit picker only appears when the device class has a converter, and
    energy_distance is the one carrying mi/kWh (#37). Two things have to hold:
    the three entities must go through _ENERGY_DISTANCE rather than naming the
    member directly, since it is absent before Home Assistant 2025.3, and their
    unit strings must stay members of UnitOfEnergyDistance or Home Assistant
    rejects the pair instead of converting it."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    from homeassistant.components.sensor import SensorDeviceClass
    from homeassistant.components.sensor.const import DEVICE_CLASS_UNITS
    from homeassistant.util.unit_conversion import EnergyDistanceConverter

    sensor = load("sensor")
    assert sensor._ENERGY_DISTANCE == SensorDeviceClass.ENERGY_DISTANCE
    allowed = DEVICE_CLASS_UNITS[SensorDeviceClass.ENERGY_DISTANCE]

    eff = _make("GeelyEfficiencySensor", _status())
    assert eff.device_class is sensor._ENERGY_DISTANCE
    assert eff.native_unit_of_measurement in allowed

    for key in ("avg_consumption", "power_consumption_trip"):
        spec = next(x for x in sensor.SENSOR_SPECS if x[0] == key)
        assert spec[4] is sensor._ENERGY_DISTANCE, key
        assert spec[3] in allowed, key

    # 15.9 kWh/100km is what a real EX5 reports; a miles install reads 3.9.
    assert round(EnergyDistanceConverter.convert(
        15.9, "kWh/100km", "mi/kWh"), 1) == 3.9


# ---------------------------------------------------------- full range ---

def test_full_range_extrapolates_to_a_hundred_percent():
    s = _make("GeelyFullRangeSensor", _status(ev={"chargeLevel": "50",
                                                  "distanceToEmptyOnBatteryOnly": "200"}))
    assert abs(s.native_value - 400) < 1, s.native_value


def test_full_range_is_blank_at_a_low_charge_where_it_is_noise():
    s = _make("GeelyFullRangeSensor", _status(ev={"chargeLevel": "5",
                                                  "distanceToEmptyOnBatteryOnly": "20"}))
    assert s.native_value is None


def test_full_range_survives_a_zero_or_missing_charge():
    for junk in ("0", "", None, "abc"):
        s = _make("GeelyFullRangeSensor", _status(ev={"chargeLevel": junk}))
        s.native_value           # must not raise


# ------------------------------------------------------ charge complete ---

def test_charge_complete_is_a_timestamp_while_charging():
    s = _make("GeelyChargeCompleteSensor",
              _status(ev={"statusOfChargerConnection": "3", "timeToFullyCharged": "60"}))
    assert s.native_value is not None
    assert hasattr(s.native_value, "tzinfo"), "must be timezone-aware"


def test_charge_complete_is_blank_when_not_charging():
    s = _make("GeelyChargeCompleteSensor",
              _status(ev={"statusOfChargerConnection": "0", "timeToFullyCharged": "60"}))
    assert s.native_value is None


def test_charge_complete_handles_a_garbled_minute_count():
    # "nan" earns its place: it passes float() and every ordinary comparison
    # (all False), and timedelta(minutes=nan) raises inside the state write.
    for junk in ("", None, "abc", "-5", "nan", "NaN", "inf"):
        s = _make("GeelyChargeCompleteSensor",
                  _status(ev={"statusOfChargerConnection": "3", "timeToFullyCharged": junk}))
        assert s.native_value is None, junk


def test_charge_complete_rejects_the_not_available_sentinel():
    """2047 is 0x7FF - an 11-bit field with every bit set, which is how the car
    says "no estimate". Published verbatim it reads as a 34-hour charge."""
    s = _make("GeelyChargeCompleteSensor",
              _status(ev={"statusOfChargerConnection": "3",
                          "timeToFullyCharged": "2047"}))
    assert s.native_value is None


def test_time_to_full_spec_rejects_the_sentinel_too():
    """The curated minute sensor reads the same field, so it must filter the
    same value - otherwise the two disagree about the same quantity."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    sensor = load("sensor")
    assert sensor._coerce("2047", "minutes") is None
    assert sensor._coerce("95", "minutes") == 95.0
    assert sensor._coerce("0", "minutes") is None
    assert sensor._coerce("abc", "minutes") is None
    spec = next(s for s in sensor.SENSOR_SPECS if s[0] == "time_to_full_min")
    assert spec[5] == "minutes", "time_to_full_min must go through the filter"


# ------------------------------------------------------------- tires ---

def _tire(data, unit="psi"):
    # (key, friendly_name, field-path) as sensor.py builds them
    return _make("GeelyTireSensor", data, "front_left", "Tire Front-Left",
                 ("tyreStatusDriver",), unit)


def test_tire_sensor_converts_to_the_unit_picked_at_setup():
    kpa = 240.0
    expect = {"kPa": kpa, "bar": kpa / 100.0, "psi": kpa / 6.894757}
    for unit, want in expect.items():
        s = _tire(_status(), unit)
        assert s.native_value is not None, f"{unit}: no reading"
        assert abs(s.native_value - want) < 0.5, f"{unit}: {s.native_value} vs {want}"


def test_tire_sensor_exposes_all_three_units_regardless_of_the_choice():
    s = _tire(_status(), "psi")
    attrs = s.extra_state_attributes
    for unit in ("psi", "bar", "kPa"):
        assert unit in attrs, f"{unit} attribute missing"


def test_tire_sensor_is_blank_when_the_car_reports_nothing():
    data = _status()
    data["vehicleStatus"]["additionalVehicleStatus"]["maintenanceStatus"] = {}
    s = _tire(data)
    assert s.native_value is None


def test_tire_sensor_has_no_device_class():
    """A pressure device_class hands the display unit to Home Assistant, which
    is exactly what stopped the setup choice from being honoured."""
    s = _tire(_status())
    assert getattr(s, "_attr_device_class", None) is None


# -------------------------------------------------------------- trips ---

def test_a_trip_is_measured_from_the_odometer_between_engine_on_and_off():
    trip = _make("GeelyLastTripSensor", _status(basic={"engineStatus": "engine_off"}))
    trip._was_running = False
    # engine on at 4646
    trip.coordinator.data = _status(basic={"engineStatus": "engine_running"},
                                    maint={"odometer": "4646"})
    trip._advance()
    assert trip._start_km == 4646.0
    # engine off at 4696 -> 50 km
    trip.coordinator.data = _status(basic={"engineStatus": "engine_off"},
                                    maint={"odometer": "4696"})
    trip._advance()
    assert abs(trip._last_trip - 50.0) < 0.1, trip._last_trip


def test_an_absurd_or_negative_odometer_delta_is_ignored():
    """A misreported odometer must not overwrite a good trip value."""
    trip = _make("GeelyLastTripSensor", _status())
    trip._was_running = True
    trip._start_km = 4646.0
    trip._last_trip = 50.0
    for bad in ("4000", "99999"):     # backwards, and absurdly far
        trip.coordinator.data = _status(basic={"engineStatus": "engine_off"},
                                        maint={"odometer": bad})
        trip._was_running = True
        trip._start_km = 4646.0
        trip._advance()
        assert trip._last_trip == 50.0, f"{bad} overwrote the previous trip"


def test_a_poll_with_nothing_useful_changes_no_trip_state():
    trip = _make("GeelyLastTripSensor", {"vehicleStatus": {}})
    before = (trip._start_km, trip._last_trip, trip._was_running)
    trip._advance()
    assert (trip._start_km, trip._last_trip, trip._was_running) == before


def test_trip_in_progress_reads_zero_when_parked():
    s = _make("GeelyTripInProgressSensor", _status(basic={"engineStatus": "engine_off"}))
    s._start_km = None
    assert s.native_value in (0, 0.0, None)


# ------------------------------------------- the retracted P145 "offset" ---
# A per-series exterior-temperature correction shipped in v1.21.4 and was
# retracted in v1.21.5: one P145 car reported the field a steady +10 above
# its cluster across three synchronized captures, but a second P145 read it
# 10 LOW while parked and 10 HIGH after a drive (#11). No constant can fix
# a field that behaves like that, so the value passes through untouched and
# only the generic offset plumbing remains.

def test_no_series_temperature_correction_is_applied():
    if not have_homeassistant():
        skip("homeassistant not installed")
    sensor = load("sensor")
    assert not hasattr(sensor, "_exterior_temp_offset")


def test_the_offset_plumbing_still_works_for_a_future_calibration():
    """Kept deliberately: the next calibration question will want it, and an
    untested spare part is worse than none."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    sensor = load("sensor")
    data = {"vehicleStatus": {"additionalVehicleStatus": {
        "climateStatus": {"exteriorTemp": "34.0"}}}}
    class _C:
        def __init__(self): self.data = data; self.last_update_success = True
        def async_add_listener(self, cb, *a, **k): return lambda: None
    path = ("vehicleStatus", "additionalVehicleStatus", "climateStatus",
            "exteriorTemp")
    plain = sensor.GeelySensor(_C(), "VIN000", "Geely (0000)", "exterior_temp",
                               "Exterior Temperature", path, "°C", None, "float", None)
    assert plain.native_value == 34.0
    shifted = sensor.GeelySensor(_C(), "VIN000", "Geely (0000)", "exterior_temp",
                                 "Exterior Temperature", path, "°C", None, "float",
                                 None, value_offset=-10.0)
    assert shifted.native_value == 24.0


# ------------------------- Range At Full Charge: two answers, one honest state
# An owner circled this figure: his card read 426 km while the same card showed
# his lifetime consumption at 22.7 kWh/100 km, which on a 60.22 kWh pack is 265.
# Both numbers are real. 426 is the car's own optimistic estimate scaled up - it
# lands near the WLTP figure for that pack - and 265 is what this car actually
# does. The pack size is not in any payload and cannot be guessed (the EX5 alone
# ships 49.52, 60.22 and 68.39 kWh, and the rated range moves again with the
# trim's wheels), so it is configured, and only then does the honest figure win.

def test_the_measured_figure_wins_once_the_pack_size_is_known():
    s = _make("GeelyFullRangeSensor",
              _status(ev={"chargeLevel": "60", "distanceToEmptyOnBatteryOnly": "256",
                          "averPowerConsumption": "22.7"}),
              60.22)
    # 60.22 kWh at 22.7 kWh/100km
    assert s.native_value == 265
    a = s.extra_state_attributes
    assert a["method"] == "measured consumption"
    assert a["at_measured_consumption_km"] == 265
    # The optimistic one is still there to be compared against - this is the
    # 426 from the report.
    assert a["car_estimate_scaled_km"] == 427
    assert a["battery_capacity_kwh"] == 60.22


def test_without_a_pack_size_nothing_changes_for_anyone():
    s = _make("GeelyFullRangeSensor",
              _status(ev={"chargeLevel": "60", "distanceToEmptyOnBatteryOnly": "256",
                          "averPowerConsumption": "22.7"}))
    assert s.native_value == 427
    a = s.extra_state_attributes
    assert a["method"] == "car estimate scaled to 100%"
    assert a["at_measured_consumption_km"] is None
    assert a["battery_capacity_kwh"] is None


def test_a_pack_size_with_no_consumption_yet_falls_back_rather_than_blanking():
    """A car fresh from the factory reports no lifetime average. Better the
    optimistic number than an empty tile."""
    # The fixture supplies a lifetime average by default; a new car has none.
    data = _status(ev={"chargeLevel": "60", "distanceToEmptyOnBatteryOnly": "256"})
    del data["vehicleStatus"]["additionalVehicleStatus"][
        "electricVehicleStatus"]["averPowerConsumption"]
    sensor = load("sensor")
    s = sensor.GeelyFullRangeSensor(_Coord(data), FAKE_VIN, "Geely EX5 (0000)", 60.22)
    assert s.native_value == 427
    assert s.extra_state_attributes["method"] == "car estimate scaled to 100%"
    for junk in ("0", "-3", "", None, "abc"):
        s = _make("GeelyFullRangeSensor",
                  _status(ev={"chargeLevel": "60",
                              "distanceToEmptyOnBatteryOnly": "256",
                              "averPowerConsumption": junk}), 60.22)
        assert s.native_value == 427, junk


def test_the_measured_figure_stands_alone_when_the_car_estimate_is_unusable():
    """Below 10% charge the extrapolation is noise and used to blank the entity.
    With a pack size the measured figure does not depend on the charge at all."""
    s = _make("GeelyFullRangeSensor",
              _status(ev={"chargeLevel": "4", "distanceToEmptyOnBatteryOnly": "11",
                          "averPowerConsumption": "22.7"}), 60.22)
    assert s.native_value == 265
    assert s.extra_state_attributes["car_estimate_scaled_km"] is None


# ---------------------- the exterior temperature, and why it is opt-in --------
# Every synchronised sample anyone produced reads exactly +10.0 against the car's
# own cluster: three on a Starray (15->25, 15->25, 24->34), one on an EX5
# (22->32), one after a drive (19->29). A sixth, on a car parked for hours, read
# 19.7 real against 10 reported - ten the other way. A shipped constant was
# retracted for that reason (v1.21.5); this is the owner's own measurement.

def test_the_offset_moves_only_the_exterior_temperature():
    if not have_homeassistant():
        skip("homeassistant not installed")
    import asyncio
    import types
    sensor = load("sensor")

    class Hass:
        def __init__(self):
            self.data = {"geely_connect": {"e1": {
                "api": object(), "coordinator": _Coord(_status(
                    clim={"exteriorTemp": "25.0", "interiorTemp": "21.1"})),
                "vin": FAKE_VIN, "device_name": "Geely", "capabilities": {}}}}

    entry = types.SimpleNamespace(entry_id="e1",
                                  data={"vin": FAKE_VIN},
                                  options={"exterior_temp_offset": -10})
    got = []
    asyncio.run(sensor.async_setup_entry(Hass(), entry,
                                         lambda e, *a, **k: got.extend(list(e))))
    by_key = {e._attr_unique_id.rsplit(f"{FAKE_VIN}_", 1)[-1]: e
              for e in got if hasattr(e, "_attr_unique_id")}
    # The Starray pair from #11: cluster said 15, the cloud said 25.
    assert by_key["exterior_temp"].native_value == 15.0
    # Nothing else moves - the interior reading was never in question.
    assert by_key["interior_temp"].native_value == 21.1


def test_no_offset_is_applied_unless_someone_asks():
    if not have_homeassistant():
        skip("homeassistant not installed")
    import asyncio
    import types
    sensor = load("sensor")

    class Hass:
        def __init__(self):
            self.data = {"geely_connect": {"e1": {
                "api": object(), "coordinator": _Coord(_status(
                    clim={"exteriorTemp": "25.0"})),
                "vin": FAKE_VIN, "device_name": "Geely", "capabilities": {}}}}

    for options in ({}, {"exterior_temp_offset": 0},
                    {"exterior_temp_offset": "not a number"}):
        entry = types.SimpleNamespace(entry_id="e1", data={"vin": FAKE_VIN},
                                      options=options)
        got = []
        asyncio.run(sensor.async_setup_entry(Hass(), entry,
                                            lambda e, *a, **k: got.extend(list(e))))
        ext = next(e for e in got if getattr(e, "_attr_unique_id", "")
                   .endswith("_exterior_temp"))
        assert ext.native_value == 25.0, options


def test_the_tyre_temperatures_are_read_at_last():
    """In every payload the whole time, exposed by nothing. On a car that has
    stood a while they are the only ambient measurement in the payload that is
    not ten degrees out - #11's own diagnostics had them at 19-21 while
    exteriorTemp said 25 and the cluster said 15."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    data = _status(maint={"tyreTempDriver": "21.0", "tyreTempPassenger": "21.0",
                          "tyreTempDriverRear": "19.0",
                          "tyreTempPassengerRear": "19.0"})
    sensor = load("sensor")
    got = {}
    for key, want in (("tire_temp_fl", 21.0), ("tire_temp_fr", 21.0),
                      ("tire_temp_rl", 19.0), ("tire_temp_rr", 19.0)):
        spec = next(x for x in sensor.SENSOR_SPECS if x[0] == key)
        e = sensor.GeelySensor(_Coord(data), FAKE_VIN, "Geely", *spec)
        got[key] = e.native_value
        assert e.native_value == want, (key, e.native_value)
    assert len(got) == 4


# ------------------ #24: the car stops reporting when it sleeps ---------------
# An owner graphed his exterior AND interior temperatures against two real
# sensors over 24 hours. Both Geely lines sat flat - the outside one at 35 C -
# from midnight to 14:00 while the real air went 13 -> 9 -> 19, and moved only
# when he shifted the car. A poll every thirty seconds republishes a snapshot the
# car has not refreshed, and the recorder draws that as a live measurement.

def _with_update_time(ms):
    data = _status(clim={"exteriorTemp": "35.0", "interiorTemp": "17.3"})
    data["vehicleStatus"]["updateTime"] = ms
    return data


def test_the_car_reported_at_sensor_reads_the_cars_own_stamp():
    if not have_homeassistant():
        skip("homeassistant not installed")
    import datetime as dt
    s = _make("GeelyCarReportedAtSensor", _with_update_time("1785990368223"))
    got = s.native_value
    assert got is not None
    assert got.tzinfo is not None, "a timestamp entity needs an aware datetime"
    assert got == dt.datetime.fromtimestamp(1785990368.223, dt.timezone.utc)


def test_a_car_that_sent_no_stamp_says_nothing_rather_than_1970():
    if not have_homeassistant():
        skip("homeassistant not installed")
    for junk in (None, "", "0", 0, "-1", "abc", {}):
        s = _make("GeelyCarReportedAtSensor", _with_update_time(junk))
        assert s.native_value is None, junk


def test_the_two_temperatures_carry_their_own_age():
    """So that a fourteen-hour-old reading does not have to be inferred from a
    flat line on a graph."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    import datetime as dt
    sensor = load("sensor")
    fourteen_hours_ago = (dt.datetime.now(dt.timezone.utc)
                          - dt.timedelta(hours=14)).timestamp() * 1000
    data = _with_update_time(str(int(fourteen_hours_ago)))
    for key in ("exterior_temp", "interior_temp"):
        spec = next(x for x in sensor.SENSOR_SPECS if x[0] == key)
        e = sensor.GeelySensor(_Coord(data), FAKE_VIN, "Geely", *spec)
        a = e.extra_state_attributes
        assert a is not None, key
        assert 835 < a["age_minutes"] < 845, (key, a)
        assert a["car_reported_at"].endswith("+00:00"), a


def test_nothing_else_is_cluttered_with_an_age():
    """Every field in the payload ages together; these are the two anyone holds a
    thermometer against, and 70-odd entities carrying the same attribute would be
    noise."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    sensor = load("sensor")
    data = _with_update_time("1785990368223")
    for key in ("battery", "range", "speed", "total_mileage", "12v_battery"):
        spec = next(x for x in sensor.SENSOR_SPECS if x[0] == key)
        e = sensor.GeelySensor(_Coord(data), FAKE_VIN, "Geely", *spec)
        assert e.extra_state_attributes is None, key
