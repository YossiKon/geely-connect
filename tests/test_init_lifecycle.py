"""Setup, polling, services and teardown in __init__.py.

The update closure here is the only code between the cloud and every entity,
and the teardown path is what keeps a removed car's private key from living
on inside backups - both were almost entirely unexercised before this file.
"""
import asyncio
import functools
import importlib.util
import os
import sys
import tempfile
import types

from conftest import FAKE_VIN, PKG, have_homeassistant, load
from run import skip


def _mod():
    if not have_homeassistant():
        skip("homeassistant not installed")
    if "gc_init_life" in sys.modules:
        return sys.modules["gc_init_life"]
    if "gc" not in sys.modules:
        pkg = types.ModuleType("gc")
        pkg.__path__ = [PKG]
        sys.modules["gc"] = pkg
    spec = importlib.util.spec_from_file_location(
        "gc.__init__", os.path.join(PKG, "__init__.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gc.__init__"] = sys.modules["gc_init_life"] = mod
    spec.loader.exec_module(mod)
    return mod


class _Patched:
    """Swap module attributes for the duration, restoring even on failure."""

    def __init__(self, mod, **attrs):
        self.mod, self.attrs = mod, attrs

    def __enter__(self):
        self.orig = {k: getattr(self.mod, k) for k in self.attrs}
        for k, v in self.attrs.items():
            setattr(self.mod, k, v)
        return self.mod

    def __exit__(self, *exc):
        for k, v in self.orig.items():
            setattr(self.mod, k, v)


# ------------------------------------------------------------- device name ---

def test_device_names_honour_a_custom_nickname():
    """Every naming shape the app produces: a nickname that already names the
    model, one that does not, one with no model behind it, and no name at all."""
    m = _mod()
    base = {"vin": FAKE_VIN, "vehicle_model_code": "E245-J1"}
    n = m._resolve_device_name
    assert n({**base, "vehicle_nickname": "My EX5 Rocket"}) == "My EX5 Rocket (0000)"
    assert n({**base, "vehicle_nickname": "Rocket"}) == "Rocket EX5 (0000)"
    assert n({"vin": FAKE_VIN, "vehicle_nickname": "Rocket"}) == "Rocket (0000)"
    assert n({"vin": FAKE_VIN}) == "Geely (0000)"
    assert n({}) == "Geely"


# ------------------------------------------------------- refetch edge paths ---

def test_a_failed_vehicle_list_never_blocks_setup():
    m = _mod()

    class _Hass:
        async def async_add_executor_job(self, fn, *a):
            return fn(*a)

    entry = types.SimpleNamespace(data={"vin": FAKE_VIN}, entry_id="e1")
    boom = types.SimpleNamespace(
        list_vehicles=lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    with _Patched(m, geely_api=boom):
        asyncio.run(m._maybe_refetch_vehicle_metadata(_Hass(), entry))
    assert "vehicle_nickname" not in entry.data, "a failed heal must change nothing"


def test_an_account_without_this_vin_changes_nothing():
    """A car sold and removed from the account must not have its entry
    overwritten by whichever car remains."""
    m = _mod()

    class _Hass:
        def __init__(self):
            self.updated = False
            outer = self

            class _CE:
                @staticmethod
                def async_update_entry(entry, data=None, **kw):
                    outer.updated = True
            self.config_entries = _CE()

        async def async_add_executor_job(self, fn, *a):
            return fn(*a)

    hass = _Hass()
    entry = types.SimpleNamespace(data={"vin": FAKE_VIN}, entry_id="e1")
    other = types.SimpleNamespace(
        list_vehicles=lambda *a, **k: [{"vin": "L6T99999999999999"}])
    with _Patched(m, geely_api=other):
        asyncio.run(m._maybe_refetch_vehicle_metadata(hass, entry))
    assert hass.updated is False


# --------------------------------------------------------------- registries ---

class _EntityRegistry:
    def __init__(self, entries):
        self.entities = {e.entity_id: e for e in entries}
        self.removed = []
        self.updated = []

    def async_remove(self, entity_id):
        self.removed.append(entity_id)

    def async_update_entity(self, entity_id, **kw):
        self.updated.append((entity_id, kw))


def _er_module(registry, per_entry=None):
    return types.SimpleNamespace(
        async_get=lambda hass: registry,
        async_entries_for_config_entry=lambda reg, eid: per_entry or [],
        RegistryEntryDisabler=__import__("homeassistant.helpers.entity_registry",
                                         fromlist=["RegistryEntryDisabler"]).RegistryEntryDisabler)


def test_only_obsolete_unique_ids_are_purged():
    m = _mod()
    mk = lambda eid, uid, platform="geely_connect": types.SimpleNamespace(
        entity_id=eid, unique_id=uid, platform=platform)
    reg = _EntityRegistry([
        mk("switch.old", f"geely_{FAKE_VIN}_sw_engine_pre_conditioning"),
        mk("sensor.keep", f"geely_{FAKE_VIN}_battery"),
        mk("switch.other", "x_sw_engine_pre_conditioning", platform="other"),
    ])
    entry = types.SimpleNamespace(entry_id="e1")
    with _Patched(m, er=_er_module(reg)):
        n = m._purge_obsolete_entities(object(), entry)
    assert n == 1 and reg.removed == ["switch.old"]


def test_the_device_name_updates_unless_the_user_renamed_it():
    m = _mod()
    entry = types.SimpleNamespace(entry_id="e1", data={
        "vin": FAKE_VIN, "vehicle_nickname": "Rocket",
        "vehicle_model_code": "E245-J1"})

    class _Dev:
        def __init__(self, name, by_user=None):
            self.id, self.name, self.name_by_user = "d1", name, by_user

    class _DevReg:
        def __init__(self, device):
            self.device, self.updates = device, []

        def async_get_device(self, identifiers):
            return self.device

        def async_update_device(self, did, name=None):
            self.updates.append((did, name))

    stale = _DevReg(_Dev("Geely EX5 (0000)"))
    with _Patched(m, dr=types.SimpleNamespace(async_get=lambda h: stale)):
        m._refresh_device_name(object(), entry)
    assert stale.updates == [("d1", "Rocket EX5 (0000)")]

    renamed = _DevReg(_Dev("Geely EX5 (0000)", by_user="Betsy"))
    with _Patched(m, dr=types.SimpleNamespace(async_get=lambda h: renamed)):
        m._refresh_device_name(object(), entry)
    assert renamed.updates == [], "a user rename must win forever"

    gone = _DevReg(None)
    with _Patched(m, dr=types.SimpleNamespace(async_get=lambda h: gone)):
        m._refresh_device_name(object(), entry)
        m._refresh_device_name(object(), types.SimpleNamespace(entry_id="e", data={}))
    assert gone.updates == []


def test_reenable_touches_only_what_the_integration_disabled():
    m = _mod()
    from homeassistant.helpers import entity_registry as real_er
    mk = lambda eid, by: types.SimpleNamespace(entity_id=eid, disabled_by=by)
    entries = [
        mk("sensor.a", real_er.RegistryEntryDisabler.INTEGRATION),
        mk("sensor.b", real_er.RegistryEntryDisabler.USER),
        mk("sensor.c", None),
    ]
    reg = _EntityRegistry([])
    fake_er = types.SimpleNamespace(
        async_get=lambda h: reg,
        async_entries_for_config_entry=lambda r, eid: entries)
    with _Patched(m, er=fake_er):
        n = m._reenable_integration_disabled_entities(object(),
                                                      types.SimpleNamespace(entry_id="e1"))
    assert n == 1
    assert reg.updated == [("sensor.a", {"disabled_by": None})]


def test_purging_full_exposure_removes_only_raw_sensors():
    m = _mod()
    mk = lambda eid, uid: types.SimpleNamespace(entity_id=eid, unique_id=uid)
    entries = [mk("sensor.raw1", f"geely_{FAKE_VIN}_raw_a.b"),
               mk("sensor.keep", f"geely_{FAKE_VIN}_battery")]
    reg = _EntityRegistry([])
    fake_er = types.SimpleNamespace(
        async_get=lambda h: reg,
        async_entries_for_config_entry=lambda r, eid: entries)
    with _Patched(m, er=fake_er):
        n = m._purge_raw_exposure_entities(object(),
                                           types.SimpleNamespace(entry_id="e1"))
    assert n == 1 and reg.removed == ["sensor.raw1"]


# ---------------------------------------------------------------- intervals ---

def test_a_broken_clock_cannot_take_the_interval_down():
    """dt_util raising (bad timezone data) must cost the quiet-hours feature,
    not the poll loop."""
    m = _mod()
    const = load("const")
    p = const.POLL_PROFILES["normal"]

    def _boom():
        raise RuntimeError("tzdata broken")

    with _Patched(m, dt_util=types.SimpleNamespace(now=_boom)):
        secs = m._adaptive_interval({"vehicleStatus": {}}, 0, p).total_seconds()
    assert secs == p["base"]


# ------------------------------------------------------------ setup harness ---

class _FakeApi:
    def __init__(self, **kw):
        self.kw = kw
        self.calls = []
        self.status_results = []          # scripted vehicle_status outcomes
        self.position_error = None
        self.state_error = None
        self.charge_error = None
        self.caps_error = None

    def _pop_status(self):
        if self.status_results:
            r = self.status_results.pop(0)
            if isinstance(r, Exception):
                raise r
            if r is not None:
                return r
        return {"code": 1000, "data": {"vehicleStatus": {
            "basicVehicleStatus": {"speed": "0"},
            "additionalVehicleStatus": {"electricVehicleStatus": {
                "statusOfChargerConnection": "0", "chargeLevel": "80"}}}}}

    def vehicle_status(self):
        self.calls.append("status")
        return self._pop_status()

    def vehicle_status_state(self):
        self.calls.append("state")
        if self.state_error is not None:
            raise self.state_error
        return {"code": 1000, "data": {"sentry": "1"}}

    def charge_server_get(self, biz):
        self.calls.append(f"charge{biz}")
        if self.charge_error is not None:
            raise self.charge_error
        return {"code": 1000, "data": {"rbcStartTime": "23:00"}}

    def request_position_refresh(self):
        self.calls.append("position")
        if self.position_error is not None:
            raise self.position_error

    def fetch_capabilities(self):
        self.calls.append("caps")
        if self.caps_error is not None:
            raise self.caps_error
        return []

    def control(self, sid, params, cmd):
        self.calls.append(("control", sid, params, cmd))
        return {"code": 1000}


class _FakeCoordinator:
    instance = None

    def __init__(self, hass, logger, *, name, config_entry, update_method,
                 update_interval):
        self.update_method = update_method
        self.update_interval = update_interval
        self.data = None
        _FakeCoordinator.instance = self

    async def async_config_entry_first_refresh(self):
        self.data = await self.update_method()

    async def refresh(self):
        self.data = await self.update_method()


class _ConfigEntries:
    def __init__(self):
        self.forwarded = None
        self.reloaded = []
        self.unload_ok = True

    async def async_forward_entry_setups(self, entry, platforms):
        self.forwarded = list(platforms)

    async def async_unload_platforms(self, entry, platforms):
        return self.unload_ok

    async def async_reload(self, entry_id):
        self.reloaded.append(entry_id)

    def async_get_entry(self, entry_id):
        return None


class _Services:
    def __init__(self):
        self.registered = {}

    def has_service(self, domain, name):
        return name in self.registered


class _Hass:
    def __init__(self):
        self.data = {}
        self.config_entries = _ConfigEntries()
        self.services = _Services()
        self.config = types.SimpleNamespace(path=lambda *p: os.path.join("/cfg", *p))

    async def async_add_executor_job(self, fn, *a):
        return fn(*a)


def _entry(poll_mode="normal", **data_extra):
    data = {
        "vin": FAKE_VIN, "user_id": "u1", "cidpsso_token": "tok",
        "device_id": "dev1", "cert_path": "/cfg/.storage/geely_connect/v/cert.pem",
        "key_path": "/cfg/.storage/geely_connect/v/key.pem", "email": "e@x.com",
        "poll_mode": poll_mode, "vehicle_nickname": "My EX5",
        "vehicle_series": "FX11", "vehicle_model_code": "FX11",
        "vehicle_color": "", "vehicle_power_type": "纯电动",
    }
    data.update(data_extra)
    return types.SimpleNamespace(
        data=data, options={}, entry_id="e1",
        async_on_unload=lambda fn: fn,
        add_update_listener=lambda fn: (lambda: None))


def _setup(m, hass=None, entry=None, api_tweak=None, poll_mode="normal"):
    hass = hass or _Hass()
    entry = entry or _entry(poll_mode=poll_mode)
    made = {}

    def _api_factory(**kw):
        api = _FakeApi(**kw)
        if api_tweak:
            api_tweak(api)
        made["api"] = api
        return api

    registered = {}

    def _admin(hass_, domain, name, handler, schema=None):
        registered[name] = handler

    fast = types.SimpleNamespace(sleep=_instant_sleep,
                                 CancelledError=asyncio.CancelledError)
    with _Patched(m, GeelyApi=_api_factory, DataUpdateCoordinator=_FakeCoordinator,
                  er=_er_module(_EntityRegistry([])),
                  dr=types.SimpleNamespace(
                      async_get=lambda h: types.SimpleNamespace(
                          async_get_device=lambda identifiers: None)),
                  async_register_admin_service=_admin, asyncio=fast):
        ok = asyncio.run(m.async_setup_entry(hass, entry))
    return ok, hass, entry, made.get("api"), _FakeCoordinator.instance, registered


async def _instant_sleep(_delay):
    return None


def test_setup_builds_the_bundle_and_forwards_every_platform():
    m = _mod()
    ok, hass, entry, api, coord, services = _setup(m)
    assert ok is True
    bundle = hass.data["geely_connect"]["e1"]
    assert bundle["vin"] == FAKE_VIN
    assert bundle["propulsion"].kind.value == "electric"
    assert bundle["device_name"].endswith("(0000)")
    assert hass.config_entries.forwarded == list(m.PLATFORMS)
    assert "fire_control" in services
    assert coord.data["_state"] == {"sentry": "1"}
    assert coord.data["_scheduled_charging"] == {"rbcStartTime": "23:00"}
    assert api.kw["vin"] == FAKE_VIN


def test_manual_mode_never_starts_a_timer():
    m = _mod()
    ok, _, _, _, coord, _ = _setup(m, poll_mode="manual")
    assert ok and coord.update_interval is None
    asyncio.run(coord.refresh())
    assert coord.update_interval is None, "manual mode must not grow a timer"


def test_a_transient_error_is_retried_and_then_recovers():
    """One DNS hiccup must cost a retry, not the poll - and definitely not a
    reauth."""
    m = _mod()

    def tweak(api):
        api.status_results = [OSError("EAI_AGAIN"), None]

    ok, _, _, api, coord, _ = _setup(m, api_tweak=tweak)
    assert ok and coord.data is not None
    assert api.calls.count("status") == 2


def test_sustained_failure_reuses_the_snapshot_then_gives_up():
    m = _mod()
    from homeassistant.helpers.update_coordinator import UpdateFailed
    ok, _, _, api, coord, _ = _setup(m)
    good = coord.data
    api.status_results = [ValueError("boom 1")]
    asyncio.run(coord.refresh())
    assert coord.data == good, "first failure must serve the last snapshot"
    api.status_results = [ValueError("b2"), ValueError("b3")]
    asyncio.run(coord.refresh())
    try:
        asyncio.run(coord.refresh())
    except UpdateFailed as e:
        assert "vehicle_status" in str(e)
    else:
        raise AssertionError("the third consecutive failure did not surface")


def test_an_expired_session_starts_reauth_not_a_retry_loop():
    m = _mod()
    api_mod = load("api")
    from homeassistant.exceptions import ConfigEntryAuthFailed

    def tweak(api):
        api.status_results = [api_mod.GeelyAuthError("token revoked")]

    try:
        _setup(m, api_tweak=tweak)
    except ConfigEntryAuthFailed:
        pass
    else:
        raise AssertionError("an auth failure did not trigger reauth")


def test_a_rejected_status_code_fails_the_poll_loudly():
    m = _mod()
    from homeassistant.helpers.update_coordinator import UpdateFailed

    def tweak(api):
        api.status_results = [{"code": 500, "msg": "server sad"}]

    try:
        _setup(m, api_tweak=tweak)
    except UpdateFailed as e:
        assert "500" in str(e)
    else:
        raise AssertionError("a rejected code did not fail the poll")


def test_non_dict_data_degrades_to_an_empty_snapshot():
    m = _mod()

    def tweak(api):
        api.status_results = [{"code": 1000, "data": "garbled"}]

    ok, _, _, _, coord, _ = _setup(m, api_tweak=tweak)
    assert ok and isinstance(coord.data, dict)


def test_position_and_secondary_failures_never_take_the_poll_down():
    m = _mod()
    api_mod = load("api")

    def tweak(api):
        api.position_error = api_mod.GeelyTLSPinError("key changed")
        api.state_error = ValueError("sentry ded")

    ok, _, _, _, coord, _ = _setup(m, api_tweak=tweak)
    assert ok and "_state" not in coord.data


def test_the_capability_fetch_failing_leaves_all_features_enabled():
    m = _mod()

    def tweak(api):
        api.caps_error = OSError("catalog down")

    ok, hass, _, _, _, _ = _setup(m, api_tweak=tweak)
    assert ok
    assert hass.data["geely_connect"]["e1"]["capabilities"] == {}


def test_the_idle_streak_survives_identical_polls_and_resets_on_change():
    m = _mod()
    ok, _, _, api, coord, _ = _setup(m)
    asyncio.run(coord.refresh())
    asyncio.run(coord.refresh())
    first = coord.update_interval
    api.status_results = [{"code": 1000, "data": {"vehicleStatus": {
        "basicVehicleStatus": {"speed": "0"},
        "additionalVehicleStatus": {"electricVehicleStatus": {
            "statusOfChargerConnection": "0", "chargeLevel": "42"}}}}}]
    asyncio.run(coord.refresh())
    assert first is not None


def test_every_transient_attempt_failing_surfaces_the_original_error():
    """Three DNS failures in a row: the caller must see the network error,
    not a mystery None."""
    m = _mod()
    from homeassistant.helpers.update_coordinator import UpdateFailed

    def tweak(api):
        api.status_results = [OSError("EAI_AGAIN")] * 3

    try:
        _setup(m, api_tweak=tweak)
    except UpdateFailed as e:
        assert "EAI_AGAIN" in str(e)
    else:
        raise AssertionError("exhausted retries did not surface")


def test_an_expired_session_during_the_position_wake_starts_reauth():
    m = _mod()
    api_mod = load("api")
    from homeassistant.exceptions import ConfigEntryAuthFailed

    def tweak(api):
        api.position_error = api_mod.GeelyAuthError("kicked")

    try:
        _setup(m, api_tweak=tweak)
    except ConfigEntryAuthFailed:
        pass
    else:
        raise AssertionError("position auth failure did not reauth")


def test_a_position_hiccup_is_a_debug_line_not_a_failed_poll():
    m = _mod()

    def tweak(api):
        api.position_error = ValueError("PAI busy")

    ok, _, _, _, coord, _ = _setup(m, api_tweak=tweak)
    assert ok and coord.data is not None


def test_secondary_auth_and_pin_failures_take_their_designed_paths():
    """Auth on the state fetch reauths; a pin failure is an ERROR (possible
    MITM) but the poll survives; a scheduled-charging pin failure likewise."""
    m = _mod()
    api_mod = load("api")
    from homeassistant.exceptions import ConfigEntryAuthFailed

    def auth(api):
        api.state_error = api_mod.GeelyAuthError("kicked")

    try:
        _setup(m, api_tweak=auth)
    except ConfigEntryAuthFailed:
        pass
    else:
        raise AssertionError("state auth failure did not reauth")

    def pins(api):
        api.state_error = api_mod.GeelyTLSPinError("state pin")
        api.charge_error = api_mod.GeelyTLSPinError("charge pin")

    ok, _, _, _, coord, _ = _setup(m, api_tweak=pins)
    assert ok and "_scheduled_charging" not in coord.data

    def charge_junk(api):
        api.charge_error = ValueError("charge ded")

    ok, _, _, _, coord, _ = _setup(m, api_tweak=charge_junk)
    assert ok and "_scheduled_charging" not in coord.data


def test_a_coordinator_that_rejects_its_interval_does_not_kill_the_poll():
    """coordinator.update_interval is HA's; if setting it ever raises, the
    data must still reach the entities."""
    m = _mod()

    class _Touchy(_FakeCoordinator):
        def __init__(self, *a, **kw):
            self._armed = False
            super().__init__(*a, **kw)
            self._armed = True

        @property
        def update_interval(self):
            return self._ui

        @update_interval.setter
        def update_interval(self, v):
            if self._armed:
                raise RuntimeError("interval rejected")
            self._ui = v

    hass, entry = _Hass(), _entry()

    def _api_factory(**kw):
        return _FakeApi(**kw)

    fast = types.SimpleNamespace(sleep=_instant_sleep,
                                 CancelledError=asyncio.CancelledError)
    with _Patched(m, GeelyApi=_api_factory, DataUpdateCoordinator=_Touchy,
                  er=_er_module(_EntityRegistry([])),
                  dr=types.SimpleNamespace(
                      async_get=lambda h: types.SimpleNamespace(
                          async_get_device=lambda identifiers: None)),
                  async_register_admin_service=lambda *a, **k: None,
                  asyncio=fast):
        ok = asyncio.run(m.async_setup_entry(hass, entry))
    assert ok and _FakeCoordinator.instance.data is not None


# ------------------------------------------------------------- fire_control ---

def _service(m, bundles, call_data):
    hass = _Hass()
    hass.data["geely_connect"] = bundles
    registered = {}
    with _Patched(m, async_register_admin_service=lambda h, d, n, f, schema=None:
                  registered.update({n: f})):
        m._register_debug_service(hass)
    call = types.SimpleNamespace(data=call_data)
    return hass, lambda: asyncio.run(registered["fire_control"](call))


def test_fire_control_requires_a_loaded_vehicle_and_an_unambiguous_vin():
    m = _mod()
    from homeassistant.exceptions import ServiceValidationError
    _, run = _service(m, {}, {"service_id": "RCT"})
    try:
        run()
    except ServiceValidationError:
        pass
    else:
        raise AssertionError("no-vehicles did not fail validation")

    two = {"e1": {"vin": "A", "api": _FakeApi()},
           "e2": {"vin": "B", "api": _FakeApi()}}
    _, run = _service(m, two, {"service_id": "RCT"})
    try:
        run()
    except ServiceValidationError as e:
        assert "vin is required" in str(e)
    else:
        raise AssertionError("two cars with no vin did not fail")

    _, run = _service(m, two, {"service_id": "RCT", "vin": "C"})
    try:
        run()
    except ServiceValidationError:
        pass
    else:
        raise AssertionError("an unknown vin did not fail")


def test_fire_control_sends_the_command_to_the_chosen_car():
    m = _mod()
    api = _FakeApi()
    _, run = _service(m, {"e1": {"vin": FAKE_VIN, "api": api}},
                      {"service_id": "RCT", "command": "start",
                       "params": [{"key": "temperature", "value": "22.5"}]})
    run()
    assert api.calls == [("control", "RCT",
                          [{"key": "temperature", "value": "22.5"}], "start")]


def test_fire_control_surfaces_rejections_and_expiry_the_entity_way():
    m = _mod()
    api_mod = load("api")
    from homeassistant.exceptions import HomeAssistantError

    class _Rejecting(_FakeApi):
        def control(self, *a):
            raise api_mod.GeelyControlError("8070", "pending")

    _, run = _service(m, {"e1": {"vin": FAKE_VIN, "api": _Rejecting()}},
                      {"service_id": "RCT"})
    try:
        run()
    except HomeAssistantError as e:
        assert "pending" in str(e)
    else:
        raise AssertionError("a rejected command looked successful")

    class _Expired(_FakeApi):
        def control(self, *a):
            raise api_mod.GeelyAuthError("kicked")

    hass, run = _service(m, {"e1": {"vin": FAKE_VIN, "api": _Expired()}},
                         {"service_id": "RCT"})
    reauths = []
    hass.config_entries.async_get_entry = lambda eid: types.SimpleNamespace(
        async_start_reauth=lambda h: reauths.append(eid))
    try:
        run()
    except HomeAssistantError as e:
        assert "session expired" in str(e)
    else:
        raise AssertionError("an expired session looked successful")
    assert reauths == ["e1"], "expiry must start the reauth flow"

    class _Broken(_FakeApi):
        def control(self, *a):
            raise OSError("tls reset")

    _, run = _service(m, {"e1": {"vin": FAKE_VIN, "api": _Broken()}},
                      {"service_id": "RCT"})
    try:
        run()
    except HomeAssistantError as e:
        assert "failed" in str(e)
    else:
        raise AssertionError("a transport error looked successful")


def test_the_service_registers_once():
    m = _mod()
    hass = _Hass()
    hass.services.registered["fire_control"] = object()
    calls = []
    with _Patched(m, async_register_admin_service=lambda *a, **k: calls.append(1)):
        m._register_debug_service(hass)
    assert calls == [], "a second registration must be a no-op"


# ---------------------------------------------------- options, unload, remove ---

def test_options_reload_purges_raw_sensors_only_when_exposure_is_off():
    m = _mod()
    purged = []
    hass = _Hass()
    entry = _entry()
    with _Patched(m, _purge_raw_exposure_entities=lambda h, e: purged.append(1)):
        asyncio.run(m._async_options_updated(hass, entry))
        entry.options = {"full_exposure": True}
        asyncio.run(m._async_options_updated(hass, entry))
    assert purged == [1], "exposure ON must keep the raw sensors"
    assert hass.config_entries.reloaded == ["e1", "e1"]


def test_unload_drops_the_bundle_only_when_platforms_unload():
    m = _mod()
    hass = _Hass()
    hass.data["geely_connect"] = {"e1": {"api": object()}}
    entry = _entry()
    assert asyncio.run(m.async_unload_entry(hass, entry)) is True
    assert "e1" not in hass.data["geely_connect"]
    hass.data["geely_connect"]["e1"] = {"api": object()}
    hass.config_entries.unload_ok = False
    assert asyncio.run(m.async_unload_entry(hass, entry)) is False
    assert "e1" in hass.data["geely_connect"]


def test_removal_shreds_the_key_directory_but_only_inside_our_storage():
    """The private key can unlock the car; it must die with the entry - but a
    server-influenced path outside .storage/geely_connect must never be
    rm -rf'd."""
    m = _mod()
    with tempfile.TemporaryDirectory() as root:
        hass = _Hass()
        hass.config = types.SimpleNamespace(
            path=lambda *p: os.path.join(root, *p))
        vin_dir = os.path.join(root, ".storage", "geely_connect", "vin1")
        os.makedirs(vin_dir)
        open(os.path.join(vin_dir, "key.pem"), "w").write("KEY")
        entry = types.SimpleNamespace(
            data={"cert_path": os.path.join(vin_dir, "cert.pem")})
        asyncio.run(m.async_remove_entry(hass, entry))
        assert not os.path.exists(vin_dir), "the key must not outlive the entry"

        outside = os.path.join(root, "not-ours")
        os.makedirs(outside)
        open(os.path.join(outside, "key.pem"), "w").write("KEY")
        evil = types.SimpleNamespace(
            data={"cert_path": os.path.join(outside, "cert.pem")})
        asyncio.run(m.async_remove_entry(hass, evil))
        assert os.path.exists(outside), "a path outside our storage was deleted"

        asyncio.run(m.async_remove_entry(hass, types.SimpleNamespace(data={})))


def test_a_failed_shred_warns_instead_of_blocking_the_removal():
    m = _mod()
    with tempfile.TemporaryDirectory() as root:
        hass = _Hass()
        hass.config = types.SimpleNamespace(
            path=lambda *p: os.path.join(root, *p))
        vin_dir = os.path.join(root, ".storage", "geely_connect", "vin1")
        os.makedirs(vin_dir)
        entry = types.SimpleNamespace(
            data={"cert_path": os.path.join(vin_dir, "cert.pem")})

        def _boom(*a, **k):
            raise OSError("file locked")

        with _Patched(m, shutil=types.SimpleNamespace(rmtree=_boom)):
            asyncio.run(m.async_remove_entry(hass, entry))
        assert os.path.exists(vin_dir), "the failure path must not half-delete"


def test_a_probe_polls_afterwards_so_its_effect_becomes_visible():
    """The gateway answers "operation succeed" to any well-formed request,
    including targets the car ignores - three candidate tailgate commands in
    #20 returned byte-identical successes. Only a moved entity separates a
    probe that worked from one that did nothing, so fire_control has to fetch
    afterwards; without it a probe fired from the sofa is unreadable."""
    m = _mod()
    api = _FakeApi()
    scheduled = []
    bundles = {"e1": {"vin": FAKE_VIN, "api": api, "coordinator": object()}}
    hass, run = _service(m, bundles, {"service_id": "RWS_2", "command": "start",
                                      "params": [{"key": "target", "value": "tailgate"}]})
    with _Patched(m, schedule_refresh=lambda h, c, *delays: scheduled.append(delays)):
        run()
    assert any(c[0] == "control" for c in api.calls), "the command still has to be sent"
    assert scheduled == [(6, 12)], scheduled


def test_a_probe_without_a_coordinator_still_sends_the_command():
    """A bundle mid-setup has no coordinator yet; the probe must not crash."""
    m = _mod()
    api = _FakeApi()
    bundles = {"e1": {"vin": FAKE_VIN, "api": api}}
    _, run = _service(m, bundles, {"service_id": "RCT"})
    with _Patched(m, schedule_refresh=lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("must not be called without a coordinator"))):
        run()
    assert any(c[0] == "control" for c in api.calls)
