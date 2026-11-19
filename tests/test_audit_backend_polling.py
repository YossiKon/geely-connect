"""ARCH-1: failed acquisition and communication must remain observable."""
import asyncio
import copy
from unittest.mock import patch

from conftest import FAKE_VIN, load
from test_init_lifecycle import _mod, _setup


def _snapshot(odo):
    return {"code": 1000, "data": {"vehicleStatus": {
        "basicVehicleStatus": {"speed": "0", "engineStatus": "engine_off"},
        "additionalVehicleStatus": {"maintenanceStatus": {"odometer": str(odo)}}}}}


def test_failed_gps_acquisition_does_not_satisfy_the_parked_anchor():
    m = _mod()
    def configure(api):
        api.vehicle_status = lambda: copy.deepcopy(_snapshot(100))
        api.request_position_refresh = lambda: {"code": 1000, "success": True}
    _, _, _, api, coord, _ = _setup(m, api_tweak=configure, poll_mode="eco")
    api.vehicle_status = lambda: copy.deepcopy(_snapshot(110))
    asyncio.run(coord.refresh())
    attempts = []
    def refused():
        attempts.append(1)
        return {"code": "8070", "success": False, "message": "busy"}
    api.request_position_refresh = refused
    for _ in range(30):
        asyncio.run(coord.refresh())
    assert 2 <= len(attempts) <= 3, attempts


def test_transport_failure_retries_then_success_satisfies_the_anchor():
    m = _mod()
    def configure(api):
        api.vehicle_status = lambda: copy.deepcopy(_snapshot(100))
        api.request_position_refresh = lambda: {"code": 1000, "success": True}
    _, hass, entry, api, coord, _ = _setup(m, api_tweak=configure, poll_mode="eco")
    state = hass.data["geely_connect"][entry.entry_id]["poll_state"]
    api.vehicle_status = lambda: copy.deepcopy(_snapshot(110))
    asyncio.run(coord.refresh())
    attempts = []
    def position():
        attempts.append(state["cycle"])
        if len(attempts) == 1:
            raise OSError("temporary network failure")
        return {"code": "1000", "success": True}
    api.request_position_refresh = position
    for _ in range(20):
        asyncio.run(coord.refresh())
    assert len(attempts) == 2 and attempts[1] - attempts[0] == 2, attempts
    assert state["fix_odo"] == 110
    assert state["position_pending"] is False


def test_retry_budget_resets_only_for_new_movement_or_an_explicit_refresh():
    m = _mod()
    def configure(api):
        api.vehicle_status = lambda: copy.deepcopy(_snapshot(100))
        api.request_position_refresh = lambda: {"code": 1000, "success": True}
    _, hass, entry, api, coord, _ = _setup(m, api_tweak=configure, poll_mode="eco")
    state = hass.data["geely_connect"][entry.entry_id]["poll_state"]
    attempts = []
    api.request_position_refresh = lambda: (attempts.append(state["cycle"]) or
        {"code": "8070", "success": False})
    api.vehicle_status = lambda: copy.deepcopy(_snapshot(110))
    for _ in range(25):
        asyncio.run(coord.refresh())
    assert len(attempts) == 3
    assert state["fix_odo"] == 100
    assert [b - a for a, b in zip(attempts, attempts[1:])] == [2, 4]
    state["force_secondary"] = True
    asyncio.run(coord.refresh())
    assert len(attempts) == 4
    api.vehicle_status = lambda: copy.deepcopy(_snapshot(120))
    for _ in range(10):
        asyncio.run(coord.refresh())
    assert len(attempts) >= 5
    assert state["fix_odo"] == 100


def test_successful_wake_followed_by_failed_status_does_not_advance_anchor():
    m = _mod()
    def configure(api):
        api.vehicle_status = lambda: copy.deepcopy(_snapshot(100))
        api.request_position_refresh = lambda: {"code": 1000, "success": True}
    _, hass, entry, api, coord, _ = _setup(m, api_tweak=configure)
    state = hass.data["geely_connect"][entry.entry_id]["poll_state"]
    coord.data = _snapshot(110)["data"]
    def failed():
        raise ValueError("offline")
    api.vehicle_status = failed
    asyncio.run(coord.refresh())
    assert state["fix_odo"] == 100
    assert state["position_pending"] is True


def test_actual_legacy_pai_rejects_refused_envelopes():
    from test_api_client import _client
    module = load("api")
    api = _client()
    api._authed_apis_call = lambda *a: {"code": "8070", "success": False}
    try:
        api.request_position_refresh()
    except module.GeelyControlError:
        pass
    else:
        raise AssertionError("refused PAI was accepted")


def test_position_acknowledgements_respect_each_gateway_contract():
    module = load("api")
    for response in ({"code": "000000", "data": {}}, {"code": 0}, {"code": "1000"}):
        assert module._check_position_resp(response, new_platform=True) is response
    for response, new_platform in ((None, False), ({"code": "8070"}, True),
            ({"code": "000000", "success": False}, True), ({"code": 1000}, False)):
        try:
            module._check_position_resp(response, new_platform=new_platform)
        except module.GeelyControlError:
            pass
        else:
            raise AssertionError("missing or refused acknowledgement was accepted")


def test_an_entry_reload_has_a_fresh_budget_and_no_other_entry_anchor():
    m = _mod()
    def configure(api):
        api.vehicle_status = lambda: copy.deepcopy(_snapshot(100))
        api.request_position_refresh = lambda: {"code": 1000, "success": True}
    _, first_hass, first_entry, _, _, _ = _setup(m, api_tweak=configure)
    first = first_hass.data["geely_connect"][first_entry.entry_id]["poll_state"]
    first.update(position_tries=3, fix_odo=500)
    _, next_hass, next_entry, _, _, _ = _setup(m, api_tweak=configure)
    second = next_hass.data["geely_connect"][next_entry.entry_id]["poll_state"]
    assert second["fix_odo"] == 100 and second["position_tries"] == 0
    assert first is not second and first["fix_odo"] == 500

