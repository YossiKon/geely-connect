"""Keep early charge-server reads from undoing a locally accepted window.

Exercise the real coordinator update closure and command entities. No transport
write-path policy or real vehicle is involved: the fake server lags by 8 seconds.
"""
import asyncio
import copy
from contextlib import ExitStack
from datetime import time as dtime
from unittest.mock import patch

from conftest import load
from test_command_entities import _Api, _quiet
from test_init_lifecycle import _mod, _setup, _Patched, _e2_catalogue


def _fixture(slot, mode="normal"):
    m = _mod()
    _, hass, _, _, coord, _ = _setup(
        m, poll_mode=mode, api_tweak=_e2_catalogue if slot == 2 else None)
    bundle = hass.data["geely_connect"]["e1"]
    poll = bundle["poll_state"]
    old = ({"bcCycleActive": "true", "rbcStartTime": "23:00", "rbcEndTime": "07:00"}
           if slot == 6 else {"timerId": "1", "scheduleList": [{
               "startTime": "23:00", "endtime": "07:00", "duration": "08:00",
               "day": "1,2,3,4,5,6,7"}]})
    coord.data["_scheduled_charging"] = copy.deepcopy(old)
    api = bundle["api"]
    api.charge_server_get = lambda _: {"code": 1000, "data": copy.deepcopy(old)}
    write_api = _Api()
    api.scheduled_charging_set = write_api.scheduled_charging_set
    api.scheduled_charging_set_slot_2 = write_api.scheduled_charging_set_slot_2
    tm, sw = load("time"), load("switch")
    start = _quiet(tm.GeelyScheduledChargingTime(hass, bundle, "start"))
    end = _quiet(tm.GeelyScheduledChargingTime(hass, bundle, "end"))
    toggle = _quiet(sw.GeelyScheduledChargingSwitch(hass, bundle))
    clock = [1000.0]
    stack = ExitStack()
    stack.enter_context(patch("time.time", lambda: clock[0]))
    stack.enter_context(patch("time.monotonic", lambda: clock[0]))
    stack.enter_context(_Patched(m, monotonic=lambda: clock[0]))
    stack.enter_context(_Patched(tm, schedule_refresh=lambda *a, **k: None))
    stack.enter_context(_Patched(sw, schedule_refresh=lambda *a, **k: None))
    # Service writes and reads use the actual command/coordinator code; this
    # harness's executor simply invokes its fake transport synchronously.
    return stack, clock, poll, api, coord, start, end, toggle, write_api, old


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
                    stack, clock, poll, api, coord, start, end, toggle, writes, _ = _fixture(slot, mode)
                    with stack:
                        asyncio.run(start.async_set_value(dtime(22)))
                        clock[0] = 1003
                        # A timer poll happens to fetch the schedule inside the
                        # propagation window; the next Eco read can be far away.
                        poll["force_secondary"] = True
                        asyncio.run(coord.refresh())
                        assert _window(coord, slot) == ("22:00", "07:00"), (slot, mode, second_at, action)
                        clock[0] = 1000 + second_at
                        if action == "end":
                            asyncio.run(end.async_set_value(dtime(8)))
                        else:
                            asyncio.run(toggle.async_turn_off())
                        body = writes.calls[-1]
                        sent = ((body[2], body[3]) if slot == 2 else
                                (body[1]["start_time"], body[1]["end_time"]))
                        assert sent == ("22:00", "08:00" if action == "end" else "07:00"), body


def test_refresh_at_fifteen_seconds_installs_the_server_answer_and_external_edits():
    for slot in (2, 6):
        stack, clock, poll, api, coord, start, _, _, _, old = _fixture(slot)
        with stack:
            asyncio.run(start.async_set_value(dtime(22)))
            clock[0] = 1015
            # The guard ends at the existing first post-write refresh. Server
            # truth wins then, including an edit made by the phone/another HA.
            poll["force_secondary"] = True
            asyncio.run(coord.refresh())
            assert _window(coord, slot) == ("23:00", "07:00")


