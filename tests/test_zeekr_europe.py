"""European auth/discovery and renewal, with fake accounts and no network."""
import base64
import hashlib
import hmac
import json
from unittest.mock import patch
from urllib.parse import urlparse

from conftest import FAKE_VIN, load

zc = load("zeekr_client")
regions = load("zeekr_regions")

# Deliberately invented app identity. No vendor keys or vendor-key vectors.
FAKE_KEYS = {
    "hmac_access_key": "test-only-access", "hmac_secret": "test-only-hmac-secret",
    "snc_secret": "test-only-snc-secret",
    "vin_key_hex": b"0123456789abcdef".hex(),
    "vin_iv_hex": b"fedcba9876543210".hex(),
}
FAKE_PROFILE = regions.profile_from_keys(FAKE_KEYS)


def _assert_idaas_signature(method, url, raw, headers):
    parsed = urlparse(url)
    profile = FAKE_PROFILE
    canonical = "\n".join((
        method, parsed.path, parsed.query, profile.hmac_access_key,
        headers["X-DATE"], "",
    )).encode()
    expected = base64.b64encode(
        hmac.new(profile.hmac_secret.encode(), canonical, hashlib.sha256).digest()
    ).decode()
    assert headers["X-HMAC-SIGNATURE"] == expected
    assert headers["X-HMAC-DIGEST"] == base64.b64encode(
        hmac.new(profile.hmac_secret.encode(), raw, hashlib.sha256).digest()
    ).decode()


def test_europe_headers_match_fixed_protocol_vector():
    headers = zc.idaas_headers(
        "POST", "/zeekr-cuc-idaas/auth/checkUserV2",
        b'{"email":"owner@example.invalid","checkType":"1"}',
        xdate="Tuesday, 20 Oct 2026 12:00:00 GMT", country="ES", profile=FAKE_PROFILE,
    )
    assert headers["X-HMAC-SIGNATURE"] == "hPXM2HVZ6Iwdhov+zVmJRswcKuBC08n+AAEyQoWDAuc="
    assert headers["X-HMAC-DIGEST"] == "DUC/4M+HWAUfNg1VDz6FXdJZDIThlL/SOB6WYWgU0I0="
    assert headers["msgAppId"] == "10027"
    assert headers["msgClientId"] == headers["app-authorization"] == "1026"
    assert headers["AppVersion"] == "1.1.1"
    assert headers["Country"] == headers["RegistCountry"] == "ES"
    assert headers["Tmp-Tenant-Code"] == zc.TMP_TENANT


def test_every_idaas_operation_signs_its_actual_path():
    calls = []

    def post(url, raw, headers):
        calls.append(("POST", url))
        _assert_idaas_signature("POST", url, raw, headers)
        data = True if url.endswith("/captcha/verify") else {"tokenValue": "fake-token"}
        return {"success": True, "data": data}

    def get(url, headers):
        calls.append(("GET", url))
        _assert_idaas_signature("GET", url, b"", headers)
        return {"success": True, "data": {"code": "fake-tsp-code"}}

    # An explicit path also must be signed verbatim. Previously, requests
    # used the custom URL but signatures always used the SEA path.
    for path in (None, "custom-user-center"):
        client = zc.ZeekrIdaas(country=" es ", path=path, profile=FAKE_PROFILE)
        with patch.object(zc, "_post_raw_json", post), patch.object(zc, "_get_json", get):
            client.check_user("owner@example.invalid")
            client.request_code("owner@example.invalid")
            client.login_by_email("owner@example.invalid", "fake-id", "fake-code")
            client.login_by_email_password("owner@example.invalid", "fake-password")
            client.edit_password("owner@example.invalid", "fake-id", "fake-code", "fake-password")
            client.verify_code("owner@example.invalid", "fake-id", "fake-code")
            client.complete_migration("owner@example.invalid", "fake-id", "fake-code",
                                      "fake-password", "Example", "Owner")
            client.user_info("fake-token")
            client.tsp_code("fake-token")
        expected = path or "zeekr-cuc-idaas"
        assert all(urlparse(url).path.startswith(f"/{expected}/") for _, url in calls)
        calls.clear()


class _Response:
    status = 200

    def __init__(self, data):
        self.data = data

    def read(self):
        return json.dumps({"success": True, "code": "000000", "data": self.data}).encode()


class _EuropeanGateway:
    calls = []

    def __init__(self, host, *args, **kwargs):
        self.host = host

    def request(self, method, path, body=b"", headers=None):
        headers = headers or {}
        self.calls.append((self.host, method, path, body, headers))
        if self.host == "gateway-pub-azure.zeekr.eu":
            assert path.startswith("/zeekr-cuc-idaas/")
            _assert_idaas_signature(method, f"https://{self.host}{path}", body, headers)
            if "/user/tspCode?" in path:
                self.data = {"code": "fake-tsp-code"}
            else:
                self.data = {"tokenValue": "fake-user-token"}
        elif self.host == "eu-snc-tsp-api-gw.zeekrlife.com":
            assert headers["X-PROJECT-ID"] == "GEELY_EU"
            assert headers["X-APP-ID"] == "GEELYSEACH001M0001"
            assert headers["ACCEPT-LANGUAGE"] == "en-ES"
            assert headers["X-SIGNATURE"] == zc.snc_sign(
                method=method, url=f"https://{self.host}{path}", headers=headers,
                body=body, secret=FAKE_PROFILE.snc_secret,
            )
            assert headers["X-SIGNATURE"] != zc.snc_sign(
                method=method, url=f"https://{self.host}{path}", headers=headers, body=body,
            )
            if path.endswith("/auth/login"):
                self.data = {"accessToken": "fake-access", "refreshToken": "fake-refresh",
                             "userId": "123456"}
            else:
                self.data = [{"vin": FAKE_VIN}]
        elif self.host == "api.ecloudeu.com":
            if path.startswith("/auth/account/session/secure"):
                assert json.loads(body)["area"] == "EU"
                self.data = {"accessToken": "fake-hf", "expiresIn": 172800}
            else:
                assert headers["Authorization"] == "fake-hf"
                self.data = {"list": [{"vin": FAKE_VIN}]}
        else:
            raise AssertionError(f"Unexpected regional host: {self.host}")

    def getresponse(self):
        return _Response(self.data)

    def close(self):
        pass


