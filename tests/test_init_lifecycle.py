"""Setup, polling, services and teardown in __init__.py.

The update closure here is the only code between the cloud and every entity,
and the teardown path is what keeps a removed car's private key from living
on inside backups - both were almost entirely unexercised before this file.
"""
import ast
import asyncio
import functools
import importlib
import importlib.util
import json
import logging
import os
import sys
import tempfile
import time
import traceback
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


class _IssueRegistry:
    """Stands in for homeassistant.helpers.issue_registry: records the Repairs
    issues the integration raises and clears (#84)."""

    def __init__(self):
        self.created = []
        self.deleted = []
        self.IssueSeverity = types.SimpleNamespace(WARNING="warning", ERROR="error")

    def async_create_issue(self, hass, domain, issue_id, **kw):
        self.created.append((domain, issue_id, kw))

    def async_delete_issue(self, hass, domain, issue_id):
        self.deleted.append((domain, issue_id))


def _ir_module():
    return _IssueRegistry()


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


def test_the_e2s_plug_sensor_survives_the_purge_of_its_retired_namesake():
    """Plugged In (reported) is back for the E2-scheme cars (#129) under a new
    key. The retired `is_plugged_in` is purged by substring at the start of
    every setup, before the platforms build anything, so a key containing it
    would have the sensor deleted and registered again on every start. The
    key is the one binary_sensor builds the sensor with."""
    m = _mod()
    key = load("binary_sensor")._PLUGGED_IN_REPORTED[0]
    mk = lambda eid, uid: types.SimpleNamespace(
        entity_id=eid, unique_id=uid, platform="geely_connect")
    reg = _EntityRegistry([
        mk("binary_sensor.retired", f"geely_{FAKE_VIN}_bs_is_plugged_in"),
        mk("binary_sensor.e2", f"geely_{FAKE_VIN}_bs_{key}"),
    ])
    with _Patched(m, er=_er_module(reg)):
        n = m._purge_obsolete_entities(object(), types.SimpleNamespace(entry_id="e1"))
    assert n == 1 and reg.removed == ["binary_sensor.retired"], reg.removed


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


def test_the_device_lookup_prefers_the_entry_scoped_method_when_it_exists():
    """Home Assistant 2027.8 removes the registry-wide async_get_device, and
    the replacement takes the owning entry because identifiers are no longer
    unique across entries. It is absent on older cores this integration still
    supports, so both lookups have to work - and the new one has to be called
    with the entry id, not with the same argument shape as the old one."""
    m = _mod()
    entry = types.SimpleNamespace(entry_id="e1", data={
        "vin": FAKE_VIN, "vehicle_nickname": "Rocket",
        "vehicle_model_code": "E245-J1"})

    class _Dev:
        def __init__(self):
            self.id, self.name, self.name_by_user = "d1", "Geely EX5 (0000)", None

    class _NewDevReg:
        """A core that has made the switch: the old method is gone entirely."""

        def __init__(self):
            self.asked, self.updates = [], []

        def async_get_device_by_identifier(self, identifier, config_entry_id):
            self.asked.append((identifier, config_entry_id))
            return _Dev()

        def async_update_device(self, did, name=None):
            self.updates.append((did, name))

    new = _NewDevReg()
    with _Patched(m, dr=types.SimpleNamespace(async_get=lambda h: new)):
        m._refresh_device_name(object(), entry)
    assert new.asked == [(("geely_connect", FAKE_VIN), "e1")]
    assert new.updates == [("d1", "Rocket EX5 (0000)")]


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


def _unserved_registry():
    mk = lambda eid, uid: types.SimpleNamespace(entity_id=eid, unique_id=uid)
    return [mk("binary_sensor.btm", f"geely_{FAKE_VIN}_bs_battery_temp_maintenance"),
            mk("switch.sched", f"geely_{FAKE_VIN}_sw_scheduled_charging"),
            mk("time.start", f"geely_{FAKE_VIN}_time_scheduled_charging_start"),
            mk("time.end", f"geely_{FAKE_VIN}_time_scheduled_charging_end"),
            mk("switch.pc", f"geely_{FAKE_VIN}_sw_parking_comfort"),
            mk("switch.charging", f"geely_{FAKE_VIN}_sw_charging"),
            mk("sensor.keep", f"geely_{FAKE_VIN}_battery")]


def test_entities_the_api_cannot_serve_are_cleared_from_the_registry():
    """An earlier version built them on a new-platform car, where they could
    only read unknown and fail when pressed. Left alone they would sit on the
    device page as "no longer provided" (#88). The Charging switch stays: the
    state it reads is real even where its command is not mapped."""
    m = _mod()
    reg = _EntityRegistry([])
    fake_er = _er_module(reg, per_entry=_unserved_registry())
    api = types.SimpleNamespace(serves_state=False, serves_charge_server=False,
                                can_send=lambda sid: sid not in ("RSM", "RCS"))
    with _Patched(m, er=fake_er):
        n = m._purge_unserved_entities(object(), types.SimpleNamespace(entry_id="e1"), api)
    assert n == 5
    assert sorted(reg.removed) == ["binary_sensor.btm", "switch.pc", "switch.sched",
                                   "time.end", "time.start"], reg.removed


def test_a_1_0_car_loses_the_scheduled_trip_sensors_and_nothing_else():
    """Its catalogue rules slot 4 out (#72), so the three sensors are not
    built - and the Battery Temperature Maintenance an earlier version built
    there from a status flag must not linger as "no longer provided"."""
    m = _mod()
    reg = _EntityRegistry([])
    with _Patched(m, er=_er_module(reg, per_entry=_unserved_registry())):
        n = m._purge_unserved_entities(object(), types.SimpleNamespace(entry_id="e1"),
                                       object(), {"scheduled_trip.enabled": False})
    assert n == 1 and reg.removed == ["binary_sensor.btm"], reg.removed
    for caps in ({}, {"scheduled_trip.enabled": True}, None):
        reg = _EntityRegistry([])
        with _Patched(m, er=_er_module(reg, per_entry=_unserved_registry())):
            n = m._purge_unserved_entities(object(), types.SimpleNamespace(entry_id="e1"),
                                           object(), caps)
        assert n == 0, (caps, reg.removed)


def test_an_api_that_serves_everything_loses_nothing():
    """These are live entities on every old-platform car - the reason they are
    cleared per entry and never through the global obsolete list. A new-
    platform car without an x-vin keeps Parking Comfort: the legacy route it
    commands through takes RSM."""
    m = _mod()
    for api, kept in ((object(), 7),
                      (types.SimpleNamespace(serves_state=False, serves_charge_server=False,
                                             can_send=lambda sid: True), 3)):
        reg = _EntityRegistry([])
        with _Patched(m, er=_er_module(reg, per_entry=_unserved_registry())):
            m._purge_unserved_entities(object(), types.SimpleNamespace(entry_id="e1"), api)
        assert len(_unserved_registry()) - len(reg.removed) == kept, reg.removed
    for pattern in m._OBSOLETE_UNIQUE_ID_PATTERNS:
        for e in _unserved_registry():
            assert pattern not in e.unique_id, (pattern, e.unique_id)


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

    def without_secrets(self, text):
        # The coordinator quotes every error through the client (the real
        # one is GeelyApi.without_secrets); this fake holds no secrets.
        return str(text)

    def vehicle_status_state(self):
        self.calls.append("state")
        if self.state_error is not None:
            raise self.state_error
        return {"code": 1000, "data": {"sentry": "1"}}

    def charge_server_get(self, biz):
        self.calls.append(f"charge{biz}")
        if self.charge_error is not None:
            raise self.charge_error
        if biz == "4":
            # Slot 4 as a real EX5 answered it with a trip set (#4).
            return {"code": 1000, "data": getattr(self, "slot4", {
                "btActive": "false", "ac": "true", "bizType": 4,
                "scheduledTime": "1787229000000", "vet": "", "btTempActive": "true",
                "bw": "1", "bwl": "1", "vst": "", "id": 282456})}
        return {"code": 1000, "data": {"rbcStartTime": "23:00"}}

    def scheduled_trip_set(self, **kw):
        self.calls.append(("trip", kw))
        return {"code": 1000, "success": True}

    def request_position_refresh(self):
        self.calls.append("position")
        if self.position_error is not None:
            raise self.position_error

    def fetch_capabilities(self):
        self.calls.append("caps")
        if self.caps_error is not None:
            raise self.caps_error
        return []

    def control(self, sid, params, cmd, **shape):
        self.calls.append(("control", sid, params, cmd, *sorted(shape)))
        return {"code": 1000}

    def rapid_climate(self, **kw):
        self.calls.append(("rapid", kw))
        return {"code": "1000", "data": {"param": {"paa.ac": "true"}}}


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
        self.entries = []

    def async_entries(self, domain=None):
        return list(self.entries)

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


def _setup(m, hass=None, entry=None, api_tweak=None, poll_mode="normal", ir=None):
    hass = hass or _Hass()
    entry = entry or _entry(poll_mode=poll_mode)
    ir = ir if ir is not None else _ir_module()
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
                  er=_er_module(_EntityRegistry([])), ir=ir,
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
    assert coord.data["_scheduled_trip"]["bw"] == "1"
    assert api.kw["vin"] == FAKE_VIN
    # A shipped region keeps refresh_jwt's own choice of login-code host.
    assert api.kw["control_host"] == "apis.ecloudeu.com"
    assert api.kw["mint_host"] is None
    # No session kept yet: the client starts from the cidpsso exchange.
    assert api.kw["access_token"] is None and api.kw["refresh_token"] is None


def test_a_restart_hands_the_kept_session_pair_to_the_client():
    """The pair an earlier poll kept goes back into the client, so a restart
    renews with the refresh token instead of spending cidpsso (#101)."""
    m = _mod()
    ok, hass, entry, api, coord, _ = _setup(
        m, entry=_entry(access_token="jwt-kept", refresh_token="rt-kept"))
    assert ok is True
    assert api.kw["access_token"] == "jwt-kept"
    assert api.kw["refresh_token"] == "rt-kept"


def _session_writes(entry_extra, takes):
    """Set up with a client that hands out `takes` one poll at a time, and
    collect the entry writes that follow."""
    m = _mod()
    queue = list(takes)
    hass = _Hass()
    writes = []

    def _update(entry, data=None, **kw):
        writes.append(dict(data))
        entry.data = data

    hass.config_entries.async_update_entry = _update

    def _tweak(api):
        api.take_renewed_jwt = lambda: queue.pop(0) if queue else None

    ok, hass, entry, api, coord, _ = _setup(
        m, hass=hass, entry=_entry(**entry_extra), api_tweak=_tweak)
    assert ok is True
    return m, hass, entry, coord, writes


def test_a_renewed_session_pair_is_kept_in_the_entry_only_when_it_changes():
    m, hass, entry, coord, writes = _session_writes(
        {}, [("jwt-1", "rt-1"), None, ("jwt-1", "rt-1"), ("jwt-2", "rt-1")])
    # The first refresh ran inside setup, before the bundle existed.
    assert writes == [dict(entry.data)]
    assert (entry.data["access_token"], entry.data["refresh_token"]) == (
        "jwt-1", "rt-1")
    bundle = hass.data["geely_connect"]["e1"]
    asyncio.run(coord.refresh())                 # no renewal
    asyncio.run(coord.refresh())                 # the same pair handed back
    assert len(writes) == 1, "an unchanged pair must not rewrite the entry"
    assert "_skip_reload_once" not in bundle
    asyncio.run(coord.refresh())
    assert len(writes) == 2
    assert entry.data["access_token"] == "jwt-2"
    assert bundle.get("_skip_reload_once") is True, \
        "the write flags itself so the update listener skips the reload"


def _keepalive_setup(applies=True, keep=None, takes=()):
    """Set up with the timer helper recorded, not run. Returns the setup and
    what was scheduled: (hass, action, interval, name) per call."""
    m = _mod()
    hass = _Hass()
    writes, unloads, timers, unsubbed = [], [], [], []

    def _update(entry, data=None, **kw):
        writes.append(dict(data))
        entry.data = data

    hass.config_entries.async_update_entry = _update
    entry = _entry()
    entry.async_on_unload = unloads.append
    hass.config_entries.async_get_entry = (
        lambda entry_id: entry if entry_id == entry.entry_id else None)
    queue = list(takes)

    def _tweak(api):
        api.kept = 0

        def _keep():
            api.kept += 1
            if keep is not None:
                keep(hass, api)

        if applies is not None:
            api.session_keepalive_applies = lambda: applies
        api.keep_session_alive = _keep
        api.take_renewed_jwt = lambda: queue.pop(0) if queue else None

    def _track(hass_, action, interval, *, name=None):
        timers.append((hass_, action, interval, name))
        return lambda: unsubbed.append(name)

    with _Patched(m, async_track_time_interval=_track):
        ok, hass, entry, api, coord, _ = _setup(
            m, hass=hass, entry=entry, api_tweak=_tweak)
    assert ok is True
    return types.SimpleNamespace(m=m, hass=hass, entry=entry, api=api,
                                 coord=coord, writes=writes, unloads=unloads,
                                 timers=timers, unsubbed=unsubbed)


def test_the_session_keepalive_runs_while_a_refresh_token_is_held():
    """Super Eco's 2 h cap and Manual mode renew only after the JWT expired,
    which the PUT refuses (#101): a gateway-only timer keeps it alive."""
    s = _keepalive_setup(takes=[None, ("jwt-k", "rt-k")])
    assert len(s.timers) == 1
    hass_, action, interval, name = s.timers[0]
    assert hass_ is s.hass and interval.total_seconds() == 50 * 60
    assert name and "keepalive" in name
    calls, writes = list(s.api.calls), len(s.writes)
    data = s.coord.data
    asyncio.run(action(None))
    assert s.api.kept == 1
    assert s.api.calls == calls, "the keepalive must never reach the car"
    assert s.coord.data is data, "nor run a coordinator refresh"
    assert len(s.writes) == writes + 1
    assert (s.entry.data["access_token"], s.entry.data["refresh_token"]) == (
        "jwt-k", "rt-k")
    assert s.hass.data["geely_connect"]["e1"].get("_skip_reload_once") is True


