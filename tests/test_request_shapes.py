"""The exact bytes sent to the car.

These bodies were derived from packet captures of the official app, and the
server rejects anything that differs - often with an error that says nothing
useful. Each test here pins a shape that was painful to work out.
"""
import json
import threading

from conftest import FAKE_VIN, load

api = load("api")


def _fake_api(**over):
    a = api.GeelyApi.__new__(api.GeelyApi)
    a.vin = FAKE_VIN
    a.user_id = "1234567"
    a.control_host = "apis.ecloudeu.com"
    a.client_id = "CLIENT"
    a.vehicle_series = a.vehicle_model = "E245-J1"
    a.app_id, a.app_secret = "GEELYE245", "0" * 32
    a.cidpsso_token = "tok"
    a.email = "owner@example.com"
    a.pin_path = None
    a._jwt, a._jwt_exp, a._jwt_lock = "jwt", 2 ** 31, threading.Lock()
    a._jwt_uid = None          # falls back to user_id, as after a fresh login
    a.__dict__.update(over)
    return a


def _capture(a):
    """Record what _authed_apis_call would send, and reply with success."""
    sent = {}

    def fake(method, path, body):
        sent.update(method=method, path=path,
                    body=json.loads(body) if body else None)
        return {"code": 1000, "success": True, "data": {}}

    a._authed_apis_call = fake
    return sent


def test_scheduled_charging_uses_chargeModel_not_rbcModel():
    """The set call must send `chargeModel`.

    The GET echoes `rbcModel`, and sending that back is rejected with
    "illegal request parameter: rbcStartTime must be empty". Upstream shipped a
    fix for exactly this; without a test a refactor can silently undo it.
    """
    a = _fake_api()
    sent = _capture(a)
    a.scheduled_charging_set(command="1", charge_model="2", rbc="1",
                             start_time="23:00", end_time="07:00", rbc_target="80")
    body = sent["body"]
    assert "chargeModel" in body, "must send chargeModel"
    assert "rbcModel" not in body, "rbcModel is the READ key and is rejected on write"
    assert body["bizType"] == "6"
    # With chargeModel present the window must be populated, not empty.
    assert body["rbcStartTime"] == "23:00" and body["rbcEndTime"] == "07:00"


def test_scheduled_charging_posts_to_the_charge_server_path():
    a = _fake_api()
    sent = _capture(a)
    a.scheduled_charging_set(command="0", charge_model="2", rbc="0",
                             start_time="", end_time="", rbc_target="80")
    assert sent["method"] == "POST"
    assert sent["path"].startswith("/charge-server/ecarx_charge_set/")


def test_control_sends_the_telematics_shape():
    a = _fake_api()
    sent = _capture(a)
    a.control("RDL_2", [{"key": "operation", "value": "1"}], "start")
    assert sent["method"] == "PUT"
    assert "/remote-control/vehicle/telematics/" in sent["path"]
    b = sent["body"]
    assert b["serviceId"] == "RDL_2"
    assert b["command"] == "start"
    assert b["creator"] == "tc"
    assert b["serviceParameters"] == [{"key": "operation", "value": "1"}]


def test_position_refresh_is_the_documented_pai_call():
    a = _fake_api()
    sent = _capture(a)
    a.request_position_refresh()
    b = sent["body"]
    assert b["serviceId"] == "PAI"
    params = {p["key"]: p["value"] for p in b["serviceParameters"]}
    assert params == {"operation": "4", "pai": "1"}


def test_every_authenticated_call_goes_through_the_refresh_wrapper():
    """A 1402 must refresh the JWT and retry, not kill the command.

    Opening the Geely app invalidates Home Assistant's JWT. Nine methods used
    to call the transport directly, so that produced a failed command and, for
    control calls, an unnecessary re-auth.
    """
    import ast, io, os
    from conftest import PKG
    tree = ast.parse(io.open(os.path.join(PKG, "api.py"), encoding="utf-8").read())
    offenders = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.FunctionDef):
            continue
        calls = {c.func.attr for c in ast.walk(n)
                 if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)}
        if "_mtls_send" in calls and "_authed_apis_call" not in calls:
            offenders.append(n.name)
    # refresh_jwt performs the refresh itself; _authed_apis_call IS the wrapper.
    assert set(offenders) <= {"refresh_jwt", "_authed_apis_call"}, offenders


