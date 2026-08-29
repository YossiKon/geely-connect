"""New-platform vehicle reads: the ms-app-bff garage and the ms-vehicle-status
live read, plus the payload-nesting fix that makes them usable.

Every assertion here is against a capture of the official app (2026-08-27,
AU account, EX5 on the new EM platform) rather than an assumption:

  * the status route is /ms-vehicle-status/api/v1.0/vehicle/status/latest with
    query latest=false&target=new,
  * the vehicle is addressed by the opaque `x-vin` token, not the plain VIN,
  * the nonce on this gateway is a UUID and it is inside the signed canonical,
  * and the payload omits the old platform's "vehicleStatus" wrapper.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from conftest import FAKE_VIN, have_homeassistant, load
from run import skip

zc = load("zeekr_client")
adapter = load("zeekr_adapter")

_STATUS_DATA = {
    "basicVehicleStatus": {"engineStatus": "engine-off", "speed": 0},
    "additionalVehicleStatus": {
        "electricVehicleStatus": {"chargeLevel": "92.0",
                                  "distanceToEmptyOnBatteryOnly": "270"},
        "drivingSafetyStatus": {"centralLockingStatus": "1"},
    },
    "updateTime": 1787817174561,
}
_LIST_DATA = [{"vin": FAKE_VIN, "nickName": "EX5", "tboxPlatform": "1"}]
_CAP_DATA = [
    {"functionCategory": "remote_control", "functionCode": "honk_flash",
     "functionName": "远程闪灯鸣笛", "paramValueUse": "Y"},
    "not a row",
]


def _start_gateway(seen: list[dict]):
    """A gateway that records what it was sent and verifies the snc signature."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # noqa: D102 - silence the test server
            pass

        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's spelling
            path, _, query = self.path.partition("?")
            headers = {k.lower(): v for k, v in self.headers.items()}
            want = zc.snc_sign(
                method="GET",
                url=f"http://127.0.0.1:{self.server.server_address[1]}{self.path}",
                headers=dict(self.headers), body=b"")
            seen.append({
                "path": path,
                "query": query,
                "authorization": headers.get("authorization", ""),
                "x_vin": headers.get("x-vin"),
                "nonce": headers.get("x-api-signature-nonce", ""),
                "signature_ok": headers.get("x-signature") == want,
            })
            if "ms-vehicle-status" in path:
                data = _STATUS_DATA
            elif "ms-vehicle-capability" in path:
                data = _CAP_DATA
            else:
                data = _LIST_DATA
            self._reply({"code": "000000", "msg": "ok", "data": data})

        def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler's spelling
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length)
            headers = {k.lower(): v for k, v in self.headers.items()}
            want = zc.snc_sign(
                method="POST",
                url=f"http://127.0.0.1:{self.server.server_address[1]}{self.path}",
                headers=dict(self.headers), body=body)
            seen.append({
                "path": self.path,
                "body": json.loads(body or b"{}"),
                "x_vin": headers.get("x-vin"),
                "signature_ok": headers.get("x-signature") == want,
            })
            self._reply({"code": "000000", "msg": "ok",
                         "data": {"sessionId": "abc123"}})

        def _reply(self, body):
            raw = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _client(srv) -> object:
    c = zc.ZeekrClient(email="", password="",
                       gateway=f"http://127.0.0.1:{srv.server_address[1]}")
    c.access_token = "test-access-token"
    c.user_id = "u123"
    return c


def test_status_read_matches_the_captured_request():
    seen: list[dict] = []
    srv = _start_gateway(seen)
    try:
        c = _client(srv)
        c.enc_vin = "ENC-VIN-TOKEN=="
        resp = c.vehicle_status_new_resp()
    finally:
        srv.shutdown()

    sent = seen[-1]
    assert sent["path"] == "/ms-vehicle-status/api/v1.0/vehicle/status/latest"
    assert sent["query"] == "latest=false&target=new"
    # The bare token is what this gateway accepts; "Bearer <token>" is
    # rejected with 079020 Invalid token.
    assert sent["authorization"] == "test-access-token"
    assert sent["x_vin"] == "ENC-VIN-TOKEN=="
    assert len(sent["nonce"]) == 36 and sent["nonce"].count("-") == 4
    assert sent["signature_ok"], "x-vin/nonce must be inside the signed canonical"
    assert resp["data"]["basicVehicleStatus"]["engineStatus"] == "engine-off"


