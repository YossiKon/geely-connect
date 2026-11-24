"""A VIN named in another case than the one held here is left out of error
text, the log and the diagnostics report as the VIN itself is.

Setup takes a VIN that differs only in letter case for the same car, and a
re-authentication keeps the entry's spelling (#126), so a gateway can name
the car in a spelling no copy held here has. Whoever holds a VIN marks it
(api.AnyCase), hide_values matches a marked one in any case, and
_shorten_vin shortens a VIN in any case. Each path is driven through the
real code that words the text: the clients, the setup flow, the report.
"""
import asyncio
import datetime
import json
from types import MappingProxyType

from conftest import FAKE_VIN, have_homeassistant, load
from run import skip

api = load("api")

# Invented, 17 characters, with letters among its last four so that the
# shortened form shows the case it stood in.
VIN = "TESTVIN000000ABCD"
# FAKE_VIN as a gateway might write it back.
LOWER = FAKE_VIN.lower()


def _spellings(vin):
    """`vin` in other cases: all lower, and lower for its first half only."""
    half = len(vin) // 2
    return (vin.lower(), vin[:half].lower() + vin[half:])


def test_a_marked_vin_is_hidden_in_any_case_with_or_without_an_address():
    """An address's case-blind match runs only in a text with an @ in it; a
    VIN's runs in every text."""
    for said in _spellings(VIN):
        for text in (f"vehicle {said} is unknown",
                     f"vehicle {said} is not bound to owner@example.invalid"):
            out = api.hide_values(text, [api.AnyCase(VIN)])
            assert out == text.replace(said, api._mask(said)), (text, out)
            assert len(out) == len(text), out


def test_a_vin_nobody_marked_is_matched_as_it_stands_only():
    """The mark is what makes the match case-blind: a token or an id in
    another case is another value."""
    text = f"vehicle {VIN.lower()} is unknown"
    assert api.hide_values(text, [VIN]) == text
    assert api.hide_values(f"vehicle {VIN}", [VIN]) == f"vehicle {api._mask(VIN)}"


def test_a_plain_copy_beside_the_marked_one_leaves_the_mark_in_force():
    """The report holds the entry's VIN plain, under its key, and marked."""
    text = f"vehicle {VIN.lower()} is unknown"
    for values in ([VIN, api.AnyCase(VIN)], [api.AnyCase(VIN), VIN]):
        assert VIN.lower() not in api.hide_values(text, values), values


def test_a_marked_value_too_short_to_tell_from_a_word_is_left():
    assert api.hide_values("id abc12 here", [api.AnyCase("ABC12")]) == "id abc12 here"


def test_a_marked_vin_goes_in_its_place_in_the_order():
    """Longest first: a longer value holding the VIN is taken out whole, and
    the VIN in another case beside it as well."""
    session = f"{VIN}-session-01"
    text = f"{session} for {VIN.lower()}"
    assert api.hide_values(text, [api.AnyCase(VIN), session]) == \
        f"{api._mask(session)} for {api._mask(VIN)}"


def test_the_vin_is_shortened_in_any_case_and_as_it_stands():
    assert api._shorten_vin(f"car {VIN.lower()} busy, {VIN} too", VIN) == \
        "car ...abcd busy, ...ABCD too"
    assert api._shorten_vin(None, VIN) is None
    assert api._shorten_vin(f"car {VIN}", "") == f"car {VIN}"


def test_the_original_platform_shortens_the_vin_a_refusal_names_in_another_case():
    """The refusal's message is the press's error, and the trail's note."""
    import test_api_client as tac
    a = tac._client_with_jwt()
    tac._scripted_mtls(a, [{"code": "8070", "success": False,
                            "message": f"vehicle {LOWER} is busy"}])
    try:
        a.control("RDL", [{"key": "door", "value": "all"}])
    except api.GeelyControlError as e:
        assert e.message == f"vehicle ...{LOWER[-4:]} is busy", e.message
    else:
        raise AssertionError("a refusal was taken as accepted")
    note = a.command_trail[-1]
    assert note["code"] == "8070" and "is busy" in note["message"], note
    assert LOWER not in json.dumps(note).lower(), note


def test_the_trail_shortens_the_vin_in_any_case():
    import test_api_client as tac
    a = tac._client(vin=VIN)
    for said in _spellings(VIN):
        assert a._note_text(f"reset for {said}") == f"reset for ...{said[-4:]}", said


