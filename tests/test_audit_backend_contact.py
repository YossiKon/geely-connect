"""ARCH-3: honest contact freshness during grace and recovery."""
import asyncio
import http.client
import json
import types
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from conftest import FAKE_VIN, have_homeassistant, load
from run import skip
from test_init_lifecycle import _mod, _quoted_clean, _real_quoter, _setup


def _bind_ha_refresh(m, coord):
    """Mirror HA DataUpdateCoordinator._async_refresh on the fake coordinator:
    catch UpdateFailed/ConfigEntryAuthFailed into last_exception and
    last_update_success=False, and leave last_exception untouched on success."""
    coord.last_update_success = True
    coord.last_exception = None

    async def _async_refresh():
        try:
            coord.data = await coord.update_method()
        except (m.UpdateFailed, m.ConfigEntryAuthFailed) as err:
            coord.last_exception = err
            coord.last_update_success = False
        else:
            coord.last_update_success = True

    coord.async_refresh = _async_refresh
    return coord


def test_grace_snapshot_retains_successful_contact_time_and_reports_disconnected():
    m = _mod()
    _, hass, _, api, _, _ = _setup(m)
    coord = _bind_ha_refresh(m, hass.data["geely_connect"]["e1"]["coordinator"])
    sensor = load("sensor").GeelyLastUpdatedSensor(coord, FAKE_VIN, "Fake car")
    connected = load("binary_sensor").GeelyConnectivity(coord, FAKE_VIN, "Fake car")
    sensor._stamp()
    first = sensor.native_value

    def failed():
        raise ValueError("offline")

    api.vehicle_status = failed
    asyncio.run(coord.async_refresh())
    sensor._stamp()
    assert sensor.native_value == first
    assert connected.is_on is False
    assert coord.last_update_success is True, "general grace availability should remain"


def test_clock_advances_only_on_real_status_success_through_failure_and_recovery():
    m = _mod()
    sensor_mod = load("sensor")
    stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    now = [stamp]
    with patch.object(sensor_mod._dt_util, "utcnow", side_effect=lambda: now[0]):
        _, hass, _, api, _, _ = _setup(m)
        coord = _bind_ha_refresh(m, hass.data["geely_connect"]["e1"]["coordinator"])
        entity = sensor_mod.GeelyLastUpdatedSensor(coord, FAKE_VIN, "Fake car")
        connected = load("binary_sensor").GeelyConnectivity(coord, FAKE_VIN, "Fake car")
        entity._stamp()
        assert entity.native_value == stamp
        assert connected.is_on

        def failed():
            raise ValueError("offline")

        good = api.vehicle_status
        api.vehicle_status = failed
        for attempt in range(3):
            now[0] = stamp + timedelta(minutes=attempt + 1)
            asyncio.run(coord.async_refresh())
            assert coord.last_update_success is (attempt < 2)
            entity._stamp()
            assert entity.native_value == stamp
            assert not connected.is_on

        api.vehicle_status = good
        recovered = stamp + timedelta(hours=1)
        now[0] = recovered
        asyncio.run(coord.async_refresh())
        entity._stamp()
        assert entity.native_value == recovered and connected.is_on


def test_connected_stays_on_during_healthy_in_flight_poll():
    m = _mod()
    _, hass, _, api, _, _ = _setup(m)
    bundle = hass.data["geely_connect"]["e1"]
    coord = _bind_ha_refresh(m, bundle["coordinator"])
    connected = load("binary_sensor").GeelyConnectivity(coord, FAKE_VIN, "Fake car")
    assert connected.is_on is True

    seen = {}
    orig_pos = api.request_position_refresh
    orig_status = api.vehicle_status

    def pos_probe():
        seen["position"] = connected.is_on
        return orig_pos()

    def status_probe():
        seen["status"] = connected.is_on
        return orig_status()

    api.request_position_refresh = pos_probe
    api.vehicle_status = status_probe
    # Force a position wake so both PAI and status round-trips are observed.
    bundle["poll_state"]["force_secondary"] = True
    asyncio.run(coord.async_refresh())
    assert seen == {"position": True, "status": True}, seen
    assert connected.is_on is True


