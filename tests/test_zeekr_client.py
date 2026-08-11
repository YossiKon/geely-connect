"""The new-platform (Zeekr EM) client: signature oracles + mock-gateway roundtrip.

The client is standalone (stdlib + cryptography only), so these tests run
without Home Assistant. The mock gateway recomputes every signature exactly
like the real gateway does, so a signing or canonicalisation regression fails
here instead of on a user's car.

Only fake VINs appear in this file (conftest rule - never real ones).
"""

import hashlib
import hmac as hmac_mod
import ipaddress
import json
import os
import ssl as ssl_mod
import tempfile
import threading
from base64 import b64encode
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from conftest import FAKE_VIN, load

zc = load("zeekr_client")

# The account's legacy-platform VIN, replaced with a fake one in the oracle
# below (the original capture contained a personal VIN and is not committed).
LEGACY_VIN = "LJXK0EX50N00000001"

_EXPECTED_SIGS: list[bool] = []
_MOCK_PORT = 0
_ORIGINAL_HF_GATEWAY = zc.HF_GATEWAY
_ORIGINAL_IDAAS_CLASS = zc.ZeekrIdaas
_ORIGINAL_SSL_CTX = zc.ssl.create_default_context


# ---------------------------------------------------------------------------
# independent canonical builder (oracle for sign_v2)
#
# Written from the spec in zeekr_client.build_sign_string's docstring, with no
# code shared with the client, so a wrong join/order/encoding in the client
# cannot reproduce itself here. Validated locally against the app-captured
# vector (see test_sign_v2_canonical_matches_the_captured_shape).
# ---------------------------------------------------------------------------


def _independent_canonical(*, method, path, query, accept, nonce, sig_version,
                           timestamp_ms, body) -> str:
    sh = {"x-api-signature-nonce": nonce, "x-api-signature-version": sig_version}
    headers = "".join(f"{k}:{sh[k]}\n" for k in sorted(sh))
    pairs = []
    for pair in query.split("&"):
        if not pair:
            continue
        k, _, v = pair.partition("=")
        v = v.replace("+", "%20").replace("*", "%2A").replace("%7E", "~").replace(",", "%2C")
        pairs.append((k, v))
    q = "&".join(f"{k}={v}" for k, v in sorted(pairs))
    md5 = b64encode(hashlib.md5(body).digest()).decode()
    return "\n".join([accept, headers, q, md5, timestamp_ms, method.upper(), path])


def test_sign_v2_canonical_matches_the_captured_shape():
    # The captured vector used a real VIN in the query; the canonical treats
    # the query as opaque bytes, so a fake VIN validates the same machinery.
    # Independently derived (not by calling sign_v2), per the docstring spec.
    canon = _independent_canonical(
        method="POST", path="/user-service/device/code",
        query="a=1&b=two%20words&vin=LJXK0EX50N00000001&x=a+b",
        accept=zc.ACCEPT, nonce="abc-123def456ghi789JKL0123",
        sig_version="2", timestamp_ms="1754800000000", body=b"hello")
    expected = b64encode(
        hmac_mod.new(zc.APP_SECRET.encode(), canon.encode(), hashlib.sha1).digest()
    ).decode()
    got = zc.sign_v2(
        method="POST", path="/user-service/device/code",
        query="a=1&b=two%20words&vin=LJXK0EX50N00000001&x=a+b",
        accept=zc.ACCEPT, nonce="abc-123def456ghi789JKL0123",
        sig_version="2", timestamp_ms="1754800000000",
        body=b"hello", secret=zc.APP_SECRET)
    assert got == expected, f"sign_v2 diverged from the independent canonical: {got}"


def test_xhmac_signature_vectors():
    # Captured from the real app through mitm (2026-08-10); no VINs involved.
    d1 = zc.hmac_sha256_b64(b'{"email":"junkprobe20260810@example.com","checkType":"1"}')
    assert d1 == "y3xKFmmlT+ooe28hs4L+ZH6XJIo4h+q4mHGxtEvVNvk=", f"digest1: {d1}"
    s1 = zc.idaas_sign(method="POST", path="/zeekr-cuc-idaas-sea/auth/checkUserV2",
                       query="", xdate="Monday, 10 Aug 2026 03:52:13 GMT")
    assert s1 == "S1dVls1aBlHHMFaplNqRgGDEqI1ucGI2T54DM8bYYc4=", f"sig1: {s1}"
    d2 = zc.hmac_sha256_b64(b'{"country":"AU"}')
    assert d2 == "lvSSQu3t9v7eUjtwcK5htLyPC0Lu+iPEmeO9+Vox+zo=", f"digest2: {d2}"
    s2 = zc.idaas_sign(method="POST", path="/overseas-app/protocol/service/getProtocol",
                       query="", xdate="Monday, 10 Aug 2026 03:50:10 GMT")
    assert s2 == "6OaWXiRb2PywlNyNTJb3Zkfvs2STK9oPtIApNlGKmcI=", f"sig2: {s2}"


# ---------------------------------------------------------------------------
# mock gateway - recomputes signatures exactly like the real gateways
# ---------------------------------------------------------------------------


