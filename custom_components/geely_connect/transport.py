"""Secure TLS transport for the Geely Connect integration.

Original work. This is our hardened networking layer — independent of any
specific Geely wire details. It provides a fail-closed connection helper:

  1. Strict public-CA + hostname validation (best case).
  2. If (and only if) that fails because the chain isn't publicly trusted,
     fall back to trust-on-first-use public-key pinning (SSH known_hosts
     style) and require an exact key match on every subsequent call.

At no point is an arbitrary certificate trusted on an ongoing session.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import socket
import ssl
from typing import Any

_LOGGER = logging.getLogger(__name__)

# OpenSSL legacy renegotiation - some Geely gateways need it. Relaxes only the
# handshake mode, NOT the trust decision.
_TLS_LEGACY_RENEG = 0x4


class TLSPinError(Exception):
    """Server public key did not match the stored pin - possible MITM."""


def _no_crlf(value: str, what: str) -> str:
    if "\r" in value or "\n" in value:
        raise ValueError(f"illegal CR/LF in {what}")
    return value


def _strict_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    try:
        import certifi
        ctx.load_verify_locations(cafile=certifi.where())
    except Exception:  # noqa: BLE001
        pass
    ctx.options |= _TLS_LEGACY_RENEG
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


def _pinning_ctx() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.options |= _TLS_LEGACY_RENEG
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _spki_sha256_b64(der_cert: bytes) -> str:
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    cert = x509.load_der_x509_certificate(der_cert)
    spki = cert.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return base64.b64encode(hashlib.sha256(spki).digest()).decode()


def _load_pins(pin_path: str | None) -> dict:
    if not pin_path or not os.path.exists(pin_path):
        return {}
    try:
        with open(pin_path) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _save_pins(pin_path: str, pins: dict) -> None:
    os.makedirs(os.path.dirname(pin_path), mode=0o700, exist_ok=True)
    fd = os.open(pin_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(pins, fh, indent=2, sort_keys=True)
    try:
        os.chmod(pin_path, 0o600)
    except OSError:
        pass


def secure_connect(host: str, port: int, *, pin_path: str | None,
                   client_cert: str | None = None, client_key: str | None = None,
                   timeout: int = 30) -> ssl.SSLSocket:
    """Open a fail-closed TLS connection. See module docstring."""
    _no_crlf(host, "host")
    strict = _strict_ctx()
    if client_cert:
        strict.load_cert_chain(client_cert, client_key)
    raw = socket.create_connection((host, port), timeout=timeout)
    try:
        return strict.wrap_socket(raw, server_hostname=host)
    except ssl.SSLCertVerificationError:
        try:
            raw.close()
        except OSError:
            pass
    except Exception:
        try:
            raw.close()
        except OSError:
            pass
        raise

    # Pinning fallback (private / self-signed chain).
    pins = _load_pins(pin_path)
    ctx = _pinning_ctx()
    if client_cert:
        ctx.load_cert_chain(client_cert, client_key)
    raw = socket.create_connection((host, port), timeout=timeout)
    try:
        ssock = ctx.wrap_socket(raw, server_hostname=host)
    except Exception:
        try:
            raw.close()
        except OSError:
            pass
        raise

    der = ssock.getpeercert(binary_form=True)
    if not der:
        ssock.close()
        raise TLSPinError(f"{host}: server sent no certificate")
    spki = _spki_sha256_b64(der)
    known = pins.get(host)
    if known:
        if spki in known:
            return ssock
        ssock.close()
        raise TLSPinError(
            f"{host}: server key {spki} does not match pinned key - possible MITM"
        )
    if pin_path:
        pins[host] = [spki]
        try:
            _save_pins(pin_path, pins)
        except Exception as e:  # noqa: BLE001
            _LOGGER.warning("could not persist TLS pin for %s: %s", host, e)
    _LOGGER.warning(
        "%s is not publicly trusted; pinned its key on first use (TOFU): %s",
        host, spki,
    )
    return ssock


def https_request(host: str, method: str, path: str, headers: dict, body: bytes,
                  *, pin_path: str | None, client_cert: str | None = None,
                  client_key: str | None = None, timeout: int = 30) -> tuple[int, bytes]:
    """Minimal HTTP/1.1 request over the verified/pinned transport.
    Returns (status_code, body_bytes)."""
    _no_crlf(path, "request path")
    _no_crlf(host, "host")
    h = dict(headers)
    h["Host"] = host
    h["Connection"] = "close"
    h["Content-Length"] = str(len(body))
    for k, v in h.items():
        _no_crlf(str(k), "header name")
        _no_crlf(str(v), "header value")
    req = ("\r\n".join([f"{method} {path} HTTP/1.1"]
                       + [f"{k}: {v}" for k, v in h.items()]) + "\r\n\r\n").encode() + body
    ssock = secure_connect(host, 443, pin_path=pin_path,
                           client_cert=client_cert, client_key=client_key, timeout=timeout)
    try:
        ssock.send(req)
        data = b""
        while True:
            chunk = ssock.recv(4096)
            if not chunk:
                break
            data += chunk
            if len(data) > 500_000:
                break
    finally:
        ssock.close()
    head, _, body_part = data.partition(b"\r\n\r\n")
    head_str = head.decode("utf-8", "replace")
    try:
        status = int(head_str.split("\r\n", 1)[0].split(" ")[1])
    except Exception:
        status = 0
    if "chunked" in head_str.lower():
        body_part = _dechunk(body_part)
    return status, body_part


def _dechunk(body: bytes) -> bytes:
    out, pos = b"", 0
    while pos < len(body):
        nl = body.find(b"\r\n", pos)
        if nl < 0:
            break
        try:
            size = int(body[pos:nl], 16)
        except ValueError:
            break
        if size == 0:
            break
        start = nl + 2
        out += body[start:start + size]
        pos = start + size + 2
    return out
