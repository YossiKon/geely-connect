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


def test_both_climate_entries_are_read_because_the_car_splits_them():
    """A real EX5 catalogue (#20) declares climate across two entries, and reading
    only the preferred one lost three flags on every car.

    `remote_climate_control_2` is enabled on every car seen so far, so it was
    always the chosen source - and `steel_wheel_heating`, `AC_step` and
    `window_ventilation_duration` all live in `combined_climate_control`. The
    consequence was that `steering_wheel_heat.enabled` could not be derived
    anywhere, which then got quoted on #4 as evidence that a car did not have the
    feature. The shapes below are that car's, trimmed to the fields at issue."""
    out = cap.parse([
        _entry("remote_climate_control_2", True, valueRange="15.5|28.5",
               showType="support",
               paramsJson=[{"nameKey": "climate_devices", "name": "d",
                            "config": "AC,seat_heat,seat_ventilation,steer_wheel,defrost"}]),
        _entry("combined_climate_control", True, showType="0.5",
               paramsJson=[
                   {"nameKey": "steel_wheel_heating", "name": "w", "config": "true"},
                   {"nameKey": "AC_step", "name": "s", "config": "0.5"},
                   {"nameKey": "window_ventilation", "name": "v", "config": "true"},
                   {"nameKey": "window_ventilation_duration", "name": "t", "config": "60"}]),
    ])
    assert out["steering_wheel_heat.enabled"] is True
    assert out["ac.step"] == 0.5
    assert out["window_vent.duration_s"] == 60
    # And the preferred entry still supplies the range and the device list.
    assert out["ac.min"] == 15.5 and out["ac.max"] == 28.5
    assert out["defrost.enabled"] is True


def test_the_preferred_entry_wins_where_the_two_disagree():
    """Merging must not let the fallback overwrite the entry we chose, or a stale
    duplicate in the other block would quietly change the temperature step."""
    out = cap.parse([
        _entry("remote_climate_control_2", True,
               paramsJson=[{"nameKey": "AC_step", "name": "s", "config": "1.0"}]),
        _entry("combined_climate_control", True,
               paramsJson=[{"nameKey": "AC_step", "name": "s", "config": "0.5"}]),
    ])
    assert out["ac.step"] == 1.0


def test_the_fallback_source_still_supplies_its_own_params():
    """With the preferred entry disabled the fallback is the only source, and it
    must still be read - the merge above must not have made it conditional."""
    out = cap.parse([
        _entry("remote_climate_control_2", False),
        _entry("combined_climate_control", True,
               paramsJson=[{"nameKey": "steel_wheel_heating", "name": "w", "config": "true"},
                           {"nameKey": "ad_temp_range", "name": "r", "config": "16|30"}]),
    ])
    assert out["steering_wheel_heat.enabled"] is True
    assert out["ac.min"] == 16.0 and out["ac.max"] == 30.0


def test_a_starray_catalogue_declares_no_trunk_open_command():
    """The evidence behind #20, pinned so it cannot rot.

    `tailgate.enabled` comes from `remote_control_open_2` alone. Three real
    Starray dumps (#11) lack the flag while a real EX5 carries it, which is how
    we know the two models differ - so this derivation must stay keyed to that
    one entry, and must not start accepting the unlock entry as a substitute."""
    starray = cap.parse([_entry("remote_control_unlock_2", True,
                                valueEnum="door,trunk")])
    assert starray.get("tailgate.enabled") is None
    assert starray["unlock.enabled"] is True
    ex5 = cap.parse([_entry("remote_control_unlock_2", True, valueEnum="door,trunk"),
                     _entry("remote_control_open_2", True, valueEnum="trunk")])
    assert ex5["tailgate.enabled"] is True


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
