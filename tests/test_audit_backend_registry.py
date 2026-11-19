"""ARCH-2: preserve customized registry rows, including ambiguous collisions."""
import types
import asyncio
from unittest.mock import patch

from conftest import FAKE_VIN
from test_init_lifecycle import _mod


class _Registry:
    def __init__(self, rows):
        self.entities = {row.entity_id: row for row in rows}
        self.updated = []
    def async_remove(self, entity_id):
        raise AssertionError("a history-bearing row was deleted")
    def async_get_entity_id(self, domain, platform, unique_id):
        return next((row.entity_id for row in self.entities.values()
                     if row.entity_id.split(".")[0] == domain and row.platform == platform
                     and row.unique_id == unique_id), None)
    def async_update_entity(self, entity_id, **kw):
        self.updated.append((entity_id, kw))
        row = self.entities[entity_id]
        for key, value in kw.items():
            setattr(row, "unique_id" if key == "new_unique_id" else key, value)


def _row(key, *, domain="sensor", owner="audit", entity_id=None, platform="geely_connect"):
    return types.SimpleNamespace(entity_id=entity_id or f"{domain}.custom_{key}",
        platform=platform, config_entry_id=owner, unique_id=f"geely_{FAKE_VIN}_{key}",
        disabled_by=None, options={"user": True})


def test_legacy_rename_keeps_entity_id_and_all_user_settings():
    m = _mod()
    old = types.SimpleNamespace(entity_id="sensor.family_distance", platform="geely_connect",
        config_entry_id="audit", unique_id=f"geely_{FAKE_VIN}_odometer",
        name="Family mileage", disabled_by="user", area_id="garage", options={"sensor": {"precision": 2}})
    class Registry:
        entities = {old.entity_id: old}
        def async_remove(self, entity_id):
            raise AssertionError("history-bearing registry entry removed")
        def async_update_entity(self, entity_id, **kw):
            assert entity_id == old.entity_id
            for k, v in kw.items():
                setattr(old, "unique_id" if k == "new_unique_id" else k, v)
        def async_get_entity_id(self, domain, platform, unique_id):
            return next((e.entity_id for e in self.entities.values()
                         if e.unique_id == unique_id and e.platform == platform
                         and e.entity_id.split(".")[0] == domain), None)
    reg = Registry()
    entry = types.SimpleNamespace(entry_id="audit", data={"vin": FAKE_VIN})
    with patch.object(m.er, "async_get", return_value=reg):
        m._purge_obsolete_entities(object(), entry)
    assert old.unique_id == f"geely_{FAKE_VIN}_total_mileage"
    assert old.entity_id == "sensor.family_distance"
    assert old.disabled_by == "user" and old.name == "Family mileage"
    assert old.area_id == "garage" and old.options == {"sensor": {"precision": 2}}


def test_all_documented_renames_migrate_once_and_preserve_references():
    m = _mod()
    rows = [_row(key, domain=domain) for domain, mapping in m._LEGACY_ENTITY_KEYS.items()
            for key in mapping]
    reg = _Registry(rows)
    entity_ids = list(reg.entities)
    entry = types.SimpleNamespace(entry_id="audit", data={"vin": FAKE_VIN})
    with patch.object(m.er, "async_get", return_value=reg):
        assert m._purge_obsolete_entities(object(), entry) == 9
        assert m._purge_obsolete_entities(object(), entry) == 0
    assert list(reg.entities) == entity_ids
    assert all(row.options == {"user": True} for row in rows)
    assert len(reg.updated) == 9


def test_collisions_orphans_siblings_and_retired_features_are_kept():
    m = _mod()
    rows = [_row("odometer"), _row("total_mileage"),
            _row("tyre_pressure_fl", owner="other"),
            _row("tyre_pressure_fr", owner=None), _row("gear"),
            _row("tyre_pressure_rl", platform="another_integration")]
    reg = _Registry(rows)
    entry = types.SimpleNamespace(entry_id="audit", data={"vin": FAKE_VIN})
    with patch.object(m.er, "async_get", return_value=reg), patch.object(m._LOGGER, "warning") as warning:
        assert m._purge_obsolete_entities(object(), entry) == 0
    assert warning.call_count == 3
    assert len(reg.entities) == len(rows) and reg.updated == []


def test_unsafe_vin_does_not_guess_registry_ownership():
    m = _mod()
    reg = _Registry([_row("odometer")])
    with patch.object(m.er, "async_get", return_value=reg):
        assert m._purge_obsolete_entities(object(), types.SimpleNamespace(
            entry_id="audit", data={"vin": "../other"})) == 0
    assert not reg.updated


def test_retired_raw_and_unserved_rows_keep_user_settings_and_history():
    m = _mod()
    rows = [_row("raw_example"), _row("time_scheduled_charging_start", domain="time")]
    reg = _Registry(rows)
    entry = types.SimpleNamespace(entry_id="audit")
    with patch.object(m.er, "async_get", return_value=reg), patch.object(
            m.er, "async_entries_for_config_entry", return_value=rows):
        assert m._purge_raw_exposure_entities(object(), entry) == 1
        assert m._purge_unserved_entities(object(), entry,
            types.SimpleNamespace(serves_charge_server=False)) == 1
    assert len(reg.entities) == 2 and not reg.updated


def test_versioned_migration_is_idempotent_including_legacy_entry_metadata():
    from test_migrations import _Hass, _Entry
    m = _mod()
    for version in range(1, 8):
        reg = _Registry([_row("odometer", owner="e1")])
        hass = _Hass()
        entry = _Entry(version, {"vin": FAKE_VIN, "custom": "keep"})
        with patch.object(m.er, "async_get", return_value=reg), patch.object(
            m.er, "async_entries_for_config_entry", return_value=[]):
            assert asyncio.run(m.async_migrate_entry(hass, entry))
            assert entry.version == 7 and entry.data["custom"] == "keep"
            assert asyncio.run(m.async_migrate_entry(hass, entry))
        assert len(reg.updated) == (0 if version == 7 else 1)
