"""Coordinator setup + HF persistence for zeekr-platform entries (__init__.py).

The legacy path is covered by test_init_lifecycle.py; this file drives the
CONF_PLATFORM=zeekr branch: the adapter construction in async_setup_entry
and the silent-renewal persistence in the update closure.
"""

import asyncio
import logging
import types
import json
from pathlib import Path
import tempfile

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
        self._hf_takes = [("mock-hf-new", 1750000001, "mock-at-new", "mock-rt-new")]
        # The x-vin surface the setup-time derivation uses (#84): what the
        # entry handed over, what a probe answers (or raises), what was adopted.
        self.enc_vin = kw.get("enc_vin") or ""
        self.probe_result = ""
        self.probe_exc = None
        self.probes = []
        self.adopted = []
        self.listed = True
        self.listed_exc = None
        self.car_refused = False
        # What the probe drew when no build matched; "" models a probe that
        # tried none (no session to ask on), which gives no advice.
        self.probe_summary = ""

    def probe_x_vin(self, vin):
        self.probes.append(vin)
        if self.probe_exc is not None:
            raise self.probe_exc
        return self.probe_result

    def new_gateway_lists_vehicle(self):
        if self.listed_exc is not None:
            raise self.listed_exc
        return self.listed

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

    def take_renewed_session(self):
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


def _setup_zeekr(m, hass=None, entry=None, api_tweak=None, ir=None, factory=None):
    hass = hass or _Hass()
    entry = entry or _zeekr_entry()
    ir = ir if ir is not None else _ir_module()
    made = {}

    def _api_factory(**kw):
        api = (factory or _FakeZeekrApi)(**kw)
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


def test_european_entry_restores_from_the_local_file_without_storing_app_keys():
    from test_zeekr_europe import FAKE_KEYS, FAKE_PROFILE

    m = _mod()
    entry = _zeekr_entry()
    entry.data.update({"country_code": "ES", "zeekr_region": "EU"})
    hass = _Hass()
    with tempfile.TemporaryDirectory() as root:
        (Path(root) / "geely_eu_app_keys.json").write_text(json.dumps(FAKE_KEYS))
        hass.config.path = lambda *p: str(Path(root).joinpath(*p))
        ok, hass, entry, api = _setup_zeekr(m, hass=hass, entry=entry)
    assert ok
    assert api.kw["profile"] == FAKE_PROFILE
    assert api.kw["country_code"] == "ES"
    assert api.kw["access_token"] == "mock-at"
    for value in FAKE_KEYS.values():
        assert value not in repr(entry.data)
        assert value not in repr(hass.config_entries.updates)


def test_european_restore_waits_for_local_keys_before_constructing_the_adapter():
    m = _mod()
    for contents in (None, "not-json"):
        entry = _zeekr_entry()
        entry.data.update({"country_code": "ES", "zeekr_region": "EU"})
        hass = _Hass()
        with tempfile.TemporaryDirectory() as root:
            if contents is not None:
                (Path(root) / "geely_eu_app_keys.json").write_text(contents)
            hass.config.path = lambda *p: str(Path(root).joinpath(*p))
            try:
                _setup_zeekr(m, hass=hass, entry=entry,
                             api_tweak=lambda _: (_ for _ in ()).throw(
                                 AssertionError("Adapter must not be constructed")))
            except m.ConfigEntryNotReady as exc:
                assert "geely_eu_app_keys.json" in str(exc)
            else:
                raise AssertionError("Entry must wait for valid local keys")
        assert not hass.config_entries.updates


