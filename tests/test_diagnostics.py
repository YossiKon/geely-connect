"""The diagnostics download is meant to be safe to paste into a bug report.

It leaked the VIN once, through the scheduled-charging `pin` field, because it
kept its own redaction list which had drifted from api.py's. These tests exist
so that cannot happen again unnoticed.
"""
import asyncio
import datetime
import json

from conftest import FAKE_VIN, have_homeassistant, load
from run import skip

TOKEN = "eyJhbGciOiJIUzI1NiJ9.header.signature"
EMAIL = "owner@example.com"
USER_ID = "8817263412"
LAT, LON = 32.0853123, 34.7817456


def _report():
    diag = load("diagnostics")

    class Coord:
        # A failure whose message names the vehicle. Error text is supposed to be
        # VIN-free and was fixed to be - but a report is the wrong place to bet
        # on every future `raise` remembering that.
        last_exception = Exception(f"status fetch failed for {FAKE_VIN}")
        last_update_success = False
        update_interval = datetime.timedelta(seconds=300)
        data = {
            "vehicleStatus": {"basicVehicleStatus": {
                "position": {"latitude": LAT, "longitude": LON}}},
            "_scheduled_charging": {"rbcStartTime": "23:00", "pin": FAKE_VIN,
                                    "sessionId": "sess", "vin": FAKE_VIN},
            "_state": {"parkComfortState": "1"},
        }

    class Entry:
        entry_id = "e1"
        data = {"vin": FAKE_VIN, "email": EMAIL, "cidpsso_token": TOKEN,
                "user_id": USER_ID, "device_idfa": "8E4F1B22-9C3D",
                "cert_path": f"/c/.storage/geely_connect/{FAKE_VIN}/cert.pem",
                "passToken": TOKEN, "appSecret": "0" * 32,
                "poll_mode": "manual"}
        options = {"pressure_unit": "psi"}

    class Api:
        command_trail = [
            {"at": "2026-08-07T09:01:47Z", "command": "RCE_2 start",
             "detail": [{"key": "rce.temp", "value": "15.5"}],
             "outcome": "accepted", "code": "1000", "ms": 812},
            {"at": "2026-08-07T09:01:47Z", "command": "RCE_2 start",
             "detail": [{"key": "rce.heat", "value": "front-left"},
                        {"key": "sessionId", "value": TOKEN}],
             "outcome": "rejected", "code": "8070",
             "message": "The last request has not yet been executed",
             "ms": 190},
        ]

    class Hass:
        data = {"geely_connect": {"e1": {
            "coordinator": Coord(),
            "api": Api(),
            "poll_state": {"cycle": 41, "idle": 7, "sig": "9f2c1d",
                           "force_secondary": True},
            "capabilities": {"ac.enabled": True, "vin": FAKE_VIN},
            "capabilities_raw": [
                {"functionId": "remote_climate_control_2", "vin": FAKE_VIN,
                 "valueEnable": True, "valueRange": "15.5|28.5",
                 "paramsJson": '{"dpt_vent_loc":"front-left,front-right"}'},
            ]}}}

    return asyncio.run(diag.async_get_config_entry_diagnostics(Hass(), Entry()))


def test_nothing_identifying_survives_the_report():
    if not have_homeassistant():
        skip("homeassistant not installed")
    blob = json.dumps(_report())
    for label, secret in (("VIN", FAKE_VIN), ("email", EMAIL), ("token", TOKEN),
                          ("user id", USER_ID), ("app secret", "0" * 32),
                          ("latitude", str(LAT)), ("longitude", str(LON))):
        assert secret not in blob, f"{label} leaked into diagnostics"


def test_the_pin_field_is_masked_because_it_carries_the_vin():
    if not have_homeassistant():
        skip("homeassistant not installed")
    sc = _report()["status"]["_scheduled_charging"]
    assert FAKE_VIN not in json.dumps(sc)
    # the useful part survives
    assert sc["rbcStartTime"] == "23:00"


def test_the_capability_catalog_is_redacted_too():
    if not have_homeassistant():
        skip("homeassistant not installed")
    caps = _report()["capabilities"]
    assert FAKE_VIN not in json.dumps(caps)
    assert caps["ac.enabled"] is True, "redaction ate a useful flag"


