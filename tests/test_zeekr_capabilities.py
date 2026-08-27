"""Translating the new platform's capability catalogue into the old shape.

Rows here are copied from a real catalogue (Geely EX2, 74 rows, 2026-08-28).
The vendor ships a `functionName` label with every row, which is what makes
the service-shaped codes readable at all:

    C_RDU_2_2  远程解锁-控制设备_后备箱   remote unlock, control device: tailgate
    C_RWS_1    远程一键透气_远程车窗微开  one-touch ventilate: window slightly open
    C_PAA_6    …PAA是否支持方向盘加热     steering-wheel heating supported
"""
from __future__ import annotations

from conftest import load

adapter = load("zeekr_adapter")
capabilities = load("capabilities")


def _row(code, use="Y", category="remote_control", param=None):
    return {"functionCategory": category, "functionCode": code,
            "functionName": "", "paramCode": param, "paramName": None,
            "paramValueCode": None, "paramValueName": None,
            "paramValueUse": use, "logicSymbol": None, "dataType": None}


REAL = [
    _row("remote_climate_control", param="control_device"),
    _row("remote_control_lock_2", param="control_device"),
    _row("remote_control_unlock_2", param="control_device"),
    _row("remote_charge_2", param="battery_type"),
    _row("remote_window_ventilate"),
    _row("parking_comfortable_2"),
    _row("honk_flash"),
    _row("C_RDU_2_1"), _row("C_RDU_2_2"), _row("C_RDU_2_3"),
    _row("C_RWS_1"), _row("C_RWS_1_5"),
    _row("C_PAA_1"), _row("C_PAA_5_1"), _row("C_PAA_5_2"),
    _row("C_PAA_6"), _row("C_PAA_9", use="1,2,3"),
    _row("C_RHL_1"), _row("C_RHL_2"), _row("C_RHL_3"),
    _row("V_RVS_8", category="remote_vehicle_status"),
    _row("V_RVS_8_1", category="remote_vehicle_status"),
    _row("sunroof_automatic_close", category="message_box",
         param="notification_method"),
]


def _view(rows):
    return capabilities.parse(adapter.translate_capabilities(rows))


def test_the_features_this_car_has_survive_translation():
    view = _view(REAL)
    for flag in ("ac.enabled", "defrost.enabled", "charging.enabled",
                 "find_car.enabled", "lock.enabled", "unlock.enabled",
                 "tailgate.enabled", "window_vent.enabled", "windows.enabled",
                 "seat.heat.enabled", "steering_wheel_heat.enabled",
                 "parking_comfort.enabled"):
        assert view.get(flag) is True, f"{flag} was lost in translation"


def test_seat_positions_and_unlock_targets_come_from_the_codes():
    view = _view(REAL)
    assert view["seat.heat.positions"] == ["front-left", "front-right"]
    assert set(view["unlock.targets"]) == {"door", "trunk", "hood"}


def test_a_notification_row_is_not_a_feature():
    """`sunroof_automatic_close` is a message_box preference, not a sunroof."""
    view = _view(REAL)
    assert "sunroof.enabled" not in view
    assert "sunshade.enabled" not in view


def test_defrost_is_asserted_because_the_service_cannot_be_asked():
    """The climate entry is labelled 空调服务不区分命令 - the AC service does
    not distinguish commands - so the catalogue cannot say whether defrost
    exists. Asserting it keeps a working control; reading the silence as "no
    defrost" would remove one."""
    view = _view([_row("remote_climate_control")])
    assert view.get("defrost.enabled") is True
    assert view.get("ac.enabled") is True


def test_disabled_rows_do_not_enable_anything():
    for flag in ("N", "0", "", "false"):
        rows = [_row("remote_charge_2", use=flag), _row("honk_flash", use=flag)]
        assert adapter.translate_capabilities(rows) == []


def test_an_empty_or_unknown_catalogue_stays_permissive():
    """Returning [] makes capabilities.py fall back to the all-features view,
    so a car whose catalogue we cannot read keeps every entity it has today."""
    assert adapter.translate_capabilities([]) == []
    assert adapter.translate_capabilities([_row("some_code_we_have_never_seen")]) == []


def test_wheel_heat_needs_its_own_code():
    without = _view([_row("remote_climate_control"), _row("C_PAA_5_1")])
    assert "steering_wheel_heat.enabled" not in without
    with_it = _view([_row("remote_climate_control"), _row("C_PAA_6")])
    assert with_it.get("steering_wheel_heat.enabled") is True
