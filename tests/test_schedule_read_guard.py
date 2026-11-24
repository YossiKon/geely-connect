"""Keep early charge-server reads from undoing a locally accepted window.

Exercise the real coordinator update closure and command entities with separate
wall and monotonic clocks. The fake charge-server GET returns whatever snapshot
`server` currently holds (unchanged for a lagging/unstored write, or mutated
explicitly to model an external edit), or raises `server["raise"]` when that is
set, and records every call.
"""
import asyncio
import copy
import http.client
import json
import sys
import types
from contextlib import ExitStack
from datetime import time as dtime
from unittest.mock import patch

from conftest import load
from test_command_entities import _Api, _quiet
from test_init_lifecycle import _mod, _setup, _Patched, _e2_catalogue


def _make_sched(slot, start="23:00", end="07:00", active=True):
    if slot == 6:
        return {
            "bcCycleActive": "true" if active else "false",
            "rbcStartTime": start,
            "rbcEndTime": end,
        }
    return {
        "timerId": "1" if active else "2",
        "scheduleList": [{
            "startTime": start,
            "endtime": end,
            "duration": load("api").window_length(start, end),
            "day": "1,2,3,4,5,6,7",
        }],
    }


def _fixture(slot, mode="normal"):
    m = _mod()
    _, hass, _, _, coord, _ = _setup(
        m, poll_mode=mode, api_tweak=_e2_catalogue if slot == 2 else None)
    bundle = hass.data["geely_connect"]["e1"]
    poll = bundle["poll_state"]
    server = {"data": _make_sched(slot), "code": 1000}
    coord.data["_scheduled_charging"] = copy.deepcopy(server["data"])
    api = bundle["api"]
    slot_key = str(slot)
    gets = []
    orig_get = api.charge_server_get

    def fake_get(s):
        gets.append(str(s))
        if str(s) == slot_key:
            if server.get("raise") is not None:
                raise server["raise"]
            return {"code": server["code"], "data": copy.deepcopy(server["data"])}
        return orig_get(s)

    api.charge_server_get = fake_get
    write_api = _Api()
    api.scheduled_charging_set = write_api.scheduled_charging_set
    api.scheduled_charging_set_slot_2 = write_api.scheduled_charging_set_slot_2
    tm, sw = load("time"), load("switch")
    start = _quiet(tm.GeelyScheduledChargingTime(hass, bundle, "start"))
    end = _quiet(tm.GeelyScheduledChargingTime(hass, bundle, "end"))
    toggle = _quiet(sw.GeelyScheduledChargingSwitch(hass, bundle))
    # Distinct epochs: stamping with time.time() instead of time.monotonic()
    # puts written_at ~1.7e9 seconds away from monotonic() and breaks the guard.
    wall = [1_700_000_000.0]
    mono = [1000.0]

    def advance(dt):
        wall[0] += dt
        mono[0] += dt

    def align_secondary():
        # Set cycle so the next normal refresh hits (cyc - 1) % N == 0 without
        # setting force_secondary; cycles 2..4 after it are off-cycle for both
        # secondary reads and the odometer-less position timer.
        poll["cycle"] = 0

    stack = ExitStack()
    stack.enter_context(patch("time.time", lambda: wall[0]))
    stack.enter_context(patch("time.monotonic", lambda: mono[0]))
    stack.enter_context(_Patched(m, monotonic=lambda: mono[0]))
    stack.enter_context(_Patched(tm, schedule_refresh=lambda *a, **k: None))
    stack.enter_context(_Patched(sw, schedule_refresh=lambda *a, **k: None))
    return (stack, wall, mono, advance, align_secondary, poll, api, coord,
            start, end, toggle, write_api, server, gets)


def _window(coord, slot):
    sched = coord.data["_scheduled_charging"]
    if slot == 2:
        w = sched["scheduleList"][0]
        return w["startTime"], w["endtime"]
    return sched["rbcStartTime"], sched["rbcEndTime"]


