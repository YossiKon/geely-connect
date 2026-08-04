"""GeelyApi client behavior: signing, transport, JWT lifecycle, error mapping.

Everything here is offline. The requests layer is faked at module attributes
(_raw_https / _secure_tls_connect / socket) or at instance attributes
(_mtls_send / _authed_apis_call), and every module-level monkeypatch is
restored in a finally / context manager so tests stay order-independent.

Scope: the GeelyApi class and the helpers it calls. The module-level
login/captcha/cert-provisioning functions are covered elsewhere.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import ssl
import sys
import tempfile
import time
import types
import uuid

from conftest import FAKE_VIN, have_homeassistant, load  # noqa: F401
from run import skip  # noqa: F401

api = load("api")


# --------------------------------------------------------------- plumbing ---

class _patched:
    """Swap attributes on the loaded api module for a with-block, then restore.

    The module object is shared with every other test file (conftest.load
    caches it), so a patch that leaks would poison tests that run later.
    """

    def __init__(self, **attrs):
        self._attrs = attrs

    def __enter__(self):
        self._saved = {k: getattr(api, k) for k in self._attrs}
        for k, v in self._attrs.items():
            setattr(api, k, v)
        return self

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            setattr(api, k, v)
        return False


class _FakeTLSSock:
    """What _secure_tls_connect hands back: send/recv/close/getpeercert."""

    def __init__(self, response=b"", der=b"PINA"):
        self._rx = [response[i:i + 4096] for i in range(0, len(response), 4096)]
        self._der = der
        self.sent = b""
        self.closed = False

    def send(self, data):
        self.sent += data
        return len(data)

    def recv(self, n):
        return self._rx.pop(0) if self._rx else b""

    def close(self):
        self.closed = True

    def getpeercert(self, binary_form=False):
        return self._der


class _FakeRawSock:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _FakeSocketModule:
    """Stands in for the stdlib socket module inside api's globals."""

    def __init__(self):
        self.raws = []

    def create_connection(self, addr, timeout=None):
        r = _FakeRawSock()
        self.raws.append(r)
        return r


class _FakeCtx:
    """SSLContext stand-in: wrap_socket returns a socket or raises."""

    def __init__(self, outcome):
        self.outcome = outcome
        self.cert_chain = None
        self.wrapped = 0

    def load_cert_chain(self, cert, key=None):
        self.cert_chain = (cert, key)

    def wrap_socket(self, raw, server_hostname=None):
        self.wrapped += 1
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def _verify_error(code, message="unable to get local issuer certificate"):
    e = ssl.SSLCertVerificationError(message)
    e.verify_code = code
    e.verify_message = message
    return e


def _never_ctx():
    raise AssertionError("this TLS context must not be used in this scenario")


def _tmp_pin_path():
    return os.path.join(tempfile.mkdtemp(), "server_pins.json")


def _client(**over):
    kw = dict(
        app_id="APPID", app_secret="0" * 32, user_id="1234567", vin=FAKE_VIN,
        cidpsso_token="cidp-tok", client_id="CLIENT", vehicle_series="E245",
        vehicle_model="E245-J1", device_id="dev", cert_path="", key_path="",
        email="owner@example.com",
    )
    kw.update(over)
    return api.GeelyApi(**kw)


def _client_with_jwt(**over):
    a = _client(**over)
    a._jwt = "jwt0"
    a._jwt_exp = int(time.time()) + 3600
    return a


def _capture_authed(a, response=None):
    """Record what a high-level method hands to _authed_apis_call."""
    box = {}

    def fake(method, path, body):
        box.update(method=method, path=path, body=body)
        return {"code": 1000, "success": True, "data": {}} if response is None else response

    a._authed_apis_call = fake
    return box


def _scripted_mtls(a, responses):
    """Replace _mtls_send with a queue of canned JSON replies."""
    calls = []

    def fake(host, method, path, body, extra_headers=None):
        calls.append({"host": host, "method": method, "path": path,
                      "body": body, "headers": dict(extra_headers or {})})
        return 200, json.dumps(responses.pop(0)).encode()

    a._mtls_send = fake
    return calls


# ------------------------------------------------------------ HMAC signer ---

def test_percent_encoding_keeps_the_apps_safe_set():
    """The signer's canonical query must match the app byte-for-byte -
    /:, are encoded even though urllib would leave some of them alone,
    and the app's odd safe-set (!*'();@&=+$?#[]) passes through."""
    assert api._percent_encode_value("a/b:c,d e") == "a%2Fb%3Ac%2Cd%20e"
    assert api._percent_encode_value("!*'();@&=+$?#[]") == "!*'();@&=+$?#[]"


def test_the_sign_string_is_built_exactly_as_the_gateway_expects():
    """Sorted query, percent-encoded values, md5-of-body, uppercased method.
    Any drift here makes every request fail with an unhelpful 1445."""
    md5 = base64.b64encode(hashlib.md5(b"BODY").digest()).decode()
    ss = api._build_sign_string(
        method="get", path="/p", query="b=2&a=1/", accept=None,
        nonce="N", sig_version="1.0", timestamp_ms=123, body=b"BODY",
    )
    assert ss == (
        "application/json;responseformat=3\n"          # default Accept
        "x-api-signature-nonce:N\n"
        "x-api-signature-version:1.0\n"
        "\n"                                            # canonical headers end
        "a=1%2F&b=2\n"                                  # sorted + encoded
        f"{md5}\n"
        "123\n"
        "GET\n"                                         # upper-cased
        "/p"
    )


def test_an_empty_query_stays_empty_in_the_sign_string():
    # The trailing-& strip must not eat characters out of an empty query.
    ss = api._build_sign_string(
        method="POST", path="/p", query="", accept="A",
        nonce="N", sig_version="1.0", timestamp_ms=1, body=b"",
    )
    assert ss.split("\n")[4] == ""


