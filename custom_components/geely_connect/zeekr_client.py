"""Zeekr-platform client for the new Geely EM backend (com.geely.global.em 1.1.0).

Standalone, additive module: does NOT touch the existing Ecarx/APAC client.
Port spec derived from static analysis + emulator extraction + LIVE capture
of the app's own forced-migration flow (2026-08-10):

  IDaaS gateway (SEA)      https://gateway-pub-hw-em-sg.zeekrlife.com
  IDaaS path               zeekr-cuc-idaas-sea   (per-region: -sea, -israel, ...)
  Identity                 X-APP-ID GEELYSEACH001M0001 / X-PROJECT-ID GEELY
                           (snc stack; derived via pj.j.e/d, verified live)
  checkUserV2              POST /auth/checkUserV2 {email, checkType:"1"}
                           -> {status, uuid, registerSite, passwordSet, ...}
  code-send                POST /captcha/email {email, operationType, ...}
                           operationType: "addPassword" (pre-switch accounts)
                           | "login" | ... -- wrong op -> 3007
  code-verify              POST /captcha/verify {account, code, codeId, operationType}
  set password             POST /auth/editPasswordByEmailEncrypt
                           {email, codeId, code, password=RSA(plain)}  (reset path)
  complete migration       POST /auth/completeMigration
                           {email, codeId, code, password=RSA(plain),
                            firstName, lastName} -> LoginTokenResponse
                           (forced set-password for old-geely accounts)
  login (password)         POST /auth/loginByEmailEncrypt
                           {email, password=RSA(plain)} -> tokenValue
  login (OTP)              POST /auth/loginByEmailEncrypt {email, codeId, code}
                           -> tokenValue   (only for login-op codes; pre-switch
                           accounts have no login-op path -- they MUST migrate)
  password RSA             RSA/ECB/PKCS1Padding, 1024-bit public key
                           ub/b.G (KtxExtendedKt.a1), base64 DEFAULT (76-col wrap)
  profile                  GET  /user/info (Authorization: tokenValue)
  tsp handoff              GET  /user/tspCode?tspClientId=<client>
                           -> {code, clientId}  (vehicle-platform auth code)

NOTE: the new platform's vehicle chain goes IDaaS token -> tspCode -> TSP
exchange; the old /ms-user-auth leg (below) is legacy machinery retained
for the old backend only.
"""
# -----------------------------------------------------------------------------
# The new Geely EM (Zeekr) platform port - the live capture of the migration
# flow, the snc / IDaaS / HF signers, the RSA password path and the two-session
# model in this file - is the reverse-engineering work of Scott Lorien
# (@scottaki), contributed as pull request #33. Merged with security fixes on
# top (response bodies are run through api.redact() at every raise, and the new
# secret keys are in api._SECRET_KEYS / diagnostics._REDACT). See NOTICE.txt.
# -----------------------------------------------------------------------------
from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import random
import ssl
import string
import time
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse, parse_qsl, quote

from .api import redact

# Public protocol identity, embedded in the shipped app APK and extractable
# by anyone - the same pattern as the legacy integration's per-region app
# secrets (const.py). Sent in cleartext request headers by design and only
# ever used for request signing.
GATEWAY = "https://sea-snc-tsp-api-gw.zeekrlife.com"
APP_ID = "GEELY-APP-NEW"
APP_SECRET = "eeec50cb855a4c69a12f297c0d27a07f"
ACCEPT = "application/json;responseformat=3"
SIG_VERSION = "2"

# Derived app identity for the snc stack (pj.j.e("geely") / pj.j.d("geely","SEA")):
# the PROD TSP gateway's registry REJECTS APP_ID ("GEELY-APP-NEW", 079002)
# but accepts these (verified live 2026-08-10). The ecarx/vehicle stack keeps
# sending APP_ID (user_app_id) exactly like the app does.
SNC_APP_ID = "GEELYSEACH001M0001"
SNC_PROJECT_ID = "GEELY"
# snc SignInterceptor HMAC-SHA256 secret for (geely, SEA, prod), decrypted
# from the whitebox si.a blob via the emulator harness (2026-08-10).
SNC_SECRET = "NZbf6kT86uOaNxQsukhQGA=="

# Headers the snc SignInterceptor includes in the canonical string.
_SNC_WHITELIST = frozenset({
    "x-app-id", "content-type", "x-api-signature-nonce", "x-timestamp",
    "x-api-signature-version", "x-project-id", "authorization",
    "accept-language", "x-vin", "x-device-id", "x-platform",
})

# ub/b.G -- 1024-bit RSA public key for IDaaS password encryption
# (KtxExtendedKt.a1: RSA/ECB/PKCS1Padding, base64 DEFAULT = 76-col wrap).
_UB_B_G = (
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQCBzg6+dwMVtGTNo8EPL+XFyz0OY0pM"
    "Mo3HdRZGauuCSgISfVMkMmOhNEb2q9UfiQcEeOwVmOgts9VF4q0BJYrRNGQaPkLybwkW"
    "sx1JmbBRcr3qq+WWhqq8xQFksfn8KeXmwgVMFX+bzup43LE0vy0yyb+SuQ9FBBGuE1d/"
    "BfHHpQIDAQAB"
)

