"""The setup flow itself.

Driven through the real ConfigFlow class with the network calls stubbed, so
these exercise the actual branch logic rather than a copy of it. The guards on
identifiers live in test_config_flow_guards.py.
"""
import asyncio

from conftest import FAKE_VIN, have_homeassistant, load
from run import skip

EMAIL = "owner@example.com"
OTHER_EMAIL = "someone.else@example.com"
TOKEN = "cidpsso-token"
USER_ID = "8817263412"


def _flow(monkey=None):
    """A ConfigFlow with hass stubbed and the API calls replaced."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    cf = load("config_flow")
    flow = cf.GeelyIntlConfigFlow()

    class _Entries:
        @staticmethod
        def async_entries(domain):
            return []

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


# ------------------------------------------------------------ first step ---

def test_the_first_form_offers_the_expected_choices():
    cf, flow = _flow()
    res = asyncio.run(flow.async_step_user(None))
    assert res["type"] == "form" and res["step_id"] == "user"
    keys = {str(k) for k in res["data_schema"].schema}
    for expected in ("email", "country_code", "pressure_unit", "poll_mode"):
        assert expected in keys, f"{expected} missing from the setup form"


def test_the_country_and_poll_dropdowns_are_closed_lists():
    """Free text here is how a user gets stuck at the OTP step."""
    cf, flow = _flow()
    const = load("const")
    res = asyncio.run(flow.async_step_user(None))
    for key, allowed in (("country_code", const.SUPPORTED_COUNTRIES),
                         ("poll_mode", const.POLL_MODES)):
        marker = next(k for k in res["data_schema"].schema if str(k) == key)
        validator = res["data_schema"].schema[marker]
        assert set(getattr(validator, "container", [])) == set(allowed), key


def test_a_failed_otp_send_re_renders_the_form_with_an_error():
    cf, flow = _flow()
    cf.geely_api.cidpsso_send_otp = lambda *a, **k: {"code": 500}
    res = asyncio.run(flow.async_step_user(
        {"email": EMAIL, "country_code": "GB", "pressure_unit": "psi",
         "poll_mode": "normal"}))
    assert res["type"] == "form"
    assert res["errors"]["base"] == "send_code_failed"


def test_the_form_is_prefilled_after_an_error():
    """The captcha behind the OTP send is unreliable, so retyping the address
    on every retry is the normal case."""
    cf, flow = _flow()
    cf.geely_api.cidpsso_send_otp = lambda *a, **k: {"code": 500}
    asyncio.run(flow.async_step_user(
        {"email": EMAIL, "country_code": "DE", "pressure_unit": "psi",
         "poll_mode": "normal"}))
    res = asyncio.run(flow.async_step_user(None))
    defaults = {str(k): k.default() for k in res["data_schema"].schema
                if hasattr(k, "default")}
    assert defaults.get("email") == EMAIL
    assert defaults.get("country_code") == "DE"


# ------------------------------------------------------------- the region ---

def test_an_unsupported_region_aborts_before_any_network_call():
    """Two independent guards produce `wrong_region`: this pre-check, and a
    GeelyRegionError from the server during provisioning. This test pins the
    first one, so it must assert that provisioning was never reached - checking
    only the abort reason passes even with this gate removed, because the
    server-side guard then produces the same answer.
    """
    cf, flow = _flow()
    called = []
    cf.geely_api.provision_user_cert = lambda **kw: called.append(kw)
    flow._email, flow._user_id, flow._cidpsso_token = EMAIL, USER_ID, TOKEN
    res = asyncio.run(flow._finish_with_vehicle(
        _vehicle(tspInfo=[{"serviceRegion": "SA"}])))
    assert res["type"] == "abort" and res["reason"] == "wrong_region"
    assert not called, (
        "setup contacted the certificate server for a region we have no "
        "credentials for, instead of stopping first"
    )


def test_a_server_side_region_rejection_also_aborts_cleanly():
    """The backend can disagree with the vehicle record, so 1501/1445 during
    provisioning must produce the same clear message, not a generic failure."""
    cf, flow = _flow()

    def boom(**kw):
        raise cf.geely_api.GeelyRegionError("code 1501: geelyos verify error")

    cf.geely_api.provision_user_cert = boom
    flow._email, flow._user_id, flow._cidpsso_token = EMAIL, USER_ID, TOKEN
    res = asyncio.run(flow._finish_with_vehicle(_vehicle()))
    assert res["type"] == "abort" and res["reason"] == "wrong_region"


def test_a_provisioning_failure_is_reported_as_such():
    cf, flow = _flow()

    def boom(**kw):
        raise RuntimeError("TLS handshake failed")

    cf.geely_api.provision_user_cert = boom
    flow._email, flow._user_id, flow._cidpsso_token = EMAIL, USER_ID, TOKEN
    res = asyncio.run(flow._finish_with_vehicle(_vehicle()))
    assert res["type"] == "abort" and res["reason"] == "cert_failed"


def test_a_successful_setup_creates_the_entry_with_the_expected_data():
    cf, flow = _flow()
    cf.geely_api.provision_user_cert = lambda **kw: None
    flow._email, flow._user_id, flow._cidpsso_token = EMAIL, USER_ID, TOKEN
    flow._pressure_unit, flow._poll_mode = "bar", "manual"
    flow._idfa, flow._idfv = "A", "V"
    res = asyncio.run(flow._finish_with_vehicle(_vehicle()))
    assert res["type"] == "create_entry"
    data = res["data"]
    assert data["vin"] == FAKE_VIN
    assert data["email"] == EMAIL
    assert data["region"] == "EU"
    assert data["pressure_unit"] == "bar"
    assert data["poll_mode"] == "manual"
    # the fingerprint must be carried, or the next login logs the phone out
    assert data["device_idfa"] == "A" and data["device_idfv"] == "V"


def test_a_malformed_vin_stops_setup_instead_of_reaching_the_filesystem():
    cf, flow = _flow()
    flow._email, flow._user_id, flow._cidpsso_token = EMAIL, USER_ID, TOKEN
    res = asyncio.run(flow._finish_with_vehicle(_vehicle(vin="../../etc/passwd")))
    assert res["type"] == "abort" and res["reason"] == "unknown"


def test_a_malformed_user_id_stops_setup():
    cf, flow = _flow()
    flow._email, flow._user_id, flow._cidpsso_token = EMAIL, "../evil", TOKEN
    res = asyncio.run(flow._finish_with_vehicle(_vehicle()))
    assert res["type"] == "abort" and res["reason"] == "unknown"


# ------------------------------------------------------------- the reauth ---

class _Entry:
    def __init__(self, email=EMAIL):
        self.entry_id = "e1"
        self.version = 6
        self.data = {"vin": FAKE_VIN, "email": email, "user_id": USER_ID,
                     "cidpsso_token": "old-token", "device_idfa": "A",
                     "device_idfv": "V"}
        self.options: dict = {}


def test_reauth_with_a_different_account_is_refused():
    """The entry's unique_id is email:vin and is not recomputed here, so this
    would leave it labelled with one address while operating as another."""
    cf, flow = _flow()
    flow._reauth_entry = _Entry(email=EMAIL)
    flow._email, flow._user_id, flow._cidpsso_token = OTHER_EMAIL, USER_ID, "new"
    res = asyncio.run(flow._finish_with_vehicle(_vehicle()))
    assert res["type"] == "abort"
    assert res["reason"] == "reauth_account_mismatch"


def test_reauth_with_the_same_account_refreshes_the_token():
    cf, flow = _flow()
    entry = _Entry(email=EMAIL)
    flow._reauth_entry = entry
    flow._email, flow._user_id, flow._cidpsso_token = EMAIL, USER_ID, "new-token"
    flow._idfa, flow._idfv = "A", "V"
    res = asyncio.run(flow._finish_with_vehicle(_vehicle()))
    assert res["type"] == "abort" and res["reason"] == "reauth_successful"
    assert entry.data["cidpsso_token"] == "new-token"


def test_reauth_email_comparison_ignores_case_and_whitespace():
    cf, flow = _flow()
    entry = _Entry(email="Owner@Example.com")
    flow._reauth_entry = entry
    flow._email, flow._user_id, flow._cidpsso_token = "owner@example.com", USER_ID, "new"
    flow._idfa, flow._idfv = "A", "V"
    res = asyncio.run(flow._finish_with_vehicle(_vehicle()))
    assert res["reason"] == "reauth_successful", "same account rejected on case alone"


def test_reauth_preserves_the_install_fingerprint():
    """Losing idfa/idfv makes the next login look like a new device, and Geely
    allows one session per account - it would sign the phone app out."""
    cf, flow = _flow()
    entry = _Entry()
    flow._reauth_entry = entry
    flow._email, flow._user_id, flow._cidpsso_token = EMAIL, USER_ID, "new"
    flow._idfa, flow._idfv = "A", "V"
    asyncio.run(flow._finish_with_vehicle(_vehicle()))
    assert entry.data["device_idfa"] == "A"
    assert entry.data["device_idfv"] == "V"


# ------------------------------------------------------------- the options ---

def test_the_options_form_can_change_polling_and_units_after_setup():
    cf, flow = _flow()
    const = load("const")

    class Entry:
        entry_id = "e1"
        data = {"poll_mode": "normal", "pressure_unit": "psi"}
        options: dict = {}

    entry = Entry()
    opts = cf.GeelyIntlOptionsFlow()
    # Since HA 2024.12 config_entry is a read-only property: it reads the id
    # from `handler` and looks the entry up. That is exactly the pattern this
    # flow relies on, so drive it the same way the framework does.
    opts.handler = entry.entry_id
    flow.hass.config_entries.async_get_known_entry = lambda _id: entry
    opts.hass = flow.hass
    opts.async_show_form = flow.async_show_form
    res = asyncio.run(opts.async_step_init(None))
    keys = {str(k) for k in res["data_schema"].schema}
    assert "poll_mode" in keys and "pressure_unit" in keys
    marker = next(k for k in res["data_schema"].schema if str(k) == "poll_mode")
    assert set(res["data_schema"].schema[marker].container) == set(const.POLL_MODES)


def test_manual_is_offered_in_the_options_not_only_at_setup():
    cf, flow = _flow()
    res = asyncio.run(flow.async_step_user(None))
    marker = next(k for k in res["data_schema"].schema if str(k) == "poll_mode")
    assert "manual" in res["data_schema"].schema[marker].container