def test_an_early_stale_read_cannot_poison_later_edits_or_toggles():
    for slot in (2, 6):
        for mode in ("normal", "eco"):
            for second_at in (5, 62, 75, 150, 400):
                for action in ("end", "toggle"):
                    (stack, _, _, advance, align_secondary, _, _, coord,
                     start, end, toggle, writes, _, gets) = _fixture(slot, mode)
                    with stack:
                        asyncio.run(start.async_set_value(dtime(22)))
                        advance(3)
                        # A natural secondary-cycle poll fetches the schedule
                        # inside the guard window; no force_secondary needed.
                        align_secondary()
                        asyncio.run(coord.refresh())
                        assert str(slot) in gets
                        assert _window(coord, slot) == ("22:00", "07:00"), (
                            slot, mode, second_at, action)
                        advance(second_at - 3)
                        if action == "end":
                            asyncio.run(end.async_set_value(dtime(8)))
                        else:
                            asyncio.run(toggle.async_turn_off())
                        body = writes.calls[-1]
                        sent = ((body[2], body[3]) if slot == 2 else
                                (body[1]["start_time"], body[1]["end_time"]))
                        assert sent == ("22:00", "08:00" if action == "end" else "07:00"), body


def test_discarded_read_triggers_one_catchup_get_on_normal_refresh_at_15s():
    """#123's case: an unstored write whose early read was discarded must not
    stand for a whole secondary cycle. The next normal refresh at >= 15 s
    reads only the schedule (1 extra GET) and restores server truth."""
    for slot in (2, 6):
        for mode in ("normal", "eco"):
            (stack, _, _, advance, align_secondary, _, api, coord,
             start, _, _, _, _, gets) = _fixture(slot, mode)
            with stack:
                asyncio.run(start.async_set_value(dtime(22)))
                # Cycle hits secondary naturally at +3 s; server still holds 23:00.
                advance(3)
                align_secondary()
                asyncio.run(coord.refresh())
                assert gets.count(str(slot)) == 1
                assert _window(coord, slot) == ("22:00", "07:00")

                # Off-cycle normal refresh at +14 s (still < 15 s): no extra GET yet.
                advance(11)
                asyncio.run(coord.refresh())
                assert gets.count(str(slot)) == 1
                assert _window(coord, slot) == ("22:00", "07:00")

                # Off-cycle normal refresh at +15 s (no force_secondary): reads
                # the schedule once, without state, slot 4, or a position wake.
                state_before = api.calls.count("state")
                pos_before = api.calls.count("position")
                trip_before = gets.count("4")
                advance(1)
                asyncio.run(coord.refresh())
                assert gets.count(str(slot)) == 2
                assert api.calls.count("state") == state_before
                assert api.calls.count("position") == pos_before
                assert gets.count("4") == trip_before
                assert _window(coord, slot) == ("23:00", "07:00")

                # Follow-up normal refresh at +35 s: catch-up flag is cleared.
                advance(20)
                asyncio.run(coord.refresh())
                assert gets.count(str(slot)) == 2


def test_external_edit_installs_on_normal_refresh_at_15s_after_early_refresh_data():
    """An edit made in the phone app after an HA write is held off during the
    15 s guard (including a Refresh Data press at 10 s), then installed by the
    15 s normal refresh without waiting for the next secondary cycle."""
    for slot in (2, 6):
        (stack, _, _, advance, _, poll, _, coord,
         start, _, _, _, server, gets) = _fixture(slot)
        with stack:
            asyncio.run(start.async_set_value(dtime(22)))
            # External app edits the schedule at +2 s.
            advance(2)
            server["data"] = _make_sched(slot, "21:00", "06:00")
            # Refresh Data at +10 s reads it, guard discards it and spends the press.
            advance(8)
            poll["force_secondary"] = True
            asyncio.run(coord.refresh())
            assert "force_secondary" not in poll
            assert gets.count(str(slot)) == 1
            assert _window(coord, slot) == ("22:00", "07:00")

            # Off-cycle normal refresh at +14.5 s: still inside the 15 s guard.
            advance(4.5)
            asyncio.run(coord.refresh())
            assert gets.count(str(slot)) == 1
            assert _window(coord, slot) == ("22:00", "07:00")

            # Off-cycle normal refresh at +15.0 s: installs the external edit.
            advance(0.5)
            asyncio.run(coord.refresh())
            assert gets.count(str(slot)) == 2
            assert _window(coord, slot) == ("21:00", "06:00")


def test_no_extra_schedule_get_when_no_read_was_discarded():
    for slot in (2, 6):
        for mode in ("normal", "eco"):
            (stack, _, _, advance, _, _, _, coord,
             start, _, _, _, _, gets) = _fixture(slot, mode)
            with stack:
                asyncio.run(start.async_set_value(dtime(22)))
                # No schedule read ran during [0, 15); cycle 2 is off-cycle.
                advance(15)
                asyncio.run(coord.refresh())
                assert gets.count(str(slot)) == 0
                assert _window(coord, slot) == ("22:00", "07:00")