_IDAAS_TSP_CLIENT = "1JwLroFkFFIpgFGdTRrm4_nzkkwDkfHj7RxJQb7J8tc"  # ub/b.h
# HF (old-platform) leg: the second tspCode client + the old Ecarx platform.
# Verified live 2026-08-10 from the app's own traffic (API-35 emulator capture):
#   POST api.ecloudkr.com/auth/account/session/secure?identity_type=geelynos
#   body {"area":"SEA","authCode":<tspCode2>}  -> {code:1000, data:{accessToken:
#   <old-platform JWT>, expiresIn:172800, ...}}
# Signed with the NEW app's secret (v1.0 canonical, byte-exact verified).
HF_CLIENT2 = "QSLS6WmZWjGm-DrlCaAwYNd4c6MwSvhes-itKDr-bX4"  # ub/b.f45611s
HF_GATEWAY = "https://api.ecloudkr.com"
HF_APP_ID = "GEELY-APP-NEW"
HF_SECRET = "eeec50cb855a4c69a12f297c0d27a07f"  # NativeSecretLib "GEELY_EM"
HF_ACCEPT = "application/json;responseformat=3"

# Per-install device identity for HF requests: the app derives one per
# device, so a shared static value would let the vendor correlate every HA
# install as a single device. Regenerated per process; not part of any
# signed canonical, so restarts are harmless.
_HF_DEVICE_ID = uuid.uuid4().hex


def _sign_v1(*, method: str, path: str, query: str, accept: str, nonce: str,
             sig_version: str, timestamp_ms: str, body: bytes, secret: str) -> str:
    """v1.0 canonical (bit-exact vs the app's SignUtil, verified live):
    <accept>\\n + sorted x-api-* headers + \\n + sorted query (RFC-encoded
    values) + \\n + Base64(MD5(body)) + <ts> + \\n + METHOD + \\n + path."""
    sh = {"x-api-signature-nonce": nonce, "x-api-signature-version": sig_version}
    canonical_headers = "".join(f"{k}:{sh[k]}\n" for k in sorted(sh.keys()))
    qis = sorted(parse_qsl(query, keep_blank_values=True), key=lambda kv: kv[0])
    enc_pairs = []
    for k, v in qis:
        safe = "!*'();@&=+$?#[]"
        enc = quote(v, safe=safe).replace("/", "%2F").replace(":", "%3A").replace(",", "%2C")
        enc_pairs.append(f"{k}={enc}")
    canonical_query = "&".join(enc_pairs)
    md5_b64 = base64.b64encode(hashlib.md5(body).digest()).decode()
    canon = "\n".join([accept, canonical_headers, canonical_query, md5_b64,
                       f"{timestamp_ms}", method.upper(), path])
    return base64.b64encode(
        hmac.new(secret.encode(), canon.encode(), hashlib.sha1).digest()).decode()


