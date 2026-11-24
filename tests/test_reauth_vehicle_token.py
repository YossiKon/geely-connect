"""Vehicle changes must separate stored and owner-supplied gateway tokens."""

import asyncio
import json
import logging
import os
from unittest.mock import AsyncMock, patch

from conftest import FAKE_VIN
from test_zeekr_config_flow import _flow, _legacy_entry, _zeekr_login_input


_OTHER_VIN = "L6T00000000000099"
_OLD_TOKEN = "OLD-CAR-TOKEN"
_MANUAL_TOKEN = "DISTINCT-MANUAL-TOKEN"
_NEW_TOKEN = "SELECTED-CAR-TOKEN"


class _Client:
    access_token = "mock-at"
    refresh_token = "mock-rt"
    hf_token = "mock-hf"
    user_id = "mock-uid"

    def __init__(self, vins, result=_NEW_TOKEN, bff=None):
        self.vins = vins
        self.result = result
        self.bff = bff if bff is not None else []
        self.probes = []
        self.bff_calls = 0

    @staticmethod
    def _records(values):
        return [v if isinstance(v, dict) else {
            "vin": v, "nickName": "Mock EX5", "engineType": "BEV"}
                for v in values]

    def list_vehicles(self, user_id):
        return self._records(self.vins)

    def list_vehicles_bff(self):
        self.bff_calls += 1
        if isinstance(self.bff, Exception):
            raise self.bff
        return self._records(self.bff)

    def probe_x_vin(self, vin):
        self.probes.append(vin)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _setup(token_source="both", same_vin=False, vins=None, result=_NEW_TOKEN,
           source="reauth", bff=None):
    entry = _legacy_entry()
    entry.data.update({"platform": "zeekr", "zeekr_region": "SEA"})
    entry.options = {"full_exposure": True}
    if same_vin:
        entry.data["vin"] = FAKE_VIN
    if token_source in ("data", "both"):
        entry.data["zeekr_enc_vin"] = _OLD_TOKEN
    if token_source in ("options", "both"):
        entry.options["zeekr_enc_vin"] = _MANUAL_TOKEN
    entry.unique_id = f"zeekr:user@example.com:{entry.data['vin']}"
    cf, flow = _flow([entry])
    flow.context = {"entry_id": entry.entry_id, "source": source}
    if source == "reauth":
        form = asyncio.run(flow.async_step_reauth(entry.data))
    else:
        form = asyncio.run(flow.async_step_reconfigure({"platform": "zeekr"}))
    assert form["step_id"] == "zeekr_login", form
    client = _Client(vins if vins is not None else [FAKE_VIN], result, bff)
    return cf, flow, entry, client


def _login(cf, flow, client, **overrides):
    with patch.object(cf, "_zeekr_login_password", return_value=client):
        return asyncio.run(flow.async_step_zeekr_login(
            _zeekr_login_input(overrides)))


def _pick_changed(cf, flow, entry, client, chosen_vin=FAKE_VIN):
    before_data, before_options = dict(entry.data), dict(entry.options)
    picker = _login(cf, flow, client)
    assert picker["step_id"] == "zeekr_pick", picker
    assert client.probes == [], client.probes
    assert entry.data == before_data and entry.options == before_options
    return asyncio.run(flow.async_step_zeekr_pick({"vin": chosen_vin}))


def test_changed_vin_requires_explicit_pick_then_replaces_stored_and_manual_tokens():
    for source in ("reauth", "reconfigure"):
        for token_source in ("data", "options", "both"):
            cf, flow, entry, client = _setup(token_source=token_source, source=source)
            result = _pick_changed(cf, flow, entry, client)
            assert result["reason"] == source + "_successful", result
            assert entry.data["vin"] == FAKE_VIN
            assert client.probes == [FAKE_VIN]
            assert entry.data.get("zeekr_enc_vin") == _NEW_TOKEN
            assert entry.options == {"full_exposure": True}, entry.options
            assert entry.unique_id == f"zeekr:user@example.com:{FAKE_VIN}"


def test_changed_vin_picker_probes_only_the_selected_vehicle():
    cf, flow, entry, client = _setup(vins=[_OTHER_VIN, FAKE_VIN])
    result = _pick_changed(cf, flow, entry, client)
    assert result["reason"] == "reauth_successful", result
    assert client.probes == [FAKE_VIN]


