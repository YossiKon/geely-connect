"""Command paths and state parsing for lock, button, cover and device_tracker.

Write paths use a recorder fake for the API, so every assertion is on the
exact serviceId / params / command that would go over the wire. Refresh
scheduling and the lock's optimistic clock are patched at module level and
restored in try/finally so nothing leaks between tests. Skipped when Home
Assistant is not importable, like the rest of the entity tests.

All coordinates in fixtures are obviously fake (0.0 / 51.5-style values) and
only FAKE_VIN ever appears; error-message tests also assert the VIN stays out.
"""
import asyncio

from conftest import FAKE_VIN, have_homeassistant, load
from run import skip

DOMAIN = "geely_connect"


# ------------------------------------------------------------------ fakes ---

class RecorderApi:
    """Records every control() call; raises `exc` instead when set."""

    def __init__(self, exc=None):
        self.calls = []
        self.exc = exc

    def control(self, service_id, parameters=None, command="start", duration=0):
        self.calls.append((service_id, parameters, command, duration))
        if self.exc is not None:
            raise self.exc
        return {"code": "1000", "message": "ok"}


class FakeCoordinator:
    def __init__(self, data=None):
        self.data = data
        self.last_update_success = True
        self.last_exception = None
        self.refresh_calls = 0

    def async_add_listener(self, cb, *a, **k):
        return lambda: None

    async def async_refresh(self):
        self.refresh_calls += 1


class FakeHass:
    def __init__(self):
        self.data = {}

    async def async_add_executor_job(self, fn, *args):
        return fn(*args)

    def async_create_task(self, coro, *a, **k):
        coro.close()
        return None


class FakeEntry:
    entry_id = "e1"
    data = {"vin": FAKE_VIN}
    options: dict = {}


class FakeTime:
    """Stands in for lock.py's `time` module so the optimistic window is pinned."""

    def __init__(self, now=1_000_000.0):
        self.now = now

    def time(self):
        return self.now


def _bundle(api=None, data=None, caps=None, coordinator=None):
    return {
        "api": api if api is not None else RecorderApi(),
        "coordinator": coordinator if coordinator is not None else FakeCoordinator(data),
        "vin": FAKE_VIN,
        "device_name": "Geely EX5 (0000)",
        "capabilities": caps if caps is not None else {},
    }


def _need_ha():
    if not have_homeassistant():
        skip("homeassistant not installed")


def _expect_ha_error(coro):
    """Run `coro`; return the HomeAssistantError text, fail if none is raised."""
    from homeassistant.exceptions import HomeAssistantError
    try:
        asyncio.run(coro)
    except HomeAssistantError as e:
        return str(e)
    raise AssertionError("expected HomeAssistantError, nothing was raised")


class _quiet:
    """Silence a module's _LOGGER (the expected-failure tests would otherwise
    dump tracebacks into the run output). Restored on exit."""

    def __init__(self, mod):
        self._logger = mod._LOGGER

    def __enter__(self):
        self._was = self._logger.disabled
        self._logger.disabled = True

    def __exit__(self, *exc):
        self._logger.disabled = self._was
        return False


# ------------------------------------------------------------------- lock ---

def _lock_data(status):
    return {"vehicleStatus": {"additionalVehicleStatus": {
        "drivingSafetyStatus": {"centralLockingStatus": status}}}}


def _make_lock(api=None, data=None):
    lock = load("lock")
    entity = lock.GeelyLock(FakeHass(), _bundle(api=api, data=data))
    writes = []
    entity.async_write_ha_state = lambda: writes.append(1)
    return lock, entity, writes


def test_lock_state_follows_central_locking_status():
    _need_ha()
    # 1 / 2 (double-locked) are locked, in both string and int spellings.
    for raw in ("1", 1, "2", 2):
        _, entity, _ = _make_lock(data=_lock_data(raw))
        assert entity.is_locked is True, raw
    _, entity, _ = _make_lock(data=_lock_data("0"))
    assert entity.is_locked is False


def test_lock_treats_unknown_status_codes_as_not_locked():
    _need_ha()
    for junk in ("3", "banana", "", "locked"):
        _, entity, _ = _make_lock(data=_lock_data(junk))
        assert entity.is_locked is False, junk


def test_lock_state_is_unknown_when_the_car_reports_nothing():
    _need_ha()
    for data in (None, {}, {"vehicleStatus": {}}):
        _, entity, _ = _make_lock(data=data)
        assert entity.is_locked is None, data
        assert entity.is_locking is False
        assert entity.is_unlocking is False


