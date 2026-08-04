"""The parts of the setup flow test_config_flow.py leaves untouched.

The OTP/code step branches, the vehicle picker, the re-auth entry point and
the options flow, driven through the real classes with the network calls
stubbed - the same harness pattern as test_config_flow.py.
"""
import asyncio
import hashlib
import types

from conftest import FAKE_VIN, have_homeassistant, load
from run import skip

EMAIL = "owner@example.com"
USER_ID = "8817263412"
# A second obviously-fake VIN for the multi-car account tests.
FAKE_VIN_2 = "L6T00000000000001"


def _flow(entries=None):
    """A ConfigFlow with hass stubbed and the API calls replaced.

    `entries` seeds hass.config_entries.async_entries(), so a test can
    pretend some vehicles are already configured.
    """
    if not have_homeassistant():
        skip("homeassistant not installed")
    cf = load("config_flow")
    flow = cf.GeelyIntlConfigFlow()

    existing = list(entries or [])

    class _Entries:
        @staticmethod
        def async_entries(domain):
            return existing

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
        async def async_add_executor_job(fn, *a):
            return fn(*a)

    flow.hass = _Hass()
    # Home Assistant fills these in; the stubs keep the flow self-contained.
    flow.async_set_unique_id = lambda uid: asyncio.sleep(0)
    flow._abort_if_unique_id_configured = lambda: None
    flow.async_show_form = lambda **kw: {"type": "form", **kw}
    flow.async_abort = lambda **kw: {"type": "abort", **kw}
    flow.async_create_entry = lambda **kw: {"type": "create_entry", **kw}
    return cf, flow


def _vehicle(vin=FAKE_VIN, **over):
    v = {"vin": vin, "nickname": "My EX5", "modelCode": "E245-J1",
         "tspInfo": [{"serviceRegion": "EU"}]}
    v.update(over)
    return v


def _prime(flow):
    """The state async_step_user would have left behind."""
    flow._email = EMAIL
    flow._country_code = "GB"
    flow._idfa, flow._idfv = "A", "V"


def _login_ok(token="tok-1", user_id=USER_ID):
    return {"code": 10000000, "data": {"token": token, "userId": user_id}}


class _Entry:
    def __init__(self, email=EMAIL):
        self.entry_id = "e1"
        self.version = 6
        self.data = {"vin": FAKE_VIN, "email": email, "user_id": USER_ID,
                     "cidpsso_token": "old-token", "device_idfa": "A",
                     "device_idfv": "V"}
        self.options: dict = {}


USER_FORM = {"email": EMAIL, "country_code": "GB", "pressure_unit": "psi",
             "poll_mode": "normal"}


# ------------------------------------------------------------ first step ---

def test_a_successful_otp_send_advances_to_the_code_step():
    cf, flow = _flow()
    cf.geely_api.cidpsso_send_otp = lambda *a, **k: {"code": 10000000}
    res = asyncio.run(flow.async_step_user(dict(USER_FORM)))
    assert res["type"] == "form" and res["step_id"] == "code"
    # the code form tells the user which inbox to check
    assert res["description_placeholders"] == {"email": EMAIL}
    assert {str(k) for k in res["data_schema"].schema} == {"code"}


def test_a_network_error_sending_the_otp_reads_as_send_code_failed():
    """The captcha-unreachable case has its own key; every other exception
    must fall back to the generic one, not escape the flow."""
    cf, flow = _flow()

    def _boom(*a, **k):
        raise RuntimeError("connection reset")

    cf.geely_api.cidpsso_send_otp = _boom
    res = asyncio.run(flow.async_step_user(dict(USER_FORM)))
    assert res["type"] == "form" and res["step_id"] == "user"
    assert res["errors"]["base"] == "send_code_failed"


def test_reauth_reuses_the_stored_install_fingerprint_for_the_otp():
    """Geely allows one session per install fingerprint; minting a fresh one
    on re-auth would log the phone app out."""
    cf, flow = _flow()
    captured = {}

    def _send(email, country, idfa=None, idfv=None):
        captured.update(idfa=idfa, idfv=idfv)
        return {"code": 10000000}

    cf.geely_api.cidpsso_send_otp = _send
    flow._reauth_entry = _Entry()
    res = asyncio.run(flow.async_step_user(dict(USER_FORM)))
    assert res["step_id"] == "code"
    assert captured == {"idfa": "A", "idfv": "V"}, (
        "re-auth generated a new fingerprint instead of reusing the entry's"
    )


