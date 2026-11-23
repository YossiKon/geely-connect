"""What the setup flow writes to the log when a step fails or is refused.

The form shows a translation key, never the text, so the log is where a value
could go. Each step's error is logged as its class - with its hops through
this package, but for the x-vin derivation's debug line - and its words with
what the flow sent or was handed taken out: the address, the one-time code,
the session token, the user id, the install fingerprint, the garage's VINs
and the new platform's session. Never as a traceback, which printed the error
whole, the one it was raised from too.
"""
import asyncio
import contextlib
import logging

from conftest import FAKE_VIN, have_homeassistant, load
# The flows these build skip the test without Home Assistant.
from test_config_flow import _flow as _legacy_flow, _vehicle
from test_zeekr_config_flow import (
    _REAL_LOGIN_HELPER, _flow as _zeekr_flow, _zeekr_login_input,
)

# The real garage read, taken at load: another file replaces the module
# attribute and leaves its replacement for the rest of the run.
_REAL_LIST_VEHICLES = load("api").list_vehicles if have_homeassistant() else None

EMAIL = "owner@example.com"
OTP = "482913"
TOKEN = "cidpsso-token-0001"
USER_ID = "8817263412"
IDFA = "AAAAAAAA-1111-2222-3333-444444444444"
IDFV = "BBBBBBBB-5555-6666-7777-888888888888"
FORM = {"email": EMAIL, "country_code": "GB", "pressure_unit": "psi",
        "poll_mode": "normal"}


@contextlib.contextmanager
def _patched(obj, **attrs):
    saved = {k: getattr(obj, k) for k in attrs}
    for k, v in attrs.items():
        setattr(obj, k, v)
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(obj, k, v)


class _Records(logging.Handler):
    """Each record as a handler writes it out, a traceback included."""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records, self.lines = [], []

    def emit(self, record):
        self.records.append(record)
        self.lines.append(logging.Formatter().format(record))


@contextlib.contextmanager
def _captured(logger):
    handler, level = _Records(), logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)


def _echo(*values):
    """Words that repeat what a call was sent."""
    return "denied for " + " / ".join(values)


def _raising(cls, *values):
    """A call that fails with an error naming `values`, raised from another
    that names them too: a traceback printed both."""
    def call(*a, **k):
        try:
            raise OSError(_echo(*values))
        except OSError as inner:
            raise cls(_echo(*values)) from inner
    return call


def _left_out(api, log, values):
    """No value in any line, each masked where it stood, and no record
    carrying an error for a handler to print."""
    text = "\n".join(log.lines)
    for v in values:
        assert v not in text, (v, log.lines)
        assert api._mask(v) in text, (v, log.lines)
    assert all(r.exc_info is None for r in log.records), log.lines


def _one(log, start):
    found = [line for line in log.lines if line.startswith(start)]
    assert len(found) == 1, (start, log.lines)
    return found[0]


def _primed():
    """A legacy flow at its code step, the address and fingerprint known."""
    cf, flow = _legacy_flow()
    flow._email, flow._country_code = EMAIL, "GB"
    flow._idfa, flow._idfv = IDFA, IDFV
    return cf, flow


_LOGGED_IN = {"code": 10000000, "data": {"token": TOKEN, "userId": USER_ID}}


# ------------------------------------------------- the original platform ---

def test_a_failed_code_send_is_logged_without_the_address_or_the_fingerprint():
    """The send carries the address and the install's fingerprint. An
    unreachable captcha host keeps its own error, and the line names it."""
    cf, flow = _legacy_flow()
    api = cf.geely_api
    for cls, key in ((api.GeelyCaptchaUnreachableError, "captcha_unreachable"),
                     (RuntimeError, "send_code_failed")):
        flow._idfa, flow._idfv = IDFA, IDFV
        with _patched(api, cidpsso_send_otp=_raising(cls, EMAIL, IDFA, IDFV)), \
                _captured(cf._LOGGER) as log:
            res = asyncio.run(flow.async_step_user(dict(FORM)))
        assert res["type"] == "form" and res["errors"]["base"] == key, res
        _left_out(api, log, (EMAIL, IDFA, IDFV))
        line = _one(log, "send-otp failed (")
        assert f"{cls.__name__} at config_flow.py:" in line, line