def test_europe_login_vehicle_list_and_hf_renewal_use_one_region():
    _EuropeanGateway.calls = []
    with patch.object(zc.http.client, "HTTPSConnection", _EuropeanGateway):
        token = zc.ZeekrIdaas(country="ES", profile=FAKE_PROFILE).login_by_email_password(
            "owner@example.invalid", "fake-password"
        )
        client = zc.ZeekrClient("", "", profile=FAKE_PROFILE)
        client.country_code = "ES"  # same order as config_flow
        client.login_tsp(token)
        assert client.list_vehicles(client.user_id) == [{"vin": FAKE_VIN}]
        assert client.list_vehicles_bff() == [{"vin": FAKE_VIN}]
        client.login_hf(token)
        client.vehicle_status_resp(FAKE_VIN)
        client.fetch_capabilities(FAKE_VIN)
    assert {c[0] for c in _EuropeanGateway.calls} == {
        "gateway-pub-azure.zeekr.eu",
        "eu-snc-tsp-api-gw.zeekrlife.com", "api.ecloudeu.com",
    }
    assert len([c for c in _EuropeanGateway.calls if "/user/tspCode?" in c[2]]) == 3


def test_adapter_restores_country_before_renewing_sessions():
    adapter_module = load("zeekr_adapter")
    adapter = adapter_module.ZeekrAdapter(
        email="owner@example.invalid", vin=FAKE_VIN, user_id="123456",
        access_token="expired", refresh_token="expired", country_code="ES",
        password="fake-password", profile=FAKE_PROFILE,
    )
    assert adapter._client.gateway == FAKE_PROFILE.tsp_gateway
    with patch.object(zc.http.client, "HTTPSConnection", _EuropeanGateway):
        adapter._renew_hf()
    assert adapter._client.hf_token == "fake-hf"
    assert adapter._client.access_token == "fake-access"


def test_europe_vin_probe_uses_only_the_european_material_and_restores_state():
    client = zc.ZeekrClient("", "", access_token="fake-access", profile=FAKE_PROFILE)
    client.country_code = "ES"
    client.enc_vin = "previous"
    candidates = []

    def capabilities():
        candidates.append(client.enc_vin)
        return [{"functionCode": "fake-capability"}]

    with patch.object(client, "capabilities_new", capabilities):
        result = client.probe_x_vin(FAKE_VIN)
    assert candidates == [result]
    assert result == "BPjtwPEl20V5daPpc8uVRZOJFiL/uCGWDxc5hwWLrM0="
    assert client.enc_vin == "previous"
    with patch.object(client, "capabilities_new", return_value=[]):
        assert client.probe_x_vin(FAKE_VIN) == ""
    assert client.enc_vin == "previous"


def test_europe_probe_takes_079001_as_a_verdict_not_a_dead_session():
    """One key on this profile, so a 079001 was the probe's only failure, and the
    bare "401" among the session hints made that "every candidate met a dead
    session": a renewal at every start (signing the phone app out), then a skip
    at debug with no warning and no Repairs issue. 079001 is not the session's
    answer (#63, #53), so it is a plain "no match" now."""
    client = zc.ZeekrClient("", "", access_token="fake-access", profile=FAKE_PROFILE)
    client.country_code = "ES"

    def refused():
        raise zc.ZeekrApiError(
            "HTTP 401: {'code': '079001', 'msg': '[SDK]此接口未被授权，无法访问!'}")

    with patch.object(client, "capabilities_new", refused):
        assert client.probe_x_vin(FAKE_VIN) == ""


def test_explicit_profile_selects_servers_independently_of_country():
    eu = zc.ZeekrClient("", "", profile=FAKE_PROFILE)
    sea = zc.ZeekrClient("", "")
    eu.country_code = "AU"
    for country in ("AU", "GB", "ES", "FR", "DE", "NL"):
        sea.country_code = country
        assert sea.gateway == zc.GATEWAY
        assert zc.ZeekrIdaas(country=country).base == f"{zc.IDAAS_GATEWAY}/{zc.IDAAS_PATH}"
        assert zc.idaas_headers("GET", "/test", b"", country=country)["X-HMAC-ACCESS-KEY"] == zc.HMAC_ACCESS_KEY
        assert zc.ZeekrIdaas(country=country, profile=FAKE_PROFILE).base == FAKE_PROFILE.idaas_gateway + "/" + FAKE_PROFILE.idaas_path
    assert eu.gateway == FAKE_PROFILE.tsp_gateway
    assert sea.gateway == zc.GATEWAY
    assert zc.ZeekrIdaas(country="NZ").base == f"{zc.IDAAS_GATEWAY}/{zc.IDAAS_PATH}"
    assert zc.idaas_headers("GET", "/test", b"", country="AU")["msgClientId"] == "1116"
    assert zc.idaas_headers("GET", "/test", b"", country="AU")["X-HMAC-ACCESS-KEY"] == zc.HMAC_ACCESS_KEY
    eu.gateway = "https://example.invalid/"
    assert eu.gateway == "https://example.invalid"
    assert sea.gateway == zc.GATEWAY
