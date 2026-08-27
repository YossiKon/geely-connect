"""The module-level login, cert and transport functions in api.py.

These run exactly once per install - setup, reauth, cert provisioning - which
is why they had the least coverage and deserve the most: a regression here is
found by the next new user, not by anyone's running install.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import os
import tempfile
import types

from conftest import FAKE_VIN, have_homeassistant, load
from run import skip


def _deps():
    import importlib.util
    return all(importlib.util.find_spec(m)
               for m in ("requests", "numpy", "PIL", "scipy", "cryptography"))


def _api():
    if not have_homeassistant():
        skip("homeassistant not installed")
    return load("api")


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _Session:
    """Records every request; answers from a scripted list of payloads."""

    def __init__(self, *payloads):
        self.calls = []
        self._payloads = list(payloads)

    def _next(self):
        return self._payloads.pop(0) if len(self._payloads) > 1 else self._payloads[0]

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append(("POST", url, headers, json))
        return _Resp(self._next())

    def get(self, url, headers=None, timeout=None):
        self.calls.append(("GET", url, headers, None))
        return _Resp(self._next())


class _SessionPatch:
    def __init__(self, api, session):
        self.api, self.session = api, session

    def __enter__(self):
        self.orig = self.api._legacy_session
        self.api._legacy_session = lambda: self.session
        return self.session

    def __exit__(self, *exc):
        self.api._legacy_session = self.orig


# ------------------------------------------------------------- pin store ---

def test_a_failed_atomic_write_cleans_up_and_still_raises():
    """The .tmp file must not survive a crashed write - and if even the
    cleanup fails, the ORIGINAL error is the one the caller needs to see."""
    api = _api()

    class _Os:
        def __getattr__(self, name):
            return getattr(os, name)

        def replace(self, a, b):
            raise RuntimeError("disk full")

        def unlink(self, p):
            raise OSError("and unlink too")

    orig = api.os
    api.os = _Os()
    try:
        with tempfile.TemporaryDirectory() as d:
            try:
                api._save_pin_store(os.path.join(d, "pins.json"), {})
            except RuntimeError as e:
                assert "disk full" in str(e)
            else:
                raise AssertionError("the write failure was swallowed")
    finally:
        api.os = orig


# ------------------------------------------------------------- raw https ---

def test_raw_https_stops_reading_at_the_size_cap():
    """A server that streams forever must cost 200 KB, not the process."""
    api = _api()

    class _Sock:
        def __init__(self):
            self.sent = b""
            self.closed = False
            self._first = True

        def send(self, b):
            self.sent += b

        def recv(self, n):
            if self._first:
                self._first = False
                return b"HTTP/1.1 200 OK\r\n\r\n"
            return b"x" * n

        def close(self):
            self.closed = True

    sock = _Sock()
    orig = api._secure_tls_connect
    api._secure_tls_connect = lambda *a, **k: sock
    try:
        body = api._raw_https("h.example", "POST", "/p", {"A": "1"}, b"body",
                              pin_path=None)
    finally:
        api._secure_tls_connect = orig
    assert 190_000 < len(body) <= 200_000 + 4096, len(body)
    assert sock.closed
    assert b"POST /p HTTP/1.1" in sock.sent and sock.sent.endswith(b"body")


# ------------------------------------------------------ ecloudeu signing ---

def test_the_cert_endpoint_signature_is_a_real_hmac_over_the_sign_string():
    """The server rejects a bad signature with an opaque error, so the test
    recomputes it from the emitted nonce and timestamp."""
    api = _api()
    h = api._sign_request_for_api_ecloudeu(
        "app-id", "app-secret", "POST",
        "https://api.ecloudeu.com/auth/cert/info?x=1", b'{"a":1}')
    assert h["X-APP-ID"] == "app-id"
    ss = api._build_sign_string(
        method="POST", path="/auth/cert/info", query="x=1",
        accept="application/json;responseformat=3",
        nonce=h["X-api-signature-nonce"], sig_version="1.0",
        timestamp_ms=int(h["X-timestamp"]), body=b'{"a":1}')
    expected = base64.b64encode(
        hmac.new(b"app-secret", ss.encode(), hashlib.sha1).digest()).decode()
    assert h["X-signature"] == expected


# ------------------------------------------------------ cert provisioning ---

def _provision(api, info, file=None, tmpdir=None):
    """Run provision_user_cert against scripted /info and /file answers."""
    calls = []

    def _fake_raw(host, method, path, headers, body, **kw):
        calls.append((path, json.loads(body)))
        payload = info if path.endswith("/info") else file
        return json.dumps(payload).encode()

    orig = api._raw_https
    api._raw_https = _fake_raw
    try:
        return api.provision_user_cert(
            app_id="a", app_secret="s", user_id="user-1",
            cidpsso_token="tok", cert_out_path=os.path.join(tmpdir, "v", "cert.pem"),
            key_out_path=os.path.join(tmpdir, "v", "key.pem")), calls
    finally:
        api._raw_https = orig


def _provision_seq(api, infos, files, tmpdir, calls=None):
    """Like _provision, but answers /info and /file from queues.

    The 8200 retry needs a second, different answer from each endpoint, which the
    single-payload helper above cannot express. Kept separate so the existing
    tests that rely on that helper's behaviour are untouched.

    Pass `calls` to own the trail: provisioning raises on a failure path, and the
    number of requests made before it raised is exactly what those tests need.
    """
    calls = [] if calls is None else calls
    infos, files = list(infos), list(files)

    def _fake_raw(host, method, path, headers, body, **kw):
        calls.append((path, json.loads(body)))
        q = infos if path.endswith("/info") else files
        return json.dumps(q.pop(0) if len(q) > 1 else q[0]).encode()

    orig = api._raw_https
    api._raw_https = _fake_raw
    try:
        return api.provision_user_cert(
            app_id="a", app_secret="s", user_id="user-1",
            cidpsso_token="tok", cert_out_path=os.path.join(tmpdir, "v", "cert.pem"),
            key_out_path=os.path.join(tmpdir, "v", "key.pem")), calls
    finally:
        api._raw_https = orig


def test_apac_gets_a_second_chance_under_the_name_it_asked_for():
    """#32: APAC calls the same challenge `checkValue` and answers 8200 when it
    arrives as `checkCode`. The challenge is single-use, so the retry has to
    fetch a fresh one - reusing the spent value would fail for a second reason
    and look like the rename had not helped."""
    if not _deps():
        skip("cryptography not installed")
    api = _api()
    with tempfile.TemporaryDirectory() as d:
        (cert, _), calls = _provision_seq(
            api,
            infos=[{"code": 1000, "data": {"checkCode": "first"}},
                   {"code": 1000, "data": {"checkCode": "fresh"}}],
            files=[{"code": 8200, "hint": "checkValue invalid"},
                   {"code": 1000, "data": {"cert": "-----APAC CERT-----"}}],
            tmpdir=d)
        assert open(cert).read() == "-----APAC CERT-----"
        assert [c[0].rsplit("/", 1)[1] for c in calls] == ["info", "file", "info", "file"]
        assert calls[1][1]["checkCode"] == "first"        # the original attempt
        assert "checkCode" not in calls[3][1], calls[3][1]  # renamed on the retry
        assert calls[3][1]["checkValue"] == "fresh"       # and a new challenge
        # The retry must not quietly change anything else about the request.
        assert calls[3][1]["csr"] == calls[1][1]["csr"]
        assert calls[3][1]["deviceId"] == calls[1][1]["deviceId"]
        assert calls[3][1]["accessToken"] == calls[1][1]["accessToken"]


def test_an_8200_that_is_not_about_the_challenge_name_is_not_retried():
    """The retry is keyed to the hint on purpose. Retrying every 8200 would spend
    a second challenge and hide the real reason behind an identical failure."""
    if not _deps():
        skip("cryptography not installed")
    api = _api()
    seen = []
    with tempfile.TemporaryDirectory() as d:
        try:
            _provision_seq(api,
                           infos=[{"code": 1000, "data": {"checkCode": "c"}}],
                           files=[{"code": 8200, "hint": "device limit reached"}],
                           tmpdir=d, calls=seen)
        except RuntimeError as e:
            assert "cert/file failed" in str(e)
        else:
            raise AssertionError("an unrelated 8200 was swallowed")
    # The point of the test: no second challenge was spent. Asserting only on the
    # message let a mutant that retries every 8200 through, because its retry
    # failed with the same message.
    assert [c[0] for c in seen] == ["/auth/cert/info", "/auth/cert/file"], seen


def test_a_failed_retry_challenge_still_reports_the_original_failure():
    """If the fresh /info also fails there is nothing to retry with, and the user
    must see the cert error rather than a KeyError out of the retry path."""
    if not _deps():
        skip("cryptography not installed")
    api = _api()
    with tempfile.TemporaryDirectory() as d:
        try:
            _provision_seq(api,
                           infos=[{"code": 1000, "data": {"checkCode": "c"}},
                                  {"code": 500, "message": "boom"}],
                           files=[{"code": 8200, "hint": "checkValue invalid"}],
                           tmpdir=d)
        except RuntimeError as e:
            assert "cert/file failed" in str(e)
        else:
            raise AssertionError("a dead retry challenge passed silently")


def test_provisioning_writes_the_cert_and_a_private_key():
    if not _deps():
        skip("cryptography not installed")
    api = _api()
    with tempfile.TemporaryDirectory() as d:
        (cert, key), calls = _provision(
            api, info={"code": 1000, "data": {"checkCode": "chk"}},
            file={"code": 1000, "data": {"cert": "-----FAKE CERT-----"}},
            tmpdir=d)
        assert open(cert).read() == "-----FAKE CERT-----"
        assert b"PRIVATE KEY" in open(key, "rb").read()
        assert calls[0][0].endswith("/info") and calls[1][0].endswith("/file")
        assert calls[1][1]["checkCode"] == "chk"
        assert "BEGIN CERTIFICATE REQUEST" in calls[1][1]["csr"]


def test_a_wrong_region_account_gets_the_explanation_not_a_dump():
    """Code 1501 means 'wrong backend', and everything before it succeeds -
    without this mapping it looks like a bug in the integration."""
    if not _deps():
        skip("cryptography not installed")
    api = _api()
    with tempfile.TemporaryDirectory() as d:
        for stage in ("info", "file"):
            info = ({"code": 1501, "hint": "geelyos verify error"}
                    if stage == "info" else {"code": 1000, "data": {"checkCode": "c"}})
            file = {"code": 1501, "hint": "geelyos verify error"}
            try:
                _provision(api, info=info, file=file, tmpdir=d)
            except api.GeelyRegionError as e:
                assert "regional backend" in str(e), stage
            else:
                raise AssertionError(f"{stage}: region mismatch not raised")


def test_any_other_cert_failure_raises_with_a_redacted_body():
    if not _deps():
        skip("cryptography not installed")
    api = _api()
    with tempfile.TemporaryDirectory() as d:
        for info, file, where in (
                ({"code": 500, "message": "boom"}, None, "cert/info"),
                ({"code": 1000, "data": {"checkCode": "c"}},
                 {"code": 500, "message": "boom"}, "cert/file")):
            try:
                _provision(api, info=info, file=file, tmpdir=d)
            except RuntimeError as e:
                assert f"{where} failed" in str(e), where
            else:
                raise AssertionError(f"{where} failure passed silently")


def test_unwritable_permissions_do_not_block_the_provisioned_cert():
    """chmod fails on filesystems that don't support modes (FAT, some mounts).
    The key still lands with the O_CREAT 0600, so degrade instead of failing
    the whole setup."""
    if not _deps():
        skip("cryptography not installed")
    api = _api()

    class _Os:
        def __getattr__(self, name):
            return getattr(os, name)

        def chmod(self, *a, **k):
            raise OSError("mode bits unsupported")

    orig = api.os
    api.os = _Os()
    try:
        with tempfile.TemporaryDirectory() as d:
            (cert, key), _ = _provision(
                api, info={"code": 1000, "data": {"checkCode": "chk"}},
                file={"code": 1000, "data": {"cert": "PEM"}},
                tmpdir=d)
            assert open(cert).read() == "PEM"
    finally:
        api.os = orig


# ----------------------------------------------------------- ios headers ---

def test_session_headers_carry_the_token_and_user_only_when_present():
    api = _api()
    bare = api._ios_headers()
    assert "token" not in bare and "userid" not in bare
    full = api._ios_headers(token="t-1", user_id="u-1", idfa="A", idfv="B")
    assert full["token"] == "t-1" and full["userid"] == "u-1"
    assert full["devicehardwareidfa"] == "A"


def test_the_legacy_session_survives_a_broken_trust_store_setup():
    """certifi missing or load_default_certs failing must degrade, not stop
    login - verification itself stays required either way."""
    if not _deps():
        skip("requests not installed")
    api = _api()
    import ssl
    # Import requests (and requests.certs) BEFORE breaking certifi: the first
    # `import requests` snapshots certifi.where at module level, so patching
    # first would poison every later requests user in the process.
    import requests  # noqa: F401
    import certifi
    orig_where, orig_load = certifi.where, ssl.SSLContext.load_default_certs

    def _boom(*a, **k):
        raise OSError("no bundle")

    certifi.where = _boom
    ssl.SSLContext.load_default_certs = _boom
    try:
        s = api._legacy_session()
        assert s is not None
    finally:
        certifi.where = orig_where
        ssl.SSLContext.load_default_certs = orig_load


# ------------------------------------------------------------- otp + login ---

def _solver_patch(api, results):
    """Sequence of solve() outcomes: dicts are returned, exceptions raised."""
    gs = load("geetest_solver")
    seq = list(results)

    def _solve(**kw):
        r = seq.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    class _Ctx:
        def __enter__(self):
            self.orig = gs.solve
            gs.solve = _solve

        def __exit__(self, *exc):
            gs.solve = self.orig
    return _Ctx()


_GOOD_CAPTCHA = {"status": "success", "data": {
    "result": "success", "pass_token": "pt", "lot_number": "ln",
    "captcha_output": "co", "gen_time": 1}}


def test_a_solved_captcha_sends_the_otp_email_request():
    if not _deps():
        skip("solver deps not installed")
    api = _api()
    session = _Session({"success": True, "code": 10000000})
    with _solver_patch(api, [_GOOD_CAPTCHA]), _SessionPatch(api, session):
        resp = api.cidpsso_send_otp("user@example.com", "AU")
    assert resp.get("success") is True
    method, url, headers, body = session.calls[0]
    assert url.endswith("/cidpsso/captcha/v3/getCaptcha")
    assert body["email"] == "user@example.com"
    assert body["passToken"] == "pt" and body["captchaScene"] == "101"


def test_a_rejected_solve_is_retried_until_one_passes():
    """The solver is ~85% accurate - a miss must burn a retry, not the flow."""
    if not _deps():
        skip("solver deps not installed")
    api = _api()
    session = _Session({"success": True, "code": 10000000})
    miss = {"status": "success", "data": {"result": "fail"}}
    with _solver_patch(api, [miss, ValueError("solver blew up"), _GOOD_CAPTCHA]), \
            _SessionPatch(api, session):
        resp = api.cidpsso_send_otp("user@example.com", "AU")
    assert resp.get("success") is True
    assert len(session.calls) == 1, "only the good solve may reach the server"


def test_a_server_rejection_returns_the_last_response_for_the_flow_to_show():
    if not _deps():
        skip("solver deps not installed")
    api = _api()
    session = _Session({"code": 500, "message": "rate limited"})
    with _solver_patch(api, [_GOOD_CAPTCHA, _GOOD_CAPTCHA]), _SessionPatch(api, session):
        resp = api.cidpsso_send_otp("user@example.com", "AU", max_attempts=2)
    assert resp == {"code": 500, "message": "rate limited"}
    assert len(session.calls) == 2


def test_all_attempts_missing_raises_with_the_last_error():
    if not _deps():
        skip("solver deps not installed")
    api = _api()
    session = _Session({"success": True})
    miss = {"status": "success", "data": {"result": "fail"}}
    with _solver_patch(api, [miss, miss]), _SessionPatch(api, session):
        try:
            api.cidpsso_send_otp("user@example.com", "AU", max_attempts=2)
        except RuntimeError as e:
            assert "all 2 attempts" in str(e)
        else:
            raise AssertionError("total failure returned silently")
    assert session.calls == []


def test_login_exchanges_the_code_with_the_email_login_type():
    if not _deps():
        skip("requests not installed")
    api = _api()
    session = _Session({"data": {"token": "tok", "userId": "u"}})
    with _SessionPatch(api, session):
        resp = api.cidpsso_login("user@example.com", "123456", "AU",
                                 idfa="A", idfv="B")
    assert resp["data"]["token"] == "tok"
    _, url, headers, body = session.calls[0]
    assert url.endswith("/cidpsso/user/v3/login")
    assert body["code"] == "123456"
    assert body["loginType"] == 3 and body["accountType"] == 2
    assert headers["countrycode"] == "AU"


def test_the_vehicle_list_needs_the_token_and_tolerates_an_empty_account():
    if not _deps():
        skip("requests not installed")
    api = _api()
    session = _Session({"data": [{"vin": FAKE_VIN}]})
    with _SessionPatch(api, session):
        cars = api.list_vehicles("tok-1", "u-1", "AU")
    assert cars == [{"vin": FAKE_VIN}]
    _, url, headers, _ = session.calls[0]
    assert url.endswith("/cidpcar/vehicleOwner/v2/controlCars")
    assert headers["token"] == "tok-1" and headers["userid"] == "u-1"
    empty = _Session({"code": 200})
    with _SessionPatch(api, empty):
        assert api.list_vehicles("tok-1") == []


def test_a_non_empty_garage_never_touches_the_apac_fallback():
    """The safety property of the #32 fallback, and the one worth pinning hardest:
    every account that lists cars today must take exactly the same single request
    it always took. If this test ever fails, working installs are at risk."""
    if not _deps():
        skip("requests not installed")
    api = _api()
    session = _Session({"data": [{"vin": FAKE_VIN}]})
    with _SessionPatch(api, session):
        assert api.list_vehicles("tok-1", "u-1", "AU") == [{"vin": FAKE_VIN}]
    assert len(session.calls) == 1, session.calls
    assert "m-lcmsam-kr" not in session.calls[0][1]


def test_an_empty_garage_falls_back_to_the_korean_v1_endpoint():
    """#32: an Australian account authenticates on the global gateway and finds an
    empty garage there, while its own app reads the cars from the KR host."""
    if not _deps():
        skip("requests not installed")
    api = _api()
    session = _Session({"data": []}, {"data": [{"vin": FAKE_VIN}]})
    with _SessionPatch(api, session):
        cars = api.list_vehicles("tok-1", "u-1", "AU")
    assert cars == [{"vin": FAKE_VIN}]
    assert len(session.calls) == 2, session.calls
    first, second = session.calls[0][1], session.calls[1][1]
    assert first.endswith("/cidpcar/vehicleOwner/v2/controlCars")
    assert second == ("https://m-lcmsam-kr.geely.com"
                      "/cidpcar/vehicleOwner/v1/listControlCars")
    # Same token on both, and nothing new invented for the second request.
    assert session.calls[1][2] == session.calls[0][2]


def test_the_vehicle_list_leaves_a_diagnosable_trail_without_leaking_vins():
    """#32 again, from lanpup: an owner hit "no vehicles", turned DEBUG on, and
    the discovery step logged nothing at all. It now logs record counts at
    each stage - and counts only, never the records, which carry the VIN."""
    if not _deps():
        skip("requests not installed")
    import logging
    api = _api()

    records = []

    class _Capture(logging.Handler):
        def emit(self, r):
            records.append(r.getMessage())

    logger = logging.getLogger(api.__name__)
    h = _Capture()
    logger.addHandler(h)
    old_level = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        # v2 empty, KR fallback also empty: the case that used to be silent.
        session = _Session({"data": []}, {"data": []})
        with _SessionPatch(api, session):
            assert api.list_vehicles("tok-1", "u-1", "AU") == []
    finally:
        logger.removeHandler(h)
        logger.setLevel(old_level)

    joined = " | ".join(records)
    assert "v2/controlCars returned 0" in joined, joined
    assert "fallback" in joined.lower(), joined
    assert FAKE_VIN not in joined, "a VIN reached the log"


def test_a_broken_fallback_still_reports_an_empty_garage():
    """An account that genuinely owns no cars must get the config flow's
    "no_vehicles" message, not a stack trace out of the fallback."""
    if not _deps():
        skip("requests not installed")
    api = _api()

    class _HalfDead(_Session):
        def get(self, url, headers=None, timeout=None):
            if "m-lcmsam-kr" in url:
                raise RuntimeError("kr host unreachable")
            return super().get(url, headers=headers, timeout=timeout)

    session = _HalfDead({"data": []})
    with _SessionPatch(api, session):
        assert api.list_vehicles("tok-1", "u-1", "AU") == []