def test_the_session_keepalive_is_cancelled_on_unload():
    s = _keepalive_setup()
    for cb in s.unloads:
        cb()
    assert s.unsubbed == ["geely_connect session keepalive"]


def test_the_keepalive_is_registered_on_eu_and_na_even_without_a_token_yet():
    """Whether a refresh token is held is the tick's question
    (GeelyApi.keep_session_alive), so a session that gains one later is
    kept alive too. A tick with nothing renewed writes nothing."""
    s = _keepalive_setup(takes=[])
    assert len(s.timers) == 1
    asyncio.run(s.timers[0][1](None))
    assert s.api.kept == 1 and s.writes == []


def test_no_keepalive_on_apac_or_the_geely_em_platform():
    # APAC answers False (GeelyApi.session_keepalive_applies); a Geely EM
    # adapter has no such method.
    for applies in (False, None):
        s = _keepalive_setup(applies=applies)
        assert s.timers == [], applies


def test_a_tick_for_an_entry_that_is_gone_writes_nothing():
    """A tick waiting on its executor job while the entry is unloaded or
    removed must not write into it; one that starts after does nothing."""
    def removed(hass, api):
        hass.config_entries.async_get_entry = lambda entry_id: None

    def replaced(hass, api):
        hass.data["geely_connect"]["e1"]["api"] = object()

    for gone in (removed, replaced):
        s = _keepalive_setup(keep=gone, takes=[None, ("jwt-k", "rt-k")])
        asyncio.run(s.timers[0][1](None))
        assert s.api.kept == 1 and s.writes == [], gone.__name__
        asyncio.run(s.timers[0][1](None))
        assert s.api.kept == 1, "no renewal for an entry that is gone"


def test_a_failing_keepalive_never_escalates():
    """A failure in the timer is logged and left for the next poll, which
    raises any re-authentication; the timer itself never raises."""
    from homeassistant.exceptions import ConfigEntryAuthFailed
    api_mod = load("api")

    for error in (api_mod.GeelyAuthError("cidpsso token rejected"),
                  api_mod.GeelyTLSPinError("pin mismatch"),
                  TimeoutError("timed out"),
                  RuntimeError("session refresh failed")):
        def _fail(hass, api, error=error):
            raise error

        s = _keepalive_setup(keep=_fail)
        _, action, _, _ = s.timers[0]
        try:
            asyncio.run(action(None))
        except ConfigEntryAuthFailed:
            raise AssertionError("the timer asked for re-auth itself")
        assert s.api.kept == 1 and s.writes == [], error


def test_a_session_without_a_refresh_token_clears_only_a_kept_pair():
    _, _, entry, _, writes = _session_writes({}, [("", "")])
    assert writes == [] and "access_token" not in entry.data
    _, _, entry, _, writes = _session_writes(
        {"access_token": "jwt-old", "refresh_token": "rt-old"}, [("", "")])
    assert len(writes) == 1
    assert (entry.data["access_token"], entry.data["refresh_token"]) == ("", "")


def _uae_hass(keys):
    """A hass whose config dir is a temp dir, with the UAE keys file when
    given (#105)."""
    import json
    import tempfile

    root = tempfile.mkdtemp()
    if keys is not None:
        with open(os.path.join(root, "geely_region_keys.json"), "w") as f:
            f.write(json.dumps(keys))
    hass = _Hass()
    hass.config = types.SimpleNamespace(path=lambda *p: os.path.join(root, *p))
    return hass


_UAE_KEYS = {"region": "AE", "app_id": "FAKEAPPID01",
             "app_secret": "fakesecret0123456789abcdef"}


def test_a_uae_entry_reads_its_keys_from_the_file_at_every_start():
    """The entry stores the area and nothing of the owner's file, so setup
    reads the file again and builds the client on the UAE servers, signed with
    its app id and secret, minting the login code at the area's own host."""
    m = _mod()
    const = load("const")
    hosts = const.LOCAL_KEY_REGIONS["AE"]
    ok, hass, entry, api, coord, _ = _setup(
        m, hass=_uae_hass(_UAE_KEYS), entry=_entry(region="AE"))
    assert ok is True
    assert api.kw["app_id"] == "FAKEAPPID01"
    assert api.kw["app_secret"] == "fakesecret0123456789abcdef"
    assert api.kw["control_host"] == hosts["control_host"]
    assert api.kw["mint_host"] == hosts["mint_host"]
    assert "FAKEAPPID01" not in repr(entry.data)
    # The area is read case-insensitively from an entry, as it is stored.
    ok, hass, entry, api, coord, _ = _setup(
        m, hass=_uae_hass(_UAE_KEYS), entry=_entry(region="ae"))
    assert ok is True and api.kw["control_host"] == hosts["control_host"]


def test_a_uae_entry_without_its_keys_waits_instead_of_signing_for_the_eu():
    """Missing or invalid keys make the entry wait, with a message naming the
    file and never a value, rather than build a client for another area."""
    m = _mod()
    from homeassistant.exceptions import ConfigEntryNotReady

    for keys in (None, dict(_UAE_KEYS, region="SA")):
        built = []
        try:
            _setup(m, hass=_uae_hass(keys), entry=_entry(region="AE"),
                   api_tweak=built.append)
        except ConfigEntryNotReady as e:
            assert "geely_region_keys.json" in str(e), e
            assert "fakesecret" not in str(e) and "FAKEAPPID01" not in str(e), e
        else:
            raise AssertionError("a UAE entry started without its keys")
        assert not built, "a client was built without the area's keys"


def _without_secondary(api):
    api.serves_state = False
    api.serves_charge_server = False


def test_an_api_without_the_secondary_endpoints_is_not_asked_for_them():
    """The new platform raised on both, on every poll that fetched them - which
    is every poll while the car charges (#88)."""
    m = _mod()
    ok, hass, _, api, coord, _ = _setup(m, api_tweak=_without_secondary)
    hass.data["geely_connect"]["e1"]["poll_state"]["force_secondary"] = True
    asyncio.run(coord.refresh())
    assert ok and "status" in api.calls
    assert "state" not in api.calls and "charge6" not in api.calls, api.calls
    assert "charge4" not in api.calls, api.calls
    assert "_state" not in coord.data and "_scheduled_charging" not in coord.data
    assert "_scheduled_trip" not in coord.data


def test_a_session_that_stopped_reading_the_schedules_keeps_their_entities():
    """After a refusal the poll stops reading the schedules, but the entities
    stay: setup purges by serves_charge_server, which a refusal leaves alone,
    so a 079001 at startup does not take their names and areas (#114)."""
    m = _mod()

    def refused(api):
        api.reads_charge_server = False
    ok, hass, _, api, coord, _ = _setup(m, api_tweak=refused)
    hass.data["geely_connect"]["e1"]["poll_state"]["force_secondary"] = True
    asyncio.run(coord.refresh())
    assert ok and "status" in api.calls
    assert "charge6" not in api.calls and "charge4" not in api.calls, api.calls
    assert "_scheduled_charging" not in coord.data
    reg = _EntityRegistry([])
    with _Patched(m, er=_er_module(reg, per_entry=_unserved_registry())):
        m._purge_unserved_entities(
            object(), types.SimpleNamespace(entry_id="e1"),
            types.SimpleNamespace(serves_charge_server=True,
                                  reads_charge_server=False))
    assert not any(r.startswith(("time.", "switch.sched")) for r in reg.removed), \
        reg.removed


def test_a_refresh_press_is_spent_where_there_is_no_state_block_to_wait_for():
    """The flag waits for `_state` so a failed fetch does not swallow the press.
    On an api with no state block that wait never ended, and the flag also wakes
    the car for a position fix - so one press woke it on every poll after (#88).
    Where the block exists and its fetch fails, the press still waits."""
    m = _mod()
    ok, hass, _, api, coord, _ = _setup(m, api_tweak=_without_secondary)
    poll = hass.data["geely_connect"]["e1"]["poll_state"]
    poll["force_secondary"] = True
    before = api.calls.count("position")
    asyncio.run(coord.refresh())
    assert api.calls.count("position") == before + 1, "the press must still wake the car once"
    assert "force_secondary" not in poll, "the press was never spent"

    def failing_state(api_):
        api_.state_error = ValueError("gateway said no")

    ok, hass, _, api, coord, _ = _setup(m, api_tweak=failing_state)
    poll = hass.data["geely_connect"]["e1"]["poll_state"]
    poll["force_secondary"] = True
    asyncio.run(coord.refresh())
    assert poll.get("force_secondary") is True, "a failed state fetch must keep the press"


def test_a_press_that_waits_wakes_the_car_once_not_on_every_poll():
    """A press waits for the state block, so a failed fetch does not swallow
    it - and each poll it waited through used to wake the car again for a
    position fix. Every wake spends the 12 V battery of a parked car (#95).
    One press, one wake; the state block is still retried each poll, and the
    next press may wake the car once more."""
    m = _mod()

    def failing_state(api_):
        api_.state_error = ValueError("gateway said no")

    ok, hass, _, api, coord, _ = _setup(m, api_tweak=failing_state)
    poll = hass.data["geely_connect"]["e1"]["poll_state"]
    poll["force_secondary"] = True
    before = (api.calls.count("position"), api.calls.count("state"))
    for _ in range(4):
        asyncio.run(coord.refresh())
    assert api.calls.count("position") == before[0] + 1, api.calls
    assert api.calls.count("state") == before[1] + 4, "the state block must still be retried"
    assert poll.get("force_secondary") is True
    poll.pop("force_woke")                     # what a second press does
    asyncio.run(coord.refresh())
    assert api.calls.count("position") == before[0] + 2, api.calls
    api.state_error = None                     # the block arrives: press spent
    asyncio.run(coord.refresh())
    assert "force_secondary" not in poll and "force_woke" not in poll, poll


def test_a_press_after_a_good_read_still_waits_for_a_read_of_its_own():
    """The last block is carried into a poll's data before the fetch, so once
    the car had answered once, a press whose own fetch failed was spent on the
    old block - the swallowed press the wait is there to prevent. Whether the
    fetch raised or the server answered with an error, the press waits."""
    m = _mod()
    ok, hass, _, api, coord, _ = _setup(m)          # setup read the block
    poll = hass.data["geely_connect"]["e1"]["poll_state"]
    wakes = api.calls.count("position")
    api.state_error = ValueError("gateway said no")
    poll["force_secondary"] = True
    asyncio.run(coord.refresh())
    assert poll.get("force_secondary") is True, "spent on the carried-forward block"
    assert coord.data["_state"] == {"sentry": "1"}, "the last block is still shown"
    api.state_error = None
    plain = api.vehicle_status_state
    api.vehicle_status_state = lambda: {"code": 5000, "msg": "busy"}
    asyncio.run(coord.refresh())
    assert poll.get("force_secondary") is True, "spent by an error answer"
    api.vehicle_status_state = plain
    asyncio.run(coord.refresh())
    assert "force_secondary" not in poll and "force_woke" not in poll, poll
    assert api.calls.count("position") == wakes + 1, "one press, one wake"


def test_a_switch_press_reads_the_state_block_without_waking_the_car():
    """A Parking Comfort press sets state_until (switch.py), because the block
    its state lives in is read only every few polls. Every poll before that
    time reads it, whatever the cycle counter says - a read, never a position
    wake - and once it has passed, the counter decides again."""
    m = _mod()
    ok, hass, _, api, coord, _ = _setup(m)          # cycle 1 read the block
    poll = hass.data["geely_connect"]["e1"]["poll_state"]
    state, wakes, trips = (api.calls.count("state"), api.calls.count("position"),
                           api.calls.count("charge4"))
    poll["state_until"] = time.monotonic() - 1      # an earlier press, spent
    asyncio.run(coord.refresh())                    # cycle 2 of 4
    assert api.calls.count("state") == state, "the counter decides again"
    poll["state_until"] = time.monotonic() + 60     # a press just now
    asyncio.run(coord.refresh())                    # cycle 3
    asyncio.run(coord.refresh())                    # cycle 4
    assert api.calls.count("state") == state + 2, api.calls
    assert api.calls.count("position") == wakes, "a read must not wake the car"
    assert api.calls.count("charge4") == trips, "the block, not the slot-4 trip"


def test_a_refresh_press_during_a_poll_waits_for_one_that_saw_it():
    """A poll already running when Refresh Data is pressed read `forced` as
    False, so it must not spend the press: that press would get neither its
    position fix nor its slot-4 read. A switch's state_until makes such polls
    run the secondary block far more often than the cycle counter alone."""
    m = _mod()
    ok, hass, _, api, coord, _ = _setup(m)          # cycle 1
    poll = hass.data["geely_connect"]["e1"]["poll_state"]
    poll["state_until"] = time.monotonic() + 60     # a switch press just now
    plain = api.vehicle_status_state

    def pressed_meanwhile():
        poll["force_secondary"] = True              # Refresh, mid-poll
        return plain()

    api.vehicle_status_state = pressed_meanwhile
    asyncio.run(coord.refresh())                    # cycle 2, already running
    assert poll.get("force_secondary") is True, "spent by a poll that never saw it"
    api.vehicle_status_state = plain
    wakes, trips = api.calls.count("position"), api.calls.count("charge4")
    asyncio.run(coord.refresh())                    # cycle 3 sees the press
    assert api.calls.count("position") == wakes + 1, api.calls
    assert api.calls.count("charge4") == trips + 1, api.calls
    assert "force_secondary" not in poll and "force_woke" not in poll, poll


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


def test_an_entry_back_from_the_new_platform_asks_for_reauth_not_a_keyerror():
    """#81: the new platform drops the legacy credentials, and an entry that
    came back without them used to die on `KeyError: 'device_id'` inside
    async_setup_entry - a traceback against an entry that would never load and
    carried no button. The owner who hit it got out by restoring the whole
    instance from a backup. Re-authentication is the remedy, so say so."""
    m = _mod()
    from homeassistant.exceptions import ConfigEntryAuthFailed

    for missing in ("device_id", "cert_path", "key_path", "cidpsso_token"):
        entry = _entry()
        del entry.data[missing]
        try:
            _setup(m, entry=entry)
        except ConfigEntryAuthFailed as e:
            assert missing in str(e), (
                f"the message must name what is missing, got {e!r}")
        except KeyError:  # pragma: no cover - the bug this test exists for
            raise AssertionError(
                f"a missing {missing} still raises KeyError from setup")
        else:
            raise AssertionError(f"a missing {missing} set up anyway")


