"""Self-contained Geely TSP / mTLS API client.

Bundles the proven HMAC-SHA1 7-field signer + raw-socket mTLS helper from
poc/geely_mtls.py so this integration has no external poc/ dependency at
runtime.

All public methods are sync - HA wraps them with async_add_executor_job.
"""
# -----------------------------------------------------------------------------
# Portions of this file - the reverse-engineered Geely protocol / field mappings
# (the parts that required protocol research) - are derived from
# nitaybz/geely-global-ha, used under the MIT License. See NOTICE.txt.
# Original framework, security hardening and transport are our own work.
# -----------------------------------------------------------------------------
from __future__ import annotations

import base64
import collections
import hashlib
import hmac
import json
import logging
import os
import random
import re
import secrets
import socket
import ssl
import string
import threading
import time
import uuid
from typing import Any
from urllib.parse import parse_qsl, quote, urlparse

_LOGGER = logging.getLogger(__name__)


class GeelyAuthError(Exception):
    """Raised when the Geely server rejects our credentials.

    Caused by: cidpsso token revoked (e.g. iPhone re-login kicked us out,
    or token aged out), or apis.ecloudeu JWT permanently invalid. The
    coordinator should catch this and surface a re-auth flow."""


class GeelyRegionError(Exception):
    """Raised when the account is not served by the EU/International cloud.

    Geely runs separate backends per region (EU, APAC, NA, SA), each with its
    own app credentials, and the region is a property of the account rather
    than of the country picked at setup. An account registered elsewhere gets
    code 1501 'geelyos verify error' from the EU cert endpoint - the login and
    the OTP succeed first, which is what makes it look like a bug rather than
    an unsupported region."""


class GeelyCaptchaUnreachableError(Exception):
    """Raised when the captcha host cannot be reached at the network level.

    The captcha retries exist for the solver's ~85% accuracy, not for the
    network: an unreachable host will not become reachable 45 seconds from
    now, and each dead attempt burns the full connect timeout per resolved
    address. Common causes are DNS filtering (Pi-hole/AdGuard blocklists)
    and router or firewall geo-blocking - captcha4.geely.com is hosted in
    mainland China. The config flow maps this onto a message that names the
    host so the user has something to act on."""


class GeelyControlError(Exception):
    """Raised when the Geely server rejects a control command.

    Common causes:
      - code "8070" 'The last request has not yet been executed'
        (rate-limit; previous command still pending)
      - code "failure" 'Operation failed' (wrong serviceId/params for
        this trim)
      - code 1404/1405 (feature unavailable / vehicle in wrong state)
    Entities should catch this and re-raise as HomeAssistantError so
    the user sees a toast notification.
    """

    def __init__(self, code: Any, message: str | None) -> None:
        self.code = code
        self.message = message or f"Geely server returned code={code!r}"
        super().__init__(self.message)


# Error codes the Geely gateway returns when our session is invalid.
# Distilled from observed responses + poc/geely_client.py.
_AUTH_FAILURE_CODES: set = {
    # cidpsso/cidpcar token rejected
    60000000, 60000001, 60000110,
    "60000000", "60000001", "60000110",
    # apis.ecloudeu JWT invalid / expired beyond auto-refresh
    1402, "1402",
}

# Codes we treat as "command accepted by the server".
_CONTROL_SUCCESS_CODES: set = {1000, "1000"}

# Cert-provisioning codes that mean "wrong regional backend for this account",
# not "something went wrong". 1501 is the EU server declining to verify a
# GeelyOS token issued by another region; 1445 is the signature check failing,
# which is what a different region's app credentials produce.
_REGION_MISMATCH_CODES: set = {"1501", "1445"}


# Keys whose values are secrets and must never reach logs or exception text.
# Values compared after normalizing the key with lower() + stripping "-"/"_",
# so every entry here must be in that separator-free form.
_SECRET_KEYS: set = {
    "token", "accesstoken", "accesscode", "authcode", "refreshtoken",
    "cert", "csr", "privatekey", "password",
    "passtoken", "captchaoutput", "sessionid",
    "cidpssotoken", "jwt", "secret", "appsecret",
    # The scheduled-charging body sends the VIN in "pin", but the field is a
    # PIN by contract and a future firmware could put a real one there.
    "pin",
}

# "key" is ambiguous: it holds PEM material in provisioning responses, but the
# control API uses {"key": "operation", "value": "1"} name/value pairs, where
# the value is a parameter *name* and masking it makes a debug log useless.
# Decide from the value, not the key.
_AMBIGUOUS_KEYS: set = {"key"}

# Not secrets, but they identify a specific person or car, and a debug log is
# the single most-shared artefact when someone opens an issue. Kept partially
# visible - the last four characters are enough to tell two vehicles apart in
# a log without publishing the identifier. Mirrors diagnostics.py, which
# already masks these; the two lists must not drift apart.
_IDENTIFYING_KEYS: set = {
    "vin", "userid", "deviceid", "devicehardwareidfa", "devicehardwareidfv",
    "deviceidfa", "deviceidfv", "email", "mobile", "phone", "username",
}


def _no_crlf(value: str, what: str) -> str:
    """Guard against HTTP request-line / header injection. The VIN and user_id
    originate from the Geely backend JSON and are interpolated into a hand-built
    raw HTTP request; a CR/LF there would allow header injection / request
    smuggling. Reject rather than sanitize."""
    if "\r" in value or "\n" in value:
        raise ValueError(f"illegal CR/LF in {what}")
    return value


def _mask_tail(value: Any) -> Any:
    """Show only the last four characters of an identifier."""
    text = str(value)
    return f"...{text[-4:]}" if len(text) > 4 else "***"


# A VIN is a path segment in both the storage tree
# (.storage/geely_connect/<VIN>/key.pem) and every control URL
# (/remote-control/vehicle/telematics/<VIN>). Those strings end up in exception
# text, and GeelyAuthError text is shown on Home Assistant's re-authentication
# card as well as written to the log - so redacting response bodies is not
# enough on its own. Config_flow accepts [A-Za-z0-9]{8,20} for a VIN; requiring
# 11 here keeps real path components ("geely_connect", "server_pins.json",
# "telematics", ".storage") intact, since they all carry a separator or are
# shorter.
_VIN_LIKE_SEGMENT = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9]{11,20}(?![A-Za-z0-9])")


def mask_path(value: Any) -> str:
    """Replace VIN-shaped segments of a path or URL with their last 4 chars."""
    return _VIN_LIKE_SEGMENT.sub(lambda m: f"...{m.group(0)[-4:]}", str(value))


def _looks_like_key_material(value: Any) -> bool:
    """True if a "key" field holds a credential rather than a parameter name.

    Control params carry short identifiers like "operation" or "rcs.restart";
    PEM blocks and base64 key blobs are long and have tell-tale markers.
    """
    if not isinstance(value, str):
        return True                       # dict/list under "key" - do not risk it
    return "-----BEGIN" in value or len(value) > 40


def _norm_key(name: Any) -> str | None:
    """The spelling-insensitive form the key lists are written in."""
    if not isinstance(name, str):
        return None
    return name.lower().replace("-", "").replace("_", "")


def redact(obj: Any):
    """Return a copy of a server response / dict with secret values masked.

    Used anywhere a raw response is folded into a log line or exception
    message. Prevents tokens, JWTs, and certificate material from being
    written to Home Assistant's log file, which is often shared when a
    user asks for help. Identifiers in _IDENTIFYING_KEYS are reduced to their
    last four characters rather than removed, so a log stays readable.
    """
    if isinstance(obj, dict):
        # `{"key": <name>, "value": <data>}` - the serviceParameters shape, and
        # the one fire_control prints and the command trail stores. The field
        # name is a *value* here, so matching on key names never sees it and a
        # secret sails straight through. This is the same class of miss that let
        # the VIN out through the scheduled-charging `pin` field once already.
        pair_name = obj.get("key")
        pair_norm = (_norm_key(pair_name)
                     if "value" in obj and isinstance(pair_name, str)
                     and len(pair_name) <= 40 else None)
        out = {}
        for k, v in obj.items():
            norm = _norm_key(k)
            if k == "value" and pair_norm is not None:
                norm = pair_norm      # judge the payload by the name beside it
            if norm in _SECRET_KEYS or (
                norm in _AMBIGUOUS_KEYS and _looks_like_key_material(v)
            ):
                out[k] = "***redacted***"
            elif norm in _IDENTIFYING_KEYS and not isinstance(v, (dict, list, tuple)):
                out[k] = _mask_tail(v)
            else:
                out[k] = redact(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact(v) for v in obj]
    return obj