def test_lock_sends_rdl2_door_all_and_goes_optimistic():
    _need_ha()
    api = RecorderApi()
    lock, entity, writes = _make_lock(api=api, data=_lock_data("0"))
    sched = []
    orig_sched, orig_time = lock.schedule_refresh, lock.time
    lock.schedule_refresh = lambda h, c, *d, after=None: sched.append((c, d, after))
    lock.time = FakeTime(1000.0)
    try:
        asyncio.run(entity.async_lock())
        assert api.calls == [("RDL_2", [{"key": "door", "value": "all"}], "start", 0)]
        # The server still says unlocked, but the entity is optimistically locking.
        assert entity.is_locked is True
        assert entity.is_locking is True
        assert entity.is_unlocking is False
        assert writes == [1], "the optimistic flip was not written to HA"
        (coord, delays, after) = sched[0]
        assert coord is entity.coordinator
        # Two polls, and deliberately NO release callback: a poll that still
        # shows the old state must not end the hold, so only agreement or the
        # timeout does (see test_a_stale_poll_does_not_snap_the_lock_back).
        assert delays == (8, 12)
        assert after is None, "a forced release is the stale-poll bug again"
    finally:
        lock.schedule_refresh, lock.time = orig_sched, orig_time


def test_unlock_sends_rdu2_door_all_and_goes_optimistic():
    _need_ha()
    api = RecorderApi()
    lock, entity, _ = _make_lock(api=api, data=_lock_data("1"))
    orig_sched, orig_time = lock.schedule_refresh, lock.time
    lock.schedule_refresh = lambda *a, **k: None
    lock.time = FakeTime(1000.0)
    try:
        asyncio.run(entity.async_unlock())
        assert api.calls == [("RDU_2", [{"key": "door", "value": "all"}], "start", 0)]
        assert entity.is_locked is False
        assert entity.is_unlocking is True
        assert entity.is_locking is False
    finally:
        lock.schedule_refresh, lock.time = orig_sched, orig_time


def test_lock_transition_ends_the_moment_the_api_catches_up():
    _need_ha()
    lock, entity, _ = _make_lock(data=_lock_data("0"))
    orig_sched, orig_time = lock.schedule_refresh, lock.time
    lock.schedule_refresh = lambda *a, **k: None
    lock.time = FakeTime(1000.0)
    try:
        asyncio.run(entity.async_lock())
        assert entity.is_locking is True
        entity.coordinator.data = _lock_data("1")   # poll confirms the lock
        assert entity.is_locking is False, "spinner still on after confirmation"
        assert entity.is_locked is True
    finally:
        lock.schedule_refresh, lock.time = orig_sched, orig_time


def test_lock_optimistic_window_expires_after_the_timeout():
    _need_ha()
    lock, entity, _ = _make_lock(data=_lock_data("0"))
    clock = FakeTime(1000.0)
    orig_sched, orig_time = lock.schedule_refresh, lock.time
    lock.schedule_refresh = lambda *a, **k: None
    lock.time = clock
    try:
        asyncio.run(entity.async_lock())
        assert entity.is_locked is True                  # optimistic
        clock.now = 1000.0 + lock._TRANSITION_TIMEOUT_S + 0.1
        assert entity.is_locking is False, "spinner never times out"
        assert entity.is_locked is False, "expired optimism beats server truth"
    finally:
        lock.schedule_refresh, lock.time = orig_sched, orig_time


def test_lock_stays_in_transition_while_the_api_reports_nothing():
    _need_ha()
    lock, entity, _ = _make_lock(data=None)     # no telemetry at all
    orig_sched, orig_time = lock.schedule_refresh, lock.time
    lock.schedule_refresh = lambda *a, **k: None
    lock.time = FakeTime(1000.0)
    try:
        asyncio.run(entity.async_lock())
        assert entity.is_locking is True
        assert entity.is_locked is True
    finally:
        lock.schedule_refresh, lock.time = orig_sched, orig_time


