"""Coordinator setup + HF persistence for zeekr-platform entries (__init__.py).

The legacy path is covered by test_init_lifecycle.py; this file drives the
CONF_PLATFORM=zeekr branch: the adapter construction in async_setup_entry
and the silent-renewal persistence in the update closure.
"""

import asyncio
import types

from conftest import FAKE_VIN, have_homeassistant, load
from run import skip

from test_init_lifecycle import _FakeCoordinator, _er_module, _instant_sleep
from test_init_lifecycle import _EntityRegistry


def _mod():
    if not have_homeassistant():
        skip("homeassistant not installed")
    return load("__init__")


class _Entries:
    def __init__(self):
        self.updates = []

    def async_entries(self, domain):
        return []

    async def async_forward_entry_setups(self, entry, platforms):
        self.forwarded = list(platforms)

    async def async_reload(self, entry_id):
        return True

    def async_update_entry(self, entry, **kw):
        self.updates.append(kw)
        for k, v in kw.items():
            setattr(entry, k, v)


class _Hass:
    def __init__(self):
        self.data = {}
        self.config_entries = _Entries()
        self.services = types.SimpleNamespace(
            has_service=lambda domain, name: False,
            async_register_admin_service=lambda *a, **k: None)
        self.config = types.SimpleNamespace(
            path=lambda *p: "/cfg/" + "/".join(p),
            time_zone="Europe/Berlin")

    async def async_add_executor_job(self, fn, *a):
        return fn(*a)


def _zeekr_entry():
    return types.SimpleNamespace(
        entry_id="e1",
        data={
            "platform": "zeekr", "email": "user@example.com",
            "country_code": "AU", "vin": FAKE_VIN, "user_id": "mock-uid",
            "zeekr_access_token": "mock-at", "zeekr_refresh_token": "mock-rt",
            "zeekr_hf_token": "mock-hf", "zeekr_hf_expiry": 1750000000,
            "zeekr_password": "hunter2",
            "vehicle_nickname": "My EX5", "vehicle_series": "E245-J1",
            "vehicle_model_code": "E245-J1", "vehicle_color": "White",
            "vehicle_power_type": "BEV",
            "pressure_unit": "kPa", "poll_mode": "normal",
        },
        options={},
        async_on_unload=lambda fn: fn,
        add_update_listener=lambda fn: (lambda: None))


class _FakeZeekrApi:
    """The adapter surface the update closure touches, with a renewal queue."""

    def __init__(self, **kw):
        self.kw = kw
        self._hf_takes = [("mock-hf-new", 1750000001)]

    def vehicle_status(self):
        return {"code": "1000", "data": {"vehicleStatus": {
            "basicVehicleStatus": {"powerLevel": 98}}}}

    def vehicle_status_state(self):
        return {"code": "1000", "data": {"sentry": "1"}}

    def charge_server_get(self, biz_type):
        return {"code": "1000", "data": {"rbcStartTime": "23:00"}}

    def scheduled_charging_set(self, **kw):
        return {"code": "1000", "data": {}}

    def rapid_climate(self, **kw):
        return {"code": "1000", "data": {}}

    def request_position_refresh(self):
        return {"code": "1000", "data": {}}

    def control(self, *a, **k):
        return {"code": "1000", "data": {}}

    def fetch_capabilities(self):
        return []

    def take_renewed_hf_token(self):
        return self._hf_takes.pop(0) if self._hf_takes else None


class _Patched:
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


def _setup_zeekr(m, hass=None, entry=None, api_tweak=None):
    hass = hass or _Hass()
    entry = entry or _zeekr_entry()
    made = {}

    def _api_factory(**kw):
        api = _FakeZeekrApi(**kw)
        if api_tweak:
            api_tweak(api)
        made["api"] = api
        return api

    registered = {}

    def _admin(hass_, domain, name, handler, schema=None):
        registered[name] = handler

    fast = types.SimpleNamespace(sleep=_instant_sleep,
                                 CancelledError=asyncio.CancelledError)
    with _Patched(m, ZeekrAdapter=_api_factory, DataUpdateCoordinator=_FakeCoordinator,
                  er=_er_module(_EntityRegistry([])),
                  dr=types.SimpleNamespace(
                      async_get=lambda h: types.SimpleNamespace(
                          async_get_device=lambda identifiers: None)),
                  async_register_admin_service=_admin,
                  asyncio=fast):
        ok = asyncio.run(m.async_setup_entry(hass, entry))
    return ok, hass, entry, made.get("api")


def test_zeekr_entry_builds_the_adapter_with_its_tokens():
    m = _mod()
    ok, hass, entry, api = _setup_zeekr(m)
    assert ok is True
    assert api is not None, "ZeekrAdapter was not constructed"
    kw = api.kw
    assert kw["email"] == "user@example.com"
    assert kw["vin"] == FAKE_VIN
    assert kw["user_id"] == "mock-uid"
    assert kw["access_token"] == "mock-at"
    assert kw["refresh_token"] == "mock-rt"
    assert kw["hf_token"] == "mock-hf"
    assert kw["hf_expiry"] == 1750000000
    assert kw["password"] == "hunter2", "plaintext stored password passes through"
    assert kw["country_code"] == "AU"
    assert kw["timezone"] == "Europe/Berlin", \
        "the HA time zone must drive the HF timezone header"
    assert kw["vehicle_model"] == "E245-J1"


def test_zeekr_hf_renewal_is_persisted_into_the_entry():
    m = _mod()
    ok, hass, entry, api = _setup_zeekr(m)
    assert ok is True
    asyncio.run(_FakeCoordinator.instance.refresh())
    assert hass.config_entries.updates, "no entry update after the renewal"
    last = hass.config_entries.updates[-1]["data"]
    assert last["zeekr_hf_token"] == "mock-hf-new", last
    assert last["zeekr_hf_expiry"] == 1750000001, last
    # A second cycle with no renewal must not touch the entry again.
    n = len(hass.config_entries.updates)
    asyncio.run(_FakeCoordinator.instance.refresh())
    assert len(hass.config_entries.updates) == n, "spurious entry update"
