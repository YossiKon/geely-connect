"""New-platform remote-command translation (find, windows, climate, air-clean).

Every mapping here is from a capture of the official app (2026-08-27..29):
the request is watched on the wire, not read off the service names. The
lock/unlock mapping lives in its own change; this covers everything else.
"""
from conftest import load

adapter = load("zeekr_adapter")
tc = adapter._translate_command


def test_find_car_and_lights_pass_through():
    # The app sends RHL unchanged; the value picks the effect.
    assert tc("RHL", "start", [{"key": "rhl", "value": "horn-light-flash"}]) == (
        "RHL", "start", [{"key": "rhl", "value": "horn-light-flash"}])
    assert tc("RHL", "start", [{"key": "rhl", "value": "light-flash"}]) == (
        "RHL", "start", [{"key": "rhl", "value": "light-flash"}])


def test_windows_open_close_and_vent():
    # serviceId RWS (no _2); start = open/down, stop = close/up.
    assert tc("RWS_2", "start", [{"key": "target", "value": "window"}]) == (
        "RWS", "start", [{"key": "target", "value": "window"}])
    assert tc("RWS_2", "stop", [{"key": "target", "value": "window"}]) == (
        "RWS", "stop", [{"key": "target", "value": "window"}])
    assert tc("RWS_2", "start", [{"key": "target", "value": "ventilate"}]) == (
        "RWS", "start", [{"key": "target", "value": "ventilate"}])


def test_steering_wheel_carries_conditioner_5_even_without_a_level():
    # The wheel switch fires with no rce.level; the captured body always
    # carries rce.conditioner=5 on BOTH on and off. Without it the car heats
    # but never turns off (it cannot tell which conditioner to stop).
    on = tc("RCE_2", "start", [{"key": "rce.heat", "value": "steering_wheel"}])
    off = tc("RCE_2", "stop", [{"key": "rce.heat", "value": "steering_wheel"}])
    assert on == ("RCE", "start", [{"key": "rce.heat", "value": "steering_wheel"},
                                   {"key": "rce.conditioner", "value": "5"}])
    assert off == ("RCE", "stop", [{"key": "rce.heat", "value": "steering_wheel"},
                                   {"key": "rce.conditioner", "value": "5"}])


def test_seat_heat_levels_and_off():
    # front-left = driver, front-right = passenger; 1/2/3 = start, 0 = stop;
    # rce.conditioner=3 for seats.
    assert tc("RCE_2", "start", [{"key": "rce.level", "value": "2"},
                                 {"key": "rce.heat", "value": "front-left"}]) == (
        "RCE", "start", [{"key": "rce.heat.front-left", "value": "2"},
                         {"key": "rce.conditioner", "value": "3"}])
    assert tc("RCE_2", "stop", [{"key": "rce.level", "value": "0"},
                                {"key": "rce.heat", "value": "front-right"}]) == (
        "RCE", "stop", [{"key": "rce.heat.front-right", "value": "0"},
                        {"key": "rce.conditioner", "value": "3"}])


def test_defrost_and_ac_pass_their_parameters_through():
    assert tc("RCE_2", "start", [{"key": "rce.conditioner", "value": "2"}]) == (
        "RCE", "start", [{"key": "rce.conditioner", "value": "2"}])
    ac = [{"key": "rce.conditioner", "value": "1"}, {"key": "rce.temp", "value": "22.0"}]
    assert tc("RCE_2", "start", ac) == ("RCE", "start", ac)


def test_air_clean_maps_to_rcc():
    sid, cmd, params = tc("RCC_2", "start", [])
    assert sid == "RCC" and cmd == "start"
    assert {p["key"]: p["value"] for p in params} == {
        "rcc.conditioner": "50", "rcc.ventilation": "0"}


def test_an_uncaptured_service_is_refused():
    assert tc("SOMETHING_NEW", "start", []) is None