def test_a_stale_poll_does_not_snap_the_lock_back():
    """The release used to run after the first poll unconditionally, and the
    8s snapshot is routinely the PRE-command one - so pressing Lock showed
    locked, then snapped back to unlocked at t=8 while the car outside was
    executing the command (reported live by an owner). A poll that still
    shows the old state is not evidence the command failed; the hold now ends
    only on agreement or on the timeout."""
    _need_ha()
    lock, entity, writes = _make_lock(data=_lock_data("0"))
    sched = []
    orig_sched, orig_time = lock.schedule_refresh, lock.time
    lock.schedule_refresh = lambda h, c, *d, **k: sched.append(d)
    clock = FakeTime(1000.0)
    lock.time = clock
    try:
        asyncio.run(entity.async_lock())
        assert entity.is_locked is True
        clock.now = 1008.0                       # the first poll has landed...
        assert entity.is_locked is True, "a stale poll snapped the lock back"
        assert entity.is_locking is True, "the spinner dropped on no evidence"
        assert sched == [(8, 12)], "the second chance to confirm is gone"
    finally:
        lock.schedule_refresh, lock.time = orig_sched, orig_time


def test_lock_rejection_becomes_homeassistanterror_and_stays_pessimistic():
    _need_ha()
    err = load("api").GeelyControlError("8070", "The last request has not yet been executed")
    lock, entity, writes = _make_lock(api=RecorderApi(exc=err), data=_lock_data("0"))
    sched = []
    orig_sched = lock.schedule_refresh
    lock.schedule_refresh = lambda *a, **k: sched.append(a)
    try:
        msg = _expect_ha_error(entity.async_lock())
        assert "RDL_2" in msg and "not yet been executed" in msg, msg
        assert FAKE_VIN not in msg
        # A rejected command must not fake a "locking..." spinner.
        assert entity._pending_target_locked is None
        assert entity.is_locking is False
        assert sched == [] and writes == []
    finally:
        lock.schedule_refresh = orig_sched


def test_lock_unexpected_failure_becomes_homeassistanterror():
    _need_ha()
    lock, entity, _ = _make_lock(api=RecorderApi(exc=RuntimeError("socket burped")),
                                 data=_lock_data("0"))
    with _quiet(lock):
        msg = _expect_ha_error(entity.async_unlock())
    assert "RDU_2" in msg and "failure" in msg and "socket burped" in msg, msg
    assert entity._pending_target_locked is None


# ----------------------------------------------------------------- button ---

def _setup_buttons(bundle):
    button = load("button")
    hass = FakeHass()
    hass.data[DOMAIN] = {"e1": bundle}
    got = []
    asyncio.run(button.async_setup_entry(
        hass, FakeEntry(), lambda es, *a, **k: got.extend(list(es))))
    return button, got


def _by_uid(entities, suffix):
    return next(e for e in entities if e._attr_unique_id.endswith(suffix))


def test_button_setup_builds_find_car_unlock_trunk_and_refresh():
    _need_ha()
    _, got = _setup_buttons(_bundle())
    assert {e._attr_unique_id for e in got} == {
        f"geely_{FAKE_VIN}_btn_find_car",
        f"geely_{FAKE_VIN}_btn_unlock_trunk",
        f"geely_{FAKE_VIN}_btn_refresh",
    }


def test_a_false_capability_flag_drops_only_that_button():
    _need_ha()
    _, got = _setup_buttons(_bundle(caps={"find_car.enabled": False}))
    uids = {e._attr_unique_id for e in got}
    assert f"geely_{FAKE_VIN}_btn_find_car" not in uids
    assert f"geely_{FAKE_VIN}_btn_unlock_trunk" in uids
    assert f"geely_{FAKE_VIN}_btn_refresh" in uids
    # Both flags off: the refresh button survives - it never depends on caps.
    _, got = _setup_buttons(_bundle(caps={"find_car.enabled": False,
                                          "tailgate.enabled": False}))
    assert {e._attr_unique_id for e in got} == {f"geely_{FAKE_VIN}_btn_refresh"}


def test_find_car_button_fires_horn_light_flash():
    _need_ha()
    api = RecorderApi()
    _, got = _setup_buttons(_bundle(api=api))
    asyncio.run(_by_uid(got, "btn_find_car").async_press())
    assert api.calls == [("RHL", [{"key": "rhl", "value": "horn-light-flash"}], "start", 0)]


def test_unlock_trunk_button_fires_rdu2_with_target_trunk():
    _need_ha()
    api = RecorderApi()
    _, got = _setup_buttons(_bundle(api=api))
    asyncio.run(_by_uid(got, "btn_unlock_trunk").async_press())
    assert api.calls == [("RDU_2", [{"key": "target", "value": "trunk"}], "start", 0)]