def test_refused_or_auth_failed_primary_status_does_not_claim_contact():
    m = _mod()
    sensor_mod = load("sensor")
    for outcome in ({"code": 5000, "data": {}}, load("api").GeelyAuthError("revoked")):
        _, hass, _, api, _, _ = _setup(m)
        coord = _bind_ha_refresh(m, hass.data["geely_connect"]["e1"]["coordinator"])
        connected = load("binary_sensor").GeelyConnectivity(coord, FAKE_VIN, "Fake car")
        entity = sensor_mod.GeelyLastUpdatedSensor(coord, FAKE_VIN, "Fake car")
        entity._stamp()
        previous = coord.geely_last_successful_contact
        api.status_results = [outcome]
        asyncio.run(coord.async_refresh())
        assert coord.last_update_success is False
        assert isinstance(coord.last_exception, (m.UpdateFailed, m.ConfigEntryAuthFailed))
        assert not connected.is_on
        entity._stamp()
        assert coord.geely_last_successful_contact == previous
        assert entity.native_value == previous


def test_uncontacted_coordinator_and_legacy_test_double_do_not_invent_success():
    if not have_homeassistant():
        skip("homeassistant not installed")
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


def test_explicit_refresh_surfaces_both_grace_failures_then_recovers():
    m = _mod()
    sensor_mod = load("sensor")
    button_mod = load("button")
    stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    now = [stamp]
    with patch.object(sensor_mod._dt_util, "utcnow", side_effect=lambda: now[0]):
        _, hass, _, api, _, _ = _setup(m)
        bundle = hass.data["geely_connect"]["e1"]
        coord = _bind_ha_refresh(m, bundle["coordinator"])
        poll_state = bundle["poll_state"]
        good = api.vehicle_status

        def failed():
            raise ValueError("offline")

        button = button_mod.GeelyRefreshButton(hass, bundle)

        # Grace failures under a frozen clock (now == setup stamp) must still
        # raise, and each press must drive the real forced-press path.
        api.vehicle_status = failed
        for i in range(2):
            pos_before = api.calls.count("position")
            try:
                asyncio.run(button.async_press())
            except button_mod.HomeAssistantError as err:
                assert str(err) == "Geely sync failed: vehicle_status: offline", str(err)
            else:
                raise AssertionError("failed explicit refresh was reported as successful")
            assert poll_state.get("force_secondary") is True
            assert api.calls.count("position") == pos_before + 1, f"press {i + 1} did not wake"
            assert coord.last_update_success, "general availability grace must remain"
            assert not coord.geely_status_success

        # Recovery under the same frozen clock must succeed and spend force_secondary.
        api.vehicle_status = good
        state_before = api.calls.count("state")
        asyncio.run(button.async_press())
        assert coord.geely_status_success
        assert coord.geely_last_status_error is None
        assert api.calls.count("state") == state_before + 1
        assert "force_secondary" not in poll_state
        assert "force_woke" not in poll_state


def test_refresh_press_succeeds_when_overlapping_refresh_hits_grace():
    """On HA versions without a coordinator refresh lock, another refresh can
    overlap a Refresh press during its secondary reads. If the press's own
    status read succeeded at >= press start time, the press must not raise
    a false 'Geely sync failed'."""
    m = _mod()
    sensor_mod = load("sensor")
    button_mod = load("button")
    t0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    now = [t0]
    with patch.object(sensor_mod._dt_util, "utcnow", side_effect=lambda: now[0]):
        _, hass, _, api, _, _ = _setup(m)
        bundle = hass.data["geely_connect"]["e1"]
        coord = _bind_ha_refresh(m, bundle["coordinator"])
        button = button_mod.GeelyRefreshButton(hass, bundle)

        # Intercept the press's secondary read (after its own vehicle_status
        # has stamped geely_last_successful_contact) to run an overlapping
        # refresh whose vehicle_status fails into grace.
        overlapped = [False]
        orig_status = api.vehicle_status
        orig_state = api.vehicle_status_state

        async def _async_add_executor_job(fn, *args):
            if fn == orig_state and not overlapped[0]:
                overlapped[0] = True
                api.vehicle_status = lambda: (_ for _ in ()).throw(
                    ValueError("overlap glitch"))
                try:
                    await coord.async_refresh()
                finally:
                    api.vehicle_status = orig_status
            return fn(*args)

        hass.async_add_executor_job = _async_add_executor_job
        now[0] = t0 + timedelta(seconds=10)
        asyncio.run(button.async_press())
        assert overlapped[0]
        assert coord.geely_last_successful_contact == t0 + timedelta(seconds=10)
        assert coord.geely_status_success is False