def test_failed_catchup_get_retries_on_the_next_normal_refresh():
    (stack, _, _, advance, align_secondary, _, _, coord,
     start, _, _, _, server, gets) = _fixture(6)
    with stack:
        asyncio.run(start.async_set_value(dtime(22)))
        advance(3)
        align_secondary()
        asyncio.run(coord.refresh())
        assert gets.count("6") == 1
        assert _window(coord, 6) == ("22:00", "07:00")

        # Catch-up at +15 s fails (non-success code): keeps local copy and retries.
        server["code"] = 5000
        advance(12)
        asyncio.run(coord.refresh())
        assert gets.count("6") == 2
        assert _window(coord, 6) == ("22:00", "07:00")

        # Next normal off-cycle refresh at +35 s succeeds and clears the retry.
        server["code"] = 1000
        advance(20)
        asyncio.run(coord.refresh())
        assert gets.count("6") == 3
        assert _window(coord, 6) == ("23:00", "07:00")

        # Subsequent off-cycle refresh does not re-read.
        advance(20)
        asyncio.run(coord.refresh())
        assert gets.count("6") == 3


def _reads_per_poll(coord, gets, slot, advance, polls, step=20):
    """How many times each of `polls` refreshes, `step` s apart, read the slot."""
    out = []
    for _ in range(polls):
        advance(step)
        before = gets.count(str(slot))
        asyncio.run(coord.refresh())
        out.append(gets.count(str(slot)) - before)
    return out


def test_a_failing_catch_up_read_stops_at_the_cycle_s_own_read():
    """A catch-up read that keeps failing is tried on each poll only up to a
    read the poll makes anyway, here the secondary cycle's; once that one has
    failed past the guard too, the slot is back on its cycle. It used to be
    read on every poll until a read worked or the entry reloaded. Each way a
    read fails: an answer that is not a success, a raised error, a failed pin
    check."""
    m = _mod()
    failures = ({"code": 5000}, {"raise": RuntimeError("charge server down")},
                {"raise": m.GeelyTLSPinError("pin mismatch")})
    for slot in (2, 6):
        for mode, every in (("normal", 4), ("eco", 6), ("live", 3), ("super_eco", 2)):
            for failure in failures:
                (stack, _, _, advance, align_secondary, _, _, coord,
                 start, _, _, _, server, gets) = _fixture(slot, mode)
                with stack:
                    asyncio.run(start.async_set_value(dtime(22)))
                    advance(3)
                    align_secondary()
                    asyncio.run(coord.refresh())
                    assert gets.count(str(slot)) == 1
                    assert _window(coord, slot) == ("22:00", "07:00")
                    server.update(failure)
                    # Polls 2..every: the catch-up, failing; poll every+1: the
                    # cycle's own read, failing past the guard; then nothing
                    # off the cycle until its next read, at 2*every+1.
                    got = _reads_per_poll(coord, gets, slot, advance, 2 * every)
                    assert got == [1] * every + [0] * (every - 1) + [1], (
                        slot, mode, failure, got)
                    assert _window(coord, slot) == ("22:00", "07:00")
                    # The server answers again: the cycle's next read installs it.
                    server.pop("raise", None)
                    server["code"] = 1000
                    got = _reads_per_poll(coord, gets, slot, advance, every)
                    assert got == [0] * (every - 1) + [1], (slot, mode, failure, got)
                    assert _window(coord, slot) == ("23:00", "07:00")


def test_a_read_failing_inside_the_guard_leaves_the_catch_up_owed():
    """A Refresh press inside the guard whose read fails ends nothing: the
    read past the guard is still owed, and it installs what the server holds."""
    for slot in (2, 6):
        (stack, _, _, advance, align_secondary, poll, _, coord,
         start, _, _, _, server, gets) = _fixture(slot)
        with stack:
            asyncio.run(start.async_set_value(dtime(22)))
            advance(3)
            align_secondary()
            asyncio.run(coord.refresh())
            server["code"] = 5000
            advance(7)
            poll["force_secondary"] = True
            asyncio.run(coord.refresh())
            assert "force_secondary" not in poll
            assert gets.count(str(slot)) == 2
            assert _window(coord, slot) == ("22:00", "07:00")
            server["code"] = 1000
            advance(5)
            asyncio.run(coord.refresh())
            assert gets.count(str(slot)) == 3
            assert _window(coord, slot) == ("23:00", "07:00")