def test_button_rejection_becomes_homeassistanterror():
    _need_ha()
    err = load("api").GeelyControlError("failure", "Operation failed")
    _, got = _setup_buttons(_bundle(api=RecorderApi(exc=err)))
    msg = _expect_ha_error(_by_uid(got, "btn_find_car").async_press())
    assert "RHL" in msg and "Operation failed" in msg, msg
    assert FAKE_VIN not in msg


def test_button_unexpected_failure_becomes_homeassistanterror():
    _need_ha()
    button, got = _setup_buttons(_bundle(api=RecorderApi(exc=OSError("wire cut"))))
    with _quiet(button):
        msg = _expect_ha_error(_by_uid(got, "btn_unlock_trunk").async_press())
    assert "RDU_2" in msg and "failure" in msg and "wire cut" in msg, msg


def test_refresh_button_polls_immediately_and_succeeds_quietly():
    _need_ha()
    coord = FakeCoordinator()
    _, got = _setup_buttons(_bundle(coordinator=coord))
    asyncio.run(_by_uid(got, "btn_refresh").async_press())
    assert coord.refresh_calls == 1, "press did not actually run a poll"


def test_refresh_button_surfaces_the_polls_exception():
    _need_ha()
    coord = FakeCoordinator()
    coord.last_update_success = False
    coord.last_exception = RuntimeError("DNS is down")
    _, got = _setup_buttons(_bundle(coordinator=coord))
    msg = _expect_ha_error(_by_uid(got, "btn_refresh").async_press())
    assert coord.refresh_calls == 1, "failed before even polling"
    assert "DNS is down" in msg, msg


def test_refresh_button_reports_failure_even_without_a_stored_exception():
    _need_ha()
    coord = FakeCoordinator()
    coord.last_update_success = False
    coord.last_exception = None
    _, got = _setup_buttons(_bundle(coordinator=coord))
    msg = _expect_ha_error(_by_uid(got, "btn_refresh").async_press())
    assert msg == "Geely sync failed", msg


# ------------------------------------------------------------------ cover ---

_COVER_TARGETS = (
    ("GeelySunshade", "sunshade"),
    ("GeelySunroof", "sunroof"),
    ("GeelyWindows", "window"),
)


def _climate_data(**fields):
    return {"vehicleStatus": {"additionalVehicleStatus": {"climateStatus": dict(fields)}}}


def _make_cover(cls_name, api=None, data=None):
    cover = load("cover")
    return cover, getattr(cover, cls_name)(FakeHass(), _bundle(api=api, data=data))


def test_cover_open_sends_rws2_start_for_its_target():
    _need_ha()
    for cls_name, target in _COVER_TARGETS:
        api = RecorderApi()
        cover, entity = _make_cover(cls_name, api=api)
        sched, orig = [], cover.schedule_refresh
        cover.schedule_refresh = lambda h, c, *d, after=None: sched.append((d, after))
        try:
            asyncio.run(entity.async_open_cover())
        finally:
            cover.schedule_refresh = orig
        assert api.calls == [("RWS_2", [{"key": "target", "value": target}], "start", 0)], cls_name
        assert sched == [((8,), None)], cls_name


def test_cover_close_sends_rws2_stop_for_its_target():
    _need_ha()
    for cls_name, target in _COVER_TARGETS:
        api = RecorderApi()
        cover, entity = _make_cover(cls_name, api=api)
        orig = cover.schedule_refresh
        cover.schedule_refresh = lambda *a, **k: None
        try:
            asyncio.run(entity.async_close_cover())
        finally:
            cover.schedule_refresh = orig
        assert api.calls == [("RWS_2", [{"key": "target", "value": target}], "stop", 0)], cls_name


def test_cover_rejection_becomes_homeassistanterror_and_skips_the_poll():
    _need_ha()
    err = load("api").GeelyControlError(1404, None)      # default message branch
    cover, entity = _make_cover("GeelySunroof", api=RecorderApi(exc=err))
    sched, orig = [], cover.schedule_refresh
    cover.schedule_refresh = lambda *a, **k: sched.append(a)
    try:
        msg = _expect_ha_error(entity.async_open_cover())
    finally:
        cover.schedule_refresh = orig
    assert "sunroof" in msg and "1404" in msg, msg
    assert FAKE_VIN not in msg
    assert sched == [], "a rejected command still scheduled a refresh"


def test_cover_unexpected_failure_becomes_homeassistanterror():
    _need_ha()
    cover, entity = _make_cover("GeelyWindows", api=RecorderApi(exc=ValueError("bad json")))
    with _quiet(cover):
        msg = _expect_ha_error(entity.async_close_cover())
    assert "cover failure" in msg and "bad json" in msg, msg


