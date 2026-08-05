"""Charging power, and the leg-selection trap behind it.

The car sends two independent volt/amp pairs and never says which is live, and
the DC pair is the pack rather than a charge leg: it reads 349 V parked, and
338.2 V at +52.4 A while driving. No rule over the readings alone can tell a
charge from a drive, so the car's own charging state gates them and the pack
flow gets its own signed entity.

`_status` is therefore disconnected by default and `_charging` is the
plugged-in-and-charging payload; a fixture that sets volts and amps without a
charging status describes something the car never sends.
"""
from conftest import FAKE_VIN, have_homeassistant, load
from run import skip


def _status(**ev):
    base = {"chargeUAct": "0.0", "chargeIAct": "0.000",
            "dcChargeUAct": "349.0", "dcChargeIAct": "0.0",
            "chargeLevel": "100", "statusOfChargerConnection": "0"}
    base.update(ev)
    return {"vehicleStatus": {"basicVehicleStatus": {}, "additionalVehicleStatus": {
        "electricVehicleStatus": base, "maintenanceStatus": {},
        "runningStatus": {}, "climateStatus": {}, "drivingSafetyStatus": {}}},
        "_state": {}, "_scheduled_charging": {}}


def _charging(**ev):
    """The same payload with the car reporting that it is charging (code 3)."""
    ev.setdefault("statusOfChargerConnection", "3")
    return _status(**ev)


class _Coord:
    def __init__(self, data):
        self.data = data
        self.last_update_success = True

    def async_add_listener(self, cb, *a, **k):
        return lambda: None


def _make(cls_name, data):
    if not have_homeassistant():
        skip("homeassistant not installed")
    sensor = load("sensor")
    return getattr(sensor, cls_name)(_Coord(data), FAKE_VIN, "Geely (0000)")


def _power(data):
    return _make("GeelyChargePowerSensor", data).native_value


# ------------------------------------------------------------ leg choice ---

def test_a_parked_car_reads_zero_not_the_pack_voltage_times_nothing():
    """The real payload from an idle car: dcChargeUAct is 349 V with no current.
    Anything other than 0 kW here is a phantom charge on the graph."""
    assert _power(_status()) == 0.0


def test_ac_charging_uses_the_ac_pair():
    """240 V at 30 A -> 7.2 kW, and the idle 349 V DC leg must not win."""
    assert _power(_charging(chargeUAct="240.0", chargeIAct="30.0")) == 7.2


def test_dc_charging_uses_the_dc_pair():
    """400 V at 125 A -> 50 kW on a fast charger, with the AC pair at zero."""
    assert _power(_charging(dcChargeUAct="400.0", dcChargeIAct="125.0")) == 50.0


def test_a_payload_with_no_ac_pair_still_reports_the_fast_charge():
    """A trim that omits the AC keys entirely must not read unknown while DC
    fast charging - one absent pair falls through to the other."""
    data = _charging(dcChargeUAct="400.0", dcChargeIAct="125.0")
    ev = data["vehicleStatus"]["additionalVehicleStatus"]["electricVehicleStatus"]
    del ev["chargeUAct"], ev["chargeIAct"]
    assert _power(data) == 50.0


def test_the_live_leg_is_chosen_by_current_never_by_voltage():
    """The trap: DC volts are always the larger number. If the rule compared
    voltages, an AC charge would be reported at the DC pack voltage."""
    v = _power(_charging(chargeUAct="240.0", chargeIAct="32.0",
                        dcChargeUAct="349.0", dcChargeIAct="0.0"))
    assert abs(v - 7.68) < 0.01, v


def test_a_sense_current_with_no_voltage_does_not_win_the_leg():
    """Measured on the real car, plugged in and not charging: the AC leg reads
    0.2 A at 0.0 V. A "first leg with current" rule picks that leg, so a DC
    fast charge happening alongside this noise reported 0 kW."""
    v = _power(_charging(chargeUAct="0.0", chargeIAct="0.2",
                        dcChargeUAct="400.0", dcChargeIAct="125.0"))
    assert v == 50.0, v


