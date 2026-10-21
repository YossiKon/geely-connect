"""The new-platform (Zeekr) config-flow steps.

The legacy flow is upstream's contract (async_step_user stays the legacy
form); this file covers the platform field that routes new-platform accounts
to the zeekr login, the zeekr steps themselves, and the in-place migration
path that re-stamps an existing entry.
"""

import asyncio

from conftest import FAKE_VIN, have_homeassistant, load
from run import skip


def _cf():
    if not have_homeassistant():
        skip("homeassistant not installed")
    return load("config_flow")


# The real _zeekr_login_password, captured at module load: other tests in
# this file replace the module attribute with lambdas, and those replacements
# persist for the rest of the run.
if have_homeassistant():
    _REAL_LOGIN_HELPER = _cf()._zeekr_login_password
else:
    _REAL_LOGIN_HELPER = None


def _flow(entries=None):
    cf = _cf()
    flow = cf.GeelyIntlConfigFlow()

    class _Entries:
        @staticmethod
        def async_entries(domain):
            return entries or []

        class flow:
            @staticmethod
            def async_progress_by_handler(handler, *a, **k):
                return []

        @staticmethod
        def async_entry_for_domain_unique_id(domain, unique_id):
            return None

        @staticmethod
        def async_get_entry(entry_id):
            return (entries or [None])[0]

        @staticmethod
        def async_get_known_entry(entry_id):
            return (entries or [None])[0]

        @staticmethod
        def async_update_entry(entry, **kw):
            for k, v in kw.items():
                setattr(entry, k, v)

        @staticmethod
        async def async_reload(entry_id):
            return True

    class _Hass:
        config_entries = _Entries()

        class config:
            @staticmethod
            def path(p):
                return "/config/" + p

        @staticmethod
        async def async_add_executor_job(fn, *args):
            return fn(*args)

    flow.hass = _Hass()
    flow.context = dict(flow.context)  # HA's is a mappingproxy
    return cf, flow


class _FakeVehicle:
    def __init__(self, vin=FAKE_VIN, nickname="Mock EX5"):
        self._vin, self._nickname = vin, nickname

    def list_vehicles(self, user_id):
        return [{"vin": self._vin, "nickName": self._nickname,
                 "appModelCode": "E245-J1", "engineType": "BEV"}]


class _FakeClient:
    def __init__(self, vin=FAKE_VIN, n_vehicles=1, token="mock-tv"):
        self.access_token = "mock-at"
        self.refresh_token = "mock-rt"
        self.hf_token = "mock-hf"
        self.user_id = "mock-uid"
        self._vin, self._n, self._token = vin, n_vehicles, token

    def list_vehicles(self, user_id):
        return [{"vin": self._vin, "nickName": "Mock EX5",
                 "appModelCode": "E245-J1", "engineType": "BEV"}
                for _ in range(self._n)]

    def list_vehicles_bff(self):
        # The new-platform garage probe, fired only when list_vehicles is
        # empty. By default a car found on the old platform means this is
        # never reached; a fake with an empty old garage returns empty here
        # too unless it is specifically modelling a migrated account.
        return []


def _zeekr_login_input(over=None):
    data = {"email": "user@example.com", "password": "hunter2",
            "country_code": "AU", "pressure_unit": "kPa",
            "poll_mode": "normal", "store_password": False}
    data.update(over or {})
    return data


def _legacy_entry():
    return type("_Entry", (), {
        "entry_id": "e1",
        "title": "Mock Geely",
        "data": {"email": "user@example.com", "vin": "LJXK0EX50N00000001",
                 "cidpsso_token": "old-token", "user_id": "old-uid",
                 "cert_path": "/certs/c.pem", "key_path": "/certs/k.pem",
                 "device_id": "d1", "device_idfa": "a", "device_idfv": "v",
                 "region": "APAC", "country_code": "AU"},
        "options": {},
    })()


def test_the_user_form_carries_the_platform_field():
    cf, flow = _flow()
    res = asyncio.run(flow.async_step_user(None))
    keys = {str(k) for k in res["data_schema"].schema}
    assert "platform" in keys, "the setup form must offer the platform choice"
    assert res["step_id"] == "user"


def test_picking_zeekr_routes_to_the_zeekr_login_form():
    cf, flow = _flow()
    res = asyncio.run(flow.async_step_user({"platform": "zeekr"}))
    assert res["type"] == "form" and res["step_id"] == "zeekr_login"