def _is_auth_failure(resp: dict) -> bool:
    return resp.get("code") in _AUTH_FAILURE_CODES


def _check_control_resp(resp: dict) -> dict:
    """Raise GeelyControlError if the response is not a success.

    Note: a successful response (`code=1000, success=True`) means the
    GATEWAY accepted the command - not that the car physically executed
    it. Status-field diff is the only way to verify execution. But this
    check at least catches obvious failures (wrong params, rate limit,
    unsupported feature).
    """
    if _is_auth_failure(resp):
        raise GeelyAuthError(f"control rejected: {resp.get('code')}")
    code = resp.get("code")
    if code in _CONTROL_SUCCESS_CODES and resp.get("success") in (True, "true"):
        return resp
    raise GeelyControlError(code, resp.get("message"))


# ---------- HMAC signer (proven bit-exact against iOS framework) ----------

def _percent_encode_value(s: str) -> str:
    enc = quote(s, safe="!*'();@&=+$?#[]")
    return enc.replace("/", "%2F").replace(":", "%3A").replace(",", "%2C")


def _build_sign_string(*, method, path, query, accept, nonce, sig_version,
                       timestamp_ms, body) -> str:
    accept = accept or "application/json;responseformat=3"
    sh = {
        "x-api-signature-nonce": nonce,
        "x-api-signature-version": sig_version,
    }
    canonical_headers = "".join(f"{k}:{sh[k]}\n" for k in sorted(sh.keys()))
    qis = sorted(parse_qsl(query, keep_blank_values=True), key=lambda kv: kv[0])
    canonical_query = ""
    for k, v in qis:
        canonical_query += f"{k}={_percent_encode_value(v)}&"
    if len(canonical_query) >= 2:
        canonical_query = canonical_query[:-1]
    md5_b64 = base64.b64encode(hashlib.md5(body).digest()).decode()
    return "\n".join([accept, canonical_headers, canonical_query, md5_b64,
                      f"{timestamp_ms}", method.upper(), path])


_NONCE_HEX = "0123456789abcdef"
_NONCE_ALNUM = string.ascii_uppercase + string.digits


def _make_nonce() -> str:
    """Mimic Android's nonce format: 3hex-12hex 7alnum 13ts.

    Cosmetic only - the nonce just has to look like the app's and be unique
    per request; it carries no security weight, which is why `random` rather
    than `secrets` is fine here."""
    prefix = "".join(random.choices(_NONCE_HEX, k=3))
    middle = "".join(random.choices(_NONCE_HEX, k=12))
    suffix = "".join(random.choices(_NONCE_ALNUM, k=7))
    return f"{prefix}-{middle}{suffix}{int(time.time() * 1000)}"


# ---------- Raw-socket mTLS sender ----------

def _parse_chunked(body: bytes) -> bytes:
    out = b''
    pos = 0
    while pos < len(body):
        end = body.find(b'\r\n', pos)
        if end < 0:
            break
        try:
            sz = int(body[pos:end], 16)
        except ValueError:
            break
        if sz == 0:
            break
        pos = end + 2
        out += body[pos:pos + sz]
        pos += sz + 2
    return out


class GeelyTLSPinError(Exception):
    """Raised when a server's public key does not match the stored pin.

    A mismatch means the certificate the server presented is NOT the one we
    recorded on first contact - i.e. a possible man-in-the-middle. We fail
    the connection closed rather than sending any credentials."""


# ---------------------------------------------------------------------------
# Secure transport
# ---------------------------------------------------------------------------
# The upstream project connected with `check_hostname = False` +
# `verify_mode = CERT_NONE`, i.e. it trusted ANY certificate any server
# presented, on EVERY request - including the ones carrying the cidpsso
# token, the rotating JWT, and the mTLS client key. That makes a
# man-in-the-middle (rogue Wi-Fi, DNS spoof, compromised router) able to
# impersonate `apis.ecloudeu.com`, capture those credentials and then
# lock/unlock or climate-control the car.
#
# We replace that with a two-tier, always-fail-closed strategy:
#
#   1. Try a STRICT connection: full chain + hostname validation against
#      the OS trust store (public CAs). If Geely uses public certificates
#      this Just Works with zero config and is the strongest option.
#
#   2. If (and only if) strict validation fails, fall back to PUBLIC-KEY
#      PINNING - but only for the one host that provably cannot chain to a
#      public root, and only against a pin we already know. The pinned key
#      ships with the integration (_BUNDLED_TLS_PINS), so even the very
#      first connection is checked instead of trusting whatever answers.
#
# Two rules keep the fallback from becoming a downgrade attack, which is the
# mistake an audit found in the first version of this file: a host must be
# on the private-PKI allowlist to use it at all, and a host that has ever
# validated strictly is remembered and can never use it afterwards. Without
# those, an attacker could present any self-signed certificate on any
# connection, force the fallback, and be trusted on first use.

_TLS_LEGACY_RENEG = 0x4   # OP_LEGACY_SERVER_CONNECT - old handshake mode only

# Hosts that legitimately cannot validate against a public CA. Only
# apis.ecloudeu.com qualifies: it serves a leaf issued by Geely's own PKI
# ("Geely Trust Center / External Services Issuing EU-CA", valid 2022-2032)
# and sends no intermediate. Every other Geely host answers with a normal
# GlobalSign or Amazon chain and so must validate strictly.
_PRIVATE_PKI_HOSTS: frozenset = frozenset({
    "apis.ecloudeu.com",
    "apis.ecloudus.com",
    "apis.ecloudkr.com",
})

# SubjectPublicKeyInfo SHA-256 pins (base64) for those hosts. Captured
# 2026-07-30 and cross-checked against all three published A records and two
# independent DoH resolvers, so a single poisoned network path could not have
# produced them. Because a pin is present here, the host below never uses
# trust-on-first-use - it is verified from the first connection onwards.
_BUNDLED_TLS_PINS: dict[str, tuple[str, ...]] = {
    "apis.ecloudeu.com": ("Hm0olBoClunXgMp4wFvdrr8SC5iSt+LX6iyB4N828C8=",),
    "apis.ecloudus.com": ("yTrWM8YjYq6ivb6HP1usQiBdgZYGiCwR6yuQYOm2KVs=",),
    # Captured 2026-08-03 from all three published A records
    # (3.34.148.140, 3.36.84.152, 3.39.68.203) - same SPKI on all, cross-checked
    # against the strict-failure chain (Geely Trust Center / EU-CA leaf,
    # subject CN=apis.ecloudkr.com, valid 2022-2032).
    "apis.ecloudkr.com": ("XFUV7VhyECWtALG5wEQoAj3FSWtsf3IpBq6l2mBgVWA=",),
}

# OpenSSL verify codes that mean "the chain is fine, we just don't have this
# CA": 18 self-signed leaf, 19 self-signed root in chain, 20 issuer not found
# locally (what apis.ecloudeu.com actually returns, since it sends no
# intermediate). An expired certificate or a hostname mismatch is a bad
# certificate no matter who issued it, so those never reach the fallback.
_PRIVATE_CA_VERIFY_CODES: frozenset = frozenset({18, 19, 20})


def _strict_ctx() -> ssl.SSLContext:
    """Verifying TLS context: OS trust store + certifi, hostname checked."""
    ctx = ssl.create_default_context()
    try:
        import certifi
        ctx.load_verify_locations(cafile=certifi.where())
    except Exception:  # noqa: BLE001 - certifi optional; OS store still loaded
        pass
    ctx.options |= _TLS_LEGACY_RENEG
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


