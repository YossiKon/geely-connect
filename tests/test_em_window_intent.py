"""EM schedule contracts with class-defined fake clients; no cloud or devices.

The 60 s accepted-intent hold is a software policy shared with the legacy
time setter contract, not a measured EM propagation bound.
"""
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace

from conftest import FAKE_VIN, load

ad = load("zeekr_adapter")


class _Client:
    enc_vin = "fake-vehicle-token"
    hf_token = "fake-hf-token"

    def __init__(self):
        self.trip = {"btActive": "true", "btTempActive": "false", "bw": "1"}
        self.calls = []
        self.error = None
        self.entered = None
        self.release = None

    def travel_plan_new(self):
        return {"code": "000000", "data": self.trip, "message": "ok"}

    def charging_plan_new(self):
        # Deliberately stays stale after writes, like a poll during propagation.
        return {"code": "000000", "data": {
            "bcCycleActive": True, "startTime": "23:00", "endTime": "07:00"}}

    def set_charging_plan_new(self, **kw):
        self.calls.append(kw)
        if self.entered is not None:
            self.entered.set()
            assert self.release.wait(5), "test did not release the first write"
        if self.error is not None:
            raise self.error
        return {"code": "000000"}


def _make():
    adapter = ad.ZeekrAdapter(
        email="fake@example.invalid", vin=FAKE_VIN, user_id="fake-user",
        access_token="fake-access", refresh_token="fake-refresh",
        hf_token="fake-hf", hf_expiry=10 ** 15)
    adapter._client = _Client()
    return adapter, adapter._client


def _write(adapter, boundary=None, start="23:00", end="07:00", command="start"):
    return adapter.scheduled_charging_set(
        command=command, start_time=start, end_time=end, changed_time=boundary)


@contextmanager
def _clock():
    old = ad.time
    now = SimpleNamespace(value=1000.0)
    ad.time = SimpleNamespace(monotonic=lambda: now.value, time=old.time)
    try:
        yield now
    finally:
        ad.time = old








def test_stale_poll_between_boundary_edits_cannot_undo_the_accepted_start():
    adapter, client = _make()
    _write(adapter, "start", start="22:00")
    stale = adapter.charge_server_get("6")["data"]
    _write(adapter, "end", start=stale["rbcStartTime"], end="08:00")
    assert [(c["start_time"], c["end_time"]) for c in client.calls] == [
        ("22:00", "07:00"), ("22:00", "08:00")]


def test_each_boundary_expires_after_its_own_60_second_software_hold():
    adapter, client = _make()
    with _clock() as now:
        _write(adapter, "start", start="22:00")
        now.value += 30
        _write(adapter, "end", end="08:00")
        now.value += 30
        _write(adapter)
        assert client.calls[-1]["start_time"] == "23:00", "expired start was held longer"
        assert client.calls[-1]["end_time"] == "08:00", "end expired with the start"
        now.value += 30
        _write(adapter)
        assert client.calls[-1]["end_time"] == "07:00"


def test_a_new_explicit_boundary_edit_wins_over_its_earlier_intent():
    adapter, client = _make()
    _write(adapter, "start", start="22:00")
    _write(adapter, "end", end="08:00")
    _write(adapter, "start", start="21:00")
    assert (client.calls[-1]["start_time"], client.calls[-1]["end_time"]) == ("21:00", "08:00")


def test_overlapping_executor_writes_merge_only_after_the_first_accepts():
    adapter, client = _make()
    client.entered, client.release = threading.Event(), threading.Event()
    second_started = threading.Event()

    def second():
        second_started.set()
        return _write(adapter, "end", end="08:00")

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(_write, adapter, "start", "22:00")
        try:
            assert client.entered.wait(5)
            other = pool.submit(second)
            assert second_started.wait(5)
        finally:
            client.release.set()
        assert first.result(timeout=5)["code"] == 1000
        assert other.result(timeout=5)["code"] == 1000
    assert [(c["start_time"], c["end_time"]) for c in client.calls] == [
        ("22:00", "07:00"), ("22:00", "08:00")]