def _hf_request(method: str, path: str, *, query: str = "", body: bytes = b"",
                token: str | None = None, vin: str | None = None,
                vehicle_model: str = "", timezone: str = "UTC",
                device_identifier: str | None = None) -> dict:
    """Signed call to the old Ecarx platform (api.ecloudkr.com) with the HF
    identity (GEELY-APP-NEW + new-app secret + device headers), mirroring the
    new app's own HF client (HFOkHttpClientUtil.RequestInterceptor + SignUtil).
    """
    nonce = _make_nonce()
    ts_ms = str(int(time.time() * 1000))
    headers = {
        "proprietaryplatform": "0",
        "x-app-id": HF_APP_ID,
        "accept": HF_ACCEPT,
        "x-agent-type": "android",
        "x-device-type": "mobile",
        "x-operator-code": "geely",
        "x-device-identifier": device_identifier or _HF_DEVICE_ID,
        "x-env-type": "production",
        "accept-encoding": "identity",
        "x-version": "geelyNew",
        "x-timezone": timezone,
        "accept-language": "en_US",
        "x-api-signature-version": "1.0",
        "x-api-signature-nonce": nonce,
        "x-device-manufacture": "google",
        "x-device-brand": "google",
        "x-device-model": "Pixel 9 Pro XL",
        "x-device-release-date": "",
        "x-agent-version": "15",
        "content-type": "application/json; charset=UTF-8",
        "user-agent": "okhttp/4.12.0",
        "Connection": "close",
    }
    if token:
        headers["Authorization"] = token  # raw HF JWT, no Bearer prefix
    if vin:
        headers["X-Vehicle-IDENTIFIER"] = vin
    if vehicle_model:
        headers["X-VEHICLE-SERIES"] = vehicle_model
        headers["X-VEHICLE-MODEL"] = vehicle_model
    headers["x-signature"] = _sign_v1(
        method=method, path=path, query=query, accept=HF_ACCEPT, nonce=nonce,
        sig_version="1.0", timestamp_ms=ts_ms, body=body, secret=HF_SECRET)
    headers["x-timestamp"] = ts_ms

    p = urlparse(HF_GATEWAY)
    if p.scheme == "https":
        ctx = ssl.create_default_context()
        conn: http.client.HTTPConnection = http.client.HTTPSConnection(
            p.hostname or "", p.port or 443, timeout=20, context=ctx)
    else:
        conn = http.client.HTTPConnection(p.hostname or "", p.port or 80, timeout=20)
    url_path = f"{path}?{query}" if query else path
    try:
        conn.request(method, url_path, body=body, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        if resp.status != 200:
            raise ZeekrApiError(f"HF HTTP {resp.status}: {_safe_detail(raw)}")
        # Strict here: 1000 is capture-verified on this gateway, so a 200-wrapped
        # non-1000 code is a real error the adapter must see (renewal / reauth).
        return _check_resp(json.loads(raw), ok_codes=_OK_CODES)
    finally:
        conn.close()


def _rsa_encrypt_password(plain: str) -> str:
    """Mirror KtxExtendedKt.a1: RSA/ECB/PKCS1Padding + base64 (76-col wrap).

    Uses the `cryptography` package, a Home Assistant core dependency.
    """
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
        from cryptography.hazmat.primitives.asymmetric import rsa as asym_rsa
    except ImportError as exc:  # pragma: no cover - test env may lack it
        raise ZeekrAuthError(f"cryptography not available for RSA password: {exc}") from exc
    der = base64.b64decode(_UB_B_G)
    # load_der_public_key accepts both SubjectPublicKeyInfo and PKCS#1 DER,
    # so no manual fallback parse is needed (live-verified against the
    # production gateway).
    key = serialization.load_der_public_key(der)
    if not isinstance(key, asym_rsa.RSAPublicKey):
        raise ZeekrAuthError("ub/b.G is not an RSA public key")
    ct = key.encrypt(plain.encode("utf-8"), asym_padding.PKCS1v15())
    b64 = base64.b64encode(ct).decode("ascii")
    return "\n".join(b64[i:i + 76] for i in range(0, len(b64), 76))

_NONCE_HEX = "0123456789abcdef"
_NONCE_ALNUM = string.ascii_uppercase + string.digits


class ZeekrAuthError(Exception):
    """Auth-layer failure (bad credentials, expired token, rejected login)."""


class ZeekrApiError(Exception):
    """Gateway rejected the request (signature, params, or server error)."""


def _safe_detail(raw: bytes | str, limit: int = 200) -> str:
    """A response body reduced to something safe to fold into an exception.

    Every raise in this module can surface on Home Assistant's re-auth card
    and in the log (the adapter maps our errors to GeelyAuthError /
    GeelyControlError, whose text renders there), so a raw body must never
    ride along verbatim - the same redact()-at-the-raise invariant the legacy
    api.py holds. JSON is parsed and run through redact(); anything else is
    dropped rather than guessed at."""
    try:
        return str(redact(json.loads(raw)))[:limit]
    except Exception:  # noqa: BLE001 - non-JSON / undecodable body: say nothing
        return "<non-JSON body omitted>"


def _make_nonce() -> str:
    """Mirror the app's nonce shape: 3hex-12hex 7alnum 13ts (cosmetic, unique)."""
    prefix = "".join(random.choices(_NONCE_HEX, k=3))
    middle = "".join(random.choices(_NONCE_HEX, k=12))
    suffix = "".join(random.choices(_NONCE_ALNUM, k=7))
    return f"{prefix}-{middle}{suffix}{int(time.time() * 1000)}"


def _normalize_query_value(v: str) -> str:
    """Java SignUtil.getParam normalization on raw query values."""
    return v.replace("+", "%20").replace("*", "%2A").replace("%7E", "~").replace(",", "%2C")


def build_sign_string(*, method: str, path: str, query: str, accept: str,
                      nonce: str, sig_version: str, timestamp_ms: str,
                      body: bytes) -> str:
    """Canonical string for signer v2 (byte-exact vs app's SignUtil, oracle-verified)."""
    sh = {"x-api-signature-nonce": nonce, "x-api-signature-version": sig_version}
    canonical_headers = "".join(f"{k}:{sh[k]}\n" for k in sorted(sh))
    kv = []
    for pair in query.split("&"):
        if not pair:
            continue
        k, _, v = pair.partition("=")
        kv.append((k, _normalize_query_value(v)))
    canonical_query = "&".join(f"{k}={v}" for k, v in sorted(kv))
    md5_b64 = base64.b64encode(hashlib.md5(body).digest()).decode()
    return "\n".join([accept, canonical_headers, canonical_query, md5_b64,
                      timestamp_ms, method.upper(), path])


def sign_v2(*, method: str, path: str, query: str, accept: str, nonce: str,
            sig_version: str, timestamp_ms: str, body: bytes, secret: str) -> str:
    ss = build_sign_string(method=method, path=path, query=query, accept=accept,
                           nonce=nonce, sig_version=sig_version,
                           timestamp_ms=timestamp_ms, body=body)
    return base64.b64encode(
        hmac.new(secret.encode(), ss.encode(), hashlib.sha1).digest()
    ).decode()


def snc_sign(*, method: str, url: str, headers: dict, body: bytes,
             secret: str = SNC_SECRET) -> str:
    """X-SIGNATURE for the snc SignInterceptor (nj/h.java), ported verbatim
    from the decompiled app and VERIFIED live 2026-08-10: the PROD gateway
    accepted this exact construction on ms-user-auth (HTTP 200, only the
    junk identifier was rejected). Canonical:

      <sorted whitelisted headers 'lower:value\\n'>
      [<sorted query '&k=v' (values: * -> %2A, %2F -> /, %3F -> ?)>] '\\n'
      [<Base64(MD5(body)) for JSON bodies>] '\\n'
      <METHOD> '\\n'
      <path-from-after-.com>

    X-SIGNATURE = Base64(HMAC-SHA256(canonical, secret-as-ASCII-bytes)).
    """
    parsed = urlparse(url)
    hdrs = []
    for k, v in headers.items():
        lk = k.lower()
        if lk not in _SNC_WHITELIST:
            continue
        if lk in ("authorization", "x-vin") and not v:
            continue
        hdrs.append(f"{lk}:{v}\n")
    header_canon = "".join(sorted(hdrs))

    kvs = []
    if parsed.query:
        for pair in parsed.query.split("&"):
            k, _, v = pair.partition("=")
            v = v.replace("*", "%2A").replace("%2F", "/").replace("%3F", "?")
            kvs.append(f"{k}={v}")
    query_canon = "&".join(sorted(kvs))

    body_canon = ""
    ct = (headers.get("Content-Type") or "").lower()
    if body and "json" in ct:
        body_canon = base64.b64encode(hashlib.md5(body).digest()).decode()

    parts = []
    if header_canon:
        parts.append(header_canon)
    if query_canon:
        parts.append(query_canon + "\n")
    if body_canon:
        parts.append(body_canon + "\n")
    parts.append(method.upper() + "\n")
    host_end = url.find(".com")
    path = url[host_end + 4:] if host_end != -1 else parsed.path
    if "?" in path:
        path = path[: path.find("?")]
    parts.append(path)
    canon = "".join(parts)
    return base64.b64encode(
        hmac.new(secret.encode(), canon.encode(), hashlib.sha256).digest()
    ).decode()


# Success code for the HF / vehicle gateway (api.ecloudkr.com). Capture-verified
# there (the HF session-secure body is {code:1000, data:{accessToken...}}), and
# it is what the coordinator's own _SUCCESS_CODES uses. Deliberately NOT assumed
# for the IDaaS user-center or ms-user-auth legs, whose success code is not
# captured - the legacy CIDP login, for one, succeeds on 10000000, so codes
# differ per backend and guessing one would raise on a good login.
_OK_CODES: set = {"1000", 1000}


def _check_resp(resp: dict, *, ok_codes: set | None = None) -> dict:
    """BaseResult shape: {code, success, message, data}. Raise on auth/gateway errors.

    Business failures (bad/expired token, rejected params) arrive inside an HTTP
    200 envelope, so the HTTP status alone is not enough. An explicit `success`
    decides it either way. When `ok_codes` is given - only the HF/vehicle gateway,
    where code 1000 is capture-verified - a *present* code outside that set is a
    200-wrapped error too. It is omitted for the IDaaS / ms-user-auth legs, whose
    success code is unverified; there the callers detect failure by the specific
    field they expect (tokenValue, accessToken, ddcCode), so treating an
    unfamiliar code as failure could raise on a genuinely successful login.

    This matters beyond a clean error: only a raised error re-arms recovery -
    the adapter's silent HF renewal retry and, failing that, the HA reauth flow.
    A 200-wrapped `{"code": 401}` from the HF gateway slipping through as success
    would wedge the integration on a stale token until a manual reconfigure.
    """
    success = resp.get("success")
    if success in (True, "true", "1"):
        return resp
    code = resp.get("code")
    if success in (False, "false", "0") or (
        ok_codes is not None and code is not None and code not in ok_codes
    ):
        msg = resp.get("message") or resp.get("msg") or "unknown"
        raise ZeekrApiError(f"code={code} message={msg}")
    return resp


# ---------------------------------------------------------------------------
# IDaaS (user-center) layer — X-HMAC-SHA256 signer, verified byte-exact
# against two captured live vectors (2026-08-10, mitm capture of the real app)
# ---------------------------------------------------------------------------
IDAAS_GATEWAY = "https://gateway-pub-hw-em-sg.zeekrlife.com"
IDAAS_PATH = "zeekr-cuc-idaas-sea"          # per-region: -sea, -israel, ...
HMAC_SECRET = "dhn8kcmr903f39ccdd9f458f893bb6fac5e16968"   # libenv.so
HMAC_ACCESS_KEY = "673ca869165e446eb5356b8b5ae26938"       # libenv.so
CLIENT_ID = "1JwLroFkFFIpgFGdTRrm4_nzkkwDkfHj7RxJQb7J8tc"  # ub/b.java f45617y
TMP_TENANT = "3300743799505195008"
MSG_APP_ID = "11016"
MSG_CLIENT_ID = "1116"


# strftime's %A/%b are locale-dependent, so a non-English-locale HA host
# would produce a different X-DATE canonical and break the signature; the
# day/month names are fixed English tables instead.
_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
             "Saturday", "Sunday")