def _pinning_ctx() -> ssl.SSLContext:
    """Context used ONLY for the pinning fallback. Verification is done
    manually against the stored pin after the handshake, so the context
    itself does not chain-validate - but we never trust the result unless
    the pin matches."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.options |= _TLS_LEGACY_RENEG
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _spki_sha256_b64(der_cert: bytes) -> str:
    """SHA-256 of the certificate's SubjectPublicKeyInfo, base64. This is a
    key pin, not a cert pin, so it survives routine certificate renewals
    that keep the same key pair."""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    cert = x509.load_der_x509_certificate(der_cert)
    spki = cert.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return base64.b64encode(hashlib.sha256(spki).digest()).decode()


def _close_quietly(sock: socket.socket) -> None:
    try:
        sock.close()
    except OSError:
        pass


def _load_pin_store(pin_path: str | None) -> dict[str, dict]:
    """Read server_pins.json as {host: {"pins": [...], "strict": bool}}.

    Fails closed on purpose: a file that exists but cannot be read or parsed
    raises instead of returning an empty store, because an empty store is
    what re-opens trust-on-first-use for that host.

    Pin files written by 0.9.x stored a bare list per host; those are migrated
    in memory and rewritten on the next successful connection."""
    if not pin_path or not os.path.exists(pin_path):
        return {}
    try:
        with open(pin_path, "r") as fh:
            raw = json.load(fh)
    except (OSError, ValueError) as e:
        raise GeelyTLSPinError(
            f"{mask_path(pin_path)} exists but could not be read ({e}). Refusing to "
            "continue without the pins it should contain - fix or delete it."
        ) from e
    if not isinstance(raw, dict):
        raise GeelyTLSPinError(f"{mask_path(pin_path)} is not a JSON object; refusing to use it")

    store: dict[str, dict] = {}
    for host, entry in raw.items():
        if isinstance(entry, list):
            pins, strict = entry, False
        elif isinstance(entry, dict):
            pins, strict = entry.get("pins") or [], bool(entry.get("strict"))
        else:
            raise GeelyTLSPinError(f"{mask_path(pin_path)}: malformed entry for {host}")
        store[host] = {
            "pins": [p for p in pins if isinstance(p, str)],
            "strict": strict,
        }
    return store


def _save_pin_store(pin_path: str, store: dict) -> None:
    """Write the pin store atomically, so an interrupted write cannot leave a
    truncated file that _load_pin_store would then refuse (or, worse, that an
    older parser would read as 'no pins known')."""
    os.makedirs(os.path.dirname(pin_path), mode=0o700, exist_ok=True)
    tmp_path = f"{pin_path}.tmp"
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(store, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, pin_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _secure_tls_connect(host: str, port: int, *, pin_path: str | None,
                        client_cert: str | None = None,
                        client_key: str | None = None,
                        timeout: int = 30) -> ssl.SSLSocket:
    """Open a TLS connection to (host, port), fail-closed.

    Strict public-CA validation first. Only a *verification* failure, on a
    host that is both on the private-PKI allowlist and has never validated
    strictly before, may fall back to public-key pinning; anything else
    propagates and no data is sent.

    One residual exposure in the fallback: the mTLS client certificate is
    offered during the handshake, so a pin mismatch is detected only after
    an impostor has seen that certificate. It is a public certificate - the
    private key never leaves the host and the handshake signature is not
    replayable - so this leaks the vehicle's certificate to an attacker who
    is already on the path, not the ability to use it."""
    store = _load_pin_store(pin_path)
    entry = store.get(host) or {}
    accepted = set(_BUNDLED_TLS_PINS.get(host, ())) | set(entry.get("pins") or ())

    # --- Tier 1: strict public-CA validation ---
    strict = _strict_ctx()
    if client_cert:
        strict.load_cert_chain(client_cert, client_key)
    raw = socket.create_connection((host, port), timeout=timeout)
    try:
        ssock = strict.wrap_socket(raw, server_hostname=host)
    except ssl.SSLCertVerificationError as e:
        _close_quietly(raw)
        strict_error = e
    except Exception:
        _close_quietly(raw)
        raise
    else:
        # A pin is a requirement, not a fallback. Chain validation succeeding
        # is not enough for a host we ship a key for: an interception proxy
        # whose root is in the OS trust store - a corporate middlebox, an
        # antivirus TLS scanner, a mis-issuing CA - produces a chain that
        # validates here, and checking the pin only on the failure path would
        # let exactly the attacker pinning exists to stop walk straight
        # through. So verify it on this path too whenever we have one.
        if accepted:
            der = ssock.getpeercert(binary_form=True)
            spki = _spki_sha256_b64(der) if der else None
            if spki not in accepted:
                ssock.close()
                raise GeelyTLSPinError(
                    f"{host} presented a publicly-trusted certificate whose key "
                    f"{spki} is not one we expect {sorted(accepted)}. A valid "
                    "chain is not sufficient for this host - refusing to send "
                    "credentials. This is what an intercepting proxy looks "
                    "like. If Geely has genuinely moved this host to a public "
                    "CA, please open an issue so the pin can be updated."
                )
        # Remember that this host validates publicly, so no later connection
        # can be pushed into the pinning fallback by a bad certificate.
        if pin_path and not entry.get("strict"):
            store[host] = {"pins": sorted(entry.get("pins") or []), "strict": True}
            try:
                _save_pin_store(pin_path, store)
            except OSError as e:
                _LOGGER.warning(
                    "could not record that %s validates publicly (%s); the "
                    "host allowlist still blocks a pinning downgrade", host, e,
                )
        return ssock

    # --- Tier 2: public-key pinning, for Geely's private-PKI gateway only ---
    if entry.get("strict"):
        raise GeelyTLSPinError(
            f"{host} has presented a publicly-trusted certificate before but "
            f"now fails validation ({strict_error.verify_message or strict_error}). "
            "Refusing to downgrade to key pinning - possible man-in-the-middle."
        )
    if host not in _PRIVATE_PKI_HOSTS:
        raise GeelyTLSPinError(
            f"{host}: certificate validation failed "
            f"({strict_error.verify_message or strict_error}) and this host is "
            "not one that uses Geely's private CA, so key pinning does not "
            "apply. Refusing to connect."
        ) from strict_error
    if getattr(strict_error, "verify_code", None) not in _PRIVATE_CA_VERIFY_CODES:
        raise GeelyTLSPinError(
            f"{host}: certificate validation failed with "
            f"{strict_error.verify_message or strict_error}, which is not a "
            "private-CA condition - an expired certificate or a hostname "
            "mismatch is a bad certificate however it was issued. Refusing to "
            "fall back to key pinning."
        ) from strict_error

    ctx = _pinning_ctx()
    if client_cert:
        ctx.load_cert_chain(client_cert, client_key)
    raw = socket.create_connection((host, port), timeout=timeout)
    try:
        ssock = ctx.wrap_socket(raw, server_hostname=host)
    except Exception:
        _close_quietly(raw)
        raise

    der = ssock.getpeercert(binary_form=True)   # available even under CERT_NONE
    if not der:
        ssock.close()
        raise GeelyTLSPinError(f"{host}: server sent no certificate")
    spki = _spki_sha256_b64(der)

    if accepted:
        if spki in accepted:
            return ssock                          # pin matches → trusted
        ssock.close()
        raise GeelyTLSPinError(
            f"{host}: server key {spki} does not match the expected key(s) "
            f"{sorted(accepted)} - possible man-in-the-middle; refusing to "
            "send credentials. If Geely has legitimately rotated this key, "
            f"add the new one to {pin_path or 'server_pins.json'} and open an "
            "issue so it can ship as the bundled pin."
        )

    # No bundled pin and nothing stored: first contact with a private-PKI
    # host we do not ship a pin for. Record what it presented (TOFU) and treat
    # a failure to persist as fatal - a pin that is not written means the next
    # connection would trust a different key just as readily.
    if not pin_path:
        ssock.close()
        raise GeelyTLSPinError(
            f"{host}: no pin store available, so its key cannot be remembered; "
            "refusing to trust it for a single connection."
        )
    store[host] = {"pins": [spki], "strict": False}
    try:
        _save_pin_store(pin_path, store)
    except OSError as e:
        ssock.close()
        raise GeelyTLSPinError(
            f"{host}: could not persist the first-use pin to {mask_path(pin_path)} ({e}); "
            "refusing to continue unverified."
        ) from e
    _LOGGER.warning(
        "%s is not publicly-trusted and ships no bundled pin; recorded its "
        "public key on first use: %s. Later connections require this exact "
        "key.", host, spki,
    )
    return ssock


def _raw_https(host: str, method: str, path: str, headers: dict, body: bytes,
               *, pin_path: str | None, client_cert: str | None = None,
               client_key: str | None = None, timeout: int = 20) -> bytes:
    """Minimal HTTP/1.1 request over the verified/pinned transport. Returns
    the response body bytes. Used for the non-mTLS token calls so they get
    the same MITM protection as the main data path (the upstream project
    sent these over an unverified `urllib` connection)."""
    _no_crlf(path, "request path")
    _no_crlf(host, "host")
    h = dict(headers)
    h["Host"] = host
    h["Connection"] = "close"
    h["Content-Length"] = str(len(body))
    for _hk, _hv in h.items():
        _no_crlf(str(_hk), "header name")
        _no_crlf(str(_hv), "header value")
    head_lines = [f"{method} {path} HTTP/1.1"] + [f"{k}: {v}" for k, v in h.items()]
    req_bytes = ("\r\n".join(head_lines) + "\r\n\r\n").encode() + body
    ssock = _secure_tls_connect(host, 443, pin_path=pin_path,
                                client_cert=client_cert, client_key=client_key,
                                timeout=timeout)
    try:
        ssock.send(req_bytes)
        data = b""
        while True:
            c = ssock.recv(4096)
            if not c:
                break
            data += c
            if len(data) > 200_000:
                break
    finally:
        ssock.close()
    head_part, _, body_part = data.partition(b"\r\n\r\n")
    if b"chunked" in head_part.lower():
        body_part = _parse_chunked(body_part)
    return body_part


# ---------- API client ----------

class GeelyApi:
    """Holds the long-lived cidpsso token + per-vehicle cert/key, plus a
    rotating JWT for apis.ecloudeu.com calls."""

    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        user_id: str,
        vin: str,
        cidpsso_token: str,
        client_id: str,
        vehicle_series: str,
        vehicle_model: str,
        device_id: str,
        cert_path: str,
        key_path: str,
        control_host: str = "apis.ecloudeu.com",
        email: str | None = None,
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.user_id = user_id
        self.vin = vin
        self.cidpsso_token = cidpsso_token
        self.client_id = client_id
        self.vehicle_series = vehicle_series
        self.vehicle_model = vehicle_model
        self.device_id = device_id
        self.cert_path = cert_path
        self.key_path = key_path
        # Regional control endpoint; see const.REGIONS.
        self.control_host = control_host
        # Login email: the APAC session exchange authenticates with it as
        # receiverId in the request body (the EU exchange does not).
        self.email = email
        # Server-key pin store lives next to the mTLS material for this VIN.
        self.pin_path = os.path.join(os.path.dirname(cert_path), "server_pins.json") \
            if cert_path else None
        # JWT cache. Guarded by _jwt_lock: every public method runs in a Home
        # Assistant executor thread, so refreshes can overlap (see _ensure_jwt).
        self._jwt: str | None = None
        self._jwt_uid: str | None = None
        self._jwt_exp: int = 0   # unix ms
        self._jwt_lock = threading.Lock()

    # ---- low-level helpers ----

    def _sign_headers(self, method: str, url: str, body: bytes) -> dict:
        p = urlparse(url)
        nonce = _make_nonce()
        ts_ms = int(time.time() * 1000)
        accept = "application/json;responseformat=3"
        ss = _build_sign_string(
            method=method, path=p.path, query=p.query, accept=accept,
            nonce=nonce, sig_version="1.0", timestamp_ms=ts_ms, body=body,
        )
        sig = base64.b64encode(
            hmac.new(self.app_secret.encode(), ss.encode(), hashlib.sha1).digest()
        ).decode()
        return {
            "X-APP-ID": self.app_id,
            "Accept": accept,
            "X-AGENT-TYPE": "android",
            "X-DEVICE-TYPE": "mobile",
            "X-OPERATOR-CODE": "geely",
            "X-DEVICE-IDENTIFIER": self.device_id,
            "X-ENV-TYPE": "production",
            "X-VERSION": "geelyNew",
            "X-TIMEZONE": "UTC",
            "Accept-Language": "en_US",
            "Content-Type": "application/json; charset=utf-8",
            "X-api-signature-version": "1.0",
            "X-api-signature-nonce": nonce,
            "X-timestamp": str(ts_ms),
            "X-signature": sig,
            "user-agent": "okhttp/4.11.0",
        }

    def _mtls_send(self, host: str, method: str, path: str, body: bytes,
                   extra_headers: dict | None = None) -> tuple[int, bytes]:
        _no_crlf(path, "request path")
        _no_crlf(host, "host")
        headers = self._sign_headers(method, f"https://{host}{path}", body)
        headers["Host"] = host
        headers["connection"] = "close"
        headers["content-length"] = str(len(body))
        if extra_headers:
            headers.update(extra_headers)
        # The JWT and the vehicle series/model headers come from server JSON, so
        # they get the same CR/LF check _raw_https applies - a newline in one of
        # them would otherwise terminate the header block and smuggle a second
        # request onto the socket. Checked after the update() so the merged-in
        # values are covered too.
        for _hk, _hv in headers.items():
            _no_crlf(str(_hk), "header name")
            _no_crlf(str(_hv), "header value")
        head_lines = [f"{method} {path} HTTP/1.1"] + [f"{k}: {v}" for k, v in headers.items()]
        req_bytes = ("\r\n".join(head_lines) + "\r\n\r\n").encode() + body

        ssock = _secure_tls_connect(
            host, 443, pin_path=self.pin_path,
            client_cert=self.cert_path, client_key=self.key_path, timeout=30,
        )
        try:
            ssock.send(req_bytes)
            data = b''
            while True:
                c = ssock.recv(4096)
                if not c:
                    break
                data += c
                if len(data) > 200_000:
                    break
        finally:
            ssock.close()
        head_part, _, body_part = data.partition(b'\r\n\r\n')
        head_str = head_part.decode('utf-8', errors='replace')
        status_line = head_str.split('\r\n', 1)[0]
        try:
            status = int(status_line.split(' ')[1])
        except Exception:
            status = 0
        if 'chunked' in head_str.lower():
            body_part = _parse_chunked(body_part)
        return status, body_part

    # ---- high-level operations ----

    def _get_access_code(self, host: str = "m-lcmsam-eu.geely.com") -> str:
        """1-time accessCode from cidpsso (used to fetch apis JWT).

        The code must be minted by the SAME regional backend that will later
        exchange it: the APAC session service only recognises codes issued by
        m-lcmsam-kr.geely.com (EU-minted codes make it crash with 8500).
        """
        body = json.dumps({"state": str(uuid.uuid4())}).encode()
        resp_bytes = _raw_https(
            host, "POST",
            "/cidpsso/oauth2/v1/getCode",
            {
                "token": self.cidpsso_token,
                "user-agent": "okhttp/4.11.0",
                "content-type": "application/json; charset=utf-8",
            },
            body, pin_path=self.pin_path, timeout=15,
        )
        j = json.loads(resp_bytes)
        if _is_auth_failure(j):
            raise GeelyAuthError(f"cidpsso token rejected: {redact(j)}")
        if j.get("code") != 10000000:
            raise RuntimeError(f"getCode failed: {redact(j)}")
        return j["data"]["accessCode"]

    # ---- APAC session exchange (api.ecloudkr.com, NO mTLS) ----
    #
    # The EU flow exchanges the accessCode on the mTLS control host
    # (apis.ecloudeu.com /auth/account/session/secure). The APAC backend
    # instead runs the exchange on the PUBLIC host api.ecloudkr.com at
    # /auth-center/account/session with a receiverId body field, a
    # charset=utf-8 Accept, and UPPERCASE X-SIGNATURE/X-TIMESTAMP headers
    # (the lowercase variants are rejected with 1445 by the APAC gateway).
    # Verified end-to-end 2026-08-03: getCode via m-lcmsam-kr -> JWT
    # (HS256, uid, appId=GEELYE245) -> vehicle_status via mTLS = code 1000.

    def _apac_session_exchange(self) -> dict:
        """Exchange a KR-minted accessCode for the apis.ecloudkr.com JWT."""
        if not self.email:
            raise GeelyAuthError(
                "APAC session exchange needs the login email (receiverId) "
                "but the config entry has none - re-add the integration."
            )
        ac = self._get_access_code(host="m-lcmsam-kr.geely.com")
        body = json.dumps({
            "identityType": "geelyos",
            "authCode": ac,
            "receiverId": self.email,
        }, separators=(",", ":")).encode()

        host = "api.ecloudkr.com"
        path = "/auth-center/account/session"
        nonce = _make_nonce()
        ts_ms = int(time.time() * 1000)
        # Sign string uses the charset=utf-8 Accept; headers are UPPERCASE.
        ss = _build_sign_string(
            method="POST", path=path, query="",
            accept="application/json; charset=utf-8",
            nonce=nonce, sig_version="1.0", timestamp_ms=ts_ms, body=body,
        )
        sig = base64.b64encode(
            hmac.new(self.app_secret.encode(), ss.encode(), hashlib.sha1).digest()
        ).decode()
        headers = {
            "Accept": "application/json; charset=utf-8",
            "X-APP-ID": self.app_id,
            "X-CLIENT-TYPE": "2",
            "X-ENV-TYPE": "production",
            "X-OPERATOR-CODE": "GEELY",
            "X-VIN-ID": self.vin,
            "urlname": "user-api",
            "Authorization": self.cidpsso_token,
            "Content-Type": "application/json; charset=utf-8",
            "X-api-signature-version": "1.0",
            "X-api-signature-nonce": nonce,
            "X-TIMESTAMP": str(ts_ms),
            "X-SIGNATURE": sig,
            "user-agent": "okhttp/4.11.0",
        }
        resp_bytes = _raw_https(host, "POST", path, headers, body,
                                pin_path=self.pin_path, timeout=15)
        j = json.loads(resp_bytes)
        # APAC success envelope: {"resultCode": "0", "resultMessage":
        # "Success", "accessToken": ..., "userId": ..., "expiresIn": 7200}
        code = j.get("resultCode")
        if str(code) == "0":
            return j
        # Split auth failure from server failure, as the EU branch does.
        # GeelyAuthError is not retried and becomes ConfigEntryAuthFailed, which
        # costs the user a captcha and a fresh email code - far too harsh for
        # the transient failures this endpoint actually returns (8500 server
        # internal exception, 1445 signature rejected). The cidpsso token has
        # already been validated by _get_access_code above, so a failure here
        # is the session service's problem, not a dead credential. RuntimeError
        # surfaces as UpdateFailed instead: retried, last snapshot kept.
        if _is_auth_failure({"code": code}):
            raise GeelyAuthError(f"APAC session exchange rejected our auth: {redact(j)}")
        raise RuntimeError(f"APAC session exchange failed: {redact(j)}")

    def refresh_jwt(self) -> dict:
        """Exchange a cidpsso accessCode for an apis.ecloudeu.com JWT.

        APAC uses a different exchange (public host, receiverId body); see
        _apac_session_exchange.
        """
        if self.control_host == "apis.ecloudkr.com":
            d = self._apac_session_exchange()
            self._jwt = d["accessToken"]
            self._jwt_uid = d.get("userId") or self.user_id
            self._jwt_exp = int(time.time()) + int(d.get("expiresIn", 7200))
            return d
        # The code must be minted by the SAME regional backend that will
        # exchange it - the rule the APAC path already documents. A Brazilian
        # account resolved to NA proved the EU-minted default fails the
        # apis.ecloudus.com exchange with 8500 (#9); the NA gateway wants its
        # code from the US cidpsso host.
        mint_host = ("m-lcmsam-us.geely.com"
                     if "ecloudus" in (self.control_host or "")
                     else "m-lcmsam-eu.geely.com")
        ac = self._get_access_code(host=mint_host)
        body = json.dumps({"authCode": ac}).encode()
        status, resp = self._mtls_send(
            self.control_host, "POST",
            "/auth/account/session/secure?identity_type=geelyos",
            body,
        )
        j = json.loads(resp)
        if _is_auth_failure(j):
            raise GeelyAuthError(f"session/secure rejected our auth: {redact(j)}")
        if j.get("code") not in (1000, "1000"):
            # Still retryable, never GeelyAuthError: 8500 also shows up as a
            # plain transient fault on healthy EU accounts, and escalating it
            # to re-auth would cost a captcha and an email code each time.
            # But when it repeats on every attempt from the very first setup,
            # the known cause is regional (#9): the login code was minted by
            # a backend this integration has no credentials for - South
            # America / LATAM being the reported case - and this host will
            # refuse it forever. Say so, because the raw 8500 text is
            # unreadable garbage.
            hint = (
                " (if this happens on every setup attempt, the account may "
                "live on an unsupported regional backend such as South "
                "America - see issue #9)"
            ) if j.get("code") in (8500, "8500") else ""
            raise RuntimeError(f"session/secure failed{hint}: {redact(j)}")
        d = j["data"]
        self._jwt = d["accessToken"]
        self._jwt_uid = d["userId"]
        self._jwt_exp = int(time.time()) + int(d.get("expiresIn", 7200))
        return d

    def _ensure_jwt(self) -> str:
        """Return a live JWT, refreshing it at most once across all threads.

        Home Assistant runs every call here in an executor thread, so a
        coordinator poll and a user pressing unlock can arrive together. Without
        the lock both see a stale token and both run the full refresh: two
        access codes burned, two sessions opened - and Geely allows one session
        per account, so each one signs the owner's phone app out. Whichever
        refresh finishes second also overwrites _jwt with a token the server has
        already replaced.

        Double-checked: the fast path stays lock-free once a valid token is
        cached, and the second thread re-tests expiry inside the lock so it
        reuses what the first one just fetched.
        """
        if self._jwt and time.time() <= self._jwt_exp - 60:
            return self._jwt
        with self._jwt_lock:
            if not self._jwt or time.time() > self._jwt_exp - 60:
                self.refresh_jwt()
            if not self._jwt:
                # refresh_jwt returned without setting a token; better to fail
                # here than to format None into an Authorization header.
                raise GeelyAuthError("JWT refresh produced no token")
            return self._jwt

    def _headers_with_jwt(self) -> dict:
        return {
            "Authorization": self._ensure_jwt(),
            "X-CLIENT-ID": self.client_id,
            "X-VEHICLE-SERIES": self.vehicle_series,
            "X-VEHICLE-MODEL": self.vehicle_model,
            "X-Vehicle-IDENTIFIER": self.vin,
        }

    # ---- READ ----

    def _authed_apis_call(self, method: str, path: str, body: bytes) -> dict:
        """Call apis.ecloudeu.com with JWT. On code 1402 (JWT invalidated -
        most commonly because another client like the iOS app just logged
        in), auto-refresh the JWT once and retry. Only escalates to
        GeelyAuthError when the cidpsso token itself has been revoked."""
        status, resp = self._mtls_send(
            self.control_host, method, path, body,
            extra_headers=self._headers_with_jwt(),
        )
        j = json.loads(resp)
        if j.get("code") in {1402, "1402"}:
            _LOGGER.info("JWT invalidated mid-call (likely another client "
                         "logged in); refreshing and retrying once")
            self._jwt = None
            self._jwt_exp = 0
            try:
                self.refresh_jwt()
            except GeelyAuthError:
                # cidpsso token also dead - needs reauth
                raise
            status, resp = self._mtls_send(
                self.control_host, method, path, body,
                extra_headers=self._headers_with_jwt(),
            )
            j = json.loads(resp)
        if _is_auth_failure(j):
            raise GeelyAuthError(f"{method} {mask_path(path)} auth-rejected: {redact(j)}")
        return j

    def vehicle_status(self) -> dict:
        """GET full vehicle status with the same query the Geely app uses on
        map-view open: `?userId=&latest=&target=`. The empty `latest=` and
        `target=` flags signal the cloud to return the most recently uploaded
        snapshot (incl. fresh GPS if the car just pushed it). Without those
        flags the gateway serves an older cached snapshot for the position
        field. AVD-Frida confirmed (2026-05-03)."""
        path = (f"/remote-control/vehicle/status/{self.vin}"
                f"?userId={self.user_id}&latest=&target=")
        return self._authed_apis_call("GET", path, b"")

    def request_position_refresh(self) -> dict:
        """Fire PAI/operation:4/pai:1 - the Geely app fires this every time the
        map view opens to wake the car and request a fresh GPS upload. After
        the cloud ACKs (code=1000), wait a few seconds then re-fetch
        vehicle_status with `?...&latest=&target=` to read the new position.
        AVD-Frida confirmed (2026-05-03)."""
        body = {
            "command": "start",
            "creator": "tc",
            "latest": True,
            "serviceId": "PAI",
            "serviceParameters": [
                {"key": "operation", "value": "4"},
                {"key": "pai", "value": "1"},
            ],
            "timestamp": str(int(time.time() * 1000)),
            "userId": str(self.user_id),
        }
        path = f"/remote-control/vehicle/telematics/{self.vin}"
        return self._authed_apis_call("PUT", path, json.dumps(body).encode())

    def vehicle_status_state(self) -> dict:
        path = f"/remote-control/vehicle/status/state/{self.vin}"
        return self._authed_apis_call("GET", path, b"")

    def charging_reservation(self) -> dict:
        path = f"/remote-control/charging/reservation/{self.vin}"
        return self._authed_apis_call("GET", path, b"")

    def charge_server_get(self, biz_type: str) -> dict:
        """GET /charge-server/ecarx_charge_set/{VIN}?bizType=N.

        Reads schedules for charge-server features:
          - bizType=4 → Parking Comfort schedule
          - bizType=6 → Scheduled Charging (rbcStartTime/rbcEndTime/rbcTarget/bcCycleActive)
          - bizType=7 → Rapid (write-only; GET returns nothing useful)
        Returns the full response dict ({"code":"1000","data":{...}}).
        """
        path = f"/charge-server/ecarx_charge_set/{self.vin}?bizType={biz_type}"
        return self._authed_apis_call("GET", path, b"")

    def scheduled_charging_set(self, *, command: str, start_time: str,
                                end_time: str, rbc_target: str = "2",
                                rbc: str = "2", charge_model: str = "0") -> dict:
        """Set scheduled charging. command="start" enables, "stop" disables.

        Body shape for bizType=6 (charge-server). The charge-model write key
        is `chargeModel` - NOT `rbcModel`. `rbcModel` is only the read-only
        echo the GET returns; sending it as the write key puts the server in
        a branch that rejects a populated window with
        `illegal request parameter: rbcStartTime must be empty`. With
        `chargeModel` present, `rbcStartTime`/`rbcEndTime` are the *writable*
        schedule window and must be populated (sending them empty then fails
        with `rbcEndTime is missing`). The same body shape serves both
        start (enable + arm at the window) and stop (disable); `command`
        selects the forwarded operation (1/0). Verified live 2026-05-31:
        start -> op=1 + forwarded rbc.startTime, stop -> op=0.
        """
        body = {
            "bizType": "6",
            "command": command,
            "chargeModel": charge_model,
            "endTime": "",
            "pin": self.vin,
            "rbc": rbc,
            "rbcEndTime": end_time,
            "rbcStartTime": start_time,
            "rbcTarget": rbc_target,
            "scheduledTime": "",
            "sessionId": "",
            "vin": self.vin,
        }
        body_bytes = json.dumps(body, separators=(",", ":")).encode()
        path = f"/charge-server/ecarx_charge_set/{self.vin}"
        # The body carries `pin` and `vin`; only the schedule is worth a note.
        return self._recorded(
            f"scheduled_charging {command}",
            {"start": start_time, "end": end_time, "target": rbc_target,
             "chargeModel": charge_model},
            lambda: _check_control_resp(
                self._authed_apis_call("POST", path, body_bytes)))

    # ---- WRITE (control) ----

    # ---- Command trail (read by diagnostics) ----

    COMMAND_TRAIL_SIZE = 25

    def _note_text(self, text: Any) -> str:
        """One line of error text, with this car's VIN taken out of it."""
        out = str(text)
        vin = getattr(self, "vin", "") or ""
        if vin and vin in out:
            out = out.replace(vin, f"...{vin[-4:]}")
        return out[:160]

    def _recorded(self, label: str, detail: Any, send):
        """Run one remote command and keep a note of how it went.

        The faults that cost this integration the most are invisible after the
        fact: a command rejected with "the last request has not yet been
        executed" is not queued, it is lost, and the only trace is a debug line
        the reporter would have had to enable *before* it happened. Nobody does.
        So the last few commands and their outcomes are kept here, and the
        diagnostics download can answer "what did the car accept, in what order,
        and how long apart" on its own.

        Recorded: the label, the parameters, the outcome, the round trip. Not
        the URL - every control path carries the VIN - and not the response
        body, which is large and adds nothing a code does not.

        Exception text is scrubbed rather than trusted. A transport error names
        the URL it failed on ("Max retries exceeded with url:
        /remote-control/vehicle/telematics/<VIN>"), and both redaction passes
        match key names, so a VIN sitting inside a sentence would travel intact
        into a shared diagnostics report.

        Deliberately not applied to `request_position_refresh`: it is fired by
        the poller rather than by anyone, and it would push every real command
        out of a 25-entry trail within an hour.
        """
        trail = getattr(self, "command_trail", None)
        if trail is None:
            # Lazily built: instances are also created through __new__ (tests,
            # and any future path that skips the constructor), and a missing
            # attribute must not turn a working command into an exception.
            trail = self.command_trail = collections.deque(
                maxlen=self.COMMAND_TRAIL_SIZE)
        note: dict[str, Any] = {
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "command": label,
            "detail": redact(detail),
        }
        started = time.monotonic()
        try:
            resp = send()
        except GeelyControlError as e:
            note.update(outcome="rejected", code=str(e.code),
                        message=self._note_text(e.message))
            raise
        except GeelyAuthError as e:
            note.update(outcome="session-expired", message=self._note_text(e))
            raise
        except Exception as e:
            note.update(outcome="error",
                        message=self._note_text(f"{type(e).__name__}: {e}"))
            raise
        else:
            note.update(outcome="accepted", code=str(resp.get("code")))
            return resp
        finally:
            note["ms"] = int((time.monotonic() - started) * 1000)
            trail.append(note)

    def control(self, service_id: str, parameters: list[dict] | None = None,
                command: str = "start", duration: int = 0) -> dict:
        """Fire a control command via PUT /remote-control/vehicle/telematics/{VIN}.

        `duration` is the value put into operationScheduling.duration. The
        AVD-captured Geely app uses 90 for seat features, 180 for AC,
        6 for G-clean, and 0 for stop commands. Default 0 (legacy behaviour).
        """
        body_dict = {
            "command": command,
            "creator": "tc",
            "operationScheduling": {
                "duration": duration, "interval": 0, "occurs": 1, "recurrentOperation": False,
            },
            "serviceId": service_id,
            "serviceParameters": parameters or [],
            "timestamp": str(int(time.time() * 1000)),
            "userId": self._jwt_uid or self.user_id,
        }
        body = json.dumps(body_dict, separators=(",", ":")).encode()
        path = f"/remote-control/vehicle/telematics/{self.vin}"
        return self._recorded(
            f"{service_id} {command}", parameters or [],
            lambda: _check_control_resp(
                self._authed_apis_call("PUT", path, body)))

    # ---- Compound rapid warm/cool (different endpoint) ----

    def rapid_climate(self, *, ac: bool, temp: str, heat_seats: list[str] | None,
                      vent_seats: list[str] | None, vlt: bool,
                      duration: str = "180", vlt_duration: str = "60",
                      vlt_pos: str = "12", level: str = "3",
                      sw: bool | None = None,
                      extra: dict[str, str] | None = None) -> dict:
        """Fire compound climate command via POST /charge-server/ecarx_charge_set.

        Captured from the Android app's "rapid warming" / "rapid cooling"
        buttons (bizType=7). Bundles AC + seat heat OR vent + window vent
        in a single shot.

        seats: numeric positions - 11=driver, 19=passenger. Verified on a real
        EX5 (#19): the owner fired exactly this body through the `fire_rapid`
        service and both front seats went to high, which settled a long-running
        suspicion that the encoding was wrong. It is not - when the seats appear
        not to respond, suspect the read-back timing instead, because the seat
        state arrives in `climateStatus` after the request is already accepted.

        `sw` is the steering wheel. The app's own rapid-warming body carries
        `"sw": "true"` (captured on a real car, #4) - the field two rounds of
        probe candidates were guessing at. None omits the key entirely, which
        keeps the body byte-identical to the shape verified on cars without
        the wheel; earlier probe rounds proved the gateway accepts unknown
        extra keys, so sending it can at worst be ignored.

        `level` and `extra` exist for the `fire_rapid` probe service rather than
        the entities. `extra` is applied last and may therefore override a
        computed key: deliberate, and the reason nothing but the probe passes it.
        """
        body: dict = {
            "ac": "true" if ac else "false",
            "bizType": "7",
            "command": "immediately",
            "duration": duration,
            "paa": "0",
            "temp": temp,
            "timestamp": str(int(time.time() * 1000)),
            "vlt": "true" if vlt else "false",
            "vltDuration": vlt_duration,
            "vltPos": vlt_pos,
        }
        if heat_seats:
            body["heat"] = [{"level": level, "pos": p} for p in heat_seats]
        if vent_seats:
            body["ventilation"] = [{"level": level, "pos": p} for p in vent_seats]
        if sw is not None:
            body["sw"] = "true" if sw else "false"
        if extra:
            body.update(extra)
        body_bytes = json.dumps(body, separators=(",", ":")).encode()
        path = f"/charge-server/ecarx_charge_set/{self.vin}"
        return self._recorded(
            "rapid_climate",
            {"temp": temp, "ac": body["ac"], "heat": body.get("heat"),
             "ventilation": body.get("ventilation"), "vlt": body["vlt"]},
            lambda: _check_control_resp(
                self._authed_apis_call("POST", path, body_bytes)))

    # ---- Capability discovery ----

    def fetch_capabilities(self) -> list[dict]:
        """Fetch the per-vehicle feature catalog. Returns the data.list array.

        Used at coordinator setup to decide which entities to expose. The
        catalog returns one entry per `functionId` (e.g. `remote_climate_control_2`,
        `combined_climate_control`, `remote_purification`, `temperature_2`,
        plus battery/door/etc. status fields). Each entry has `valueEnable`,
        `paramsJson`, `valueRange`, `valueEnum`. See docs/AVD_CAPTURE_GUIDE.md
        for the full schema.
        """
        path = (
            f"/geelyTCAccess/tcservices/capability/{self.vin}"
            "?pageSize=2000&pageIndex=1&vehicleType=0&sortField=&direction="
        )
        try:
            j = self._authed_apis_call("GET", path, b"")
            return (j.get("data") or {}).get("list", []) or []
        except Exception:
            return []