def test_zeekr_hf_renewal_is_persisted_into_the_entry():
    m = _mod()
    ok, hass, entry, api = _setup_zeekr(m)
    assert ok is True
    asyncio.run(_FakeCoordinator.instance.refresh())
    assert hass.config_entries.updates, "no entry update after the renewal"
    last = hass.config_entries.updates[-1]["data"]
    assert last["zeekr_hf_token"] == "mock-hf-new", last
    assert last["zeekr_hf_expiry"] == 1750000001, last
    assert last["zeekr_access_token"] == "mock-at-new", last
    assert last["zeekr_refresh_token"] == "mock-rt-new", last
    # A renewal that fires once the bundle is in hass.data (the normal case,
    # after setup) flags its own writeback so the update listener skips the
    # reload it would otherwise do on any entry.data change.
    bundle = hass.data["geely_connect"]["e1"]
    assert isinstance(bundle, dict)
    api._hf_takes = [("mock-hf-2", 1750000002, "mock-at-2", "")]
    asyncio.run(_FakeCoordinator.instance.refresh())
    assert bundle.get("_skip_reload_once") is True, \
        "the HF writeback must flag itself so the listener skips the reload"
    last = hass.config_entries.updates[-1]["data"]
    assert last["zeekr_access_token"] == "mock-at-2", last
    assert last["zeekr_refresh_token"] == "mock-rt-new", \
        "a login that returned no refresh token must not blank the stored one"
    # A second cycle with no renewal must not touch the entry again.
    n = len(hass.config_entries.updates)
    asyncio.run(_FakeCoordinator.instance.refresh())
    assert len(hass.config_entries.updates) == n, "spurious entry update"


def test_a_renewal_puts_the_session_it_minted_into_the_entry():
    """login_tsp re-mints the new-platform access and refresh tokens with the
    HF JWT, and the entry took only the HF half - so a restart loaded the pair
    from setup, which HA had itself replaced. Driven through the real adapter,
    so the take and the write are checked together."""
    from test_zeekr_adapter import _make_adapter, _patch_idaas, _restore_idaas

    def _real(**kw):
        a, c = _make_adapter(password="hunter2", hf_expiry=1)   # expired
        c.probe_x_vin = lambda vin: ""
        return a

    m = _mod()
    _patch_idaas()
    try:
        ok, hass, entry, api = _setup_zeekr(m, factory=_real)
    finally:
        _restore_idaas()
    assert ok is True
    assert entry.data["zeekr_hf_token"] == "mock-hf-new", entry.data
    assert entry.data["zeekr_access_token"] == "mock-at-new", entry.data
    assert entry.data["zeekr_refresh_token"] == "mock-rt-new", entry.data


# ------------------------------------------ #84: the x-vin, derived at setup

