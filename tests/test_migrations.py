"""Config-entry migration, v1 through v6.

Every existing install runs this exactly once on upgrade, and a mistake here
is not recoverable by the user - it either strands the entry on an old version
or deletes entities they had customised. Nothing had exercised it.
"""
import asyncio
import importlib.util
import os
import sys
import types

from conftest import FAKE_VIN, PKG, have_homeassistant
from run import skip


def _mod():
    if not have_homeassistant():
        skip("homeassistant not installed")
    if "gc_init_mig" in sys.modules:
        return sys.modules["gc_init_mig"]
    if "gc" not in sys.modules:
        pkg = types.ModuleType("gc")
        pkg.__path__ = [PKG]
        sys.modules["gc"] = pkg
    spec = importlib.util.spec_from_file_location(
        "gc.__init__", os.path.join(PKG, "__init__.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gc.__init__"] = sys.modules["gc_init_mig"] = mod
    spec.loader.exec_module(mod)
    return mod


class _Entry:
    def __init__(self, version, data=None):
        self.version = version
        self.entry_id = "e1"
        self.domain = "geely_connect"
        self.data = data if data is not None else {"vin": FAKE_VIN}
        self.options: dict = {}


class _Hass:
    """Records what the migration did, without a real Home Assistant."""

    def __init__(self):
        self.updated = None
        self.data: dict = {}

        outer = self

        class _CE:
            @staticmethod
            def async_update_entry(entry, data=None, version=None, **kw):
                outer.updated = {"data": data, "version": version}
                if data is not None:
                    entry.data = data
                if version is not None:
                    entry.version = version

        self.config_entries = _CE()


def _migrate(entry):
    """Run the migration with the registry helpers stubbed out."""
    m = _mod()
    hass = _Hass()
    calls = []
    orig = (m._reenable_integration_disabled_entities, m._purge_raw_exposure_entities)
    m._reenable_integration_disabled_entities = lambda h, e: calls.append("reenable")
    m._purge_raw_exposure_entities = lambda h, e: calls.append("purge")
    try:
        ok = asyncio.run(m.async_migrate_entry(hass, entry))
    finally:
        (m._reenable_integration_disabled_entities,
         m._purge_raw_exposure_entities) = orig
    return ok, hass, calls


def test_every_old_version_reaches_the_current_one():
    for v in (1, 2, 3, 4, 5):
        entry = _Entry(v)
        ok, hass, _ = _migrate(entry)
        assert ok is True, f"v{v} migration returned {ok}"
        assert entry.version == 6, f"v{v} ended on v{entry.version}"


def test_a_v1_entry_gains_an_install_fingerprint():
    """Without idfa/idfv every login looks like a new device, and Geely allows
    one session per account - so it signs the owner's phone app out."""
    entry = _Entry(1, {"vin": FAKE_VIN})
    _migrate(entry)
    assert entry.data.get("device_idfa"), "no idfa generated"
    assert entry.data.get("device_idfv"), "no idfv generated"
    assert entry.data["device_idfa"] != entry.data["device_idfv"]


def test_an_existing_fingerprint_is_never_regenerated():
    """Replacing it would be the exact logout the v1 migration exists to stop."""
    entry = _Entry(1, {"vin": FAKE_VIN, "device_idfa": "KEEP-A", "device_idfv": "KEEP-V"})
    _migrate(entry)
    assert entry.data["device_idfa"] == "KEEP-A"
    assert entry.data["device_idfv"] == "KEEP-V"


def test_versions_above_one_do_not_get_a_new_fingerprint():
    entry = _Entry(3, {"vin": FAKE_VIN})
    _migrate(entry)
    assert "device_idfa" not in entry.data


def test_migration_preserves_everything_already_in_the_entry():
    data = {"vin": FAKE_VIN, "email": "owner@example.com", "user_id": "1",
            "cert_path": "/c/cert.pem", "key_path": "/c/key.pem",
            "poll_mode": "manual", "pressure_unit": "bar", "region": "APAC"}
    entry = _Entry(2, dict(data))
    _migrate(entry)
    for k, v in data.items():
        assert entry.data[k] == v, f"{k} was lost or changed"


def test_an_already_current_entry_is_left_completely_alone():
    entry = _Entry(6, {"vin": FAKE_VIN})
    ok, hass, calls = _migrate(entry)
    assert ok is True
    assert hass.updated is None, "rewrote an entry that needed no migration"
    assert calls == [], f"touched the registry unnecessarily: {calls}"


def test_a_future_version_is_not_downgraded():
    """If a user rolls back the integration, do not rewrite their entry."""
    entry = _Entry(7, {"vin": FAKE_VIN})
    ok, hass, _ = _migrate(entry)
    assert ok is True and entry.version == 7
    assert hass.updated is None


def test_the_registry_cleanup_runs_for_every_upgrade_path():
    for v in (1, 2, 3, 4, 5):
        _, _, calls = _migrate(_Entry(v))
        assert "reenable" in calls, f"v{v} skipped re-enabling entities"
        assert "purge" in calls, f"v{v} skipped the raw-exposure purge"


def test_migration_is_idempotent():
    """Running it twice must not double-apply anything."""
    entry = _Entry(1, {"vin": FAKE_VIN})
    _migrate(entry)
    first = dict(entry.data)
    _migrate(entry)
    assert entry.data == first
    assert entry.version == 6


def test_the_flow_version_and_the_migration_target_agree():
    """A VERSION bump without extending the migration strands every install."""
    m = _mod()
    import io
    src = io.open(os.path.join(PKG, "__init__.py"), encoding="utf-8").read()
    cf_src = io.open(os.path.join(PKG, "config_flow.py"), encoding="utf-8").read()
    import re
    flow_version = int(re.search(r"VERSION\s*=\s*(\d+)", cf_src).group(1))
    assert f"version={flow_version}" in src, (
        f"config flow is at v{flow_version} but the migration does not target it"
    )
    assert f"entry.version >= {flow_version}" in src, (
        f"the early-return does not match v{flow_version}"
    )