# ---------- Cert provisioning (one-time during config_flow) ----------

def _sign_request_for_api_ecloudeu(app_id: str, app_secret: str,
                                    method: str, url: str, body: bytes) -> dict:
    """Standalone signer for /auth/cert/* on api.ecloudeu.com (no mTLS)."""
    p = urlparse(url)
    nonce = _make_nonce()
    ts_ms = int(time.time() * 1000)
    ss = _build_sign_string(
        method=method, path=p.path, query=p.query, accept="application/json;responseformat=3",
        nonce=nonce, sig_version="1.0", timestamp_ms=ts_ms, body=body,
    )
    sig = base64.b64encode(
        hmac.new(app_secret.encode(), ss.encode(), hashlib.sha1).digest()
    ).decode()
    return {
        "X-APP-ID": app_id,
        "Accept": "application/json;responseformat=3",
        "X-AGENT-TYPE": "android",
        "X-DEVICE-TYPE": "mobile",
        "X-OPERATOR-CODE": "geely",
        "X-ENV-TYPE": "production",
        "X-VERSION": "geelyNew",
        "Content-Type": "application/json; charset=utf-8",
        "X-api-signature-version": "1.0",
        "X-api-signature-nonce": nonce,
        "X-timestamp": str(ts_ms),
        "X-signature": sig,
        "user-agent": "okhttp/4.11.0",
    }