def test_a_legacy_entry_clears_the_new_platforms_token_issue():
    """An entry reconfigured back from Geely EM keeps its entry id, and the
    x-vin Repairs issue it raised there is keyed on that id. Setting up on
    this backend clears it - also when the entry still has to re-authenticate
    first - or its owner is told to paste a token the old login never reads
    (#84)."""
    m = _mod()
    from homeassistant.exceptions import ConfigEntryAuthFailed

    ir = _ir_module()
    ok, *_ = _setup(m, ir=ir)
    assert ok is True
    assert ir.deleted == [("geely_connect", "x_vin_missing_e1")], ir.deleted
    assert ir.created == [], ir.created

    ir = _ir_module()
    entry = _entry()
    del entry.data["device_id"]
    try:
        _setup(m, entry=entry, ir=ir)
    except ConfigEntryAuthFailed:
        pass
    else:
        raise AssertionError("an entry without its device id set up anyway")
    assert ir.deleted == [("geely_connect", "x_vin_missing_e1")], ir.deleted


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
                  er=_er_module(_EntityRegistry([])), ir=_ir_module(),
                  dr=types.SimpleNamespace(
                      async_get=lambda h: types.SimpleNamespace(
                          async_get_device=lambda identifiers: None)),
                  async_register_admin_service=lambda *a, **k: None,
                  asyncio=fast):
        ok = asyncio.run(m.async_setup_entry(hass, entry))
    assert ok and _FakeCoordinator.instance.data is not None


# ------------------------------------------------------------- fire_control ---

def _service(m, bundles, call_data, name="fire_control"):
    hass = _Hass()
    hass.data["geely_connect"] = bundles
    registered = {}
    with _Patched(m, async_register_admin_service=lambda h, d, n, f, schema=None:
                  registered.update({n: f})):
        m._register_debug_service(hass)
    call = types.SimpleNamespace(data=call_data)
    return hass, lambda: asyncio.run(registered[name](call))


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


def test_fire_control_passes_the_latest_shape_only_when_asked():
    """The E2 app's frunk request carries `latest: true` and no scheduling
    block (#72). The flag reaches the client only when set, so the default
    call keeps the three-argument shape every fake and every car has seen."""
    m = _mod()
    api = _FakeApi()
    _, run = _service(m, {"e1": {"vin": FAKE_VIN, "api": api}},
                      {"service_id": "rlu", "command": "start",
                       "params": [{"key": "operation", "value": "1"},
                                  {"key": "rlu.target", "value": "1"}],
                       "latest": True})
    run()
    assert api.calls == [("control", "rlu",
                          [{"key": "operation", "value": "1"},
                           {"key": "rlu.target", "value": "1"}],
                          "start", "latest")]

    api = _FakeApi()
    _, run = _service(m, {"e1": {"vin": FAKE_VIN, "api": api}},
                      {"service_id": "rlu", "latest": False})
    run()
    assert api.calls == [("control", "rlu", [], "start")]


def test_a_named_vin_picks_that_car_and_not_the_first_one_loaded():
    """hass.data order is entry-setup order, so the wrong pick here sends a
    command to whichever car happened to load first - which can change across
    restarts."""
    m = _mod()
    first, second = _FakeApi(), _FakeApi()
    _, run = _service(m, {"e1": {"vin": "AAA", "api": first},
                          "e2": {"vin": "BBB", "api": second}},
                      {"service_id": "RCT", "vin": "BBB"})
    run()
    assert first.calls == [] and second.calls == [("control", "RCT", [], "start")]


# ------------------------------------------------------ set_scheduled_trip ---
# The app's Scheduled trip toggle (#4), built on the rule the thread settled
# on: read slot 4 first, send its contents back with only the command - and,
# when given, the departure - changed, and refuse when there is nothing to
# send. Never a switch, because an invented departure would move the owner's.

def test_scheduled_trip_stop_sends_back_what_the_car_holds():
    m = _mod()
    api = _FakeApi()
    _, run = _service(m, {"e1": {"vin": FAKE_VIN, "api": api}},
                      {"command": "stop"}, name="set_scheduled_trip")
    run()
    assert api.calls[0] == "charge4", "the slot must be read before anything is sent"
    (kind, kw), = [c for c in api.calls if isinstance(c, tuple)]
    assert kind == "trip"
    assert kw == {"command": "stop", "scheduled_time_ms": "1787229000000",
                  "ac": "true", "bw": "1", "bwl": "1"}


def test_scheduled_trip_start_with_a_departure_sends_it_in_ha_time():
    """The selector hands over a naive 07:00 meant in Home Assistant's zone.
    Read in the OS zone instead - UTC in a Docker install without TZ - it went
    out hours off. Pinned against a zone that is neither UTC nor this
    machine's, with the expected instant written out rather than computed by
    the code under test."""
    m = _mod()
    api = _FakeApi()
    dt_util = load("__init__").dt_util
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    before = dt_util.DEFAULT_TIME_ZONE
    dt_util.set_default_time_zone(ZoneInfo("Africa/Johannesburg"))   # UTC+2, no DST
    try:
        _, run = _service(m, {"e1": {"vin": FAKE_VIN, "api": api}},
                          {"command": "start", "departure": datetime(2026, 10, 2, 7, 0)},
                          name="set_scheduled_trip")
        run()
    finally:
        dt_util.set_default_time_zone(before)
    (_, kw), = [c for c in api.calls if isinstance(c, tuple)]
    want = str(int(datetime(2026, 10, 2, 5, 0, tzinfo=timezone.utc).timestamp() * 1000))
    assert kw["scheduled_time_ms"] == want, (kw, want)
    assert kw["command"] == "start"
    # The rest of the schedule travels as the car holds it, not as defaults.
    api.slot4 = {"bizType": 4, "scheduledTime": "1", "ac": "false", "bw": "0", "bwl": "2"}
    api.calls.clear()
    run()
    (_, kw), = [c for c in api.calls if isinstance(c, tuple)]
    assert (kw["ac"], kw["bw"], kw["bwl"]) == ("false", "0", "2")
    # A 0 is the toggle off, not a field to default; only an absent or empty
    # field takes the captured default.
    api.slot4 = {"bizType": 4, "scheduledTime": "1", "ac": "", "bw": 0}
    api.calls.clear()
    run()
    (_, kw), = [c for c in api.calls if isinstance(c, tuple)]
    assert (kw["ac"], kw["bw"], kw["bwl"]) == ("true", "0", "1")


def test_scheduled_trip_stops_before_reading_where_it_cannot_be_written():
    """A new-platform car reads its trip but has no captured write: the
    service refuses before the read, where it used to end in an
    AttributeError after it (#114 review, 4)."""
    m = _mod()
    from homeassistant.exceptions import ServiceValidationError
    api = _FakeApi()
    api.serves_trip_write = False
    _, run = _service(m, {"e1": {"vin": FAKE_VIN, "api": api}},
                      {"command": "stop"}, name="set_scheduled_trip")
    try:
        run()
    except ServiceValidationError as e:
        assert "not mapped" in str(e), e
    else:
        raise AssertionError("the service went on")
    assert api.calls == [], "nothing may be read or sent"


def test_scheduled_trip_refuses_to_invent_a_departure():
    """A car with no trip set and a call with no departure: nothing is sent,
    and the error says why. Defaults for ac/bw/bwl apply only once there IS a
    departure, as on a car that answers the generic empty envelope."""
    m = _mod()
    from homeassistant.exceptions import ServiceValidationError
    api = _FakeApi()
    api.slot4 = {"bizType": 4, "scheduleList": [], "bw": "", "startTime": "", "endTime": ""}
    _, run = _service(m, {"e1": {"vin": FAKE_VIN, "api": api}},
                      {"command": "start"}, name="set_scheduled_trip")
    try:
        run()
    except ServiceValidationError as e:
        assert "no departure" in str(e)
    else:
        raise AssertionError("a departure was invented")
    assert not [c for c in api.calls if isinstance(c, tuple)], "nothing may be sent"
    # With a departure given, the empty envelope's blanks fall back to the
    # captured defaults rather than travelling as "".
    from datetime import datetime
    _, run = _service(m, {"e1": {"vin": FAKE_VIN, "api": api}},
                      {"command": "start", "departure": datetime(2026, 10, 2, 7, 0)},
                      name="set_scheduled_trip")
    run()
    (_, kw), = [c for c in api.calls if isinstance(c, tuple)]
    assert (kw["ac"], kw["bw"], kw["bwl"]) == ("true", "1", "1")


def test_scheduled_trip_start_will_not_reuse_a_departure_already_gone():
    """A Starray on a weekday schedule held a one-off departure nine days past
    in slot 4 (#4). A start with no departure given would have armed a trip
    for that day; it is refused instead, and so is a value that is not a time.
    A stop still sends the stored value - that car disarmed with exactly it -
    and a departure still ahead is reused as before."""
    m = _mod()
    from homeassistant.exceptions import ServiceValidationError
    api = _FakeApi()
    _, run = _service(m, {"e1": {"vin": FAKE_VIN, "api": api}},
                      {"command": "start"}, name="set_scheduled_trip")
    for stored in ("946684800000", "soon"):        # 1 January 2000; not a time
        api.slot4 = {"bizType": 4, "scheduledTime": stored, "ac": "true", "bw": "1"}
        try:
            run()
        except ServiceValidationError as e:
            assert "already passed" in str(e)
        else:
            raise AssertionError(f"a start re-armed {stored!r}")
    assert not [c for c in api.calls if isinstance(c, tuple)], "nothing may be sent"

    api.slot4 = {"bizType": 4, "scheduledTime": "4102444800000", "ac": "true", "bw": "1"}
    run()                                           # 1 January 2100
    (_, kw), = [c for c in api.calls if isinstance(c, tuple)]
    assert kw["command"] == "start" and kw["scheduled_time_ms"] == "4102444800000"

    api.calls.clear()
    api.slot4 = {"bizType": 4, "scheduledTime": "946684800000", "ac": "true", "bw": "1"}
    _, run = _service(m, {"e1": {"vin": FAKE_VIN, "api": api}},
                      {"command": "stop"}, name="set_scheduled_trip")
    run()
    (_, kw), = [c for c in api.calls if isinstance(c, tuple)]
    assert kw["command"] == "stop" and kw["scheduled_time_ms"] == "946684800000"


def test_scheduled_trip_surfaces_a_failed_read_and_an_unmapped_platform():
    m = _mod()
    from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
    api = _FakeApi()
    api.charge_error = NotImplementedError("new-platform charge-server GET not mapped yet")
    _, run = _service(m, {"e1": {"vin": FAKE_VIN, "api": api}},
                      {"command": "stop"}, name="set_scheduled_trip")
    try:
        run()
    except ServiceValidationError as e:
        assert "not mapped" in str(e)
    else:
        raise AssertionError("an unmapped platform looked like a success")
    api.charge_error = RuntimeError("socket closed")
    try:
        run()
    except HomeAssistantError as e:
        assert "could not read" in str(e) and "socket closed" in str(e)
    else:
        raise AssertionError("a failed read looked like a success")
    assert not [c for c in api.calls if isinstance(c, tuple)]
    # A read that answers no dict at all is an empty schedule, not a crash.
    api.charge_error = None
    api.slot4 = None
    orig = api.charge_server_get
    api.charge_server_get = lambda biz: {"code": 1000, "data": "nonsense"}
    try:
        run()
    except ServiceValidationError as e:
        assert "no departure" in str(e)
    else:
        raise AssertionError("a non-dict read was not treated as empty")
    api.charge_server_get = orig


def test_scheduled_trip_refuses_a_car_that_speaks_the_1_0_scheme():
    """An E2 declares the trip as `booking_travel_1` and answers slot 4 in its
    own shape; the EX5 body would reach it with defaults filled in. Refused
    before anything goes out - not even the read."""
    m = _mod()
    from datetime import datetime
    from homeassistant.exceptions import ServiceValidationError
    api = _FakeApi()
    _, run = _service(m, {"e1": {"vin": FAKE_VIN, "api": api,
                                 "capabilities": {"scheduled_trip.enabled": False}}},
                      {"command": "start", "departure": datetime(2026, 10, 2, 7, 0)},
                      name="set_scheduled_trip")
    try:
        run()
    except ServiceValidationError as e:
        assert "1.0 scheme" in str(e)
    else:
        raise AssertionError("a 1.0-scheme car was sent the EX5 body")
    assert api.calls == [], api.calls


def test_scheduled_trip_treats_a_refused_read_as_a_failure_not_an_empty_slot():
    """A read the gateway answers with an error code carries no schedule, and
    with a departure given an empty schedule would send the defaults."""
    m = _mod()
    from datetime import datetime
    from homeassistant.exceptions import HomeAssistantError
    api = _FakeApi()
    api.charge_server_get = lambda biz: {"code": "8070", "message": "busy"}
    _, run = _service(m, {"e1": {"vin": FAKE_VIN, "api": api}},
                      {"command": "start", "departure": datetime(2026, 10, 2, 7, 0)},
                      name="set_scheduled_trip")
    try:
        run()
    except HomeAssistantError as e:
        assert "could not read" in str(e) and "8070" in str(e)
    else:
        raise AssertionError("a refused read was taken for an empty schedule")
    assert not [c for c in api.calls if isinstance(c, tuple)], "nothing may be sent"


def test_scheduled_trip_reads_the_car_back_afterwards():
    m = _mod()

    class _Coord:
        def __init__(self):
            self.refreshes = 0

        async def async_request_refresh(self):
            self.refreshes += 1

    coord = _Coord()
    hass, run = _service(m, {"e1": {"vin": FAKE_VIN, "api": _FakeApi(),
                                    "coordinator": coord}},
                         {"command": "stop"}, name="set_scheduled_trip")
    with _Patched(m, schedule_refresh=lambda h, c, *d: setattr(c, "scheduled", d)):
        run()
    assert coord.scheduled == (6, 12), "no read-back after the write"


