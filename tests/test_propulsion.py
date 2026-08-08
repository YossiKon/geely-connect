"""Which car gets which entities.

The gate decides whether a user sees fuel entities at all, so the failure modes
are both user-visible and silent: a BEV given fuel entities shows a row of
`unavailable` tiles, and a PHEV denied them shows half a car with no error
anywhere. Both are covered here, including the empty-`powerType` case that every
entry created before the metadata refresh carried it will hit.
"""
from conftest import have_homeassistant, load
from run import skip


def _mod():
    if not have_homeassistant():
        skip("homeassistant not installed")
    return load("propulsion")


def _status(fuel=True, plug=True, config=True):
    add = {}
    if fuel:
        add["fuelStatus"] = {"odometerOnFuelOnly": "630"}
        add["runningStatus"] = {"fuelLevel": "35.8", "fuelLevelPct": "71",
                                "aveFuelConsumption": "7.1"}
    else:
        add["runningStatus"] = {"tripMeter1": "96.2"}
    if plug:
        add["electricVehicleStatus"] = {"chargeLevel": "100",
                                        "statusOfChargerConnection": "0",
                                        "distanceToEmptyOnBatteryOnly": "136"}
    vs = {"additionalVehicleStatus": add, "basicVehicleStatus": {}}
    if config:
        vs["configuration"] = {"propulsionType": "3", "fuelType": "2"}
    return {"vehicleStatus": vs}


# ------------------------------------------------------------- declared ---

def test_the_wordings_the_server_actually_uses():
    p = _mod()
    assert p.declared("\u6df7\u52a8") is p.Propulsion.HYBRID          # hybrid, AU account
    assert p.declared("\u63d2\u7535\u6df7\u52a8") is p.Propulsion.HYBRID
    assert p.declared("\u7eaf\u7535\u52a8") is p.Propulsion.ELECTRIC   # pure electric
    assert p.declared("PHEV") is p.Propulsion.HYBRID
    assert p.declared("BEV") is p.Propulsion.ELECTRIC
    assert p.declared("Plug-in Hybrid") is p.Propulsion.HYBRID
    assert p.declared("Battery Electric") is p.Propulsion.ELECTRIC
    assert p.declared("Petrol") is p.Propulsion.FUEL


def test_short_acronyms_are_whole_tokens_not_substrings():
    """"ev" sits inside plenty of words and "ice" inside "service" - matching
    those as substrings would classify a car by accident."""
    p = _mod()
    assert p.declared("Service Vehicle") is p.Propulsion.UNKNOWN
    assert p.declared("Seven") is p.Propulsion.UNKNOWN
    assert p.declared("EV") is p.Propulsion.ELECTRIC
    assert p.declared("ICE") is p.Propulsion.FUEL


def test_an_unknown_or_empty_wording_is_not_a_guess():
    p = _mod()
    for junk in ("", None, "   ", "wibble", "\u4e0d\u77e5\u9053"):
        assert p.declared(junk) is p.Propulsion.UNKNOWN, junk


# -------------------------------------------------------------- verdict ---

def test_a_declared_hybrid_gets_both_halves():
    p = _mod()
    v = p.classify("\u6df7\u52a8", _status())
    assert v.kind is p.Propulsion.HYBRID
    assert (v.has_tank, v.has_plug) == (True, True)
    assert v.source == "declared"
    # The corroborating fields ride along for bug reports, and decide nothing.
    assert (v.propulsion_type, v.fuel_type) == ("3", "2")


def test_a_declared_ev_never_gets_fuel_entities():
    """The whole point of the gate: an EX5 owner must not see fuel tiles."""
    p = _mod()
    v = p.classify("\u7eaf\u7535\u52a8", _status(fuel=False))
    assert v.kind is p.Propulsion.ELECTRIC
    assert v.has_tank is False
    assert v.has_plug is True