def test_that_same_noise_alone_still_reads_zero():
    """With nothing charging, 0.2 A behind 0 V is 0 kW, not a phantom draw."""
    assert _power(_status(chargeUAct="0.0", chargeIAct="0.2")) == 0.0


def test_the_larger_product_wins_when_both_legs_look_live():
    """Neither a phantom voltage nor a phantom current can beat real power."""
    v = _power(_charging(chargeUAct="240.0", chargeIAct="0.1",
                        dcChargeUAct="349.0", dcChargeIAct="100.0"))
    assert v == 34.9, v


# ------------------------------------------------ measured on the real car ---

def test_driving_is_not_charging_however_large_the_dc_product():
    """The defect this gate exists for, with the numbers it was found on.

    Driving home unplugged, the DC pair read 338.2 V at +52.4 A. Both halves
    are positive, so a product rule called it a 17.7 kW charge and wrote that
    into the long-term statistics of a car whose supply tops out near 1.8 kW.
    """
    assert _power(_status(dcChargeUAct="338.2", dcChargeIAct="52.4")) == 0.0


def test_the_real_charging_payload_reads_the_wall_leg():
    """Charging at 8 A from a 233 V lead, the pack draws -4.4 A at 346.7 V.

    The negative sign is what makes the DC leg lose here, but it only appears
    once a charge is under way - which is why the sign cannot be the gate."""
    v = _power(_charging(chargeUAct="233.0", chargeIAct="7.800",
                         dcChargeUAct="346.7", dcChargeIAct="-4.4"))
    assert abs(v - 1.82) < 0.01, v


def test_plugged_in_but_idle_reads_zero_not_the_sense_current():
    """Code 1/2 is "Plugged in", not charging: the 0.2 A sense current on the
    AC leg is real and means nothing, so the rate is 0 kW."""
    for code in ("1", "2"):
        v = _power(_status(statusOfChargerConnection=code,
                           chargeUAct="0.0", chargeIAct="0.2"))
        assert v == 0.0, (code, v)


# ---------------------------------------------------------- pack power ---

def _pack(data):
    return _make("GeelyPackPowerSensor", data).native_value


def test_pack_power_is_positive_while_driving():
    """The 17.7 kW the charge sensor must not claim is real, and belongs here."""
    v = _pack(_status(dcChargeUAct="338.2", dcChargeIAct="52.4"))
    assert abs(v - 17.72) < 0.01, v


def test_pack_power_is_negative_while_charging():
    """Into the pack is negative, keeping the car's own sign convention."""
    v = _pack(_charging(dcChargeUAct="346.7", dcChargeIAct="-4.4"))
    assert abs(v + 1.53) < 0.01, v


def test_pack_power_is_not_gated_on_the_charger():
    """Unlike the charge sensors: the flow is real with nothing plugged in,
    which is the reason it is a separate entity rather than a mode of one."""
    assert _pack(_status(dcChargeUAct="349.0", dcChargeIAct="0.0")) == 0.0
    assert _pack(_status(dcChargeUAct="340.0", dcChargeIAct="13.2")) != 0.0


def test_pack_power_records_statistics_and_reports_none_when_absent():
    s = _make("GeelyPackPowerSensor", _status())
    assert s.device_class == "power"
    assert s.state_class == "measurement"
    assert s.native_unit_of_measurement == "kW"
    bare = {"vehicleStatus": {"basicVehicleStatus": {},
                              "additionalVehicleStatus": {"electricVehicleStatus": {}}},
            "_state": {}, "_scheduled_charging": {}}
    assert _pack(bare) is None


# --------------------------------------------------------------- absence ---

def test_no_charge_telemetry_at_all_is_unknown_not_zero():
    """A trim that reports no charge fields must not claim a confident 0 kW."""
    bare = {"vehicleStatus": {"basicVehicleStatus": {},
                              "additionalVehicleStatus": {"electricVehicleStatus": {}}},
            "_state": {}, "_scheduled_charging": {}}
    assert _power(bare) is None


def test_junk_readings_do_not_raise():
    for junk in ("", None, "abc", {}):
        v = _power(_status(chargeUAct=junk, chargeIAct=junk))
        assert v is None or v >= 0, f"{junk!r} -> {v!r}"