def test_the_nonce_matches_the_android_format():
    """3hex-12hex 7alnum 13-digit ms timestamp. Cosmetic, but a nonce that
    stops looking like the app's is a fingerprinting risk."""
    for _ in range(20):
        n = api._make_nonce()
        assert re.fullmatch(
            r"[0-9a-f]{3}-[0-9a-f]{12}[A-Z0-9]{7}[0-9]{13}", n), n


def test_sign_headers_produce_a_verifiable_hmac():
    """Recompute the signature from the returned nonce/timestamp and the
    request parts; if the header block and the signed string ever disagree,
    the gateway rejects the request with a bare 1445."""
    a = _client()
    body = b'{"authCode":"x"}'
    h = a._sign_headers("POST", "https://apis.ecloudeu.com/x/y?b=2&a=1", body)
    ss = api._build_sign_string(
        method="POST", path="/x/y", query="b=2&a=1",
        accept="application/json;responseformat=3",
        nonce=h["X-api-signature-nonce"], sig_version="1.0",
        timestamp_ms=int(h["X-timestamp"]), body=body,
    )
    expected = base64.b64encode(
        hmac.new(a.app_secret.encode(), ss.encode(), hashlib.sha1).digest()
    ).decode()
    assert h["X-signature"] == expected
    assert h["X-APP-ID"] == "APPID"
    assert h["X-DEVICE-IDENTIFIER"] == "dev"
    assert h["Accept"] == "application/json;responseformat=3"


# --------------------------------------------------------- chunked bodies ---

def test_chunked_bodies_are_reassembled():
    assert api._parse_chunked(b"4\r\nWiki\r\n5\r\npedia\r\n0\r\n\r\n") == b"Wikipedia"


def test_garbage_chunking_ends_the_parse_instead_of_raising():
    """A truncated or corrupt chunk stream comes from the network; it must
    degrade to partial data, never take the poll down with a ValueError."""
    assert api._parse_chunked(b"XYZ\r\ndata") == b""       # non-hex size
    assert api._parse_chunked(b"no-crlf-anywhere") == b""  # no size line
    assert api._parse_chunked(b"4\r\nWiki") == b"Wiki"     # truncated tail


# ---------------------------------------------------------- error mapping ---

def test_an_auth_code_in_a_control_reply_is_an_auth_error():
    # 1402 during a control call means the session died, not that the command
    # was bad - it must trigger re-auth, not a "command failed" toast.
    try:
        api._check_control_resp({"code": 1402})
    except api.GeelyAuthError:
        pass
    else:
        raise AssertionError("auth failure was not escalated")


def test_a_gateway_ack_requires_the_success_flag_too():
    """code=1000 with success missing/false is not an accepted command."""
    try:
        api._check_control_resp({"code": 1000})
    except api.GeelyControlError as e:
        assert e.code == 1000
    else:
        raise AssertionError("accepted a reply without success=true")
    # ...but the server sometimes spells the flag as the string "true".
    assert api._check_control_resp({"code": "1000", "success": "true"})["code"] == "1000"


def test_control_error_composes_a_message_when_the_server_sends_none():
    e = api.GeelyControlError("failure", None)
    assert "failure" in str(e)


def test_a_non_string_key_value_counts_as_key_material():
    """redact() decides "key" fields by value; anything that is not a short
    parameter-name string must be masked rather than risked."""
    assert api._looks_like_key_material({"nested": 1}) is True
    assert api._looks_like_key_material("operation") is False
    assert api._looks_like_key_material("-----BEGIN EC PRIVATE KEY-----") is True
    assert api._looks_like_key_material("x" * 41) is True


# ------------------------------------------------------------ TLS contexts ---

def test_the_strict_context_verifies_chain_and_hostname():
    ctx = api._strict_ctx()
    assert ctx.check_hostname is True
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.options & api._TLS_LEGACY_RENEG


def test_a_broken_certifi_does_not_disable_verification():
    """certifi is an optional extra trust source; if it blows up, the OS
    store must still be used with verification ON, not silently off."""
    def _boom():
        raise RuntimeError("no bundle")

    saved = sys.modules.get("certifi")
    sys.modules["certifi"] = types.SimpleNamespace(where=_boom)
    try:
        ctx = api._strict_ctx()
        assert ctx.check_hostname is True
        assert ctx.verify_mode == ssl.CERT_REQUIRED
    finally:
        if saved is None:
            sys.modules.pop("certifi", None)
        else:
            sys.modules["certifi"] = saved


def test_the_pinning_context_disables_chain_checks_for_manual_pinning():
    # Verification happens against the stored pin after the handshake; the
    # context itself must not pretend to validate.
    ctx = api._pinning_ctx()
    assert ctx.check_hostname is False
    assert ctx.verify_mode == ssl.CERT_NONE
    assert ctx.options & api._TLS_LEGACY_RENEG


