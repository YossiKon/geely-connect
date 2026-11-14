"""SEC-5: fake local material only; no account or cloud access."""
import asyncio
import io
import logging
import os
import tempfile
import traceback
import types
from unittest.mock import patch

from conftest import FAKE_VIN, load
from test_init_lifecycle import _Hass, _mod, _ir_module


def test_pin_read_error_masks_the_complete_traceback():
    api = load("api")
    with tempfile.TemporaryDirectory() as root:
        path = os.path.join(root, FAKE_VIN, "server_pins.json")
        os.makedirs(path)
        try:
            api._load_pin_store(path)
        except api.GeelyTLSPinError:
            rendered = traceback.format_exc()
        else:
            raise AssertionError("pin read did not fail closed")
        assert FAKE_VIN not in rendered, rendered
        assert "could not be read" in rendered

