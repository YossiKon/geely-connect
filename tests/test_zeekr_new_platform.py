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

from conftest import FAKE_VIN, load

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
            body = {"code": "000000", "msg": "ok",
                    "data": _STATUS_DATA if "ms-vehicle-status" in path else _LIST_DATA}
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
    # Nothing is moved, so top-level readers keep working.
    assert wrapped["updateTime"] == 1787817174561


def test_the_cars_own_clock_lands_where_car_reported_at_reads_it():
    """`updateTime` is the CAR's stamp on the snapshot, and the old platform
    nests it inside vehicleStatus - which is where sensor._reported_at looks.
    Left only at the top level it resolved to nothing, so `Car Reported At`
    read unknown on every new-platform car: the one entity whose purpose is to
    show that the cloud is replaying a stale snapshot (#24)."""
    wrapped = adapter._wrap_vehicle_status(_STATUS_DATA)
    assert wrapped["vehicleStatus"]["updateTime"] == 1787817174561


def test_wrapper_leaves_everything_else_alone():
    old_shape = {"vehicleStatus": {"basicVehicleStatus": {}}, "updateTime": 1}
    assert adapter._wrap_vehicle_status(old_shape) is old_shape
    assert adapter._wrap_vehicle_status({"a": 1}) == {"a": 1}
    assert adapter._wrap_vehicle_status(None) is None
    assert adapter._wrap_vehicle_status("not a dict") == "not a dict"
