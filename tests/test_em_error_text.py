"""Geely EM: error text built from a gateway's answer never repeats what the
request sent - the vehicle token, the VIN, the session tokens, the email.

redact() masks by key name, so a value the gateway words into a sentence went
whole into the log, the errors Home Assistant shows, the command trail and the
diagnostics report. No answer seen so far quotes one; these gateways quote
whatever they like, so every answer here does. Driven through the real client
with only the HTTPS connection replaced: the scrub sits inside the transport.
"""
import asyncio
import json
import logging
import types
from unittest.mock import patch

from conftest import FAKE_VIN, have_homeassistant, load
from run import skip

zc = load("zeekr_client")
ad = load("zeekr_adapter")

# Invented values, each long enough to be taken out (zc._HIDE_MIN_LEN).
TOKEN = "ZmFrZS12ZWhpY2xlLXRva2VuLWZvci10ZXN0cw=="
ACCESS = "fake-access-token-0123456789"
HF = "fake-hf-jwt-0123456789"
EMAIL = "owner@example.invalid"
LOGIN = "fake-idaas-token-value-0123"
SECRETS = (TOKEN, FAKE_VIN, ACCESS, HF, EMAIL, LOGIN)


def _clean(text):
    for secret in SECRETS:
        assert secret not in text, (secret, text)


class _Answer:
    def __init__(self, status, body):
        self.status = status
        self._raw = json.dumps(body).encode()

    def read(self):
        return self._raw


class _Gateway:
    """An HTTPS connection answered by `reply(headers)` - which sees what the
    request sent, the x-vin header included."""

    reply = None

    def __init__(self, host, *args, **kwargs):
        self.host = host

    def request(self, method, path, body=b"", headers=None):
        self._answer = _Answer(*type(self).reply(headers or {}))

    def getresponse(self):
        return self._answer

    def close(self):
        pass


def _through(reply, fn, *args):
    _Gateway.reply = staticmethod(reply)
    with patch.object(zc.http.client, "HTTPSConnection", _Gateway):
        return fn(*args)


def _raises(fn, *args):
    try:
        fn(*args)
    except Exception as exc:  # noqa: BLE001 - the error is what is checked
        return exc
    raise AssertionError("expected an error")


def _says(status, body):
    return lambda headers: (status, body)


def _client():
    c = zc.ZeekrClient(EMAIL, "", gateway="https://gw.example.invalid",
                       access_token=ACCESS)
    c.hf_token = HF
    c.enc_vin = TOKEN
    c.vin = FAKE_VIN
    return c


def _adapter():
    return ad.ZeekrAdapter(
        email=EMAIL, vin=FAKE_VIN, user_id="fake-user-0001",
        access_token=ACCESS, refresh_token="fake-refresh-0123",
        hf_token=HF, hf_expiry=10 ** 15, gateway="https://gw.example.invalid",
        enc_vin=TOKEN)


class _Records(logging.Handler):
    """Every line logged, as a handler prints it - a traceback included,
    which record.getMessage() leaves out."""

    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append((record.levelno, logging.Formatter().format(record)))


def _logged(fn):
    """fn's result and every line logged meanwhile, from debug up."""
    root = logging.getLogger()
    handler, old = _Records(), root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        return fn(), handler.lines
    finally:
        root.removeHandler(handler)
        root.setLevel(old)


def test_hide_values_takes_the_longest_first_and_leaves_short_ones():
    inner, outer = "abcdefgh", "abcdefgh-ijkl"
    text = f"x {outer} y {inner} z"
    assert zc.hide_values(text, [inner, outer]) == \
        "x XXXXXXXX*XXXX y XXXXXXXX z", "the longer value must not be split"
    # A short value would take pieces out of words the classifiers read.
    assert zc.hide_values("token tok", ["tok", "", None]) == "token tok"


def test_a_hidden_value_moves_no_word_boundary():
    """A code written right against a value: `401` ran into the token's first
    letter, so no word ended after it, and an asterisk there would end one -
    and `code=401` read on its own is a dead session, a password login."""
    c = _client()
    e = _raises(_through, _says(500, {"message": f"code=401{TOKEN}"}),
                c.vehicle_status_new_resp)
    _clean(str(e))
    assert not ad._looks_authy(str(e)), str(e)


def test_a_session_replaced_while_a_call_is_out_is_still_left_out():
    """Another sign-in can replace the session while a call is out: the
    error is about the session the call went out with."""
    c = _client()

    def reply(headers):
        c.access_token = "fake-newer-session-0123"
        return 401, {"message": f"session {ACCESS} was replaced"}

    e = _raises(_through, reply, c.vehicle_status_new_resp)
    _clean(str(e))
    assert "was replaced" in str(e), str(e)