def test_changed_vin_failed_probe_keeps_manual_token_but_drops_stored_data_token():
    for probe_result in ("", RuntimeError("offline fake probe failure")):
        for token_source in ("data", "options", "both"):
            cf, flow, entry, client = _setup(token_source=token_source,
                                            result=probe_result)
            result = _pick_changed(cf, flow, entry, client)
            assert result["reason"] == "reauth_successful", result
            assert entry.data["vin"] == FAKE_VIN
            assert client.probes == [FAKE_VIN]
            assert "zeekr_enc_vin" not in entry.data, entry.data
            expected = {"full_exposure": True}
            if token_source in ("options", "both"):
                expected["zeekr_enc_vin"] = _MANUAL_TOKEN
            assert entry.options == expected, entry.options


def test_changed_vin_failed_probe_keeps_manual_token_copied_into_data_by_earlier_reauth():
    """An ordinary same-VIN re-auth copies options['zeekr_enc_vin'] into
    data['zeekr_enc_vin']. A later re-point whose probe finds nothing must
    still keep that manual override rather than dropping it."""
    for probe_result in ("", RuntimeError("offline fake probe failure")):
        cf, flow, entry, client = _setup(token_source="options", same_vin=True,
                                         result=probe_result)
        first = _login(cf, flow, client)
        assert first["reason"] == "reauth_successful", first
        assert entry.data["zeekr_enc_vin"] == _MANUAL_TOKEN
        assert entry.options["zeekr_enc_vin"] == _MANUAL_TOKEN

        # Later re-auth where the garage now lists a different VIN.
        cf2, flow2 = _flow([entry])
        flow2.context = {"entry_id": entry.entry_id, "source": "reauth"}
        asyncio.run(flow2.async_step_reauth(entry.data))
        client2 = _Client([_OTHER_VIN], result=probe_result)
        second = _pick_changed(cf2, flow2, entry, client2, chosen_vin=_OTHER_VIN)
        assert second["reason"] == "reauth_successful", second
        assert entry.data["vin"] == _OTHER_VIN
        assert "zeekr_enc_vin" not in entry.data
        assert entry.options == {"full_exposure": True,
                                 "zeekr_enc_vin": _MANUAL_TOKEN}


def test_unchanged_vin_reuses_stored_token_without_probe():
    cf, flow, entry, client = _setup(token_source="data", same_vin=True)
    result = _login(cf, flow, client)
    assert result["reason"] == "reauth_successful", result
    assert client.probes == []
    assert entry.data["zeekr_enc_vin"] == _OLD_TOKEN
    assert entry.options == {"full_exposure": True}


def test_unchanged_vin_preserves_manual_override_without_probe():
    for token_source in ("options", "both"):
        cf, flow, entry, client = _setup(token_source=token_source, same_vin=True)
        before_options = dict(entry.options)
        result = _login(cf, flow, client)
        assert result["reason"] == "reauth_successful", result
        assert client.probes == []
        assert entry.data["zeekr_enc_vin"] == _MANUAL_TOKEN
        assert entry.options == before_options


def test_vin_case_changes_preserve_entity_identity_and_tokens_without_selection():
    from conftest import load
    from test_platform_commands import FakeCoordinator

    lock = load("lock")
    for entry_lower in (False, True):
        cf, flow, entry, client = _setup(same_vin=True)
        if entry_lower:
            entry.data["vin"] = FAKE_VIN.lower()
        else:
            client.vins = [FAKE_VIN.lower()]
        stored_vin = entry.data["vin"]
        stored_unique_id = entry.unique_id
        before = lock.GeelyLock(flow.hass, {
            "vin": stored_vin, "api": object(), "coordinator": FakeCoordinator()})
        result = _login(cf, flow, client)
        assert result["reason"] == "reauth_successful", result
        assert entry.data["vin"] == stored_vin
        assert entry.unique_id == stored_unique_id
        after = lock.GeelyLock(flow.hass, {
            "vin": entry.data["vin"], "api": object(),
            "coordinator": FakeCoordinator()})
        assert before.unique_id == after.unique_id
        assert before.device_info["identifiers"] == after.device_info["identifiers"]
        assert entry.options["zeekr_enc_vin"] == _MANUAL_TOKEN
        assert client.probes == []