def test_a_blank_power_type_falls_back_to_what_the_car_reports():
    """Every entry created before the refresh carried powerType has "" here.
    Deciding "no fuel" from that would hide half of a real PHEV."""
    p = _mod()
    hybrid = p.classify("", _status())
    assert hybrid.kind is p.Propulsion.HYBRID
    assert hybrid.has_tank is True
    assert hybrid.source == "observed"

    ev = p.classify("", _status(fuel=False))
    assert ev.kind is p.Propulsion.ELECTRIC
    assert ev.has_tank is False


def test_an_unrecognised_wording_falls_back_too():
    p = _mod()
    v = p.classify("Nachrichtenantrieb", _status())
    assert v.kind is p.Propulsion.HYBRID
    assert v.source == "observed"
    assert v.declared_raw == "Nachrichtenantrieb", "the raw string must survive for the log"


def test_a_declared_hybrid_with_no_charge_block_is_a_non_plug_hybrid():
    """`混动` alone cannot distinguish a PHEV from an HEV, so the plug is
    observed. An HEV must not get charging entities it cannot use - which is
    what `charges` answers for the platforms."""
    p = _mod()
    v = p.classify("\u6df7\u52a8", _status(plug=False))
    assert v.kind is p.Propulsion.HYBRID
    assert v.has_tank is True
    assert v.has_plug is False
    assert v.charges is False


def test_a_declared_petrol_car_is_a_tank_with_no_plug():
    """The FUEL branch: a fuel burner gets its tank half and no charging."""
    p = _mod()
    v = p.classify("\u6c7d\u6cb9", _status(plug=False))
    assert v.kind is p.Propulsion.FUEL
    assert v.has_tank is True
    assert v.has_plug is False
    assert v.charges is False


def test_a_declared_petrol_car_with_charge_telemetry_warns_and_keeps_no_plug():
    """Declared FUEL against an observed plug is a real contradiction - unlike
    the hybrid case - so it must be reported, and the declaration decides."""
    p = _mod()
    v, warned = _warnings_from(lambda: p.classify("\u6c7d\u6cb9", _status()))
    assert v.kind is p.Propulsion.FUEL
    assert v.has_plug is False
    assert warned, "a fuel car reporting charge telemetry went unreported"


def test_charges_stays_permissive_without_positive_no_plug_evidence():
    """The gate only closes on evidence. UNKNOWN keeps the pre-hybrid set."""
    p = _mod()
    assert p.classify(None, None).charges is True
    assert p.classify("\u7eaf\u7535\u52a8", _status(fuel=False)).charges is True
    assert p.classify("\u6df7\u52a8", _status()).charges is True


def _warnings_from(fn):
    """Run fn while collecting WARNING records off the propulsion logger."""
    import logging
    logger = logging.getLogger("gc.propulsion")
    got: list[str] = []

    class _Grab(logging.Handler):
        def emit(self, record):
            if record.levelno >= logging.WARNING:
                got.append(record.getMessage())

    h = _Grab()
    logger.addHandler(h)
    try:
        result = fn()
    finally:
        logger.removeHandler(h)
    return result, got


def test_the_declaration_wins_a_contradiction_but_does_not_bury_it():
    p = _mod()
    v, warned = _warnings_from(lambda: p.classify("\u7eaf\u7535\u52a8", _status()))
    assert v.kind is p.Propulsion.ELECTRIC, "the declared value decides"
    assert v.has_tank is False
    assert v.source == "declared"
    assert warned, "a car contradicting its own powerType must be reported"


def test_a_non_plug_hybrid_is_not_reported_as_a_contradiction():
    """A warning on every HEV is how a log stops being read."""
    p = _mod()
    _, warned = _warnings_from(lambda: p.classify("\u6df7\u52a8", _status(plug=False)))
    assert warned == [], warned


def test_an_unreadable_payload_degrades_to_the_ev_entity_set():
    """What every install had before this module existed - never a crash."""
    p = _mod()
    for junk in (None, {}, {"vehicleStatus": {}}, {"vehicleStatus": {"additionalVehicleStatus": {}}}):
        v = p.classify(None, junk)
        assert v.kind is p.Propulsion.UNKNOWN, junk
        assert v.has_tank is False, junk