def test_a_refused_code_send_is_logged_without_the_address():
    """redact() goes by key name, and a refusal worded around the address it
    was sent carried it whole into a warning."""
    cf, flow = _legacy_flow()
    api = cf.geely_api
    flow._idfa, flow._idfv = IDFA, IDFV
    answer = {"code": 10010003, "message": _echo(EMAIL, IDFA, IDFV)}
    with _patched(api, cidpsso_send_otp=lambda *a, **k: answer), \
            _captured(cf._LOGGER) as log:
        res = asyncio.run(flow.async_step_user(dict(FORM)))
    assert res["errors"]["base"] == "send_code_failed", res
    _left_out(api, log, (EMAIL, IDFA, IDFV))
    _one(log, "OTP send response: ")


def test_a_failed_or_refused_login_is_logged_without_the_code_or_the_address():
    cf, flow = _primed()
    api = cf.geely_api
    with _patched(api, cidpsso_login=_raising(RuntimeError, EMAIL, OTP, IDFA)), \
            _captured(cf._LOGGER) as log:
        res = asyncio.run(flow.async_step_code({"code": OTP}))
    assert res["errors"]["code"] == "invalid_code", res
    _left_out(api, log, (EMAIL, OTP, IDFA))
    _one(log, "login failed (RuntimeError at config_flow.py:")

    answer = {"code": 10010007, "message": _echo(EMAIL, OTP, IDFV), "data": None}
    with _patched(api, cidpsso_login=lambda *a, **k: answer), \
            _captured(cf._LOGGER) as log:
        res = asyncio.run(flow.async_step_code({"code": OTP}))
    assert res["errors"]["code"] == "invalid_code", res
    _left_out(api, log, (EMAIL, OTP, IDFV))
    _one(log, "login resp: ")


def test_a_failed_garage_read_is_logged_without_the_session():
    """The garage is read with the token and user id the login handed back."""
    cf, flow = _primed()
    api = cf.geely_api
    with _patched(api, cidpsso_login=lambda *a, **k: _LOGGED_IN,
                  list_vehicles=_raising(RuntimeError, TOKEN, USER_ID, EMAIL, IDFV)), \
            _captured(cf._LOGGER) as log:
        res = asyncio.run(flow.async_step_code({"code": OTP}))
    assert res["errors"]["base"] == "no_vehicles", res
    _left_out(api, log, (TOKEN, USER_ID, EMAIL, IDFV))
    _one(log, "list_vehicles failed (RuntimeError at config_flow.py:")


def test_a_token_a_transport_refuses_is_logged_in_neither_half():
    """How a value the flow sent reaches an outside error at all: requests
    refuses a header holding a line break and names the value in its error,
    escaped. Through the real garage read, nothing stubbed past the login."""
    cf, flow = _primed()
    api = cf.geely_api
    assert _REAL_LIST_VEHICLES.__name__ == "list_vehicles", _REAL_LIST_VEHICLES
    broken = "cidpsso-tok\nen-000000001"
    head, tail = broken.split("\n")
    answer = {"code": 10000000, "data": {"token": broken, "userId": USER_ID}}
    with _patched(api, cidpsso_login=lambda *a, **k: answer,
                  list_vehicles=_REAL_LIST_VEHICLES), \
            _captured(cf._LOGGER) as log:
        res = asyncio.run(flow.async_step_code({"code": OTP}))
    assert res["errors"]["base"] == "no_vehicles", res
    line = _one(log, "list_vehicles failed (requests.exceptions.InvalidHeader at ")
    assert "in header value: " in line, line
    assert head not in line and tail not in line, line


def test_a_failed_or_refused_provisioning_is_logged_without_the_vin_or_the_session():
    """Through the code step, so the VIN is the one the garage listed. The
    region refusal keeps its own line, which still names the region."""
    cf, flow = _primed()
    api = cf.geely_api
    secret = (FAKE_VIN, TOKEN, USER_ID, EMAIL, IDFA, IDFV)
    for cls, reason, start in (
            (RuntimeError, "cert_failed",
             "cert provisioning failed (RuntimeError at config_flow.py:"),
            (api.GeelyRegionError, "wrong_region",
             "cert provisioning refused for a vehicle detected as region EU: ")):
        with _patched(api, cidpsso_login=lambda *a, **k: _LOGGED_IN,
                      list_vehicles=lambda *a, **k: [_vehicle()],
                      provision_user_cert=_raising(cls, *secret)), \
                _captured(cf._LOGGER) as log:
            res = asyncio.run(flow.async_step_code({"code": OTP}))
        assert res == {"type": "abort", "reason": reason}, res
        _left_out(api, log, secret)
        _one(log, start)