def test_sunshade_maps_1_closed_2_open_and_tolerates_junk():
    _need_ha()
    for raw, expect in (("1", True), ("2", False), (1, True), (2, False),
                        ("banana", False)):
        _, entity = _make_cover("GeelySunshade", data=_climate_data(curtainOpenStatus=raw))
        assert entity.is_closed is expect, raw
    _, entity = _make_cover("GeelySunshade", data=_climate_data())
    assert entity.is_closed is None, "missing field must read unknown"
    _, entity = _make_cover("GeelySunshade", data=None)
    assert entity.is_closed is None, "no payload must read unknown"


def test_sunroof_maps_1_closed_2_open_and_tolerates_junk():
    _need_ha()
    for raw, expect in (("1", True), ("2", False), (1, True), (2, False),
                        ("wide-open", False)):
        _, entity = _make_cover("GeelySunroof", data=_climate_data(sunroofOpenStatus=raw))
        assert entity.is_closed is expect, raw
    _, entity = _make_cover("GeelySunroof", data=_climate_data())
    assert entity.is_closed is None
    # The sunroof must not read the curtain's field.
    _, entity = _make_cover("GeelySunroof", data=_climate_data(curtainOpenStatus="1"))
    assert entity.is_closed is None, "sunroof answered from the curtain field"


def test_windows_cover_follows_the_four_corner_fields():
    _need_ha()
    closed = dict(winStatusDriver="2", winStatusPassenger="2",
                  winStatusDriverRear="2", winStatusPassengerRear="2")
    _, entity = _make_cover("GeelyWindows", data=_climate_data(**closed))
    assert entity.is_closed is True
    one_open = dict(closed, winStatusDriver="1")
    _, entity = _make_cover("GeelyWindows", data=_climate_data(**one_open))
    assert entity.is_closed is False
    junk = dict(closed, winStatusPassengerRear="garbled")
    _, entity = _make_cover("GeelyWindows", data=_climate_data(**junk))
    assert entity.is_closed is False, "junk must count as not-closed, not crash"
    # A trim reporting a single corner is still an answer...
    _, entity = _make_cover("GeelyWindows", data=_climate_data(winStatusDriver="2"))
    assert entity.is_closed is True
    # ...but reporting none of them is unknown, not open.
    _, entity = _make_cover("GeelyWindows", data=_climate_data())
    assert entity.is_closed is None
    _, entity = _make_cover("GeelyWindows", data=None)
    assert entity.is_closed is None


def test_windows_cover_reports_the_most_open_corner_as_its_position():
    _need_ha()
    # A vent crack reads ~8 on every corner; the aggregate reports the max.
    _, entity = _make_cover("GeelyWindows", data=_climate_data(
        winPosDriver=8, winPosPassenger=8, winPosDriverRear=8, winPosPassengerRear=8))
    assert entity.current_cover_position == 8
    # One window further down wins; junk and out-of-range are tolerated/clamped.
    _, entity = _make_cover("GeelyWindows", data=_climate_data(
        winPosDriver="8", winPosPassenger="60", winPosDriverRear="x", winPosPassengerRear="140"))
    assert entity.current_cover_position == 100
    # No winPos field at all -> unknown, not 0.
    _, entity = _make_cover("GeelyWindows", data=_climate_data())
    assert entity.current_cover_position is None
    _, entity = _make_cover("GeelyWindows", data=None)
    assert entity.current_cover_position is None


# --------------------------------------------------------- device_tracker ---

def _pos_data(**pos):
    return {"vehicleStatus": {"basicVehicleStatus": {"position": pos}}}


def _make_tracker(data):
    dt = load("device_tracker")
    return dt.GeelyTracker(FakeCoordinator(data), FAKE_VIN, "Geely EX5 (0000)")


def test_tracker_decodes_arc_millisecond_coordinates():
    _need_ha()
    # 51.5 deg * 3,600,000 arc-ms; obviously fake test coordinates.
    t = _make_tracker(_pos_data(latitude="185400000", longitude="0"))
    assert abs(t.latitude - 51.5) < 1e-9, t.latitude
    assert t.longitude == 0.0, "a genuine zero coordinate must survive"
    assert t.source_type == "gps"