# ------------------------------------------------------------- fire_rapid ---
# The compound bizType=7 body is the one that carries the seats, and it was
# never addressable by hand: fire_control speaks the telematics PUT, this is the
# charge-server POST. #19 has an EX5 accepting `paa.heat.11: 3` for both front
# seats and ignoring them, so settling the position encoding needs the body
# varied against a real car.

def test_fire_rapid_passes_the_probe_body_through_verbatim():
    m = _mod()
    api = _FakeApi()
    _, run = _service(m, {"e1": {"vin": FAKE_VIN, "api": api}},
                      {"temp": "15.5", "ac": True, "heat_seats": [],
                       "vent_seats": ["front-left", "front-right"],
                       "level": "3", "window_vent": True, "extra": {}},
                      name="fire_rapid")
    run()
    (kind, kw), = api.calls
    assert kind == "rapid"
    assert kw == {"ac": True, "temp": "15.5", "heat_seats": None,
                  "vent_seats": ["front-left", "front-right"], "vlt": True,
                  "level": "3", "extra": None}


def test_fire_rapid_defaults_leave_every_optional_field_alone():
    """Only `temp` is required, so a probe aimed at one field does not have to
    restate the rest."""
    m = _mod()
    api = _FakeApi()
    _, run = _service(m, {"e1": {"vin": FAKE_VIN, "api": api}},
                      {"temp": "28.5"}, name="fire_rapid")
    run()
    (_, kw), = api.calls
    assert kw == {"ac": True, "temp": "28.5", "heat_seats": None,
                  "vent_seats": None, "vlt": False, "level": "3", "extra": None}


def test_fire_rapid_forwards_extra_fields_and_a_chosen_level():
    """`extra` is how a field with no capture yet gets probed at all - the
    steering-wheel heat in #4 being the open example."""
    m = _mod()
    api = _FakeApi()
    _, run = _service(m, {"e1": {"vin": FAKE_VIN, "api": api}},
                      {"temp": "28.5", "heat_seats": ["11", "19"],
                       "level": "1", "extra": {"bw": "true"}},
                      name="fire_rapid")
    run()
    (_, kw), = api.calls
    assert kw["level"] == "1"
    assert kw["extra"] == {"bw": "true"}
    assert kw["heat_seats"] == ["11", "19"]


def test_fire_rapid_says_what_a_new_platform_entry_left_out_of_the_body():
    """An entry on the new platform sends the app's captured body and nothing
    more. The log line is what an owner reads and pastes into an issue, and it
    listed `extra` as sent: a probe that probed nothing read as a car that
    ignored the field."""
    m = _mod()
    import logging

    class _Lines(logging.Handler):
        def __init__(self):
            super().__init__(level=logging.WARNING)
            self.seen = []

        def emit(self, record):
            self.seen.append(record.getMessage())

    class _NewPlatform(_FakeApi):
        def rapid_ignores(self):
            return frozenset({"vent_seats", "vlt", "extra"})

    def fire(api, data):
        lines = _Lines()
        m._LOGGER.addHandler(lines)
        try:
            _, run = _service(m, {"e1": {"vin": FAKE_VIN, "api": api}}, data,
                              name="fire_rapid")
            run()
        finally:
            m._LOGGER.removeHandler(lines)
        line, = [s for s in lines.seen if s.startswith("fire_rapid ")]
        return line

    probe = {"temp": "28.5", "heat_seats": ["11"], "vent_seats": ["19"],
             "window_vent": True, "extra": {"sw": "true"}}
    line = fire(_NewPlatform(), probe)
    assert "NOT sent" in line and "extra, vent_seats, window_vent" in line, line
    # Only what was asked for and then dropped is named...
    line = fire(_NewPlatform(), {"temp": "28.5", "heat_seats": ["11"]})
    assert "NOT sent" not in line, line
    # ...and an entry whose client applies every field says nothing.
    assert "NOT sent" not in fire(_FakeApi(), probe)


def test_fire_rapid_refuses_an_extra_that_moves_the_body_to_another_slot():
    """rapid_climate applies `extra` last, so without this check the comment
    that bizType is pinned was only a comment: bizType 4 and 6 on the same
    endpoint write the scheduled-trip and scheduled-charging windows."""
    m = _mod()
    from homeassistant.exceptions import ServiceValidationError
    for extra in ({"bizType": "6"}, {"biztype": "4"}, {"command": "later"}):
        api = _FakeApi()
        _, run = _service(m, {"e1": {"vin": FAKE_VIN, "api": api}},
                          {"temp": "28.5", "extra": extra}, name="fire_rapid")
        try:
            run()
        except ServiceValidationError:
            pass
        else:
            raise AssertionError(f"{extra} reached the car")
        assert api.calls == [], extra


def test_fire_rapid_surfaces_a_rejection_instead_of_looking_successful():
    m = _mod()
    api_mod = load("api")
    from homeassistant.exceptions import HomeAssistantError

    class _Rejecting(_FakeApi):
        def rapid_climate(self, **kw):
            raise api_mod.GeelyControlError("8070", "pending")

    _, run = _service(m, {"e1": {"vin": FAKE_VIN, "api": _Rejecting()}},
                      {"temp": "15.5"}, name="fire_rapid")
    try:
        run()
    except HomeAssistantError as e:
        assert "pending" in str(e)
    else:
        raise AssertionError("a rejected probe looked successful")


def test_fire_rapid_reads_the_car_back_because_success_proves_nothing():
    """The gateway answers Success to any well-formed body, including seat
    positions the car does not recognise. What separates a probe that worked
    from one that was ignored is whether an entity moved."""
    m = _mod()

    class _Coord:
        def __init__(self):
            self.refreshes = 0

        async def async_request_refresh(self):
            self.refreshes += 1

    coord = _Coord()
    hass, run = _service(m, {"e1": {"vin": FAKE_VIN, "api": _FakeApi(),
                                    "coordinator": coord}},
                         {"temp": "15.5"}, name="fire_rapid")
    scheduled = []
    hass.async_create_task = scheduled.append
    fast = types.SimpleNamespace(sleep=_instant_sleep,
                                 CancelledError=asyncio.CancelledError)
    helpers = load("helpers")
    with _Patched(helpers, asyncio=fast):
        run()
        for coro in scheduled:
            asyncio.run(coro)
    assert coord.refreshes == 2, coord.refreshes


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


def test_the_services_register_once_but_a_missing_one_is_still_added():
    """Registering per vehicle entry means this runs again on every setup. The
    guard has to name every service it stands for: keyed on one of them, the
    next service added would be skipped for good on any system where the first
    was already present."""
    m = _mod()

    def _register(hass):
        calls = []
        with _Patched(m, async_register_admin_service=lambda *a, **k: calls.append(1)):
            m._register_debug_service(hass)
        return calls

    hass = _Hass()
    hass.services.registered.update({"fire_control": object(),
                                     "fire_rapid": object(),
                                     "set_scheduled_trip": object()})
    assert _register(hass) == [], "a second registration must be a no-op"

    partial = _Hass()
    partial.services.registered["fire_control"] = object()
    assert _register(partial), "a service that is missing must still be added"


# ------------------------------------------- services.yaml against the code ---
# services.yaml is what Developer Tools builds its form from, and nothing tied
# it to the schemas the handlers are registered with. A field in one and not the
# other is either missing from the form or refused when the form sends it, and
# an example the schema rejects teaches a call that cannot work.

def _registered_schemas(m):
    hass = _Hass()
    found = {}
    with _Patched(m, async_register_admin_service=lambda h, d, n, f, schema=None:
                  found.update({n: schema})):
        m._register_debug_service(hass)
    return found


