"""Command paths of the switch, select and time platforms.

test_entities.py proves the platforms BUILD; this file proves the built
entities FIRE correctly: the exact serviceId / params each command sends,
the state each entity derives from its documented status path, the
optimistic windows that bridge the slow server propagation, and the
GeelyControlError -> HomeAssistantError wrapping the user actually sees.

Everything runs against a recorder fake for the API - no network, and no
sleeps: `schedule_refresh` is replaced with a recorder and the platforms'
`time` modules with a pinned clock, both restored in try/finally.
"""
import asyncio
import logging
from contextlib import contextmanager
from datetime import time as dtime

from conftest import FAKE_VIN, have_homeassistant, load
from run import skip

# Any fixed epoch works - only deltas matter. Deliberately in the past, so
# an optimistic window computed against the pinned clock reads as already
# expired if an assert accidentally runs against the real clock.
_PIN = 1_700_000_000.0


# ---------------------------------------------------------------- harness ---

class _Clock:
    """Stands in for the `time` module inside a platform (only .time())."""

    def __init__(self, now: float = _PIN):
        self.now = now

    def time(self):
        return self.now


class _RefreshRec:
    """Records schedule_refresh(hass, coordinator, *delays) calls."""

    def __init__(self):
        self.calls = []

    def __call__(self, hass, coordinator, *delays, **kw):
        self.calls.append(delays)


class _Api:
    """Recorder fake for bundle["api"]: logs every call, optionally raises."""

    def __init__(self, raise_=None):
        self.calls = []
        self.raise_ = raise_

    def control(self, service_id, parameters=None, command="start", duration=0):
        self.calls.append(("control", service_id, parameters, command, duration))
        if self.raise_ is not None:
            raise self.raise_
        return {"code": "1000"}

    def scheduled_charging_set(self, *, command, start_time, end_time,
                               rbc_target="2", rbc="2", charge_model="0"):
        self.calls.append(("scheduled_charging_set", {
            "command": command, "start_time": start_time,
            "end_time": end_time, "rbc_target": rbc_target,
            "charge_model": charge_model}))
        if self.raise_ is not None:
            raise self.raise_
        return {"code": "1000"}


class _Coord:
    last_update_success = True

    def __init__(self, data):
        self.data = data

    def async_add_listener(self, cb, *a, **k):
        return lambda: None


class _Entry:
    entry_id = "e1"
    data = {"vin": FAKE_VIN, "pressure_unit": "psi"}
    options: dict = {}

    def async_on_unload(self, fn):
        return fn


class _Hass:
    def __init__(self):
        self.data = {}

    async def async_add_executor_job(self, fn, *args):
        return fn(*args)

    def async_create_task(self, coro, *a, **k):
        coro.close()
        return None


def _status(climate=None, ev=None, state=None, sched=None):
    return {
        "vehicleStatus": {
            "basicVehicleStatus": {},
            "additionalVehicleStatus": {
                "climateStatus": climate if climate is not None else {},
                "electricVehicleStatus": ev if ev is not None else {},
            },
        },
        "_state": state if state is not None else {},
        "_scheduled_charging": sched if sched is not None else {},
    }


def _bundle(data, api=None, caps=None):
    hass = _Hass()
    b = {"api": api if api is not None else _Api(),
         "coordinator": _Coord(data), "vin": FAKE_VIN,
         "device_name": "Geely EX5 (0000)", "capabilities": caps or {}}
    hass.data["geely_connect"] = {"e1": b}
    return hass, b