def test_zeekr_login_creates_a_stamped_entry_without_storing_the_password():
    cf, flow = _flow()
    cf._zeekr_login_password = lambda e, p, c: _FakeClient()
    res = asyncio.run(flow.async_step_user({"platform": "zeekr"}))
    assert res["step_id"] == "zeekr_login"
    res = asyncio.run(flow.async_step_zeekr_login(_zeekr_login_input()))
    assert res["type"] == "create_entry", res
    data = res["data"]
    assert data["platform"] == "zeekr", "entry must be stamped zeekr"
    assert data["email"] == "user@example.com"
    assert data["vin"] == FAKE_VIN
    assert data["zeekr_access_token"] == "mock-at"
    assert data["zeekr_hf_token"] == "mock-hf"
    assert data["zeekr_password"] == "", "store_password=False must not store it"


def test_zeekr_login_stores_the_password_when_consented():
    cf, flow = _flow()
    cf._zeekr_login_password = lambda e, p, c: _FakeClient()
    asyncio.run(flow.async_step_user({"platform": "zeekr"}))
    res = asyncio.run(flow.async_step_zeekr_login(
        _zeekr_login_input({"store_password": True})))
    assert res["type"] == "create_entry", res
    assert res["data"]["zeekr_password"], "consented password must be stored"


def test_zeekr_login_rejects_bad_credentials_with_an_error():
    cf, flow = _flow()
    cf._zeekr_login_password = lambda e, p, c: (_ for _ in ()).throw(
        cf.ZeekrAuthError("login rejected"))
    asyncio.run(flow.async_step_user({"platform": "zeekr"}))
    res = asyncio.run(flow.async_step_zeekr_login(_zeekr_login_input()))
    assert res["type"] == "form" and res["errors"]["base"] == "invalid_credentials"


def test_zeekr_login_with_no_vehicles_shows_an_error():
    cf, flow = _flow()

    class _Empty(_FakeClient):
        def list_vehicles(self, user_id):
            return []

    cf._zeekr_login_password = lambda e, p, c: _Empty()
    asyncio.run(flow.async_step_user({"platform": "zeekr"}))
    res = asyncio.run(flow.async_step_zeekr_login(_zeekr_login_input()))
    assert res["type"] == "form" and res["errors"]["base"] == "no_vehicles"


def test_a_migrated_account_is_found_on_the_new_platform_garage():
    """The new fallback: an account migrated to the new app can have an empty
    OLD-platform garage while its car is listed on the new gateway. When
    list_vehicles is empty, the ms-app-bff probe runs, and a car there completes
    setup instead of failing with no_vehicles. An account that lists a car on
    the old platform never reaches this branch."""
    cf, flow = _flow()

    class _Migrated(_FakeClient):
        def list_vehicles(self, user_id):
            return []                      # old platform: empty

        def list_vehicles_bff(self):
            return [{"vin": FAKE_VIN, "nickName": "Migrated EX5",
                     "appModelCode": "E22H-GP", "engineType": "BEV"}]

    cf._zeekr_login_password = lambda e, p, c: _Migrated()
    asyncio.run(flow.async_step_user({"platform": "zeekr"}))
    res = asyncio.run(flow.async_step_zeekr_login(_zeekr_login_input()))
    assert res["type"] == "create_entry", res
    assert res["data"]["vin"] == FAKE_VIN


def test_zeekr_login_with_multiple_vehicles_picks_one():
    cf, flow = _flow()
    cf._zeekr_login_password = lambda e, p, c: _FakeClient(n_vehicles=2)
    asyncio.run(flow.async_step_user({"platform": "zeekr"}))
    res = asyncio.run(flow.async_step_zeekr_login(_zeekr_login_input()))
    assert res["type"] == "form" and res["step_id"] == "zeekr_pick", res
    res = asyncio.run(flow.async_step_zeekr_pick({"vin": FAKE_VIN}))
    assert res["type"] == "create_entry", res
    assert res["data"]["vin"] == FAKE_VIN