def test_status_read_needs_the_vehicle_token():
    seen: list[dict] = []
    srv = _start_gateway(seen)
    try:
        c = _client(srv)
        try:
            c.vehicle_status_new_resp()
        except zc.ZeekrAuthError:
            pass
        else:  # pragma: no cover - only reached on a regression
            raise AssertionError("expected ZeekrAuthError without enc_vin")
    finally:
        srv.shutdown()
    assert not seen, "must not reach the gateway without an x-vin token"


def test_bff_garage_returns_records():
    seen: list[dict] = []
    srv = _start_gateway(seen)
    try:
        records = _client(srv).list_vehicles_bff()
    finally:
        srv.shutdown()
    assert [r["vin"] for r in records] == [FAKE_VIN]
    assert seen[-1]["path"] == "/ms-app-bff/api/v4.0/veh/vehicle-list"
    assert seen[-1]["query"] == "needSharedCar=true"


def test_wrapper_restores_the_old_platform_nesting():
    wrapped = adapter._wrap_vehicle_status(_STATUS_DATA)
    basic = wrapped["vehicleStatus"]["basicVehicleStatus"]
    ev = (wrapped["vehicleStatus"]["additionalVehicleStatus"]
          ["electricVehicleStatus"])
    assert basic["engineStatus"] == "engine-off"
    assert ev["chargeLevel"] == "92.0"
    # Moved, not copied: a second path to the same field would become a second
    # raw diagnostic entity under full exposure.
    assert "updateTime" not in wrapped
    # It lives inside vehicleStatus, where Car Reported At walks
    # for it (vehicleStatus.updateTime). Without this the staleness sensor is
    # blank on every new-platform car - #24's failure with the one indicator
    # of it switched off (#53).
    assert wrapped["vehicleStatus"]["updateTime"] == 1787817174561