_MONTHS_ABBR = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _http_date_gmt() -> str:
    now = datetime.now(timezone.utc)
    return (f"{_WEEKDAYS[now.weekday()]}, {now.day:02d} "
            f"{_MONTHS_ABBR[now.month - 1]} {now.year} "
            f"{now.hour:02d}:{now.minute:02d}:{now.second:02d} GMT")


def hmac_sha256_b64(data: bytes, secret: str = HMAC_SECRET) -> str:
    return base64.b64encode(hmac.new(secret.encode(), data, hashlib.sha256).digest()).decode()


def idaas_sign(*, method: str, path: str, query: str, xdate: str,
               access_key: str = HMAC_ACCESS_KEY, secret: str = HMAC_SECRET) -> str:
    """X-HMAC-SIGNATURE: canonical METHOD\\npath\\nquery\\naccesskey\\nX-DATE\\n (trailing \\n!)."""
    canon = "\n".join([method.upper(), path, query, access_key, xdate]) + "\n"
    return hmac_sha256_b64(canon.encode(), secret)


def idaas_headers(method: str, path: str, body: bytes, query: str = "",
                  xdate: str | None = None, country: str = "AU",
                  token: str | None = None) -> dict:
    xdate = xdate or _http_date_gmt()
    headers = {
        "msgClientId": MSG_CLIENT_ID,
        "Device-Name": "Pixel 9 Pro XL",
        "msgAppId": MSG_APP_ID,
        "App-Code": CLIENT_ID,
        "RegistCountry": country,
        "appCode": "eu-app",
        "Client-Id": CLIENT_ID,
        "Call-Source": "android",
        "AppVersion": "1.1.0",
        "Brand": "GEELY",
        "Language": "en",
        "Device-Type": "app",
        "appId": "TSP",
        "Country": country,
        "appSecret": "zeekr_tis",
        "X-Language": "en",
        "app-authorization": MSG_CLIENT_ID,
        "X-HMAC-ALGORITHM": "hmac-sha256",
        "X-HMAC-SIGNATURE": idaas_sign(method=method, path=path, query=query, xdate=xdate),
        "X-HMAC-ACCESS-KEY": HMAC_ACCESS_KEY,
        "X-HMAC-DIGEST": hmac_sha256_b64(body),
        "X-DATE": xdate,
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": "okhttp/4.12.0",
    }
    if token:
        headers["Authorization"] = token  # raw tokenValue, no Bearer prefix (app parity)
    return headers


