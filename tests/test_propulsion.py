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
    observed. An HEV must not get charging entities it cannot use."""
    p = _mod()
    v = p.classify("\u6df7\u52a8", _status(plug=False))
    assert v.kind is p.Propulsion.HYBRID
    assert v.has_tank is True
    assert v.has_plug is False


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
