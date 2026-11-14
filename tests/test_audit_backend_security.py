"""SEC-4: fake local material only; no account or cloud access."""
import asyncio
import io
import logging
import os
import tempfile
import types
from unittest.mock import patch

from conftest import FAKE_VIN, load
from test_init_lifecycle import _Hass, _mod, _ir_module


def test_failed_credential_deletion_never_claims_success():
    m = _mod()
    with tempfile.TemporaryDirectory() as root:
        hass = _Hass()
        hass.config.path = lambda *p: os.path.join(root, *p)
        folder = hass.config.path(".storage", "geely_connect", FAKE_VIN)
        os.makedirs(folder)
        entry = types.SimpleNamespace(entry_id="audit", data={
            "vin": FAKE_VIN, "cert_path": os.path.join(folder, "cert.pem")})
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        m._LOGGER.addHandler(handler)
        old_level = m._LOGGER.level
        m._LOGGER.setLevel(logging.INFO)
        try:
            with patch.object(m, "ir", _ir_module()), patch.object(
                m.shutil, "rmtree", side_effect=PermissionError(13, "denied", folder)
            ):
                asyncio.run(m.async_remove_entry(hass, entry))
        finally:
            m._LOGGER.removeHandler(handler)
            m._LOGGER.setLevel(old_level)
        text = stream.getvalue()
        assert "Removed stored" not in text, text
        assert "could not remove" in text, text
        assert FAKE_VIN not in text, text


def test_changed_vin_migration_retains_original_path_until_verified_removal():
    from test_zeekr_config_flow import _flow
    m = _mod()
    with tempfile.TemporaryDirectory() as root:
        old = os.path.join(root, ".storage", "geely_connect", FAKE_VIN)
        os.makedirs(old)
        with open(os.path.join(old, "key.pem"), "w") as f:
            f.write("FAKE KEY")
        entry = types.SimpleNamespace(entry_id="audit", data={"vin": FAKE_VIN,
            "cert_path": os.path.join(old, "cert.pem"), "key_path": os.path.join(old, "key.pem"),
            "email": "test@example.invalid"}, options={})
        cf, flow = _flow([entry])
        flow.hass.config = types.SimpleNamespace(path=lambda *p: os.path.join(root, *p))
        flow._reauth_entry = entry
        flow._email = entry.data["email"]
        flow._user_id = "fake-user"
        flow._zeekr_tokens = ("fake-access", "fake-refresh")
        flow._zeekr_hf_token = "fake-hf"
        result = asyncio.run(flow._finish_zeekr({"vin": "L6T00000000000001"}))
        assert result["reason"] == "reauth_successful"
        assert entry.data["vin"] != FAKE_VIN and "cert_path" not in entry.data
        assert entry.data["legacy_cert_paths"] == [os.path.join(os.path.realpath(old), "cert.pem")]
        assert "legacy_cert_paths" in load("api").redact(entry.data)
        assert FAKE_VIN not in str(load("api").redact(entry.data))
        with patch.object(m, "ir", _ir_module()):
            asyncio.run(m.async_remove_entry(flow.hass, entry))
        assert not os.path.exists(old)


def test_shared_material_survives_removal_of_only_one_entry():
    m = _mod()
    with tempfile.TemporaryDirectory() as root:
        hass = _Hass()
        hass.config.path = lambda *p: os.path.join(root, *p)
        folder = hass.config.path(".storage", "geely_connect", FAKE_VIN)
        os.makedirs(folder)
        entry = types.SimpleNamespace(entry_id="audit", data={"vin": FAKE_VIN})
        other = types.SimpleNamespace(entry_id="other", data={"vin": FAKE_VIN})
        hass.config_entries.async_entries = lambda domain: [entry, other]
        with patch.object(m, "ir", _ir_module()):
            asyncio.run(m.async_remove_entry(hass, entry))
        assert os.path.isdir(folder)


def test_unsafe_storage_references_are_never_retained_or_deleted():
    helpers = load("helpers")
    with tempfile.TemporaryDirectory() as root:
        hass = _Hass()
        hass.config.path = lambda *p: os.path.join(root, *p)
        base = hass.config.path(".storage", "geely_connect")
        os.makedirs(base)
        outside = os.path.join(root, "outside")
        os.makedirs(outside)
        os.symlink(outside, os.path.join(base, FAKE_VIN))
        for path in (os.path.join(outside, "cert.pem"), os.path.join(base, "cert.pem"),
                     os.path.join(base, FAKE_VIN, "cert.pem"), 3, "relative/cert.pem",
                     os.path.join(base, "validname", "unexpected.pem"),
                     os.path.join(base, "short", "cert.pem")):
            assert helpers.legacy_material_dirs(hass, {"legacy_cert_paths": [path]}) == [], path
        assert helpers.legacy_material_dirs(hass, {"vin": "../outside"}) == []


def test_silent_partial_deletion_and_child_missing_never_claim_success():
    m = _mod()
    for error in (None, FileNotFoundError("missing child")):
        with tempfile.TemporaryDirectory() as root:
            hass = _Hass()
            hass.config.path = lambda *p: os.path.join(root, *p)
            folder = hass.config.path(".storage", "geely_connect", FAKE_VIN)
            os.makedirs(folder)
            entry = types.SimpleNamespace(entry_id="audit", data={"vin": FAKE_VIN})
            def incomplete(*args):
                if error:
                    raise error
            with patch.object(m, "ir", _ir_module()), patch.object(m.shutil, "rmtree", incomplete), \
                    patch.object(m._LOGGER, "info") as info, patch.object(m._LOGGER, "warning") as warning:
                asyncio.run(m.async_remove_entry(hass, entry))
            assert not info.called
            assert warning.called


def test_absent_and_multiple_legacy_directories_are_handled_without_guessing():
    m = _mod()
    with tempfile.TemporaryDirectory() as root:
        hass = _Hass()
        hass.config.path = lambda *p: os.path.join(root, *p)
        old = hass.config.path(".storage", "geely_connect", FAKE_VIN)
        current = hass.config.path(".storage", "geely_connect", "L6T00000000000001")
        for directory in (old, current):
            os.makedirs(directory)
        entry = types.SimpleNamespace(entry_id="audit", data={"vin": "L6T00000000000001",
            "legacy_cert_paths": [os.path.join(old, "cert.pem"), os.path.join(old, "cert.pem")]})
        with patch.object(m, "ir", _ir_module()):
            asyncio.run(m.async_remove_entry(hass, entry))
            asyncio.run(m.async_remove_entry(hass, entry))
        assert not os.path.exists(old) and not os.path.exists(current)
