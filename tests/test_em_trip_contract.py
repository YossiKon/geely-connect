"""Preserve EM travel-plan reads from ordinary class-defined client methods."""
from types import SimpleNamespace

from conftest import FAKE_VIN, have_homeassistant, load
from run import skip

ad = load("zeekr_adapter")


class _Client:
    enc_vin = "fake-vehicle-token"
    hf_token = "fake-hf-token"

    def __init__(self):
        self.trip = {"btActive": "true", "btTempActive": "false", "bw": "1"}

    def travel_plan_new(self):
        return {"code": "000000", "data": self.trip, "message": "ok"}

    def charging_plan_new(self):
        return {"code": "000000", "data": {
            "bcCycleActive": True, "startTime": "23:00", "endTime": "07:00"}}


def _make():
    adapter = ad.ZeekrAdapter(
        email="fake@example.invalid", vin=FAKE_VIN, user_id="fake-user",
        access_token="fake-access", refresh_token="fake-refresh",
        hf_token="fake-hf", hf_expiry=10 ** 15)
    adapter._client = _Client()
    return adapter, adapter._client


def test_bound_travel_method_preserves_raw_fields_for_slot_4():
    adapter, client = _make()
    # Each attribute lookup makes a different bound method object.
    assert client.travel_plan_new is not client.travel_plan_new
    for slot in ("4", 4):
        assert adapter.charge_server_get(slot) == {
            "code": 1000, "data": client.trip, "message": "ok"}
    for slot in ("2", "6"):
        data = adapter.charge_server_get(slot)["data"]
        assert data["rbcStartTime"] == "23:00"
        assert "btActive" not in data


def test_bound_travel_method_projects_on_off_and_unknown_in_real_entities():
    if not have_homeassistant():
        skip("homeassistant not installed")
    bs = load("binary_sensor")
    adapter, client = _make()
    for raw, expected in (
            ({"btActive": "true", "btTempActive": "false", "bw": "1"}, (True, False, True)),
            ({"btActive": "false", "btTempActive": "true", "bw": "0"}, (False, True, False)),
            ({}, (None, None, None))):
        client.trip = raw
        coord = SimpleNamespace(data={"_scheduled_trip": adapter.charge_server_get("4")["data"]})
        values = []
        for key in ("scheduled_trip_weekly", "scheduled_trip_one_off", "battery_temp_maintenance"):
            spec = next(s for s in bs.SPECS if s[0] == key)
            values.append(bs.GeelyBinarySensor(coord, FAKE_VIN, "Geely", *spec).is_on)
        assert tuple(values) == expected, (raw, values)


def test_null_travel_data_and_refused_reads_keep_their_existing_contract():
    adapter, client = _make()
    client.trip = None
    assert adapter.charge_server_get("4")["data"] == {}
    refused = {"code": "8500", "data": {"btActive": "true"}}
    client.travel_plan_new = lambda: refused
    assert adapter.charge_server_get("4") is refused
    try:
        adapter.scheduled_trip_set(command="start")
    except NotImplementedError:
        pass
    else:
        raise AssertionError("unmapped travel write became available")