def _post_json(url: str, body: dict, headers: dict) -> dict:
    return _post_raw_json(url, json.dumps(body).encode(), headers)


def _post_raw_json(url: str, raw: bytes, headers: dict) -> dict:
    p = urlparse(url)
    host = p.hostname or ""
    ctx = ssl.create_default_context()
    conn = http.client.HTTPSConnection(host, p.port or 443, timeout=20, context=ctx)
    try:
        conn.request("POST", p.path, body=raw, headers=headers)
        resp = conn.getresponse()
        resp_raw = resp.read()
        if resp.status != 200:
            raise ZeekrApiError(f"HTTP {resp.status}: {_safe_detail(resp_raw)}")
        return json.loads(resp_raw)
    finally:
        conn.close()


def _get_json(url: str, headers: dict) -> dict:
    p = urlparse(url)
    host = p.hostname or ""
    ctx = ssl.create_default_context()
    conn = http.client.HTTPSConnection(host, p.port or 443, timeout=20, context=ctx)
    try:
        conn.request("GET", p.path + (f"?{p.query}" if p.query else ""), headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        if resp.status != 200:
            raise ZeekrApiError(f"HTTP {resp.status}: {_safe_detail(raw)}")
        return json.loads(raw)
    finally:
        conn.close()


class ZeekrIdaas:
    """IDaaS user-center client: checkUserV2 / captcha-email / loginByEmailEncrypt."""

    def __init__(self, gateway: str = IDAAS_GATEWAY, path: str = IDAAS_PATH,
                 country: str = "AU"):
        self.base = f"{gateway.rstrip('/')}/{path}"
        # The account's country. Threaded into every request's Country /
        # RegistCountry headers below - it used to be accepted and dropped, so
        # every call went out as "AU" regardless of who logged in. Only the
        # SEA-region gateway/path (this default) is live-verified; a non-SEA
        # region would also need its own gateway host and idaas path, which
        # have not been captured, so those accounts stay best-effort.
        self.country = country

    def _headers(self, method: str, path: str, body: bytes, query: str = "",
                 token: str | None = None) -> dict:
        """idaas_headers bound to this client's country (was always 'AU')."""
        return idaas_headers(method, path, body, query=query,
                             country=self.country, token=token)

    def check_user(self, email: str) -> dict:
        body = {"email": email, "checkType": "1"}
        resp = _post_json(f"{self.base}/auth/checkUserV2", body,
                          self._headers("POST", f"/{IDAAS_PATH}/auth/checkUserV2", json.dumps(body).encode()))
        return _check_resp(resp).get("data") or {}

    def request_code(self, email: str, operation_type: str = "login") -> dict:
        """captcha/email: sends the OTP email, returns SendCodeResponse (codeId)."""
        body = {"email": email, "operationType": operation_type,
                "humanMachineTicket": "", "language": "en"}
        resp = _post_json(f"{self.base}/captcha/email", body,
                          self._headers("POST", f"/{IDAAS_PATH}/captcha/email", json.dumps(body).encode()))
        return _check_resp(resp).get("data") or {}

    def login_by_email(self, email: str, code_id: str, code: str) -> str:
        """loginByEmailEncrypt -> tokenValue. password omitted for OTP-only accounts."""
        body = {"email": email, "codeId": code_id, "code": code}
        resp = _post_json(f"{self.base}/auth/loginByEmailEncrypt", body,
                          self._headers("POST", f"/{IDAAS_PATH}/auth/loginByEmailEncrypt",
                                        json.dumps(body).encode()))
        data = _check_resp(resp).get("data") or {}
        token = data.get("tokenValue")
        if not token:
            raise ZeekrAuthError(f"loginByEmailEncrypt returned no tokenValue: {str(redact(resp))[:200]}")
        return token

    def login_by_email_password(self, email: str, password: str) -> str:
        """loginByEmailEncrypt{email, RSA(password)} -> tokenValue (post-switch)."""
        body = {"email": email, "password": _rsa_encrypt_password(password)}
        resp = _post_json(f"{self.base}/auth/loginByEmailEncrypt", body,
                          self._headers("POST", f"/{IDAAS_PATH}/auth/loginByEmailEncrypt",
                                        json.dumps(body).encode()))
        data = _check_resp(resp).get("data") or {}
        token = data.get("tokenValue")
        if not token:
            raise ZeekrAuthError(f"loginByEmailEncrypt(password) returned no tokenValue: {str(redact(resp))[:200]}")
        return token

    def edit_password(self, email: str, code_id: str, code: str, password: str) -> dict:
        """editPasswordByEmailEncrypt: reset password with a reset-op OTP."""
        body = {"email": email, "codeId": code_id, "code": code,
                "password": _rsa_encrypt_password(password)}
        resp = _post_json(f"{self.base}/auth/editPasswordByEmailEncrypt", body,
                          self._headers("POST", f"/{IDAAS_PATH}/auth/editPasswordByEmailEncrypt",
                                        json.dumps(body).encode()))
        return _check_resp(resp).get("data") or {}

    def verify_code(self, account: str, code_id: str, code: str,
                    operation_type: str = "addPassword") -> bool:
        """captcha/verify: validates the OTP before completing the operation."""
        body = {"account": account, "code": code, "codeId": code_id,
                "operationType": operation_type}
        resp = _post_json(f"{self.base}/captcha/verify", body,
                          self._headers("POST", f"/{IDAAS_PATH}/captcha/verify",
                                        json.dumps(body).encode()))
        data = _check_resp(resp).get("data")
        return data is True

    def complete_migration(self, email: str, code_id: str, code: str, password: str,
                           first_name: str, last_name: str) -> dict:
        """completeMigration: set password for old-geely accounts, returns
        LoginTokenResponse {tokenValue, uuid, ...} (login included)."""
        body = {"email": email, "codeId": code_id, "code": code,
                "password": _rsa_encrypt_password(password),
                "firstName": first_name, "lastName": last_name}
        resp = _post_json(f"{self.base}/auth/completeMigration", body,
                          self._headers("POST", f"/{IDAAS_PATH}/auth/completeMigration",
                                        json.dumps(body).encode()))
        data = _check_resp(resp).get("data") or {}
        if not data.get("tokenValue"):
            raise ZeekrAuthError(f"completeMigration returned no tokenValue: {str(redact(resp))[:200]}")
        return data

    def user_info(self, token_value: str) -> dict:
        """POST /user/info (empty body) with the login token: confirms identity."""
        resp = _post_raw_json(
            f"{self.base}/user/info", b"",
            self._headers("POST", f"/{IDAAS_PATH}/user/info", b"",
                          token=token_value))
        return _check_resp(resp).get("data") or {}

    def tsp_code(self, token_value: str, client_id: str = _IDAAS_TSP_CLIENT) -> dict:
        """user/tspCode: exchange the IDaaS login token for a TSP auth code.

        GET with query signed raw (canonical verified against the app's own
        live capture 2026-08-10: METHOD\\npath\\nquery\\naccesskey\\nX-DATE\\n).
        """
        query = f"tspClientId={client_id}"
        resp = _get_json(
            f"{self.base}/user/tspCode?{query}",
            self._headers("GET", f"/{IDAAS_PATH}/user/tspCode", b"", query=query,
                          token=token_value))
        return _check_resp(resp).get("data") or {}


class ZeekrClient:
    """Minimal new-platform client: login + vehicle status. Thread-safe per call."""

    def __init__(self, email: str, password: str, gateway: str = GATEWAY,
                 access_token: str | None = None,
                 refresh_token: str | None = None,
                 user_id: str | None = None,
                 vehicle_model: str = ""):
        self.email = email
        self.password = password
        self.gateway = gateway.rstrip("/")
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.user_id = user_id
        self.vehicle_model = vehicle_model
        self.hf_token: str | None = None
        self.hf_expires_in: int = 172800
        self.country_code: str = "AU"
        # HF-leg identity: IANA timezone (the adapter sets it from the HA
        # config; "UTC" is the neutral default) and one random device
        # identifier per client instance.
        self.timezone: str = "UTC"
        self.hf_device_id = uuid.uuid4().hex

    # ---- transport ----------------------------------------------------------

    def _request(self, method: str, path: str, *, body: bytes = b"",
                 signed: bool = True, urlname: str | None = None,
                 query: str = "", extra: dict | None = None,
                 signer: str = "v2") -> dict:
        p = urlparse(self.gateway)
        port = p.port or (443 if p.scheme == "https" else 80)
        nonce = _make_nonce()
        ts_ms = str(int(time.time() * 1000))
        headers = {
            "X-APP-ID": APP_ID,
            "Accept": ACCEPT,
            "X-AGENT-TYPE": "android",
            "X-OPERATOR-CODE": "geely",   # parity with old platform; harmless if unused
            "Connection": "close",
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "okhttp/4.11.0",
        }
        if urlname:
            headers["urlname"] = urlname
        if extra:
            headers.update(extra)
        # Authorization rides INSIDE the snc canonical (the app's nj/a
        # interceptor adds it before nj/h signs), so attach it before
        # computing X-SIGNATURE. Signer v2's canonical never includes it.
        if self.access_token:
            headers["Authorization"] = self.access_token
        if signer == "snc":
            # snc stack (ms-user-auth): derived app identity + the header set
            # the app's own SignInterceptor signs (nj/a.java + nj/h.java).
            headers.update({
                "X-APP-ID": SNC_APP_ID,
                "X-PROJECT-ID": SNC_PROJECT_ID,
                "AppId": "ONEX97FB91F061405",
                "X-API-SIGNATURE-VERSION": "2.0",
                "X-API-SIGNATURE-NONCE": nonce,
                "X-TIMESTAMP": ts_ms,
                "X-DEVICE-ID": str(uuid.uuid4()),
                "X-PLATFORM": "APP",
                "X-APP-OS-VERSION": "17",
                "ACCEPT-LANGUAGE": "en-AU",
                "Content-Type": "application/json; charset=UTF-8",
            })
            full_path = f"{path}?{query}" if query else path
            headers["X-SIGNATURE"] = snc_sign(
                method=method, url=f"{self.gateway}{full_path}",
                headers=headers, body=body)
        elif signed:
            headers["X-api-signature-version"] = SIG_VERSION
            headers["X-api-signature-nonce"] = nonce
            headers["X-TIMESTAMP"] = ts_ms
            headers["X-SIGNATURE"] = sign_v2(
                method=method, path=path, query=query, accept=ACCEPT, nonce=nonce,
                sig_version=SIG_VERSION, timestamp_ms=ts_ms, body=body, secret=APP_SECRET)
        if query:
            path = f"{path}?{query}"

        host = p.hostname or ""
        if p.scheme == "https":
            ctx = ssl.create_default_context()  # strict public-CA validation
            conn = http.client.HTTPSConnection(host, port, timeout=20, context=ctx)
        else:
            conn = http.client.HTTPConnection(host, port, timeout=20)
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            if resp.status != 200:
                raise ZeekrApiError(f"HTTP {resp.status}: {_safe_detail(raw)}")
            return _check_resp(json.loads(raw))
        finally:
            conn.close()

    # ---- auth ---------------------------------------------------------------

    def _ms_user_auth_login(self, identifier: str) -> None:
        """The unsigned ms-user-auth leg: identifier (DDC code or the IDaaS
        tokenValue) -> accessToken/refreshToken/userId."""
        login_body = json.dumps({
            "identifier": identifier,
            "identityType": 10,
            "loginDeviceId": str(uuid.uuid4()),
            "loginDeviceJgId": str(uuid.uuid4()),
            "loginDeviceType": 1,
            "loginPhoneBrand": "Google",
            "loginPhoneModel": "Pixel 9 Pro XL",
            "loginSystem": "Android 17",
        }).encode()
        auth = self._request("POST", "/ms-user-auth/v1.0/auth/login",
                             body=login_body, signed=False, signer="snc")
        data = auth.get("data") or {}
        self.access_token = data.get("accessToken")
        self.refresh_token = data.get("refreshToken")
        self.user_id = data.get("userId")
        if not self.access_token:
            raise ZeekrAuthError(f"login returned no accessToken: {str(redact(auth))[:200]}")

    def login(self) -> None:
        """email+password -> DDC (signed) -> ms-user-auth (unsigned) -> tokens."""
        ddc_body = json.dumps({"email": self.email, "password": self.password}).encode()
        ddc = self._request("POST", "/user-service/device/code", body=ddc_body,
                            urlname="user-api")
        ddc_code = (ddc.get("data") or {}).get("ddcCode")
        if not ddc_code:
            raise ZeekrAuthError(f"device/code returned no ddcCode: {str(redact(ddc))[:200]}")
        self._ms_user_auth_login(ddc_code)

    def login_otp(self, token_value: str) -> None:
        """IDaaS OTP path: the user-center tokenValue as the ms-user-auth
        identifier (identityType 10), same unsigned leg."""
        self._ms_user_auth_login(token_value)

    def login_tsp(self, token_value: str) -> None:
        """New-platform chain (app-verified 2026-08-10): tokenValue ->
        user/tspCode?tspClientId=... -> ms-user-auth{identifier: tspCode,
        identityType: 10} -> {accessToken, refreshToken, userId}, PLUS the
        HF leg: the client-2 tspCode exchanged at the OLD platform
        (api.ecloudkr.com/auth/account/session/secure?identity_type=geelynos)
        -> old-platform JWT (self.hf_token). Vehicle data rides the HF JWT."""
        idaas = ZeekrIdaas(country=self.country_code)
        tsp = idaas.tsp_code(token_value)
        code = tsp.get("code")
        if not code:
            raise ZeekrAuthError(f"tspCode returned no code: {str(redact(tsp))[:200]}")
        self._ms_user_auth_login(code)
        self.login_hf(token_value)

    def login_hf(self, token_value: str) -> None:
        """HF leg only (the app's silent JWT renewal): client-2 tspCode ->
        old-platform session/secure -> self.hf_token (expiresIn ~2 days)."""
        idaas = ZeekrIdaas(country=self.country_code)
        tsp2 = idaas.tsp_code(token_value, client_id=HF_CLIENT2)
        code2 = tsp2.get("code")
        if not code2:
            raise ZeekrAuthError(f"tspCode(client2) returned no code: {str(redact(tsp2))[:200]}")
        body = json.dumps({"area": "SEA", "authCode": code2},
                          separators=(",", ":")).encode()
        resp = _hf_request("POST", "/auth/account/session/secure",
                           query="identity_type=geelynos", body=body,
                           timezone=self.timezone,
                           device_identifier=self.hf_device_id)
        data = resp.get("data") or {}
        hf_token = data.get("accessToken")
        if not hf_token:
            raise ZeekrAuthError(f"HF exchange returned no accessToken: {str(redact(resp))[:200]}")
        self.hf_token = hf_token
        self.hf_expires_in = data.get("expiresIn", 172800)

    # ---- vehicle (old-platform endpoints, HF JWT) ---------------------------
    # The new app's vehicle data rides the OLD platform (api.ecloudkr.com)
    # with the HF JWT (capture-verified 2026-08-10): status, capability and
    # vehicle-list all hit ecloudkr with the raw HF JWT + vehicle headers.

    def get_vehicle_status(self, vin: str) -> dict:
        if not self.hf_token:
            raise ZeekrAuthError("not logged in (no HF session)")
        resp = _hf_request(
            "GET", f"/remote-control/vehicle/status/{vin}",
            query=f"userId={self.user_id or ''}",
            token=self.hf_token, vin=vin, vehicle_model=self.vehicle_model,
            timezone=self.timezone, device_identifier=self.hf_device_id)
        return resp.get("data") or {}

    def vehicle_status_resp(self, vin: str, user_id: str | None = None) -> dict:
        """Full BaseResult envelope for the old-platform status endpoint
        (remote-control/vehicle/status/{vin}?userId=...) with the HF JWT.
        Same endpoint family the legacy integration uses, so the existing
        coordinator parsing applies unchanged."""
        if not self.hf_token:
            raise ZeekrAuthError("not logged in (no HF session)")
        return _hf_request(
            "GET", f"/remote-control/vehicle/status/{vin}",
            query=f"userId={user_id or self.user_id or ''}",
            token=self.hf_token, vin=vin, vehicle_model=self.vehicle_model,
            timezone=self.timezone, device_identifier=self.hf_device_id)

    def vehicle_status(self, vin: str, user_id: str | None = None) -> dict:
        """The data block only (legacy-style convenience for callers that
        already stripped the envelope)."""
        return self.vehicle_status_resp(vin, user_id).get("data") or {}

    def refresh_session(self) -> bool:
        """PUT /auth/account/session/secure with the refresh token (Ecarx
        refreshTspToken family). Returns True on success (new accessToken set)."""
        if not self.refresh_token:
            return False
        body = json.dumps({"refreshToken": self.refresh_token,
                           "morecloud": "0"}).encode()
        try:
            resp = self._request("PUT", "/auth/account/session/secure",
                                 body=body, urlname="user-api")
        except ZeekrApiError:
            return False
        data = resp.get("data") or {}
        new_token = data.get("accessToken")
        if new_token:
            self.access_token = new_token
            return True
        return False

    def control(self, vin: str, body: dict) -> dict:
        """PUT /remote-control/vehicle/telematics/{vin} — same RemoteControlRequest
        family as the legacy client (climate, locks, windows...)."""
        return self.control_resp(vin, body).get("data") or {}

    def control_resp(self, vin: str, body: dict) -> dict:
        """Full response for a telematics control call (HF JWT, ecloudkr)."""
        if not self.hf_token:
            raise ZeekrAuthError("not logged in (no HF session)")
        return _hf_request(
            "PUT", f"/remote-control/vehicle/telematics/{vin}",
            body=json.dumps(body).encode(),
            token=self.hf_token, vin=vin, vehicle_model=self.vehicle_model,
            timezone=self.timezone, device_identifier=self.hf_device_id)

    def fetch_capabilities(self, vin: str) -> list[dict]:
        """GET /geelyTCAccess/tcservices/capability/{vin} (the app's own
        query: pageSize=2000&pageIndex=1&vehicleType=0&sortField=)."""
        if not self.hf_token:
            raise ZeekrAuthError("not logged in (no HF session)")
        resp = _hf_request(
            "GET", f"/geelyTCAccess/tcservices/capability/{vin}",
            query="pageSize=2000&pageIndex=1&vehicleType=0&sortField=",
            token=self.hf_token, vin=vin, vehicle_model=self.vehicle_model,
            timezone=self.timezone, device_identifier=self.hf_device_id)
        data = resp.get("data")
        if isinstance(data, list):
            return [v for v in data if isinstance(v, dict)]
        return []

    def list_vehicles(self, user_id: str) -> list[dict]:
        """GET /device-platform/api/v4.0/veh/vehicle-list?needSharedCar=true
        on the OLD platform with the HF JWT — the list the new app actually
        shows (capture-verified; response data.list of {vin, nickName,
        appModelCode, ...}). Returns a list of vehicle-record dicts."""
        if not self.hf_token:
            raise ZeekrAuthError("not logged in (no HF session)")
        resp = _hf_request(
            "GET", "/device-platform/api/v4.0/veh/vehicle-list",
            query="needSharedCar=true",
            token=self.hf_token, vehicle_model=self.vehicle_model,
            timezone=self.timezone, device_identifier=self.hf_device_id)
        data = resp.get("data")
        if isinstance(data, list):
            return [v for v in data if isinstance(v, dict)]
        if isinstance(data, dict):
            for key in ("list", "records", "vehicles", "userVehicleList",
                        "vehicleList"):
                val = data.get(key)
                if isinstance(val, list):
                    return [v for v in val if isinstance(v, dict)]
            if vehicle_vin(data):
                return [data]
        return []


def vehicle_vin(v: dict) -> str | None:
    """Best-effort VIN from a vehicle-list record. The new platform's record
    shape is not yet verified live, so several spellings are accepted."""
    if not isinstance(v, dict):
        return None
    for k in ("vin", "VIN", "vehicleVin", "vinCode", "tboxVin"):
        val = v.get(k)
        if isinstance(val, str) and val:
            return val
    for k in ("vehicleInfo", "vehicle", "carInfo"):
        nested = v.get(k)
        if isinstance(nested, dict):
            found = vehicle_vin(nested)
            if found:
                return found
    return None


def vehicle_nickname(v: dict) -> str:
    """Best-effort display name from a vehicle-list record (empty when the
    record carries none)."""
    if not isinstance(v, dict):
        return ""
    for k in ("nickname", "carNickName", "vehicleName", "name",
              "vehicleNickname", "nickName"):
        val = v.get(k)
        if isinstance(val, str) and val:
            return val
    for k in ("vehicleInfo", "vehicle", "carInfo"):
        nested = v.get(k)
        if isinstance(nested, dict):
            got = vehicle_nickname(nested)
            if got:
                return got
    return ""