def test_the_geely_em_client_leaves_out_its_vin_named_in_another_case():
    """Through the real client, only the HTTPS connection replaced: the old
    route's answer and the new gateway's, which resolves the vehicle token
    to the VIN and can name it back."""
    import test_em_error_text as tem
    for said in _spellings(FAKE_VIN):
        c = tem._client()
        words = f"vehicle {said} is unknown"
        e = tem._raises(tem._through, tem._says(200, {"code": 8060, "message": words}),
                        c.vehicle_status_resp, FAKE_VIN)
        assert said not in str(e) and "is unknown" in str(e), e
        e = tem._raises(tem._through, tem._says(500, {"message": words}),
                        c.vehicle_status_new_resp)
        assert said not in str(e) and "is unknown" in str(e), e


def test_the_geely_em_adapter_shortens_its_vin_in_another_case():
    """For the coordinator's warning and an action's error, and in the
    trail of a refused command."""
    import test_em_error_text as tem
    a = tem._adapter()
    for said in _spellings(FAKE_VIN):
        assert a.without_secrets(OSError(f"reset for {said}")) == \
            f"reset for ...{said[-4:]}", said
    e = tem._raises(tem._through,
                    tem._says(200, {"success": False, "code": "079025",
                                    "message": f"bad x-vin for {LOWER}"}),
                    a.control, "RDL_2", [], "start")
    assert e.code == "079025" and LOWER not in e.message, e.message
    note, = a.command_trail
    assert LOWER not in json.dumps(note), note


def test_the_flow_s_warning_leaves_out_the_entry_s_vin_in_another_case():
    """The entry's car may be in neither garage the flow read, and a gateway
    may name it in another case than the entry keeps."""
    from test_config_flow_error_text import _captured
    from test_reauth_vehicle_token import _login, _setup

    cf, flow, entry, client = _setup(vins=[FAKE_VIN])
    own = entry.data["vin"]
    assert own != FAKE_VIN
    said = _spellings(own)
    client.bff = TimeoutError(f"timed out reading {said[0]} ({said[1]})")
    with _captured(cf._LOGGER) as log:
        _login(cf, flow, client)
    assert any("new-platform garage read failed" in line for line in log.lines), log.lines
    for line in log.lines:
        assert own.lower() not in line.lower(), line


def test_the_report_masks_the_entry_s_vin_in_another_case():
    """On the original platform the entry's copy is the only one the report
    holds: that client shortens its VIN rather than hiding it, and text
    raised outside a client was never scrubbed - here, a failed poll."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    diag = load("diagnostics")

    class Coord:
        last_exception = Exception(f"status fetch failed for {LOWER}")
        last_update_success = False
        update_interval = datetime.timedelta(seconds=300)
        data = {"_note": f"read for {LOWER}"}

    class Entry:
        entry_id = "e1"
        data = MappingProxyType({"vin": FAKE_VIN})
        options = MappingProxyType({})

    class Api:
        command_trail = [{"command": "RDL", "outcome": "error",
                          "message": f"reset for {LOWER}"}]

    class Hass:
        data = {"geely_connect": {"e1": {"coordinator": Coord(), "api": Api()}}}

    report = asyncio.run(diag.async_get_config_entry_diagnostics(Hass(), Entry()))
    text = json.dumps(report)
    assert "status fetch failed for" in text and "reset for" in text, text
    assert LOWER not in text.lower(), text


def test_the_geely_em_transport_leaves_out_the_vin_it_was_handed_in_another_case():
    """The module transport on its own, with no client to hand it the VIN
    marked: the VIN it was called with is marked there too."""
    import test_em_error_text as tem
    zc = load("zeekr_client")
    for said in _spellings(FAKE_VIN):
        e = tem._raises(tem._through, tem._says(403, {"message": f"vehicle {said} unknown"}),
                        lambda: zc._hf_request("GET", "/x", token=tem.HF, vin=FAKE_VIN))
        assert said not in str(e) and "unknown" in str(e), e


def test_an_action_names_a_vin_no_loaded_car_has_by_its_last_four_only():
    """Home Assistant logs a failed action's error, and the VIN was the
    owner's own or one typed close to it."""
    from test_platform_commands import _need_ha
    _need_ha()
    from homeassistant.exceptions import ServiceValidationError
    from test_init_lifecycle import _FakeApi, _mod, _service
    m = _mod()
    for given, named in (("L6T00000000000099", "...0099"), ("ABCD", "***")):
        _, run = _service(m, {"e1": {"vin": FAKE_VIN, "api": _FakeApi()}},
                          {"service_id": "RCT", "vin": given})
        try:
            run()
        except ServiceValidationError as e:
            assert str(e) == f"No configured Geely vehicle with VIN {named}", str(e)
        else:
            raise AssertionError(f"an unknown VIN {given!r} did not fail")