def test_the_reauth_entry_point_loads_the_entry_and_prefills_the_form():
    cf, flow = _flow()
    entry = _Entry()
    entry.data["country_code"] = "DE"
    flow.hass.config_entries.async_get_entry = \
        lambda eid: entry if eid == "e1" else None
    flow.context = {"entry_id": "e1", "source": "reauth"}
    res = asyncio.run(flow.async_step_reauth(dict(entry.data)))
    assert flow._reauth_entry is entry
    assert res["type"] == "form" and res["step_id"] == "user"
    defaults = {str(k): k.default() for k in res["data_schema"].schema
                if hasattr(k, "default")}
    assert defaults.get("email") == EMAIL
    assert defaults.get("country_code") == "DE"


# ------------------------------------------------------------- code step ---

def test_the_code_form_renders_before_any_input():
    cf, flow = _flow()
    _prime(flow)
    res = asyncio.run(flow.async_step_code(None))
    assert res["type"] == "form" and res["step_id"] == "code"
    assert res["errors"] == {}


def test_a_login_exception_reads_as_invalid_code():
    cf, flow = _flow()
    _prime(flow)

    def _boom(*a, **k):
        raise RuntimeError("timeout")

    cf.geely_api.cidpsso_login = _boom
    res = asyncio.run(flow.async_step_code({"code": "123456"}))
    assert res["type"] == "form" and res["step_id"] == "code"
    assert res["errors"]["code"] == "invalid_code"


def test_a_login_rejection_reads_as_invalid_code():
    cf, flow = _flow()
    _prime(flow)
    cf.geely_api.cidpsso_login = lambda *a, **k: {"code": 40001}
    res = asyncio.run(flow.async_step_code({"code": "000000"}))
    assert res["type"] == "form" and res["step_id"] == "code"
    assert res["errors"]["code"] == "invalid_code"


def test_a_login_that_returns_no_token_errors_out():
    cf, flow = _flow()
    _prime(flow)
    cf.geely_api.cidpsso_login = lambda *a, **k: {"code": 10000000, "data": {}}
    res = asyncio.run(flow.async_step_code({"code": "123456"}))
    assert res["type"] == "form" and res["step_id"] == "code"
    assert res["errors"]["base"] == "unknown"


def test_a_malformed_user_id_from_login_is_refused():
    """The user_id is interpolated into request paths, so a server response
    outside the strict identifier charset must stop the flow here."""
    cf, flow = _flow()
    _prime(flow)
    cf.geely_api.cidpsso_login = lambda *a, **k: _login_ok(user_id="../evil")
    res = asyncio.run(flow.async_step_code({"code": "123456"}))
    assert res["type"] == "form" and res["step_id"] == "code"
    assert res["errors"]["base"] == "unknown"


def test_a_vehicle_list_failure_reads_as_no_vehicles():
    cf, flow = _flow()
    _prime(flow)
    cf.geely_api.cidpsso_login = lambda *a, **k: _login_ok()

    def _boom(*a, **k):
        raise RuntimeError("504")

    cf.geely_api.list_vehicles = _boom
    res = asyncio.run(flow.async_step_code({"code": "123456"}))
    assert res["type"] == "form" and res["step_id"] == "code"
    assert res["errors"]["base"] == "no_vehicles"


def test_an_empty_garage_reads_as_no_vehicles():
    cf, flow = _flow()
    _prime(flow)
    cf.geely_api.cidpsso_login = lambda *a, **k: _login_ok()
    cf.geely_api.list_vehicles = lambda *a, **k: []
    res = asyncio.run(flow.async_step_code({"code": "123456"}))
    assert res["type"] == "form" and res["step_id"] == "code"
    assert res["errors"]["base"] == "no_vehicles"


def test_vehicles_with_malformed_vins_are_dropped_not_configured():
    """A VIN reaches the filesystem and the raw request line, so a backend
    answer full of traversal shapes must count as having no vehicles."""
    cf, flow = _flow()
    _prime(flow)
    cf.geely_api.cidpsso_login = lambda *a, **k: _login_ok()
    cf.geely_api.list_vehicles = \
        lambda *a, **k: [_vehicle(vin="../../etc/passwd")]
    res = asyncio.run(flow.async_step_code({"code": "123456"}))
    assert res["type"] == "form" and res["step_id"] == "code"
    assert res["errors"]["base"] == "no_vehicles"