def test_a_read_started_before_a_write_cannot_erase_it_on_late_completion():
    stack, clock, poll, api, coord, start, _, _, _, _ = _fixture(6)
    with stack:
        old_executor = start._hass.async_add_executor_job
        paused = [False]
        async def executor(fn, *args):
            if fn == api.charge_server_get and args == ("6",) and not paused[0]:
                paused[0] = True
                # The server captured its response before the accepted write,
                # and the request finishes beyond the ordinary short guard.
                stale = fn(*args)
                clock[0] = 1001
                await start.async_set_value(dtime(22))
                clock[0] = 1030
                return stale
            return await old_executor(fn, *args)
        start._hass.async_add_executor_job = executor
        poll["force_secondary"] = True
        asyncio.run(coord.refresh())
        assert paused[0]
        assert _window(coord, 6) == ("22:00", "07:00")


def test_refused_writes_do_not_protect_a_stale_local_copy():
    from homeassistant.exceptions import HomeAssistantError
    for slot in (2, 6):
        for kind in ("start", "toggle"):
            stack, clock, poll, api, coord, start, _, toggle, writes, _ = _fixture(slot)
            with stack:
                writes.raise_ = load("api").GeelyControlError("8070", "refused")
                try:
                    asyncio.run(start.async_set_value(dtime(22)) if kind == "start" else toggle.async_turn_on())
                except HomeAssistantError:
                    pass
                else:
                    raise AssertionError("refused write succeeded")
                # An unrelated stale cache must not be held when the command
                # failed before acceptance.
                coord.data["_scheduled_charging"] = {}
                clock[0] += 3
                poll["force_secondary"] = True
                asyncio.run(coord.refresh())
                assert _window(coord, slot) == ("23:00", "07:00")


def test_accepted_toggles_arm_the_same_guard_as_time_edits():
    for slot in (2, 6):
        stack, clock, poll, api, coord, _, _, toggle, _, _ = _fixture(slot)
        with stack:
            asyncio.run(toggle.async_turn_off())
            clock[0] += 3
            poll["force_secondary"] = True
            asyncio.run(coord.refresh())
            sched = coord.data["_scheduled_charging"]
            assert sched["timerId"] == "2" if slot == 2 else sched["bcCycleActive"] == "false"


def test_an_early_read_remains_stale_when_it_completes_after_the_guard():
    for slot in (2, 6):
        stack, clock, poll, api, coord, start, _, _, _, _ = _fixture(slot)
        with stack:
            asyncio.run(start.async_set_value(dtime(22)))
            old_executor = start._hass.async_add_executor_job
            delayed = [False]
            async def executor(fn, *args):
                if fn == api.charge_server_get and args == (str(slot),) and not delayed[0]:
                    delayed[0] = True
                    stale = fn(*args)
                    clock[0] = 1016
                    return stale
                return await old_executor(fn, *args)
            start._hass.async_add_executor_job = executor
            clock[0] = 1003
            poll["force_secondary"] = True
            asyncio.run(coord.refresh())
            assert delayed[0]
            assert _window(coord, slot) == ("22:00", "07:00"), slot


def test_a_write_during_the_later_trip_read_survives_snapshot_publication():
    stack, clock, poll, api, coord, start, _, _, _, _ = _fixture(6)
    with stack:
        old_executor = start._hass.async_add_executor_job
        interleaved = [False]
        async def executor(fn, *args):
            if fn == api.charge_server_get and args == ("4",) and not interleaved[0]:
                interleaved[0] = True
                # The charging GET is already complete. While the same poll
                # awaits slot 4, a successful command patches coordinator.data.
                # Publication must use that current copy, not the older GET.
                clock[0] = 1001
                await start.async_set_value(dtime(22))
                clock[0] = 1030
            return await old_executor(fn, *args)
        start._hass.async_add_executor_job = executor
        poll["force_secondary"] = True
        asyncio.run(coord.refresh())
        assert interleaved[0]
        assert _window(coord, 6) == ("22:00", "07:00")