def test_zeekr_migration_restamps_and_purges_legacy_keys():
    cf, flow = _flow()
    entry = _legacy_entry()
    flow._reauth_entry = entry
    cf._zeekr_login_password = lambda e, p, c: _FakeClient()
    asyncio.run(flow.async_step_user({"platform": "zeekr"}))
    res = asyncio.run(flow.async_step_zeekr_login(_zeekr_login_input()))
    assert res["type"] == "abort" and res["reason"] == "reauth_successful", res
    data = entry.data
    assert data["platform"] == "zeekr", "migration must stamp the platform"
    assert data["vin"] == FAKE_VIN, "VIN must be re-pointed at the new record"
    for legacy in ("cidpsso_token", "cert_path", "key_path",
                   "device_id", "device_idfa", "device_idfv", "region"):
        assert legacy not in data, f"legacy key {legacy} survived the purge"
    assert data["zeekr_access_token"] == "mock-at"
    assert data["user_id"] == "mock-uid"


def test_zeekr_reconfigure_aborts_with_reconfigure_successful():
    cf, flow = _flow()
    entry = _legacy_entry()
    flow._reauth_entry = entry
    flow.context = {"source": "reconfigure"}
    cf._zeekr_login_password = lambda e, p, c: _FakeClient()
    asyncio.run(flow.async_step_user({"platform": "zeekr"}))
    res = asyncio.run(flow.async_step_zeekr_login(_zeekr_login_input()))
    assert res["type"] == "abort" and res["reason"] == "reconfigure_successful", res


def test_zeekr_reauth_prefills_and_persists_the_form_defaults():
    cf, flow = _flow()
    entry = _legacy_entry()
    entry.data.update({"pressure_unit": "psi", "poll_mode": "manual",
                       "platform": "zeekr", "zeekr_access_token": "old-at"})
    flow._reauth_entry = entry
    cf._zeekr_login_password = lambda e, p, c: _FakeClient()
    res = asyncio.run(flow.async_step_zeekr_login(None))
    schema = res["data_schema"].schema
    defaults = {}
    for k, v in schema.items():
        d = getattr(k, "default", None)
        if d is not None and not isinstance(d, type(cf.vol.UNDEFINED)):
            defaults[str(k)] = d() if callable(d) else d
    assert defaults.get("pressure_unit") == "psi", defaults
    assert defaults.get("poll_mode") == "manual", defaults
    res = asyncio.run(flow.async_step_zeekr_login(
        _zeekr_login_input({"pressure_unit": "bar", "poll_mode": "eco"})))
    assert res["reason"] == "reauth_successful", res
    assert entry.data["pressure_unit"] == "bar", "changed unit must persist"
    assert entry.data["poll_mode"] == "eco", "changed mode must persist"


def test_zeekr_new_entry_for_a_known_vin_aborts():
    existing = _legacy_entry()
    existing.data["vin"] = FAKE_VIN
    cf, flow = _flow(entries=[existing])
    cf._zeekr_login_password = lambda e, p, c: _FakeClient()
    asyncio.run(flow.async_step_user({"platform": "zeekr"}))
    res = asyncio.run(flow.async_step_zeekr_login(_zeekr_login_input()))
    assert res["type"] == "abort" and res["reason"] == "already_configured", res


def test_reauth_of_a_zeekr_entry_routes_to_the_zeekr_login():
    entry = _legacy_entry()
    entry.data["platform"] = "zeekr"
    cf, flow = _flow(entries=[entry])
    flow._reauth_entry = entry
    flow.context = {"entry_id": entry.entry_id}
    res = asyncio.run(flow.async_step_reauth(entry.data))
    assert res["type"] == "form" and res["step_id"] == "zeekr_login", res


def test_reconfigure_shows_the_backend_picker():
    cf, flow = _flow()
    entry = _legacy_entry()
    flow.context = {"entry_id": "e1"}
    flow.hass.config_entries.async_get_entry = lambda eid: entry
    res = asyncio.run(flow.async_step_reconfigure(None))
    assert res["type"] == "form" and res["step_id"] == "platform", res
    res = asyncio.run(flow.async_step_reconfigure({"platform": "zeekr"}))
    assert res["step_id"] == "zeekr_login", res


class _BadUidClient(_FakeClient):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.user_id = "bad/uid!"


class _BoomClient(_FakeClient):
    def list_vehicles(self, user_id):
        raise RuntimeError("boom")


