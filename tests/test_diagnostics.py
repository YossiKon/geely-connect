"""The diagnostics download is meant to be safe to paste into a bug report.

It leaked the VIN once, through the scheduled-charging `pin` field, because it
kept its own redaction list which had drifted from api.py's. These tests exist
so that cannot happen again unnoticed.
"""
import asyncio
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

    class Hass:
        data = {"geely_connect": {"e1": {
            "coordinator": Coord(),
            "capabilities": {"ac.enabled": True, "vin": FAKE_VIN}}}}

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


def test_the_report_is_still_worth_reading():
    if not have_homeassistant():
        skip("homeassistant not installed")
    r = _report()
    assert set(r) == {"entry_data", "options", "capabilities", "status"}
    assert r["options"]["pressure_unit"] == "psi"
    assert r["status"]["_state"]["parkComfortState"] == "1"


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


def test_the_two_redaction_lists_have_not_drifted_apart():
    """diagnostics runs api.redact() as a second pass precisely so that a key
    missing from one list is still caught by the other. If that second pass is
    ever removed, this is the test that should fail."""
    import io, os
    from conftest import PKG
    src = io.open(os.path.join(PKG, "diagnostics.py"), encoding="utf-8").read()
    assert "from .api import redact" in src
    assert "redact(" in src, "the normalised-key pass is gone"