class _Records(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


def _with_logs(fn):
    """Run fn with every logger's records captured from debug up, so a test
    sees what the probe and setup told the owner between them."""
    root = logging.getLogger()
    handler, old = _Records(), root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        return fn(), handler.records
    finally:
        root.removeHandler(handler)
        root.setLevel(old)


def _token_warnings(records):
    """This integration's warnings that mention a token - what an owner on a
    default log level reads and acts on - as (logger, message) pairs."""
    return [(r.name, r.getMessage()) for r in records
            if r.levelno >= logging.WARNING and r.name.startswith("gc.")
            and "token" in r.getMessage().lower()]


# Shaped like what the real probe summarises when every known build drew
# 079025 (the build names are shortened here).
_SUMMARY = ("PR #57: ZeekrApiError: HTTP 400: 079025 Decrypt X-VIN failed; "
            "Geely Global EM: ZeekrApiError: HTTP 400: 079025 Decrypt X-VIN failed")


def _no_build_matched(api):
    api.probe_summary = _SUMMARY


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
    """The new gateway lists the car and no known build's token was accepted:
    a token can be missing, so setup warns where to paste it, once, naming
    what each build drew - a default log shows warnings only, and that detail
    is what #63 and #84 were read from - and raises the Repairs issue that
    says the same."""
    m = _mod()
    ir = _ir_module()
    (ok, hass, entry, api), records = _with_logs(
        lambda: _setup_zeekr(m, api_tweak=_no_build_matched, ir=ir))
    assert ok is True
    advice = _token_warnings(records)
    assert len(advice) == 1, advice
    name, text = advice[0]
    assert name.endswith("__init__"), "the advice is setup's, after the garage"
    assert "Configure -> Vehicle token" in text and "retried" in text, text
    assert _SUMMARY in text, text
    assert FAKE_VIN not in text
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
    # A probe that tried no build at all (no session to ask on) gives the log
    # nothing to name, so no advice goes there; the Repairs issue still goes
    # up on that path, as it did before the advice moved to setup.
    ir2 = _ir_module()
    (ok, hass, entry, api), records = _with_logs(lambda: _setup_zeekr(m, ir=ir2))
    assert ok is True
    assert [i[1] for i in ir2.created] == ["x_vin_missing_e1"], ir2.created
    assert _token_warnings(records) == [], _token_warnings(records)


def test_a_car_the_new_gateway_does_not_list_raises_no_repairs_issue():
    """An account on the new app whose car the new gateway does not list is
    served by plain VIN on the old route: no build can match, and no token is
    missing - a pasted one fails every read with 079001. The issue would send
    its owner to break a working entry, so it is cleared instead."""
    m = _mod()
    ir = _ir_module()

    def _unlisted(api):
        _no_build_matched(api)
        api.listed = False

    (ok, hass, entry, api), records = _with_logs(
        lambda: _setup_zeekr(m, api_tweak=_unlisted, ir=ir))
    assert ok is True
    assert api.probes == [FAKE_VIN]
    assert ir.created == [], ir.created
    assert ir.deleted == [("geely_connect", "x_vin_missing_e1")], ir.deleted
    # Nor does the log send its owner there: the paste advice used to come at
    # every start, whatever the garage said.
    assert _token_warnings(records) == [], _token_warnings(records)
    said = [r.getMessage() for r in records
            if r.levelno == logging.INFO and "does not list" in r.getMessage()]
    assert len(said) == 1 and "needs no vehicle token" in said[0], said


def test_a_car_the_new_gateway_turns_away_gets_no_repairs_issue():
    """079001: the gateway turned the car away, not the token. On the cars seen
    so far the app reads such a car by its plain VIN, the old way (#106), so
    the issue's "paste the token from your phone" sends its owner after
    something that is not there. v1.56.0 raised it for exactly these cars
    (#63, #84), so the upgrade has to take it down, not only stop raising it."""
    m = _mod()
    ir = _ir_module()

    def _refused(api):
        api.car_refused = True

    (ok, hass, entry, api), records = _with_logs(
        lambda: _setup_zeekr(m, api_tweak=_refused, ir=ir))
    assert ok is True
    assert api.probes == [FAKE_VIN], "the derivation still runs at every start"
    assert api.adopted == []
    assert ir.created == [], ir.created
    assert ir.deleted == [("geely_connect", "x_vin_missing_e1")], ir.deleted
    assert _token_warnings(records) == [], _token_warnings(records)


def test_a_failed_vehicle_list_keeps_the_repairs_issue():
    """On doubt the issue stays, so a car that has moved to the new gateway
    keeps the one hint that says where its token goes. The log says what is
    known instead of the advice: on a car the new gateway does not hold, one
    hiccup at start would otherwise send its owner to paste a token that
    fails every read with 079001. A garage answer that cannot tell - in no
    shape the client reads, or with no recognisable VIN - is the same doubt."""
    m = _mod()
    for why, tweak in (
            ("RuntimeError", lambda api: setattr(api, "listed_exc", RuntimeError("gateway down"))),
            ("unreadable answer", lambda api: setattr(api, "listed", None))):
        ir = _ir_module()

        def _doubt(api, tweak=tweak):
            _no_build_matched(api)
            tweak(api)

        (ok, hass, entry, api), records = _with_logs(
            lambda: _setup_zeekr(m, api_tweak=_doubt, ir=ir))
        assert ok is True, why
        assert [i[1] for i in ir.created] == ["x_vin_missing_e1"], (why, ir.created)
        assert ir.deleted == [], (why, ir.deleted)
        said = _token_warnings(records)
        assert len(said) == 1, (why, said)
        name, text = said[0]
        assert name.endswith("__init__"), name
        assert f"holds this car ({why})" in text and "not known yet" in text, text
        assert _SUMMARY in text, text
        assert "gateway down" not in text, "the exception type only, not its text"
        assert "Paste" not in text and "Configure" not in text, text


def test_the_probe_and_setup_agree_on_a_car_the_new_gateway_does_not_hold():
    """Every known build's token drew 079025. The probe cannot tell an app
    build nobody has captured from a car the new gateway does not hold, and
    only the second has been seen without a token - where pasting one turns
    every read into 079001 (#106). Driven through the real adapter and
    client, so both modules' lines are read together: with the garage empty
    there is no warning at all and no Repairs issue; with the car in it,
    the one warning is setup's, and the issue goes up; with an answer the
    client cannot read, the issue goes up and the warning gives no advice."""
    from test_zeekr_adapter import _make_adapter
    from test_zeekr_x_vin import _REJECTED

    m = _mod()
    zc = load("zeekr_client")
    for garage, held in (([], False), ([{"vin": FAKE_VIN}], True), (None, None)):
        def _real(**kw):
            a, c = _make_adapter(password="hunter2")

            def caps():
                raise zc.ZeekrApiError(_REJECTED)

            c.capabilities_new = caps
            c.vehicle_list_bff = lambda: garage
            return a

        ir = _ir_module()
        (ok, hass, entry, api), records = _with_logs(
            lambda: _setup_zeekr(m, factory=_real, ir=ir))
        assert ok is True, held
        advice = _token_warnings(records)
        if held is None:
            assert len(advice) == 1, advice
            name, text = advice[0]
            assert name.endswith("__init__"), "the one warning is setup's"
            assert "unreadable answer" in text and "079025" in text, text
            assert "Configure" not in text, text
            assert [i[1] for i in ir.created] == ["x_vin_missing_e1"], ir.created
        elif held:
            assert len(advice) == 1, advice
            name, text = advice[0]
            assert name.endswith("__init__"), "the one warning is setup's"
            assert "Configure -> Vehicle token" in text and "079025" in text, text
            assert [i[1] for i in ir.created] == ["x_vin_missing_e1"], ir.created
        else:
            assert advice == [], advice
            assert not [r for r in records if r.levelno >= logging.WARNING
                        and r.name.endswith(("zeekr_client", "__init__"))], \
                [r.getMessage() for r in records if r.levelno >= logging.WARNING]
            assert ir.created == [], ir.created
            assert ("geely_connect", "x_vin_missing_e1") in ir.deleted, ir.deleted
        # What the probe found is still said, at info, either way.
        summary = [r.getMessage() for r in records if r.levelno == logging.INFO
                   and "none of the" in r.getMessage()]
        assert len(summary) == 1 and "079025" in summary[0], (held, summary)
        for r in records:
            assert FAKE_VIN not in r.getMessage(), r.getMessage()


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
    """A candidate that met `079021` was never judged, so the probe raises and
    the adapter renews; when even that fails it surfaces as GeelyAuthError.
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


def test_old_new_app_entries_keep_sea_regardless_of_saved_country_or_local_file():
    from unittest.mock import patch

    m = _mod()
    for country in (None, "GB", "FR", "HU", "MD", "DE", "NL", "ES", "BE", "LU", "AU"):
        for explicit_sea in (False, True):
            entry = _zeekr_entry()
            if country is None:
                entry.data.pop("country_code")
            else:
                entry.data["country_code"] = country
            if explicit_sea:
                entry.data["zeekr_region"] = "SEA"
            with patch.object(Path, "open", side_effect=AssertionError("Must not read EU keys")):
                ok, _, _, api = _setup_zeekr(m, entry=entry)
            assert ok
            assert api.kw["profile"] is None
            assert api.kw["access_token"] == "mock-at"