def _services_yaml():
    import yaml
    with open(os.path.join(PKG, "services.yaml"), encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def test_services_yaml_and_the_registered_schemas_name_the_same_fields():
    m = _mod()
    import voluptuous as vol
    schemas, described = _registered_schemas(m), _services_yaml()
    assert set(described) == set(schemas)
    for name, schema in schemas.items():
        fields = described[name]["fields"]
        keys = {str(k): k for k in schema.schema}
        assert set(fields) == set(keys), (name, set(fields) ^ set(keys))
        for field, key in keys.items():
            spec = fields[field]
            assert bool(spec.get("required")) == isinstance(key, vol.Required), (
                name, field)
            if "default" in spec:
                assert key.default is not vol.UNDEFINED, (name, field)
                assert spec["default"] == key.default(), (name, field)
            else:
                assert key.default is vol.UNDEFINED, (name, field)


def test_every_example_and_option_in_services_yaml_is_one_the_schema_accepts():
    m = _mod()
    import voluptuous as vol
    schemas, described = _registered_schemas(m), _services_yaml()
    examples = options = 0
    for name, schema in schemas.items():
        validators = {str(k): v for k, v in schema.schema.items()}
        for field, spec in described[name]["fields"].items():
            accepts = vol.Schema({field: validators[field]})
            # What a drop-down offers is a value the form is going to send.
            for option in (spec["selector"].get("select") or {}).get("options", ()):
                accepts({field: option})
                options += 1
            if "example" not in spec:
                continue
            example = spec["example"]
            if "object" in spec["selector"]:
                example = json.loads(example)       # the form takes these as JSON
            accepts({field: example})
            examples += 1
    assert examples >= 8 and options >= 7, "not found - has the layout changed?"


def test_the_seat_examples_are_the_positions_the_presets_send():
    """The heated-seat field explained 11 and 19 and then showed "front-left" as
    its example: the name the capability list gives a seat, which this body has
    never carried. A probe copied from it asks for a seat the car does not have,
    and the gateway answers Success all the same."""
    _mod()                                  # PyYAML arrives with Home Assistant
    fields = _services_yaml()["fire_rapid"]["fields"]
    for field in ("heat_seats", "vent_seats"):
        assert json.loads(fields[field]["example"]) == ["11", "19"], field
    with open(os.path.join(PKG, "climate.py"), encoding="utf-8") as fh:
        source = "".join(fh.read().split())   # a reflowed line is no change
    assert '["11","19"]' in source, "the presets no longer send these"


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


def test_a_flagged_hf_writeback_makes_the_options_listener_skip_the_reload():
    """A silent HF-token refresh writes entry.data and so fires this listener
    like any change - but it flags itself, and the listener must consume the
    flag and do nothing, or every ~2-day renewal would reload the integration."""
    m = _mod()
    hass = _Hass()
    entry = _entry()
    hass.data["geely_connect"] = {"e1": {"_skip_reload_once": True}}
    purged = []
    with _Patched(m, _purge_raw_exposure_entities=lambda h, e: purged.append(1)):
        asyncio.run(m._async_options_updated(hass, entry))
    assert hass.config_entries.reloaded == [], "a flagged write must not reload"
    assert purged == [], "and must not run the exposure purge either"
    assert "_skip_reload_once" not in hass.data["geely_connect"]["e1"], \
        "the flag is consumed, so the next real change still reloads"


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
        vin_dir = os.path.join(root, ".storage", "geely_connect", FAKE_VIN)
        os.makedirs(vin_dir)
        open(os.path.join(vin_dir, "key.pem"), "w").write("KEY")
        entry = types.SimpleNamespace(
            entry_id="e1", data={"cert_path": os.path.join(vin_dir, "cert.pem")})
        ir = _ir_module()
        with _Patched(m, ir=ir):
            asyncio.run(m.async_remove_entry(hass, entry))
        assert not os.path.exists(vin_dir), "the key must not outlive the entry"
        # Nor may the x-vin Repairs issue, which would point at a Configure
        # page that no longer exists (#84).
        assert ir.deleted == [("geely_connect", "x_vin_missing_e1")], ir.deleted

        outside = os.path.join(root, "not-ours")
        os.makedirs(outside)
        open(os.path.join(outside, "key.pem"), "w").write("KEY")
        evil = types.SimpleNamespace(
            entry_id="e2", data={"cert_path": os.path.join(outside, "cert.pem")})
        with _Patched(m, ir=_ir_module()):
            asyncio.run(m.async_remove_entry(hass, evil))
        assert os.path.exists(outside), "a path outside our storage was deleted"

        with _Patched(m, ir=_ir_module()):
            asyncio.run(m.async_remove_entry(
                hass, types.SimpleNamespace(entry_id="e3", data={})))


def test_a_failed_shred_warns_instead_of_blocking_the_removal():
    import io
    m = _mod()
    with tempfile.TemporaryDirectory() as root:
        hass = _Hass()
        hass.config = types.SimpleNamespace(
            path=lambda *p: os.path.join(root, *p))
        vin_dir = os.path.join(root, ".storage", "geely_connect", FAKE_VIN)
        os.makedirs(vin_dir)
        entry = types.SimpleNamespace(
            entry_id="e1", data={"cert_path": os.path.join(vin_dir, "cert.pem")})
        tried = []

        def _boom(*a, **k):
            tried.append(a)
            raise PermissionError(13, "denied", vin_dir)

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        m._LOGGER.addHandler(handler)
        old_level = m._LOGGER.level
        m._LOGGER.setLevel(logging.INFO)
        try:
            with _Patched(m, shutil=types.SimpleNamespace(rmtree=_boom), ir=_ir_module()):
                asyncio.run(m.async_remove_entry(hass, entry))
        finally:
            m._LOGGER.removeHandler(handler)
            m._LOGGER.setLevel(old_level)
        assert tried, "rmtree was never attempted"
        assert os.path.exists(vin_dir), "the failure path must not half-delete"
        text = stream.getvalue()
        assert "Removed stored" not in text, text
        assert "could not remove" in text, text
        assert "denied" in text, text
        assert FAKE_VIN not in text, text
        # A short 8-char VIN (below mask_path's 11-char threshold) must not
        # leak through the masked OS reason either.
        short_vin = "VIN12345"
        short_dir = os.path.join(root, ".storage", "geely_connect", short_vin)
        os.makedirs(short_dir)
        short_entry = types.SimpleNamespace(
            entry_id="e2", data={"cert_path": os.path.join(short_dir, "cert.pem")})
        stream2 = io.StringIO()
        handler2 = logging.StreamHandler(stream2)
        m._LOGGER.addHandler(handler2)
        m._LOGGER.setLevel(logging.INFO)
        try:
            with _Patched(m, shutil=types.SimpleNamespace(
                rmtree=lambda *a, **k: (_ for _ in ()).throw(
                    OSError(30, "Read-only file system", short_dir))),
                    ir=_ir_module()):
                asyncio.run(m.async_remove_entry(hass, short_entry))
        finally:
            m._LOGGER.removeHandler(handler2)
            m._LOGGER.setLevel(old_level)
        text2 = stream2.getvalue()
        assert "Read-only file system" in text2, text2
        assert short_vin not in text2, text2


def test_silent_partial_deletion_and_missing_child_never_claim_success():
    from unittest.mock import patch
    m = _mod()
    for error in (None, FileNotFoundError("missing child")):
        with tempfile.TemporaryDirectory() as root:
            hass = _Hass()
            hass.config = types.SimpleNamespace(
                path=lambda *p: os.path.join(root, *p))
            folder = hass.config.path(".storage", "geely_connect", FAKE_VIN)
            os.makedirs(folder)
            entry = types.SimpleNamespace(entry_id="e1", data={"vin": FAKE_VIN})

            def incomplete(*args, **kwargs):
                if error:
                    raise error

            with _Patched(m, ir=_ir_module()), \
                    patch.object(m.shutil, "rmtree", incomplete), \
                    patch.object(m._LOGGER, "info") as info, \
                    patch.object(m._LOGGER, "warning") as warning:
                asyncio.run(m.async_remove_entry(hass, entry))
            assert not info.called
            assert warning.called
            rendered = warning.call_args[0][0] % warning.call_args[0][1:]
            assert "legacy material remains on disk" in rendered, rendered
            assert FAKE_VIN not in rendered, rendered


def test_removal_of_absent_key_directory_stays_quiet():
    from unittest.mock import patch
    m = _mod()
    with tempfile.TemporaryDirectory() as root:
        hass = _Hass()
        hass.config = types.SimpleNamespace(
            path=lambda *p: os.path.join(root, *p))
        entry = types.SimpleNamespace(
            entry_id="e1", data={"vin": FAKE_VIN, "platform": "zeekr"})
        with _Patched(m, ir=_ir_module()), \
                patch.object(m._LOGGER, "info") as info, \
                patch.object(m._LOGGER, "warning") as warning:
            asyncio.run(m.async_remove_entry(hass, entry))
        assert not info.called
        assert not warning.called


def test_changed_vin_migration_retains_original_vin_until_verified_removal():
    from test_zeekr_config_flow import _flow
    m = _mod()
    with tempfile.TemporaryDirectory() as root, \
            tempfile.TemporaryDirectory() as restored_root:
        old = os.path.join(root, ".storage", "geely_connect", FAKE_VIN)
        os.makedirs(old)
        with open(os.path.join(old, "key.pem"), "w") as f:
            f.write("FAKE KEY")
        entry = types.SimpleNamespace(entry_id="e1", data={
            "vin": FAKE_VIN,
            "cert_path": os.path.join(old, "cert.pem"),
            "key_path": os.path.join(old, "key.pem"),
            "email": "test@example.invalid",
        }, options={})
        _, flow = _flow([entry])
        flow.hass.config = types.SimpleNamespace(path=lambda *p: os.path.join(root, *p))
        flow._reauth_entry = entry
        flow._email = entry.data["email"]
        flow._user_id = "fake-user"
        flow._zeekr_tokens = ("fake-access", "fake-refresh")
        flow._zeekr_hf_token = "fake-hf"
        result = asyncio.run(flow._finish_zeekr({"vin": "L6T00000000000001"}))
        assert result["reason"] == "reauth_successful"
        assert entry.data["vin"] != FAKE_VIN and "cert_path" not in entry.data
        assert entry.data["legacy_cert_paths"] == [{"vin": FAKE_VIN}]
        redacted = load("api").redact(entry.data)
        assert redacted["legacy_cert_paths"] == "***redacted***"
        assert FAKE_VIN not in str(redacted)
        diag = load("diagnostics")
        diag_redacted = diag.async_redact_data(entry.data, diag._REDACT)
        assert FAKE_VIN not in str(diag_redacted["legacy_cert_paths"])
        # Rollback to v1.61.0 (redaction knowing only "vin") still masks it.
        v161_redacted = diag.async_redact_data(entry.data, {"vin"})
        assert v161_redacted["legacy_cert_paths"] == [{"vin": "**REDACTED**"}]
        assert FAKE_VIN not in str(v161_redacted)
        # Rebuilding the target directory from the current storage root survives
        # a backup restore that moves the HA config directory.
        restored_old = os.path.join(restored_root, ".storage", "geely_connect", FAKE_VIN)
        os.makedirs(restored_old)
        with open(os.path.join(restored_old, "key.pem"), "w") as f:
            f.write("FAKE KEY")
        restored_hass = _Hass()
        restored_hass.config = types.SimpleNamespace(
            path=lambda *p: os.path.join(restored_root, *p))
        with _Patched(m, ir=_ir_module()):
            asyncio.run(m.async_remove_entry(restored_hass, entry))
        assert not os.path.exists(restored_old)
        assert os.path.exists(old), "removal must target the current storage root only"


def test_migration_handles_legacy_dir_inspection_failure_and_stale_paths():
    from unittest.mock import patch
    from test_zeekr_config_flow import _flow
    with tempfile.TemporaryDirectory() as root:
        stale = {"vin": FAKE_VIN}
        entry = types.SimpleNamespace(entry_id="e1", data={
            "vin": FAKE_VIN,
            "legacy_cert_paths": [stale],
            "email": "test@example.invalid",
        }, options={})
        cf, flow = _flow([entry])
        flow.hass.config = types.SimpleNamespace(path=lambda *p: os.path.join(root, *p))
        flow._reauth_entry = entry
        flow._email = entry.data["email"]
        flow._user_id = "fake-user"
        flow._zeekr_tokens = ("fake-access", "fake-refresh")
        flow._zeekr_hf_token = "fake-hf"
        # An unexpected error inspecting legacy storage warns without aborting
        # and keeps any already-recorded legacy_cert_paths intact.
        with patch.object(cf, "legacy_material_dirs", side_effect=OSError(f"boom {FAKE_VIN}")), \
                patch.object(cf._LOGGER, "warning") as warning:
            result = asyncio.run(flow._finish_zeekr({"vin": "L6T00000000000001"}))
        assert result["reason"] == "reauth_successful"
        assert entry.data["legacy_cert_paths"] == [stale]
        assert warning.called
        assert FAKE_VIN not in str(warning.call_args)
        # Once the legacy directory is gone on disk, a subsequent reauth drops
        # the stale reference instead of carrying a dead entry forward.
        result = asyncio.run(flow._finish_zeekr({"vin": "L6T00000000000001"}))
        assert result["reason"] == "reauth_successful"
        assert "legacy_cert_paths" not in entry.data


def test_unsafe_storage_references_are_never_retained_or_deleted():
    from unittest.mock import patch
    helpers = load("helpers")
    with tempfile.TemporaryDirectory() as root:
        hass = _Hass()
        hass.config = types.SimpleNamespace(
            path=lambda *p: os.path.join(root, *p))
        base = hass.config.path(".storage", "geely_connect")
        os.makedirs(base)
        outside = os.path.join(root, "outside")
        os.makedirs(outside)
        for bad_item in (
            3,
            "relative/cert.pem",
            os.path.join(base, FAKE_VIN, "cert.pem"),
            {"vin": "../outside"},
            {"vin": "short"},
            {"other": FAKE_VIN},
            {"vin": f"{FAKE_VIN}\x00"},
        ):
            with patch.object(helpers._LOGGER, "warning") as warning:
                assert helpers.legacy_material_dirs(
                    hass, {"legacy_cert_paths": [bad_item]}) == [], bad_item
            assert warning.called, bad_item
            msg = warning.call_args[0][0] % warning.call_args[0][1:]
            assert "legacy_cert_paths" in msg and FAKE_VIN not in msg, msg
        with patch.object(helpers._LOGGER, "warning") as warning:
            assert helpers.legacy_material_dirs(
                hass, {"legacy_cert_paths": "not-a-list"}) == []
        assert warning.called
        msg = warning.call_args[0][0] % warning.call_args[0][1:]
        assert "legacy_cert_paths" in msg, msg
        for bad_path in (
            os.path.join(outside, "cert.pem"),
            os.path.join(base, "cert.pem"),
            3,
            "relative/cert.pem",
            os.path.join(base, FAKE_VIN, "unexpected.pem"),
            os.path.join(base, "short", "cert.pem"),
            os.path.join(root, "bad\x00dir", FAKE_VIN, "cert.pem"),
        ):
            with patch.object(helpers._LOGGER, "warning") as warning:
                assert helpers.legacy_material_dirs(
                    hass, {"cert_path": bad_path}) == [], bad_path
            assert warning.called, bad_path
            msg = warning.call_args[0][0] % warning.call_args[0][1:]
            assert "cert_path" in msg and FAKE_VIN not in msg, msg
        assert helpers.legacy_material_dirs(hass, {"vin": "../outside"}) == []


def test_symlinked_storage_directory_is_refused_with_real_symlink():
    from run import skip
    helpers = load("helpers")
    m = _mod()
    with tempfile.TemporaryDirectory() as root:
        hass = _Hass()
        hass.config = types.SimpleNamespace(
            path=lambda *p: os.path.join(root, *p))
        base = hass.config.path(".storage", "geely_connect")
        os.makedirs(base)
        outside = os.path.join(root, "outside")
        os.makedirs(outside)
        open(os.path.join(outside, "keep.txt"), "w").write("KEEP")
        link = os.path.join(base, FAKE_VIN)
        try:
            os.symlink(outside, link)
        except OSError as exc:
            skip(f"os.symlink unavailable: {exc}")
        for data in (
            {"vin": FAKE_VIN},
            {"cert_path": os.path.join(link, "cert.pem")},
            {"legacy_cert_paths": [{"vin": FAKE_VIN}]},
        ):
            assert helpers.legacy_material_dirs(hass, data) == [], data
            with _Patched(m, ir=_ir_module()):
                asyncio.run(m.async_remove_entry(
                    hass, types.SimpleNamespace(entry_id="e1", data=data)))
        assert os.path.exists(os.path.join(outside, "keep.txt"))


def test_symlink_guard_refuses_linked_directory_without_os_symlink():
    """Cross-platform coverage of the islink guard even on Windows where
    os.symlink raises WinError 1314 without elevated privileges."""
    from unittest.mock import patch
    helpers = load("helpers")
    with tempfile.TemporaryDirectory() as root:
        hass = _Hass()
        hass.config = types.SimpleNamespace(
            path=lambda *p: os.path.join(root, *p))
        vin_dir = hass.config.path(".storage", "geely_connect", FAKE_VIN)
        os.makedirs(vin_dir)
        target = os.path.realpath(vin_dir)
        real_islink = os.path.islink
        with patch.object(helpers.os.path, "islink",
                          side_effect=lambda p: p == target or real_islink(p)), \
                patch.object(helpers._LOGGER, "warning") as warning:
            assert helpers.legacy_material_dirs(
                hass, {"legacy_cert_paths": [{"vin": FAKE_VIN}]}) == []
        assert warning.called
        msg = warning.call_args[0][0] % warning.call_args[0][1:]
        assert "legacy_cert_paths" in msg and FAKE_VIN not in msg, msg


def test_outside_cert_path_with_matching_vin_never_deletes_storage_dir():
    """An outside cert_path whose parent folder happens to share a valid VIN
    with a real directory inside storage must never resolve to or delete the
    storage directory; both realpath checks are guarded individually too."""
    from unittest.mock import patch
    helpers = load("helpers")
    m = _mod()
    with tempfile.TemporaryDirectory() as root:
        hass = _Hass()
        hass.config = types.SimpleNamespace(
            path=lambda *p: os.path.join(root, *p))
        in_storage = hass.config.path(".storage", "geely_connect", FAKE_VIN)
        os.makedirs(in_storage)
        open(os.path.join(in_storage, "key.pem"), "w").write("KEEP")
        outside_vin = os.path.join(root, "outside", FAKE_VIN)
        os.makedirs(outside_vin)
        outside_cert = os.path.join(outside_vin, "cert.pem")
        entry = types.SimpleNamespace(entry_id="e1", data={"cert_path": outside_cert})
        assert helpers.legacy_material_dirs(hass, entry.data) == []
        with _Patched(m, ir=_ir_module()):
            asyncio.run(m.async_remove_entry(hass, entry))
        assert os.path.exists(os.path.join(in_storage, "key.pem"))

        # Independently guard realpath(dirname(original)) != root (e.g. an
        # outside junction pointing into storage when islink reports False).
        storage_root = os.path.realpath(hass.config.path(".storage", "geely_connect"))
        storage_target = os.path.join(storage_root, FAKE_VIN)
        real_realpath = os.path.realpath
        with patch.object(helpers.os.path, "islink", return_value=False), \
                patch.object(helpers.os.path, "realpath", side_effect=lambda p: (
                    storage_target if p == os.path.abspath(outside_vin)
                    else real_realpath(p))):
            assert helpers.legacy_material_dirs(hass, entry.data) == []

        # Independently guard realpath(original) != directory (e.g. a storage
        # junction pointing outside when islink reports False).
        inside_cert = os.path.join(storage_target, "cert.pem")
        with patch.object(helpers.os.path, "islink", return_value=False), \
                patch.object(helpers.os.path, "realpath", side_effect=lambda p: (
                    os.path.abspath(outside_vin) if p == os.path.abspath(storage_target)
                    else real_realpath(p))):
            assert helpers.legacy_material_dirs(hass, {"cert_path": inside_cert}) == []


def test_multiple_legacy_directories_deduplicate_and_remove_idempotently():
    m = _mod()
    with tempfile.TemporaryDirectory() as root:
        hass = _Hass()
        hass.config = types.SimpleNamespace(
            path=lambda *p: os.path.join(root, *p))
        old = hass.config.path(".storage", "geely_connect", FAKE_VIN)
        current = hass.config.path(".storage", "geely_connect", "L6T00000000000001")
        for directory in (old, current):
            os.makedirs(directory)
        entry = types.SimpleNamespace(entry_id="e1", data={
            "vin": "L6T00000000000001",
            "legacy_cert_paths": [{"vin": FAKE_VIN}, {"vin": FAKE_VIN}],
        })
        with _Patched(m, ir=_ir_module()):
            asyncio.run(m.async_remove_entry(hass, entry))
            asyncio.run(m.async_remove_entry(hass, entry))
        assert not os.path.exists(old) and not os.path.exists(current)


def test_removal_finds_the_key_directory_of_an_entry_that_moved_platforms():
    """The move to the new platform drops cert_path from the entry and leaves
    the key on disk. Removing that entry later used to return at once, so the
    key - which can unlock the car - stayed in .storage and in every backup."""
    m = _mod()
    with tempfile.TemporaryDirectory() as root:
        hass = _Hass()
        hass.config = types.SimpleNamespace(
            path=lambda *p: os.path.join(root, *p))
        vin_dir = os.path.join(root, ".storage", "geely_connect", FAKE_VIN)
        os.makedirs(vin_dir)
        open(os.path.join(vin_dir, "key.pem"), "w").write("KEY")
        entry = types.SimpleNamespace(
            entry_id="e1", data={"vin": FAKE_VIN, "platform": "zeekr"})
        with _Patched(m, ir=_ir_module()):
            asyncio.run(m.async_remove_entry(hass, entry))
        assert not os.path.exists(vin_dir), "the key outlived the entry"
        # A VIN that is not a plain identifier never becomes a path, and a car
        # that never had a key directory (a new-platform-native entry) is left
        # alone without a word in the log.
        for vin in ("../not-ours", "L6T00000000000001"):
            with _Patched(m, ir=_ir_module()):
                asyncio.run(m.async_remove_entry(hass, types.SimpleNamespace(
                    entry_id="e2", data={"vin": vin})))
        assert os.listdir(os.path.join(root, ".storage", "geely_connect")) == []


def test_removal_keeps_a_key_directory_another_entry_still_uses():
    """Two entries on one car - which a re-authentication in an earlier
    version could leave behind - share the VIN-named directory. Removing one
    must protect a surviving entry's active directory, but a surviving entry's
    retired legacy_cert_paths must not keep an unowned key alive."""
    m = _mod()
    with tempfile.TemporaryDirectory() as root:
        hass = _Hass()
        hass.config = types.SimpleNamespace(
            path=lambda *p: os.path.join(root, *p))
        vin_dir = os.path.join(root, ".storage", "geely_connect", FAKE_VIN)
        os.makedirs(vin_dir)
        open(os.path.join(vin_dir, "key.pem"), "w").write("KEY")
        moved = types.SimpleNamespace(
            entry_id="e1", data={"vin": FAKE_VIN, "platform": "zeekr"})
        migrated_legacy = types.SimpleNamespace(
            entry_id="e4", data={
                "vin": "OTHERVIN03",
                "platform": "zeekr",
                "legacy_cert_paths": [{"vin": FAKE_VIN}],
            })
        unrelated = types.SimpleNamespace(entry_id="e9", data={"vin": "OTHERVIN01"})
        # Active siblings (by VIN, cert_path, or key_path) protect the key when
        # either another same-VIN entry or a migrated legacy entry is removed.
        for sibling in (
                types.SimpleNamespace(entry_id="e2", data={"vin": FAKE_VIN}),
                types.SimpleNamespace(entry_id="e3", data={
                    "vin": "OTHERVIN02",
                    "cert_path": os.path.join(vin_dir, "cert.pem")}),
                types.SimpleNamespace(entry_id="e5", data={
                    "vin": "OTHERVIN04",
                    "key_path": os.path.join(vin_dir, "key.pem")})):
            for removed in (moved, migrated_legacy):
                hass.config_entries.entries = [removed, unrelated, sibling]
                with _Patched(m, ir=_ir_module()):
                    asyncio.run(m.async_remove_entry(hass, removed))
                assert os.path.exists(os.path.join(vin_dir, "key.pem")), (
                    removed.entry_id, sibling.entry_id)
        # Reverse direction: when the original active owner is removed and the
        # only surviving entry merely lists FAKE_VIN in retired legacy_cert_paths,
        # the directory is deleted immediately instead of outliving its owner.
        active_owner = types.SimpleNamespace(
            entry_id="e2", data={
                "vin": FAKE_VIN,
                "cert_path": os.path.join(vin_dir, "cert.pem"),
            })
        hass.config_entries.entries = [active_owner, unrelated, migrated_legacy]
        with _Patched(m, ir=_ir_module()):
            asyncio.run(m.async_remove_entry(hass, active_owner))
        assert not os.path.exists(vin_dir)


def test_removal_stays_quiet_for_shared_absent_dir_and_bad_sibling_refs():
    """Two Geely EM entries on one car with no key directory on disk must not
    log 'keeping ...', and inspecting unrelated siblings with malformed paths
    must not emit warnings during another entry's removal."""
    from unittest.mock import patch
    helpers = load("helpers")
    m = _mod()
    with tempfile.TemporaryDirectory() as root:
        hass = _Hass()
        hass.config = types.SimpleNamespace(
            path=lambda *p: os.path.join(root, *p))
        e1 = types.SimpleNamespace(
            entry_id="e1", data={"vin": FAKE_VIN, "platform": "zeekr"})
        e2 = types.SimpleNamespace(
            entry_id="e2", data={"vin": FAKE_VIN, "platform": "zeekr"})
        bad_sibling = types.SimpleNamespace(
            entry_id="e3", data={
                "vin": "OTHERVIN01",
                "cert_path": "relative/cert.pem",
                "key_path": os.path.join(root, "outside", "OTHERVIN01", "key.pem"),
            })
        hass.config_entries.entries = [e1, e2, bad_sibling]
        with _Patched(m, ir=_ir_module()), \
                patch.object(m._LOGGER, "info") as info, \
                patch.object(m._LOGGER, "warning") as warning, \
                patch.object(helpers._LOGGER, "warning") as helper_warning:
            asyncio.run(m.async_remove_entry(hass, e1))
        assert not info.called, info.call_args_list
        assert not warning.called, warning.call_args_list
        assert not helper_warning.called, helper_warning.call_args_list


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


# ------------------------------------------------ when to wake the car ---
# The position wake reaches the car itself and costs 12 V charge, so it fires
# when the fix we hold could be wrong - not on a timer. The odometer is what
# makes that knowable without asking: strictly increasing, in every payload.

def _snap(speed, odo):
    return {"code": 1000, "data": {"vehicleStatus": {
        "basicVehicleStatus": {"speed": speed},
        "additionalVehicleStatus": {
            "electricVehicleStatus": {"statusOfChargerConnection": "0",
                                      "chargeLevel": "80"},
            "maintenanceStatus": {"odometer": odo}}}}}


def _wakes(api):
    return api.calls.count("position")


def test_a_parked_car_is_not_woken_to_re_ask_where_it_is():
    """The change this replaces woke a car every Nth poll while parked - about
    every ninety minutes on Normal, for a fortnight at an airport. An odometer
    that has not moved is the car already saying it is where it was."""
    m = _mod()
    _, _, _, api, coord, _ = _setup(m)
    api.status_results = [_snap("0", "2174") for _ in range(6)]
    for _ in range(6):
        asyncio.run(coord.refresh())
    before = _wakes(api)
    api.status_results = [_snap("0", "2174") for _ in range(10)]
    for _ in range(10):
        asyncio.run(coord.refresh())
    assert _wakes(api) == before, "a stationary car was woken again"


def test_the_last_fix_of_a_drive_is_taken_once_the_car_has_parked():
    """Where the car is parked is the reading an owner actually wants, and it
    only exists after the car stops. `was_driving` reads the PREVIOUS snapshot,
    so the first parked cycle still sees driving and takes that final fix."""
    m = _mod()
    _, _, _, api, coord, _ = _setup(m)
    api.status_results = [_snap("0", "2174"), _snap("0", "2174")]
    for _ in range(2):
        asyncio.run(coord.refresh())
    quiet = _wakes(api)

    api.status_results = [_snap("60", "2180"), _snap("55", "2186")]
    for _ in range(2):
        asyncio.run(coord.refresh())
    assert _wakes(api) > quiet, "the map must follow a moving car"

    driving = _wakes(api)
    api.status_results = [_snap("0", "2192")]
    asyncio.run(coord.refresh())
    assert _wakes(api) == driving + 1, "no final fix once the car parked"

    parked = _wakes(api)
    api.status_results = [_snap("0", "2192") for _ in range(5)]
    for _ in range(5):
        asyncio.run(coord.refresh())
    assert _wakes(api) == parked, "it kept waking a car that had already parked"


def test_a_trip_this_integration_never_saw_still_refreshes_the_fix():
    """A drive can begin and end inside one parked back-off window - fifteen
    minutes on Normal - leaving speed at zero on both sides of it. The odometer
    is the only evidence left that the fix we hold is somewhere else."""
    m = _mod()
    _, _, _, api, coord, _ = _setup(m)
    api.status_results = [_snap("0", "2174") for _ in range(4)]
    for _ in range(4):
        asyncio.run(coord.refresh())
    settled = _wakes(api)

    api.status_results = [_snap("0", "2192"), _snap("0", "2192")]
    for _ in range(2):
        asyncio.run(coord.refresh())
    assert _wakes(api) > settled, "an unobserved trip left a stale position"


def test_a_car_with_no_readable_odometer_keeps_the_old_cadence():
    """Nothing is proven without an odometer, so the timer stands exactly as it
    did - this change must not cost any car its coverage."""
    m = _mod()
    _, _, _, api, coord, _ = _setup(m)
    api.status_results = [_snap("0", None) for _ in range(12)]
    for _ in range(12):
        asyncio.run(coord.refresh())
    profile_n = 6      # POLL_PROFILES["normal"]["position_every"]
    assert _wakes(api) >= 12 // profile_n, "the fallback timer stopped firing"



# ------------------------------------- scheduled charging in slot 2 (#72) ---

_E2_SLOT_2 = {"bizType": 2, "id": 898610, "timerId": "1", "scheduleList": [
    {"duration": "07:00", "endtime": "17:00", "startTime": "10:00",
     "timerActivation": "1", "day": "1,2,3,4,5,6,7"}]}


def _e2_catalogue(api):
    api.fetch_capabilities = lambda: [
        {"functionId": "remote_appointment_charging", "valueEnable": True,
         "paramsJson": [{"nameKey": "remote_charge_1_chargestarttime",
                         "config": "remote_charge_1_chargestart"}]}]
    plain = api.charge_server_get

    def get(biz):
        if biz == "2":
            api.calls.append("charge2")
            return {"code": 1000, "data": dict(_E2_SLOT_2)}
        return plain(biz)
    api.charge_server_get = get


def test_a_car_whose_catalogue_names_slot_2_reads_its_schedule_there():
    """The first refresh runs before the catalogue is read, so it can only ask
    slot 6. Once the catalogue says slot 2, slot 2 is read at once, rather than
    the switch reading slot 6's empty answer until the next secondary poll,
    and every secondary poll after asks slot 2."""
    m = _mod()
    ok, hass, _, api, coord, _ = _setup(m, api_tweak=_e2_catalogue)
    poll = hass.data["geely_connect"]["e1"]["poll_state"]
    assert ok and poll["charge_slot"] == "2"
    assert api.calls.index("charge6") < api.calls.index("charge2"), api.calls
    assert coord.data["_scheduled_charging"] == _E2_SLOT_2
    poll["force_secondary"] = True
    before = api.calls.count("charge2")
    asyncio.run(coord.refresh())
    assert api.calls.count("charge2") == before + 1, api.calls
    assert coord.data["_scheduled_charging"] == _E2_SLOT_2


def _plugged_e2(is_charging, minutes):
    return {"code": 1000, "data": {"vehicleStatus": {
        "basicVehicleStatus": {"speed": "0"},
        "additionalVehicleStatus": {"electricVehicleStatus": {
            "statusOfChargerConnection": "3", "chargeLevel": "100",
            "isCharging": is_charging, "timeToFullyCharged": minutes}}}}}


def test_a_1_0_car_with_the_cable_in_polls_as_parked_unless_it_is_charging():
    """An E2 reports connection code 3 whenever the cable is in (#72, ten
    files), so a full car left plugged in overnight polled on the fast
    interval and the cards lit charging. Its catalogue's 1.0 charge scheme
    now marks the car, the poller writes its verdict into every snapshot,
    and the first snapshot - taken before the catalogue - is judged again."""
    m = _mod()
    idle, live = _plugged_e2("false", "0"), _plugged_e2("true", "155")
    fast = m.POLL_PROFILES["normal"]["fast"]

    def e2(api):
        _e2_catalogue(api)
        schedule_row = api.fetch_capabilities()
        # The E2's charge row: a start time, not a switch - the 1.0 scheme
        # that marks the car (capabilities.py).
        api.fetch_capabilities = lambda: schedule_row + [
            {"functionId": "remote_charge_1", "valueEnable": True,
             "valueEnum": "remote_charge_1_chargestart",
             "paramsJson": [{"nameKey": "remote_charge_1_chargestarttime",
                             "config": "remote_charge_1_chargestart"}]}]
        api.status_results = [dict(idle), dict(idle), dict(live)]

    ok, hass, _, api, coord, _ = _setup(m, api_tweak=e2)
    poll = hass.data["geely_connect"]["e1"]["poll_state"]
    assert ok and poll["code_3_is_plugged"] is True
    assert coord.data["_charging"] is False, "the first snapshot, judged again"
    assert coord.update_interval.total_seconds() != fast, "an idle car, not a charging one"
    asyncio.run(coord.refresh())
    assert coord.data["_charging"] is False and m._poll_flags(coord.data)[0] is False
    assert coord.update_interval.total_seconds() != fast
    asyncio.run(coord.refresh())                     # the charge starts
    assert coord.data["_charging"] is True and m._poll_flags(coord.data)[0] is True
    assert coord.update_interval.total_seconds() == fast
    # An EX5's catalogue leaves the code its word: 3 is charging there.
    ok, hass, _, api, coord, _ = _setup(
        m, api_tweak=lambda api: setattr(api, "status_results", [dict(idle)]))
    assert ok and hass.data["geely_connect"]["e1"]["poll_state"]["code_3_is_plugged"] is False
    assert coord.data["_charging"] is True
    assert coord.update_interval.total_seconds() == fast


def _one_zero_trip_catalogue(api):
    api.fetch_capabilities = lambda: [
        {"functionId": "booking_travel_1", "valueEnable": True, "paramsJson": []}]


def test_the_scheduled_trip_is_read_on_the_timer_and_a_refresh_press_only():
    """Battery Temperature Maintenance is slot 4's `bw` and nowhere in the
    status block (#4), so slot 4 is polled beside the charging schedule - on
    the timer's cadence and a Refresh press. Not on the fast path a charging
    car takes every poll: that is for the state block and the charging
    schedule, which move, and a plugged-in E2 reports the charging code all
    day (#72). The last value is carried across the polls that skip it."""
    m = _mod()
    ok, hass, _, api, coord, _ = _setup(m)          # cycle 1 is the timer's
    assert ok and api.calls.count("charge4") == 1
    api.status_results = [{"code": 1000, "data": {"vehicleStatus": {
        "basicVehicleStatus": {"speed": "0"},
        "additionalVehicleStatus": {"electricVehicleStatus": {
            "statusOfChargerConnection": "3", "chargeLevel": "80"}}}}}]
    asyncio.run(coord.refresh())                     # cycle 2, charging
    assert api.calls.count("state") == 2 and api.calls.count("charge6") == 2, api.calls
    assert api.calls.count("charge4") == 1, api.calls
    assert coord.data["_scheduled_trip"]["bw"] == "1"
    hass.data["geely_connect"]["e1"]["poll_state"]["force_secondary"] = True
    asyncio.run(coord.refresh())                     # cycle 3, a press
    assert api.calls.count("charge4") == 2, api.calls
    assert coord.data["_scheduled_trip"]["bw"] == "1"


def test_a_1_0_car_stops_reading_the_scheduled_trip_once_its_catalogue_says_so():
    """The first refresh runs before the catalogue; what it read from slot 4 is
    dropped, and no poll after asks again (#72)."""
    m = _mod()
    ok, hass, _, api, coord, _ = _setup(m, api_tweak=_one_zero_trip_catalogue)
    poll = hass.data["geely_connect"]["e1"]["poll_state"]
    assert ok and poll["read_trip"] is False
    assert "_scheduled_trip" not in coord.data
    poll["force_secondary"] = True
    before = api.calls.count("charge4")
    asyncio.run(coord.refresh())
    assert api.calls.count("charge4") == before, api.calls
    assert "_scheduled_trip" not in coord.data


def test_every_other_car_keeps_reading_slot_6():
    m = _mod()
    ok, hass, _, api, coord, _ = _setup(m)
    assert hass.data["geely_connect"]["e1"]["poll_state"]["charge_slot"] == "6"
    assert "charge2" not in api.calls and "charge6" in api.calls, api.calls


def test_the_slot_2_read_at_setup_leaves_the_data_alone_when_it_fails():
    m = _mod()
    hass = _Hass()
    codes = {1000, "1000"}

    def run(api, data):
        coord = types.SimpleNamespace(data=data)
        asyncio.run(m._read_slot_2_now(hass, api, coord, codes))
        return coord.data

    def raising(biz):
        raise RuntimeError("socket closed")
    keep = {"_scheduled_charging": {"bizType": 6}}
    for get in (raising, lambda biz: {"code": "8070", "message": "busy"},
                lambda biz: {"code": 1000, "data": "nonsense"}):
        # Every client quotes an error through without_secrets().
        api = types.SimpleNamespace(charge_server_get=get, without_secrets=str)
        assert run(api, dict(keep)) == keep
    # An api that does not serve the slot, or has stopped reading it, is not
    # asked at all. The getter answers well, so only the gate can keep the
    # data as it was - a getter that raised was swallowed either way.
    def good(biz):
        return {"code": 1000, "data": {"bizType": 2}}
    served = types.SimpleNamespace(serves_charge_server=False,
                                   charge_server_get=good)
    assert run(served, dict(keep)) == keep
    stopped = types.SimpleNamespace(reads_charge_server=False,
                                    charge_server_get=good)
    assert run(stopped, dict(keep)) == keep
    assert run(types.SimpleNamespace(charge_server_get=good), dict(keep)) == {
        "_scheduled_charging": {"bizType": 2}}
    assert run(types.SimpleNamespace(charge_server_get=lambda biz: 1 / 0), None) is None


# -------------------------------------------------------- package namespace ---

def _assigned_names(target) -> set:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.Tuple, ast.List)):
        return set().union(*(_assigned_names(e) for e in target.elts))
    return set()                                    # x.y = ..., x[i] = ...: not a binding