def test_reauth_whose_car_left_the_account_reads_as_no_vehicles():
    """The entry's VIN no longer appears in the account's list - refreshing
    the token would leave a working entry for a car we cannot see."""
    cf, flow = _flow()
    _prime(flow)
    flow._reauth_entry = _Entry()
    cf.geely_api.cidpsso_login = lambda *a, **k: _login_ok(token="tok-2")
    cf.geely_api.list_vehicles = lambda *a, **k: [_vehicle(vin=FAKE_VIN_2)]
    res = asyncio.run(flow.async_step_code({"code": "123456"}))
    assert res["type"] == "form" and res["step_id"] == "code"
    assert res["errors"]["base"] == "no_vehicles"


def test_reauth_through_the_code_step_updates_only_the_credentials():
    cf, flow = _flow()
    _prime(flow)
    entry = _Entry()
    before = dict(entry.data)
    flow._reauth_entry = entry
    # "id" instead of "userId": the login response carries either name.
    cf.geely_api.cidpsso_login = lambda *a, **k: {
        "code": 10000000, "data": {"token": "tok-2", "id": USER_ID}}
    cf.geely_api.list_vehicles = lambda *a, **k: [_vehicle()]
    res = asyncio.run(flow.async_step_code({"code": "123456"}))
    assert res["type"] == "abort" and res["reason"] == "reauth_successful"
    assert entry.data["cidpsso_token"] == "tok-2"
    assert set(entry.data) == set(before), "re-auth added or dropped keys"
    changed = {k for k in entry.data if entry.data[k] != before[k]}
    assert changed <= {"cidpsso_token", "user_id",
                       "device_idfa", "device_idfv"}, changed


def test_an_account_with_every_car_configured_aborts_cleanly():
    already = types.SimpleNamespace(data={"vin": FAKE_VIN})
    cf, flow = _flow(entries=[already])
    _prime(flow)
    cf.geely_api.cidpsso_login = lambda *a, **k: _login_ok()
    cf.geely_api.list_vehicles = lambda *a, **k: [_vehicle()]
    res = asyncio.run(flow.async_step_code({"code": "123456"}))
    assert res["type"] == "abort" and res["reason"] == "all_configured"


def test_a_single_new_vehicle_goes_straight_to_a_created_entry():
    cf, flow = _flow()
    _prime(flow)
    cf.geely_api.cidpsso_login = lambda *a, **k: _login_ok(token="tok-3")
    cf.geely_api.list_vehicles = lambda *a, **k: [_vehicle()]
    cf.geely_api.provision_user_cert = lambda **kw: None
    res = asyncio.run(flow.async_step_code({"code": "123456"}))
    assert res["type"] == "create_entry"
    assert res["title"] == f"My EX5 ({FAKE_VIN})"
    data = res["data"]
    assert set(data) == {
        "email", "country_code", "region", "cidpsso_token", "user_id", "vin",
        "device_id", "cert_path", "key_path", "device_idfa", "device_idfv",
        "vehicle_nickname", "vehicle_series", "vehicle_model_code",
        "vehicle_color", "vehicle_power_type", "pressure_unit", "poll_mode",
    }, "entry data keys drifted from the documented set"
    assert data["vin"] == FAKE_VIN and data["email"] == EMAIL
    assert data["cidpsso_token"] == "tok-3" and data["user_id"] == USER_ID
    assert data["region"] == "EU"
    # the metadata fields come from helpers.vehicle_metadata
    assert data["vehicle_nickname"] == "My EX5"
    assert data["vehicle_model_code"] == "E245-J1"
    assert data["vehicle_series"] == "" and data["vehicle_color"] == ""
    assert data["vehicle_power_type"] == ""
    # stable device id derived from user and VIN, never random
    assert data["device_id"] == hashlib.md5(
        f"ha:{USER_ID}:{FAKE_VIN}".encode()).hexdigest()
    assert data["cert_path"].endswith("cert.pem") and FAKE_VIN in data["cert_path"]
    assert data["key_path"].endswith("key.pem") and FAKE_VIN in data["key_path"]


# ---------------------------------------------------------------- picker ---

def test_multiple_vehicles_offer_a_picker_then_finish_with_the_choice():
    cf, flow = _flow()
    _prime(flow)
    cf.geely_api.cidpsso_login = lambda *a, **k: _login_ok()
    # the second car has neither nickname nor model, so its label falls back
    cf.geely_api.list_vehicles = lambda *a, **k: [
        _vehicle(),
        {"vin": FAKE_VIN_2, "tspInfo": [{"serviceRegion": "EU"}]},
    ]
    cf.geely_api.provision_user_cert = lambda **kw: None
    res = asyncio.run(flow.async_step_code({"code": "123456"}))
    assert res["type"] == "form" and res["step_id"] == "pick_vehicle"
    marker = next(k for k in res["data_schema"].schema if str(k) == "vin")
    options = res["data_schema"].schema[marker].container
    assert set(options) == {FAKE_VIN, FAKE_VIN_2}
    assert options[FAKE_VIN] == f"My EX5 ({FAKE_VIN})"
    assert options[FAKE_VIN_2] == f"Geely ({FAKE_VIN_2})"

    picked = asyncio.run(flow.async_step_pick_vehicle({"vin": FAKE_VIN_2}))
    assert picked["type"] == "create_entry"
    assert picked["data"]["vin"] == FAKE_VIN_2
    assert picked["title"] == f"Geely ({FAKE_VIN_2})"
    assert picked["data"]["vehicle_nickname"] == ""


