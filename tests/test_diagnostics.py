"""The diagnostics download is meant to be safe to paste into a bug report.

It leaked the VIN once, through the scheduled-charging `pin` field, because it
kept its own redaction list which had drifted from api.py's. These tests exist
so that cannot happen again unnoticed.
"""
import asyncio
import datetime
import json
from types import MappingProxyType

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
        # Read-only mappings, as Home Assistant holds them: with dicts here
        # the value mask passed every test and read nothing from a real
        # entry, the VIN of an original-platform car included.
        entry_id = "e1"
        data = MappingProxyType({
            "vin": FAKE_VIN, "email": EMAIL, "cidpsso_token": TOKEN,
            "user_id": USER_ID, "device_idfa": "8E4F1B22-9C3D",
            "cert_path": f"/c/.storage/geely_connect/{FAKE_VIN}/cert.pem",
            "passToken": TOKEN, "appSecret": "0" * 32,
            "poll_mode": "manual"})
        options = MappingProxyType({"pressure_unit": "psi"})

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
            ],
            "capabilities_new_raw": [
                {"functionCategory": "remote_control", "functionCode": "C_PAA_5_1",
                 "functionName": "座椅加热-主驾", "paramValueUse": "Y",
                 "vin": FAKE_VIN},
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
    assert set(r) == {"entry_data", "options", "vehicle_token", "polling",
                      "recent_commands", "logging", "capabilities",
                      "capabilities_raw", "capabilities_new_raw", "status",
                      "charge_server", "charge_soc", "cards"}
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
        data = MappingProxyType({"vin": FAKE_VIN})
        options = MappingProxyType({})

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

    # The shape cards.py also reads, a dict: the report listed nothing there,
    # which reads as a card that was never registered.
    Hass.data = {**Hass.data, "lovelace": {"resources": _Res()}}
    cards = asyncio.run(
        diag.async_get_config_entry_diagnostics(Hass(), Entry()))["cards"]
    assert cards["lovelace_resources"] == ["/geely_connect/geely-card.js?v=1.2.3"]


def test_a_resource_list_that_will_not_be_read_does_not_break_the_report():
    if not have_homeassistant():
        skip("homeassistant not installed")
    import types
    diag = load("diagnostics")

    class Entry:
        entry_id = "e1"
        data = MappingProxyType({"vin": FAKE_VIN})
        options = MappingProxyType({})

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
        data = MappingProxyType({"vin": FAKE_VIN})
        options = MappingProxyType({})

    class Hass:
        data: dict = {}

    r = asyncio.run(diag.async_get_config_entry_diagnostics(Hass(), Entry()))
    assert r["status"] == {}


def _token_report(data, options, api):
    """A report from a Geely EM entry, with or without a running client."""
    diag = load("diagnostics")

    class Entry:
        entry_id = "e1"

    Entry.data = MappingProxyType({"vin": FAKE_VIN, "platform": "zeekr", **data})
    Entry.options = MappingProxyType(options)

    class Hass:
        data = {"geely_connect": {"e1": {"api": api}}} if api else {}

    return asyncio.run(diag.async_get_config_entry_diagnostics(Hass(), Entry()))


def test_the_report_says_whether_the_vehicle_token_is_set_but_not_what_it_is():
    """Both passes mask the stored token whatever it holds, so a report could
    not tell a Geely EM entry with a token from one without (#63) - and that
    decides which way every read and command went. Three booleans and the
    probe's verdict on the car, never the value; a legacy entry has no such
    token at all."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    assert _report()["vehicle_token"] is None
    x_vin = "c2VjcmV0LXZlaGljbGUtdG9rZW4tdmFsdWU="

    class Api:
        command_trail: list = []
        enc_vin = x_vin
        car_refused = False

    r = _token_report({"zeekr_enc_vin": x_vin}, {}, Api())
    assert r["vehicle_token"] == {
        "in_entry_data": True, "in_options": False, "in_use": True,
        "car_refused": False}
    assert x_vin not in json.dumps(r), "the token itself reached the report"
    # Pasted under Configure, on an entry whose setup never built a client.
    r = _token_report({}, {"zeekr_enc_vin": x_vin}, None)
    assert r["vehicle_token"] == {
        "in_entry_data": False, "in_options": True, "in_use": None,
        "car_refused": None}
    assert x_vin not in json.dumps(r)
    # #63: a running entry the derivation left without one - no verdict on
    # the car (a build nobody has captured, or no answer)...
    Api.enc_vin = ""
    r = _token_report({}, {}, Api())
    assert r["vehicle_token"] == {
        "in_entry_data": False, "in_options": False, "in_use": False,
        "car_refused": False}
    # ...or for a car the new gateway turns away (079001), which the probe
    # says only at info: the report has to tell the two apart on its own.
    Api.car_refused = True
    r = _token_report({}, {}, Api())
    assert r["vehicle_token"] == {
        "in_entry_data": False, "in_options": False, "in_use": False,
        "car_refused": True}


def test_a_geely_em_entrys_commands_reach_the_report():
    """The trail was the old login's client's alone, so on a Geely EM entry
    this section came back empty whatever had been pressed (#104)."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    ad = load("zeekr_adapter")
    a = ad.ZeekrAdapter(email=EMAIL, vin=FAKE_VIN, user_id=USER_ID,
                        access_token=TOKEN, refresh_token=TOKEN, hf_token=TOKEN,
                        gateway="https://unused.invalid")
    a._client.control_resp = lambda vin, body: {"code": "1000"}
    a.control("RCE_2", [{"key": "rce.temp", "value": "22.0"}], "start", 180)
    trail = _token_report({}, {}, a)["recent_commands"]
    assert [(n["command"], n["outcome"], n["code"]) for n in trail] == [
        ("RCE_2 start", "accepted", "1000")]
    assert trail[0]["detail"] == [{"key": "rce.temp", "value": "22.0"}]


def _sweep_report(get):
    """A report from an entry whose client can read charge-server slots."""
    diag = load("diagnostics")

    class Api:
        command_trail: list = []
        charge_server_get = staticmethod(get)

    class Entry:
        entry_id = "e1"
        data = MappingProxyType({"vin": FAKE_VIN})
        options = MappingProxyType({})

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


def test_an_adapter_that_does_not_serve_the_endpoint_is_not_asked_either():
    """The EM adapter carries charge_server_get and says it does not serve
    it; asked anyway, every slot came back as a NotImplementedError row, seven
    of what read like failures in every Geely EM report."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    asked = []
    diag = load("diagnostics")

    class Api:
        command_trail: list = []
        serves_charge_server = False

        def charge_server_get(self, slot):
            asked.append(slot)
            raise NotImplementedError("not mapped on the new platform")

    class Entry:
        entry_id = "e1"
        data = MappingProxyType({"vin": FAKE_VIN})
        options = MappingProxyType({})

    class Hass:
        data = {"geely_connect": {"e1": {"api": Api()}}}

        async def async_add_executor_job(self, fn, *args):
            return fn(*args)

    got = asyncio.run(diag.async_get_config_entry_diagnostics(Hass(), Entry()))
    assert got["charge_server"] == {} and asked == [], (got["charge_server"], asked)


def test_every_schedule_slot_is_read_and_redacted():
    if not have_homeassistant():
        skip("homeassistant not installed")
    seen = []

    def get(slot):
        seen.append(slot)
        # Everything the write side of this same endpoint is known to carry,
        # echoed back: `pin` holds the VIN by convention here, which is the
        # field that leaked it once already.
        return {"code": "1000", "sessionId": TOKEN,
                "data": {"vin": FAKE_VIN, "pin": FAKE_VIN, "userId": USER_ID,
                         "bizType": slot, "startTime": "22:00"}}

    out = _sweep_report(get)
    # 7 is the rapid-warming write; its GET returns nothing worth a round trip.
    assert seen == ["1", "2", "3", "4", "5", "6", "8"]
    blob = json.dumps(out)
    for label, secret in (("VIN", FAKE_VIN), ("user id", USER_ID),
                          ("session token", TOKEN)):
        assert secret not in blob, f"{label} reached the report from a schedule slot"
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


def test_the_new_gateway_catalogue_reaches_the_report_untranslated():
    """A token-served Geely EM car's `capabilities_raw` is the translated list,
    which drops every code the translation has no rule for - and the codes a
    car with vented seats or a sunroof carries are the ones #63 waits for. The
    gateway's own rows go in beside it, through both redaction passes."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    rows = _report()["capabilities_new_raw"]
    assert rows[0]["functionCode"] == "C_PAA_5_1"
    assert rows[0]["functionName"] == "座椅加热-主驾"
    assert rows[0]["vin"] != FAKE_VIN and FAKE_VIN not in str(rows)


def test_the_old_platform_refresh_token_is_on_the_literal_list_too():
    """The old platform's session exchange returns a refresh token (#101), and
    a build that stores it may spell the key either way. redact() masks both by
    normalised name; the literal list is the belt to those braces, and it had
    only the new platform's spelling."""
    import io, os
    from conftest import PKG
    src = io.open(os.path.join(PKG, "diagnostics.py"), encoding="utf-8").read()
    diag = load("diagnostics")
    assert {"refresh_token", "refreshToken", "zeekr_refresh_token"} <= diag._REDACT
    assert '"refresh_token", "refreshToken",' in src


def test_the_two_redaction_lists_have_not_drifted_apart():
    """diagnostics runs api.redact() as a second pass precisely so that a key
    missing from one list is still caught by the other. If that second pass is
    ever removed, this is the test that should fail."""
    import io, os, re
    from conftest import PKG
    src = io.open(os.path.join(PKG, "diagnostics.py"), encoding="utf-8").read()
    assert re.search(r"from \.api import [^\n]*\bredact\b", src)
    assert "redact(" in src, "the normalised-key pass is gone"


def _strings(obj):
    """Every string a report holds, a key's included."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from _strings(k)
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)


def _swept(api, data=None):
    """A report from an entry whose client is `api`, the sweep run inline."""
    diag = load("diagnostics")

    class Entry:
        entry_id = "e1"
        options = MappingProxyType({})

    Entry.data = MappingProxyType({"vin": FAKE_VIN, **(data or {})})

    class Hass:
        data = {"geely_connect": {"e1": {"api": api}}}

        async def async_add_executor_job(self, fn, *args):
            return fn(*args)

    return asyncio.run(diag.async_get_config_entry_diagnostics(Hass(), Entry()))


def test_a_session_token_a_refused_header_quotes_stays_out_of_the_sweep():
    """The Geely EM client sends the session token as a header, and
    http.client refuses a value holding a line break with an error that
    quotes it, escaped. The coordinator's warning masks it through the
    client; the sweep's rows took out only the VIN and the vehicle token, so
    the session token reached the report - the file an owner attaches to a
    public issue. Asked of each row's own text: json escapes the escape, so
    a check for the whole token in the dumped report passes either way."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    ad = load("zeekr_adapter")
    first, second = "fake-access-AAAA", "BBBB-session-tail"
    a = ad.ZeekrAdapter(email=EMAIL, vin=FAKE_VIN, user_id=USER_ID,
                        access_token=f"{first}\n{second}", refresh_token=TOKEN,
                        hf_token=TOKEN, gateway="https://unused.invalid")
    a.adopt_enc_vin("c2VjcmV0LXZlaGljbGUtdG9rZW4tdmFsdWU=")
    rows = _swept(a, {"platform": "zeekr"})["charge_server"]
    for slot in ("2", "4", "6"):           # the schedule slots it maps
        text = rows[slot]["error"]
        assert text.startswith("ValueError: Invalid header value"), text
        assert first not in text and second not in text, text
    assert "not mapped yet" in rows["1"]["error"], rows["1"]


def test_a_refusal_the_original_platform_hands_back_is_masked_in_the_report():
    """The original platform's client hands a refused slot read back as
    data, its sentence and all - of the refusals, only an auth failure is
    raised - and redact() masks by name, which a value inside the sentence
    escapes. No gateway has been seen to word a refusal around the car or
    the account (the twenty reports from that platform attached to issues
    answered every slot with a success), so the sentence is made up; the
    way it takes into the report is the real client's."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    api = load("api")
    jwt = "jwt-session-0001-current"
    a = api.GeelyApi(app_id="APPID", app_secret="0" * 32, user_id=USER_ID,
                     vin=FAKE_VIN, cidpsso_token=TOKEN, client_id="CLIENT",
                     vehicle_series="E245", vehicle_model="E245-J1",
                     device_id="dev", cert_path="", key_path="", email=EMAIL)
    a._jwt = jwt
    a._headers_with_jwt = lambda: {"Authorization": jwt}
    words = (f"vehicle {FAKE_VIN} is not bound to account {USER_ID} "
             f"({EMAIL}) for session {jwt}")
    a._mtls_send = lambda host, method, path, body, extra_headers=None: (
        200, json.dumps({"code": "8155", "message": words,
                         "httpStatus": "FORBIDDEN"}).encode())
    row = _swept(a, {"email": EMAIL, "user_id": USER_ID,
                     "cidpsso_token": TOKEN})["charge_server"]["1"]
    assert row["code"] == "8155", row
    assert "is not bound to account" in row["message"], row
    for label, value in (("VIN", FAKE_VIN), ("user id", USER_ID),
                         ("e-mail", EMAIL), ("session token", jwt)):
        assert value not in row["message"], f"the {label} survived: {row}"


def test_the_kept_session_pair_and_the_live_one_never_reach_the_report():
    """The entry keeps the original platform's JWT and refresh token as
    access_token and refresh_token (#101), and the client holds a newer pair
    once a renewal ran. Both passes mask the stored names, and a gateway's
    sentence naming any of the four values is cleared as well."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    api = load("api")
    kept_jwt, kept_rt = "jwt-session-0007-kept", "rt-session-0007-kept"
    live_jwt, live_rt = "jwt-session-0008-live", "rt-session-0008-live"
    a = api.GeelyApi(app_id="APPID", app_secret="0" * 32, user_id=USER_ID,
                     vin=FAKE_VIN, cidpsso_token=TOKEN, client_id="CLIENT",
                     vehicle_series="E245", vehicle_model="E245-J1",
                     device_id="dev", cert_path="", key_path="", email=EMAIL,
                     access_token=kept_jwt, refresh_token=kept_rt)
    a._jwt, a._rt = live_jwt, live_rt
    a._headers_with_jwt = lambda: {"Authorization": a._jwt}
    words = f"sessions {kept_jwt} {kept_rt} {live_jwt} {live_rt} refused"
    a._mtls_send = lambda host, method, path, body, extra_headers=None: (
        200, json.dumps({"code": "8155", "message": words}).encode())
    report = _swept(a, {"access_token": kept_jwt, "refresh_token": kept_rt})
    assert report["entry_data"]["access_token"] == "**REDACTED**"
    assert report["entry_data"]["refresh_token"] == "**REDACTED**"
    assert "refused" in report["charge_server"]["1"]["message"], report
    for text in _strings(report):
        for value in (kept_jwt, kept_rt, live_jwt, live_rt):
            assert value not in text, text


def test_an_address_the_entry_keeps_as_typed_is_masked_in_any_case():
    """The original platform keeps the address as it was typed; a gateway
    may name it lower-cased, in a slot's sentence or anywhere else the
    report carries."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    api = load("api")
    typed = "Owner.Name+cars@Example.COM"
    a = api.GeelyApi(app_id="APPID", app_secret="0" * 32, user_id=USER_ID,
                     vin=FAKE_VIN, cidpsso_token=TOKEN, client_id="CLIENT",
                     vehicle_series="E245", vehicle_model="E245-J1",
                     device_id="dev", cert_path="", key_path="", email=typed)
    a._jwt = "jwt-session-0001-current"
    a._headers_with_jwt = lambda: {"Authorization": a._jwt}
    a._mtls_send = lambda host, method, path, body, extra_headers=None: (
        200, json.dumps({"code": "8155", "httpStatus": "FORBIDDEN",
                         "message": f"not bound to {typed.lower()}"}).encode())
    report = _swept(a, {"email": typed, "user_id": USER_ID})
    assert "not bound to" in report["charge_server"]["1"]["message"], report
    for text in _strings(report):
        assert typed.lower() not in text.lower(), text


def _legacy_client():
    """The original platform's real client with a session already held, so
    a read goes straight out through _mtls_send."""
    api = load("api")
    a = api.GeelyApi(app_id="APPID", app_secret="0" * 32, user_id=USER_ID,
                     vin=FAKE_VIN, cidpsso_token=TOKEN, client_id="CLIENT",
                     vehicle_series="E245", vehicle_model="E245-J1",
                     device_id="dev", cert_path="", key_path="", email=EMAIL)
    a._jwt = "jwt-session-0003-current"
    a._headers_with_jwt = lambda: {"Authorization": a._jwt}
    return a


def test_the_charge_limit_and_the_discharge_floor_are_read_as_the_app_reads_them():
    """#107 found the app's own read of both on an EU EX5: one GET each, on
    the session every other read uses. #131 asks whether an E2 answers it.
    Each row is the answer through both redaction passes, and the slots are
    read as before."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    a = _legacy_client()
    calls = []

    def send(host, method, path, body, extra_headers=None):
        calls.append((host, method, path, body, (extra_headers or {}).get("Authorization")))
        if "/status/soc/" in path:
            soc = "1000" if path.endswith("?setting=charging") else "200"
            answer = {"code": "1000", "message": "operation succeed",
                      "data": {"soc": soc, "socTime": "1793213264334", "vin": FAKE_VIN}}
        else:
            answer = {"code": "1000", "data": {"bizType": path[-1]}}
        return 200, json.dumps(answer).encode()

    a._mtls_send = send
    report = _swept(a, {"email": EMAIL, "user_id": USER_ID})
    soc = report["charge_soc"]
    assert set(soc) == {"charging", "discharging"}, soc
    assert soc["charging"]["data"]["soc"] == "1000", soc
    assert soc["discharging"]["data"]["soc"] == "200", soc
    assert soc["charging"]["data"]["socTime"] == "1793213264334", soc
    assert FAKE_VIN not in json.dumps(report), "the VIN an answer names survived"
    path = f"/remote-control/vehicle/status/soc/{FAKE_VIN}?setting="
    assert [c for c in calls if "/status/soc/" in c[2]] == [
        ("apis.ecloudeu.com", "GET", path + "charging", b"", a._jwt),
        ("apis.ecloudeu.com", "GET", path + "discharging", b"", a._jwt),
    ], calls
    assert len([c for c in calls if "/charge-server/" in c[2]]) == 7, calls


def test_a_charge_limit_read_that_fails_is_a_row_with_its_error_masked():
    """A backend without the read answers with an error, and the error is
    the finding - recorded, never raised, and masked before its cut: the
    session token here runs across the 300th character, where a mask after
    the cut would leave its first half, which no later mask can match."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    a = _legacy_client()
    head = f"OSError: no route for {FAKE_VIN} of {EMAIL} "
    words = head[len("OSError: "):] + "x" * (300 - len(head) - 10) + a._jwt + " later"

    def send(host, method, path, body, extra_headers=None):
        if path.endswith("?setting=charging"):
            raise OSError(words)
        return 200, json.dumps({"code": "1000", "data": {"soc": "200"}}).encode()

    a._mtls_send = send
    soc = _swept(a, {"email": EMAIL, "user_id": USER_ID})["charge_soc"]
    row = soc["charging"]["error"]
    assert row.startswith("OSError: no route for ") and len(row) == 300, row
    for value in (FAKE_VIN, EMAIL, a._jwt[:10]):
        assert value not in row, (value, row)
    assert soc["discharging"]["data"]["soc"] == "200", soc


def test_a_car_on_another_gateway_is_not_asked_for_the_charge_limit():
    """The read has answered on the EU gateway only. Elsewhere the path is
    unseen, and a 1402 from a gateway that does not know it would renew the
    session on every report - so it is not sent there, and the slots are
    read as before."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    for host in ("apis.ecloudkr.com", "apis.ecloudus.com",
                 "tsp-geely-apis-uae.xcloudsvc.com"):
        a = _legacy_client()
        a.control_host = host
        paths = []

        def send(host_, method, path, body, extra_headers=None):
            paths.append(path)
            return 200, json.dumps({"code": "1000", "data": {}}).encode()

        a._mtls_send = send
        report = _swept(a, {"email": EMAIL, "user_id": USER_ID})
        assert report["charge_soc"] == {}, (host, report["charge_soc"])
        assert not [p for p in paths if "/status/soc/" in p], (host, paths)
        assert len([p for p in paths if "/charge-server/" in p]) == 7, (host, paths)


def test_a_geely_em_entry_is_not_asked_for_the_charge_limit():
    """The read is the original platform's; the new one's adapter has no
    such method, and its report has no such rows."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    ad = load("zeekr_adapter")
    a = ad.ZeekrAdapter(email=EMAIL, vin=FAKE_VIN, user_id=USER_ID,
                        access_token="fake-access-0123456789",
                        refresh_token="fake-refresh-0123456789",
                        hf_token="fake-hf-0123456789",
                        gateway="https://unused.invalid")
    assert not hasattr(a, "charge_soc_get")
    assert _swept(a, {"platform": "zeekr"})["charge_soc"] == {}


def test_the_charge_limit_reads_come_out_of_the_sweep_s_time():
    """The slots and the two settings share one budget: a sweep that uses
    all of it leaves the settings unasked rather than making the report
    wait a second time."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    import time
    diag = load("diagnostics")
    asked = []

    class Api:
        command_trail: list = []
        control_host = "apis.ecloudeu.com"

        def charge_server_get(self, slot):
            time.sleep(0.5)
            return {}

        def charge_soc_get(self, setting):
            asked.append(setting)
            return {"code": "1000", "data": {"soc": "1000"}}

    class Entry:
        entry_id = "e1"
        data = MappingProxyType({"vin": FAKE_VIN})
        options = MappingProxyType({})

    class Hass:
        data = {"geely_connect": {"e1": {"api": Api()}}}

        async def async_add_executor_job(self, fn, *args):
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, fn, *args)

    original = diag._SWEEP_TIMEOUT_S
    diag._SWEEP_TIMEOUT_S = 0.1
    try:
        began = time.monotonic()
        report = asyncio.run(diag.async_get_config_entry_diagnostics(Hass(), Entry()))
        took = time.monotonic() - began
    finally:
        diag._SWEEP_TIMEOUT_S = original
    assert "cut short" in report["charge_server"]["truncated"], report["charge_server"]
    assert report["charge_soc"] == {
        "truncated": "the sweep passed 0.1s before these were asked"}, report["charge_soc"]
    assert asked == [], asked
    # One budget, not two: the report waited for the stalled slot's thread
    # (asyncio.run joins it) but asked the settings nothing after it.
    assert took < 1.0, took


def test_every_string_in_the_report_is_cleared_of_what_the_entry_holds():
    """Both redaction passes mask a field by its name. A value the entry
    keeps under such a name, standing in a sentence or as a key anywhere
    else in the report - a status field, a catalogue's label, a command's
    error, the last error - is masked by value. A name only one pass
    knows counts: `appSecret` is on redact()'s normalised lists alone, and
    `latitude` on Home Assistant's exact one alone. A password is not
    masked: by value, one that is a word would be pointed at wherever the
    word stands."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    diag = load("diagnostics")
    user_id, idfa, hmac = 8817263412, "8E4F1B22-9C3D-4A55", "hmac-access-0123456"
    x_vin, jwt = "c2VjcmV0LXZlaGljbGUtdG9rZW4tdmFsdWU=", "jwt-session-0002-held"
    app_secret, latitude = "app-secret-0123456", "32.0853123"

    class Coord:
        last_exception = Exception(f"status read failed for {user_id}: {x_vin}")
        last_update_success = False
        update_interval = None
        data = {"_state": {"note": f"{FAKE_VIN} last seen by {EMAIL} on {idfa}",
                           "why": "cannot reach the network"},
                FAKE_VIN: {"odometer": "1234"},
                "_trip": {"sent": f"session {jwt}, key {hmac}, secret "
                                  f"{app_secret}, at {latitude}"}}

    class Api:
        command_trail = [{"command": "RCE_2 start", "outcome": "error",
                          "message": f"refused for {TOKEN}"}]

        def hidden(self):
            return (jwt,)

    class Entry:
        entry_id = "e1"
        data = MappingProxyType({
            "vin": FAKE_VIN, "email": EMAIL, "cidpsso_token": TOKEN,
            "user_id": user_id, "device_idfa": idfa,
            "eu_app_keys": [{"eu": hmac}], "appSecret": app_secret,
            "latitude": latitude,
            "zeekr_password": "network", "poll_mode": "manual"})
        options = MappingProxyType({"zeekr_enc_vin": x_vin})

    class Hass:
        data = {"geely_connect": {"e1": {
            "coordinator": Coord(), "api": Api(),
            "capabilities_raw": [{"functionId": "remote_climate_control_2",
                                  "functionName": f"climate for {FAKE_VIN}"}]}}}

    r = asyncio.run(diag.async_get_config_entry_diagnostics(Hass(), Entry()))
    for text in _strings(r):
        for value in (FAKE_VIN, EMAIL, TOKEN, str(user_id), idfa, hmac, x_vin, jwt,
                      app_secret, latitude):
            assert value not in text, f"{value!r} survived in {text!r}"
    assert "last seen by" in r["status"]["_state"]["note"]
    assert r["status"]["_state"]["why"] == "cannot reach the network", \
        "a password was masked by value"
    assert {"odometer": "1234"} in r["status"].values(), "a masked key lost its data"
    assert r["recent_commands"][0]["message"].startswith("refused for "), r["recent_commands"]
    assert r["polling"]["last_exception"].startswith("status read failed for ")
    assert r["capabilities_raw"][0]["functionId"] == "remote_climate_control_2"


def test_a_value_the_cut_falls_through_is_masked_before_it():
    """A row and the last error are cut to 300 characters. Cut first, a value
    straddling the cut would leave a prefix no mask can match."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    words = "x" * 280 + " " + TOKEN

    class Api:
        command_trail: list = []

        def charge_server_get(self, slot):
            raise RuntimeError(words)

    class Coord:
        last_exception = RuntimeError(words)
        last_update_success = False
        update_interval = None
        data: dict = {}

    diag = load("diagnostics")

    class Entry:
        entry_id = "e1"
        data = MappingProxyType({"vin": FAKE_VIN, "cidpsso_token": TOKEN})
        options = MappingProxyType({})

    class Hass:
        data = {"geely_connect": {"e1": {"api": Api(), "coordinator": Coord()}}}

        async def async_add_executor_job(self, fn, *args):
            return fn(*args)

    r = asyncio.run(diag.async_get_config_entry_diagnostics(Hass(), Entry()))
    # The row's cut leaves five characters of the token, the last error's
    # nineteen.
    for text in (r["charge_server"]["1"]["error"], r["polling"]["last_exception"]):
        assert len(text) == 300 and text.endswith("XXXXX"), text[-25:]
        assert TOKEN[:5] not in text, text[-25:]


def test_a_token_minted_during_the_sweep_is_masked_too():
    """A slot read can renew the session (the Geely EM adapter's
    _authed_charge). Read once before the sweep, the values held the token
    the session had then: a later answer naming the new one carried it
    whole, and so did a later row quoting it - refused for a line break,
    as http.client words it - where the row's cut fell through it, which
    leaves a prefix no later mask can match. An answer from before the
    renewal names the old one, which the client no longer holds."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    old, first, second = "fake-access-first-0001", "fake-access-second", "second-half"

    class Api:
        command_trail: list = []
        token = old

        def hidden(self):
            return (self.token,)

        def charge_server_get(self, slot):
            if slot == "1":                     # renews the session as it reads
                said, self.token = self.token, f"{first}\n{second}"
                return {"code": "1000", "data": {"note": f"for {said!r}"}}
            if slot == "2":
                return {"code": "1000", "data": {"note": f"for {self.token!r}"}}
            # "ValueError: " and the padding put the token at 286, across the
            # cut at 300.
            raise ValueError("x" * 271 + f" {self.token.encode()!r}")

    rows = _swept(Api())["charge_server"]
    for slot, value in (("1", old), ("2", first)):
        note = rows[slot]["data"]["note"]
        assert note.startswith("for '") and value not in note, note
    for slot in ("3", "4", "5", "6", "8"):
        text = rows[slot]["error"]
        assert len(text) == 300 and text.startswith("ValueError: xxx"), text[:20]
        assert "fake-acc" not in text, text[-40:]


def test_a_token_a_read_went_out_with_is_masked_after_another_renewal():
    """A renewal can land while a read is out - a poll's, say - and the
    read's error quotes the token it went out with, which the client holds
    no longer, nor did it before the sweep. The values are taken as each
    read goes out, a failed one's included, and the whole report is masked
    with all of them and with those held once the sweep is over. A read
    can renew too and have its retry refused, quoting the token it was
    renewed to, which is read as the row is written - or answer naming
    it, as the last slot does here."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    t1 = "fake-access-mid\nmid-half"        # minted by slot 1's read
    t4 = "fake-access-tail\ntail-half"      # minted by the last read

    class Api:
        command_trail: list = []
        token = "fake-access-first-0001"

        def hidden(self):
            return (self.token,)

        def charge_server_get(self, slot):
            if slot == "1":                     # renews, then fails
                self.token = t1
                raise RuntimeError("the read was refused")
            if slot == "2":                     # renewed while this read is out
                sent, self.token = self.token, "fake-access-last-0003"
                raise ValueError("x" * 271 + f" {sent.encode()!r}")
            if slot == "3":
                return {"code": "1000", "data": {"note": f"for {t1!r}"}}
            if slot == "4":                     # renews, and its retry is refused
                self.token = "fake-access-own\nown-half"
                raise ValueError("x" * 271 + f" {self.token.encode()!r}")
            if slot == "8":                     # renews, and answers naming it
                self.token = t4
                return {"code": "1000", "data": {"note": f"for {t4!r}"}}
            raise NotImplementedError("not mapped")

    rows = _swept(Api())["charge_server"]
    assert rows["1"]["error"] == "RuntimeError: the read was refused", rows["1"]
    for slot in ("2", "4"):
        text = rows[slot]["error"]
        assert len(text) == 300 and "fake-acc" not in text, (slot, text[-40:])
    for slot in ("3", "8"):
        note = rows[slot]["data"]["note"]
        assert note.startswith("for '") and "fake-acc" not in note, (slot, note)


def test_a_client_whose_hidden_values_cannot_be_read_still_gives_a_report():
    """The values the entry keeps are masked all the same."""
    if not have_homeassistant():
        skip("homeassistant not installed")

    class Api:
        command_trail = [{"command": "RCT start", "outcome": "error",
                          "message": f"refused for {FAKE_VIN}"}]

        def charge_server_get(self, slot):
            raise RuntimeError(f"illegal request parameter for {FAKE_VIN}")

        def hidden(self):
            raise RuntimeError("no values")

    r = _swept(Api())
    assert FAKE_VIN not in r["recent_commands"][0]["message"], r["recent_commands"]
    rows = r["charge_server"]
    assert set(rows) == {"1", "2", "3", "4", "5", "6", "8"}, rows
    for row in rows.values():
        assert row["error"].startswith("RuntimeError: illegal request parameter for "), row
        assert FAKE_VIN not in row["error"], row