def test_a_hidden_value_moves_nothing_around_it():
    """The body is cut at 200 characters after the values are taken out, and
    the classifiers read what is left: a mask shorter than the value would
    pull a word from past the cut into the text - here "session", which
    reads as a dead session and sets off a password login."""
    c = _client()
    head = str({"code": "500100", "message": "MARK"}).split("MARK")[0]
    said = "x" * (200 - len(head) - len(TOKEN)) + TOKEN + " session closed"
    e = _raises(_through, _says(500, {"code": "500100", "message": said}),
                c.vehicle_status_new_resp)
    _clean(str(e))
    assert "session" not in str(e) and not ad._looks_authy(str(e)), str(e)


def test_a_sign_in_answer_quoting_the_session_it_replaced_leaves_it_out():
    """The ms-user-auth leg stores the answer's tokens before it words the
    error; the session the call went out with, and the one-time code in its
    body, must still be left out."""
    c = _client()
    code = "fake-one-time-code-0123"
    said = f"replaced {ACCESS} for {code}"
    e = _raises(_through, _says(200, {"success": True, "data": {"note": said}}),
                c._ms_user_auth_login, code)
    assert isinstance(e, zc.ZeekrAuthError) and "no accessToken" in str(e), e
    _clean(str(e))
    assert code not in str(e), str(e)
    e = _raises(_through, _says(401, {"message": said}), c._ms_user_auth_login, code)
    assert code not in str(e) and "401" in str(e), str(e)


def test_the_hf_exchange_leaves_out_its_one_time_code():
    c = _client()
    code2 = "fake-hf-auth-code-0123"

    def reply(headers):
        return 200, {"success": True, "data": {"code": code2}}

    def exchange_fails(headers):
        if headers.get("Authorization") == LOGIN:     # the IDaaS tspCode leg
            return reply(headers)
        return 403, {"message": f"authCode {code2} already used"}

    e = _raises(_through, exchange_fails, c.login_hf, LOGIN)
    assert code2 not in str(e) and "403" in str(e), str(e)
    _clean(str(e))


def test_a_refusal_quoting_the_request_comes_out_without_it():
    c = _client()
    said = (f"Decrypt X-VIN failed: {TOKEN} ({FAKE_VIN}) on session {ACCESS} "
            f"for {EMAIL}")
    e = _raises(_through, _says(200, {"success": False, "code": "079025",
                                      "message": said}), c.capabilities_new)
    _clean(str(e))
    # What decides a renewal, a refusal and a probe's verdict still reads.
    assert zc._x_vin_rejected(e) and ad._refusal_code(e) == "079025", e
    assert "Decrypt X-VIN failed" in str(e), e


def test_a_cut_through_the_token_leaves_no_piece_of_it():
    """The body is cut at 200 characters; a token across the cut used to
    leave its first characters behind, where no later replace finds it."""
    c = _client()
    head = str({"code": "079001", "message": "MARK"}).split("MARK")[0]
    said = "x" * (200 - len(head) - 10) + TOKEN
    e = _raises(_through, _says(401, {"code": "079001", "message": said}),
                c.charging_plan_new)
    assert TOKEN[:10] not in str(e), str(e)
    assert zc._car_refused(e), e


def test_the_old_route_leaves_out_the_jwt_and_the_vin_it_sent():
    c = _client()
    said = f"vehicle {FAKE_VIN} is unknown to session {HF}"
    for status, body in ((200, {"code": 8060, "message": said}),
                         (403, {"message": said})):
        e = _raises(_through, _says(status, body), c.vehicle_status_resp, FAKE_VIN)
        _clean(str(e))
        assert str(status if status != 200 else 8060) in str(e), e
    # The module transport leaves out what it was handed, client or none.
    e = _raises(_through, _says(403, {"message": said}),
                lambda: zc._hf_request("GET", "/x", token=HF, vin=FAKE_VIN))
    _clean(str(e))


def test_an_answer_handed_back_has_its_sentence_cleared_and_its_data_kept():
    """A code that is not a success, without success:false, is handed back -
    the coordinator words a failed poll around its msg - and the data in it
    is data: the garage's VINs are compared."""
    c = _client()
    got = _through(_says(200, {"code": "079001",
                               "msg": f"car {FAKE_VIN} refused on {TOKEN}",
                               "data": {"vin": FAKE_VIN}}),
                   c.vehicle_status_new_resp)
    _clean(got["msg"])
    assert got["code"] == "079001" and got["data"] == {"vin": FAKE_VIN}, got