def test_new_entry_and_repoint_keep_garages_own_vin_spelling_and_compare_normalized():
    """New entries store the garage's own VIN spelling (and probe with it) so
    runtime exact-string lookups match; duplicate checks compare normalized."""
    lower_vin = FAKE_VIN.lower()
    cf, flow = _flow()
    client = _Client([lower_vin])
    result = _login(cf, flow, client)
    assert result["type"] == "create_entry", result
    assert result["data"]["vin"] == lower_vin
    assert client.probes == [lower_vin]
    assert flow.unique_id == f"zeekr:user@example.com:{lower_vin}"

    # Existing lower-case entry blocks a new setup carrying the upper-case VIN.
    existing = _legacy_entry()
    existing.data.update({"platform": "zeekr", "vin": lower_vin})
    cf2, flow2 = _flow([existing])
    client2 = _Client([FAKE_VIN])
    dup = _login(cf2, flow2, client2)
    assert dup["type"] == "abort" and dup["reason"] == "already_configured", dup

    # Re-pointing an existing entry onto a lower-case garage VIN stores that spelling.
    cf3, flow3, entry3, client3 = _setup(vins=[lower_vin])
    moved = _pick_changed(cf3, flow3, entry3, client3, chosen_vin=lower_vin)
    assert moved["reason"] == "reauth_successful", moved
    assert entry3.data["vin"] == lower_vin
    assert entry3.unique_id == f"zeekr:user@example.com:{lower_vin}"
    assert client3.probes == [lower_vin]


def test_reauth_finds_original_vehicle_across_both_garages():
    for legacy in ([_OTHER_VIN], [{"vin": "invalid\n"}], []):
        cf, flow, entry, client = _setup(same_vin=True, vins=legacy,
                                        bff=[FAKE_VIN.lower()])
        result = _login(cf, flow, client)
        assert result["reason"] == "reauth_successful", result
        assert client.bff_calls == 1
        assert entry.data["vin"] == FAKE_VIN
        assert entry.options["zeekr_enc_vin"] == _MANUAL_TOKEN
        assert client.probes == []


def test_reauth_keeps_legacy_candidates_when_new_garage_is_also_populated():
    cf, flow, entry, client = _setup(vins=[FAKE_VIN], bff=[_OTHER_VIN])
    picker = _login(cf, flow, client)
    assert picker["step_id"] == "zeekr_pick", picker
    assert {cf.vehicle_vin(v) for v in flow._vehicles} == {FAKE_VIN, _OTHER_VIN}
    assert client.probes == []


def test_failing_new_garage_read_is_treated_as_no_extra_records():
    """A failing ms-app-bff read must not abort re-auth with invalid_credentials
    or network_unreachable; legacy records remain available and errors stay
    scrubbed at debug without a traceback."""
    from test_config_flow_error_text import _captured

    for err_factory in (
        lambda cf: cf.ZeekrApiError(f"denied for {FAKE_VIN} / mock-at"),
        lambda cf: cf.ZeekrAuthError(f"expired for {FAKE_VIN} / mock-at"),
        lambda cf: TimeoutError(f"timed out for {FAKE_VIN} / mock-at"),
    ):
        cf, flow, entry, client = _setup(vins=[FAKE_VIN])
        client.bff = err_factory(cf)
        with _captured(cf._LOGGER) as log:
            picker = _login(cf, flow, client)
        assert picker["step_id"] == "zeekr_pick", picker
        assert client.bff_calls == 1
        assert all(r.exc_info is None for r in log.records), log.lines
        debug_lines = [line for r, line in zip(log.records, log.lines)
                       if r.levelno == logging.DEBUG
                       and line.startswith("new-platform garage read failed (")]
        assert len(debug_lines) == 1, log.lines
        assert FAKE_VIN not in debug_lines[0], debug_lines[0]
        assert "mock-at" not in debug_lines[0], debug_lines[0]
        res = asyncio.run(flow.async_step_zeekr_pick({"vin": FAKE_VIN}))
        assert res["reason"] == "reauth_successful", res

    # When both garages are empty / failing, show no_vehicles (not bad password).
    cf, flow, entry, client = _setup(vins=[])
    client.bff = cf.ZeekrApiError("bff unavailable")
    res = _login(cf, flow, client)
    assert res["type"] == "form" and res["errors"] == {"base": "no_vehicles"}, res


def test_normalized_identity_does_not_make_malformed_vins_valid():
    for vin in (FAKE_VIN + "\n", " " + FAKE_VIN, FAKE_VIN + "\r\nInjected: value"):
        cf, flow, entry, client = _setup(vins=[{"vehicleInfo": {"vin": vin}}])
        before_data, before_options = dict(entry.data), dict(entry.options)
        result = _login(cf, flow, client)
        assert result["errors"] == {"base": "no_vehicles"}, result
        assert entry.data == before_data and entry.options == before_options
        assert client.probes == []