def test_the_pin_is_a_key_hash_not_a_cert_hash():
    """A key pin survives routine certificate renewal (same key pair); a cert
    pin would break every connection at renewal time."""
    try:
        import datetime as dt
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID
    except ImportError:
        skip("cryptography not installed")
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "unit-test")])
    now = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now).not_valid_after(now + dt.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    der = cert.public_bytes(serialization.Encoding.DER)
    spki = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    assert api._spki_sha256_b64(der) == base64.b64encode(
        hashlib.sha256(spki).digest()).decode()
    assert api._spki_sha256_b64(der) != base64.b64encode(
        hashlib.sha256(der).digest()).decode()


def test_close_quietly_swallows_close_failures():
    class _Sock:
        def close(self):
            raise OSError("already gone")
    api._close_quietly(_Sock())  # must not raise
    s = _FakeTLSSock()
    api._close_quietly(s)
    assert s.closed


def test_an_interrupted_pin_store_write_leaves_no_debris():
    """A failed write must remove the .tmp file: a stale tmp is confusing and
    a truncated real file would make _load_pin_store fail closed forever."""
    p = _tmp_pin_path()
    try:
        api._save_pin_store(p, {"h": {"pins": {"not", "json"}, "strict": False}})
    except TypeError:
        pass
    else:
        raise AssertionError("serialized the unserializable")
    assert not os.path.exists(p + ".tmp")
    assert not os.path.exists(p)


# ---------------------------------------------------- _secure_tls_connect ---
# The socket and both TLS contexts are faked; _spki_sha256_b64 is replaced by
# a function that just decodes the fake DER bytes, so a socket whose
# getpeercert() returns b"PINA" "hashes" to the pin string "PINA".

def test_a_publicly_valid_certificate_with_an_unexpected_key_is_refused():
    """A chain that validates is NOT sufficient for a pinned host: an
    interception proxy with an OS-trusted root produces exactly that."""
    ssock = _FakeTLSSock(der=b"EVIL")
    with _patched(socket=_FakeSocketModule(),
                  _strict_ctx=lambda: _FakeCtx(ssock),
                  _pinning_ctx=_never_ctx,
                  _spki_sha256_b64=lambda d: d.decode(),
                  _BUNDLED_TLS_PINS={"apis.ecloudeu.com": ("PINA",)}):
        p = _tmp_pin_path()
        try:
            api._secure_tls_connect("apis.ecloudeu.com", 443, pin_path=p)
        except api.GeelyTLSPinError as e:
            assert "not sufficient" in str(e)
        else:
            raise AssertionError("trusted a valid chain with the wrong key")
    assert ssock.closed
    assert not os.path.exists(p), "must not record strict for a refused peer"


def test_a_strictly_validated_host_is_recorded_as_strict():
    """Once a host has validated publicly, the pin store remembers it so no
    later connection can be pushed into the pinning fallback."""
    ssock = _FakeTLSSock(der=b"PINA")
    strict = _FakeCtx(ssock)
    p = _tmp_pin_path()
    with _patched(socket=_FakeSocketModule(),
                  _strict_ctx=lambda: strict,
                  _pinning_ctx=_never_ctx,
                  _spki_sha256_b64=lambda d: d.decode(),
                  _BUNDLED_TLS_PINS={"apis.ecloudeu.com": ("PINA",)}):
        got = api._secure_tls_connect(
            "apis.ecloudeu.com", 443, pin_path=p,
            client_cert="C.pem", client_key="K.pem")
    assert got is ssock
    assert strict.cert_chain == ("C.pem", "K.pem")
    assert api._load_pin_store(p)["apis.ecloudeu.com"]["strict"] is True


def test_a_host_with_no_pins_and_no_store_just_validates_strictly():
    # Public Geely hosts (login, captcha, cert) have no bundled pin and often
    # no pin store; strict validation alone must succeed for them.
    ssock = _FakeTLSSock()
    with _patched(socket=_FakeSocketModule(),
                  _strict_ctx=lambda: _FakeCtx(ssock),
                  _pinning_ctx=_never_ctx):
        got = api._secure_tls_connect("access-app-global.geely.com", 443,
                                      pin_path=None)
    assert got is ssock


def test_failing_to_record_strict_validation_is_not_fatal():
    """The strict marker is defense in depth; a read-only filesystem must not
    take the whole connection down when validation itself succeeded."""
    ssock = _FakeTLSSock(der=b"PINA")

    def _cannot_save(path, store):
        raise OSError("read-only filesystem")

    with _patched(socket=_FakeSocketModule(),
                  _strict_ctx=lambda: _FakeCtx(ssock),
                  _pinning_ctx=_never_ctx,
                  _spki_sha256_b64=lambda d: d.decode(),
                  _BUNDLED_TLS_PINS={"apis.ecloudeu.com": ("PINA",)},
                  _save_pin_store=_cannot_save):
        got = api._secure_tls_connect("apis.ecloudeu.com", 443,
                                      pin_path=_tmp_pin_path())
    assert got is ssock


def test_a_once_strict_host_can_never_downgrade_to_pinning():
    """The downgrade attack the audit found: force a verification failure,
    then be trusted via the weaker pinning path. The strict marker blocks it."""
    p = _tmp_pin_path()
    api._save_pin_store(p, {"apis.ecloudeu.com": {"pins": [], "strict": True}})
    sockmod = _FakeSocketModule()
    with _patched(socket=sockmod,
                  _strict_ctx=lambda: _FakeCtx(_verify_error(20)),
                  _pinning_ctx=_never_ctx):
        try:
            api._secure_tls_connect("apis.ecloudeu.com", 443, pin_path=p)
        except api.GeelyTLSPinError as e:
            assert "downgrade" in str(e)
        else:
            raise AssertionError("downgraded a strict host to pinning")
    assert sockmod.raws[0].closed


def test_the_pinning_fallback_is_reserved_for_private_pki_hosts():
    # Every other host answers with a public chain; a verification failure
    # there is an attack or an outage, never a reason to pin.
    with _patched(socket=_FakeSocketModule(),
                  _strict_ctx=lambda: _FakeCtx(_verify_error(20)),
                  _pinning_ctx=_never_ctx):
        try:
            api._secure_tls_connect("api.ecloudeu.com", 443, pin_path=None)
        except api.GeelyTLSPinError as e:
            assert "private CA" in str(e)
        else:
            raise AssertionError("pinning offered to a public host")


def test_an_expired_certificate_never_reaches_the_pinning_fallback():
    """verify_code 10 (expired) is a bad certificate however it was issued;
    only unknown-CA codes (18/19/20) may fall back."""
    with _patched(socket=_FakeSocketModule(),
                  _strict_ctx=lambda: _FakeCtx(
                      _verify_error(10, "certificate has expired")),
                  _pinning_ctx=_never_ctx):
        try:
            api._secure_tls_connect("apis.ecloudeu.com", 443, pin_path=None)
        except api.GeelyTLSPinError as e:
            assert "expired" in str(e)
        else:
            raise AssertionError("an expired cert reached the pinning path")


def test_the_pinning_fallback_accepts_the_bundled_key():
    pin_sock = _FakeTLSSock(der=b"PINA")
    pin_ctx = _FakeCtx(pin_sock)
    with _patched(socket=_FakeSocketModule(),
                  _strict_ctx=lambda: _FakeCtx(_verify_error(20)),
                  _pinning_ctx=lambda: pin_ctx,
                  _spki_sha256_b64=lambda d: d.decode(),
                  _BUNDLED_TLS_PINS={"apis.ecloudeu.com": ("PINA",)}):
        got = api._secure_tls_connect(
            "apis.ecloudeu.com", 443, pin_path=None,
            client_cert="C.pem", client_key="K.pem")
    assert got is pin_sock
    # mTLS material must be offered on the fallback handshake too.
    assert pin_ctx.cert_chain == ("C.pem", "K.pem")


def test_the_pinning_fallback_refuses_an_unknown_key():
    pin_sock = _FakeTLSSock(der=b"EVIL")
    with _patched(socket=_FakeSocketModule(),
                  _strict_ctx=lambda: _FakeCtx(_verify_error(20)),
                  _pinning_ctx=lambda: _FakeCtx(pin_sock),
                  _spki_sha256_b64=lambda d: d.decode(),
                  _BUNDLED_TLS_PINS={"apis.ecloudeu.com": ("PINA",)}):
        try:
            api._secure_tls_connect("apis.ecloudeu.com", 443, pin_path=None)
        except api.GeelyTLSPinError as e:
            assert "man-in-the-middle" in str(e)
        else:
            raise AssertionError("trusted a key that matches no pin")
    assert pin_sock.closed, "must close before any credentials could flow"


def test_a_server_that_sends_no_certificate_is_refused():
    pin_sock = _FakeTLSSock(der=None)
    with _patched(socket=_FakeSocketModule(),
                  _strict_ctx=lambda: _FakeCtx(_verify_error(20)),
                  _pinning_ctx=lambda: _FakeCtx(pin_sock)):
        try:
            api._secure_tls_connect("apis.ecloudeu.com", 443, pin_path=None)
        except api.GeelyTLSPinError as e:
            assert "no certificate" in str(e)
        else:
            raise AssertionError("trusted a peer with no certificate at all")
    assert pin_sock.closed


def test_first_contact_with_no_pin_store_is_refused():
    """TOFU is only acceptable if the pin can be remembered; a key that is
    trusted once and forgotten would be re-trusted differently tomorrow."""
    pin_sock = _FakeTLSSock(der=b"NEWKEY")
    with _patched(socket=_FakeSocketModule(),
                  _strict_ctx=lambda: _FakeCtx(_verify_error(20)),
                  _pinning_ctx=lambda: _FakeCtx(pin_sock),
                  _spki_sha256_b64=lambda d: d.decode(),
                  _BUNDLED_TLS_PINS={}):
        try:
            api._secure_tls_connect("apis.ecloudeu.com", 443, pin_path=None)
        except api.GeelyTLSPinError as e:
            assert "pin store" in str(e)
        else:
            raise AssertionError("trusted an unpersistable first-use key")
    assert pin_sock.closed


def test_first_contact_records_the_presented_key():
    pin_sock = _FakeTLSSock(der=b"NEWKEY")
    p = _tmp_pin_path()
    with _patched(socket=_FakeSocketModule(),
                  _strict_ctx=lambda: _FakeCtx(_verify_error(20)),
                  _pinning_ctx=lambda: _FakeCtx(pin_sock),
                  _spki_sha256_b64=lambda d: d.decode(),
                  _BUNDLED_TLS_PINS={}):
        got = api._secure_tls_connect("apis.ecloudeu.com", 443, pin_path=p)
    assert got is pin_sock
    entry = api._load_pin_store(p)["apis.ecloudeu.com"]
    assert entry == {"pins": ["NEWKEY"], "strict": False}


def test_an_unpersistable_first_use_pin_is_fatal():
    pin_sock = _FakeTLSSock(der=b"NEWKEY")

    def _cannot_save(path, store):
        raise OSError("disk full")

    with _patched(socket=_FakeSocketModule(),
                  _strict_ctx=lambda: _FakeCtx(_verify_error(20)),
                  _pinning_ctx=lambda: _FakeCtx(pin_sock),
                  _spki_sha256_b64=lambda d: d.decode(),
                  _BUNDLED_TLS_PINS={},
                  _save_pin_store=_cannot_save):
        try:
            api._secure_tls_connect("apis.ecloudeu.com", 443,
                                    pin_path=_tmp_pin_path())
        except api.GeelyTLSPinError as e:
            assert "persist" in str(e)
        else:
            raise AssertionError("continued with a pin that was never saved")
    assert pin_sock.closed


def test_network_errors_during_the_strict_handshake_propagate():
    # Only *verification* failures may consider the fallback; a reset is a
    # network problem and must surface as-is, with the socket closed.
    sockmod = _FakeSocketModule()
    with _patched(socket=sockmod,
                  _strict_ctx=lambda: _FakeCtx(ConnectionResetError("rst")),
                  _pinning_ctx=_never_ctx):
        try:
            api._secure_tls_connect("apis.ecloudeu.com", 443, pin_path=None)
        except ConnectionResetError:
            pass
        else:
            raise AssertionError("a network error was swallowed")
    assert sockmod.raws[0].closed


def test_network_errors_during_the_pinning_handshake_propagate():
    sockmod = _FakeSocketModule()
    with _patched(socket=sockmod,
                  _strict_ctx=lambda: _FakeCtx(_verify_error(20)),
                  _pinning_ctx=lambda: _FakeCtx(ConnectionResetError("rst"))):
        try:
            api._secure_tls_connect("apis.ecloudeu.com", 443, pin_path=None)
        except ConnectionResetError:
            pass
        else:
            raise AssertionError("a network error was swallowed")
    assert sockmod.raws[1].closed, "the fallback's raw socket must be closed"


# -------------------------------------------------------------- _raw_https ---

def test_raw_https_sends_a_wellformed_request_and_returns_the_body():
    sock = _FakeTLSSock(
        response=b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n"
                 b'{"code":10000000}')
    seen = {}

    def fake_connect(host, port, **kw):
        seen.update(host=host, port=port, **kw)
        return sock

    with _patched(_secure_tls_connect=fake_connect):
        out = api._raw_https("h.example", "POST", "/p",
                             {"token": "cidp-tok"}, b"data",
                             pin_path="/pins.json", timeout=15)
    assert out == b'{"code":10000000}'
    assert seen == {"host": "h.example", "port": 443, "pin_path": "/pins.json",
                    "client_cert": None, "client_key": None, "timeout": 15}
    head, _, body = sock.sent.decode().partition("\r\n\r\n")
    lines = head.split("\r\n")
    assert lines[0] == "POST /p HTTP/1.1"
    hdrs = dict(line.split(": ", 1) for line in lines[1:])
    assert hdrs["Host"] == "h.example"
    assert hdrs["Connection"] == "close"
    assert hdrs["Content-Length"] == "4"
    assert hdrs["token"] == "cidp-tok"
    assert body == "data"
    assert sock.closed


def test_raw_https_decodes_chunked_replies():
    sock = _FakeTLSSock(
        response=b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
                 b"5\r\nhello\r\n0\r\n\r\n")
    with _patched(_secure_tls_connect=lambda *a, **k: sock):
        out = api._raw_https("h", "GET", "/p", {}, b"", pin_path=None)
    assert out == b"hello"


def test_raw_https_rejects_header_injection_before_connecting():
    """A CR/LF in a header value would smuggle a second request onto the
    socket; the check must run before any connection is opened."""
    def _no_connect(*a, **k):
        raise AssertionError("socket opened before the CR/LF check")

    with _patched(_secure_tls_connect=_no_connect):
        for headers, path in ((({"token": "a\r\nX-Evil: 1"}), "/p"),
                              (({}), "/p\r\nGET /evil HTTP/1.1")):
            try:
                api._raw_https("h", "POST", path, headers, b"", pin_path=None)
            except ValueError:
                continue
            raise AssertionError(f"accepted {headers!r} {path!r}")


# ------------------------------------------------------ GeelyApi.__init__ ---

def test_the_pin_store_lives_beside_the_client_certificate():
    # The pins are per-VIN state, like the mTLS material they protect.
    cert = os.path.join("certs", FAKE_VIN, "cert.pem")
    a = _client(cert_path=cert, key_path=os.path.join("certs", FAKE_VIN, "key.pem"))
    assert a.pin_path == os.path.join("certs", FAKE_VIN, "server_pins.json")
    assert _client(cert_path="").pin_path is None


# -------------------------------------------------------------- _mtls_send ---

def test_mtls_send_builds_a_signed_request_over_the_pinned_transport():
    a = _client(cert_path=os.path.join("d", "cert.pem"),
                key_path=os.path.join("d", "key.pem"))
    sock = _FakeTLSSock(
        response=b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n"
                 b'{"code":1000}')
    seen = {}

    def fake_connect(host, port, **kw):
        seen.update(host=host, port=port, **kw)
        return sock

    with _patched(_secure_tls_connect=fake_connect):
        status, body = a._mtls_send(
            "apis.ecloudeu.com", "PUT", "/x", b'{"a":1}',
            extra_headers={"Authorization": "jwt0"})
    assert (status, body) == (200, b'{"code":1000}')
    # The client cert/key and the per-VIN pin store ride on every request.
    assert seen == {"host": "apis.ecloudeu.com", "port": 443,
                    "pin_path": a.pin_path, "client_cert": a.cert_path,
                    "client_key": a.key_path, "timeout": 30}
    head, _, sent_body = sock.sent.decode().partition("\r\n\r\n")
    lines = head.split("\r\n")
    assert lines[0] == "PUT /x HTTP/1.1"
    hdrs = dict(line.split(": ", 1) for line in lines[1:])
    assert hdrs["Host"] == "apis.ecloudeu.com"
    assert hdrs["connection"] == "close"
    assert hdrs["content-length"] == "7"
    assert hdrs["Authorization"] == "jwt0"          # extra header merged in
    assert "X-signature" in hdrs and "X-APP-ID" in hdrs
    assert sent_body == '{"a":1}'
    assert sock.closed


def test_mtls_send_decodes_chunked_responses():
    a = _client()
    sock = _FakeTLSSock(
        response=b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
                 b"D\r\n{\"code\":1000}\r\n0\r\n\r\n")
    with _patched(_secure_tls_connect=lambda *a_, **k: sock):
        status, body = a._mtls_send("h", "GET", "/x", b"")
    assert (status, body) == (200, b'{"code":1000}')


def test_an_unparseable_status_line_reads_as_zero():
    # A proxy error page or garbage must not raise inside the transport.
    a = _client()
    sock = _FakeTLSSock(response=b"BOGUS\r\n\r\nnot-http")
    with _patched(_secure_tls_connect=lambda *a_, **k: sock):
        status, body = a._mtls_send("h", "GET", "/x", b"")
    assert (status, body) == (0, b"not-http")


def test_mtls_send_rejects_crlf_in_server_supplied_headers():
    """The JWT and vehicle series/model come from server JSON and end up as
    header values; a newline there would smuggle a second request. Checked
    after the merge so extra_headers are covered too."""
    a = _client()

    def _no_connect(*a_, **k):
        raise AssertionError("socket opened before the CR/LF check")

    with _patched(_secure_tls_connect=_no_connect):
        try:
            a._mtls_send("h", "GET", "/x", b"",
                         extra_headers={"Authorization": "jwt\r\nX-Evil: 1"})
        except ValueError:
            pass
        else:
            raise AssertionError("accepted an injected header")


def test_mtls_send_rejects_crlf_in_the_path():
    a = _client()

    def _no_connect(*a_, **k):
        raise AssertionError("socket opened before the CR/LF check")

    with _patched(_secure_tls_connect=_no_connect):
        try:
            a._mtls_send("h", "GET", "/x\r\nGET /evil HTTP/1.1", b"")
        except ValueError:
            pass
        else:
            raise AssertionError("accepted an injected path")


def test_mtls_send_stops_reading_a_runaway_response():
    """A server (or MITM) streaming forever must not buffer unbounded data;
    the read loop caps out around 200 kB and returns."""
    class _EndlessSock(_FakeTLSSock):
        def __init__(self):
            super().__init__()
            self._head_sent = False

        def recv(self, n):
            if not self._head_sent:
                self._head_sent = True
                return b"HTTP/1.1 200 OK\r\n\r\n"
            return b"A" * 4096

    a = _client()
    sock = _EndlessSock()
    with _patched(_secure_tls_connect=lambda *a_, **k: sock):
        status, body = a._mtls_send("h", "GET", "/x", b"")
    assert status == 200
    assert 200_000 <= len(body) + 20 <= 250_000
    assert sock.closed


def test_the_socket_is_closed_even_when_send_fails():
    class _ExplodingSock:
        def __init__(self):
            self.closed = False

        def send(self, data):
            raise ConnectionResetError("rst")

        def close(self):
            self.closed = True

    a = _client()
    sock = _ExplodingSock()
    with _patched(_secure_tls_connect=lambda *a_, **k: sock):
        try:
            a._mtls_send("h", "GET", "/x", b"")
        except ConnectionResetError:
            pass
        else:
            raise AssertionError("send failure was swallowed")
    assert sock.closed


# -------------------------------------------------------- _get_access_code ---

def test_get_access_code_posts_the_cidpsso_token():
    a = _client()
    seen = {}

    def fake_raw(host, method, path, headers, body, **kw):
        seen.update(host=host, method=method, path=path,
                    headers=headers, body=body, kw=kw)
        return json.dumps({"code": 10000000,
                           "data": {"accessCode": "AC-1"}}).encode()

    with _patched(_raw_https=fake_raw):
        assert a._get_access_code() == "AC-1"
    assert seen["host"] == "m-lcmsam-eu.geely.com"     # EU mint by default
    assert seen["method"] == "POST"
    assert seen["path"] == "/cidpsso/oauth2/v1/getCode"
    assert seen["headers"]["token"] == "cidp-tok"
    uuid.UUID(json.loads(seen["body"])["state"])       # a real UUID state
    assert seen["kw"]["pin_path"] == a.pin_path


def test_a_rejected_cidpsso_token_is_an_auth_error_with_no_secrets():
    """60000000 means the token is dead (re-auth needed), and the exception
    text lands on HA's re-auth card - the token must be redacted from it."""
    a = _client()

    def fake_raw(*a_, **k):
        return json.dumps({"code": 60000000, "token": "TOPSECRET"}).encode()

    with _patched(_raw_https=fake_raw):
        try:
            a._get_access_code()
        except api.GeelyAuthError as e:
            assert "TOPSECRET" not in str(e)
        else:
            raise AssertionError("a dead token did not trigger re-auth")


def test_an_unexpected_getcode_reply_is_a_server_fault_not_a_reauth():
    a = _client()
    with _patched(_raw_https=lambda *a_, **k: json.dumps({"code": 500}).encode()):
        try:
            a._get_access_code()
        except api.GeelyAuthError:
            raise AssertionError("a server fault must not cost the user a captcha")
        except RuntimeError:
            pass
        else:
            raise AssertionError("a failed getCode was reported as success")


# --------------------------------------------------- _apac_session_exchange ---

def test_apac_exchange_needs_the_login_email():
    # receiverId is mandatory in the APAC body; without it the request is
    # doomed, so fail before minting (and burning) an access code.
    a = _client(email=None, control_host="apis.ecloudkr.com")
    try:
        a._apac_session_exchange()
    except api.GeelyAuthError as e:
        assert "email" in str(e)
    else:
        raise AssertionError("proceeded without the receiverId email")


def test_apac_exchange_sends_the_uppercase_header_shape():
    """The APAC gateway rejects the EU header spelling with 1445: signature
    headers must be UPPERCASE, the code must be minted by the KR host, and
    the signature must cover the charset=utf-8 Accept."""
    a = _client(control_host="apis.ecloudkr.com")
    seen = {}

    def fake_ac(host=None):
        seen["ac_host"] = host
        return "AC-KR"

    a._get_access_code = fake_ac

    def fake_raw(host, method, path, headers, body, **kw):
        seen.update(host=host, method=method, path=path,
                    headers=headers, body=body)
        return json.dumps({"resultCode": "0", "accessToken": "kr-jwt",
                           "userId": "uid-kr", "expiresIn": 7200}).encode()

    with _patched(_raw_https=fake_raw):
        d = a._apac_session_exchange()
    assert d["accessToken"] == "kr-jwt"
    assert seen["ac_host"] == "m-lcmsam-kr.geely.com"  # KR-minted, not EU
    assert seen["host"] == "api.ecloudkr.com"          # public host, no mTLS
    assert seen["path"] == "/auth-center/account/session"
    h = seen["headers"]
    assert "X-SIGNATURE" in h and "X-TIMESTAMP" in h
    assert "X-signature" not in h and "X-timestamp" not in h
    assert h["Authorization"] == "cidp-tok"
    assert h["X-VIN-ID"] == FAKE_VIN
    assert h["Accept"] == "application/json; charset=utf-8"
    body = json.loads(seen["body"])
    assert body == {"identityType": "geelyos", "authCode": "AC-KR",
                    "receiverId": "owner@example.com"}
    # The signature must be reproducible from the headers it shipped with.
    ss = api._build_sign_string(
        method="POST", path=seen["path"], query="",
        accept="application/json; charset=utf-8",
        nonce=h["X-api-signature-nonce"], sig_version="1.0",
        timestamp_ms=int(h["X-TIMESTAMP"]), body=seen["body"])
    expected = base64.b64encode(
        hmac.new(a.app_secret.encode(), ss.encode(), hashlib.sha1).digest()
    ).decode()
    assert h["X-SIGNATURE"] == expected


def test_a_transient_apac_fault_is_not_an_auth_error():
    """8500/1445 from the session service are server faults. GeelyAuthError
    would become ConfigEntryAuthFailed and cost the user a captcha plus a
    fresh email code; RuntimeError is retried with the snapshot kept."""
    a = _client(control_host="apis.ecloudkr.com")
    a._get_access_code = lambda host=None: "AC-KR"

    def fake_raw(*a_, **k):
        return json.dumps({"resultCode": "8500",
                           "accessToken": "MUST-NOT-LEAK"}).encode()

    with _patched(_raw_https=fake_raw):
        try:
            a._apac_session_exchange()
        except api.GeelyAuthError:
            raise AssertionError("a transient fault escalated to re-auth")
        except RuntimeError as e:
            assert "MUST-NOT-LEAK" not in str(e)
        else:
            raise AssertionError("a failed exchange was reported as success")


def test_an_apac_auth_rejection_is_an_auth_error():
    a = _client(control_host="apis.ecloudkr.com")
    a._get_access_code = lambda host=None: "AC-KR"
    with _patched(_raw_https=lambda *a_, **k:
                  json.dumps({"resultCode": "60000000"}).encode()):
        try:
            a._apac_session_exchange()
        except api.GeelyAuthError:
            pass
        else:
            raise AssertionError("a revoked token did not trigger re-auth")


# -------------------------------------------------------------- refresh_jwt ---

def test_eu_refresh_exchanges_the_access_code_and_caches_the_jwt():
    a = _client()
    a._get_access_code = lambda host="m-lcmsam-eu.geely.com": "AC-EU"
    calls = _scripted_mtls(a, [{"code": 1000, "data": {
        "accessToken": "JWT-1", "userId": "uid-9", "expiresIn": 3600}}])
    before = int(time.time())
    d = a.refresh_jwt()
    assert d["accessToken"] == "JWT-1"
    assert a._jwt == "JWT-1"
    assert a._jwt_uid == "uid-9"                 # server uid, not config uid
    assert before + 3599 <= a._jwt_exp <= int(time.time()) + 3600
    c = calls[0]
    assert c["host"] == "apis.ecloudeu.com"      # exchange runs on mTLS host
    assert c["method"] == "POST"
    assert c["path"] == "/auth/account/session/secure?identity_type=geelyos"
    assert json.loads(c["body"]) == {"authCode": "AC-EU"}


def test_a_rejected_eu_session_is_an_auth_error_without_the_token():
    a = _client()
    a._get_access_code = lambda host="m-lcmsam-eu.geely.com": "AC-EU"
    _scripted_mtls(a, [{"code": 60000001,
                        "data": {"accessToken": "SECRET-JWT"}}])
    try:
        a.refresh_jwt()
    except api.GeelyAuthError as e:
        assert "SECRET-JWT" not in str(e)
    else:
        raise AssertionError("a rejected session did not trigger re-auth")


def test_an_eu_server_fault_during_refresh_is_retryable_not_reauth():
    a = _client()
    a._get_access_code = lambda host="m-lcmsam-eu.geely.com": "AC-EU"
    _scripted_mtls(a, [{"code": 8500}])
    try:
        a.refresh_jwt()
    except api.GeelyAuthError:
        raise AssertionError("a server fault escalated to re-auth")
    except RuntimeError:
        pass
    else:
        raise AssertionError("a failed refresh was reported as success")


def test_apac_refresh_uses_the_apac_exchange_and_its_defaults():
    """The KR envelope is flat (no data wrapper) and may omit userId and
    expiresIn; the client must fall back to the config uid and 7200s."""
    a = _client(control_host="apis.ecloudkr.com")
    a._apac_session_exchange = lambda: {"accessToken": "JWT-KR"}
    before = int(time.time())
    d = a.refresh_jwt()
    assert d == {"accessToken": "JWT-KR"}
    assert a._jwt == "JWT-KR"
    assert a._jwt_uid == "1234567"               # falls back to config uid
    assert before + 7199 <= a._jwt_exp <= int(time.time()) + 7200


# -------------------------------------------------------- _authed_apis_call ---

def test_a_live_jwt_is_attached_to_every_authed_call():
    a = _client_with_jwt()
    calls = _scripted_mtls(a, [{"code": 1000, "data": {"x": 1}}])
    j = a._authed_apis_call("GET", "/path", b"")
    assert j == {"code": 1000, "data": {"x": 1}}
    h = calls[0]["headers"]
    assert h["Authorization"] == "jwt0"
    assert h["X-CLIENT-ID"] == "CLIENT"
    assert h["X-VEHICLE-SERIES"] == "E245"
    assert h["X-VEHICLE-MODEL"] == "E245-J1"
    assert h["X-Vehicle-IDENTIFIER"] == FAKE_VIN
    assert calls[0]["host"] == "apis.ecloudeu.com"


def test_a_1402_refreshes_the_jwt_and_retries_once():
    """Opening the phone app invalidates HA's JWT (code 1402). That must
    cost one silent refresh and a retry - not a failed poll or a re-auth."""
    a = _client_with_jwt()
    refreshes = []

    def fake_refresh():
        refreshes.append(1)
        a._jwt = "jwt1"
        a._jwt_exp = int(time.time()) + 3600

    a.refresh_jwt = fake_refresh
    calls = _scripted_mtls(a, [{"code": 1402}, {"code": 1000, "data": {}}])
    j = a._authed_apis_call("GET", "/path", b"")
    assert j == {"code": 1000, "data": {}}
    assert len(refreshes) == 1
    assert calls[0]["headers"]["Authorization"] == "jwt0"
    assert calls[1]["headers"]["Authorization"] == "jwt1", \
        "the retry must carry the fresh token"


def test_a_dead_cidpsso_token_during_the_1402_retry_propagates():
    # If the refresh itself fails with an auth error, the cidpsso token is
    # gone too - that IS a re-auth condition and must not be masked.
    a = _client_with_jwt()

    def dead_refresh():
        raise api.GeelyAuthError("cidpsso token rejected")

    a.refresh_jwt = dead_refresh
    calls = _scripted_mtls(a, [{"code": 1402}])
    try:
        a._authed_apis_call("GET", "/path", b"")
    except api.GeelyAuthError:
        pass
    else:
        raise AssertionError("a dead cidpsso token did not trigger re-auth")
    assert len(calls) == 1, "no retry once the refresh itself failed"


def test_a_second_1402_is_an_auth_error_with_the_vin_masked():
    """1402 after a successful refresh means the session is truly dead. The
    exception text reaches HA's re-auth card and the log, and the path
    contains the VIN - it must appear only as its last four characters."""
    a = _client_with_jwt()

    def fake_refresh():
        a._jwt = "jwt1"
        a._jwt_exp = int(time.time()) + 3600

    a.refresh_jwt = fake_refresh
    _scripted_mtls(a, [{"code": 1402}, {"code": 1402}])
    path = f"/remote-control/vehicle/status/{FAKE_VIN}"
    try:
        a._authed_apis_call("GET", path, b"")
    except api.GeelyAuthError as e:
        assert FAKE_VIN not in str(e), "full VIN leaked into re-auth text"
        assert f"...{FAKE_VIN[-4:]}" in str(e)
    else:
        raise AssertionError("a dead session was reported as success")


def test_an_auth_rejection_never_leaks_secrets_or_the_vin():
    a = _client_with_jwt()
    _scripted_mtls(a, [{"code": "60000000", "token": "TOPSECRET"}])
    try:
        a._authed_apis_call("GET", f"/x/{FAKE_VIN}", b"")
    except api.GeelyAuthError as e:
        assert "TOPSECRET" not in str(e)
        assert FAKE_VIN not in str(e)
    else:
        raise AssertionError("an auth rejection was reported as success")


# ------------------------------------------------------------- READ paths ---

def test_vehicle_status_asks_for_the_latest_snapshot():
    """The empty latest=/target= flags are what make the cloud return the
    freshest GPS; without them it serves a stale cached position."""
    a = _client()
    box = _capture_authed(a)
    a.vehicle_status()
    assert box["method"] == "GET"
    assert box["body"] == b""
    assert box["path"] == (f"/remote-control/vehicle/status/{FAKE_VIN}"
                           "?userId=1234567&latest=&target=")


def test_the_simple_read_endpoints_hit_their_documented_paths():
    a = _client()
    for call, path in (
        (a.vehicle_status_state,
         f"/remote-control/vehicle/status/state/{FAKE_VIN}"),
        (a.charging_reservation,
         f"/remote-control/charging/reservation/{FAKE_VIN}"),
        (lambda: a.charge_server_get("6"),
         f"/charge-server/ecarx_charge_set/{FAKE_VIN}?bizType=6"),
    ):
        box = _capture_authed(a)
        call()
        assert box["method"] == "GET" and box["body"] == b""
        assert box["path"] == path


def test_fetch_capabilities_returns_the_catalog_list():
    a = _client()
    catalog = [{"functionId": "remote_climate_control_2", "valueEnable": "1"}]
    box = _capture_authed(a, {"code": 1000, "data": {"list": catalog}})
    assert a.fetch_capabilities() == catalog
    assert box["path"].startswith(
        f"/geelyTCAccess/tcservices/capability/{FAKE_VIN}?pageSize=2000")


def test_fetch_capabilities_degrades_to_an_empty_list():
    """Entity setup keys off this catalog; a flaky endpoint must mean 'no
    extra entities this boot', never a failed integration setup."""
    a = _client()
    _capture_authed(a, {"code": 1000, "data": None})
    assert a.fetch_capabilities() == []
    _capture_authed(a, {"code": 1000, "data": {"list": None}})
    assert a.fetch_capabilities() == []

    def boom(method, path, body):
        raise RuntimeError("gateway down")

    a._authed_apis_call = boom
    assert a.fetch_capabilities() == []


# ---------------------------------------------------------------- control ---

def test_control_prefers_the_uid_the_jwt_exchange_returned():
    """The session exchange can return a different userId than the config
    entry stores; commands must run as the identity the JWT belongs to."""
    a = _client()
    a._jwt_uid = "uid-from-jwt"
    box = _capture_authed(a)
    a.control("RCC", [{"key": "operation", "value": "1"}], duration=180)
    body = json.loads(box["body"])
    assert body["userId"] == "uid-from-jwt"
    assert body["operationScheduling"]["duration"] == 180


def test_rapid_climate_bundles_ac_and_seat_heat():
    a = _client()
    box = _capture_authed(a)
    a.rapid_climate(ac=True, temp="22", heat_seats=["11", "19"],
                    vent_seats=None, vlt=True)
    assert box["method"] == "POST"
    assert box["path"] == f"/charge-server/ecarx_charge_set/{FAKE_VIN}"
    body = json.loads(box["body"])
    assert body["bizType"] == "7"
    assert body["command"] == "immediately"
    assert body["ac"] == "true" and body["vlt"] == "true"  # strings, not bools
    assert body["temp"] == "22"
    assert body["heat"] == [{"level": "3", "pos": "11"},
                            {"level": "3", "pos": "19"}]
    assert "ventilation" not in body, "unused seat blocks must be absent"


def test_rapid_climate_vent_variant_omits_the_heat_block():
    a = _client()
    box = _capture_authed(a)
    a.rapid_climate(ac=False, temp="20", heat_seats=None,
                    vent_seats=["11"], vlt=False)
    body = json.loads(box["body"])
    assert body["ac"] == "false" and body["vlt"] == "false"
    assert body["ventilation"] == [{"level": "3", "pos": "11"}]
    assert "heat" not in body


def test_a_rejected_rapid_climate_is_a_control_error():
    a = _client()
    _capture_authed(a, {"code": "failure", "message": "Operation failed"})
    try:
        a.rapid_climate(ac=True, temp="22", heat_seats=None,
                        vent_seats=None, vlt=False)
    except api.GeelyControlError as e:
        assert e.code == "failure"
    else:
        raise AssertionError("a rejected command was reported as success")