def _module_level_bindings(source: str) -> set:
    """Names a module binds in its own namespace: imports, defs, classes and
    assignments at the top level, looked for inside top-level if/try/with
    blocks too; function and class bodies bind elsewhere and are not walked.
    The siblings `from . import x` binds on purpose are left out - those are
    the very objects the import system would set."""
    bound = set()

    def visit(stmts):
        for node in stmts:
            if isinstance(node, ast.Import):
                bound.update(a.asname or a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                if not (node.level and node.module is None):
                    bound.update(a.asname or a.name for a in node.names)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bound.add(node.name)
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    bound.update(_assigned_names(t))
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                bound.update(_assigned_names(node.target))
            elif isinstance(node, ast.If):
                visit(node.body)
                visit(node.orelse)
            elif isinstance(node, ast.Try):
                visit(node.body)
                visit(node.orelse)
                visit(node.finalbody)
                for handler in node.handlers:
                    visit(handler.body)
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                visit(node.body)

    visit(ast.parse(source).body)
    return bound


def test_the_package_namespace_binds_no_name_one_of_its_own_files_will_take():
    """__init__.py runs in the package's namespace, and every module file in
    the package becomes an attribute of that namespace the moment Home
    Assistant imports it as a platform - `time` and `select` are also stdlib
    modules. A plain `import time` there was replaced by the time platform
    when it loaded, and every poll's clock read after that raised (#72)."""
    siblings = {os.path.splitext(f)[0] for f in os.listdir(PKG)
                if f.endswith(".py") or os.path.isdir(os.path.join(PKG, f))}
    siblings -= {"__init__", "__pycache__"}
    assert {"time", "select", "translations"} <= siblings, siblings
    with open(os.path.join(PKG, "__init__.py"), encoding="utf-8") as fh:
        bound = _module_level_bindings(fh.read())
    assert {"PLATFORMS", "async_setup_entry", "_LOGGER"} <= bound   # the rule sees every kind
    assert not (bound & siblings), bound & siblings


def test_loading_the_time_platform_leaves_the_poller_its_clock():
    """The real mechanism, not a simulation: __init__.py loaded as a package
    and its time platform imported through the import system the way Home
    Assistant does, which binds the platform to the package's `time` name.
    The next poll must still read the clock; before the fix it raised
    AttributeError, and an entry reload then failed at its first refresh with
    "module '...geely_connect.time' has no attribute 'monotonic'" (#72)."""
    m = _mod()                                      # skips without HA
    name = "gc_pkg_clock"
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(PKG, "__init__.py"), submodule_search_locations=[PKG])
    pkg = importlib.util.module_from_spec(spec)
    sys.modules[name] = pkg
    try:
        spec.loader.exec_module(pkg)
        assert pkg.PLATFORMS == m.PLATFORMS and "time" in pkg.PLATFORMS
        ok, hass, _, api, coord, _ = _setup(pkg)
        assert ok is True
        platform = importlib.import_module(f"{name}.time")
        assert getattr(pkg, "time") is platform, "the import system binds it here"
        # A press just now: the clock read decides to fetch the state block,
        # so that fetch is the witness that the read ran, not only that the
        # poll did not raise. The status read comes before the clock.
        hass.data["geely_connect"]["e1"]["poll_state"]["state_until"] = time.monotonic() + 60
        state_reads = api.calls.count("state")
        asyncio.run(coord.refresh())                # raised before the fix
        assert api.calls.count("state") == state_reads + 1, api.calls
    finally:
        _FakeCoordinator.instance = None            # let the second package tree go
        for key in [k for k in sys.modules if k == name or k.startswith(name + ".")]:
            sys.modules.pop(key, None)