def test_an_empty_fuel_block_is_not_a_tank():
    """Presence of the container says nothing; only a reading does."""
    p = _mod()
    v = p.classify("", {"vehicleStatus": {"additionalVehicleStatus": {
        "fuelStatus": {}, "runningStatus": {"fuelLevel": ""}}}})
    assert v.has_tank is False


def test_a_nulled_fuel_block_is_not_a_tank_either():
    """Backends commonly send the full schema with every value blank. That is
    a shape, not a tank - a BEV whose entry also lost its powerType must not
    grow a row of dead fuel entities from it."""
    p = _mod()
    v = p.classify("", {"vehicleStatus": {"additionalVehicleStatus": {
        "fuelStatus": {"odometerOnFuelOnly": None, "fuelUpDate": ""}}}})
    assert v.has_tank is False
    assert v.kind is p.Propulsion.UNKNOWN


# --------------------------------------------------- #28: a zero is not a tank
# An Australian EX5 - battery only - reports `aveFuelConsumption: 0`, which read
# as tank evidence. The entity set was still right (the declaration wins), but
# its owner was warned on every restart that the integration disagreed with his
# car about what it was. That is how a log stops being read.

def test_a_zero_fuel_reading_is_not_evidence_of_a_tank():
    p = _mod()
    for zero in ("0", 0, "0.0", 0.0, "0.00"):
        st = {"vehicleStatus": {"additionalVehicleStatus": {
            "electricVehicleStatus": {"chargeLevel": "61"},
            "runningStatus": {"aveFuelConsumption": zero, "fuelLevel": zero}}}}
        assert p._observed_tank(st) is False, zero


def test_the_battery_only_ex5_starts_up_silently():
    """The whole of #28: right answer, no alarm."""
    p = _mod()
    st = {"vehicleStatus": {"additionalVehicleStatus": {
        "electricVehicleStatus": {"chargeLevel": "61",
                                  "distanceToEmptyOnBatteryOnly": "256"},
        "runningStatus": {"aveFuelConsumption": "0", "fuelLevel": "0"}}}}
    v, warned = _warnings_from(lambda: p.classify("Electric", st))
    assert v.kind is p.Propulsion.ELECTRIC
    assert v.has_tank is False and v.has_plug is True
    assert warned == [], warned


def test_a_real_fuel_reading_is_still_evidence():
    """The strict rule must not blind the fallback to an actual tank."""
    p = _mod()
    for real in ("31", 31, "6.4", "0.1", "E"):
        st = {"vehicleStatus": {"additionalVehicleStatus": {
            "runningStatus": {"fuelLevel": real}}}}
        assert p._observed_tank(st) is True, real


def test_a_zero_charge_is_still_evidence_of_a_battery():
    """The asymmetry is deliberate: a BEV flat at 0%, unplugged, reports zeros -
    and reading that as "no battery" would strip the charging entities off the
    car that needs them most."""
    p = _mod()
    st = {"vehicleStatus": {"additionalVehicleStatus": {
        "electricVehicleStatus": {"chargeLevel": "0",
                                  "statusOfChargerConnection": "0",
                                  "distanceToEmptyOnBatteryOnly": "0"}}}}
    assert p._observed_plug(st) is True
    assert p.classify(None, st).charges is True


def test_a_hybrid_that_has_burned_fuel_is_unaffected():
    """The narrow cost of the strict rule, bounded: only a car with an
    unrecognised powerType AND no fuel ever burned reads as electric."""
    p = _mod()
    st = {"vehicleStatus": {"additionalVehicleStatus": {
        "electricVehicleStatus": {"chargeLevel": "40"},
        "runningStatus": {"fuelLevel": "0", "aveFuelConsumption": "6.4"}}}}
    v = p.classify("unmapped wording", st)
    assert v.kind is p.Propulsion.HYBRID, "a burned-fuel average still counts"
    assert v.has_tank is True