def test_grace_refresh_quotes_today_scrubbed_error_not_stale_last_exception():
    """HA never clears last_exception after recovery. A later grace failure on
    Refresh Data must quote today's scrubbed error, never the stale exception
    from an earlier outage; past grace it quotes the new last_exception."""
    m = _mod()
    sensor_mod = load("sensor")
    button_mod = load("button")
    quote, secret, words = _real_quoter()
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    now = [t0]
    with patch.object(sensor_mod._dt_util, "utcnow", side_effect=lambda: now[0]):
        _, hass, _, api, _, _ = _setup(m)
        api.without_secrets = quote
        bundle = hass.data["geely_connect"]["e1"]
        coord = _bind_ha_refresh(m, bundle["coordinator"])
        connected = load("binary_sensor").GeelyConnectivity(coord, FAKE_VIN, "Fake car")
        button = button_mod.GeelyRefreshButton(hass, bundle)
        good = api.vehicle_status

        # 1) Earlier outage goes past grace, populating coord.last_exception.
        api.vehicle_status = lambda: (_ for _ in ()).throw(
            ValueError("last week's outage"))
        for i in range(3):
            now[0] = t0 + timedelta(minutes=i + 1)
            asyncio.run(coord.async_refresh())
        assert coord.last_update_success is False
        assert "last week's outage" in str(coord.last_exception)

        # 2) Recovery: last_update_success returns to True, while HA keeps the
        #    stale last_exception on the coordinator.
        api.vehicle_status = good
        now[0] = t0 + timedelta(days=7)
        asyncio.run(coord.async_refresh())
        assert coord.last_update_success is True
        assert coord.geely_last_status_error is None
        assert "last week's outage" in str(coord.last_exception)

        # 3) New grace failures containing a secret and the full VIN.
        api.vehicle_status = lambda: (_ for _ in ()).throw(
            ValueError(f"today's blip: {words}"))
        for step in (1, 2):
            now[0] = t0 + timedelta(days=7, minutes=step)
            try:
                asyncio.run(button.async_press())
            except button_mod.HomeAssistantError as err:
                msg = str(err)
            else:
                raise AssertionError(f"grace press {step} did not fail")
            assert "today's blip" in msg, msg
            assert "last week's outage" not in msg, msg
            _quoted_clean(msg, secret)
            assert f"...{FAKE_VIN[-4:]}" in msg, msg
            assert coord.last_update_success is True
            assert connected.is_on is False

        # 4) Third failure goes past grace: quotes today's past-grace error.
        api.vehicle_status = lambda: (_ for _ in ()).throw(
            ValueError(f"sustained outage: {words}"))
        now[0] = t0 + timedelta(days=7, minutes=3)
        try:
            asyncio.run(button.async_press())
        except button_mod.HomeAssistantError as err:
            msg = str(err)
        else:
            raise AssertionError("past-grace press did not fail")
        assert "sustained outage" in msg, msg
        assert "today's blip" not in msg, msg
        _quoted_clean(msg, secret)
        assert coord.last_update_success is False
        assert connected.is_on is False


class _EMGateway:
    """Minimal HTTPSConnection stand-in for the Geely EM (Zeekr) new gateway."""

    answer = None

    def __init__(self, host, *args, **kwargs):
        pass

    def request(self, method, path, body=b"", headers=None):
        self._body = json.dumps(type(self).answer(method, path)).encode()

    def getresponse(self):
        return types.SimpleNamespace(status=200, read=lambda: self._body)

    def close(self):
        pass