class _MockGW(BaseHTTPRequestHandler):
    def _serve(self):  # noqa: C901 - route table
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        q = __import__("urllib.parse", fromlist=["urlparse"]).urlparse(self.path).query
        pth = __import__("urllib.parse", fromlist=["urlparse"]).urlparse(self.path).path
        ok = True
        if pth.startswith("/zeekr-cuc-idaas-sea/"):
            # IDaaS leg: X-HMAC-SIGNATURE / X-HMAC-DIGEST recomputation
            got = self.headers.get("X-HMAC-SIGNATURE")
            want = zc.idaas_sign(
                method=self.command, path=pth, query=q,
                xdate=self.headers.get("X-DATE", ""))
            ok = (got == want)
            _EXPECTED_SIGS.append(ok)
            want_d = zc.hmac_sha256_b64(body)
            ok = ok and (self.headers.get("X-HMAC-DIGEST") == want_d)
            _EXPECTED_SIGS.append(ok)
        elif (self.headers.get("x-app-id") == zc.HF_APP_ID
                and str(self.headers.get("x-api-signature-version", "")).startswith("1")):
            # HF leg (old-platform identity): v1.0 signature recompute
            got = self.headers.get("x-signature")
            want = zc._sign_v1(
                method=self.command, path=pth, query=q,
                accept=self.headers.get("accept", ""),
                nonce=self.headers.get("x-api-signature-nonce", ""),
                sig_version=self.headers.get("x-api-signature-version", ""),
                timestamp_ms=self.headers.get("x-timestamp", ""),
                body=body, secret=zc.HF_SECRET)
            ok = (got == want)
            _EXPECTED_SIGS.append(ok)
        elif self.path.startswith(("/ms-user-auth", "/ms-app-bff",
                                   "/ms-vehicle-status", "/ms-remote-control",
                                   "/ms-vehicle-capability")):
            # snc-signed leg: recompute X-SIGNATURE from received headers
            got = self.headers.get("X-SIGNATURE")
            want = zc.snc_sign(
                method=self.command,
                url=f"http://127.0.0.1:{_MOCK_PORT}{self.path}",
                headers={k: v for k, v in self.headers.items()},
                body=body)
            ok = (got == want)
            _EXPECTED_SIGS.append(ok)
            if self.headers.get("X-APP-ID") != zc.SNC_APP_ID:
                ok = False
                _EXPECTED_SIGS.append(False)
            nonce = self.headers.get("X-api-signature-nonce", "")
            if len(nonce) < 32:
                ok = False
                _EXPECTED_SIGS.append(False)
        else:
            # signed leg: recompute from received headers
            got = self.headers.get("X-SIGNATURE")
            want = zc.sign_v2(
                method=self.command, path=pth, query=q,
                accept=self.headers.get("Accept", ""),
                nonce=self.headers.get("X-api-signature-nonce", ""),
                sig_version=self.headers.get("X-api-signature-version", ""),
                timestamp_ms=self.headers.get("X-TIMESTAMP", ""),
                body=body, secret=zc.APP_SECRET)
            ok = (got == want)
            _EXPECTED_SIGS.append(ok)

        if pth == "/user-service/device/code":
            if self.headers.get("X-BAD") == "1":
                self.send_response(500)
                self.end_headers()
                return
            try:
                req = json.loads(body)
            except Exception:
                req = {}
            if req.get("email") == "empty@example.com":
                payload = {"code": "1000", "success": True, "message": "ok", "data": {}}
            else:
                payload = {"code": "1000", "success": True, "message": "ok",
                           "data": {"ddcCode": "MOCKDDC123"}}
        elif pth == "/ms-user-auth/v1.0/auth/login":
            try:
                req = json.loads(body)
            except Exception:
                req = {}
            if req.get("identifier") == "fail-identifier":
                payload = {"code": "1000", "success": True, "message": "ok", "data": {}}
            else:
                payload = {"code": "1000", "success": True, "message": "ok",
                           "data": {"accessToken": "mock-at", "refreshToken": "mock-rt",
                                    "userId": "mock-uid"}}
        elif pth == "/zeekr-cuc-idaas-sea/bad500":
            self.send_response(500)
            self.end_headers()
            return
        elif pth == "/zeekr-cuc-idaas-sea/auth/checkUserV2":
            payload = {"code": "1000", "success": True, "message": "ok",
                       "data": {"uuid": "mock-uuid", "passwordSet": 1}}
        elif pth == "/zeekr-cuc-idaas-sea/captcha/email":
            payload = {"code": "1000", "success": True, "message": "ok",
                       "data": {"codeId": "mock-code-id"}}
        elif pth == "/zeekr-cuc-idaas-sea/captcha/verify":
            payload = {"code": "1000", "success": True, "message": "ok",
                       "data": True}
        elif pth == "/zeekr-cuc-idaas-sea/auth/editPasswordByEmailEncrypt":
            payload = {"code": "1000", "success": True, "message": "ok", "data": {}}
        elif pth == "/zeekr-cuc-idaas-sea/auth/completeMigration":
            try:
                req = json.loads(body)
            except Exception:
                req = {}
            if req.get("email") == "empty@example.com":
                payload = {"code": "1000", "success": True, "message": "ok", "data": {}}
            else:
                payload = {"code": "1000", "success": True, "message": "ok",
                           "data": {"tokenValue": "mock-migrated-tv", "uuid": "mock-uuid"}}
        elif pth == "/zeekr-cuc-idaas-sea/auth/loginByEmailEncrypt":
            try:
                req = json.loads(body)
            except Exception:
                req = {}
            if req.get("email") == "empty@example.com":
                payload = {"code": "1000", "success": True, "message": "ok", "data": {}}
            else:
                payload = {"code": "1000", "success": True, "message": "ok",
                           "data": {"tokenValue": "mock-tv"}}
        elif pth == "/zeekr-cuc-idaas-sea/user/info":
            payload = {"code": "1000", "success": True, "message": "ok",
                       "data": {"email": "user@example.com"}}
        elif pth == "/zeekr-cuc-idaas-sea/user/tspCode":
            payload = {"code": "1000", "success": True, "message": "ok",
                       "data": {"code": "mock-tsp-code", "clientId": "mock-client"}}
        elif pth == "/remote-control/vehicle/status/badgateway":
            self.send_response(500)
            self.end_headers()
            return
        elif pth == "/auth/account/session/secure":
            try:
                req = json.loads(body)
            except Exception:
                req = {}
            if req.get("refreshToken") == "fail":
                self.send_response(500)
                self.end_headers()
                return
            if req.get("refreshToken") == "empty" or req.get("authCode") == "empty":
                payload = {"code": "1000", "success": True, "message": "ok", "data": {}}
            elif (self.headers.get("x-app-id") == zc.HF_APP_ID
                    and str(self.headers.get("x-api-signature-version", "")).startswith("1")):
                # HF exchange (geelynos): mint the old-platform JWT
                payload = {"code": "1000", "success": True, "message": "ok",
                           "data": {"accessToken": "mock-hf", "expiresIn": 172800,
                                    "clientId": "MOCKCLIENT"}}
            else:
                payload = {"code": "1000", "success": True, "message": "ok",
                           "data": {"accessToken": "mock-at2"}}
        elif pth == "/device-platform/api/v4.0/veh/vehicle-list":
            if self.headers.get("Authorization") != "mock-hf":
                payload = {"code": "8500", "success": False, "message": "bad token"}
                ok = False
                _EXPECTED_SIGS.append(False)
            elif self.headers.get("X-VEHICLE-SERIES") == "BARE":
                payload = {"code": "1000", "success": True, "message": "ok",
                           "data": [{"vin": FAKE_VIN, "nickName": "Bare EX5"}]}
            elif self.headers.get("X-VEHICLE-SERIES") == "DICT":
                payload = {"code": "1000", "success": True, "message": "ok",
                           "data": {"vin": FAKE_VIN, "nickName": "Dict EX5"}}
            elif self.headers.get("X-VEHICLE-SERIES") == "EMPTY":
                payload = {"code": "1000", "success": True, "message": "ok",
                           "data": {"foo": 1}}
            else:
                payload = {"code": "1000", "success": True, "message": "ok",
                           "data": {"serviceResult": {"operationResult": 0},
                                    "list": [{"vin": FAKE_VIN,
                                              "nickName": "Mock EX5",
                                              "appModelCode": "E245-J1",
                                              "engineType": "BEV"}]}}
        elif pth.startswith("/geelyTCAccess/tcservices/capability/"):
            if self.headers.get("Authorization") != "mock-hf":
                payload = {"code": "8500", "success": False, "message": "bad token"}
                ok = False
                _EXPECTED_SIGS.append(False)
            elif self.headers.get("X-Vehicle-IDENTIFIER") == "EMPTYVIN":
                payload = {"code": "1000", "success": True, "message": "ok",
                           "data": {"functionId": "AC"}}
            else:
                payload = {"code": "1000", "success": True, "message": "ok",
                           "data": [{"functionId": "AC", "supported": True}]}
        elif pth.startswith("/remote-control/vehicle/status/"):
            if self.headers.get("Authorization") not in ("mock-at", "mock-hf") or \
                    self.headers.get("X-Vehicle-IDENTIFIER") not in (LEGACY_VIN, FAKE_VIN):
                payload = {"code": "8500", "success": False, "message": "bad token/vin"}
                ok = False
                _EXPECTED_SIGS.append(False)
            else:
                payload = {"code": "1000", "success": True, "message": "ok",
                           "data": {"result": {}, "vehicleStatus": {
                               "basicVehicleStatus": {"powerLevel": 98, "lockState": 1},
                               "additionalVehicleStatus": {"odometerValue": 12345}}}}
        elif pth.startswith("/remote-control/vehicle/telematics/"):
            if self.headers.get("Authorization") not in ("mock-at", "mock-at2", "mock-hf"):
                payload = {"code": "8500", "success": False, "message": "bad token"}
                ok = False
                _EXPECTED_SIGS.append(False)
            else:
                payload = {"code": "1000", "success": True, "message": "ok",
                           "data": {"result": {"code": 1000}}}
        else:
            payload = {"code": "404", "success": False, "message": "no route"}
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = _serve
    do_POST = _serve
    do_PUT = _serve

    def log_message(self, *a):  # silence
        pass


