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


def _real(trip):
    """The adapter over ZeekrClient's own methods, with only `_request`
    replaced: signing, the x-vin header and the envelope check go with it,
    and which method was picked does not."""
    adapter = ad.ZeekrAdapter(
        email="fake@example.invalid", vin=FAKE_VIN, user_id="fake-user",
        access_token="fake-access", refresh_token="fake-refresh",
        hf_token="fake-hf", hf_expiry=10 ** 15)
    client = adapter._client
    assert "travel_plan_new" in type(client).__dict__
    assert "travel_plan_new" not in vars(client)
    client.enc_vin = "fake-vehicle-token"
    plan = {"timerId": "2", "target": "2", "startTime": "23:00", "endTime": "05:00",
            "bcCycleActive": True, "bcTempActive": False}
    asked = []

    def request(method, path, **kw):
        asked.append((method, path.rsplit("/", 1)[1], kw.get("signer")))
        return {"code": "000000", "message": "ok",
                "data": trip if path.endswith("/getLatestTravelPlan") else plan}

    client._request = request
    return adapter, asked


def test_the_real_client_class_keeps_the_trip_fields():
    """A fake that carries its reads as attributes of the instance hands back
    the same object at every lookup, so a test of which method was picked
    passes on it and fails on a car - which is how the trip's answer came to
    be reshaped as a charging schedule with every existing test green."""
    trip = {"timerId": "4", "btActive": True, "btTempActive": False, "bw": "1"}
    adapter, asked = _real(trip)
    for slot in ("4", 4):
        assert adapter.charge_server_get(slot) == {
            "code": 1000, "message": "ok", "data": trip}
    for slot in ("2", "6", 2, 6):
        assert adapter.charge_server_get(slot) == {
            "code": 1000, "message": "ok", "data": {
                "bcCycleActive": "true", "bcTempActive": "false",
                "rbcStartTime": "23:00", "rbcEndTime": "05:00",
                "rbcTarget": "2", "timerId": "2"}}
    assert asked == ([("GET", "getLatestTravelPlan", "snc")] * 2
                     + [("GET", "getChargingPlan", "snc")] * 4)


def test_a_trip_that_is_not_armed_reads_off_and_the_rest_unknown():
    """The shape the adapter's own tests carry for this read - `btActive`
    false, `bw` null, no `btTempActive` - through the real client and the
    entities: Weekly is off, and the two the body does not answer stay
    unknown."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    bs = load("binary_sensor")
    adapter, _ = _real({"timerId": "4", "btActive": False, "bw": None})
    coord = SimpleNamespace(data={"_scheduled_trip": adapter.charge_server_get("4")["data"]})
    shown = {}
    for key in ("scheduled_trip_weekly", "scheduled_trip_one_off", "battery_temp_maintenance"):
        spec = next(s for s in bs.SPECS if s[0] == key)
        shown[key] = bs.GeelyBinarySensor(coord, FAKE_VIN, "Geely", *spec).is_on
    assert shown == {"scheduled_trip_weekly": False, "scheduled_trip_one_off": None,
                     "battery_temp_maintenance": None}, shown
