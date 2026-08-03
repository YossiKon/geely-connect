"""Capability parsing decides which entities exist at all.

If parse() gets this wrong the user either loses a control their car has, or
gets a button that does nothing. The catalog shapes here come from the field
names the module documents.
"""
from conftest import load

cap = load("capabilities")


def _entry(fid, enable=True, **extra):
    e = {"functionId": fid, "valueEnable": enable}
    e.update(extra)
    return e


def test_an_empty_catalog_does_not_crash_or_invent_features():
    out = cap.parse([])
    assert out["raw_count"] == 0
    assert not any(k.endswith(".enabled") and v for k, v in out.items())


def test_malformed_entries_are_ignored_rather_than_fatal():
    for junk in ([{}], [{"functionId": None}], [None] if False else [{"x": 1}]):
        cap.parse(junk)          # must not raise


def test_value_enable_accepts_the_shapes_the_server_actually_sends():
    for truthy in (True, "true", "True", 1, "1"):
        out = cap.parse([_entry("remote_control_lock_2", truthy)])
        assert out.get("lock.enabled"), truthy
    for falsy in (False, "false", 0, "0", None):
        out = cap.parse([_entry("remote_control_lock_2", falsy)])
        assert not out.get("lock.enabled"), falsy


def test_ac_range_is_read_from_value_range():
    out = cap.parse([_entry("remote_climate_control_2", True, valueRange="15.5|28.5")])
    assert out["ac.enabled"] is True
    assert out["ac.min"] == 15.5 and out["ac.max"] == 28.5


def test_a_malformed_ac_range_leaves_the_defaults_alone():
    out = cap.parse([_entry("remote_climate_control_2", True, valueRange="nonsense")])
    assert out["ac.enabled"] is True
    assert "ac.min" not in out and "ac.max" not in out


def test_combined_climate_control_is_the_fallback_source():
    out = cap.parse([_entry("remote_climate_control_2", False),
                     _entry("combined_climate_control", True, valueRange="16|30")])
    assert out["ac.enabled"] is True and out["ac.min"] == 16.0


def test_ac_range_can_come_from_the_params_block():
    out = cap.parse([_entry(
        "remote_climate_control_2", True,
        paramsJson=[{"nameKey": "ad_temp_range", "name": "range", "config": "17|27"},
                    {"nameKey": "AC_step", "name": "step", "config": "0.5"}])])
    assert out["ac.min"] == 17.0 and out["ac.max"] == 27.0
    assert out["ac.step"] == 0.5


def test_disabled_functions_do_not_produce_entities():
    out = cap.parse([_entry("remote_purification", False),
                     _entry("honk_flash", False),
                     _entry("remote_charge_2", False)])
    assert not out.get("gclean.enabled")
    assert not out.get("find_car.enabled")
    assert not out.get("charging.enabled")


def test_raw_count_reflects_the_catalog_size():
    assert cap.parse([_entry("a"), _entry("b"), _entry("c")])["raw_count"] == 3


def test_params_to_dict_skips_incomplete_rows():
    got = cap._params_to_dict({"paramsJson": [
        {"nameKey": "a", "config": "1"},
        {"nameKey": None, "config": "2"},
        {"nameKey": "c"},
    ]})
    assert got == {"a": "1"}


def test_by_id_drops_entries_without_a_function_id():
    got = cap._by_id([{"functionId": "x"}, {"functionId": None}, {}])
    assert set(got) == {"x"}


def test_parse_is_pure():
    """Callers keep the raw catalog; parse must not edit it under them."""
    import copy
    items = [_entry("remote_climate_control_2", True, valueRange="15.5|28.5")]
    before = copy.deepcopy(items)
    cap.parse(items)
    assert items == before
