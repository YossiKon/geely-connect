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

    `remote_climate_control_2` is enabled on every car seen so far except the
    E2 and the EX2 Max, which declare the 1.0 scheme instead (#72), so it was
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


def test_what_the_unlock_command_accepts_is_read_from_the_catalogue():
    """`valueEnum` went unread until #20, and it is where the boot lives.

    A real EX5 catalogue declares `remote_control_unlock_2` with
    `valueEnum: "door,trunk"` and a `door` param repeating it - which is the
    car's own statement that the unlock command takes the boot as a target,
    and therefore the declaration behind the Unlock Trunk button. It sat in an
    attached report unread while the thread argued about it."""
    out = cap.parse([_entry("remote_control_unlock_2", True, valueEnum="door,trunk",
                            paramsJson=[{"nameKey": "door", "name": "d",
                                         "config": "door,trunk"}])])
    assert out["unlock.targets"] == ["door", "trunk"]


def test_the_two_places_a_target_list_hides_are_merged():
    """Either source alone is enough; a car carrying only one must not read as
    a car that accepts nothing."""
    enum_only = cap.parse([_entry("remote_control_unlock_2", True,
                                  valueEnum="door,trunk")])
    param_only = cap.parse([_entry("remote_control_unlock_2", True,
                                   paramsJson=[{"nameKey": "door", "name": "d",
                                                "config": "door,trunk"}])])
    assert enum_only["unlock.targets"] == param_only["unlock.targets"] == ["door", "trunk"]
    # A list, which is what the EM platform's catalogue hands back.
    as_list = cap.parse([_entry("remote_control_unlock_2", True,
                                valueEnum=["door", "trunk"])])
    assert as_list["unlock.targets"] == ["door", "trunk"]


def test_a_lock_that_takes_only_the_doors_says_so():
    out = cap.parse([_entry("remote_control_unlock_2", True, valueEnum="door")])
    assert out["unlock.targets"] == ["door"]
    assert out.get("tailgate.enabled") is None


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


# ------------------------------------------------- the 1.0 charge scheme ---
#
# Rows copied from two real E2 catalogues (#72) - a South African and a
# Colombian car, 31 entries each, not one of them a `_2` row. The functionIds
# and params are the vendor's own spellings, so a rename on their side would
# fail here rather than silently.

def _e2_remote_charge_1():
    return _entry("remote_charge_1", True,
                  valueEnum="remote_charge_1_chargestart",
                  functionCategory="remote_control",
                  paramsJson=[{"nameKey": "remote_charge_1_chargestarttime",
                               "name": "预约充电",
                               "config": "remote_charge_1_chargestart"}])


def _e2_remote_appointment_charging():
    return _entry("remote_appointment_charging", True,
                  valueEnum="booking_travel_1_batteryheating",
                  functionCategory="remote_control",
                  paramsJson=[{"nameKey": "booking_travel_1_AC",
                               "name": "乘员舱空调", "config": "booking_travel_1_AC"},
                              {"nameKey": "booking_travel_1_batteryheating",
                               "name": "动力电池预热",
                               "config": "booking_travel_1_batteryheating"},
                              {"nameKey": "remote_charge_1_chargestarttime",
                               "name": "预约充电",
                               "config": "remote_charge_1_chargestart"}])


def _ex5_remote_charge_2():
    return _entry("remote_charge_2", True,
                  valueEnum="end_charge_switch,start_charge_switch",
                  paramsJson=[{"nameKey": "battery_type", "name": "电池类型",
                               "config": "Li_iron_phosphate_battery"},
                              {"nameKey": "charging_switch", "name": "充电开关",
                               "config": "end_charge_switch,start_charge_switch"}])


def test_a_1_0_catalogue_has_no_charge_switch_and_its_schedule_in_slot_2():
    """The E2 shape: `remote_charge_1` (a start time, not a switch) and the 1.0
    scheduled-charging params, no `_2` row. The owner confirmed the app has no
    start/stop, so the Charging switch reads False. The schedule is the E2
    app's own in slot 2 (#72), so Scheduled Charging stays, flagged to use it."""
    out = cap.parse([_e2_remote_charge_1(), _e2_remote_appointment_charging(),
                     _entry("honk_flash", True), _entry("door_lock_switch_control", True)])
    assert out["charging.enabled"] is False
    assert out["scheduled_charging.enabled"] is True
    assert out["scheduled_charging.slot_2"] is True
    # The same declaration says this car's connection code 3 means the cable
    # is in, not a charge (#72) - and an EX5's does not.
    assert out["charging.code_3_is_plugged"] is True
    assert "charging.code_3_is_plugged" not in cap.parse([_ex5_remote_charge_2()])
    # And the rest of the car is untouched by the rule.
    assert out["find_car.enabled"] is True and out["lock.enabled"] is True


def test_the_1_0_rule_fires_on_a_declared_row_even_when_it_is_disabled():
    """Declared is the marker, not enabled: a disabled 1.0 charge row is still a
    car that speaks the 1.0 scheme, so neither slot 6 nor a switch on slot 2."""
    out = cap.parse([_entry("remote_charge_1", False,
                            paramsJson=[{"nameKey": "remote_charge_1_chargestarttime",
                                         "config": "remote_charge_1_chargestart"}]),
                     _entry("remote_appointment_charging", False,
                            paramsJson=[{"nameKey": "remote_charge_1_chargestarttime",
                                         "config": "remote_charge_1_chargestart"}])])
    assert out["charging.enabled"] is False
    assert out["scheduled_charging.enabled"] is False
    assert "scheduled_charging.slot_2" not in out


def test_the_2_0_rows_win_when_a_catalogue_carries_both_schemes():
    """An EX5 declares `remote_charge_2`, a bare `remote_appointment_charging`
    and `apt_charging_single_cycle_G2` side by side - and it is the car the
    slot-6 write was verified on. A car that lists both schemes keeps its
    switches."""
    out = cap.parse([_ex5_remote_charge_2(), _e2_remote_charge_1(),
                     _entry("remote_appointment_charging", True),
                     _entry("apt_charging_single_cycle_G2", True),
                     _e2_remote_appointment_charging()])
    assert out["charging.enabled"] is True
    assert out["scheduled_charging.enabled"] is True
    assert "scheduled_charging.slot_2" not in out


def test_a_catalogue_that_names_neither_charge_scheme_stays_permissive():
    """Absence alone is not evidence (#63): a catalogue with no charge row at
    all, or a bare `remote_appointment_charging` with no params, reads exactly
    as it did before this rule existed."""
    out = cap.parse([_entry("honk_flash", True)])
    assert "charging.enabled" not in out
    assert "scheduled_charging.enabled" not in out
    out = cap.parse([_entry("remote_appointment_charging", True)])
    assert "charging.enabled" not in out
    assert out["scheduled_charging.enabled"] is True


def test_the_g2_row_alone_keeps_scheduled_charging_on():
    out = cap.parse([_entry("apt_charging_single_cycle_G2", True)])
    assert out["scheduled_charging.enabled"] is True


def test_a_1_0_catalogue_gets_no_parking_comfort_switch():
    """The E2's app has no Parking Comfort option and its catalogue no row for
    it in either spelling, while the RSM start sent from here sat unexecuted
    for two minutes (#72). Any one of the 1.0 rows marks the scheme; a
    catalogue that names neither scheme stays permissive."""
    for marker in ("remote_charge_1", "remote_lock_unlock", "booking_travel_1"):
        out = cap.parse([_entry(marker, True), _entry("honk_flash", True)])
        assert out["parking_comfort.enabled"] is False, marker
    # The 2.0 row wins when both are declared.
    out = cap.parse([_entry("remote_lock_unlock", True),
                     _entry("parking_comfortable_2", True)])
    assert out["parking_comfort.enabled"] is True
    # No scheme named at all: nothing said, as before.
    out = cap.parse([_entry("honk_flash", True)])
    assert "parking_comfort.enabled" not in out
    assert cap._speaks_1_0({"honk_flash": {}}) is False


def test_the_scheduled_trip_is_the_ex5s_only_where_the_catalogue_says_2_0():
    """set_scheduled_trip sends the body an EX5 owner captured (#4), and an
    EX5 declares the feature as `booking_travel_2`. An E2 declares
    `booking_travel_1` and answers slot 4 in another shape (#72), so the
    service must refuse it rather than fill the blanks with defaults."""
    assert cap.parse([_entry("booking_travel_2", True)])["scheduled_trip.enabled"] is True
    assert cap.parse([_entry("booking_travel_1", True)])["scheduled_trip.enabled"] is False
    both = cap.parse([_entry("booking_travel_1", True), _entry("booking_travel_2", True)])
    assert both["scheduled_trip.enabled"] is True
    assert "scheduled_trip.enabled" not in cap.parse([_entry("honk_flash", True)])


def test_an_e2_unlocks_its_trunk_with_rtu_and_an_ex5_keeps_rdu_2():
    """An E2's app releases the boot with `RTU` (#72), and `RDU_2` unlocks the
    whole of that car whatever its target says. The 1.0 trunk row picks RTU
    only where the 2.0 unlock row is absent, so no EX5 or Starray moves."""
    e2 = cap.parse([_entry("remote_lock_unlock", True), _entry("remote_trunk_unlock", True)])
    assert e2["tailgate.rtu"] is True
    ex5 = cap.parse([_entry("remote_control_unlock_2", True, valueEnum="door,trunk"),
                     _entry("remote_trunk_unlock", True)])
    assert "tailgate.rtu" not in ex5
    assert "tailgate.rtu" not in cap.parse([_entry("remote_trunk_unlock", False)])
    assert "tailgate.rtu" not in cap.parse([_entry("remote_lock_unlock", True)])


def test_the_frunk_is_read_only_where_a_catalogue_names_it():
    """An E2's catalogue names the front trunk as a device of its 1.0 unlock
    row (#72), in both valueEnum and control_devices, as below. That is the
    only declaration of one seen anywhere, so the flag is presence-only: no
    EX5, no Starray and no empty catalogue may get a frunk button."""
    devices = [{"nameKey": "control_devices", "name": "控制设备", "config": "Front_trunk"}]
    e2 = cap.parse([
        _entry("remote_window_open", True, valueEnum="Front_trunk", paramsJson=devices),
        _entry("remote_lock_unlock", True, valueEnum="Front_trunk", paramsJson=devices),
        _entry("remote_trunk_unlock", True),
    ])
    assert e2["frunk.enabled"] is True
    # Either spelling of the declaration is enough on its own.
    assert cap.parse([_entry("remote_lock_unlock", True,
                             valueEnum="Front_trunk")])["frunk.enabled"] is True
    assert cap.parse([_entry("remote_lock_unlock", True,
                             paramsJson=devices)])["frunk.enabled"] is True
    # Never on absence, a disabled row, or the odd window row alone.
    ex5 = cap.parse([_entry("remote_control_unlock_2", True, valueEnum="door,trunk"),
                     _entry("remote_control_open_2", True, valueEnum="trunk")])
    for out in (cap.parse([]), ex5,
                cap.parse([_entry("remote_lock_unlock", True)]),
                cap.parse([_entry("remote_lock_unlock", False, valueEnum="Front_trunk")]),
                cap.parse([_entry("remote_window_open", True, valueEnum="Front_trunk",
                                  paramsJson=devices)])):
        assert "frunk.enabled" not in out, out


def test_an_e2s_ac_row_gets_the_1_0_request_in_whole_degrees():
    """An E2 declares its AC only as `remote_air_condition_switch` (15.5|28.5)
    and its app sends RCE with whole degrees (#72). The 2.0 climate row keeps
    its own request and bounds wherever it is declared."""
    e2 = cap.parse([_entry("remote_air_condition_switch", True, valueRange="15.5|28.5")])
    assert e2["ac.enabled"] is True and e2["ac.rce_1_0"] is True
    assert (e2["ac.min"], e2["ac.max"], e2["ac.step"]) == (16.0, 28.0, 1.0)
    for junk in (None, "", "cold|hot", "15|20|25"):
        out = cap.parse([_entry("remote_air_condition_switch", True, valueRange=junk)])
        assert out["ac.rce_1_0"] is True and "ac.min" not in out, junk
    ex5 = cap.parse([_entry("remote_climate_control_2", True, valueRange="15.5|28.5"),
                     _entry("remote_air_condition_switch", True, valueRange="15.5|28.5")])
    assert "ac.rce_1_0" not in ex5 and ex5["ac.min"] == 15.5
    assert "ac.rce_1_0" not in cap.parse([_entry("remote_air_condition_switch", False)])


def test_an_e2s_window_rows_get_the_1_0_request():
    """An E2 declares its windows as `remote_window_open/close/ventilate`, and
    its app sends RWS with `rws` (#72). A 2.0 window row keeps RWS_2."""
    for row in ("remote_window_open", "remote_window_close", "remote_window_ventilate"):
        assert cap.parse([_entry(row, True)])["windows.rws_1_0"] is True, row
    for row in ("remote_control_window_2", "remote_control_ventilate_2"):
        out = cap.parse([_entry(row, True), _entry("remote_window_ventilate", True)])
        assert "windows.rws_1_0" not in out, row
    assert "windows.rws_1_0" not in cap.parse([_entry("remote_window_open", False)])
