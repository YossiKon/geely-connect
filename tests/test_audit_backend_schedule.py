"""SAFE-3: whole-window writes retain the peer's pending intent."""
import asyncio
import copy
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import time

from conftest import load
from test_command_entities import _bundle, _status, _quiet, _patched, _RefreshRec, _Clock, _slot_2
from test_api_client import _client


def _api():
    api = _client()
    api.bodies = []
    def send(method, path, body):
        api.bodies.append(json.loads(body))
        return {"code": 1000, "success": True}
    api._authed_apis_call = send
    return api


def _write(api, *, slot="6", kind=None, start="23:00", end="07:00", command="stop"):
    fn = api.scheduled_charging_set_slot_2 if slot == "2" else api.scheduled_charging_set
    return fn(command=command, start_time=start, end_time=end, changed_time=kind)


def test_stale_poll_between_start_and_end_keeps_the_pending_start():
    t = load("time")
    old = {"rbcStartTime": "23:00", "rbcEndTime": "07:00"}
    api = _api()
    hass, bundle = _bundle(_status(sched=copy.deepcopy(old)), api=api)
    start = _quiet(t.GeelyScheduledChargingTime(hass, bundle, "start"))
    end = _quiet(t.GeelyScheduledChargingTime(hass, bundle, "end"))
    with _patched(t, schedule_refresh=_RefreshRec(), time_mod=_Clock()):
        asyncio.run(start.async_set_value(time(22)))
        bundle["coordinator"].data["_scheduled_charging"] = copy.deepcopy(old)
        asyncio.run(end.async_set_value(time(8)))
    assert api.bodies[-1]["rbcStartTime"] == "22:00", api.bodies
    assert api.bodies[-1]["rbcEndTime"] == "08:00"


def test_both_slots_keep_pending_times_across_a_toggle_and_stale_time_write():
    for slot in ("6", "2"):
        api = _api()
        _write(api, slot=slot, kind="start", start="22:00")
        _write(api, slot=slot, command="start")
        _write(api, slot=slot, kind="end", end="08:00")
        body = api.bodies[-1]
        assert body["command"] == "start", body
        if slot == "6":
            assert (body["rbcStartTime"], body["rbcEndTime"]) == ("22:00", "08:00")
        else:
            assert body["scheduleList"][0]["startTime"] == "22:00"
            assert body["scheduleList"][0]["duration"] == "10:00"


def test_slot_2_entities_also_keep_peer_intent_across_a_stale_poll():
    t = load("time")
    api = _api()
    old = _slot_2()
    hass, bundle = _bundle(_status(sched=copy.deepcopy(old)), api=api,
                           caps={"scheduled_charging.slot_2": True})
    start = _quiet(t.GeelyScheduledChargingTime(hass, bundle, "start"))
    end = _quiet(t.GeelyScheduledChargingTime(hass, bundle, "end"))
    with _patched(t, schedule_refresh=_RefreshRec(), time_mod=_Clock()):
        asyncio.run(start.async_set_value(time(11)))
        bundle["coordinator"].data["_scheduled_charging"] = copy.deepcopy(old)
        asyncio.run(end.async_set_value(time(18)))
    assert api.bodies[-1]["scheduleList"][0]["startTime"] == "11:00"
    assert api.bodies[-1]["scheduleList"][0]["duration"] == "07:00"


def test_failed_write_preserves_previous_intent_and_never_holds_failed_value():
    api = _api()
    _write(api, kind="start", start="22:00")
    send = api._authed_apis_call
    api._authed_apis_call = lambda *a: {"code": "8070", "success": False}
    try:
        _write(api, kind="end", end="09:00")
    except load("api").GeelyControlError:
        pass
    else:
        raise AssertionError("refused mutation succeeded")
    api._authed_apis_call = send
    _write(api, command="start")
    assert api.bodies[-1]["rbcStartTime"] == "22:00"
    assert api.bodies[-1]["rbcEndTime"] == "07:00"
    assert "end" not in api._charging_pending["6"]


def test_expired_intent_and_another_entry_never_override_reported_values():
    api = _api()
    _write(api, kind="start", start="22:00")
    api._charging_pending["6"]["start"] = ("22:00", -1)
    _write(api, kind="end", end="08:00")
    assert api.bodies[-1]["rbcStartTime"] == "23:00"
    fresh = _api()
    _write(fresh, kind="end", end="08:00")
    assert fresh.bodies[-1]["rbcStartTime"] == "23:00"


def test_concurrent_whole_window_writes_serialize_merge_and_acceptance():
    api = _api()
    entered, release, second_started = threading.Event(), threading.Event(), threading.Event()
    send = api._authed_apis_call
    def blocked(*args):
        if not entered.is_set():
            entered.set()
            assert release.wait(5), "test did not release first request"
        return send(*args)
    api._authed_apis_call = blocked
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(_write, api, kind="start", start="22:00")
        assert entered.wait(5)
        def second_write():
            second_started.set()
            return _write(api, kind="end", end="08:00")
        second = pool.submit(second_write)
        assert second_started.wait(5)
        assert not api.bodies, "second send entered while first was in flight"
        release.set()
        first.result(timeout=5)
        second.result(timeout=5)
    assert api.bodies[-1]["rbcStartTime"] == "22:00"
    assert api.bodies[-1]["rbcEndTime"] == "08:00"


def test_bad_boundary_and_collapsed_slot_2_window_are_refused():
    api = _api()
    for args in ({"kind": "invalid"}, {"slot": "2", "start": "07:00", "end": "07:00"}):
        try:
            _write(api, **args)
        except (ValueError, load("api").GeelyControlError):
            pass
        else:
            raise AssertionError("invalid write accepted")
    assert not api.bodies


def test_slot_2_validates_the_pending_peer_rather_than_a_stale_equal_time():
    t = load("time")
    api = _api()
    old = _slot_2()
    hass, bundle = _bundle(_status(sched=copy.deepcopy(old)), api=api,
                           caps={"scheduled_charging.slot_2": True})
    start = _quiet(t.GeelyScheduledChargingTime(hass, bundle, "start"))
    end = _quiet(t.GeelyScheduledChargingTime(hass, bundle, "end"))
    with _patched(t, schedule_refresh=_RefreshRec(), time_mod=_Clock()):
        asyncio.run(end.async_set_value(time(18)))
        bundle["coordinator"].data["_scheduled_charging"] = copy.deepcopy(old)
        asyncio.run(start.async_set_value(time(17)))
    assert api.bodies[-1]["scheduleList"][0]["duration"] == "01:00"


def test_time_write_cannot_restore_a_pending_peer_to_the_old_reported_time():
    t = load("time")
    api = _api()
    hass, bundle = _bundle(_status(sched=_slot_2()), api=api,
                           caps={"scheduled_charging.slot_2": True})
    start = _quiet(t.GeelyScheduledChargingTime(hass, bundle, "start"))
    with _patched(t, schedule_refresh=_RefreshRec(), time_mod=_Clock()):
        try:
            asyncio.run(start.async_set_value(time(17)))
        except t.HomeAssistantError as exc:
            assert "can't be the same" in str(exc)
        else:
            raise AssertionError("equal effective window was accepted")
    assert not api.bodies and start._optimistic_value is None