def test_zeekr_login_with_a_malformed_user_id_errors():
    cf, flow = _flow()
    cf._zeekr_login_password = lambda e, p, c: _BadUidClient()
    asyncio.run(flow.async_step_user({"platform": "zeekr"}))
    res = asyncio.run(flow.async_step_zeekr_login(_zeekr_login_input()))
    assert res["type"] == "form" and res["errors"]["base"] == "unknown", res


def test_zeekr_login_with_a_crashing_vehicle_list_reads_as_unreachable():
    cf, flow = _flow()
    cf._zeekr_login_password = lambda e, p, c: _BoomClient()
    asyncio.run(flow.async_step_user({"platform": "zeekr"}))
    res = asyncio.run(flow.async_step_zeekr_login(_zeekr_login_input()))
    assert res["type"] == "form" and res["errors"]["base"] == "network_unreachable", res


def test_zeekr_pick_with_an_unknown_vin_aborts():
    cf, flow = _flow()
    cf._zeekr_login_password = lambda e, p, c: _FakeClient(n_vehicles=2)
    asyncio.run(flow.async_step_user({"platform": "zeekr"}))
    asyncio.run(flow.async_step_zeekr_login(_zeekr_login_input()))
    res = asyncio.run(flow.async_step_zeekr_pick({"vin": "L6T00000000000099"}))
    assert res["type"] == "abort" and res["reason"] == "unknown", res


def test_finish_zeekr_refuses_a_malformed_vin():
    cf, flow = _flow()
    cf._zeekr_login_password = lambda e, p, c: _FakeClient()
    asyncio.run(flow.async_step_user({"platform": "zeekr"}))
    res = asyncio.run(flow.async_step_zeekr_login(
        _zeekr_login_input()))
    assert res["type"] == "create_entry", res  # the normal path still works
    flow2 = cf.GeelyIntlConfigFlow()
    flow2.hass = flow.hass
    flow2.context = dict(flow.context)
    flow2._zeekr_tokens = ("mock-at", "mock-rt")
    flow2._zeekr_hf_token = "mock-hf"
    flow2._zeekr_password = ""
    flow2._user_id = "mock-uid"
    flow2._email = "user@example.com"
    flow2._country_code = "AU"
    flow2._pressure_unit = "kPa"
    flow2._poll_mode = "normal"
    res2 = asyncio.run(flow2._finish_zeekr({"vin": "../../etc/passwd"}))
    assert res2["type"] == "abort" and res2["reason"] == "unknown", res2


def test_zeekr_reauth_with_a_different_email_aborts():
    cf, flow = _flow()
    entry = _legacy_entry()
    entry.data["email"] = "other@example.com"
    flow._reauth_entry = entry
    cf._zeekr_login_password = lambda e, p, c: _FakeClient()
    asyncio.run(flow.async_step_user({"platform": "zeekr"}))
    res = asyncio.run(flow.async_step_zeekr_login(_zeekr_login_input()))
    assert res["type"] == "abort" and res["reason"] == "reauth_account_mismatch", res


def test_reconfigure_without_an_entry_falls_back_to_setup():
    cf, flow = _flow()
    flow.context = {}
    res = asyncio.run(flow.async_step_reconfigure(None))
    assert res["type"] == "form" and res["step_id"] == "user", res


def test_the_login_helper_runs_the_idaas_and_tsp_legs():
    cf, flow = _flow()

    class _Idaas:
        def __init__(self, country="AU", profile=None):
            pass

        def login_by_email_password(self, email, password):
            return "mock-tv"

    class _Client:
        def __init__(self, email="", password="", **kw):
            self.access_token = "mock-at"
            self.refresh_token = "mock-rt"
            self.hf_token = "mock-hf"
            self.user_id = "mock-uid"

        def login_tsp(self, token_value):
            pass

    saved_i, saved_c = cf.ZeekrIdaas, cf.ZeekrClient
    cf.ZeekrIdaas, cf.ZeekrClient = _Idaas, _Client
    try:
        c = _REAL_LOGIN_HELPER("user@example.com", "hunter2", "AU")
        assert c.access_token == "mock-at"
        assert c.hf_token == "mock-hf"
        assert c.user_id == "mock-uid"
    finally:
        cf.ZeekrIdaas, cf.ZeekrClient = saved_i, saved_c