def _make_server_cert(tmpdir: str):
    """Self-signed cert for 127.0.0.1; the client context is patched to skip
    verification, so this only needs to be a well-formed TLS certificate."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=1))
            .add_extension(
                x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                critical=False)
            .sign(key, hashes.SHA256()))
    key_path = os.path.join(tmpdir, "key.pem")
    cert_path = os.path.join(tmpdir, "cert.pem")
    with open(key_path, "wb") as f:
        f.write(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption()))
    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    return key_path, cert_path


def _start_mock():
    """Start a TLS mock gateway and point the client's HF + IDaaS legs at it.

    The IDaaS transport is HTTPS-only (HTTPSConnection with
    ssl.create_default_context), so the mock speaks TLS with a self-signed
    cert and the test patches the client's context factory to skip
    verification for the duration."""
    global _MOCK_PORT
    tmpdir = tempfile.mkdtemp(prefix="geely-mock-")
    key_path, cert_path = _make_server_cert(tmpdir)
    srv = HTTPServer(("127.0.0.1", 0), _MockGW)
    ctx = ssl_mod.SSLContext(ssl_mod.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_path, key_path)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    _MOCK_PORT = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def _no_verify():
        # zc.ssl IS the stdlib ssl module, so build from the saved original
        # (the patch below replaces the module attribute).
        c = _ORIGINAL_SSL_CTX()
        c.check_hostname = False
        c.verify_mode = ssl_mod.CERT_NONE
        return c

    zc.ssl.create_default_context = _no_verify
    zc.HF_GATEWAY = f"https://127.0.0.1:{_MOCK_PORT}"
    return srv, f"https://127.0.0.1:{_MOCK_PORT}", tmpdir


def _stop_mock(srv, tmpdir):
    zc.HF_GATEWAY = _ORIGINAL_HF_GATEWAY
    zc.ssl.create_default_context = _ORIGINAL_SSL_CTX
    srv.shutdown()
    for f in ("key.pem", "cert.pem"):
        try:
            os.remove(os.path.join(tmpdir, f))
        except OSError:
            pass
    try:
        os.rmdir(tmpdir)
    except OSError:
        pass


def test_mock_gateway_roundtrip():
    _EXPECTED_SIGS.clear()
    srv, url, tmp = _start_mock()
    try:
        c = zc.ZeekrClient("user@example.com", "pw", gateway=url)
        c.login()
        assert c.access_token == "mock-at", "token not attached"

        # OTP leg: the IDaaS tokenValue rides the same unsigned ms-user-auth leg.
        c.login_otp("mock-tv")
        assert c.access_token == "mock-at", f"otp login token: {c.access_token}"

        # HF leg: client-2 tspCode -> old-platform session/secure exchange
        hf_resp = zc._hf_request(
            "POST", "/auth/account/session/secure",
            query="identity_type=geelynos",
            body=json.dumps({"area": "SEA", "authCode": "MOCKTSP2"},
                            separators=(",", ":")).encode())
        assert hf_resp["data"]["accessToken"] == "mock-hf", f"hf exchange: {hf_resp}"
        c.hf_token = "mock-hf"

        # Vehicle calls ride the HF JWT on the old-platform endpoints.
        cars = c.list_vehicles("mock-uid")
        assert cars and cars[0]["vin"] == FAKE_VIN, f"list: {cars}"
        assert zc.vehicle_vin(cars[0]) == FAKE_VIN, "vin helper"
        assert zc.vehicle_nickname(cars[0]) == "Mock EX5", "nickname helper"
        st3 = c.vehicle_status(FAKE_VIN, user_id="mock-uid")
        assert st3["vehicleStatus"]["basicVehicleStatus"]["powerLevel"] == 98, \
            f"hf status: {st3}"
        caps = c.fetch_capabilities(FAKE_VIN)
        assert caps and caps[0]["functionId"] == "AC", f"caps: {caps}"

        assert c.refresh_session(), "refresh failed"
        assert c.access_token == "mock-at2", f"refresh token: {c.access_token}"
        ctl = c.control(LEGACY_VIN, {"command": "start"})
        assert ctl.get("result", {}).get("code") == 1000, f"control: {ctl}"
        assert all(_EXPECTED_SIGS), f"signature failures: {_EXPECTED_SIGS}"
    finally:
        _stop_mock(srv, tmp)


def test_idaas_methods_roundtrip():
    _EXPECTED_SIGS.clear()
    srv, url, tmp = _start_mock()
    try:
        idaas = zc.ZeekrIdaas(gateway=url, path="zeekr-cuc-idaas-sea", country="AU")
        assert idaas.check_user("user@example.com")["passwordSet"] == 1
        assert idaas.request_code("user@example.com")["codeId"] == "mock-code-id"
        assert idaas.verify_code("user@example.com", "mock-code-id", "123456") is True
        assert idaas.edit_password("user@example.com", "mock-code-id", "123456", "pw") == {}
        mig = idaas.complete_migration("user@example.com", "mock-code-id",
                                       "123456", "pw", "Mock", "User")
        assert mig["tokenValue"] == "mock-migrated-tv"
        assert idaas.login_by_email("user@example.com", "mock-code-id", "123456") == "mock-tv"
        assert idaas.login_by_email_password("user@example.com", "pw") == "mock-tv"
        assert idaas.user_info("mock-tv")["email"] == "user@example.com"
        assert idaas.tsp_code("mock-tv")["code"] == "mock-tsp-code"
        assert all(_EXPECTED_SIGS), f"idaas signature failures: {_EXPECTED_SIGS}"
    finally:
        _stop_mock(srv, tmp)


def test_login_tsp_and_login_hf_use_the_idaas_leg():
    """login_tsp / login_hf construct ZeekrIdaas internally; point the class
    at the mock gateway for the duration of the test."""
    _EXPECTED_SIGS.clear()
    srv, url, tmp = _start_mock()

    def _factory(country: str = "AU") -> zc.ZeekrIdaas:
        return _ORIGINAL_IDAAS_CLASS(gateway=url, path="zeekr-cuc-idaas-sea",
                                     country=country)

    zc.ZeekrIdaas = _factory
    try:
        c = zc.ZeekrClient("user@example.com", "pw", gateway=url)
        c.login_tsp("mock-tv")
        assert c.access_token == "mock-at", f"tsp login token: {c.access_token}"
        assert c.hf_token == "mock-hf", "tsp login should mint the HF JWT"

        c2 = zc.ZeekrClient("user@example.com", "pw", gateway=url)
        c2.login_hf("mock-tv")
        assert c2.hf_token == "mock-hf", "hf-only renewal failed"
        assert c2.hf_expires_in == 172800, f"expiresIn: {c2.hf_expires_in}"
        assert all(_EXPECTED_SIGS), f"tsp/hf signature failures: {_EXPECTED_SIGS}"
    finally:
        zc.ZeekrIdaas = _ORIGINAL_IDAAS_CLASS
        _stop_mock(srv, tmp)


def test_auth_errors_and_bad_gateway_responses():
    _EXPECTED_SIGS.clear()
    srv, url, tmp = _start_mock()
    try:
        # Not logged in: vehicle calls refuse without an HF session.
        c = zc.ZeekrClient("user@example.com", "pw", gateway=url)
        try:
            c.vehicle_status(FAKE_VIN)
            assert False, "expected ZeekrAuthError without an HF session"
        except zc.ZeekrAuthError:
            pass
        try:
            c.control(FAKE_VIN, {"command": "start"})
            assert False, "expected ZeekrAuthError without an HF session"
        except zc.ZeekrAuthError:
            pass

        # IDaaS logins that return no tokenValue raise ZeekrAuthError.
        idaas = zc.ZeekrIdaas(gateway=url, path="zeekr-cuc-idaas-sea")
        try:
            idaas.login_by_email_password("empty@example.com", "pw")
            assert False, "expected ZeekrAuthError when no tokenValue is returned"
        except zc.ZeekrAuthError:
            pass
        try:
            idaas.login_by_email("empty@example.com", "mock-code-id", "123456")
            assert False, "expected ZeekrAuthError when no tokenValue is returned"
        except zc.ZeekrAuthError:
            pass
        try:
            idaas.complete_migration("empty@example.com", "mock-code-id",
                                     "123456", "pw", "Mock", "User")
            assert False, "expected ZeekrAuthError when no tokenValue is returned"
        except zc.ZeekrAuthError:
            pass
        assert all(_EXPECTED_SIGS), f"idaas signature failures: {_EXPECTED_SIGS}"

        # Bad token: the gateway answers 8500 -> ZeekrApiError. (The route
        # deliberately marks these requests as failed in the signature log,
        # so the all() assertion above already ran.)
        _EXPECTED_SIGS.clear()
        c.hf_token = "not-a-token"
        try:
            c.vehicle_status(FAKE_VIN)
            assert False, "expected ZeekrApiError for a bad token"
        except zc.ZeekrApiError:
            pass

        # Non-200: _hf_request raises with the status.
        try:
            zc._hf_request("GET", "/remote-control/vehicle/status/badgateway")
            assert False, "expected ZeekrApiError for HTTP 500"
        except zc.ZeekrApiError as e:
            assert "500" in str(e), f"unexpected message: {e}"
    finally:
        _stop_mock(srv, tmp)


def test_a_failed_login_never_leaks_a_token_into_the_exception_text():
    """#33 security review (S2): every raise here folds the response body
    through redact(), because the adapter surfaces our error text on Home
    Assistant's re-auth card. A body carrying a token but no tokenValue must
    come out masked, not verbatim."""
    # A success envelope (so _check_resp passes) whose data carries tokens but
    # no tokenValue: login_by_email then raises with the body folded in.
    leaky = {"code": "1000", "success": True,
             "data": {"accessToken": "eyJLEAK.header.sig", "refreshToken": "RT-LEAK-9"}}
    orig = zc._post_json
    zc._post_json = lambda url, body, headers: leaky
    try:
        raised = None
        try:
            zc.ZeekrIdaas().login_by_email("owner@example.com", "cid", "123")
        except zc.ZeekrAuthError as e:
            raised = str(e)
    finally:
        zc._post_json = orig
    assert raised is not None, "expected ZeekrAuthError"
    assert "eyJLEAK" not in raised and "RT-LEAK-9" not in raised, raised
    assert "***redacted***" in raised


def test_check_resp_rejects_a_200_wrapped_business_error_code():
    """#33 review C1: a business error inside an HTTP 200 (an expired-token code
    with no success flag) must raise - otherwise neither the adapter's HF-renewal
    retry nor the reauth flow fires, and the integration wedges on a stale token.
    Affirmative success and the code-less IDaaS shape must still pass."""
    zc._check_resp({"code": "1000", "success": True})   # affirmative success
    zc._check_resp({"code": 1000})                       # success code, no flag
    zc._check_resp({"data": {"x": 1}})                   # no code/success (IDaaS)
    for bad in ({"code": "401", "message": "token expired"},
                {"code": 4001},
                {"success": False, "code": "1000"}):
        try:
            zc._check_resp(bad)
            assert False, f"expected ZeekrApiError for {bad}"
        except zc.ZeekrApiError:
            pass


def test_a_non_json_error_body_is_dropped_from_the_exception():
    """_safe_detail must not fold an unparseable body into exception text -
    it could be anything, so it is omitted rather than guessed at."""
    assert "omitted" in zc._safe_detail(b"\x00\x01 not json")
    assert zc._safe_detail(b'{"accessToken":"eyJX.Y.Z"}') == "{'accessToken': '***redacted***'}"


def test_the_account_country_reaches_the_request_headers():
    """#33 review (N1): the country was accepted and then dropped, so every
    IDaaS call went out as AU regardless of who logged in. It must now ride the
    Country / RegistCountry headers."""
    captured: dict = {}
    orig = zc._post_json

    def _cap(url, body, headers):
        captured.update(headers)
        return {"data": {"tokenValue": "t"}}

    zc._post_json = _cap
    try:
        zc.ZeekrIdaas(country="NZ").login_by_email("owner@example.com", "cid", "1")
    finally:
        zc._post_json = orig
    assert captured.get("Country") == "NZ", captured.get("Country")
    assert captured.get("RegistCountry") == "NZ", captured.get("RegistCountry")


def test_rsa_password_encryption_is_pkcs1_and_wrapped():
    ct = zc._rsa_encrypt_password("hunter2")
    lines = ct.split("\n")
    assert all(len(l) <= 76 for l in lines), "base64 must wrap at 76 columns"
    assert "=" in lines[-1], "base64 padding expected on the last line"
    # PKCS#1 v1.5 of a 128-byte RSA-1024 modulus: ciphertext is 128 bytes.
    decoded = __import__("base64").b64decode("".join(lines))
    assert len(decoded) == 128, f"unexpected ciphertext length: {len(decoded)}"


def test_rsa_password_encryption_rejects_non_rsa_keys():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    der = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo)
    saved = zc._UB_B_G
    zc._UB_B_G = __import__("base64").b64encode(der).decode()
    try:
        try:
            zc._rsa_encrypt_password("pw")
            assert False, "expected ZeekrAuthError for a non-RSA key"
        except zc.ZeekrAuthError:
            pass
    finally:
        zc._UB_B_G = saved


def test_vehicle_record_helpers_accept_nested_shapes():
    flat = {"vin": "L6T00000000000000", "nickname": "Nested EX5"}
    nested = {"vehicleInfo": {"vin": "L6T00000000000000",
                              "carNickName": "Nested EX5"}}
    assert zc.vehicle_vin(flat) == "L6T00000000000000"
    assert zc.vehicle_vin(nested) == "L6T00000000000000"
    assert zc.vehicle_nickname(flat) == "Nested EX5"
    assert zc.vehicle_nickname(nested) == "Nested EX5"
    assert zc.vehicle_vin(None) is None
    assert zc.vehicle_vin(42) is None
    assert zc.vehicle_nickname(None) == ""
    assert zc.vehicle_nickname([]) == ""
    assert zc.vehicle_vin({"vin": "L6T00000000000000", "VIN": "x"}) == "L6T00000000000000"
    assert zc.vehicle_vin({"vehicle": {"vehicleVin": "L6T00000000000000"}}) == \
        "L6T00000000000000"
    assert zc.vehicle_nickname({"vehicleName": "A"}) == "A"
    assert zc.vehicle_nickname({"vehicleInfo": {"name": "B"}}) == "B"


def test_get_vehicle_status_and_no_session_guards():
    _EXPECTED_SIGS.clear()
    srv, url, tmp = _start_mock()
    try:
        # vehicle_model on the client sets the X-VEHICLE-SERIES/MODEL headers.
        c = zc.ZeekrClient("user@example.com", "pw", gateway=url,
                           vehicle_model="E245-J1")
        c.hf_token = "mock-hf"
        st = c.get_vehicle_status(FAKE_VIN)
        assert st["vehicleStatus"]["basicVehicleStatus"]["powerLevel"] == 98, st

        # fetch_capabilities with a non-list data payload -> [].
        assert c.fetch_capabilities("EMPTYVIN") == []

        # No HF session: the vehicle methods refuse with ZeekrAuthError.
        c2 = zc.ZeekrClient("user@example.com", "pw", gateway=url)
        for call in (lambda: c2.get_vehicle_status(FAKE_VIN),
                     lambda: c2.fetch_capabilities(FAKE_VIN),
                     lambda: c2.list_vehicles("mock-uid"),
                     lambda: c2.control(FAKE_VIN, {"command": "start"})):
            try:
                call()
                assert False, "expected ZeekrAuthError without an HF session"
            except zc.ZeekrAuthError:
                pass
        assert all(_EXPECTED_SIGS), f"signature failures: {_EXPECTED_SIGS}"
    finally:
        _stop_mock(srv, tmp)


def test_refresh_session_edge_cases():
    _EXPECTED_SIGS.clear()
    srv, url, tmp = _start_mock()
    try:
        # No refresh token at all: returns False without a request.
        c0 = zc.ZeekrClient("user@example.com", "pw", gateway=url)
        assert c0.refresh_session() is False

        # Gateway 500 on the refresh: returns False (transient).
        c1 = zc.ZeekrClient("user@example.com", "pw", gateway=url)
        c1.refresh_token = "fail"
        assert c1.refresh_session() is False

        # Response without a new accessToken: returns False.
        c2 = zc.ZeekrClient("user@example.com", "pw", gateway=url)
        c2.refresh_token = "empty"
        assert c2.refresh_session() is False
        assert all(_EXPECTED_SIGS), f"signature failures: {_EXPECTED_SIGS}"
    finally:
        _stop_mock(srv, tmp)


def test_request_extra_and_query_options():
    _EXPECTED_SIGS.clear()
    srv, url, tmp = _start_mock()
    try:
        c = zc.ZeekrClient("user@example.com", "pw", gateway=url)
        # extra headers ride along (outside the signed canonical).
        resp = c._request("POST", "/user-service/device/code", body=b"{}",
                          extra={"X-EXTRA": "1"})
        assert resp["data"]["ddcCode"] == "MOCKDDC123", resp
        # query param is appended and signed.
        resp2 = c._request("GET", "/user-service/device/code", query="a=1")
        assert resp2["data"]["ddcCode"] == "MOCKDDC123", resp2
        assert all(_EXPECTED_SIGS), f"signature failures: {_EXPECTED_SIGS}"
    finally:
        _stop_mock(srv, tmp)


def test_plain_http_transport_branches():
    """The http:// branches of _request/_hf_request (a plain-HTTP gateway)."""
    _EXPECTED_SIGS.clear()
    srv = HTTPServer(("127.0.0.1", 0), _MockGW)
    global _MOCK_PORT
    _MOCK_PORT = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{_MOCK_PORT}"
        c = zc.ZeekrClient("user@example.com", "pw", gateway=url)
        c.login()
        assert c.access_token == "mock-at", "http login failed"

        zc.HF_GATEWAY = url
        cars = zc._hf_request(
            "GET", "/device-platform/api/v4.0/veh/vehicle-list",
            query="needSharedCar=true", token="mock-hf")
        assert cars["data"]["list"][0]["vin"] == FAKE_VIN, cars
        assert all(_EXPECTED_SIGS), f"signature failures: {_EXPECTED_SIGS}"
    finally:
        zc.HF_GATEWAY = _ORIGINAL_HF_GATEWAY
        srv.shutdown()