def test_geely_em_contact_tracking_grace_and_refresh_error():
    """Real Geely EM (Zeekr) adapter + client: in-flight Connected stays on,
    grace turns Connected off while freezing Last Updated and quoting today's
    scrubbed error rather than a stale last_exception, and recovery clears
    force_secondary (serves_state=False)."""
    from test_zeekr_adapter import _make_adapter, _patch_idaas, _restore_idaas
    from test_zeekr_init import _mod as _em_mod, _setup_zeekr

    m = _em_mod()
    sensor_mod = load("sensor")
    button_mod = load("button")
    status_ok = {"code": "000000", "success": True, "data": {
        "basicVehicleStatus": {"speed": "0"},
        "additionalVehicleStatus": {"electricVehicleStatus": {
            "statusOfChargerConnection": "0", "chargeLevel": "80"}}}}
    state = {"status": status_ok, "on_status": None}

    def gateway(method, path):
        route = path.split("?")[0]
        if route.endswith("/vehicle/status/latest"):
            if state["on_status"] is not None:
                state["on_status"]()
            return state["status"]
        if route.endswith("/function/model/info"):
            return {"code": "000000", "success": True, "data": []}
        return {"code": "000000", "success": True, "data": {}}

    def real(**kw):
        adapter, client = _make_adapter(password="fake-password-0")
        client.enc_vin = "fake-x-vin-0123456789"
        return adapter

    t0 = datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc)
    now = [t0]
    _EMGateway.answer = staticmethod(gateway)
    with ExitStack() as stack:
        stack.enter_context(patch.object(http.client, "HTTPSConnection", _EMGateway))
        stack.enter_context(patch.object(sensor_mod._dt_util, "utcnow",
                                         side_effect=lambda: now[0]))
        _patch_idaas()
        stack.callback(_restore_idaas)

        ok, hass, _, api = _setup_zeekr(m, factory=real)
        assert ok is True and api.serves_state is False
        bundle = hass.data["geely_connect"]["e1"]
        coord = _bind_ha_refresh(m, bundle["coordinator"])
        poll_state = bundle["poll_state"]
        connected = load("binary_sensor").GeelyConnectivity(coord, FAKE_VIN, "Fake EM")
        last_updated = sensor_mod.GeelyLastUpdatedSensor(coord, FAKE_VIN, "Fake EM")
        button = button_mod.GeelyRefreshButton(hass, bundle)

        last_updated._stamp()
        assert connected.is_on is True
        assert last_updated.native_value == t0

        # Healthy poll: Connected must remain True while the EM status GET is in flight.
        seen_in_flight = []
        state["on_status"] = lambda: seen_in_flight.append(connected.is_on)
        t1 = t0 + timedelta(minutes=5)
        now[0] = t1
        asyncio.run(coord.async_refresh())
        state["on_status"] = None
        last_updated._stamp()
        assert seen_in_flight == [True], seen_in_flight
        assert connected.is_on is True
        assert last_updated.native_value == t1

        # Simulate a stale last_exception from an earlier outage that HA kept.
        coord.last_exception = m.UpdateFailed("vehicle_status: stale em outage")

        # Gateway now returns a failure carrying the VIN and access token.
        token = api._client.access_token
        state["status"] = {
            "code": "500", "success": False,
            "message": f"em gateway down for {FAKE_VIN} tok={token}",
        }
        for step in (1, 2):
            now[0] = t1 + timedelta(minutes=step)
            try:
                asyncio.run(button.async_press())
            except button_mod.HomeAssistantError as err:
                msg = str(err)
            else:
                raise AssertionError(f"EM grace press {step} did not fail")
            assert "em gateway down" in msg, msg
            assert "stale em outage" not in msg, msg
            assert token not in msg and FAKE_VIN not in msg, msg
            assert coord.last_update_success is True
            assert connected.is_on is False
            last_updated._stamp()
            assert last_updated.native_value == t1

        # Recovery press succeeds, clears force_secondary, and advances Last Updated.
        state["status"] = status_ok
        t_rec = t1 + timedelta(minutes=10)
        now[0] = t_rec
        asyncio.run(button.async_press())
        last_updated._stamp()
        assert connected.is_on is True
        assert last_updated.native_value == t_rec
        assert coord.geely_last_status_error is None
        assert "force_secondary" not in poll_state