@contextmanager
def _patched(mod, **attrs):
    """Swap module attributes for the duration of a with-block."""
    old = {k: getattr(mod, k) for k in attrs}
    for k, v in attrs.items():
        setattr(mod, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            setattr(mod, k, v)


def _quiet(ent):
    """Entities call async_write_ha_state after a fire; a bare stub has no
    hass to write to, so silence it on the instance."""
    ent.async_write_ha_state = lambda: None
    return ent


def _expect_error(coro):
    """Run a command; it must raise HomeAssistantError. Returns the error."""
    from homeassistant.exceptions import HomeAssistantError
    logging.disable(logging.CRITICAL)   # the failure paths log tracebacks
    try:
        asyncio.run(coro)
    except HomeAssistantError as e:
        assert FAKE_VIN not in str(e), "VIN leaked into user-facing error text"
        return e
    finally:
        logging.disable(logging.NOTSET)
    raise AssertionError("HomeAssistantError not raised")


def _switch_def(sw, key):
    return next(d for d in sw.SWITCH_DEFS if d[0] == key)


def _seat(sel, hass, bundle, key):
    defn = next(d for d in sel.SEAT_DEFS if d[0] == key)
    return _quiet(sel.GeelySeatLevel(hass, bundle, *defn))


# ------------------------------------------------------- switch: GeelySwitch ---

def test_charging_switch_sends_the_documented_rcs_params():
    if not have_homeassistant():
        skip("homeassistant not installed")
    sw = load("switch")
    api = _Api()
    hass, b = _bundle(_status(), api=api)
    ent = sw.GeelySwitch(hass, b, *_switch_def(sw, "charging")[:-1])
    rec = _RefreshRec()
    with _patched(sw, schedule_refresh=rec):
        asyncio.run(ent.async_turn_on())
        asyncio.run(ent.async_turn_off())
    assert api.calls == [
        ("control", "RCS", [{"key": "operation", "value": "1"},
                            {"key": "rcs.restart", "value": "1"}], "start", 0),
        ("control", "RCS", [{"key": "operation", "value": "0"},
                            {"key": "rcs.terminate", "value": "1"}], "stop", 0),
    ], api.calls
    assert rec.calls == [(8,), (8,)], rec.calls


def test_parking_comfort_switch_fires_rsm_with_no_params():
    if not have_homeassistant():
        skip("homeassistant not installed")
    sw = load("switch")
    api = _Api()
    hass, b = _bundle(_status(), api=api)
    ent = sw.GeelySwitch(hass, b, *_switch_def(sw, "parking_comfort")[:-1])
    with _patched(sw, schedule_refresh=_RefreshRec()):
        asyncio.run(ent.async_turn_on())
        asyncio.run(ent.async_turn_off())
    assert api.calls == [("control", "RSM", [], "start", 0),
                         ("control", "RSM", [], "stop", 0)], api.calls


def test_generic_switch_state_comes_from_its_declared_path():
    if not have_homeassistant():
        skip("homeassistant not installed")
    sw = load("switch")
    data = _status(ev={"statusOfChargerConnection": "3"})
    hass, b = _bundle(data)
    charging = sw.GeelySwitch(hass, b, *_switch_def(sw, "charging")[:-1])
    assert charging.is_on is True
    data["vehicleStatus"]["additionalVehicleStatus"][
        "electricVehicleStatus"]["statusOfChargerConnection"] = "0"
    assert charging.is_on is False
    # The path being absent no longer means unknown for THIS switch: the
    # charging composite does not need statusOfChargerConnection, so a trim
    # that omits it still gets a real answer (False here - no contactor, no
    # current). The generic missing-value rule is pinned on another switch
    # below, where it still applies.
    hass2, b2 = _bundle(_status())          # path absent entirely
    assert sw.GeelySwitch(hass2, b2, *_switch_def(sw, "charging")[:-1]).is_on is False
    parking = sw.GeelySwitch(hass2, b2, *_switch_def(sw, "parking_comfort")[:-1])
    assert parking.is_on is None, "a switch with no composite still reads unknown"


def test_parking_comfort_reports_unknown_rather_than_trusting_parkcomfortstate():
    """parkComfortState is not the on/off state: a car with parking comfort
    off still reports 1, so reading it pinned the switch to `on` forever. No
    replacement field is known, so the switch must stay `unknown` even when
    parkComfortState is present and set."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    sw = load("switch")
    hass, b = _bundle(_status(state={"parkComfortState": "1"}))
    ent = sw.GeelySwitch(hass, b, *_switch_def(sw, "parking_comfort")[:-1])
    assert ent.is_on is None


def test_a_trim_that_does_report_parkcomfortactive_gets_a_real_state():
    """The switch now watches the *Active family. No known car sends it yet,
    but if one ever does, the state should follow it rather than stay
    unknown - that is the whole point of pointing at the right field."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    sw = load("switch")
    hass, b = _bundle(_status(state={"parkComfortActive": "1"}))
    ent = sw.GeelySwitch(hass, b, *_switch_def(sw, "parking_comfort")[:-1])
    assert ent.is_on is True
    hass2, b2 = _bundle(_status(state={"parkComfortActive": "0"}))
    ent2 = sw.GeelySwitch(hass2, b2, *_switch_def(sw, "parking_comfort")[:-1])
    assert ent2.is_on is False


def test_a_rejected_generic_switch_command_raises_homeassistanterror():
    if not have_homeassistant():
        skip("homeassistant not installed")
    sw = load("switch")
    gce = load("api").GeelyControlError
    rec = _RefreshRec()

    boom = gce("8070", "The last request has not yet been executed")
    api = _Api(raise_=boom)
    hass, b = _bundle(_status(), api=api)
    ent = sw.GeelySwitch(hass, b, *_switch_def(sw, "charging")[:-1])
    with _patched(sw, schedule_refresh=rec):
        e = _expect_error(ent.async_turn_on())
    assert "Geely RCS: The last request has not yet been executed" in str(e), e
    assert e.__cause__ is boom
    assert rec.calls == [], "a failed command must not schedule a refresh"

    api2 = _Api(raise_=RuntimeError("socket closed"))
    hass2, b2 = _bundle(_status(), api=api2)
    ent2 = sw.GeelySwitch(hass2, b2, *_switch_def(sw, "charging")[:-1])
    with _patched(sw, schedule_refresh=rec):
        e2 = _expect_error(ent2.async_turn_off())
    assert "Geely RCS failure: socket closed" in str(e2), e2
    assert rec.calls == []


def test_capability_flags_skip_exactly_the_flagged_switches():
    if not have_homeassistant():
        skip("homeassistant not installed")
    sw = load("switch")
    # Flag off the two table-driven switches: the four dedicated classes stay.
    hass, b = _bundle(_status(), caps={"parking_comfort.enabled": False,
                                       "charging.enabled": False})
    got = []
    asyncio.run(sw.async_setup_entry(hass, _Entry(), lambda e, *a, **k: got.extend(e)))
    assert sorted(type(e).__name__ for e in got) == [
        "GeelyDefrostSwitch", "GeelyGCleanSwitch",
        "GeelyScheduledChargingSwitch", "GeelyWindowVentilationSwitch"], got
    # Flag off everything else: only the table-driven parking switch is left
    # (charging.enabled=False kills both the RCS switch and - together with
    # scheduled_charging.enabled=False - the scheduled-charging switch).
    hass, b = _bundle(_status(), caps={
        "windows.enabled": False, "gclean.enabled": False,
        "defrost.enabled": False, "scheduled_charging.enabled": False,
        "charging.enabled": False})
    got = []
    asyncio.run(sw.async_setup_entry(hass, _Entry(), lambda e, *a, **k: got.extend(e)))
    assert [type(e).__name__ for e in got] == ["GeelySwitch"], got
    assert got[0]._attr_unique_id == f"geely_{FAKE_VIN}_sw_parking_comfort"


# ------------------------------------------------ switch: dedicated classes ---

def test_window_ventilation_commands_and_state():
    if not have_homeassistant():
        skip("homeassistant not installed")
    sw = load("switch")
    api = _Api()
    climate = {"winStatusDriver": "2", "winStatusPassenger": "2",
               "winStatusDriverRear": "2", "winStatusPassengerRear": "2"}
    data = _status(climate=climate)
    hass, b = _bundle(data, api=api)
    ent = sw.GeelyWindowVentilationSwitch(hass, b)
    rec = _RefreshRec()
    with _patched(sw, schedule_refresh=rec):
        asyncio.run(ent.async_turn_on())
        asyncio.run(ent.async_turn_off())
    assert api.calls == [
        ("control", "RWS_2", [{"key": "target", "value": "ventilate"}], "start", 0),
        ("control", "RWS_2", [{"key": "target", "value": "window"}], "stop", 0),
    ], api.calls
    assert rec.calls == [(8,), (8,)]
    assert ent.is_on is False                 # all four corners closed
    climate["winStatusDriver"] = "1"
    assert ent.is_on is True                  # any corner off its stop
    hass2, b2 = _bundle(_status())
    assert sw.GeelyWindowVentilationSwitch(hass2, b2).is_on is None


def test_gclean_commands_carry_cabin_and_six_seconds():
    if not have_homeassistant():
        skip("homeassistant not installed")
    sw = load("switch")
    api = _Api()
    hass, b = _bundle(_status(), api=api)
    ent = sw.GeelyGCleanSwitch(hass, b)
    rec = _RefreshRec()
    with _patched(sw, schedule_refresh=rec):
        asyncio.run(ent.async_turn_on())
        asyncio.run(ent.async_turn_off())
    assert api.calls == [
        ("control", "RCC_2", [{"key": "rcc.ventilation", "value": "cabin"}], "start", 6),
        ("control", "RCC_2", [{"key": "rcc.ventilation", "value": "cabin"}], "stop", 6),
    ], api.calls
    assert rec.calls == [(8,), (8,)]


def test_gclean_state_and_the_ac_defrost_mutex():
    if not have_homeassistant():
        skip("homeassistant not installed")
    sw = load("switch")
    climate = {}
    data = _status(climate=climate)
    hass, b = _bundle(data)
    ent = sw.GeelyGCleanSwitch(hass, b)
    assert ent.is_on is None                  # no airBlowerActive reported
    climate["airBlowerActive"] = "true"
    assert ent.is_on is True
    climate["airBlowerActive"] = "false"
    assert ent.is_on is False
    assert ent.available is True
    climate["preClimateActive"] = "true"      # AC on -> car rejects G-clean
    assert ent.available is False
    climate["preClimateActive"] = "false"
    climate["defrost"] = "true"               # defrost on -> same mutex
    assert ent.available is False
    climate["defrost"] = "false"
    assert ent.available is True
    b["coordinator"].last_update_success = False
    try:
        assert ent.available is False         # coordinator failure wins
    finally:
        b["coordinator"].last_update_success = True


def test_defrost_commands_carry_conditioner_2_level_2():
    if not have_homeassistant():
        skip("homeassistant not installed")
    sw = load("switch")
    api = _Api()
    hass, b = _bundle(_status(), api=api)
    ent = sw.GeelyDefrostSwitch(hass, b)
    rec = _RefreshRec()
    with _patched(sw, schedule_refresh=rec):
        asyncio.run(ent.async_turn_on())
        asyncio.run(ent.async_turn_off())
    params = [{"key": "rce.conditioner", "value": "2"},
              {"key": "rce.level", "value": "2"}]
    assert api.calls == [("control", "RCE_2", params, "start", 90),
                        ("control", "RCE_2", params, "stop", 0)], api.calls
    assert rec.calls == [(8,), (8,)]


def test_defrost_state_reads_climate_defrost():
    if not have_homeassistant():
        skip("homeassistant not installed")
    sw = load("switch")
    climate = {}
    hass, b = _bundle(_status(climate=climate))
    ent = sw.GeelyDefrostSwitch(hass, b)
    assert ent.is_on is None
    climate["defrost"] = "true"
    assert ent.is_on is True
    climate["defrost"] = "false"
    assert ent.is_on is False


# ------------------------------------------- switch: steering wheel heat ---
# The command is a capture of the official app's own button (#4, 2026-08-10),
# not a guess: rce.heat carries "steering_wheel" - an underscore, where every
# seat name on the same key is hyphenated - with no rce.level, and the app's
# start uses scheduling duration 48. These tests pin that capture.

def test_steering_wheel_heat_sends_the_captured_command():
    if not have_homeassistant():
        skip("homeassistant not installed")
    sw = load("switch")
    api = _Api()
    hass, b = _bundle(_status(climate={"steerWhlHeatingSts": "2"}), api=api)
    ent = _quiet(sw.GeelySteeringWheelHeatSwitch(hass, b))
    rec = _RefreshRec()
    with _patched(sw, schedule_refresh=rec):
        asyncio.run(ent.async_turn_on())
        asyncio.run(ent.async_turn_off())
    params = [{"key": "rce.heat", "value": "steering_wheel"}]
    assert api.calls == [("control", "RCE_2", params, "start", 48),
                         ("control", "RCE_2", params, "stop", 0)], api.calls
    # The field lags, so the switch polls the car twice after each command.
    assert rec.calls == [(8, 30), (8, 30)]


def test_steering_wheel_heat_shows_its_requested_state_optimistically():
    """The owner reported the toggle "very slow to respond" (#4): the field lags,
    so a press must show its requested state at once and only defer to the field
    once the optimistic window has passed."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    sw = load("switch")
    climate = {"steerWhlHeatingSts": "2"}   # car says off
    hass, b = _bundle(_status(climate=climate), api=_Api())
    ent = _quiet(sw.GeelySteeringWheelHeatSwitch(hass, b))
    with _patched(sw, schedule_refresh=_RefreshRec()):
        asyncio.run(ent.async_turn_on())
    assert ent.is_on is True, "must show on at once, before the field catches up"
    ent._optimistic_until = 0.0             # window elapsed
    assert ent.is_on is False, "then it defers to the real field again"


def test_steering_wheel_heat_state_reads_the_measured_convention():
    """1 = heating at any level, 2 = off, 0 = not fitted (#4). 0 and absence
    must both read unknown - reporting 0 as "off" is what v1.27.0 got wrong."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    sw = load("switch")
    climate = {}
    hass, b = _bundle(_status(climate=climate))
    ent = sw.GeelySteeringWheelHeatSwitch(hass, b)
    assert ent.is_on is None                      # field absent
    for raw, expect in (("1", True), (1, True), ("2", False), (2, False),
                        ("0", None), (0, None), ("banana", None)):
        climate["steerWhlHeatingSts"] = raw
        assert ent.is_on is expect, raw


def test_steering_wheel_heat_switch_exists_only_on_evidence():
    """Not default-permissive like the other gates: the switch appears when
    the catalogue advertises the wheel OR the status field uses the fitted
    1/2 convention - never on a car reading 0, and never on no data."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    sw = load("switch")

    def built(data, caps):
        hass, b = _bundle(data, caps=caps)
        got = []
        asyncio.run(sw.async_setup_entry(hass, _Entry(),
                                         lambda e, *a, **k: got.extend(e)))
        return [type(e).__name__ for e in got]

    assert "GeelySteeringWheelHeatSwitch" in built(
        _status(), {"steering_wheel_heat.enabled": True})
    assert "GeelySteeringWheelHeatSwitch" in built(
        _status(climate={"steerWhlHeatingSts": "2"}), {})
    assert "GeelySteeringWheelHeatSwitch" not in built(
        _status(climate={"steerWhlHeatingSts": "0"}), {})
    assert "GeelySteeringWheelHeatSwitch" not in built(_status(), {})


def test_a_rejected_steering_wheel_command_raises_homeassistanterror():
    if not have_homeassistant():
        skip("homeassistant not installed")
    sw = load("switch")
    gce = load("api").GeelyControlError
    rec = _RefreshRec()

    boom = gce("8070", "The last request has not yet been executed")
    hass, b = _bundle(_status(), api=_Api(raise_=boom))
    ent = sw.GeelySteeringWheelHeatSwitch(hass, b)
    with _patched(sw, schedule_refresh=rec):
        e = _expect_error(ent.async_turn_on())
    assert "Geely Steering Wheel Heat: The last request" in str(e), e
    assert e.__cause__ is boom
    assert rec.calls == [], "a failed command must not schedule a refresh"

    hass2, b2 = _bundle(_status(), api=_Api(raise_=RuntimeError("socket closed")))
    ent2 = sw.GeelySteeringWheelHeatSwitch(hass2, b2)
    with _patched(sw, schedule_refresh=rec):
        e2 = _expect_error(ent2.async_turn_off())
    assert "Geely Steering Wheel Heat failure: socket closed" in str(e2), e2
    assert rec.calls == []


# ------------------------------------------- switch: scheduled charging ---

def test_scheduled_charging_switch_writes_the_full_body():
    if not have_homeassistant():
        skip("homeassistant not installed")
    sw = load("switch")
    api = _Api()
    sched = {"rbcStartTime": "22:30", "rbcEndTime": "06:15",
             "rbcTarget": "1", "rbcModel": "5", "bcCycleActive": "false"}
    data = _status(sched=sched)
    hass, b = _bundle(data, api=api)
    ent = _quiet(sw.GeelyScheduledChargingSwitch(hass, b))
    rec, clock = _RefreshRec(), _Clock()
    with _patched(sw, schedule_refresh=rec, time=clock):
        asyncio.run(ent.async_turn_on())
    # The whole current schedule rides along - only `command` flips.
    assert api.calls == [("scheduled_charging_set", {
        "command": "start", "start_time": "22:30", "end_time": "06:15",
        "rbc_target": "1", "charge_model": "5"})], api.calls
    # Peer entities read the flip from the patched coordinator data.
    assert sched["bcCycleActive"] == "true"
    assert ent._optimistic_on is True
    assert ent._optimistic_until == clock.now + 60
    assert rec.calls == [(15, 20, 20)], rec.calls


def test_scheduled_charging_switch_defaults_an_empty_schedule():
    if not have_homeassistant():
        skip("homeassistant not installed")
    sw = load("switch")
    api = _Api()
    data = _status(sched={})
    hass, b = _bundle(data, api=api)
    ent = _quiet(sw.GeelyScheduledChargingSwitch(hass, b))
    with _patched(sw, schedule_refresh=_RefreshRec(), time=_Clock()):
        asyncio.run(ent.async_turn_off())
    assert api.calls == [("scheduled_charging_set", {
        "command": "stop", "start_time": "23:00", "end_time": "07:00",
        "rbc_target": "2", "charge_model": "0"})], api.calls
    assert data["_scheduled_charging"]["bcCycleActive"] == "false"


def test_scheduled_charging_is_on_and_its_60s_optimistic_hold():
    if not have_homeassistant():
        skip("homeassistant not installed")
    sw = load("switch")
    sched = {"rbcStartTime": "23:00", "rbcEndTime": "07:00",
             "bcCycleActive": "true"}
    data = _status(sched=sched)
    hass, b = _bundle(data, api=_Api())
    ent = _quiet(sw.GeelyScheduledChargingSwitch(hass, b))
    rec, clock = _RefreshRec(), _Clock()
    with _patched(sw, schedule_refresh=rec, time=clock):
        assert ent.is_on is True              # from bcCycleActive
        asyncio.run(ent.async_turn_off())
        assert ent.is_on is False             # optimistic, immediately
        # The server keeps reporting the STALE state for ~30s - the
        # override must not be defeated by it.
        sched["bcCycleActive"] = "true"
        assert ent.is_on is False
        clock.now += 61                       # hold expires after 60s
        assert ent.is_on is True              # back to reading the server
    # State derivations without any optimistic override:
    hass2, b2 = _bundle(_status(sched={}))
    assert sw.GeelyScheduledChargingSwitch(hass2, b2).is_on is None
    hass3, b3 = _bundle(_status(sched={"rbcStartTime": "23:00"}))
    # Schedule data present but bcCycleActive absent = schedule OFF, not unknown.
    assert sw.GeelyScheduledChargingSwitch(hass3, b3).is_on is False


def test_every_dedicated_switch_wraps_rejections_in_homeassistanterror():
    if not have_homeassistant():
        skip("homeassistant not installed")
    sw = load("switch")
    gce = load("api").GeelyControlError
    cases = [
        (sw.GeelyWindowVentilationSwitch, "Geely Window Ventilation"),
        (sw.GeelyGCleanSwitch, "Geely G-Clean"),
        (sw.GeelyDefrostSwitch, "Geely Defrost"),
        (sw.GeelyScheduledChargingSwitch, "Geely Scheduled Charging"),
    ]
    rec = _RefreshRec()
    for cls, label in cases:
        # A server rejection carries the server's message.
        api = _Api(raise_=gce("failure", "Operation failed"))
        sched = {"rbcStartTime": "23:00"}
        hass, b = _bundle(_status(sched=sched), api=api)
        ent = _quiet(cls(hass, b))
        with _patched(sw, schedule_refresh=rec, time=_Clock()):
            e = _expect_error(ent.async_turn_on())
        assert f"{label}: Operation failed" in str(e), (label, e)
        # An unexpected exception is wrapped, not swallowed.
        api2 = _Api(raise_=RuntimeError("boom"))
        hass2, b2 = _bundle(_status(sched={"rbcStartTime": "23:00"}), api=api2)
        ent2 = _quiet(cls(hass2, b2))
        with _patched(sw, schedule_refresh=rec, time=_Clock()):
            e2 = _expect_error(ent2.async_turn_off())
        assert f"{label} failure: boom" in str(e2), (label, e2)
    assert rec.calls == [], "a failed command must not schedule a refresh"
    # The scheduled-charging failure must leave no optimistic residue and
    # must not patch the coordinator's schedule.
    assert ent._optimistic_on is None and ent2._optimistic_on is None
    assert "bcCycleActive" not in sched


# ------------------------------------------------------------------ select ---

def test_seat_selects_send_level_and_seat_params():
    if not have_homeassistant():
        skip("homeassistant not installed")
    sel = load("select")
    api = _Api()
    hass, b = _bundle(_status(), api=api)
    heat = _seat(sel, hass, b, "seat_heat_driver")
    vent = _seat(sel, hass, b, "seat_vent_passenger")
    rec, clock = _RefreshRec(), _Clock()
    with _patched(sel, schedule_refresh=rec, time=clock):
        asyncio.run(heat.async_select_option("High"))
        asyncio.run(heat.async_select_option("Off"))
        asyncio.run(vent.async_select_option("Low"))
    assert api.calls == [
        ("control", "RCE_2", [{"key": "rce.level", "value": "3"},
                              {"key": "rce.heat", "value": "front-left"}], "start", 90),
        ("control", "RCE_2", [{"key": "rce.level", "value": "0"},
                              {"key": "rce.heat", "value": "front-left"}], "stop", 90),
        ("control", "RCE_2", [{"key": "rce.level", "value": "1"},
                              {"key": "rce.ventilation", "value": "front-right"}], "start", 90),
    ], api.calls
    assert rec.calls == [(8, 17)] * 3, rec.calls


def test_an_unknown_seat_level_is_ignored_not_sent():
    if not have_homeassistant():
        skip("homeassistant not installed")
    sel = load("select")
    api = _Api()
    hass, b = _bundle(_status(), api=api)
    ent = _seat(sel, hass, b, "seat_heat_driver")
    rec = _RefreshRec()
    logging.disable(logging.CRITICAL)         # it warns, deliberately
    try:
        with _patched(sel, schedule_refresh=rec):
            asyncio.run(ent.async_select_option("Turbo"))
    finally:
        logging.disable(logging.NOTSET)
    assert api.calls == [] and rec.calls == [], (api.calls, rec.calls)


def test_seat_state_decoding_heat_and_vent_disagree_on_purpose():
    if not have_homeassistant():
        skip("homeassistant not installed")
    sel = load("select")
    climate = {}
    hass, b = _bundle(_status(climate=climate))
    heat = _seat(sel, hass, b, "seat_heat_driver")
    vent = _seat(sel, hass, b, "seat_vent_driver")
    assert heat.current_option == "Off"       # nothing reported
    # HEAT: *HeatSts is the level directly.
    for sts, want in (("3", "High"), ("2", "Medium"), ("1", "Low"), ("0", "Off"),
                      ("x", "Off"), ("7", "Off"), ("-1", "Off")):
        climate["drvHeatSts"] = sts
        assert heat.current_option == want, (sts, heat.current_option)
    # VENT: *VentSts is a 1=on / 2=off flag; the level lives in *VentDetail.
    climate["drvVentSts"], climate["drvVentDetail"] = "2", "3"
    assert vent.current_option == "Off"       # sts says off, detail ignored
    climate["drvVentSts"] = "1"
    assert vent.current_option == "High"
    climate["drvVentDetail"] = "2"
    assert vent.current_option == "Medium"
    del climate["drvVentDetail"]
    assert vent.current_option == "Off"       # on with no level reads Off
    climate["drvVentDetail"] = "x"
    assert vent.current_option == "Off"
    climate["drvVentDetail"] = "9"
    assert vent.current_option == "Off"       # out of range


def test_seat_select_optimism_drops_on_server_match_or_timeout():
    if not have_homeassistant():
        skip("homeassistant not installed")
    sel = load("select")
    api = _Api()
    climate = {"drvHeatSts": "0"}
    hass, b = _bundle(_status(climate=climate), api=api)
    ent = _seat(sel, hass, b, "seat_heat_driver")
    rec, clock = _RefreshRec(), _Clock()
    with _patched(sel, schedule_refresh=rec, time=clock):
        asyncio.run(ent.async_select_option("High"))
        assert ent.current_option == "High"   # server still stale at "0"
        climate["drvHeatSts"] = "3"           # server caught up
        assert ent.current_option == "High"
        assert ent._optimistic_option is None, "override not dropped on match"
        climate["drvHeatSts"] = "0"           # override gone - server rules
        assert ent.current_option == "Off"
        asyncio.run(ent.async_select_option("Low"))
        assert ent.current_option == "Low"
        clock.now += 31                       # 30s window expires
        assert ent.current_option == "Off"


def test_a_rejected_seat_command_sets_no_optimistic_state():
    if not have_homeassistant():
        skip("homeassistant not installed")
    sel = load("select")
    gce = load("api").GeelyControlError
    rec = _RefreshRec()
    api = _Api(raise_=gce("8070", "The last request has not yet been executed"))
    hass, b = _bundle(_status(climate={"drvHeatSts": "0"}), api=api)
    ent = _seat(sel, hass, b, "seat_heat_driver")
    with _patched(sel, schedule_refresh=rec, time=_Clock()):
        e = _expect_error(ent.async_select_option("High"))
        assert "Geely seat heat (front-left): The last request" in str(e), e
        # Fire-first: a rejected command must not make the UI lie.
        assert ent._optimistic_option is None
        assert ent.current_option == "Off"
    api2 = _Api(raise_=RuntimeError("boom"))
    hass2, b2 = _bundle(_status(), api=api2)
    ent2 = _seat(sel, hass2, b2, "seat_vent_driver")
    with _patched(sel, schedule_refresh=rec, time=_Clock()):
        e2 = _expect_error(ent2.async_select_option("Medium"))
    assert "Geely seat vent failure: boom" in str(e2), e2
    assert rec.calls == []


# -------------------------------------------------------------------- time ---

def test_hhmm_parse_and_format():
    if not have_homeassistant():
        skip("homeassistant not installed")
    t = load("time")
    assert t._parse_hhmm("23:05") == dtime(23, 5)
    assert t._parse_hhmm("7:5") == dtime(7, 5)
    assert t._parse_hhmm(None) is None
    assert t._parse_hhmm("") is None
    assert t._parse_hhmm("0705") is None      # no colon
    assert t._parse_hhmm("xx:yy") is None     # not numbers
    assert t._parse_hhmm("25:00") is None     # not a valid hour
    assert t._fmt_hhmm(dtime(7, 5)) == "07:05"
    assert t._fmt_hhmm(dtime(23, 59)) == "23:59"


def test_time_entities_skip_only_when_both_capability_flags_deny():
    if not have_homeassistant():
        skip("homeassistant not installed")
    t = load("time")
    hass, b = _bundle(_status(), caps={"scheduled_charging.enabled": False,
                                       "charging.enabled": False})
    got = []
    asyncio.run(t.async_setup_entry(hass, _Entry(), lambda e, *a, **k: got.extend(e)))
    assert got == [], got
    # One flag still on keeps the pair (the two flags are alternates).
    hass, b = _bundle(_status(), caps={"scheduled_charging.enabled": False})
    got = []
    asyncio.run(t.async_setup_entry(hass, _Entry(), lambda e, *a, **k: got.extend(e)))
    assert sorted(e._attr_unique_id for e in got) == [
        f"geely_{FAKE_VIN}_time_scheduled_charging_end",
        f"geely_{FAKE_VIN}_time_scheduled_charging_start"], got


def test_time_set_writes_the_full_body_and_preserves_the_command():
    if not have_homeassistant():
        skip("homeassistant not installed")
    t = load("time")
    api = _Api()
    sched = {"rbcStartTime": "23:00", "rbcEndTime": "07:00",
             "bcCycleActive": "true", "rbcTarget": "1", "rbcModel": "4"}
    data = _status(sched=sched)
    hass, b = _bundle(data, api=api)
    start = _quiet(t.GeelyScheduledChargingTime(hass, b, "start"))
    rec, clock = _RefreshRec(), _Clock()
    with _patched(t, schedule_refresh=rec, time_mod=clock):
        asyncio.run(start.async_set_value(dtime(21, 45)))
    # Full body: new start, UNCHANGED end/target/model, and command=start
    # because the schedule is currently active - editing the time alone
    # must not toggle it.
    assert api.calls == [("scheduled_charging_set", {
        "command": "start", "start_time": "21:45", "end_time": "07:00",
        "rbc_target": "1", "charge_model": "4"})], api.calls
    # Coordinator data is patched so the switch reads the fresh time.
    assert sched["rbcStartTime"] == "21:45"
    assert rec.calls == [(15, 20, 20)], rec.calls


def test_time_set_on_an_inactive_or_empty_schedule_keeps_it_off():
    if not have_homeassistant():
        skip("homeassistant not installed")
    t = load("time")
    api = _Api()
    sched = {"rbcStartTime": "23:00", "rbcEndTime": "07:00"}  # no bcCycleActive
    hass, b = _bundle(_status(sched=sched), api=api)
    end = _quiet(t.GeelyScheduledChargingTime(hass, b, "end"))
    with _patched(t, schedule_refresh=_RefreshRec(), time_mod=_Clock()):
        asyncio.run(end.async_set_value(dtime(6, 30)))
    assert api.calls == [("scheduled_charging_set", {
        "command": "stop", "start_time": "23:00", "end_time": "06:30",
        "rbc_target": "2", "charge_model": "0"})], api.calls
    assert sched["rbcEndTime"] == "06:30"
    # A car with no schedule at all falls back to the documented defaults.
    api2 = _Api()
    hass2, b2 = _bundle(_status(sched={}), api=api2)
    start2 = _quiet(t.GeelyScheduledChargingTime(hass2, b2, "start"))
    with _patched(t, schedule_refresh=_RefreshRec(), time_mod=_Clock()):
        asyncio.run(start2.async_set_value(dtime(1, 5)))
    assert api2.calls == [("scheduled_charging_set", {
        "command": "stop", "start_time": "01:05", "end_time": "07:00",
        "rbc_target": "2", "charge_model": "0"})], api2.calls


def test_time_value_reads_its_field_and_holds_optimistically():
    if not have_homeassistant():
        skip("homeassistant not installed")
    t = load("time")
    sched = {"rbcStartTime": "23:00", "rbcEndTime": "07:15"}
    hass, b = _bundle(_status(sched=sched), api=_Api())
    start = _quiet(t.GeelyScheduledChargingTime(hass, b, "start"))
    end = _quiet(t.GeelyScheduledChargingTime(hass, b, "end"))
    assert start.native_value == dtime(23, 0)
    assert end.native_value == dtime(7, 15)
    hass2, b2 = _bundle(_status(sched={}))
    assert t.GeelyScheduledChargingTime(hass2, b2, "start").native_value is None
    rec, clock = _RefreshRec(), _Clock()
    with _patched(t, schedule_refresh=rec, time_mod=clock):
        asyncio.run(start.async_set_value(dtime(21, 45)))
        sched["rbcStartTime"] = "23:00"       # a stale poll writes back
        assert start.native_value == dtime(21, 45)   # held for 60s
        clock.now += 61
        assert start.native_value == dtime(23, 0)    # then the server rules


def test_a_rejected_time_write_leaves_no_optimistic_trace():
    if not have_homeassistant():
        skip("homeassistant not installed")
    t = load("time")
    gce = load("api").GeelyControlError
    rec = _RefreshRec()
    api = _Api(raise_=gce("8070", "The last request has not yet been executed"))
    sched = {"rbcStartTime": "23:00", "rbcEndTime": "07:00"}
    hass, b = _bundle(_status(sched=sched), api=api)
    ent = _quiet(t.GeelyScheduledChargingTime(hass, b, "start"))
    with _patched(t, schedule_refresh=rec, time_mod=_Clock()):
        e = _expect_error(ent.async_set_value(dtime(1, 30)))
    assert "Geely Scheduled Charging time: The last request" in str(e), e
    assert ent._optimistic_value is None
    assert sched["rbcStartTime"] == "23:00", "failed write patched the data"
    assert rec.calls == []
    api2 = _Api(raise_=RuntimeError("boom"))
    hass2, b2 = _bundle(_status(sched={}), api=api2)
    ent2 = _quiet(t.GeelyScheduledChargingTime(hass2, b2, "end"))
    with _patched(t, schedule_refresh=rec, time_mod=_Clock()):
        e2 = _expect_error(ent2.async_set_value(dtime(2, 0)))
    assert "Geely Scheduled Charging time failure: boom" in str(e2), e2


# ----------------------------------------- the optimistic hold on toggles ---

def test_a_toggle_holds_its_requested_state_through_a_stale_poll():
    """The poll 8s after a command routinely re-reads the snapshot from BEFORE
    the command, and every unheld switch snapped back to its old state on
    screen while the car was executing - reported live by an owner pressing
    Defrost and watching it revert. The hold ignores a contradicting poll
    inside the window, because a snapshot older than the command cannot
    testify about it."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    sw = load("switch")
    data = _status(climate={"defrost": "false"})
    hass, b = _bundle(data)
    ent = sw.GeelyDefrostSwitch(hass, b)
    with _patched(sw, schedule_refresh=_RefreshRec()):
        asyncio.run(ent.async_turn_on())
    # The stale poll still says off; the switch must not follow it.
    assert ent.is_on is True, "a stale poll snapped the toggle back"


def test_the_hold_ends_the_moment_a_poll_confirms():
    if not have_homeassistant():
        skip("homeassistant not installed")
    sw = load("switch")
    data = _status(climate={"defrost": "false"})
    hass, b = _bundle(data)
    ent = sw.GeelyDefrostSwitch(hass, b)
    with _patched(sw, schedule_refresh=_RefreshRec()):
        asyncio.run(ent.async_turn_on())
    data["vehicleStatus"]["additionalVehicleStatus"]["climateStatus"]["defrost"] = "true"
    assert ent.is_on is True
    assert ent._hold_state is None, "a confirming poll did not release the hold"
    # And once released, raw state rules again - including a later off.
    data["vehicleStatus"]["additionalVehicleStatus"]["climateStatus"]["defrost"] = "false"
    assert ent.is_on is False


def test_the_hold_expires_rather_than_pinning_a_refused_command():
    """If the car never confirms - a genuinely refused command - the window
    runs out and the raw state returns. Optimism is bounded."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    sw = load("switch")
    data = _status(climate={"defrost": "false"})
    hass, b = _bundle(data)
    ent = sw.GeelyDefrostSwitch(hass, b)
    with _patched(sw, schedule_refresh=_RefreshRec()):
        asyncio.run(ent.async_turn_on())
    ent._hold_until = 0.0                       # the 45s window has passed
    assert ent.is_on is False, "expired optimism beat the raw state"


def test_every_command_switch_carries_the_hold():
    """The bounce was reported on 'the built-in cards' generally, not one
    switch, so the fix has to cover every toggle a card renders. A new switch
    class that forgets the mixin lands right back in the bug."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    sw = load("switch")
    for cls in (sw.GeelySwitch, sw.GeelyGCleanSwitch, sw.GeelyDefrostSwitch,
                sw.GeelyWindowVentilationSwitch):
        assert issubclass(cls, sw._OptimisticHold), cls.__name__