def test_tracker_decodes_negative_and_boundary_coordinates():
    _need_ha()
    t = _make_tracker(_pos_data(latitude="-185400000", longitude="-648000"))
    assert abs(t.latitude + 51.5) < 1e-9, t.latitude
    assert abs(t.longitude + 0.18) < 1e-9, t.longitude
    # Exactly the poles / date line still decode.
    t = _make_tracker(_pos_data(latitude="324000000", longitude="648000000"))
    assert t.latitude == 90.0 and t.longitude == 180.0


def test_tracker_returns_none_not_zero_for_a_missing_gps_block():
    _need_ha()
    for data in (None, {}, {"vehicleStatus": {}},
                 {"vehicleStatus": {"basicVehicleStatus": {}}},
                 {"vehicleStatus": {"basicVehicleStatus": {"position": "garbled"}}}):
        t = _make_tracker(data)
        assert t.latitude is None, data
        assert t.longitude is None, data
        assert t.extra_state_attributes == {}, data


def test_tracker_returns_none_for_garbled_coordinate_values():
    _need_ha()
    for junk in (None, "", "banana", [], {}):
        t = _make_tracker(_pos_data(latitude=junk, longitude=junk))
        assert t.latitude is None, junk
        assert t.longitude is None, junk


def test_tracker_rejects_coordinates_outside_the_valid_degree_range():
    _need_ha()
    t = _make_tracker(_pos_data(latitude="999999999999", longitude="-999999999999"))
    assert t.latitude is None and t.longitude is None


def test_tracker_reads_the_new_platforms_plain_degrees():
    """The new gateway sends decimal degrees, not arc-milliseconds. The old
    decoder tried the arc-ms divisor first and -33.49/3.6e6 is 9e-06 - a valid
    latitude - so a migrated car reported itself off West Africa while every
    other entity was correct (#53)."""
    _need_ha()
    t = _make_tracker(_pos_data(latitude="-33.49", longitude="151.2"))
    assert abs(t.latitude + 33.49) < 1e-9, t.latitude
    assert abs(t.longitude - 151.2) < 1e-9, t.longitude


def test_tracker_keeps_tropical_arc_ms_coordinates_correct():
    """The regression the naive fix (most-specific divisor first) would have
    caused: a tropical arc-ms coordinate is small enough that dividing by 1e6
    yields a valid-looking degree. Sao Paulo at -23.55, -46.63 in arc-ms must
    still decode to itself, not to -84.78, -167.87."""
    _need_ha()
    arc = 3_600_000.0
    t = _make_tracker(_pos_data(latitude=str(-23.55 * arc),
                                longitude=str(-46.63 * arc)))
    assert abs(t.latitude + 23.55) < 1e-6, t.latitude
    assert abs(t.longitude + 46.63) < 1e-6, t.longitude
    # Just past the pole: no divisor interpretation fits, so None - not a wrap.
    t = _make_tracker(_pos_data(latitude="324000001", longitude="648000001"))
    assert t.latitude is None and t.longitude is None


def test_tracker_attributes_carry_altitude_and_the_raw_fields():
    _need_ha()
    t = _make_tracker(_pos_data(latitude="185400000", longitude="0", altitude="42",
                                posCanBeTrusted="true", marsCoordinates="false"))
    assert t.extra_state_attributes == {
        "altitude_m": "42",
        "trusted": "true",
        "mars_coords": "false",
        "raw_latitude": "185400000",
        "raw_longitude": "0",
    }


def test_tracker_altitude_attribute_is_none_when_the_car_omits_it():
    _need_ha()
    t = _make_tracker(_pos_data(latitude="185400000", longitude="0"))
    attrs = t.extra_state_attributes
    # The key is always present; a missing altitude reads None, never 0.
    assert attrs["altitude_m"] is None
    assert attrs["raw_latitude"] == "185400000"


def test_the_refresh_button_asks_for_the_secondary_endpoints_too():
    """A press means "everything, now". The vehicle-state block is otherwise
    fetched every fourth cycle, so three presses in four re-fetched nothing
    but the main status - which turned hunting an unmapped field into a
    coin flip (#4)."""
    _need_ha()
    poll_state = {}
    bundle = _bundle()
    bundle["poll_state"] = poll_state
    _, got = _setup_buttons(bundle)
    btn = _by_uid(got, "btn_refresh")
    asyncio.run(btn.async_press())
    assert poll_state.get("force_secondary") is True


def test_the_refresh_button_survives_a_bundle_without_poll_state():
    """An entry set up by an older code path carries no poll_state; the button
    must still refresh rather than raise."""
    _need_ha()
    _, got = _setup_buttons(_bundle())
    asyncio.run(_by_uid(got, "btn_refresh").async_press())
