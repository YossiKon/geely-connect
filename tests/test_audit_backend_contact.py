"""ARCH-3: honest contact freshness during grace and recovery."""
import asyncio
from conftest import FAKE_VIN, load
from test_init_lifecycle import _mod, _setup
from unittest.mock import patch
from datetime import datetime, timezone, timedelta


def test_grace_snapshot_retains_successful_contact_time_and_reports_disconnected():
    m = _mod()
    _, _, _, api, coord, _ = _setup(m)
    coord.last_update_success = True
    sensor = load("sensor").GeelyLastUpdatedSensor(coord, FAKE_VIN, "Fake car")
    connected = load("binary_sensor").GeelyConnectivity(coord, FAKE_VIN, "Fake car")
    sensor._stamp()
    first = sensor.native_value
    def failed():
        raise ValueError("offline")
    api.vehicle_status = failed
    asyncio.run(coord.refresh())
    sensor._stamp()
    assert sensor.native_value == first
    assert connected.is_on is False
    assert coord.last_update_success is True, "general grace availability should remain"


def test_clock_advances_only_on_real_status_success_through_failure_and_recovery():
    m = _mod()
    stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with patch.object(m.dt_util, "utcnow", return_value=stamp):
        _, _, _, api, coord, _ = _setup(m)
    coord.last_update_success = True
    entity = load("sensor").GeelyLastUpdatedSensor(coord, FAKE_VIN, "Fake car")
    connected = load("binary_sensor").GeelyConnectivity(coord, FAKE_VIN, "Fake car")
    entity._stamp()
    assert entity.native_value == stamp
    assert connected.is_on
    def failed():
        raise ValueError("offline")
    good = api.vehicle_status
    api.vehicle_status = failed
    for attempt in range(3):
        try:
            asyncio.run(coord.refresh())
        except m.UpdateFailed:
            assert attempt == 2
        entity._stamp()
        assert entity.native_value == stamp
        assert not connected.is_on
    api.vehicle_status = good
    recovered = stamp + timedelta(hours=1)
    with patch.object(m.dt_util, "utcnow", return_value=recovered):
        asyncio.run(coord.refresh())
    entity._stamp()
    assert entity.native_value == recovered and connected.is_on


def test_refused_or_auth_failed_primary_status_does_not_claim_contact():
    m = _mod()
    for outcome in ({"code": 5000, "data": {}}, load("api").GeelyAuthError("revoked")):
        _, _, _, api, coord, _ = _setup(m)
        previous = coord.geely_last_successful_contact
        api.status_results = [outcome]
        try:
            asyncio.run(coord.refresh())
        except (m.UpdateFailed, m.ConfigEntryAuthFailed):
            pass
        else:
            raise AssertionError("refusal did not fail")
        assert not coord.geely_status_success
        assert coord.geely_last_successful_contact == previous


def test_uncontacted_coordinator_and_legacy_test_double_do_not_invent_success():
    import types
    sensor = load("sensor")
    coord = types.SimpleNamespace(data={}, last_update_success=False,
                                  geely_last_successful_contact=None)
    entity = sensor.GeelyLastUpdatedSensor(coord, FAKE_VIN, "Fake car")
    entity._stamp()
    assert entity.native_value is None
    del coord.geely_last_successful_contact
    entity._stamp()
    assert entity.native_value is None
    coord.last_update_success = True
    entity._stamp()
    assert entity.native_value is not None