def test_a_press_s_read_that_starts_as_the_guard_ends_is_past_it():
    """The guard ends at 15 s for this as for the discard: a Refresh press,
    a read the poll makes anyway, whose read starts then and fails ends the
    catch-up."""
    for slot in (2, 6):
        (stack, _, _, advance, align_secondary, poll, _, coord,
         start, _, _, _, server, gets) = _fixture(slot)
        with stack:
            asyncio.run(start.async_set_value(dtime(22)))
            advance(3)
            align_secondary()
            asyncio.run(coord.refresh())
            server["code"] = 5000
            advance(12)
            poll["force_secondary"] = True
            asyncio.run(coord.refresh())
            assert "force_secondary" not in poll
            assert gets.count(str(slot)) == 2
            assert _reads_per_poll(coord, gets, slot, advance, 1) == [0]


def test_a_read_that_starts_inside_the_guard_and_fails_after_it_ends_nothing():
    """The cycle's read starts at +14 s and fails at +17 s: it started inside
    the guard, so the catch-up is still owed on the next poll. What counts is
    when the read started, not when it ended."""
    for slot in (2, 6):
        (stack, _, _, advance, _, poll, api, coord,
         start, _, _, _, server, gets) = _fixture(slot)
        with stack:
            asyncio.run(start.async_set_value(dtime(22)))
            advance(3)
            # A Refresh press on cycle 3 reads the slot and sets it aside.
            poll["cycle"] = 2
            poll["force_secondary"] = True
            asyncio.run(coord.refresh())
            assert gets.count(str(slot)) == 1
            server["code"] = 5000
            old = start._hass.async_add_executor_job

            async def executor(fn, *args):
                if fn == api.charge_server_get and args == (str(slot),):
                    advance(3)  # a slow answer that fails
                return await old(fn, *args)

            start._hass.async_add_executor_job = executor
            advance(11)
            poll["cycle"] = 4  # +14 s: cycle 5, the cycle's own read
            asyncio.run(coord.refresh())
            start._hass.async_add_executor_job = old
            assert gets.count(str(slot)) == 2
            assert _reads_per_poll(coord, gets, slot, advance, 1) == [1]


def _status(charging):
    return {"code": 1000, "data": {"vehicleStatus": {
        "basicVehicleStatus": {"speed": "0"},
        "additionalVehicleStatus": {"electricVehicleStatus": {
            "statusOfChargerConnection": "3" if charging else "0",
            "chargeLevel": "80"}}}}}


def test_a_charging_poll_s_failed_read_ends_the_catch_up():
    """Every poll of a charging car, and the first after it stops, reads the
    slot anyway: a failed read there past the guard ends the catch-up, and
    once the car is parked the slot is back on its cycle."""
    (stack, _, _, advance, align_secondary, _, api, coord,
     start, _, _, _, server, gets) = _fixture(6)
    with stack:
        asyncio.run(start.async_set_value(dtime(22)))
        advance(3)
        align_secondary()
        api.status_results.append(_status(True))
        asyncio.run(coord.refresh())
        assert gets.count("6") == 1
        server["code"] = 5000
        api.status_results.append(_status(True))   # +15 s, cycle 2: charging
        api.status_results.append(_status(False))  # +35 s, cycle 3: just stopped
        assert _reads_per_poll(coord, gets, 6, advance, 1, step=12) == [1]
        assert _reads_per_poll(coord, gets, 6, advance, 1, step=20) == [1]
        # +55 s, cycle 4: parked, off the cycle - nothing is owed any more.
        assert _reads_per_poll(coord, gets, 6, advance, 1, step=20) == [0]


def test_a_write_after_failures_ended_the_catch_up_owes_none_by_itself():
    """Ending the catch-up clears what owes it: a new write whose guard sets
    no read aside owes no catch-up."""
    for slot in (2, 6):
        (stack, _, _, advance, align_secondary, _, _, coord,
         start, end, _, _, server, gets) = _fixture(slot)
        with stack:
            asyncio.run(start.async_set_value(dtime(22)))
            advance(3)
            align_secondary()
            asyncio.run(coord.refresh())
            server["code"] = 5000
            assert _reads_per_poll(coord, gets, slot, advance, 4) == [1, 1, 1, 1]
            server["code"] = 1000
            asyncio.run(end.async_set_value(dtime(8)))  # cycles 6 and 7 next: off
            assert _reads_per_poll(coord, gets, slot, advance, 2, step=15) == [0, 0]
            assert _window(coord, slot) == ("22:00", "08:00")