def test_the_staleness_sensor_reads_the_wrapped_timestamp():
    """End to end: the car's own stamp reaches Car Reported At after wrapping.
    The sensor walks vehicleStatus.updateTime; a new-platform payload only has
    it top-level until the wrapper carries it in."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    import datetime
    sensor = load("sensor")
    wrapped = adapter._wrap_vehicle_status(_STATUS_DATA)
    at = sensor._reported_at(wrapped)
    assert isinstance(at, datetime.datetime), at
    # 1787817174561 ms -> a real 2026 instant, not None.
    assert at.year == 2026, at


def test_wrapper_leaves_everything_else_alone():
    old_shape = {"vehicleStatus": {"basicVehicleStatus": {}}, "updateTime": 1}
    assert adapter._wrap_vehicle_status(old_shape) is old_shape
    assert adapter._wrap_vehicle_status({"a": 1}) == {"a": 1}
    assert adapter._wrap_vehicle_status(None) is None
    assert adapter._wrap_vehicle_status("not a dict") == "not a dict"


def test_capability_catalogue_is_read_with_no_query():
    """The vehicle comes from the x-vin header, so the route takes no query at
    all - asking it with ?vin= is what made this endpoint look unreachable."""
    seen: list[dict] = []
    srv = _start_gateway(seen)
    try:
        c = _client(srv)
        c.enc_vin = "ENC-VIN-TOKEN=="
        rows = c.capabilities_new()
    finally:
        srv.shutdown()
    sent = seen[-1]
    assert sent["path"] == (
        "/ms-vehicle-capability/api/v1.0/vehicle/function/model/info")
    assert sent["query"] == ""
    assert sent["x_vin"] == "ENC-VIN-TOKEN=="
    assert sent["signature_ok"]
    # Non-dict entries in the list are dropped rather than handed on.
    assert [r["functionCode"] for r in rows] == ["honk_flash"]


def test_a_catalogue_that_is_not_a_list_reads_as_empty():
    """A gateway answering with something other than a list of rows leaves the
    catalogue empty, which capabilities.py reads as the permissive all-features
    view rather than as a car with no features."""
    c = zc.ZeekrClient(email="", password="", gateway="https://unused.invalid")
    c.access_token = "test-access-token"
    c.enc_vin = "ENC-VIN-TOKEN=="
    c._request = lambda *a, **k: {"data": {"unexpected": "shape"}}
    assert c.capabilities_new() == []


def test_the_catalogue_refuses_to_run_unauthenticated():
    """Both guards: no session, and no vehicle token."""
    seen: list[dict] = []
    srv = _start_gateway(seen)
    try:
        no_session = _client(srv)
        no_session.access_token = ""
        no_session.enc_vin = "ENC-VIN-TOKEN=="
        no_token = _client(srv)          # authenticated, but no x-vin
        for c in (no_session, no_token):
            try:
                c.capabilities_new()
            except zc.ZeekrAuthError:
                pass
            else:  # pragma: no cover - only reached on a regression
                raise AssertionError("the catalogue ran without credentials")
    finally:
        srv.shutdown()
    assert not seen, "must not reach the gateway without credentials"


def test_bff_garage_guards_and_shapes():
    """The edge paths of the new-platform garage read: it refuses without a
    session, and it unwraps a dict-shaped reply (some gateways return the
    records under a `list`/`records` key rather than as the top-level array)."""
    c = zc.ZeekrClient(email="", password="", gateway="https://unused.invalid")

    # No session -> refuse before any request.
    c.access_token = ""
    try:
        c.list_vehicles_bff()
    except zc.ZeekrAuthError:
        pass
    else:  # pragma: no cover - only on a regression
        raise AssertionError("bff read ran without a session")

    c.access_token = "test-access-token"
    # Dict-wrapped: records under a key.
    c._request = lambda *a, **k: {"data": {"list": [{"vin": FAKE_VIN}, "junk"]}}
    assert c.list_vehicles_bff() == [{"vin": FAKE_VIN}], "dict-wrapped list not unwrapped"
    # A dict with no recognised key -> empty, not an error.
    c._request = lambda *a, **k: {"data": {"nothing": "useful"}}
    assert c.list_vehicles_bff() == []


def test_status_read_refuses_without_a_session():
    """The other guard on the live read - no access token means no request,
    the pair to the missing-x-vin guard already covered."""
    c = zc.ZeekrClient(email="", password="", gateway="https://unused.invalid")
    c.access_token = ""
    c.enc_vin = "ENC-VIN-TOKEN=="
    try:
        c.vehicle_status_new_resp()
    except zc.ZeekrAuthError:
        pass
    else:  # pragma: no cover - only on a regression
        raise AssertionError("status read ran without a session")


def test_the_control_route_sends_the_captured_envelope():
    """control_new_resp is the shared transport for every new-platform command:
    POST /ms-remote-control/v1.0/remoteControl/control, no /api/ segment,
    serviceParameters nested under `setting`, vehicle addressed by x-vin, and
    the body inside the signed canonical."""
    seen: list[dict] = []
    srv = _start_gateway(seen)
    try:
        c = _client(srv)
        c.enc_vin = "ENC-VIN-TOKEN=="
        resp = c.control_new_resp("RDL", "start",
                                  [{"key": "door", "value": "all"}])
    finally:
        srv.shutdown()
    sent = seen[-1]
    assert sent["path"] == "/ms-remote-control/v1.0/remoteControl/control"
    assert sent["body"] == {
        "command": "start", "serviceId": "RDL",
        "setting": {"serviceParameters": [{"key": "door", "value": "all"}]}}
    assert sent["x_vin"] == "ENC-VIN-TOKEN=="
    assert sent["signature_ok"], "the body must be inside the signed canonical"
    assert resp["data"]["sessionId"] == "abc123"


def test_a_command_with_no_parameters_still_sends_the_list():
    seen: list[dict] = []
    srv = _start_gateway(seen)
    try:
        c = _client(srv)
        c.enc_vin = "ENC-VIN-TOKEN=="
        c.control_new_resp("RHL", "start")
    finally:
        srv.shutdown()
    assert seen[-1]["body"]["setting"] == {"serviceParameters": []}


def test_the_control_route_refuses_to_run_unauthenticated():
    """A command must never leave without both a session and a vehicle token."""
    seen: list[dict] = []
    srv = _start_gateway(seen)
    try:
        no_session = _client(srv)
        no_session.access_token = ""
        no_session.enc_vin = "ENC-VIN-TOKEN=="
        no_token = _client(srv)          # authenticated, but no x-vin
        for c in (no_session, no_token):
            try:
                c.control_new_resp("RDL", "start")
            except zc.ZeekrAuthError:
                pass
            else:  # pragma: no cover - only reached on a regression
                raise AssertionError("a command ran without credentials")
    finally:
        srv.shutdown()
    assert not seen, "must not reach the gateway without credentials"


def test_the_position_wake_sends_the_captured_pai_request():
    """request_position_refresh is the whole point of the transport here: the
    map-open locator, serviceId PAI with a single pai=1 parameter (the legacy
    operation=4 is rejected on this gateway). It acquires a position; it cannot
    move the car."""
    seen: list[dict] = []
    srv = _start_gateway(seen)
    try:
        c = _client(srv)
        c.enc_vin = "ENC-VIN-TOKEN=="
        c.control_new_resp("PAI", "start", [{"key": "pai", "value": "1"}])
    finally:
        srv.shutdown()
    sent = seen[-1]
    assert sent["path"] == "/ms-remote-control/v1.0/remoteControl/control"
    assert sent["body"] == {
        "command": "start", "serviceId": "PAI",
        "setting": {"serviceParameters": [{"key": "pai", "value": "1"}]}}
    assert sent["signature_ok"]


def test_set_smart_temp_sends_the_paa_body_to_its_own_endpoint():
    """Rapid climate is a separate route from /control - setSmartTemp with
    serviceId PAA and an object setting - addressed by x-vin, body signed."""
    seen: list[dict] = []
    srv = _start_gateway(seen)
    try:
        c = _client(srv)
        c.enc_vin = "ENC-VIN-TOKEN=="
        c.set_smart_temp_new({"ac": "true", "temp": "28.5", "sw": "true"})
    finally:
        srv.shutdown()
    sent = seen[-1]
    assert sent["path"] == "/ms-remote-control/v1.0/remoteControl/setSmartTemp"
    assert sent["body"] == {
        "command": "immediately", "serviceId": "PAA",
        "setting": {"ac": "true", "temp": "28.5", "sw": "true"}}
    assert sent["x_vin"] == "ENC-VIN-TOKEN=="
    assert sent["signature_ok"], "the body must be inside the signed canonical"


def test_set_smart_temp_refuses_to_run_unauthenticated():
    seen: list[dict] = []
    srv = _start_gateway(seen)
    try:
        no_session = _client(srv)
        no_session.access_token = ""
        no_session.enc_vin = "ENC-VIN-TOKEN=="
        no_token = _client(srv)          # authenticated, but no x-vin
        for c in (no_session, no_token):
            try:
                c.set_smart_temp_new({"ac": "true"})
            except zc.ZeekrAuthError:
                pass
            else:  # pragma: no cover - only reached on a regression
                raise AssertionError("setSmartTemp ran without credentials")
    finally:
        srv.shutdown()
    assert not seen, "must not reach the gateway without credentials"
