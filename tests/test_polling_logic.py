"""Poll pacing.

Geely allows one session per account, so every poll briefly signs the owner's
phone app out. These functions decide how often that happens, which makes them
the most user-visible logic in the integration.
"""
import importlib.util
import io
import os
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


def _status(*, speed=None, charger=None, charge=None, locked=None,
            engine=None, odo=None):
    return {"vehicleStatus": {
        "basicVehicleStatus": {"speed": speed, "engineStatus": engine},
        "additionalVehicleStatus": {
            "electricVehicleStatus": {"statusOfChargerConnection": charger,
                                      "chargeLevel": charge},
            "drivingSafetyStatus": {"centralLockingStatus": locked},
            "maintenanceStatus": {"odometer": odo},
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


# ------------------------------------------- a trip that stops at a light ---
# Speed reads 0 at every red light, and treating that as parked cost the fast
# interval exactly when live data matters, then compounded into the 15-minute
# cap mid-drive (#21).

def test_a_running_car_at_a_standstill_still_counts_as_driving():
    m = _coordinator_module()
    for word in ("engine_running", "running", "ENGINE_RUNNING", "on", "1", 1):
        assert m._poll_flags(_status(speed="0", engine=word))[1] is True, word


def test_a_parked_car_is_still_parked():
    m = _coordinator_module()
    for word in ("engine_off", "off", "0", None, "", "gibberish"):
        assert m._poll_flags(_status(speed="0", engine=word))[1] is False, word


def test_the_fast_interval_holds_through_a_red_light():
    """The regression in one assertion: stopped, engine running, and the
    interval must still be the profile's fast value rather than base."""
    m = _coordinator_module()
    const = load("const")
    # Pinned: quiet hours are wall-clock, so this would fail 00:00-05:59.
    with _at_hour(m, 12):
        prof = const.POLL_PROFILES["normal"]
        stopped = _status(speed="0", engine="engine_running")
        assert m._adaptive_interval(stopped, 0, prof).total_seconds() == prof["fast"]
        assert m._adaptive_interval(stopped, 3, prof).total_seconds() == prof["fast"]


def test_a_stuck_running_flag_eventually_backs_off_anyway():
    """Every poll signs the owner's phone app out, so a car that claims to be
    running forever with nothing changing must not hold the fastest interval
    for ever either."""
    m = _coordinator_module()
    const = load("const")
    # Pinned: quiet hours are wall-clock, so this would fail 00:00-05:59.
    with _at_hour(m, 12):
        prof = const.POLL_PROFILES["normal"]
        stuck = _status(speed="0", engine="running")
        fast = m._adaptive_interval(stuck, m._STUCK_POLLS - 1, prof).total_seconds()
        slow = m._adaptive_interval(stuck, m._STUCK_POLLS, prof).total_seconds()
        assert fast == prof["fast"]
        assert slow > prof["fast"]


def test_movement_alone_changes_the_signature():
    """Two polls at the same red light look identical, but any real movement
    between polls moves the odometer - which resets the idle streak even on a
    trim that never reports an engine state."""
    m = _coordinator_module()
    a = _status(speed="0", odo="4646.0")
    b = _status(speed="0", odo="4647.0")
    assert m._poll_signature(a) != m._poll_signature(b)


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


def test_the_ladder_is_sane_at_every_hour_of_the_day():
    """Seven tests in this file had to pin the clock because a parked car is
    handled differently between 00:00 and 05:59, and each one that forgot failed
    for six hours a day - on CI included. Rather than leave that to memory, this
    walks all twenty-four hours and pins what must hold in every one of them:
    the interval is always inside the profile, a moving car is never slowed, and
    the only thing the hour is allowed to change is how fast a parked car
    reaches a cap it was going to reach anyway."""
    m = _coordinator_module()
    const = load("const")
    p = const.POLL_PROFILES["normal"]
    parked = _status(speed="0", charger="0")
    quiet = set()
    for hour in range(24):
        with _at_hour(m, hour):
            for streak in (0, 3, 9, 99):
                secs = m._adaptive_interval(parked, streak, p).total_seconds()
                assert p["fast"] <= secs <= p["cap"], (hour, streak, secs)
            # Whatever the hour, a car that is moving or charging polls fast.
            for busy in (_status(speed="60"), _status(charger="3")):
                assert m._adaptive_interval(busy, 0, p).total_seconds() == p["fast"], hour
            if m._adaptive_interval(parked, 0, p).total_seconds() == p["cap"]:
                quiet.add(hour)
    assert quiet == set(m._QUIET_HOURS), (
        f"the quiet window moved: {sorted(quiet)} vs {list(m._QUIET_HOURS)}")


def test_back_off_stops_growing_so_the_interval_cannot_run_away():
    m = _coordinator_module()
    const = load("const")
    # Pinned: quiet hours are wall-clock, so this would fail 00:00-05:59.
    with _at_hour(m, 12):
        p = const.POLL_PROFILES["eco"]
        parked = _status(speed="0")
        assert (m._adaptive_interval(parked, 100, p)
                == m._adaptive_interval(parked, 1000, p))


def test_eco_is_always_gentler_than_live():
    m = _coordinator_module()
    const = load("const")
    # Pinned: quiet hours are wall-clock, so this would fail 00:00-05:59.
    with _at_hour(m, 12):
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


def test_a_frozen_backend_during_a_drive_never_reaches_the_parked_cap():
    """The trap my own stuck-flag guard set (#21): a frozen backend snapshot
    produces an identical signature every poll, so the streak climbs on a car
    that really is moving. Once the guard withdrew the fast interval, the
    parked ladder took over and the next poll was fifteen minutes away - the
    exact symptom the issue reported. A claimed trip now holds at base."""
    m = _coordinator_module()
    const = load("const")
    # Pinned: quiet hours are wall-clock, so this would fail 00:00-05:59.
    with _at_hour(m, 12):
        prof = const.POLL_PROFILES["normal"]
        driving = _status(speed="0", engine="engine_running")
        for streak in (m._STUCK_POLLS, m._STUCK_POLLS + 5, 99):
            secs = m._adaptive_interval(driving, streak, prof).total_seconds()
            assert secs == prof["base"], (streak, secs)
            assert secs < prof["cap"], "a moving car must never sit at the parked cap"


def test_a_parked_car_still_walks_all_the_way_to_the_cap():
    """The back-off that exists to spare the owner's phone-app session must be
    untouched by the fix above."""
    m = _coordinator_module()
    const = load("const")
    # Pinned: quiet hours are wall-clock, so this would fail 00:00-05:59.
    with _at_hour(m, 12):
        prof = const.POLL_PROFILES["normal"]
        parked = _status(speed="0", engine="engine_off")
        assert m._adaptive_interval(parked, 0, prof).total_seconds() == prof["base"]
        assert m._adaptive_interval(parked, 9, prof).total_seconds() == prof["cap"]


# --------------------------------------------- what the signature must see ---
# An audit deleted the DC field from _poll_signature and the whole suite stayed
# green, because the existing signature test only iterated charge/charger/
# locked/speed. These pin the two halves that matter, in both directions.

def test_the_contactor_is_part_of_the_signature():
    """A DC session moves the contactor and little else, so a fast charge would
    otherwise look like an idle car and slow its own polling down (#10)."""
    m = _coordinator_module()
    a = _status(charge="50")
    b = _status(charge="50")
    b["vehicleStatus"]["additionalVehicleStatus"][
        "electricVehicleStatus"]["dcDcConnectStatus"] = "3"
    assert m._poll_signature(a) != m._poll_signature(b)


def test_the_pack_current_is_deliberately_not_in_the_signature():
    """The #10 log records dcChargeIAct wandering while DISCONNECTED - 1.6 A
    drifting, with a 412 A single-sample spike. A field that changes on a
    parked car resets the idle streak every poll, which pins the interval at
    base and stops the back-off ever reaching the cap - and that back-off is
    what spares the owner's phone-app session."""
    m = _coordinator_module()
    a = _status(charge="50")
    b = _status(charge="50")
    for payload, amps in ((a, "0.4"), (b, "0.5")):
        payload["vehicleStatus"]["additionalVehicleStatus"][
            "electricVehicleStatus"]["dcChargeIAct"] = amps
    assert m._poll_signature(a) == m._poll_signature(b), (
        "pack-current noise must not reset the idle streak"
    )


def test_a_parked_car_with_wandering_pack_current_still_reaches_the_cap():
    """The end-to-end version of the above, walked through the interval."""
    m = _coordinator_module()
    const = load("const")
    # Pinned: quiet hours are wall-clock, so this would fail 00:00-05:59.
    with _at_hour(m, 12):
        prof = const.POLL_PROFILES["normal"]
        sig, idle = None, 0
        for amps in ("1.6", "1.7", "412.2", "2.0", "1.9", "1.6"):
            payload = _status(charge="50", speed="0", engine="engine_off")
            payload["vehicleStatus"]["additionalVehicleStatus"][
                "electricVehicleStatus"]["dcChargeIAct"] = amps
            new = m._poll_signature(payload)
            idle = idle + 1 if new == sig else 0
            sig = new
        assert idle >= 4, f"the streak never grew on a parked car (idle={idle})"
        assert m._adaptive_interval(payload, idle, prof).total_seconds() == prof["cap"]


def test_driving_comes_from_the_composite_not_the_raw_speed_alone():
    """An audit reverted _poll_flags to the raw speed field and the suite
    stayed green. A running car at a standstill must still count (#21)."""
    m = _coordinator_module()
    body = io.open(os.path.join(PKG, "__init__.py"), encoding="utf-8").read()
    assert "_ENGINE_RUNNING" in body
    assert m._poll_flags(_status(speed="0", engine="engine_running"))[1] is True
    assert m._poll_flags(_status(speed="0", engine="engine_off"))[1] is False


def test_manual_mode_really_does_fetch_everything_every_sync():
    """`cyc % 1 == 1` is never true, so the gate that was supposed to mean
    "every sync" in Manual mode was permanently false - no vehicle-state
    block, no position, ever. Pin the arithmetic rather than the constants:
    the old test only asserted the divisors equalled 1."""
    const = load("const")
    for name, prof in const.POLL_PROFILES.items():
        for key in ("secondary_every", "position_every"):
            n = prof[key]
            span = max(13, 2 * n + 1)
            fires = [c for c in range(1, span) if (c - 1) % n == 0]
            assert fires, f"{name}.{key} never fires"
            assert fires[0] == 1, (name, key, fires)
            if n == 1:
                assert fires == list(range(1, span)), (name, key, fires)
            else:
                assert fires[1] == 1 + n, (name, key, fires)


# ----------------------------------------------------------- super eco ---

def test_super_eco_starts_where_eco_ends():
    """The mode's promise in one relation: its BASE equals Eco's CAP, so the
    quietest Eco ever gets is where Super Eco begins, and every step of its
    ladder sits at or above Eco's ceiling."""
    const = load("const")
    se, eco = const.POLL_PROFILES["super_eco"], const.POLL_PROFILES["eco"]
    assert se["base"] == eco["cap"]
    assert se["cap"] > se["base"]
    assert se["fast"] > eco["fast"], "even its fast lane is gentler than Eco's"


def test_super_eco_is_always_gentler_than_eco():
    m = _coordinator_module()
    const = load("const")
    with _at_hour(m, 12):
        parked = _status(speed="0")
        for streak in (0, 1, 3, 8):
            se = m._adaptive_interval(parked, streak, const.POLL_PROFILES["super_eco"])
            eco = m._adaptive_interval(parked, streak, const.POLL_PROFILES["eco"])
            assert se > eco, streak


def test_super_eco_wakes_the_car_rarely_but_not_never():
    """position_every drives the PAI wake, which reaches the car itself, and a
    mode sold as frugal must be frugal with the car's battery too: at the cap
    this is one wake every two days. It stays finite - the mode still updates
    on its own, which is what separates it from Manual."""
    const = load("const")
    p = const.POLL_PROFILES["super_eco"]
    assert p["position_every"] * p["cap"] >= 48 * 3600, "wakes more than every 2 days"
    assert not p.get("manual"), "Super Eco must keep a timer"
    # Rare polls should each carry value: the state block rides an already
    # open session, so it is not rationed the way Eco rations it.
    assert p["secondary_every"] <= 2


def test_a_refresh_press_forces_the_position_wake_too():
    """Refresh Data's contract is "everything, now", and in Super Eco a manual
    pull is the advertised way to a fresh GPS fix - the parked cadence is one
    wake in days. The forced flag must reach the position gate, not only the
    secondary one, and must be read ONCE per cycle so one press cannot answer
    two gates differently."""
    import io, os
    from conftest import PKG
    src = io.open(os.path.join(PKG, "__init__.py"), encoding="utf-8").read()
    assert "if was_driving or forced or ((cyc - 1) % _POSITION_EVERY == 0):" in src
    assert src.count('poll_state.get("force_secondary", False)') == 1, (
        "forced is read twice; the two gates can disagree about one press")


def test_a_disowned_speed_does_not_read_as_driving():
    """#51 taught the SENSOR to publish unknown when speedValidity is false.
    This is the half with teeth: _poll_flags reads the raw field, so a stale
    non-zero speed would hold the fast interval on a parked car and - through
    the card's driving lock, which follows the same composite - grey out every
    button behind a "Driving" banner."""
    m = _coordinator_module()
    stale = _status(speed="50")
    stale["vehicleStatus"]["basicVehicleStatus"]["speedValidity"] = "false"
    assert m._poll_flags(stale)[1] is False, "a disowned speed read as motion"


def test_a_valid_speed_still_reads_as_driving():
    m = _coordinator_module()
    live = _status(speed="50")
    live["vehicleStatus"]["basicVehicleStatus"]["speedValidity"] = "true"
    assert m._poll_flags(live)[1] is True
    # And a trim that never reports the flag keeps the old behaviour.
    assert m._poll_flags(_status(speed="50"))[1] is True


def test_a_disowned_speed_does_not_silence_a_running_car():
    """The guard removes one input, not the composite: a car whose ignition is
    on is still driving even when its speed field is disowned, which is what
    stops this from re-opening the #21 hole from the other side."""
    m = _coordinator_module()
    d = _status(speed="50", engine="engine_running")
    d["vehicleStatus"]["basicVehicleStatus"]["speedValidity"] = "false"
    assert m._poll_flags(d)[1] is True


def test_both_speed_readers_share_one_rule():
    """Two callers act on speedValidity - the sensor and the poller - and a
    rule copied into both is a rule that drifts. There is one predicate."""
    import io, os
    from conftest import PKG
    for name in ("sensor.py", "__init__.py"):
        src = io.open(os.path.join(PKG, name), encoding="utf-8").read()
        assert "speed_is_stale" in src, name
        assert '"speedValidity"' not in src, f"{name} re-implements the rule"