def test_a_negative_current_is_not_negative_power():
    """Discharge on the same pins (V2L) must not read as negative charging."""
    assert _power(_charging(chargeUAct="240.0", chargeIAct="-10.0")) == 0.0


# ----------------------------------------------- current and voltage pair ---

def test_current_and_voltage_report_the_same_leg_as_power():
    """Split resolvers would let power say DC while current says AC."""
    data = _charging(chargeUAct="240.0", chargeIAct="32.0")
    amps = _make("GeelyChargeCurrentSensor", data).native_value
    volts = _make("GeelyChargeVoltageSensor", data).native_value
    assert (volts, amps) == (240.0, 32.0)
    assert abs(volts * amps / 1000.0 - _power(data)) < 0.01


def test_the_dc_leg_is_reported_whole_when_it_is_the_live_one():
    data = _charging(dcChargeUAct="400.0", dcChargeIAct="125.0")
    assert _make("GeelyChargeVoltageSensor", data).native_value == 400.0
    assert _make("GeelyChargeCurrentSensor", data).native_value == 125.0


# --------------------------------------------------------------- metadata ---

def test_charge_power_is_a_power_entity_so_statistics_work():
    """Without device_class/state_class it records no long-term statistics and
    cannot appear in the energy dashboard - the reason to make it an entity."""
    s = _make("GeelyChargePowerSensor", _status())
    assert s.device_class == "power"
    assert s.state_class == "measurement"
    assert s.native_unit_of_measurement == "kW"


def test_the_raw_charge_fields_are_not_duplicated_by_full_exposure():
    """Each of the four paths a computed sensor owns must be excluded from the
    raw pass, or turning it on yields a twin of every one."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    sensor = load("sensor")
    ev = "vehicleStatus.additionalVehicleStatus.electricVehicleStatus"
    for k in ("chargeUAct", "chargeIAct", "dcChargeUAct", "dcChargeIAct"):
        assert f"{ev}.{k}" in sensor._CURATED_PATHS, k


# ---------------------------------------------------- the DC session of #10 -
# A real 41-minute DC fast charge on an AU-market EX5, logged poll by poll:
# statusOfChargerConnection sat at 1 ("Plugged in") from plug to unplug while
# the car pulled ~92 kW. The DC contactor (dcDcConnectStatus 3) and the sign
# of dcChargeIAct (negative = into the pack) are the signals that actually
# moved. Values below are verbatim samples from that log.

def _dc_session(**ev):
    ev.setdefault("statusOfChargerConnection", "1")
    ev.setdefault("dcDcConnectStatus", "3")
    ev.setdefault("chargerState", "15")
    return _status(**ev)


def test_a_dc_fast_charge_reports_its_power_despite_the_stuck_status():
    """23:21:11 - 459.7 V at -198.9 A, status still 1 -> 91.43 kW, not 0."""
    data = _dc_session(dcChargeUAct="459.7", dcChargeIAct="-198.9")
    assert _power(data) == 91.43


def test_the_dc_taper_steps_read_correctly_too():
    """23:57:59 - 458.5 V at -56.7 A near the top of the charge."""
    assert _power(_dc_session(dcChargeUAct="458.5", dcChargeIAct="-56.7")) == 26.0


def test_dc_current_reports_magnitude_not_direction():
    """The Charge Current entity is device_class current on a charge port:
    198.9 A, not -198.9 A - the sign is the pack's bookkeeping."""
    data = _dc_session(dcChargeUAct="459.7", dcChargeIAct="-198.9")
    assert _make("GeelyChargeCurrentSensor", data).native_value == 198.9


def test_driving_with_a_glitched_contactor_still_reads_zero():
    """The pack pair reads +52.4 A while DRIVING (positive = out of the pack).
    Even if dcDcConnectStatus glitched to 3 on the road, the current's sign
    must keep the phantom 17.7 kW charge off the graph."""
    data = _status(statusOfChargerConnection="0", dcDcConnectStatus="3",
                   dcChargeUAct="338.2", dcChargeIAct="52.4")
    assert _power(data) == 0.0