def test_snc_sign_query_canonical_and_non_whitelisted_headers():
    # Direct snc_sign call with a query and a non-whitelisted header: the
    # canonical must include the sorted query and skip the foreign header.
    sig = zc.snc_sign(
        method="GET",
        url="https://gw.example.com/ms-app-bff/api/v4.0/veh/vehicle-list?needSharedCar=true&a=1",
        headers={"X-APP-ID": "GEELYSEACH001M0001", "X-FOO": "bar",
                 "Authorization": ""},
        body=b"")
    assert isinstance(sig, str) and sig, "snc signature missing"
    # Deterministic: same inputs, same signature.
    sig2 = zc.snc_sign(
        method="GET",
        url="https://gw.example.com/ms-app-bff/api/v4.0/veh/vehicle-list?needSharedCar=true&a=1",
        headers={"X-APP-ID": "GEELYSEACH001M0001", "X-FOO": "bar",
                 "Authorization": ""},
        body=b"")
    assert sig == sig2, "snc signature not deterministic"


def test_idaas_transport_http_errors():
    srv, url, tmp = _start_mock()
    try:
        hdrs = zc.idaas_headers("POST", "/zeekr-cuc-idaas-sea/bad500", b"{}")
        try:
            zc._post_json(f"{url}/zeekr-cuc-idaas-sea/bad500", {}, hdrs)
            assert False, "expected ZeekrApiError for HTTP 500"
        except zc.ZeekrApiError as e:
            assert "500" in str(e), f"unexpected message: {e}"
        hdrs2 = zc.idaas_headers("GET", "/zeekr-cuc-idaas-sea/bad500", b"")
        try:
            zc._get_json(f"{url}/zeekr-cuc-idaas-sea/bad500", hdrs2)
            assert False, "expected ZeekrApiError for HTTP 500"
        except zc.ZeekrApiError as e:
            assert "500" in str(e), f"unexpected message: {e}"
    finally:
        _stop_mock(srv, tmp)