def test_a_catch_up_ended_by_failures_is_owed_again_after_the_next_write():
    for slot in (2, 6):
        (stack, _, _, advance, align_secondary, _, _, coord,
         start, end, _, _, server, gets) = _fixture(slot)
        with stack:
            asyncio.run(start.async_set_value(dtime(22)))
            advance(3)
            align_secondary()
            asyncio.run(coord.refresh())
            server["code"] = 5000
            assert _reads_per_poll(coord, gets, slot, advance, 4) == [1, 1, 1, 1]
            server["code"] = 1000
            asyncio.run(end.async_set_value(dtime(8)))
            advance(3)
            align_secondary()
            asyncio.run(coord.refresh())
            assert _window(coord, slot) == ("22:00", "08:00")
            assert _reads_per_poll(coord, gets, slot, advance, 1, step=12) == [1]
            assert _window(coord, slot) == ("23:00", "07:00")


def test_guard_uses_monotonic_clock_and_exact_15s_boundary():
    """Wall-clock jumps must not affect the guard, and 14 s vs 15 s pins the
    exact boundary so changing 15 -> 14 or using time.time() fails."""
    for slot in (2, 6):
        (stack, wall, mono, _, align_secondary, _, _, coord,
         start, _, _, _, server, gets) = _fixture(slot)
        with stack:
            asyncio.run(start.async_set_value(dtime(22)))
            server["data"] = _make_sched(slot, "21:00", "06:00")

            # Jump wall clock forward 1000 s while monotonic advances only 14 s:
            # a read started at +14 s must still be guarded.
            wall[0] += 1000.0
            mono[0] += 14.0
            align_secondary()
            asyncio.run(coord.refresh())
            assert gets.count(str(slot)) == 1
            assert _window(coord, slot) == ("22:00", "07:00")

            # Jump wall clock backward 2000 s while monotonic hits +15 s:
            # the catch-up normal refresh must run and install server truth.
            wall[0] -= 2000.0
            mono[0] += 1.0
            asyncio.run(coord.refresh())
            assert gets.count(str(slot)) == 2
            assert _window(coord, slot) == ("21:00", "06:00")


def test_slot_2_re_reads_window_after_await_when_poll_publishes_in_flight():
    """If a poll publishes a fresh slot-2 dict while _set_slot_2 awaits the
    write, _set_slot_2 must patch the live coordinator copy before arming the
    guard, not the replaced dict captured before the await."""
    (stack, _, _, advance, align_secondary, _, api, coord,
     start, end, _, writes, _, _) = _fixture(2)
    with stack:
        old_executor = start._hass.async_add_executor_job
        raced = [False]

        async def executor(fn, *args):
            if not raced[0] and getattr(fn, "__name__", "") == "<lambda>":
                raced[0] = True
                # A secondary poll publishes a brand-new slot-2 dict (still
                # holding 23:00-07:00) while the Start write is in flight.
                align_secondary()
                await coord.refresh()
            return await old_executor(fn, *args)

        start._hass.async_add_executor_job = executor
        asyncio.run(start.async_set_value(dtime(22)))
        assert raced[0]
        assert _window(coord, 2) == ("22:00", "07:00")

        # An End edit 10 s later (inside the 15 s guard) sends 22:00-08:00.
        advance(10)
        asyncio.run(end.async_set_value(dtime(8)))
        body = writes.calls[-1]
        assert (body[2], body[3]) == ("22:00", "08:00"), body


def test_a_read_started_before_a_write_cannot_erase_it_on_late_completion():
    (stack, _, _, advance, align_secondary, _, api, coord,
     start, _, _, _, _, gets) = _fixture(6)
    with stack:
        old_executor = start._hass.async_add_executor_job
        paused = [False]

        async def executor(fn, *args):
            if fn == api.charge_server_get and args == ("6",) and not paused[0]:
                paused[0] = True
                # The server captured its response before the accepted write,
                # and the request finishes beyond the ordinary 15 s guard.
                stale = fn(*args)
                advance(1)
                await start.async_set_value(dtime(22))
                advance(29)
                return stale
            return await old_executor(fn, *args)

        start._hass.async_add_executor_job = executor
        align_secondary()
        asyncio.run(coord.refresh())
        assert paused[0]
        assert _window(coord, 6) == ("22:00", "07:00")

        # Because the late read was discarded and the guard already elapsed,
        # the next normal off-cycle refresh fetches server truth.
        advance(5)
        asyncio.run(coord.refresh())
        assert gets.count("6") == 2
        assert _window(coord, 6) == ("23:00", "07:00")