def test_the_sign_in_service_leaves_out_the_email_and_the_login_token():
    idaas = zc.ZeekrIdaas()
    e = _raises(_through, _says(200, {"success": False, "code": "3001",
                                      "message": f"account {EMAIL} is locked"}),
                idaas.login_by_email_password, EMAIL, "fake-password")
    _clean(str(e))
    assert "3001" in str(e), e
    e = _raises(_through, _says(200, {"success": True,
                                      "data": {"note": f"welcome {EMAIL}"}}),
                idaas.login_by_email, EMAIL, "fake-id", "123456")
    _clean(str(e))
    assert "returned no tokenValue" in str(e), e
    e = _raises(_through, _says(401, {"message": f"login token {LOGIN} expired"}),
                idaas.tsp_code, LOGIN)
    _clean(str(e))
    assert "401" in str(e), e


def test_a_probe_summary_carries_no_candidate_and_no_vin():
    """The probe cuts each outcome at 80 characters and setup's warning names
    the summary; each candidate is the token in use while it is tried."""
    c = zc.ZeekrClient("", "", gateway="https://gw.example.invalid",
                       access_token=ACCESS)
    candidates = [zc.derive_x_vin(FAKE_VIN, key, iv)
                  for key, iv, _ in zc._X_VIN_MATERIAL]
    head = "HTTP 400: " + str({"code": "079025", "message": "MARK"}).split("MARK")[0]

    def reply(headers):
        return 400, {"code": "079025", "message":
                     "y" * (70 - len(head)) + headers.get("x-vin", "")
                     + f" for {FAKE_VIN}"}

    assert _through(reply, c.probe_x_vin, FAKE_VIN) == ""
    assert not c.car_refused and "079025" in c.probe_summary, c.probe_summary
    for candidate in candidates:
        assert candidate[:10] not in c.probe_summary, c.probe_summary
    assert FAKE_VIN not in c.probe_summary and c.enc_vin == ""


def test_the_schedule_read_warning_leaves_the_token_out():
    a = _adapter()
    said = f"[SDK] not authorised for {TOKEN} / {FAKE_VIN}"
    e, lines = _logged(lambda: _raises(
        _through, _says(401, {"code": "079001", "message": said}),
        a.charge_server_get, "6"))
    warned = [text for level, text in lines if level >= logging.WARNING]
    assert len(warned) == 1 and "refused this car" in warned[0], warned
    assert "079001" in warned[0] and "079001" in str(e), (warned, e)
    _clean(warned[0])
    _clean(str(e))
    assert not a.reads_charge_server


def test_a_failure_from_outside_the_client_is_scrubbed_in_that_warning_too():
    """A timeout or a dropped connection on the read a renewal was spent on
    stops the reads as well, and its text did not come through the client."""
    a = _adapter()
    a._renew_hf = lambda seen=None: None
    tries = []

    def read():
        tries.append(1)
        if len(tries) == 1:
            raise zc.ZeekrApiError("code=079021 message=logged in elsewhere")
        raise OSError(f"connection reset after sending {TOKEN} for {FAKE_VIN}")

    e, lines = _logged(lambda: _raises(lambda: a._authed_charge(read, read=True)))
    assert isinstance(e, OSError), e
    warned = [text for level, text in lines if level >= logging.WARNING]
    assert len(warned) == 1 and "fresh session" in warned[0], warned
    _clean(warned[0])


def test_a_refused_command_reaches_neither_its_error_nor_the_trail_whole():
    """The platforms show a refusal as `Geely <command>: <message>`."""
    a = _adapter()
    said = f"bad x-vin {TOKEN} for {FAKE_VIN}"
    e = _raises(_through, _says(200, {"success": False, "code": "079025",
                                      "message": said}),
                a.control, "RDL_2", [], "start")
    assert isinstance(e, ad.GeelyControlError) and e.code == "079025", e
    _clean(e.message)
    note, = a.command_trail
    assert note["code"] == "079025", note
    _clean(json.dumps(note))


def test_text_from_outside_the_client_reaches_the_trail_without_the_session():
    """The trail used to take out the token and the VIN only: a session token
    or the email in, say, a dropped connection's text went in whole."""
    a = _adapter()

    def dropped(*args):
        raise OSError(f"connection reset: {ACCESS} {EMAIL} {TOKEN}")

    a._client.control_new_resp = dropped
    e = _raises(a.control, "RDL_2", [], "start")
    assert isinstance(e, OSError), e
    note, = a.command_trail
    assert note["outcome"] == "error" and "connection reset" in note["message"], note
    _clean(json.dumps(note))