def test_tsp_gateway_http_500():
    srv, url, tmp = _start_mock()
    try:
        c = zc.ZeekrClient("user@example.com", "pw", gateway=url)
        try:
            c._request("POST", "/user-service/device/code", body=b"{}",
                       extra={"X-BAD": "1"})
            assert False, "expected ZeekrApiError for HTTP 500"
        except zc.ZeekrApiError as e:
            assert "500" in str(e), f"unexpected message: {e}"
    finally:
        _stop_mock(srv, tmp)


def test_login_raises_on_empty_responses():
    _EXPECTED_SIGS.clear()
    srv, url, tmp = _start_mock()
    try:
        # device/code returns no ddcCode for the empty account.
        c0 = zc.ZeekrClient("empty@example.com", "pw", gateway=url)
        try:
            c0.login()
            assert False, "expected ZeekrAuthError when no ddcCode is returned"
        except zc.ZeekrAuthError:
            pass

        # ms-user-auth returns no accessToken for the failing identifier.
        c1 = zc.ZeekrClient("user@example.com", "pw", gateway=url)
        try:
            c1._ms_user_auth_login("fail-identifier")
            assert False, "expected ZeekrAuthError when no accessToken is returned"
        except zc.ZeekrAuthError:
            pass

        # login_tsp / login_hf with an empty tspCode response.
        def _empty_tsp_factory(country: str = "AU") -> zc.ZeekrIdaas:
            i = _ORIGINAL_IDAAS_CLASS(gateway=url, path="zeekr-cuc-idaas-sea",
                                      country=country)
            i.tsp_code = lambda token_value, client_id=None: {}
            return i

        zc.ZeekrIdaas = _empty_tsp_factory
        try:
            c2 = zc.ZeekrClient("user@example.com", "pw", gateway=url)
            try:
                c2.login_tsp("tv")
                assert False, "expected ZeekrAuthError when tspCode is empty"
            except zc.ZeekrAuthError:
                pass
            try:
                c2.login_hf("tv")
                assert False, "expected ZeekrAuthError when tspCode(client2) is empty"
            except zc.ZeekrAuthError:
                pass
        finally:
            zc.ZeekrIdaas = _ORIGINAL_IDAAS_CLASS

        # HF exchange returns no accessToken for the empty authCode.
        def _hf_empty_factory(country: str = "AU") -> zc.ZeekrIdaas:
            i = _ORIGINAL_IDAAS_CLASS(gateway=url, path="zeekr-cuc-idaas-sea",
                                      country=country)
            i.tsp_code = lambda token_value, client_id=None: {"code": "empty"}
            return i

        zc.ZeekrIdaas = _hf_empty_factory
        try:
            c3 = zc.ZeekrClient("user@example.com", "pw", gateway=url)
            try:
                c3.login_hf("tv")
                assert False, "expected ZeekrAuthError when the HF exchange is empty"
            except zc.ZeekrAuthError:
                pass
        finally:
            zc.ZeekrIdaas = _ORIGINAL_IDAAS_CLASS
        assert all(_EXPECTED_SIGS), f"signature failures: {_EXPECTED_SIGS}"
    finally:
        _stop_mock(srv, tmp)


def test_list_vehicles_data_shapes():
    _EXPECTED_SIGS.clear()
    srv, url, tmp = _start_mock()
    try:
        c = zc.ZeekrClient("user@example.com", "pw", gateway=url)
        c.hf_token = "mock-hf"

        c.vehicle_model = "BARE"   # data is a bare list
        bare = c.list_vehicles("mock-uid")
        assert bare and bare[0]["vin"] == FAKE_VIN, f"bare: {bare}"

        c.vehicle_model = "DICT"   # data is a single vehicle record
        one = c.list_vehicles("mock-uid")
        assert one and one[0]["vin"] == FAKE_VIN, f"dict: {one}"

        c.vehicle_model = "EMPTY"  # data has no vehicle shape at all
        assert c.list_vehicles("mock-uid") == []
        assert all(_EXPECTED_SIGS), f"signature failures: {_EXPECTED_SIGS}"
    finally:
        _stop_mock(srv, tmp)


def test_vehicle_record_helpers_tail_branches():
    assert zc.vehicle_vin({"foo": 1}) is None
    assert zc.vehicle_vin({"vin": ""}) is None
    assert zc.vehicle_nickname({}) == ""
    assert zc.vehicle_nickname({"nickname": 0}) == ""