def test_the_raw_catalog_reaches_the_report_intact_but_redacted():
    """The parsed summary keeps about a dozen derived flags and drops the rest,
    which is how "does this trim advertise a blower level, or seat positions by
    name?" became unanswerable from a report - so the catalog goes in verbatim.
    It is echoed from the server and carries a vin field, hence both passes."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    raw = _report()["capabilities_raw"]
    assert FAKE_VIN not in json.dumps(raw)
    entry = raw[0]
    assert entry["functionId"] == "remote_climate_control_2"
    assert entry["valueRange"] == "15.5|28.5", "redaction ate the temp range"
    assert "front-left" in entry["paramsJson"], "redaction ate the seat positions"


def test_the_report_says_why_the_data_is_as_old_as_it_is():
    """A stale reading and a failing fetch are indistinguishable from `status`
    alone, which is how #21 stayed open for days - the owner could see numbers
    that did not move but nothing saying whether the car or the poller was at
    fault."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    p = _report()["polling"]
    assert p["cycle"] == 41 and p["unchanged_polls"] == 7
    assert p["interval_seconds"] == 300.0
    assert p["last_update_success"] is False
    assert p["force_secondary_pending"] is True
    assert "status fetch failed" in p["last_exception"]
    assert "sig" not in p, "the opaque change hash is noise in a report"


def test_a_vin_inside_an_exception_message_does_not_survive():
    """Both redaction passes match key names, so a VIN in the middle of a
    sentence walks straight through them."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    assert FAKE_VIN not in _report()["polling"]["last_exception"]


def test_the_command_trail_shows_the_rejection_that_would_otherwise_vanish():
    """A command refused with "the last request has not yet been executed" is
    dropped rather than retried, and leaves no trace unless debug logging was
    already on. This is the section that makes that visible after the fact."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    trail = _report()["recent_commands"]
    assert [c["outcome"] for c in trail] == ["accepted", "rejected"]
    assert trail[1]["code"] == "8070"
    assert "has not yet been executed" in trail[1]["message"]
    # The useful parameter survives; a secret riding along does not.
    detail = json.dumps(trail[1]["detail"])
    assert "front-left" in detail
    assert TOKEN not in detail, "a token in a command parameter reached the report"


def test_the_report_says_whether_debug_logging_is_actually_on():
    if not have_homeassistant():
        skip("homeassistant not installed")
    log = _report()["logging"]
    assert log["effective_level"] in {"DEBUG", "INFO", "WARNING", "ERROR",
                                      "CRITICAL", "NOTSET"}
    assert isinstance(log["debug_enabled"], bool)


def test_the_report_is_still_worth_reading():
    if not have_homeassistant():
        skip("homeassistant not installed")
    r = _report()
    assert set(r) == {"entry_data", "options", "polling", "recent_commands",
                      "logging", "capabilities", "capabilities_raw", "status",
                      "charge_server", "cards"}
    assert r["options"]["pressure_unit"] == "psi"
    assert r["status"]["_state"]["parkComfortState"] == "1"


def test_the_report_lists_the_card_resource_when_lovelace_has_one():
    """The resource entry is the thing to check first; the report should show
    it rather than sending anyone into Settings."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    import types
    diag = load("diagnostics")

    class Entry:
        entry_id = "e1"
        data = {"vin": FAKE_VIN}
        options = {}

    class _Res:
        def async_items(self):
            return [{"url": "/geely_connect/geely-card.js?v=1.2.3"},
                    {"url": "/hacsfiles/other/other.js"}]

    class Hass:
        data = {"geely_connect": {}, "geely_connect_cards_registered": True,
                "lovelace": types.SimpleNamespace(resources=_Res())}

    cards = asyncio.run(
        diag.async_get_config_entry_diagnostics(Hass(), Entry()))["cards"]
    assert cards["lovelace_resources"] == ["/geely_connect/geely-card.js?v=1.2.3"]
    assert cards["registered"] is True


def test_a_resource_list_that_will_not_be_read_does_not_break_the_report():
    if not have_homeassistant():
        skip("homeassistant not installed")
    import types
    diag = load("diagnostics")

    class Entry:
        entry_id = "e1"
        data = {"vin": FAKE_VIN}
        options = {}

    class _Angry:
        def async_items(self):
            raise RuntimeError("not loaded")

    class Hass:
        data = {"geely_connect": {},
                "lovelace": types.SimpleNamespace(resources=_Angry())}

    cards = asyncio.run(
        diag.async_get_config_entry_diagnostics(Hass(), Entry()))["cards"]
    assert cards["lovelace_resources"] == ["<unreadable>"]


def test_the_report_says_whether_the_cards_can_load():
    """"Custom element not found: geely-card" with a healthy vehicle has one
    cause worth ruling out first - the file never reaching the browser. The
    report answers that without asking anyone for a console."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    cards = _report()["cards"]
    assert cards["file_present"] is True, "the shipped card file is missing"
    assert cards["url"] == "/geely_connect/geely-card.js"
    assert "registered" in cards and "lovelace_resources" in cards