def test_control_raises_on_a_rejected_command():
    a = _fake_api()
    a._authed_apis_call = lambda *args: {"code": "8070",
                                         "message": "The last request has not yet been executed"}
    try:
        a.control("RDL_2", [], "start")
    except api.GeelyControlError as e:
        assert e.code == "8070"
        return
    raise AssertionError("a rejected command was reported as success")


def test_a_gateway_ack_is_not_treated_as_execution():
    # code 1000 means the gateway accepted it, not that the car acted.
    a = _fake_api()
    a._authed_apis_call = lambda *args: {"code": 1000, "success": True}
    assert a.control("RDL_2", [], "start")["code"] == 1000


# ------------------------------------------- the compound rapid warm/cool ---
# The body every Rapid Warming / Rapid Cooling press sends, and the one the
# fire_rapid probe varies. A real EX5 echoed this back accepted, seats included,
# and did not act on the seat half (#19) - so the shape is pinned here and the
# encoding question stays open in the issue, not in the code.

def test_the_rapid_body_carries_the_seats_and_the_setpoint():
    a = _fake_api()
    sent = _capture(a)
    a.rapid_climate(ac=True, temp="15.5", heat_seats=None,
                    vent_seats=["11", "19"], vlt=True)
    assert sent["method"] == "POST"
    assert sent["path"] == f"/charge-server/ecarx_charge_set/{FAKE_VIN}"
    body = sent["body"]
    assert body["bizType"] == "7"
    assert body["ac"] == "true" and body["temp"] == "15.5"
    assert body["ventilation"] == [{"level": "3", "pos": "11"},
                                   {"level": "3", "pos": "19"}]
    assert "heat" not in body, "an empty seat list must not travel as a key"
    assert body["vlt"] == "true" and body["vltPos"] == "12"


def test_the_seat_level_applies_to_every_seat_in_the_request():
    a = _fake_api()
    sent = _capture(a)
    a.rapid_climate(ac=True, temp="28.5", heat_seats=["11", "19"],
                    vent_seats=None, vlt=False, level="1")
    assert sent["body"]["heat"] == [{"level": "1", "pos": "11"},
                                    {"level": "1", "pos": "19"}]


def test_the_wheel_travels_as_sw_and_only_when_asked():
    """The captured rapid-warming body carries "sw": "true" (#4). None must
    omit the key entirely, keeping the body byte-identical to the shape a
    real EX5 accepted with the seats in it (#19)."""
    a = _fake_api()
    sent = _capture(a)
    a.rapid_climate(ac=True, temp="28.5", heat_seats=["11", "19"],
                    vent_seats=None, vlt=False, sw=True)
    assert sent["body"]["sw"] == "true"
    a.rapid_climate(ac=True, temp="28.5", heat_seats=None, vent_seats=None,
                    vlt=False, sw=False)
    assert sent["body"]["sw"] == "false"
    a.rapid_climate(ac=True, temp="28.5", heat_seats=None, vent_seats=None,
                    vlt=False)
    assert "sw" not in sent["body"], "no evidence of a wheel must mean no key"


def test_probe_fields_are_merged_last_so_they_can_override():
    """A probe has to be able to add a field with no capture yet - and to
    replace a computed one, which is the only way to test whether the server
    reads it at all."""
    a = _fake_api()
    sent = _capture(a)
    a.rapid_climate(ac=True, temp="28.5", heat_seats=None, vent_seats=None,
                    vlt=False, extra={"bw": "true", "temp": "27.0"})
    assert sent["body"]["bw"] == "true"
    assert sent["body"]["temp"] == "27.0"


# ------------------------------------------------------ the command trail ---
# What the diagnostics report reads. A command rejected because the car was
# still busy is dropped rather than retried and leaves no other trace, unless
# the reporter had debug logging on before it happened - which nobody does.