def test_an_unexpected_error_reading_an_areas_keys_stops_setup_with_a_line():
    """The keys file's reader words what it expects to meet. One it did not
    expect left the step whole, and Home Assistant's web server logged it
    with its traceback."""
    cf, flow = _primed()
    flow._user_id, flow._cidpsso_token, flow._vins = USER_ID, TOKEN, [FAKE_VIN]
    called = []
    with _patched(cf, load_region_backend=_raising(RuntimeError, FAKE_VIN, TOKEN)), \
            _patched(cf.geely_api, provision_user_cert=lambda **kw: called.append(kw)), \
            _captured(cf._LOGGER) as log:
        res = asyncio.run(flow._finish_with_vehicle(
            _vehicle(tspInfo=[], saleMarket="AE", tcamMarket="AE")))
    assert res == {"type": "abort", "reason": "wrong_region"}, res
    assert not called, "the cert server was asked without the area's keys"
    _left_out(cf.geely_api, log, (FAKE_VIN, TOKEN))
    _one(log, "reading the AE area's keys failed (RuntimeError at config_flow.py:")


def test_the_region_fields_line_hides_a_vin_under_a_name_redact_does_not_know():
    cf, flow = _primed()
    flow._user_id, flow._cidpsso_token, flow._vins = USER_ID, TOKEN, [FAKE_VIN]
    with _patched(cf.geely_api, provision_user_cert=lambda **kw: None), \
            _captured(cf._LOGGER) as log:
        res = asyncio.run(flow._finish_with_vehicle(
            _vehicle(tspInfo=[{"serviceRegion": "EU", "tbox": FAKE_VIN}])))
    assert res["type"] == "create_entry", res
    line = _one(log, "vehicle region fields: ")
    assert FAKE_VIN not in line and cf.geely_api._mask(FAKE_VIN) in line, line


# ------------------------------------------------------ the new platform ---

def _held_client(cf, **over):
    """The real client class, signed in: hidden() is its own."""

    class _Held(cf.ZeekrClient):
        def __init__(self):
            super().__init__(email="", password="")
            self.access_token, self.refresh_token = "at-0000000001", "rt-0000000002"
            self.hf_token, self.user_id = "hf-0000000003", USER_ID
            self.enc_vin = "QUJDREVGR0hJSktMTU5PUA=="

        def list_vehicles(self, user_id):
            return [{"vin": FAKE_VIN, "nickName": "Mock EX5",
                     "appModelCode": "E245-J1", "engineType": "BEV"}]

        def list_vehicles_bff(self):
            return []

    for name, fn in over.items():
        setattr(_Held, name, fn)
    return _Held()


def test_a_refused_or_failed_new_platform_sign_in_is_logged_without_the_address():
    cf, flow = _zeekr_flow()
    api = cf.geely_api
    for cls, key, start in (
            (cf.ZeekrAuthError, "invalid_credentials", "zeekr login rejected: "),
            (RuntimeError, "network_unreachable",
             "zeekr login failed (RuntimeError at config_flow.py:")):
        with _patched(cf, _zeekr_login_password=_raising(cls, EMAIL)), \
                _captured(cf._LOGGER) as log:
            res = asyncio.run(flow.async_step_zeekr_login(
                _zeekr_login_input({"email": EMAIL})))
        assert res["type"] == "form" and res["errors"]["base"] == key, res
        _left_out(api, log, (EMAIL,))
        _one(log, start)