def test_other_entry_cannot_be_bypassed_with_lowercase_vin():
    cf, flow, entry, client = _setup(vins=[FAKE_VIN.lower()])
    other = _legacy_entry()
    other.data["vin"] = FAKE_VIN
    flow.hass.config_entries.async_entries = lambda domain: [entry, other]
    result = _login(cf, flow, client)
    assert result["reason"] == "already_configured", result
    assert client.probes == []


def test_wrong_account_does_not_probe_or_change_vehicle_tokens():
    cf, flow, entry, client = _setup()
    before_data, before_options = dict(entry.data), dict(entry.options)
    result = _login(cf, flow, client, email="another@example.com")
    assert result["reason"] == "reauth_account_mismatch", result
    assert entry.data == before_data and entry.options == before_options
    assert client.probes == [], client.probes
    # The final gate also protects direct picker/finish calls.
    flow._zeekr_tokens = ("mock-at", "mock-rt")
    flow._user_id = "mock-uid"
    flow._zeekr_client = client
    result = asyncio.run(flow._finish_zeekr({"vin": FAKE_VIN}))
    assert result["reason"] == "reauth_account_mismatch", result
    assert client.probes == []


def test_changed_token_decisions_warn_without_disclosing_identifiers_or_tokens():
    for probe_result, data_tok, manual in (
            (_NEW_TOKEN, _OLD_TOKEN, _MANUAL_TOKEN),
            ("", _OLD_TOKEN, _MANUAL_TOKEN),
            ("", _MANUAL_TOKEN, _MANUAL_TOKEN)):
        cf, flow, entry, client = _setup(result=probe_result)
        entry.data["zeekr_enc_vin"] = data_tok
        entry.options["zeekr_enc_vin"] = manual
        old_vin = entry.data["vin"]
        with patch.object(cf._LOGGER, "warning") as warning:
            _pick_changed(cf, flow, entry, client)
        messages = " ".join(str(call.args[0]) for call in warning.call_args_list)
        assert "derived" not in messages.lower(), messages
        if data_tok != manual:
            assert "discarded" in messages.lower(), messages
        else:
            assert "discarded" not in messages.lower(), messages
        assert ("replaced" if probe_result else "kept") in messages.lower(), messages
        for secret in (FAKE_VIN, old_vin, _OLD_TOKEN, _MANUAL_TOKEN, _NEW_TOKEN):
            assert secret not in messages, messages


def test_account_mismatch_is_rechecked_before_finishing_a_picker():
    cf, flow, entry, client = _setup()
    before_data, before_options = dict(entry.data), dict(entry.options)
    picker = _login(cf, flow, client)
    assert picker["step_id"] == "zeekr_pick", picker
    flow._email = "another@example.com"
    result = asyncio.run(flow.async_step_zeekr_pick({"vin": FAKE_VIN}))
    assert result["reason"] == "reauth_account_mismatch", result
    assert client.probes == []
    assert entry.data == before_data and entry.options == before_options


def test_legacy_otp_keeps_existing_lowercase_vin_filtered_across_accounts():
    entry = _legacy_entry()
    entry.data.update({"vin": FAKE_VIN.lower(), "email": "other@example.invalid"})
    cf, flow = _flow([entry])
    flow._email = "owner@example.invalid"
    flow._country_code = "AU"
    with patch.object(cf.geely_api, "cidpsso_login", return_value={
            "code": 10000000, "data": {"token": "fake-session", "userId": "fake-user"}}), \
            patch.object(cf.geely_api, "list_vehicles", return_value=[{
                "vin": FAKE_VIN.lower()}]), \
            patch.object(flow, "_finish_with_vehicle", new_callable=AsyncMock,
                         return_value={"type": "unexpected_setup"}) as finish:
        result = asyncio.run(flow.async_step_code({"code": "123456"}))
    assert result.get("reason") == "all_configured", result
    finish.assert_not_called()


def test_zeekr_pick_text_fits_single_car_reauth_as_well_as_multi_car_setup():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    strings = json.load(open(os.path.join(
        root, "custom_components", "geely_connect", "strings.json"),
        encoding="utf-8"))
    desc = strings["config"]["step"]["zeekr_pick"]["description"].lower()
    assert "multiple vehicles" not in desc, desc
    assert "to add" not in desc, desc