# ------------------------------------------------ errors quoted by the client ---

def _real_quoter():
    """A real GeelyApi's without_secrets(), holding invented session values."""
    from conftest import load as _load
    api_mod = _load("api")
    real = api_mod.GeelyApi.__new__(api_mod.GeelyApi)
    real.vin = FAKE_VIN
    real._jwt, real.cidpsso_token = "jwt-session-0001-abcdef", "cidpsso-token-0001"
    real.user_id, real.email = "uid-000123456", "owner@example.com"
    secret = (real._jwt, real.cidpsso_token, real.user_id, real.email, FAKE_VIN)
    return real.without_secrets, secret, "denied for " + " / ".join(secret)


def _quoted_clean(text, secret):
    for v in secret:
        assert v not in text, (v, text)


class _Lines(logging.Handler):
    """Every line logged, as a handler prints it - a traceback included,
    which record.getMessage() leaves out."""

    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append((record.levelno, logging.Formatter().format(record)))


def _quoted_failure(press, secret):
    """A command that fails, run with every line logged meanwhile captured.
    The error - as the toast shows it, and as Home Assistant prints its
    traceback at debug - and every line hold none of `secret`, and the error
    carries no chain. Returns the error and the lines."""
    from homeassistant.exceptions import HomeAssistantError
    root = logging.getLogger()
    handler, old = _Lines(), root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    failed = None
    try:
        asyncio.run(press())
    except HomeAssistantError as e:
        failed = e
    finally:
        root.removeHandler(handler)
        root.setLevel(old)
    assert failed is not None, "a failed command was taken as sent"
    _quoted_clean(str(failed), secret)
    _quoted_clean("".join(traceback.format_exception(failed)), secret)
    for _, text in handler.lines:
        _quoted_clean(text, secret)
    assert failed.__cause__ is None and (
        failed.__context__ is None or failed.__suppress_context__), \
        "a chained error prints its raw text in the traceback"
    return failed, handler.lines


def test_a_failed_poll_quotes_the_gateway_and_outside_errors_without_the_session():
    """The coordinator words its warning, UpdateFailed and the re-auth around
    an error or an answer, and quotes them through the client: a status read
    answered with a code that is not a success hands the gateway's msg back
    as data, and an error raised outside the client - a socket's - was never
    scrubbed where it was raised."""
    quote, secret, words = _real_quoter()

    def tweak(api_):
        api_.without_secrets = quote

    m = _mod()
    ok, hass, _, api, coord, _ = _setup(m, api_tweak=tweak)
    assert ok is True
    fast = types.SimpleNamespace(sleep=_instant_sleep,
                                 CancelledError=asyncio.CancelledError)
    for msg in (words, {"detail": words}):
        api.status_results = [{"code": 5000, "msg": msg}]
        try:
            with _Patched(m, asyncio=fast):
                asyncio.run(coord.refresh())
        except m.UpdateFailed as e:
            _quoted_clean(str(e), secret)
            assert "code=5000" in str(e) and f"...{FAKE_VIN[-4:]}" in str(e), str(e)
            assert e.__cause__ is None, "the raw answer must not ride along"
        else:
            raise AssertionError("an error answer was taken as data")

    root = logging.getLogger()
    handler, old = _Lines(), root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    failed = None
    try:
        api.status_results = [OSError(words) for _ in range(9)]
        for _ in range(3):
            try:
                with _Patched(m, asyncio=fast):
                    asyncio.run(coord.refresh())
            except m.UpdateFailed as e:
                failed = e
    finally:
        root.removeHandler(handler)
        root.setLevel(old)
    warned = [text for level, text in handler.lines if level >= logging.WARNING]
    assert warned and "reusing last snapshot" in warned[0], warned
    for _, text in handler.lines:
        _quoted_clean(text, secret)
    assert failed is not None, "the third failure in a row was not raised"
    _quoted_clean(str(failed), secret)
    assert failed.__cause__ is None and failed.__suppress_context__, \
        "a chained error prints its raw text in the traceback"

    m2 = _mod()
    from homeassistant.exceptions import ConfigEntryAuthFailed
    ok, hass, _, api, coord, _ = _setup(m2, api_tweak=tweak)
    api.status_results = [m2.GeelyAuthError(words)]
    try:
        asyncio.run(coord.refresh())
    except ConfigEntryAuthFailed as e:
        _quoted_clean(str(e), secret)
    else:
        raise AssertionError("a refused session did not ask for re-authentication")


_ANSWERS = {"resp", "sc", "st", "state_resp", "login_resp"}
# Every module is held to the platforms' rules but these: __init__.py (on its
# own, in the test after next), the clients (api, zeekr_client, zeekr_adapter
# - they are the scrub), and two that word their errors apart: the captcha
# solver the config flow drives (it is sent nothing of the account, and logs
# nothing) and the card registration (Home Assistant's own errors, no request
# text). A module added later is held to them unless it is named here.
_NOT_PLATFORM = {"__init__.py", "api.py", "zeekr_client.py", "zeekr_adapter.py",
                 "geetest_solver.py", "cards.py"}


def _platform_modules():
    return sorted(name for name in os.listdir(PKG)
                  if name.endswith(".py") and name not in _NOT_PLATFORM)