def test_picking_a_vin_that_was_never_offered_aborts():
    """The picker's answer is user input; only VINs from the fetched list may
    reach provisioning."""
    cf, flow = _flow()
    _prime(flow)
    flow._vehicles = [_vehicle()]
    res = asyncio.run(flow.async_step_pick_vehicle({"vin": FAKE_VIN_2}))
    assert res["type"] == "abort" and res["reason"] == "unknown"


# --------------------------------------------------------------- options ---

def test_the_options_flow_factory_returns_the_options_class():
    cf, flow = _flow()
    entry = types.SimpleNamespace(entry_id="e1")
    opts = cf.GeelyIntlConfigFlow.async_get_options_flow(entry)
    assert isinstance(opts, cf.GeelyIntlOptionsFlow)


def _options_flow(cf, flow, entry):
    """An options flow wired up the way the framework drives it."""
    opts = cf.GeelyIntlOptionsFlow()
    opts.handler = entry.entry_id
    flow.hass.config_entries.async_get_known_entry = lambda _id: entry
    opts.hass = flow.hass
    opts.async_show_form = flow.async_show_form
    opts.async_create_entry = flow.async_create_entry
    return opts


def test_changing_the_pressure_unit_writes_options_and_repoints_sensors():
    """HA reads suggested_unit_of_measurement only at first registration, so
    the options flow must rewrite the registry's display unit itself - but
    only for the four tire sensors, and into options, never entry.data."""
    cf, flow = _flow()

    class Entry:
        entry_id = "e1"
        data = {"poll_mode": "normal", "pressure_unit": "psi"}
        options: dict = {}

    entry = Entry()
    opts = _options_flow(cf, flow, entry)

    updates = []

    class _Registry:
        @staticmethod
        def async_update_entity_options(entity_id, domain, options):
            updates.append((entity_id, domain, options))

    reg_entries = [
        types.SimpleNamespace(entity_id="sensor.car_tire_fl",
                              unique_id=f"{FAKE_VIN}_tire_pressure_fl"),
        types.SimpleNamespace(entity_id="sensor.car_battery",
                              unique_id=f"{FAKE_VIN}_battery"),
    ]
    real_er = cf.er
    cf.er = types.SimpleNamespace(
        async_get=lambda hass: _Registry(),
        async_entries_for_config_entry=lambda registry, entry_id: reg_entries,
    )
    try:
        res = asyncio.run(opts.async_step_init(
            {"poll_mode": "manual", "pressure_unit": "bar",
             "full_exposure": True}))
    finally:
        cf.er = real_er
    assert res["type"] == "create_entry"
    assert res["data"] == {"poll_mode": "manual", "pressure_unit": "bar",
                           "full_exposure": True}
    # the flow returns the options for HA to store; the entry's immutable
    # setup data must not have been touched
    assert entry.data == {"poll_mode": "normal", "pressure_unit": "psi"}
    assert updates == [
        ("sensor.car_tire_fl", "sensor", {"unit_of_measurement": "bar"})
    ], "expected exactly the tire sensor repointed, nothing else"


def test_an_unchanged_pressure_unit_leaves_the_registry_alone():
    cf, flow = _flow()

    class Entry:
        entry_id = "e1"
        data = {"poll_mode": "normal", "pressure_unit": "psi"}
        options: dict = {}

    entry = Entry()
    opts = _options_flow(cf, flow, entry)

    calls = []
    real_apply = cf._apply_pressure_unit
    cf._apply_pressure_unit = lambda *a, **k: calls.append(a)
    try:
        res = asyncio.run(opts.async_step_init(
            {"poll_mode": "manual", "pressure_unit": "psi",
             "full_exposure": False}))
    finally:
        cf._apply_pressure_unit = real_apply
    assert res["type"] == "create_entry"
    assert res["data"]["poll_mode"] == "manual"
    assert not calls, "registry rewritten although the unit did not change"