def test_reconfigure_choosing_legacy_goes_to_the_legacy_form():
    cf, flow = _flow()
    entry = _legacy_entry()
    flow.context = {"entry_id": "e1"}
    flow.hass.config_entries.async_get_entry = lambda eid: entry
    res = asyncio.run(flow.async_step_reconfigure({"platform": "legacy"}))
    assert res["type"] == "form" and res["step_id"] == "user", res


class _DerivingClient(_FakeClient):
    """A logged-in client whose app build's x-vin the gateway accepts."""
    def probe_x_vin(self, vin):
        return "DERIVED-XVIN=="


def test_zeekr_login_auto_derives_and_stores_the_x_vin():
    cf, flow = _flow()
    cf._zeekr_login_password = lambda e, p, c: _DerivingClient()
    asyncio.run(flow.async_step_user({"platform": "zeekr"}))
    res = asyncio.run(flow.async_step_zeekr_login(_zeekr_login_input()))
    assert res["type"] == "create_entry", res
    assert res["data"]["zeekr_enc_vin"] == "DERIVED-XVIN==", (
        "a verified derived x-vin must be stored so the car works without a "
        "manual token")


def test_zeekr_migration_stores_the_derived_x_vin():
    cf, flow = _flow()
    entry = _legacy_entry()
    flow._reauth_entry = entry
    cf._zeekr_login_password = lambda e, p, c: _DerivingClient()
    asyncio.run(flow.async_step_user({"platform": "zeekr"}))
    res = asyncio.run(flow.async_step_zeekr_login(_zeekr_login_input()))
    assert res["type"] == "abort" and res["reason"] == "reauth_successful", res
    assert entry.data["zeekr_enc_vin"] == "DERIVED-XVIN=="


def test_european_setup_and_reauth_load_local_keys_without_persisting_them():
    import json
    from pathlib import Path
    import tempfile
    from unittest.mock import patch
    from test_zeekr_europe import FAKE_KEYS, FAKE_PROFILE

    for reauth in (False, True):
        cf, flow = _flow()
        entry = _legacy_entry()
        if reauth:
            entry.data.update({"platform": "zeekr", "country_code": "ES", "zeekr_region": "EU"})
            flow._reauth_entry = entry
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "geely_eu_app_keys.json").write_text(json.dumps(FAKE_KEYS))
            with patch.object(flow.hass.config, "path", return_value=root), \
                    patch.object(cf, "_zeekr_login_password",
                                 return_value=_DerivingClient()) as login:
                result = asyncio.run(flow.async_step_zeekr_login(
                    _zeekr_login_input({"country_code": " es ", "zeekr_region": "EU"})))
            login.assert_called_once_with(
                "user@example.com", "hunter2", "ES", FAKE_PROFILE)
        if reauth:
            assert result["reason"] == "reauth_successful", result
            stored = entry.data
        else:
            assert result["type"] == "create_entry", result
            stored = result["data"]
        assert stored["country_code"] == "ES"
        assert stored["zeekr_region"] == "EU"
        assert stored["zeekr_enc_vin"] == "DERIVED-XVIN=="
        for value in FAKE_KEYS.values():
            assert value not in repr(stored)


def test_missing_or_invalid_eu_keys_show_setup_error_without_logging_in():
    from pathlib import Path
    import tempfile
    from unittest.mock import patch

    cf, flow = _flow()
    with tempfile.TemporaryDirectory() as root:
        for contents in (None, "invalid-json"):
            if contents is not None:
                (Path(root) / "geely_eu_app_keys.json").write_text(contents)
            with patch.object(flow.hass.config, "path", return_value=root), \
                    patch.object(cf, "_zeekr_login_password") as login:
                result = asyncio.run(flow.async_step_zeekr_login(
                    _zeekr_login_input({"country_code": "ES", "zeekr_region": "EU"})))
            login.assert_not_called()
            assert result["type"] == "form", result
            assert result["errors"] == {"base": "eu_app_keys_required"}


def test_login_helper_carries_the_local_profile_through_both_authentication_legs():
    from unittest.mock import patch
    from test_zeekr_europe import FAKE_PROFILE

    cf, _ = _flow()
    with patch.object(cf, "ZeekrIdaas") as idaas, \
            patch.object(cf, "ZeekrClient") as client:
        idaas.return_value.login_by_email_password.return_value = "fake-token"
        result = _REAL_LOGIN_HELPER(
            "owner@example.invalid", "fake-password", "ES", FAKE_PROFILE)
        idaas.assert_called_once_with(country="ES", profile=FAKE_PROFILE)
        client.assert_called_once_with(email="", password="", profile=FAKE_PROFILE)
        assert result.country_code == "ES"
        result.login_tsp.assert_called_once_with("fake-token")