def test_a_login_token_holding_a_line_break_never_reaches_the_log():
    """The new platform's sign-in sends its login token on in a header, from
    inside a helper whose token the flow never holds, so the flow's scrub
    cannot take it out of the error http.client's refusal raised. The client
    refuses the token before any connection instead, by name. Through the
    real helper and the real client; only the first leg's answer is made."""
    cf, flow = _zeekr_flow()
    zc = load("zeekr_client")
    assert _REAL_LOGIN_HELPER.__name__ == "_zeekr_login_password", _REAL_LOGIN_HELPER
    broken = "idaas-token\r\nvalue-0000001"
    head, tail = broken.split("\r\n")
    made = []

    class _Idaas:
        def __init__(self, country="AU", profile=None):
            pass

        def login_by_email_password(self, email, password):
            return broken

    class _NoConnection:
        def __init__(self, *a, **k):
            made.append(a)

    with _patched(cf, ZeekrIdaas=_Idaas, _zeekr_login_password=_REAL_LOGIN_HELPER), \
            _patched(zc.http.client, HTTPSConnection=_NoConnection), \
            _captured(cf._LOGGER) as log:
        res = asyncio.run(flow.async_step_zeekr_login(
            _zeekr_login_input({"email": EMAIL})))
    assert res["type"] == "form" and res["errors"]["base"] == "network_unreachable", res
    assert not made, "a connection was opened for a token no header can carry"
    line = _one(log, "zeekr login failed (ValueError at config_flow.py:")
    assert line.endswith(": the IDaaS login token holds a line break; it was not sent"), line
    text = "\n".join(log.lines)
    assert head not in text and tail not in text, log.lines


def test_a_signed_in_clients_error_is_logged_without_its_session():
    """Once signed in, the flow holds the client: what it hides - the vehicle
    token it holds included - is hidden in the flow's line too."""
    cf, flow = _zeekr_flow()
    api = cf.geely_api

    def broken_garage(self, user_id):
        raise RuntimeError(_echo(self.access_token, self.refresh_token,
                                 self.hf_token, self.enc_vin, USER_ID, EMAIL))

    client = _held_client(cf, list_vehicles=broken_garage)
    secret = (client.access_token, client.refresh_token, client.hf_token,
              client.enc_vin, USER_ID, EMAIL)
    with _patched(cf, _zeekr_login_password=lambda *a: client), \
            _captured(cf._LOGGER) as log:
        res = asyncio.run(flow.async_step_zeekr_login(
            _zeekr_login_input({"email": EMAIL})))
    assert res["errors"]["base"] == "network_unreachable", res
    _left_out(api, log, secret)
    _one(log, "zeekr login failed (RuntimeError at config_flow.py:")


def test_a_failed_x_vin_derivation_is_logged_at_debug_without_a_traceback():
    """Best effort: setup carries on without a token, and the line says why
    in the error's class and its scrubbed words. It was a debug traceback."""
    cf, flow = _zeekr_flow()
    api = cf.geely_api
    const = load("const")

    def broken_probe(self, vin):
        raise RuntimeError(_echo(vin, self.access_token, EMAIL))

    client = _held_client(cf, probe_x_vin=broken_probe)
    client.enc_vin = ""
    with _patched(cf, _zeekr_login_password=lambda *a: client), \
            _captured(cf._LOGGER) as log:
        res = asyncio.run(flow.async_step_zeekr_login(
            _zeekr_login_input({"email": EMAIL})))
    assert res["type"] == "create_entry", res
    assert res["data"][const.CONF_ZEEKR_ENC_VIN] == "", res["data"]
    _left_out(api, log, (FAKE_VIN, client.access_token, EMAIL))
    line = _one(log, "x-vin auto-derivation failed (RuntimeError: ")
    assert line.endswith("falling back to the manual option"), line


def test_an_unexpected_error_storing_the_password_ends_setup_with_a_line():
    """Its own error, an unreadable secrets.yaml, goes on to the callers'
    message; one it did not expect left the vehicle picker whole."""
    for cars in (1, 2):
        cf, flow = _zeekr_flow()
        api = cf.geely_api
        client = _held_client(cf, list_vehicles=lambda self, uid: [
            {"vin": FAKE_VIN, "nickName": "Mock EX5"}] * cars)
        with _patched(cf, _zeekr_login_password=lambda *a: client,
                      password_encrypt=_raising(RuntimeError, EMAIL)), \
                _captured(cf._LOGGER) as log:
            res = asyncio.run(flow.async_step_zeekr_login(
                _zeekr_login_input({"email": EMAIL, "store_password": True})))
            if cars == 2:
                assert res["type"] == "form" and res["step_id"] == "zeekr_pick", res
                res = asyncio.run(flow.async_step_zeekr_pick({"vin": FAKE_VIN}))
        assert res["type"] == "abort" and res["reason"] == "unknown", (cars, res)
        _left_out(api, log, (EMAIL,))
        _one(log, "storing the password failed (RuntimeError at config_flow.py:")
