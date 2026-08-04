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


def test_every_vehicle_field_goes_through_the_shared_metadata_shape():
    """The refresh path rebuilt this list by hand and dropped two of five -
    powerType (which decides whether the car gets fuel entities) and colour.

    Asserting the constant set, not a hand-written five, so a sixth vehicle
    field cannot be added to config flow and silently skipped by the refresh."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    from conftest import load
    const = load("const")
    helpers = load("helpers")
    declared = {v for k, v in vars(const).items()
                if k.startswith("CONF_VEHICLE_") and isinstance(v, str)}
    produced = set(helpers.vehicle_metadata({}))
    assert declared == produced, f"missing from vehicle_metadata: {declared - produced}"


def test_the_metadata_shape_reads_every_spelling_the_server_uses():
    if not have_homeassistant():
        skip("homeassistant not installed")
    from conftest import load
    helpers = load("helpers")
    const = load("const")
    full = helpers.vehicle_metadata({
        "nickname": "My P145-J1", "series": "P145-J1", "modelCode": "P145-J1",
        "color": "Blue", "powerType": "\u6df7\u52a8"})
    assert full[const.CONF_VEHICLE_POWER_TYPE] == "\u6df7\u52a8"
    assert full[const.CONF_VEHICLE_COLOR] == "Blue"
    # seriesCode is the older spelling of modelCode; model is the older nickname
    fallbacks = helpers.vehicle_metadata({"model": "EX5", "seriesCode": "FX11"})
    assert fallbacks[const.CONF_VEHICLE_NICKNAME] == "EX5"
    assert fallbacks[const.CONF_VEHICLE_MODEL_CODE] == "FX11"
    # Absent everything is empty, never None - entry data is all strings.
    assert set(helpers.vehicle_metadata({}).values()) == {""}


# ------------------------------------------------- the refetch itself ---
# The tests above pin the *shape*; these pin the one function that applies it.
# The original bug lived here: the refresh hand-built three of the five fields
# and its own guard then locked the entry out of ever acquiring the other two.


def _refetch(entry, vehicles):
    """Run _maybe_refetch_vehicle_metadata against a canned vehicle list,
    counting how many times the cloud would have been called."""
    m = _mod()

    class _ExecHass(_Hass):
        async def async_add_executor_job(self, fn, *a):
            return fn(*a)

    hass = _ExecHass()
    calls = []
    orig = m.geely_api
    m.geely_api = types.SimpleNamespace(
        list_vehicles=lambda *a, **k: (calls.append(1), vehicles)[1])
    try:
        asyncio.run(m._maybe_refetch_vehicle_metadata(hass, entry))
    finally:
        m.geely_api = orig
    return hass, len(calls)


def _record(**over):
    base = {"vin": FAKE_VIN, "nickname": "My EX5", "series": "FX11",
            "modelCode": "FX11", "color": "Silver", "powerType": "混动"}
    base.update(over)
    return base


def test_the_refetch_heals_an_entry_the_old_code_damaged():
    """A half-healed entry - nickname and series present, powerType and
    colour dropped by the old 3-of-5 refresh - must trigger the heal and come
    out carrying every metadata key. Key presence decides, not truthiness."""
    from conftest import load
    const = load("const")
    entry = _Entry(6, data={
        "vin": FAKE_VIN, const.CONF_VEHICLE_NICKNAME: "My EX5",
        const.CONF_VEHICLE_SERIES: "FX11", const.CONF_VEHICLE_MODEL_CODE: "FX11"})
    hass, called = _refetch(entry, [_record()])
    assert called == 1
    assert hass.updated is not None, "the healed entry was never written back"
    helpers = load("helpers")
    missing = set(helpers.vehicle_metadata({})) - set(entry.data)
    assert missing == set(), missing
    assert entry.data[const.CONF_VEHICLE_POWER_TYPE] == "混动"
    assert entry.data[const.CONF_VEHICLE_COLOR] == "Silver"


def test_a_complete_entry_never_logs_in_again():
    """Every key present - even as "" - means no cloud call on boot. The
    backend allows one session per account, so a refetch loop logs the
    owner's phone app out on every restart."""
    _mod()
    from conftest import load
    helpers = load("helpers")
    entry = _Entry(6, data={"vin": FAKE_VIN,
                            **{k: "" for k in helpers.vehicle_metadata({})}})
    _, called = _refetch(entry, [_record()])
    assert called == 0


def test_the_heal_never_downgrades_a_field_the_server_omits_today():
    """A stored powerType must survive a heal run whose vehicle record lacks
    it - powerType decides the entity set, and a transient omission would
    silently flip the car to telemetry-observed classification."""
    from conftest import load
    const = load("const")
    entry = _Entry(6, data={
        "vin": FAKE_VIN, const.CONF_VEHICLE_NICKNAME: "My EX5",
        const.CONF_VEHICLE_SERIES: "FX11", const.CONF_VEHICLE_MODEL_CODE: "FX11",
        const.CONF_VEHICLE_POWER_TYPE: "混动"})
    _, called = _refetch(entry, [_record(powerType=None, color="Red")])
    assert called == 1
    assert entry.data[const.CONF_VEHICLE_POWER_TYPE] == "混动", \
        "the heal cleared a stored powerType"
    assert entry.data[const.CONF_VEHICLE_COLOR] == "Red"