def test_default_new_app_setup_with_gb_still_logs_into_sea_without_keys():
    from unittest.mock import patch

    cf, flow = _flow()
    form = asyncio.run(flow.async_step_zeekr_login())
    defaults = {str(k): k.default() for k in form["data_schema"].schema
                if str(k) in ("country_code", "zeekr_region")}
    assert defaults == {"country_code": "GB", "zeekr_region": "SEA"}
    with patch.object(cf, "load_region_profile", side_effect=AssertionError("No EU file needed")), \
            patch.object(cf, "_zeekr_login_password", return_value=_DerivingClient()) as login:
        result = asyncio.run(flow.async_step_zeekr_login(_zeekr_login_input(defaults)))
    login.assert_called_once_with("user@example.com", "hunter2", "GB")
    assert result["data"]["zeekr_region"] == "SEA"


def test_reauth_and_reconfigure_remember_explicit_region_and_preserve_old_sea_entries():
    from unittest.mock import patch
    from test_zeekr_europe import FAKE_PROFILE

    for source in ("reauth", "reconfigure"):
        for saved_region in (None, "SEA", "EU"):
            entry = _legacy_entry()
            entry.data.update({"platform": "zeekr", "country_code": "GB"})
            if saved_region is not None:
                entry.data["zeekr_region"] = saved_region
            cf, flow = _flow([entry])
            flow.context = {"entry_id": "e1", "source": source}
            if source == "reauth":
                form = asyncio.run(flow.async_step_reauth(entry.data))
            else:
                form = asyncio.run(flow.async_step_reconfigure({"platform": "zeekr"}))
            defaults = {str(k): k.default() for k in form["data_schema"].schema
                        if str(k) == "zeekr_region"}
            assert defaults["zeekr_region"] == (saved_region or "SEA")
            with patch.object(cf, "load_region_profile", return_value=FAKE_PROFILE) as loader, \
                    patch.object(cf, "_zeekr_login_password", return_value=_DerivingClient()) as login:
                result = asyncio.run(flow.async_step_zeekr_login(
                    _zeekr_login_input({"country_code": "GB", **defaults})))
            if saved_region == "EU":
                loader.assert_called_once()
                login.assert_called_once_with("user@example.com", "hunter2", "GB", FAKE_PROFILE)
            else:
                loader.assert_not_called()
                login.assert_called_once_with("user@example.com", "hunter2", "GB")
            assert result["reason"] == source + "_successful", result
            assert entry.data["zeekr_region"] == (saved_region or "SEA")


def test_switching_servers_replaces_derived_vehicle_token_but_keeps_manual_override():
    from unittest.mock import patch
    from test_zeekr_europe import FAKE_PROFILE

    for probe_result, manual in (("new-eu-token", ""), ("", ""), ("unused", "manual-token")):
        entry = _legacy_entry()
        entry.data.update({"platform": "zeekr", "country_code": "GB",
                           "zeekr_enc_vin": "old-sea-token"})
        if manual:
            entry.options["zeekr_enc_vin"] = manual
        cf, flow = _flow([entry])
        flow.context = {"entry_id": "e1", "source": "reconfigure"}
        asyncio.run(flow.async_step_reconfigure({"platform": "zeekr"}))
        client = _DerivingClient()
        with patch.object(cf, "load_region_profile", return_value=FAKE_PROFILE), \
                patch.object(cf, "_zeekr_login_password", return_value=client), \
                patch.object(client, "probe_x_vin", return_value=probe_result) as probe:
            result = asyncio.run(flow.async_step_zeekr_login(
                _zeekr_login_input({"country_code": "GB", "zeekr_region": "EU"})))
        assert result["reason"] == "reconfigure_successful"
        assert entry.data["zeekr_region"] == "EU"
        assert entry.data.get("zeekr_enc_vin", "") == (manual or probe_result)
        if manual:
            probe.assert_not_called()
            assert entry.options["zeekr_enc_vin"] == manual
        else:
            probe.assert_called_once()
