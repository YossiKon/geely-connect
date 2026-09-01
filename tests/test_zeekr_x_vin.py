"""x-vin derivation: VIN -> AES-128-CBC/PKCS7 -> Base64, per known app build,
verified against the gateway before use.

Vectors use an obviously fake VIN only; no real VIN, x-vin, key provenance
beyond the public material, or captured request is in this file.
"""
from __future__ import annotations

import base64

from conftest import load

zc = load("zeekr_client")

FAKE_VIN = "TESTVIN0000000001"          # 17 chars, not a real VIN
# Pins the two shipped (key, iv) pairs so a silent constant change is caught.
_VECTORS = {
    b"a01a6db985a2f5d4": "NC5s9vGCMCIqeBTHAf2obrtzAkYDBU8zWHPJPQ0/5NM=",
    b"2a25d6c112dcf841": "vXhc1YYp6iF/RD27Bhn5RNwmARwFRTr9A1wiKclIukU=",
}


def test_derive_x_vin_matches_the_pinned_vectors():
    for key, iv, _note in zc._X_VIN_MATERIAL:
        assert zc.derive_x_vin(FAKE_VIN, key, iv) == _VECTORS[key], key


def test_derive_x_vin_is_a_reversible_aes_cbc_pkcs7():
    """Independent of any pinned vector: decrypting with the same material must
    return the VIN, i.e. it really is AES-128-CBC with PKCS7 padding."""
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    for key, iv, _note in zc._X_VIN_MATERIAL:
        ct = base64.b64decode(zc.derive_x_vin(FAKE_VIN, key, iv))
        dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        padded = dec.update(ct) + dec.finalize()
        unpad = padding.PKCS7(128).unpadder()
        assert (unpad.update(padded) + unpad.finalize()).decode() == FAKE_VIN


def _client_with(accepting_x_vin):
    """A logged-in client whose capability call only succeeds for one x-vin."""
    client = zc.ZeekrClient(email="a@b.c", password="")
    client.access_token = "tok"
    def fake_caps():
        if client.enc_vin != accepting_x_vin:
            raise zc.ZeekrApiError("HTTP 401: rejected x-vin")
        return [{"functionCode": "x"}]
    client.capabilities_new = fake_caps
    return client


def test_probe_returns_the_value_the_gateway_accepts():
    key, iv, _ = zc._X_VIN_MATERIAL[-1]         # the AU/SEA build
    accepted = zc.derive_x_vin(FAKE_VIN, key, iv)
    client = _client_with(accepted)
    client.enc_vin = "previous-value"
    assert client.probe_x_vin(FAKE_VIN) == accepted
    # the probe must not disturb whatever x-vin was already set
    assert client.enc_vin == "previous-value"


def test_probe_returns_empty_when_no_build_matches():
    client = _client_with("something-no-build-produces")
    client.enc_vin = "keep-me"
    assert client.probe_x_vin(FAKE_VIN) == ""
    assert client.enc_vin == "keep-me"


def test_probe_needs_a_session_and_a_vin():
    client = _client_with("x")
    client.access_token = None
    assert client.probe_x_vin(FAKE_VIN) == ""
    client.access_token = "tok"
    assert client.probe_x_vin("") == ""
