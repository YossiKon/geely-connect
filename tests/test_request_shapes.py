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