def test_a_report_can_be_produced_before_the_coordinator_exists():
    """Setup can fail early; diagnostics must still work, not raise."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    diag = load("diagnostics")

    class Entry:
        entry_id = "missing"
        data = {"vin": FAKE_VIN}
        options: dict = {}

    class Hass:
        data: dict = {}

    r = asyncio.run(diag.async_get_config_entry_diagnostics(Hass(), Entry()))
    assert r["status"] == {}


def _sweep_report(get):
    """A report from an entry whose client can read charge-server slots."""
    diag = load("diagnostics")

    class Api:
        command_trail: list = []
        charge_server_get = staticmethod(get)

    class Entry:
        entry_id = "e1"
        data = {"vin": FAKE_VIN}
        options: dict = {}

    class Hass:
        data = {"geely_connect": {"e1": {"api": Api()}}}

        async def async_add_executor_job(self, fn, *args):
            # A real thread, not an inline call: a client that blocks has to
            # block the way it would in Home Assistant for the timeout below
            # to mean anything.
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, fn, *args)

    return asyncio.run(
        diag.async_get_config_entry_diagnostics(Hass(), Entry()))["charge_server"]


def test_a_client_without_the_endpoint_is_not_asked_for_it():
    """The EM platform has no charge-server, and neither does an entry whose
    setup died before it built a client. Both must produce a report."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    assert _report()["charge_server"] == {}


def test_every_schedule_slot_is_read_and_redacted():
    if not have_homeassistant():
        skip("homeassistant not installed")
    seen = []

    def get(slot):
        seen.append(slot)
        return {"code": "1000", "data": {"vin": FAKE_VIN, "bizType": slot,
                                         "startTime": "22:00"}}

    out = _sweep_report(get)
    # 7 is the rapid-warming write; its GET returns nothing worth a round trip.
    assert seen == ["1", "2", "3", "4", "5", "6", "8"]
    assert FAKE_VIN not in json.dumps(out), "the echoed VIN reached the report"
    assert out["4"]["data"]["startTime"] == "22:00", "redaction ate the schedule"


def test_a_slot_that_does_not_exist_is_recorded_rather_than_raised():
    """Most of this range is expected to fail - which slot fails, and how, is
    the finding. A failure that took the report down would lose the rest."""
    if not have_homeassistant():
        skip("homeassistant not installed")

    def get(slot):
        if slot == "6":
            return {"code": "1000", "data": {"rbcStartTime": "23:00"}}
        raise RuntimeError(f"illegal request parameter for {FAKE_VIN}")

    out = _sweep_report(get)
    assert out["6"]["data"]["rbcStartTime"] == "23:00"
    assert "illegal request parameter" in out["1"]["error"]
    assert FAKE_VIN not in json.dumps(out), "a VIN inside an error message survived"


def test_a_hanging_endpoint_does_not_cost_the_whole_report():
    if not have_homeassistant():
        skip("homeassistant not installed")
    diag = load("diagnostics")
    original = diag._SWEEP_TIMEOUT_S
    diag._SWEEP_TIMEOUT_S = 0.05
    try:
        def get(slot):
            if slot == "1":
                return {"code": "1000", "data": {"ok": True}}
            import time
            time.sleep(0.5)
            return {}

        out = _sweep_report(get)
    finally:
        diag._SWEEP_TIMEOUT_S = original
    assert out["1"]["data"]["ok"] is True, "the slot read before the stall was lost"
    assert "cut short" in out["truncated"]


def test_the_two_redaction_lists_have_not_drifted_apart():
    """diagnostics runs api.redact() as a second pass precisely so that a key
    missing from one list is still caught by the other. If that second pass is
    ever removed, this is the test that should fail."""
    import io, os
    from conftest import PKG
    src = io.open(os.path.join(PKG, "diagnostics.py"), encoding="utf-8").read()
    assert "from .api import redact" in src
    assert "redact(" in src, "the normalised-key pass is gone"
