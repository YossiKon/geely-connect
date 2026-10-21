"""Local EU key handling, including failure before any network operation."""
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from conftest import load
from test_zeekr_europe import FAKE_KEYS, FAKE_PROFILE

regions = load("zeekr_regions")
zc = load("zeekr_client")


def _rejected(fn):
    try:
        fn()
    except regions.RegionKeysError as exc:
        for value in FAKE_KEYS.values():
            assert value not in str(exc)
        return str(exc)
    raise AssertionError("Expected a safe local-key error")


def test_explicit_europe_requires_keys_and_sea_never_reads_the_file():
    with tempfile.TemporaryDirectory() as root:
        with patch.object(zc.http.client, "HTTPSConnection",
                          side_effect=AssertionError("Must not contact a server")):
            _rejected(lambda: regions.load_region_profile(root, "EU"))
        with patch.object(Path, "open", side_effect=AssertionError("Must not read keys")):
            assert regions.load_region_profile(root, "SEA") is None
            _rejected(lambda: regions.load_region_profile(root, "unknown"))


def test_load_only_the_installation_local_file():
    with tempfile.TemporaryDirectory() as root:
        p = Path(root) / regions.KEYS_FILENAME
        p.write_text(json.dumps(FAKE_KEYS))
        profile = regions.load_region_profile(root, "EU")
        assert profile == FAKE_PROFILE
        # Replacing local keys is picked up at the next load, not cached globally.
        other = {**FAKE_KEYS, "hmac_secret": "different-local-key"}
        p.write_text(json.dumps(other))
        assert regions.load_region_profile(root, "EU").hmac_secret == "different-local-key"
        assert profile.hmac_secret == FAKE_KEYS["hmac_secret"]


def test_invalid_json_and_large_files_do_not_echo_contents():
    with tempfile.TemporaryDirectory() as root:
        p = Path(root) / regions.KEYS_FILENAME
        for content in ("not-json " + FAKE_KEYS["hmac_secret"],
                        json.dumps([FAKE_KEYS]), " " * 4097):
            p.write_text(content)
            message = _rejected(lambda: regions.load_region_profile(root, "EU"))
            if len(content) > 4096:
                assert "too large" in message
                assert "JSON" not in message


def test_unreadable_and_non_utf8_key_files_fail_without_exposing_contents():
    with tempfile.TemporaryDirectory() as root:
        p = Path(root) / regions.KEYS_FILENAME
        p.write_bytes(b"\xff\xfe\xff" + FAKE_KEYS["hmac_secret"].encode())
        _rejected(lambda: regions.load_region_profile(root, "EU"))
        with patch.object(Path, "open", side_effect=PermissionError("private")):
            _rejected(lambda: regions.load_region_profile(root, "EU"))


def test_europe_requires_valid_aes_lengths():
    for length in (16, 24, 32):
        profile = regions.profile_from_keys(
            {**FAKE_KEYS, "vin_key_hex": "AB" * length})
        assert profile.vin_key == b"\xab" * length
    _rejected(lambda: regions.profile_from_keys(
        {**FAKE_KEYS, "vin_iv_hex": "ab" * 15}))


def test_key_schema_rejects_invalid_values_and_host_overrides():
    variants = [
        {**FAKE_KEYS, "hmac_secret": ""},
        {**FAKE_KEYS, "hmac_access_key": "header\r\ninjection"},
        {**FAKE_KEYS, "snc_secret": None},
        {**FAKE_KEYS, "vin_key_hex": "00"},
        {**FAKE_KEYS, "vin_iv_hex": "x" * 32},
        {**FAKE_KEYS, "vin_iv_hex": "00 " * 16},
        {**FAKE_KEYS, "idaas_gateway": "https://example.invalid"},
        {k: v for k, v in FAKE_KEYS.items() if k != "snc_secret"},
    ]
    for variant in variants:
        _rejected(lambda: regions.profile_from_keys(variant))


def test_keys_are_hidden_from_repr_and_redaction():
    api = load("api")
    output = api.redact({"nested": FAKE_KEYS, "eu_app_keys": FAKE_KEYS})
    for value in FAKE_KEYS.values():
        assert value not in repr(output)
        assert value not in repr(FAKE_PROFILE)
    assert FAKE_PROFILE.vin_key.decode() not in repr(FAKE_PROFILE)
    assert FAKE_PROFILE.vin_iv.decode() not in repr(FAKE_PROFILE)


def test_one_installation_cannot_change_another_clients_identity():
    other = regions.profile_from_keys({**FAKE_KEYS, "hmac_secret": "second-installation"})
    first = zc.ZeekrIdaas(country="ES", profile=FAKE_PROFILE)
    second = zc.ZeekrIdaas(country="ES", profile=other)
    a = first._headers("POST", "/test", b"test")
    b = second._headers("POST", "/test", b"test")
    assert a["X-HMAC-DIGEST"] != b["X-HMAC-DIGEST"]
    assert first.profile is FAKE_PROFILE
    assert second.profile is other