def provision_user_cert(*, app_id: str, app_secret: str, user_id: str,
                         cidpsso_token: str, cert_out_path: str,
                         key_out_path: str,
                         cert_host: str = "api.ecloudeu.com") -> tuple[str, str]:
    """Generate EC P-256 keypair + CSR, send through /auth/cert/info + /file,
    save signed cert + key. Returns (cert_path, key_path)."""
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.x509.oid import NameOID
    from cryptography import x509

    # Pin store lives alongside the cert we're about to write.
    pin_path = os.path.join(os.path.dirname(cert_out_path), "server_pins.json")

    # 1. Generate keypair
    priv = ec.generate_private_key(ec.SECP256R1())
    cn_short = hashlib.sha256(user_id.encode()).hexdigest()[:8]
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([
            x509.NameAttribute(NameOID.COUNTRY_NAME, "CN"),
            x509.NameAttribute(NameOID.STATE_OR_PROVINCE_NAME, "ZheJiang"),
            x509.NameAttribute(NameOID.LOCALITY_NAME, "Hangzhou"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "ECARX"),
            x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, "CloudDept"),
            x509.NameAttribute(NameOID.COMMON_NAME, cn_short),
        ]))
        .sign(priv, hashes.SHA256())
    )
    csr_pem = csr.public_bytes(serialization.Encoding.PEM).decode()

    # 2. POST /auth/cert/info → checkCode
    body = json.dumps({"checkValue": user_id}, separators=(',', ':')).encode()
    headers = _sign_request_for_api_ecloudeu(
        app_id, app_secret, "POST",
        f"https://{cert_host}/auth/cert/info", body)
    resp_bytes = _raw_https(cert_host, "POST", "/auth/cert/info",
                            headers, body, pin_path=pin_path, timeout=20)
    j = json.loads(resp_bytes)
    if j.get("code") != 1000:
        if str(j.get("code")) in _REGION_MISMATCH_CODES:
            raise GeelyRegionError(
                "Geely's EU cert server rejected this account "
                f"(code {j.get('code')}: {j.get('hint') or j.get('message')}). "
                "The account is registered with another regional backend "
                "(APAC, North America or South America), which needs its own "
                "app credentials this integration does not have."
            )
        raise RuntimeError(f"cert/info failed: {redact(j)}")
    check_code = j["data"]["checkCode"]

    # 3. POST /auth/cert/file → signed cert
    device_for_cert = hashlib.sha256(f"{user_id}_geely_ex5_ha".encode()).hexdigest()
    body = json.dumps({
        "csr": csr_pem,
        "identityType": "geelyos",
        "accessToken": cidpsso_token,
        "deviceId": device_for_cert,
        "checkCode": check_code,
    }, separators=(',', ':')).encode()
    headers = _sign_request_for_api_ecloudeu(
        app_id, app_secret, "POST",
        f"https://{cert_host}/auth/cert/file", body)
    resp_bytes = _raw_https(cert_host, "POST", "/auth/cert/file",
                            headers, body, pin_path=pin_path, timeout=30)
    j = json.loads(resp_bytes)

    # APAC names the same challenge `checkValue` and answers 8200 when it arrives
    # as `checkCode` (#32, reported from an Australian account). Retried under
    # the other name, with a FRESH challenge because it is single-use - the one
    # above has now been spent either way.
    #
    # Written as an additive branch rather than a refactor of the two requests
    # above on purpose: this path cannot be tested against a real APAC backend
    # from here, so the working sequence stays byte-for-byte as it was and only a
    # response that has already failed can reach this code.
    if (str(j.get("code")) == "8200"
            and "checkvalue" in str(j.get("hint") or j.get("message") or "").lower()):
        info_body = json.dumps({"checkValue": user_id}, separators=(',', ':')).encode()
        info_headers = _sign_request_for_api_ecloudeu(
            app_id, app_secret, "POST",
            f"https://{cert_host}/auth/cert/info", info_body)
        info = json.loads(_raw_https(cert_host, "POST", "/auth/cert/info",
                                     info_headers, info_body,
                                     pin_path=pin_path, timeout=20))
        if info.get("code") == 1000:
            body = json.dumps({
                "csr": csr_pem,
                "identityType": "geelyos",
                "accessToken": cidpsso_token,
                "deviceId": device_for_cert,
                "checkValue": info["data"]["checkCode"],
            }, separators=(',', ':')).encode()
            headers = _sign_request_for_api_ecloudeu(
                app_id, app_secret, "POST",
                f"https://{cert_host}/auth/cert/file", body)
            j = json.loads(_raw_https(cert_host, "POST", "/auth/cert/file",
                                      headers, body,
                                      pin_path=pin_path, timeout=30))

    if j.get("code") != 1000:
        # 1501 'geelyos verify error' is the EU cert server refusing to verify a
        # token that belongs to another region's GeelyOS. Everything before this
        # point succeeds, so say what it means instead of dumping the response.
        if str(j.get("code")) in _REGION_MISMATCH_CODES:
            raise GeelyRegionError(
                "Geely's EU cert server rejected this account "
                f"(code {j.get('code')}: {j.get('hint') or j.get('message')}). "
                "The account is registered with another regional backend "
                "(APAC, North America or South America), which needs its own "
                "app credentials this integration does not have."
            )
        raise RuntimeError(f"cert/file failed: {redact(j)}")
    cert_pem = j["data"]["cert"]

    # 4. Save to disk with owner-only permissions.
    #    SECURITY: the private key authenticates HA to Geely as this vehicle's
    #    controller. Anyone who can read key.pem + cert.pem can lock/unlock and
    #    drive the climate remotely, so the directory is 0700 and the key is
    #    0600. We open the key via os.open with mode 0600 to avoid a brief
    #    world-readable window between create and chmod.
    key_bytes = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    vin_dir = os.path.dirname(cert_out_path)
    os.makedirs(vin_dir, mode=0o700, exist_ok=True)
    # makedirs applies `mode` to the leaf only - intermediate directories get
    # the default umask. The intermediate here is .storage/geely_connect, whose
    # entries are named after the VIN, so leaving it 0755 lets any local account
    # list it and read the VIN off the directory name even though the key inside
    # is unreadable. Tighten both.
    for d in (vin_dir, os.path.dirname(vin_dir)):
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
    with open(cert_out_path, "w") as fh:
        fh.write(cert_pem)
    try:
        os.chmod(cert_out_path, 0o600)
    except OSError:
        pass
    key_fd = os.open(key_out_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(key_fd, "wb") as fh:
        fh.write(key_bytes)
    try:
        os.chmod(key_out_path, 0o600)
    except OSError:
        pass
    return cert_out_path, key_out_path


# ---------- cidpsso login (for config_flow) ----------

LOGIN_HOST = "https://access-app-global.geely.com"
APP_HOST   = "https://m-lcmsam-eu.geely.com"
# Same host the APAC access code is minted on - see _get_access_code. Used only
# as the vehicle-list fallback for accounts whose EU garage comes back empty.
APAC_APP_HOST = "https://m-lcmsam-kr.geely.com"


def _ios_headers(token: str | None = None, user_id: str | None = None,
                 country_code: str = "GB", *,
                 idfa: str | None = None, idfv: str | None = None) -> dict:
    """Mimic the Geely iOS app's headers verbatim - required for both cidpsso
    and cidpcar gateway calls.

    `idfa` and `idfv` should be passed from the per-install fingerprint so
    HA's session is distinguishable from the user's iPhone/Android session.
    Only used as a fallback when omitted (mostly during initial setup).
    """
    rt = int(time.time() * 1000)
    h = {
        "Content-Type":  "application/json",
        "User-Agent":    "geely/1.9.8 (iPhone; iOS 26.3.1; Scale/3.00)",
        "version":       "1.9.8",
        "devicename":    "Home Assistant",
        "model":         "iPhone",
        "system-flag":   "1",
        "systemversion": "26.3.1",
        "countrycode":   country_code,
        "accept-language": "en-GB",
        "lang":          "en-GB",
        "accept":        "*/*",
        "devicehardwareidfa": (idfa or str(uuid.uuid4()).upper()),
        "devicehardwareidfv": (idfv or str(uuid.uuid4()).upper()),
        "requesttime":   str(rt),
        "requestid":     f"{rt}{secrets.token_hex(6)}",
    }
    if token:
        h["token"] = token
    if user_id:
        h["userid"] = user_id
    return h


def make_install_fingerprint() -> tuple[str, str]:
    """Generate a (idfa, idfv) pair for this HA install. Persist these in
    the ConfigEntry data and pass them to every cidpsso/cidpcar call so the
    server treats HA as a distinct device from the user's phone."""
    return (str(uuid.uuid4()).upper(), str(uuid.uuid4()).upper())


def _legacy_session():
    """`requests` session with OpenSSL legacy-renegotiation enabled - Geely's
    login/captcha gateway needs it on Python 3.12+.

    SECURITY: certificate verification is left ENABLED (CERT_REQUIRED +
    hostname check) and we explicitly load a CA trust store into the custom
    context, so the login/OTP/vehicle-list calls - which carry the e-mail,
    the OTP code and the freshly issued cidpsso token - are validated
    against public CAs. The legacy-renegotiation option (0x4) only relaxes
    the handshake mode, not the trust decision."""
    import requests
    from urllib3.util.ssl_ import create_urllib3_context

    def _build_ctx():
        ctx = create_urllib3_context()
        ctx.options |= 0x4
        # Guarantee a trust store is present (a bare urllib3 context has none).
        try:
            import certifi
            ctx.load_verify_locations(cafile=certifi.where())
        except Exception:  # noqa: BLE001
            pass
        try:
            ctx.load_default_certs()
        except Exception:  # noqa: BLE001
            pass
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        return ctx

    class _Adapter(requests.adapters.HTTPAdapter):
        def init_poolmanager(self, *a, **kw):
            kw["ssl_context"] = _build_ctx()
            return super().init_poolmanager(*a, **kw)

    s = requests.Session()
    s.mount("https://", _Adapter())
    return s


def cidpsso_send_otp(email: str, country_code: str = "GB", *,
                     max_attempts: int = 5,
                     idfa: str | None = None, idfv: str | None = None) -> dict:
    """Solve the Geely GeeTest captcha + trigger OTP email send.

    The captcha solver is image-based and ~85% accurate, so we retry up to
    `max_attempts` times. Returns the first successful /getCaptcha response,
    or the last response/error encountered.
    """
    import requests

    from . import geetest_solver

    last_response: dict | None = None
    last_error: str | None = None
    s = _legacy_session()
    headers = _ios_headers(country_code=country_code, idfa=idfa, idfv=idfv)

    for attempt in range(1, max_attempts + 1):
        try:
            captcha = geetest_solver.solve(verbose=False)
        except requests.exceptions.ConnectionError as e:
            # Network-level failure, not solver inaccuracy: fail fast with
            # the host name instead of burning every retry on a dead route.
            _LOGGER.debug("captcha attempt %d could not reach the host: %s",
                          attempt, e)
            raise GeelyCaptchaUnreachableError(
                f"cannot reach {geetest_solver.GEELY_HOST} from this "
                "Home Assistant host") from e
        except Exception as e:  # noqa: BLE001
            last_error = f"captcha solve threw: {e}"
            _LOGGER.debug("captcha attempt %d threw: %s", attempt, e)
            continue
        if not (captcha.get("status") == "success"
                and captcha.get("data", {}).get("result") == "success"):
            last_error = f"captcha solve rejected by /verify: {redact(captcha)}"
            _LOGGER.debug("captcha attempt %d rejected: %s", attempt, redact(captcha))
            continue
        v = captcha["data"]
        body = {
            "captchaType":   "2",
            "passToken":     v["pass_token"],
            "platform":      "ios-login",
            "lotNumber":     v["lot_number"],
            "captchaOutput": v["captcha_output"],
            "genTime":       str(v["gen_time"]),
            "email":         email,
            "captchaScene":  "101",
        }
        r = s.post(f"{LOGIN_HOST}/cidpsso/captcha/v3/getCaptcha",
                   headers=headers, json=body, timeout=20)
        resp = r.json()
        last_response = resp
        if resp.get("success") or resp.get("code") == 10000000:
            return resp
        _LOGGER.debug("getCaptcha attempt %d server-rejected: %s", attempt, redact(resp))

    if last_response is not None:
        return last_response
    raise RuntimeError(f"captcha solver failed all {max_attempts} attempts: {last_error}")


def cidpsso_login(email: str, otp: str, country_code: str = "GB", *,
                  idfa: str | None = None, idfv: str | None = None) -> dict:
    """Exchange OTP code for cidpsso session token. Returns server response.
    Token is in data.token; userId is data.userId."""
    body = {
        "countryCode":    country_code,
        "account":        email,
        "code":           otp,
        "registerSource": 102,
        "loginType":      3,    # 3 = email-code
        "accountType":    2,    # 2 = email
    }
    s = _legacy_session()
    r = s.post(f"{LOGIN_HOST}/cidpsso/user/v3/login",
               headers=_ios_headers(country_code=country_code,
                                    idfa=idfa, idfv=idfv),
               json=body, timeout=20)
    return r.json()


def list_vehicles(cidpsso_token: str, user_id: str | None = None,
                  country_code: str = "GB", *,
                  idfa: str | None = None, idfv: str | None = None) -> list[dict]:
    """List vehicles for the logged-in account. Returns the `data` list
    from /cidpcar/vehicleOwner/v2/controlCars.

    An empty v2 garage falls back to the Korean v1 endpoint. Reported by an
    Australian owner (#32) whose account authenticates on the global gateway and
    then shows no cars there, while his own app reads them from the KR host - and
    it matches a pattern this file already documents, since APAC access codes
    have to be minted on that same host (see _get_access_code).

    The fallback fires ONLY on an empty result, so an account that lists cars
    today cannot reach it: a non-empty v2 reply is authoritative and returns
    immediately. The cost of the fallback is one extra request during setup for
    an account that genuinely owns no cars, which is the case that fails anyway.
    """
    s = _legacy_session()
    headers = _ios_headers(token=cidpsso_token, user_id=user_id,
                           country_code=country_code, idfa=idfa, idfv=idfv)
    r = s.get(f"{APP_HOST}/cidpcar/vehicleOwner/v2/controlCars",
              headers=headers, timeout=20)
    j = r.json()
    vehicles = j.get("data") or []
    if vehicles:
        return vehicles
    # Same session, same headers, same pinned transport - only the host and the
    # API version differ, and both hosts already carry this token elsewhere in
    # this file. The v1 reply wraps the same `data` list.
    try:
        r = s.get(f"{APAC_APP_HOST}/cidpcar/vehicleOwner/v1/listControlCars",
                  headers=headers, timeout=20)
        return r.json().get("data") or []
    except Exception as e:  # noqa: BLE001
        # A dead fallback must not turn "you own no cars" into a stack trace:
        # the caller's empty-list path already says the right thing.
        _LOGGER.debug("APAC vehicle-list fallback failed: %s", e)
        return []
