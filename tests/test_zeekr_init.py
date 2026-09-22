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
from test_init_lifecycle import _EntityRegistry, _ir_module


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
        # The x-vin surface the setup-time derivation uses (#84): what the
        # entry handed over, what a probe answers (or raises), what was adopted.
        self.enc_vin = kw.get("enc_vin") or ""
        self.probe_result = ""
        self.probe_exc = None
        self.probes = []
        self.adopted = []

    def probe_x_vin(self, vin):
        self.probes.append(vin)
        if self.probe_exc is not None:
            raise self.probe_exc
        return self.probe_result

    def adopt_enc_vin(self, value):
        self.adopted.append(value)
        self.enc_vin = value or ""

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


def _setup_zeekr(m, hass=None, entry=None, api_tweak=None, ir=None):
    hass = hass or _Hass()
    entry = entry or _zeekr_entry()
    ir = ir if ir is not None else _ir_module()
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
                  er=_er_module(_EntityRegistry([])), ir=ir,
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
    # A renewal that fires once the bundle is in hass.data (the normal case,
    # after setup) flags its own writeback so the update listener skips the
    # reload it would otherwise do on any entry.data change.
    bundle = hass.data["geely_connect"]["e1"]
    assert isinstance(bundle, dict)
    api._hf_takes = [("mock-hf-2", 1750000002)]
    asyncio.run(_FakeCoordinator.instance.refresh())
    assert bundle.get("_skip_reload_once") is True, \
        "the HF writeback must flag itself so the listener skips the reload"
    # A second cycle with no renewal must not touch the entry again.
    n = len(hass.config_entries.updates)
    asyncio.run(_FakeCoordinator.instance.refresh())
    assert len(hass.config_entries.updates) == n, "spurious entry update"


# ------------------------------------------ #84: the x-vin, derived at setup

def test_setup_derives_a_missing_x_vin_and_stores_it_before_the_first_refresh():
    """The flow derives once and stores "" when no build matches; nothing tried
    again, so a key pair added in a later release could only reach an owner by
    removing and re-adding the entry. Now every setup probes while the entry
    has no token, adopts a match for this session, writes it into the entry -
    and clears the Repairs issue that a failed probe raises."""
    m = _mod()
    ir = _ir_module()

    def _match(api):
        api.probe_result = "DERIVED-XVIN=="

    ok, hass, entry, api = _setup_zeekr(m, api_tweak=_match, ir=ir)
    assert ok is True
    assert api.probes == [FAKE_VIN], "the probe must be given the plain VIN"
    assert api.adopted == ["DERIVED-XVIN=="], "the adapter did not take the token"
    stored = [u["data"] for u in hass.config_entries.updates if "data" in u]
    assert stored and stored[0]["zeekr_enc_vin"] == "DERIVED-XVIN==", stored
    assert stored[0]["zeekr_access_token"] == "mock-at", "the rest of the entry must survive"
    assert ir.created == [], ir.created
    assert ("geely_connect", "x_vin_missing_e1") in ir.deleted, ir.deleted
    # No flag left behind for the update listener to eat on the user's next
    # real options change: the write happens before the listener exists.
    assert "_skip_reload_once" not in hass.data["geely_connect"]["e1"]


def test_a_probe_that_matches_no_build_raises_the_repairs_issue():
    m = _mod()
    ir = _ir_module()
    ok, hass, entry, api = _setup_zeekr(m, ir=ir)     # probe_result "" by default
    assert ok is True
    assert api.probes == [FAKE_VIN]
    assert api.adopted == []
    assert not [u for u in hass.config_entries.updates if "zeekr_enc_vin" in u.get("data", {})]
    assert len(ir.created) == 1, ir.created
    domain, issue_id, kw = ir.created[0]
    assert (domain, issue_id) == ("geely_connect", "x_vin_missing_e1")
    assert kw["translation_key"] == "x_vin_missing"
    assert kw["is_fixable"] is False and kw["severity"] == "warning"
    assert kw["translation_placeholders"] == {"name": "My EX5 (0000)"}, kw
    assert ir.deleted == [], "nothing to clear on a failed probe"


def test_an_entry_that_already_has_a_token_is_not_probed_and_the_issue_clears():
    m = _mod()
    for where in ("options", "data"):
        ir = _ir_module()
        entry = _zeekr_entry()
        if where == "options":
            entry.options = {"zeekr_enc_vin": "PASTED=="}
        else:
            entry.data = {**entry.data, "zeekr_enc_vin": "STORED=="}
        ok, hass, entry, api = _setup_zeekr(m, entry=entry, ir=ir)
        assert ok is True, where
        assert api.probes == [], (where, "a token in hand must not be re-derived")
        assert ir.created == [], where
        assert ir.deleted == [("geely_connect", "x_vin_missing_e1")], (where, ir.deleted)


def test_a_dead_session_at_the_probe_is_not_reported_as_a_missing_key():
    """`079021` answers every candidate whatever the key, so the probe raises
    and the adapter renews; when even that fails it surfaces as GeelyAuthError.
    That is a session problem for the first refresh and the re-auth prompt,
    not a key problem for Repairs - so neither branch of the issue fires."""
    m = _mod()
    ir = _ir_module()
    gae = load("api").GeelyAuthError

    def _dead(api):
        api.probe_exc = gae("logged in elsewhere")

    ok, hass, entry, api = _setup_zeekr(m, api_tweak=_dead, ir=ir)
    assert ok is True, "setup itself must carry on"
    assert api.probes == [FAKE_VIN]
    assert api.adopted == []
    assert ir.created == [] and ir.deleted == [], (ir.created, ir.deleted)
    # Any other failure is best-effort too: setup carries on, no issue either way.
    ir2 = _ir_module()

    def _broken(api):
        api.probe_exc = RuntimeError("socket closed")

    ok, hass, entry, api = _setup_zeekr(m, api_tweak=_broken, ir=ir2)
    assert ok is True
    assert ir2.created == [] and ir2.deleted == []
