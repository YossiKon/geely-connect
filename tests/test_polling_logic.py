"""Poll pacing.

Geely allows one session per account, so every poll briefly signs the owner's
phone app out. These functions decide how often that happens, which makes them
the most user-visible logic in the integration.
"""
import importlib.util
import io
import os
import types

from conftest import PKG, have_homeassistant, load
from run import skip


def _coordinator_module():
    """__init__.py imports Home Assistant, so load it only when available."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    name = "gc_init"
    if name in sys_modules():
        return sys_modules()[name]
    import sys
    if "gc" not in sys.modules:
        pkg = types.ModuleType("gc")
        pkg.__path__ = [PKG]
        sys.modules["gc"] = pkg
    spec = importlib.util.spec_from_file_location("gc.__init__",
                                                  os.path.join(PKG, "__init__.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gc.__init__"] = sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def sys_modules():
    import sys
    return sys.modules


def _status(*, speed=None, charger=None, charge=None, locked=None):
    return {"vehicleStatus": {
        "basicVehicleStatus": {"speed": speed},
        "additionalVehicleStatus": {
            "electricVehicleStatus": {"statusOfChargerConnection": charger,
                                      "chargeLevel": charge},
            "drivingSafetyStatus": {"centralLockingStatus": locked},
        }}}


# ------------------------------------------------------------------ flags ---

def test_charging_is_detected_from_the_connection_status():
    m = _coordinator_module()
    assert m._poll_flags(_status(charger="3")) == (True, False)
    for other in ("0", "1", "2", None):
        assert m._poll_flags(_status(charger=other))[0] is False, other


def test_driving_is_any_positive_speed():
    m = _coordinator_module()
    assert m._poll_flags(_status(speed="42"))[1] is True
    assert m._poll_flags(_status(speed="0"))[1] is False


def test_a_garbled_speed_does_not_take_the_poll_down():
    m = _coordinator_module()
    for junk in (None, "", "fast", {}, []):
        assert m._poll_flags(_status(speed=junk)) == (False, False), junk


def test_flags_tolerate_a_completely_missing_payload():
    m = _coordinator_module()
    for junk in ({}, None, "not a dict", []):
        assert m._poll_flags(junk) == (False, False), junk


# -------------------------------------------------------------- signature ---

def test_the_signature_changes_when_something_meaningful_changes():
    m = _coordinator_module()
    base = _status(charge="80", charger="0", locked="1", speed="0")
    assert m._poll_signature(base) == m._poll_signature(_status(
        charge="80", charger="0", locked="1", speed="0"))
    for field in ("charge", "charger", "locked", "speed"):
        changed = dict(charge="80", charger="0", locked="1", speed="0")
        changed[field] = "99"
        assert m._poll_signature(base) != m._poll_signature(_status(**changed)), field


def test_the_signature_survives_an_empty_payload():
    m = _coordinator_module()
    assert m._poll_signature({}) == m._poll_signature(None)


# --------------------------------------------------------------- interval ---

def test_charging_or_driving_uses_the_fast_interval():
    m = _coordinator_module()
    const = load("const")
    for name, p in const.POLL_PROFILES.items():
        if p.get("manual"):
            continue
        for data in (_status(charger="3"), _status(speed="60")):
            assert m._adaptive_interval(data, 0, p).total_seconds() == p["fast"], name


def _at_hour(m, hour):
    """Pin the module's clock: quiet hours are wall-clock, tests must not be."""
    import contextlib
    import datetime

    @contextlib.contextmanager
    def pinned():
        real = m.dt_util
        m.dt_util = types.SimpleNamespace(
            now=lambda: datetime.datetime(2026, 1, 1, hour, 0, 0))
        try:
            yield
        finally:
            m.dt_util = real
    return pinned()


def test_a_parked_car_backs_off_towards_the_cap_but_never_past_it():
    m = _coordinator_module()
    const = load("const")
    p = const.POLL_PROFILES["normal"]
    parked = _status(speed="0", charger="0")
    with _at_hour(m, 12):
        seen = [m._adaptive_interval(parked, i, p).total_seconds()
                for i in range(0, 10)]
    assert seen[0] == p["base"], seen[0]
    assert seen == sorted(seen), f"back-off is not monotonic: {seen}"
    assert max(seen) <= p["cap"], f"exceeded the cap: {max(seen)}"


def test_quiet_hours_park_the_interval_at_the_cap():
    m = _coordinator_module()
    const = load("const")
    p = const.POLL_PROFILES["normal"]
    parked = _status(speed="0", charger="0")
    with _at_hour(m, 3):
        assert m._adaptive_interval(parked, 0, p).total_seconds() == p["cap"]
        # Charging or driving still wins over quiet hours.
        assert m._adaptive_interval(_status(charger="3"), 0, p).total_seconds() == p["fast"]


def test_back_off_stops_growing_so_the_interval_cannot_run_away():
    m = _coordinator_module()
    const = load("const")
    p = const.POLL_PROFILES["eco"]
    parked = _status(speed="0")
    assert (m._adaptive_interval(parked, 100, p)
            == m._adaptive_interval(parked, 1000, p))


def test_eco_is_always_gentler_than_live():
    m = _coordinator_module()
    const = load("const")
    parked = _status(speed="0")
    for streak in (0, 1, 3, 8):
        eco = m._adaptive_interval(parked, streak, const.POLL_PROFILES["eco"])
        live = m._adaptive_interval(parked, streak, const.POLL_PROFILES["live"])
        assert eco > live, streak


def test_device_name_ends_with_the_last_four_vin_characters():
    """Two cars of the same model must not collide into one device name."""
    m = _coordinator_module()
    name = m._resolve_device_name({"vin": "L6T00000000001234",
                                   "vehicle_model_code": "E245-J1"})
    assert name.endswith("(1234)"), name
    assert "L6T00000000001234" not in name, "full VIN in the device name"