def test_an_accepted_command_is_recorded_readably():
    a = _fake_api()
    _capture(a)
    a.control("RCE_2", [{"key": "rce.temp", "value": "15.5"}], "start")
    note, = a.command_trail
    assert note["command"] == "RCE_2 start"
    assert note["outcome"] == "accepted" and note["code"] == "1000"
    assert note["detail"] == [{"key": "rce.temp", "value": "15.5"}]
    assert isinstance(note["ms"], int) and note["at"].endswith("Z")


def test_a_rejection_is_recorded_with_the_reason_the_car_gave():
    a = _fake_api()
    a._authed_apis_call = lambda *args: {
        "code": "8070", "message": "The last request has not yet been executed"}
    try:
        a.control("RCE_2", [], "start")
    except api.GeelyControlError:
        pass
    note, = a.command_trail
    assert note["outcome"] == "rejected" and note["code"] == "8070"
    assert "has not yet been executed" in note["message"]


def test_an_expired_session_is_recorded_as_one():
    a = _fake_api()
    a._authed_apis_call = lambda *args: {"code": "60000001"}
    try:
        a.control("RDL_2", [], "start")
    except api.GeelyAuthError:
        pass
    note, = a.command_trail
    assert note["outcome"] == "session-expired"


def test_a_transport_failure_is_recorded_rather_than_lost():
    a = _fake_api()

    def boom(*args):
        raise TimeoutError("read timed out")

    a._authed_apis_call = boom
    try:
        a.control("RDL_2", [], "start")
    except TimeoutError:
        pass
    note, = a.command_trail
    assert note["outcome"] == "error"
    assert "TimeoutError" in note["message"]


def test_a_vin_inside_an_error_message_is_taken_out_of_the_note():
    """The failure paths are exactly where the VIN turns up: a transport error
    quotes the URL it failed on, and every control path has the VIN in it. Both
    redaction passes match key names, so this text would otherwise reach a
    shared diagnostics report intact."""
    a = _fake_api()

    def boom(*args):
        raise OSError(
            "HTTPSConnectionPool(host='apis.ecloudeu.com', port=443): Max "
            f"retries exceeded with url: /remote-control/vehicle/telematics/{FAKE_VIN}")

    a._authed_apis_call = boom
    try:
        a.control("RDL_2", [], "start")
    except OSError:
        pass
    note, = a.command_trail
    assert FAKE_VIN not in note["message"], note["message"]
    assert f"...{FAKE_VIN[-4:]}" in note["message"], "the tail keeps it readable"
    # And the same for a message the server sent back with a rejection.
    a._authed_apis_call = lambda *args: {
        "code": "8070", "message": f"vehicle {FAKE_VIN} is busy"}
    try:
        a.control("RDL_2", [], "start")
    except api.GeelyControlError:
        pass
    assert FAKE_VIN not in json.dumps(list(a.command_trail))


def test_the_trail_is_bounded_and_keeps_the_newest():
    a = _fake_api()
    _capture(a)
    for i in range(api.GeelyApi.COMMAND_TRAIL_SIZE + 5):
        a.control("RCE_2", [{"key": "n", "value": str(i)}], "start")
    assert len(a.command_trail) == api.GeelyApi.COMMAND_TRAIL_SIZE
    assert a.command_trail[-1]["detail"] == [{"key": "n", "value": "29"}]


def test_the_trail_never_records_the_url_because_it_carries_the_vin():
    a = _fake_api()
    _capture(a)
    a.control("RDL_2", [{"key": "door", "value": "all"}], "start")
    a.rapid_climate(ac=True, temp="15.5", heat_seats=None,
                    vent_seats=["11"], vlt=True)
    a.scheduled_charging_set(command="start", start_time="23:00",
                             end_time="05:00")
    blob = json.dumps(list(a.command_trail))
    assert FAKE_VIN not in blob, blob
    assert "/remote-control" not in blob and "/charge-server" not in blob
    # The trail is still worth reading.
    assert [n["command"] for n in a.command_trail] == [
        "RDL_2 start", "rapid_climate", "scheduled_charging start"]
    assert "15.5" in blob and "all" in blob
