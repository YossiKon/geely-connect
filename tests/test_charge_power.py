"""Charging power, and the leg-selection trap behind it.

The car sends two independent volt/amp pairs and never says which is live. The
DC pair reports pack voltage (349 V) even while parked and unplugged, so any
rule that looks at voltage first reports a phantom charge forever. These tests
pin the rule to current.
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
    assert _power(_status(chargeUAct="240.0", chargeIAct="30.0")) == 7.2


def test_dc_charging_uses_the_dc_pair():
    """400 V at 125 A -> 50 kW on a fast charger, with the AC pair at zero."""
    assert _power(_status(dcChargeUAct="400.0", dcChargeIAct="125.0")) == 50.0


def test_the_live_leg_is_chosen_by_current_never_by_voltage():
    """The trap: DC volts are always the larger number. If the rule compared
    voltages, an AC charge would be reported at the DC pack voltage."""
    v = _power(_status(chargeUAct="240.0", chargeIAct="32.0",
                       dcChargeUAct="349.0", dcChargeIAct="0.0"))
    assert abs(v - 7.68) < 0.01, v


def test_a_sense_current_with_no_voltage_does_not_win_the_leg():
    """Measured on the real car, plugged in and not charging: the AC leg reads
    0.2 A at 0.0 V. A "first leg with current" rule picks that leg, so a DC
    fast charge happening alongside this noise reported 0 kW."""
    v = _power(_status(chargeUAct="0.0", chargeIAct="0.2",
                       dcChargeUAct="400.0", dcChargeIAct="125.0"))
    assert v == 50.0, v


def test_that_same_noise_alone_still_reads_zero():
    """With nothing charging, 0.2 A behind 0 V is 0 kW, not a phantom draw."""
    assert _power(_status(chargeUAct="0.0", chargeIAct="0.2")) == 0.0


def test_the_larger_product_wins_when_both_legs_look_live():
    """Neither a phantom voltage nor a phantom current can beat real power."""
    v = _power(_status(chargeUAct="240.0", chargeIAct="0.1",
                       dcChargeUAct="349.0", dcChargeIAct="100.0"))
    assert v == 34.9, v


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
    assert _power(_status(chargeUAct="240.0", chargeIAct="-10.0")) == 0.0


# ----------------------------------------------- current and voltage pair ---

def test_current_and_voltage_report_the_same_leg_as_power():
    """Split resolvers would let power say DC while current says AC."""
    data = _status(chargeUAct="240.0", chargeIAct="32.0")
    amps = _make("GeelyChargeCurrentSensor", data).native_value
    volts = _make("GeelyChargeVoltageSensor", data).native_value
    assert (volts, amps) == (240.0, 32.0)
    assert abs(volts * amps / 1000.0 - _power(data)) < 0.01


def test_the_dc_leg_is_reported_whole_when_it_is_the_live_one():
    data = _status(dcChargeUAct="400.0", dcChargeIAct="125.0")
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