def test_refused_writes_do_not_protect_a_stale_local_copy():
    from homeassistant.exceptions import HomeAssistantError
    for slot in (2, 6):
        for kind in ("start", "toggle"):
            (stack, _, _, advance, align_secondary, _, _, coord,
             start, _, toggle, writes, _, _) = _fixture(slot)
            with stack:
                writes.raise_ = load("api").GeelyControlError("8070", "refused")
                try:
                    asyncio.run(
                        start.async_set_value(dtime(22))
                        if kind == "start" else toggle.async_turn_on())
                except HomeAssistantError:
                    pass
                else:
                    raise AssertionError("refused write succeeded")
                # An unrelated stale cache must not be held when the command
                # failed before acceptance.
                coord.data["_scheduled_charging"] = {}
                advance(3)
                align_secondary()
                asyncio.run(coord.refresh())
                assert _window(coord, slot) == ("23:00", "07:00")


def test_accepted_toggles_arm_the_same_guard_as_time_edits():
    for slot in (2, 6):
        (stack, _, _, advance, align_secondary, _, _, coord,
         _, _, toggle, _, _, _) = _fixture(slot)
        with stack:
            asyncio.run(toggle.async_turn_off())
            advance(3)
            align_secondary()
            asyncio.run(coord.refresh())
            sched = coord.data["_scheduled_charging"]
            assert sched["timerId"] == "2" if slot == 2 else sched["bcCycleActive"] == "false"


def test_an_early_read_remains_stale_when_it_completes_after_the_guard():
    for slot in (2, 6):
        (stack, _, _, advance, align_secondary, _, api, coord,
         start, _, _, _, _, _) = _fixture(slot)
        with stack:
            asyncio.run(start.async_set_value(dtime(22)))
            old_executor = start._hass.async_add_executor_job
            delayed = [False]

            async def executor(fn, *args):
                if fn == api.charge_server_get and args == (str(slot),) and not delayed[0]:
                    delayed[0] = True
                    stale = fn(*args)
                    advance(13)  # finishes at +16 s, past the guard
                    return stale
                return await old_executor(fn, *args)

            start._hass.async_add_executor_job = executor
            advance(3)
            align_secondary()
            asyncio.run(coord.refresh())
            assert delayed[0]
            assert _window(coord, slot) == ("22:00", "07:00"), slot


def test_a_write_during_the_later_trip_read_survives_snapshot_publication():
    (stack, _, _, advance, align_secondary, _, api, coord,
     start, _, _, _, _, _) = _fixture(6)
    with stack:
        old_executor = start._hass.async_add_executor_job
        interleaved = [False]

        async def executor(fn, *args):
            if fn == api.charge_server_get and args == ("4",) and not interleaved[0]:
                interleaved[0] = True
                # The charging GET is already complete. While the same poll
                # awaits slot 4, a successful command patches coordinator.data.
                # Publication must use that current copy, not the older GET.
                advance(1)
                await start.async_set_value(dtime(22))
                advance(29)
            return await old_executor(fn, *args)

        start._hass.async_add_executor_job = executor
        align_secondary()
        asyncio.run(coord.refresh())
        assert interleaved[0]
        assert _window(coord, 6) == ("22:00", "07:00")


class _NewGateway:
    """An HTTPS connection to the new gateway, answered by path: the client's
    own request and its check of the answer run as they do on a car."""

    answer = None   # (method, path) -> the answer's body, set by the test
    paths = None    # every path asked, query included

    def __init__(self, host, *args, **kwargs):
        pass

    def request(self, method, path, body=b"", headers=None):
        type(self).paths.append(path)
        self._body = json.dumps(type(self).answer(method, path)).encode()

    def getresponse(self):
        return types.SimpleNamespace(status=200, read=lambda: self._body)

    def close(self):
        pass