def test_toggles_preserve_pending_boundaries_and_stop_still_sends_no_window():
    adapter, client = _make()
    _write(adapter, "start", start="22:00")
    _write(adapter, "end", end="08:00")
    _write(adapter, command="stop")
    assert client.calls[-1] == {"command": "stop", "start_time": "", "end_time": "", "target": "2"}
    _write(adapter, command="start")
    assert client.calls[-1] == {"command": "start", "start_time": "22:00", "end_time": "08:00", "target": "2"}
    assert adapter.stores_window_only_with_start is True


def test_time_edit_after_an_accepted_stop_cannot_reenable_the_schedule():
    adapter, client = _make()
    _write(adapter, command="stop")
    try:
        _write(adapter, "end", end="08:00")
    except ad.GeelyControlError:
        pass
    else:
        raise AssertionError("time edit bypassed accepted Off")
    assert len(client.calls) == 1


def test_time_edit_while_off_is_refused_without_sending_or_autoenabling():
    adapter, client = _make()
    try:
        _write(adapter, "start", start="22:00", command="stop")
    except ad.GeelyControlError:
        pass
    else:
        raise AssertionError("off time edit silently discarded")
    assert client.calls == []


def test_accepted_on_survives_a_stale_off_command_only_until_its_hold_expires():
    adapter, client = _make()
    with _clock() as now:
        _write(adapter, command="start")
        now.value += 59
        _write(adapter, "start", start="22:00", command="stop")
        assert client.calls[-1]["command"] == "start"
        now.value += 1
        try:
            _write(adapter, "end", end="08:00", command="stop")
        except ad.GeelyControlError:
            pass
        else:
            raise AssertionError("time edit extended accepted On beyond its hold")
        assert len(client.calls) == 2


def test_rejected_edits_and_toggles_do_not_replace_accepted_intent():
    adapter, client = _make()
    _write(adapter, "start", start="22:00")
    client.error = ad.ZeekrApiError("code=8500 message=refused")
    for args in ({"boundary": "start", "start": "21:00"}, {"command": "stop"}):
        try:
            _write(adapter, **args)
        except ad.GeelyControlError:
            pass
        else:
            raise AssertionError("fake refusal did not propagate")
    client.error = None
    _write(adapter, "end", end="08:00")
    assert client.calls[-1] == {"command": "start", "start_time": "22:00", "end_time": "08:00", "target": "2"}


def test_a_rejected_boundary_edit_does_not_extend_the_old_hold():
    adapter, client = _make()
    with _clock() as now:
        _write(adapter, "start", start="22:00")
        now.value += 59
        client.error = ad.ZeekrApiError("code=8500 message=refused")
        try:
            _write(adapter, "start", start="21:00")
        except ad.GeelyControlError:
            pass
        else:
            raise AssertionError("fake refusal did not propagate")
        client.error = None
        now.value += 1
        _write(adapter, "end", end="08:00")
        assert client.calls[-1]["start_time"] == "23:00"


def test_a_returned_failure_does_not_become_accepted_boundary_intent():
    adapter, client = _make()
    # The production client raises refusals. Even a non-raising fake failure
    # must not be retained as a successfully accepted boundary.
    original = client.set_charging_plan_new
    client.set_charging_plan_new = lambda **kw: {"code": "8500"}
    assert _write(adapter, "start", start="22:00")["code"] == "8500"
    client.set_charging_plan_new = original
    _write(adapter, "end", end="08:00")
    assert client.calls[-1]["start_time"] == "23:00"


def test_invalid_boundary_is_refused_before_any_transport_call():
    adapter, client = _make()
    try:
        _write(adapter, "other")
    except ValueError:
        pass
    else:
        raise AssertionError("invalid boundary accepted")
    assert client.calls == []
