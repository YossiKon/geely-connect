"""Shared helpers for the geely_connect tests.

The suite runs two ways:

    python tests/run.py        # no pytest, no Home Assistant needed for most
    pytest tests/              # if you have pytest

Tests that need Home Assistant installed skip themselves when it is missing,
so the offline ones still run in a bare checkout.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "custom_components", "geely_connect")


def load(module: str):
    """Import one integration module without importing the whole package.

    custom_components/geely_connect/__init__.py pulls in Home Assistant, which
    the pure-logic tests do not need, so each module is loaded on its own under
    a synthetic package name.
    """
    name = f"gc.{module}"
    if name in sys.modules:
        return sys.modules[name]
    if "gc" not in sys.modules:
        pkg = types.ModuleType("gc")
        pkg.__path__ = [PKG]
        sys.modules["gc"] = pkg
    spec = importlib.util.spec_from_file_location(name, os.path.join(PKG, f"{module}.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def have_homeassistant() -> bool:
    return importlib.util.find_spec("homeassistant") is not None


# A VIN-shaped string that is obviously fake. Never use a real one in tests.
FAKE_VIN = "L6T00000000000000"