def test_the_diagnostics_free_text_leaves_the_token_out():
    """The sweep's error rows and the last poll error: text from anywhere,
    not only from the client."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    diag = load("diagnostics")
    a = _adapter()

    def slot_fails(slot):
        raise RuntimeError(f"slot {slot} failed for {TOKEN}")

    a.charge_server_get = slot_fails
    coordinator = types.SimpleNamespace(
        data={}, update_interval=None, last_update_success=False,
        last_exception=RuntimeError(f"poll failed for {TOKEN} ({FAKE_VIN})"))

    class Entry:
        entry_id = "e1"
        data = {"vin": FAKE_VIN}
        options: dict = {}

    class Hass:
        data = {"geely_connect": {"e1": {"api": a, "coordinator": coordinator}}}

        async def async_add_executor_job(self, fn, *args):
            return fn(*args)

    got = asyncio.run(diag.async_get_config_entry_diagnostics(Hass(), Entry()))
    rows = got["charge_server"]
    assert rows and all("error" in row for row in rows.values()), rows
    _clean(json.dumps(rows))
    _clean(got["polling"]["last_exception"])
    assert "poll failed" in got["polling"]["last_exception"], got["polling"]


def test_a_failed_poll_is_worded_without_the_token():
    """What the coordinator makes of the client's errors and answers: the
    warning that reuses the last snapshot, UpdateFailed and the re-auth."""
    from test_zeekr_init import _mod, _Patched, _setup_zeekr
    from test_init_lifecycle import _FakeCoordinator, _instant_sleep

    m = _mod()
    c = _client()
    said = f"bad x-vin {TOKEN} for {FAKE_VIN}"
    refused = _raises(_through, _says(200, {"success": False, "code": "079025",
                                            "message": said}),
                      c.vehicle_status_new_resp)
    answer = _through(_says(200, {"code": "079001", "msg": said}),
                      c.vehicle_status_new_resp)
    ok, hass, entry, api = _setup_zeekr(m)
    assert ok is True
    fast = types.SimpleNamespace(sleep=_instant_sleep,
                                 CancelledError=asyncio.CancelledError)

    def poll():
        with _Patched(m, asyncio=fast):
            return _raises(lambda: asyncio.run(_FakeCoordinator.instance.refresh()))

    def failing(exc):
        def status():
            raise exc
        return status

    # The first failure reuses the snapshot and says why, at warning.
    api.vehicle_status = failing(refused)
    with _Patched(m, asyncio=fast):
        _, lines = _logged(lambda: asyncio.run(_FakeCoordinator.instance.refresh()))
    warned = [text for level, text in lines if level >= logging.WARNING]
    assert warned and "reusing last snapshot" in warned[0], warned
    for text in warned:
        _clean(text)
    # An answer handed back with a code that is not a success.
    api.vehicle_status = lambda: answer
    e = poll()
    assert isinstance(e, m.UpdateFailed) and "079001" in str(e), e
    _clean(str(e))
    # A sign-in failure, as the adapter raises it.
    api.vehicle_status = failing(ad.GeelyAuthError(str(refused)))
    e = poll()
    assert isinstance(e, m.ConfigEntryAuthFailed), e
    _clean(str(e))


def test_the_adapter_quotes_any_text_without_the_token_for_the_coordinator():
    """The adapter's without_secrets() is what the coordinator and the
    actions quote an error through: the client's values masked, the VIN
    shortened, as the trail shows it."""
    said = _adapter().without_secrets(
        OSError(f"reset while sending {TOKEN} {ACCESS} {HF} {EMAIL} for {FAKE_VIN}"))
    _clean(said)
    assert f"...{FAKE_VIN[-4:]}" in said and zc._mask(TOKEN) in said, said


def test_a_failed_poll_quotes_an_outside_error_without_the_token():
    """An error raised outside the client - here a socket's that repeats
    what was sent - was never scrubbed where it was raised: the retries'
    debug lines, the warning that reuses the last snapshot and UpdateFailed
    quote it through the adapter."""
    from test_zeekr_init import _mod, _Patched, _setup_zeekr
    from test_init_lifecycle import _FakeCoordinator, _instant_sleep

    m = _mod()
    ok, hass, entry, api = _setup_zeekr(m)
    assert ok is True
    api.without_secrets = _adapter().without_secrets
    fast = types.SimpleNamespace(sleep=_instant_sleep,
                                 CancelledError=asyncio.CancelledError)

    def failing():
        raise OSError(f"reset while sending {TOKEN} {ACCESS} for {FAKE_VIN}")

    api.vehicle_status = failing
    failed, lines = None, []
    for _ in range(4):
        with _Patched(m, asyncio=fast):
            e, more = _logged(lambda: _raises_or_none(
                lambda: asyncio.run(_FakeCoordinator.instance.refresh())))
        lines += more
        failed = failed or e
    warned = [text for level, text in lines if level >= logging.WARNING]
    assert warned and "reusing last snapshot" in warned[0], warned
    assert any("transient" in text for level, text in lines), lines
    for _, text in lines:
        _clean(text)
    assert isinstance(failed, m.UpdateFailed), failed
    _clean(str(failed))


def _raises_or_none(fn):
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 - the error is what is checked
        return exc
    return None