def test_a_geely_em_entry_s_failing_catch_up_stops_at_the_cycle_s_own_read():
    """The same bound on a Geely EM entry, through the real adapter and
    client, only the HTTPS connection replaced. That platform has no state
    block to read (serves_state is False) and reads the schedule through
    _authed_charge. Both ways its read fails - an answer whose code is not
    a success, which comes back as it came, and one with success false,
    which the client raises - keep the reads on for the session, sign
    nothing in, and end the catch-up at the cycle's own read."""
    from test_zeekr_adapter import _make_adapter, _patch_idaas, _restore_idaas
    from test_zeekr_init import _mod as _em_mod, _setup_zeekr

    m = _em_mod()
    helpers = sys.modules[m.schedule_read_missed.__module__]
    plan = {"code": "000000", "success": True, "data": {
        "timerId": "1", "target": "2", "startTime": "23:00", "endTime": "07:00",
        "command": "start", "bcCycleActive": True, "bcTempActive": False}}
    status = {"code": "000000", "success": True, "data": {
        "basicVehicleStatus": {"speed": "0"},
        "additionalVehicleStatus": {"electricVehicleStatus": {
            "statusOfChargerConnection": "0", "chargeLevel": "80"}}}}
    for failure in ({"code": "500", "msg": "busy"},
                    {"code": "500", "success": False, "message": "busy"}):
        answer = {"plan": plan}
        paths = []
        mono = [1000.0]

        def gateway(method, path, answer=answer):
            route = path.split("?")[0]
            if route.endswith("/getChargingPlan"):
                return answer["plan"]
            if route.endswith("/vehicle/status/latest"):
                return status
            if route.endswith("/function/model/info"):
                return {"code": "000000", "success": True, "data": []}
            return {"code": "000000", "success": True, "data": {}}

        def real(**kw):
            a, c = _make_adapter(password="fake-password-0")
            c.enc_vin = "fake-x-vin-0123456789"
            return a

        def plans(paths=paths):
            return sum(p.split("?")[0].endswith("/getChargingPlan") for p in paths)

        def reads(polls, mono=mono, plans=plans):
            out = []
            for _ in range(polls):
                mono[0] += 20
                before = plans()
                asyncio.run(coord.refresh())
                out.append(plans() - before)
            return out

        _NewGateway.answer = staticmethod(gateway)
        _NewGateway.paths = paths
        with ExitStack() as stack:
            stack.enter_context(patch.object(http.client, "HTTPSConnection", _NewGateway))
            stack.enter_context(patch("time.monotonic", lambda mono=mono: mono[0]))
            stack.enter_context(_Patched(m, monotonic=lambda mono=mono: mono[0]))
            # A renewal could go through and would be counted: the adapter
            # holds a password and the IDaaS sign-in is stubbed.
            _patch_idaas()
            stack.callback(_restore_idaas)
            ok, hass, entry, api = _setup_zeekr(m, factory=real)
            assert ok is True and api.serves_state is False and api.reads_charge_server
            # The entry's own coordinator: _FakeCoordinator.instance can name
            # another test's when the suite has loaded test_init_lifecycle twice.
            bundle = hass.data["geely_connect"]["e1"]
            coord, poll = bundle["coordinator"], bundle["poll_state"]
            sched = coord.data["_scheduled_charging"]
            assert sched["rbcStartTime"] == "23:00", sched
            # A write: the local copy patched, the guard armed.
            sched["rbcStartTime"] = "22:00"
            helpers.note_schedule_write(coord)
            mono[0] += 3
            poll["cycle"] = 0
            before = plans()
            asyncio.run(coord.refresh())
            assert plans() == before + 1
            assert coord.data["_scheduled_charging"]["rbcStartTime"] == "22:00"
            answer["plan"] = failure
            # Polls 2-4 the catch-up, failing; 5 the cycle's own read, failing;
            # then nothing off the cycle until its next read at 9.
            got = reads(8)
            assert got == [1, 1, 1, 1, 0, 0, 0, 1], (failure, got)
            assert api.reads_charge_server, failure
            assert coord.data["_scheduled_charging"]["rbcStartTime"] == "22:00"
            answer["plan"] = plan
            got = reads(4)
            assert got == [0, 0, 0, 1], (failure, got)
            assert coord.data["_scheduled_charging"]["rbcStartTime"] == "23:00"
            # Nothing signed in on the way: no renewal, no sign-in path asked.
            assert api._renewals == 0, failure
            assert not [p for p in paths if "/auth/" in p], paths
