"""Shared helpers for the geely_connect tests.

The suite runs two ways:

    python tests/run.py        # no pytest, no Home Assistant needed for most
    pytest tests/              # if you have pytest

Tests that need Home Assistant installed skip themselves when it is missing,
so the offline ones still run in a bare checkout.
"""
from __future__ import annotations

import dataclasses
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
    # Home Assistant first, as in production: see have_homeassistant().
    have_homeassistant()
    name = f"gc.{module}"
    if name in sys.modules:
        return sys.modules[name]
    if "gc" not in sys.modules:
        pkg = types.ModuleType("gc")
        pkg.__path__ = [PKG]
        sys.modules["gc"] = pkg
    spec = importlib.util.spec_from_file_location(name, os.path.join(PKG, f"{module}.py"))
    mod = importlib.util.module_from_spec(spec)
    # Register before exec (circular imports), but drop the entry if the
    # import fails: a half-initialized module left in sys.modules makes every
    # later test report a misleading AttributeError instead of the real error.
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return mod


def have_homeassistant() -> bool:
    """Whether Home Assistant is installed - and, when it is, imported.

    From 2026.9, Home Assistant replaces voluptuous with probatio when the
    homeassistant package is imported (install_as_voluptuous() in its
    __init__.py), and in production that import always comes before any
    integration's. A module the suite loads earlier keeps the real
    voluptuous's classes while every later `import voluptuous` gets
    probatio's, so a schema check would depend on which test file ran first.
    Every loader asks this before it runs a module, so importing the package
    here keeps the production order."""
    if importlib.util.find_spec("homeassistant") is None:
        return False
    import homeassistant  # noqa: F401
    return True


# A VIN-shaped string that is obviously fake. Never use a real one in tests.
FAKE_VIN = "L6T00000000000000"


def picker_key(flow, vin):
    """The key a config flow's vehicle picker offered for the car with this
    VIN, as a user's choice in the form would send it: the picker's values
    are opaque keys, not VINs."""
    for key, record in flow._offered.items():
        if record.get("vin") == vin:
            return key
    raise AssertionError(f"the picker offered no car with VIN ...{vin[-4:]}")


def device_field(info, key):
    """One field of an entity's DeviceInfo, raising KeyError for a field the
    entity left unset, as reading the dict does.

    In the Home Assistant the gate installs, DeviceInfo is a TypedDict, so a
    plain dict. Newer releases make it a dataclass, where an unset field
    holds its default sentinel instead of being absent, and where reading it
    as a dict logs a deprecation warning: 2026.12.0b0 does, and the warning
    says 2027.12 stops allowing it."""
    if isinstance(info, dict):
        return info[key]
    value = getattr(info, key)
    unset = next(f.default for f in dataclasses.fields(info) if f.name == key)
    if value is unset:
        raise KeyError(key)
    return value