def test_plugged_idle_after_a_dc_session_reads_zero():
    """00:02:35, just unplugged: 452.0 V, 0.5 A, contactor back to 0."""
    data = _status(statusOfChargerConnection="0", dcDcConnectStatus="0",
                   dcChargeUAct="452.0", dcChargeIAct="0.5")
    assert _power(data) == 0.0


def test_a_contactor_without_a_current_field_is_not_charging():
    """A trim that reports dcDcConnectStatus but no dcChargeIAct must fall
    back to the official field rather than guess."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    sensor = load("sensor")
    data = _status(statusOfChargerConnection="1", dcDcConnectStatus="3")
    ev = data["vehicleStatus"]["additionalVehicleStatus"]["electricVehicleStatus"]
    del ev["dcChargeIAct"]
    assert sensor._is_charging(data) is False


def test_the_charging_switch_follows_the_dc_session():
    """switch.charging gates on the same composite: on mid-session, off after
    unplug - the field it used to read alone never says DC."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    switch = load("switch")
    sensor = load("sensor")
    mid = _dc_session(dcChargeUAct="459.7", dcChargeIAct="-198.9")
    after = _status(statusOfChargerConnection="0", dcDcConnectStatus="0",
                    dcChargeUAct="452.0", dcChargeIAct="0.5")
    assert sensor._is_charging(mid) is True
    assert sensor._is_charging(after) is False


def test_the_connection_label_says_charging_during_a_dc_session():
    """The raw field says 1 ("Plugged in") for the entire DC session; a label
    reading "Plugged in" during a 90 kW charge is practically wrong."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    sensor = load("sensor")
    mid = _dc_session(dcChargeUAct="459.7", dcChargeIAct="-198.9")
    s = sensor.GeelySensor(_Coord(mid), FAKE_VIN, "Geely (0000)",
                           "charger_connected", "Charger Connection",
                           ("vehicleStatus", "additionalVehicleStatus",
                            "electricVehicleStatus", "statusOfChargerConnection"),
                           None, None, "map", sensor._CHARGER_CONNECTION_MAP)
    assert s.native_value == "Charging"
    idle = _status(statusOfChargerConnection="1")
    s2 = sensor.GeelySensor(_Coord(idle), FAKE_VIN, "Geely (0000)",
                            "charger_connected", "Charger Connection",
                            ("vehicleStatus", "additionalVehicleStatus",
                             "electricVehicleStatus", "statusOfChargerConnection"),
                            None, None, "map", sensor._CHARGER_CONNECTION_MAP)
    assert s2.native_value == "Plugged in"


def test_charge_complete_survives_a_dc_session():
    """timeToFullyCharged counted 60 down to 16 during the #10 session while
    the old status gate hid the estimate completely."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    sensor = load("sensor")
    mid = _dc_session(dcChargeUAct="459.7", dcChargeIAct="-198.9",
                      timeToFullyCharged="42")
    s = sensor.GeelyChargeCompleteSensor(_Coord(mid), FAKE_VIN, "Geely (0000)")
    assert s.native_value is not None
    idle = _status(timeToFullyCharged="2047")
    s2 = sensor.GeelyChargeCompleteSensor(_Coord(idle), FAKE_VIN, "Geely (0000)")
    assert s2.native_value is None


def test_an_impossible_ac_pair_loses_to_the_pack():
    """#17, verbatim: chargeUAct 1581 V at 16.3 A on a 240 V / 6 kW wallbox.
    No AC supply reaches 1581 V - the mis-scaled pair published 25.77 kW.
    The pack pair carries the truthful rate and must win."""
    data = _charging(chargeUAct="1581.0", chargeIAct="16.3",
                     dcChargeUAct="460.0", dcChargeIAct="-13.7")
    assert _power(data) == 6.3
    assert _make("GeelyChargeVoltageSensor", data).native_value == 460.0
    assert _make("GeelyChargeCurrentSensor", data).native_value == 13.7


def test_a_plausible_ac_pair_still_wins_by_product():
    """The EU car's honest 233 V x 29.5 A must keep beating the pack pair -
    the gate is a wall against impossible values, not a DC preference."""
    data = _charging(chargeUAct="233.0", chargeIAct="29.5",
                     dcChargeUAct="460.0", dcChargeIAct="-13.7")
    assert _power(data) == 6.87
