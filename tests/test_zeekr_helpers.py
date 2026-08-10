"""At-rest credential encryption in helpers.py.

The zeekr flow stores the account password so the HF session can renew
itself. With a `geely_password_key` in secrets.yaml the value is AES-256-GCM
encrypted ("enc:" prefix); without one it degrades to plaintext with a
warning - the same posture as the integration's existing private-key storage.
"""

import os
import tempfile

from conftest import have_homeassistant, load
from run import skip


def _helpers():
    if not have_homeassistant():
        skip("homeassistant not installed")
    return load("helpers")


class _Hass:
    def __init__(self, secrets_content=None):
        self._dir = tempfile.mkdtemp(prefix="geely-helpers-")
        if secrets_content is not None:
            with open(os.path.join(self._dir, "secrets.yaml"), "w") as f:
                f.write(secrets_content)
        self.config = type("_Cfg", (), {
            "path": lambda cfg, p: os.path.join(self._dir, p)})()


def test_encrypt_decrypt_roundtrip_with_a_key():
    h = _helpers()
    hass = _Hass(secrets_content="geely_password_key: s3cret-key-123\n")
    stored = h.password_encrypt(hass, "hunter2")
    assert stored.startswith("enc:"), f"expected enc: prefix, got {stored!r}"
    assert "hunter2" not in stored, "plaintext must not appear in the value"
    assert h.password_decrypt(hass, stored) == "hunter2"


def test_encrypt_without_a_key_falls_back_to_plaintext():
    h = _helpers()
    hass = _Hass()  # no secrets.yaml at all
    assert h.password_encrypt(hass, "hunter2") == "hunter2"


def test_malformed_secrets_yaml_is_treated_as_no_key():
    h = _helpers()
    hass = _Hass(secrets_content="::: this is not yaml :::\n\tbroken")
    assert h.password_encrypt(hass, "hunter2") == "hunter2", \
        "a broken secrets.yaml must not crash the flow"


def test_decrypt_passes_plaintext_through():
    h = _helpers()
    hass = _Hass()
    assert h.password_decrypt(hass, "") == ""
    assert h.password_decrypt(hass, "hunter2") == "hunter2"
    assert h.password_decrypt(hass, "enc:not-a-real-value") == "", \
        "malformed enc: values must not raise"


def test_decrypt_with_the_wrong_key_returns_empty():
    h = _helpers()
    hass_a = _Hass(secrets_content="geely_password_key: key-a\n")
    hass_b = _Hass(secrets_content="geely_password_key: key-b\n")
    stored = h.password_encrypt(hass_a, "hunter2")
    assert h.password_decrypt(hass_b, stored) == "", \
        "wrong key must fail closed, not raise"


def test_decrypt_without_the_key_returns_empty():
    h = _helpers()
    hass_key = _Hass(secrets_content="geely_password_key: key-a\n")
    hass_no_key = _Hass()
    stored = h.password_encrypt(hass_key, "hunter2")
    assert h.password_decrypt(hass_no_key, stored) == ""


def test_a_secrets_file_without_the_key_falls_back_to_plaintext():
    h = _helpers()
    hass = _Hass(secrets_content="some_other_key: 12345\n")
    assert h.password_encrypt(hass, "hunter2") == "hunter2"