def _quote_rule_breaks(module, strict=False, src=None):
    """Where `module` - or `src`, given its text - quotes an error, an answer
    or the coordinator's last error without a scrub, as (line, source)
    pairs. The rules are the tests' below; `strict` adds the platforms'."""
    if src is None:
        src = open(os.path.join(PKG, module), encoding="utf-8").read()
    tree = ast.parse(src)
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    # Errors worded from this package's own text: the region keys', the
    # secrets.yaml key's, which names the file and the error's class only,
    # and the config flow's provisioning abort, which carries an abort key.
    own_text = {"RegionKeysError", "LocalRegionKeysError", "PasswordKeyError",
                "_LegacyProvisionError"}
    levels = {"debug", "info", "warning", "error", "critical", "exception", "log"}

    def name_of(call):
        f = call.func
        return f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")

    def function_of(node):
        while node in parents:
            node = parents[node]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return node
        return None

    def caught(handler):
        """The class an except block names, without its module path."""
        return "" if handler.type is None else ast.unparse(handler.type).rsplit(".", 1)[-1]

    def from_none(node):
        return isinstance(node.cause, ast.Constant) and node.cause.value is None

    def is_scrub(call):
        name = name_of(call)
        if name in {"without_secrets", "quote_failure"}:
            return True
        if name == "hide_values":
            vals = call.args[1] if len(call.args) > 1 else None
            return vals is not None and not (
                isinstance(vals, (ast.Tuple, ast.List, ast.Set)) and not vals.elts)
        if name == "mask_path":                 # VIN-shaped text only
            fn = function_of(call)
            return fn is not None and fn.name == "async_remove_entry"
        return False

    def read_safely(node):
        """An error read for its shape, its truth or its cause, or quoted
        through a scrub."""
        p = parents.get(node)
        if isinstance(p, ast.Raise) and p.cause is node:
            return True
        if isinstance(p, ast.Attribute) and p.attr in {"message", "code"}:
            return True
        if not strict and isinstance(p, ast.Assign) and all(   # the retry's
                isinstance(t, ast.Name) and t.id == "last_exc" for t in p.targets):
            return True
        if isinstance(p, (ast.If, ast.IfExp)) and p.test is node:
            return True
        up = node                       # type(e.__cause__ or e) is a type too
        while isinstance(parents.get(up), (ast.Attribute, ast.BoolOp)):
            up = parents[up]
        if (isinstance(parents.get(up), ast.Call)
                and name_of(parents[up]) in {"type", "isinstance"}):
            return True
        if isinstance(p, ast.Call):
            if name_of(p) in {"type", "isinstance"} or is_scrub(p):
                return True
            if (name_of(p) == "str" and isinstance(parents.get(p), ast.Call)
                    and is_scrub(parents[p])):
                return True
        return False

    def where(node):
        return node.lineno, ast.get_source_segment(src, parents.get(node) or node)

    bare = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute) and node.func.attr == "exception":
                bare.append((node.lineno, "logger.exception"))
            for kw in node.keywords:
                if kw.arg == "exc_info" and not (
                        isinstance(kw.value, ast.Constant) and kw.value.value is False):
                    bare.append((node.lineno, "exc_info"))
        if isinstance(node, ast.Name) and node.id == "traceback":
            bare.append((node.lineno, "traceback"))
        if (isinstance(node, ast.Attribute) and node.attr == "exc_info"
                and isinstance(node.value, ast.Name) and node.value.id == "sys"):
            bare.append((node.lineno, "sys.exc_info"))
    for handler in ast.walk(tree):
        if not isinstance(handler, ast.ExceptHandler) or not handler.name:
            continue
        if caught(handler) in own_text:
            continue
        for stmt in handler.body:
            for node in ast.walk(stmt):
                if (isinstance(node, ast.Name) and node.id == handler.name
                        and not read_safely(node)):
                    bare.append(where(node))

    def scrubbed(node, stop):
        while node is not stop and node in parents:
            node = parents[node]
            if isinstance(node, ast.Call) and name_of(node) == "without_secrets":
                return True
        return False

    sinks = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in levels and isinstance(node.func.value, ast.Name)
                and node.func.value.id in {"_LOGGER", "logger"}):
            sinks.append(node)
        if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
            sinks.append(node.exc)
    for sink in sinks:
        for node in ast.walk(sink):
            if isinstance(node, ast.Name) and node.id in _ANSWERS:
                p = parents.get(node)
                if isinstance(p, ast.Attribute) and p.attr == "keys":
                    continue
                if not scrubbed(node, sink):
                    bare.append(where(node))
    if not strict:
        return bare

    # The coordinator's last error, read in place or held in a name - as an
    # attribute or through getattr().
    for node in ast.walk(tree):
        if not ((isinstance(node, ast.Attribute) and node.attr == "last_exception"
                 and isinstance(node.ctx, ast.Load))
                or (isinstance(node, ast.Call) and name_of(node) == "getattr"
                    and len(node.args) > 1 and isinstance(node.args[1], ast.Constant)
                    and node.args[1].value == "last_exception")):
            continue
        p = parents.get(node)
        if (isinstance(p, ast.Assign) and len(p.targets) == 1
                and isinstance(p.targets[0], ast.Name)):
            held = p.targets[0].id
            for use in ast.walk(function_of(node) or tree):
                if (isinstance(use, ast.Name) and use.id == held
                        and isinstance(use.ctx, ast.Load) and not read_safely(use)):
                    bare.append(where(use))
        elif not read_safely(node):
            bare.append(where(node))
    # A raise in an except block keeps the error it caught, as its cause or
    # as its context, unless it is `from None`; a bare raise passes it on
    # whole. A refusal is the client's own words, an error of own_text this
    # package's, a cancellation has none, and a flow's abort carries its
    # reason's key, which Home Assistant shows as the abort.
    passed_on = {"AbortFlow", "CancelledError", "GeelyControlError", *own_text}
    for handler in ast.walk(tree):
        if not isinstance(handler, ast.ExceptHandler):
            continue
        kind = caught(handler)
        for stmt in handler.body:
            for node in ast.walk(stmt):
                if not isinstance(node, ast.Raise):
                    continue
                if node.exc is None:
                    if kind not in passed_on:
                        bare.append((node.lineno, "re-raised as it came"))
                elif kind != "GeelyControlError" and not from_none(node):
                    bare.append((node.lineno, "raised without `from None`"))
    # A finally block runs while an error is on its way out, and a raise
    # there takes it as context.
    for node in ast.walk(tree):
        if isinstance(node, (ast.Try, getattr(ast, "TryStar", ast.Try))):
            for stmt in node.finalbody:
                for r in ast.walk(stmt):
                    if isinstance(r, ast.Raise) and r.exc is not None and not from_none(r):
                        bare.append((r.lineno, "raised in a finally without `from None`"))

    # A call into the client runs in the executor, and whatever it raises
    # meets the rules above only inside a try that catches Exception: past
    # one, it reaches Home Assistant's "Unexpected exception", which logs a
    # traceback and shows the raw text.
    def guarded(call):
        child, up = call, parents.get(call)
        while up is not None and not isinstance(
                up, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            if (isinstance(up, ast.Try) and any(child is s for s in up.body)
                    and any(h.type is None or caught(h) in {"Exception", "BaseException"}
                            for h in up.handlers)):
                return True
            child, up = up, parents.get(up)
        return False

    threads = {"async_add_executor_job", "run_in_executor", "to_thread"}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and name_of(node) in threads
                and not guarded(node)):
            bare.append((node.lineno, "an executor call no `except Exception` meets"))
    return bare


def test_every_error_the_integration_quotes_goes_through_a_scrub():
    """__init__.py's syntax tree, held to three rules. No traceback is
    printed from it (logger.exception, exc_info, traceback, sys.exc_info):
    a traceback prints the caught error whole. An except block quotes the
    error it caught only through a scrub - the client's without_secrets(),
    hide_values() given something to hide, or mask_path() where the key
    folder is removed - and otherwise reads it only as e.message or e.code
    (worded by the client), type()/isinstance(), a `from e` cause, or keeps
    it for the retry's re-raise. The two errors worded from this package's
    own text (region keys) are left. An answer (resp, sc, st, state_resp)
    reaches a log line or a raised error only through without_secrets()."""
    bare = _quote_rule_breaks("__init__.py")
    assert not bare, bare


def test_every_error_a_platform_quotes_goes_through_a_scrub():
    """The platforms, helpers.py and every module not named apart, held to
    the same three rules and these. A raise inside an except block is `from
    None`, unless the block caught a GeelyControlError, which the client
    words itself: a `from e` cause, or the context any other raise keeps,
    prints the caught error whole in the traceback Home Assistant logs at
    debug for a failed service call - and a bare raise, or a call into the
    client no `except Exception` meets, hands it to Home Assistant raw. The
    coordinator's last error - the Refresh Data button shows it - is read
    like a caught one, and the retry's `last_exc` is __init__.py's alone.
    helpers.quote_failure() counts as a scrub: it quotes through the client
    and logs no traceback. The config flow is held to them too, its own
    without_secrets() the scrub: an error that leaves a step whole is logged
    by Home Assistant's web server with its traceback. So is the diagnostics
    report, hide_values() over what the entry and the client hold the scrub:
    it is the file an owner attaches to a public issue."""
    modules = _platform_modules()
    assert {"button.py", "climate.py", "config_flow.py", "cover.py",
            "diagnostics.py", "helpers.py", "lock.py", "select.py", "switch.py",
            "time.py"} <= set(modules), modules
    bare = [(module, *b) for module in modules
            for b in _quote_rule_breaks(module, strict=True)]
    assert not bare, bare


def test_the_platform_rules_flag_each_shape_they_name():
    """The rules are a test of their own: each shape they name, written out
    in a few lines, is flagged, and each they let through is not. A review
    found the bare raise by trying one."""
    flagged = {
        "a traceback": "_LOGGER.exception('x')",
        "exc_info": "_LOGGER.debug('x', exc_info=True)",
        "the raw text": "try:\n    a()\nexcept Exception as e:\n    raise X(f'{e}') from None",
        "a cause": "try:\n    a()\nexcept Exception as e:\n"
                   "    raise X(quote_failure(L, api, e, 'x')) from e",
        "the context": "try:\n    a()\nexcept Exception as e:\n"
                       "    raise X(quote_failure(L, api, e, 'x'))",
        "a bare raise": "try:\n    a()\nexcept Exception:\n    raise",
        "a raise in a finally": "try:\n    a()\nfinally:\n    raise X('x')",
        "the retry's hatch": "try:\n    a()\nexcept Exception as e:\n    last_exc = e",
        "an answer": "_LOGGER.debug('%s', redact(resp))",
        "a sign-in's answer": "_LOGGER.warning('%s', redact(login_resp))",
        "the last error": "def f(c):\n    err = c.last_exception\n    raise X(f'{err}')",
        "the last error by getattr":
            "def f(c):\n    raise X(str(getattr(c, 'last_exception', None)))",
        "a call nothing meets": "async def f(h):\n    await h.async_add_executor_job(g)",
        "a thread nothing meets": "async def f():\n    await asyncio.to_thread(g)",
        "a call a narrow except meets":
            "async def f(h):\n    try:\n        await h.async_add_executor_job(g)\n"
            "    except OSError:\n        pass",
    }
    allowed = {
        "the scrub": "try:\n    a()\nexcept Exception as e:\n"
                     "    raise X(quote_failure(L, api, e, 'x')) from None",
        "a refusal's words": "try:\n    a()\nexcept api.GeelyControlError as e:\n"
                             "    raise X(e.message) from e",
        "a cancellation": "try:\n    a()\nexcept asyncio.CancelledError:\n    raise",
        "a flow's abort": "try:\n    a()\nexcept AbortFlow:\n    raise",
        "an error in its own words": "try:\n    a()\nexcept RegionKeysError:\n    raise",
        "an abort's key": "try:\n    a()\nexcept _LegacyProvisionError as e:\n"
                          "    return abort(reason=e.reason)",
        "the class": "try:\n    a()\nexcept Exception as e:\n"
                     "    _LOGGER.debug('%s', type(e).__name__)",
        "a call met": "async def f(h):\n    try:\n        await h.async_add_executor_job(g)\n"
                      "    except Exception:\n        pass",
        "the last error scrubbed": "def f(c, api):\n    err = c.last_exception\n"
                                   "    raise X(api.without_secrets(err) if err else 'x')",
        "an answer scrubbed": "_LOGGER.debug('%s', api.without_secrets(redact(resp)))",
    }
    for what, src in flagged.items():
        assert _quote_rule_breaks("<case>", strict=True, src=src), what
    for what, src in allowed.items():
        assert not _quote_rule_breaks("<case>", strict=True, src=src), \
            (what, _quote_rule_breaks("<case>", strict=True, src=src))


def test_an_action_quotes_an_outside_error_without_the_session():
    """fire_control's error is shown where the action was called from, and
    an error raised outside the client was never scrubbed."""
    from homeassistant.exceptions import HomeAssistantError
    quote, secret, words = _real_quoter()
    m = _mod()
    api = _FakeApi()
    api.without_secrets = quote

    def control(*args, **kw):
        raise OSError(words)

    api.control = control
    _, run = _service(m, {"e1": {"vin": FAKE_VIN, "api": api}},
                      {"service_id": "RCT", "command": "start"})
    try:
        run()
    except HomeAssistantError as e:
        assert "fire_control RCT failed" in str(e), str(e)
        _quoted_clean(str(e), secret)
        assert e.__cause__ is None and e.__suppress_context__, \
            "a chained error prints its raw text in the traceback"
    else:
        raise AssertionError("a failed send was taken as sent")


def test_a_failed_trip_read_is_raised_without_its_chain():
    """The scheduled trip's read, failed by an error from outside the client,
    is quoted through it and raised without the chain that would print the
    raw text in a traceback."""
    from homeassistant.exceptions import HomeAssistantError
    quote, secret, words = _real_quoter()
    m = _mod()
    api = _FakeApi()
    api.without_secrets = quote

    def unreadable(_biz):
        raise OSError(words)

    api.charge_server_get = unreadable
    _, call = _service(m, {"e1": {"vin": FAKE_VIN, "api": api, "capabilities": {}}},
                       {"command": "start"}, name="set_scheduled_trip")
    try:
        call()
    except HomeAssistantError as e:
        assert "could not read the current scheduled trip" in str(e), str(e)
        _quoted_clean(str(e), secret)
        assert e.__cause__ is None and e.__suppress_context__
    else:
        raise AssertionError("an unreadable trip was taken as read")
