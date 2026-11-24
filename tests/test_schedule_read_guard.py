"""Keep early charge-server reads from undoing a locally accepted window.

Exercise the real coordinator update closure and command entities with separate
wall and monotonic clocks. The fake charge-server GET returns whatever snapshot
`server` currently holds (unchanged for a lagging/unstored write, or mutated
explicitly to model an external edit) and records every call.
"""
import asyncio
import copy
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
