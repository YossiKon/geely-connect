"""A reauthenticated vehicle must never inherit another VIN's gateway token."""

import asyncio
from unittest.mock import patch

from conftest import FAKE_VIN
from test_zeekr_config_flow import _flow, _legacy_entry, _zeekr_login_input


_OTHER_VIN = "L6T00000000000099"
_OLD_TOKEN = "OLD-CAR-TOKEN"
_MANUAL_TOKEN = "OLD-MANUAL-TOKEN"
_NEW_TOKEN = "SELECTED-CAR-TOKEN"


class _Client:
    access_token = "mock-at"
    refresh_token = "mock-rt"
    hf_token = "mock-hf"
    user_id = "mock-uid"

    def __init__(self, vins, result=_NEW_TOKEN):
        self.vins = vins
        self.result = result
        self.probes = []

    def list_vehicles(self, user_id):
        return [{"vin": vin, "nickName": "Mock EX5", "engineType": "BEV"}
                for vin in self.vins]

    def probe_x_vin(self, vin):
        self.probes.append(vin)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _setup(token_source="both", same_vin=False, vins=None, result=_NEW_TOKEN,
           source="reauth"):
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
    client = _Client(vins or [FAKE_VIN], result)
    return cf, flow, entry, client


def _login(cf, flow, client, **overrides):
    with patch.object(cf, "_zeekr_login_password", return_value=client):
        return asyncio.run(flow.async_step_zeekr_login(
            _zeekr_login_input(overrides)))


def _assert_changed_vehicle(entry, client, result, source="reauth"):
    assert result["reason"] == source + "_successful", result
    assert entry.data["vin"] == FAKE_VIN
    assert client.probes == [FAKE_VIN], (entry.data, entry.options, client.probes)
    assert entry.data.get("zeekr_enc_vin") == _NEW_TOKEN, entry.data
    assert entry.options == {"full_exposure": True}, entry.options
    assert entry.unique_id == f"zeekr:user@example.com:{FAKE_VIN}"


def test_changed_vin_replaces_stored_token_using_selected_vehicle_probe():
    for source in ("reauth", "reconfigure"):
        cf, flow, entry, client = _setup(token_source="data", source=source)
        result = _login(cf, flow, client)
        _assert_changed_vehicle(entry, client, result, source)


def test_changed_vin_discards_manual_token_and_probes_even_with_stored_token():
    for token_source in ("options", "both"):
        cf, flow, entry, client = _setup(token_source=token_source)
        result = _login(cf, flow, client)
        _assert_changed_vehicle(entry, client, result)


def test_changed_vin_picker_probes_only_the_selected_vehicle():
    cf, flow, entry, client = _setup(vins=[_OTHER_VIN, FAKE_VIN])
    before_data, before_options = dict(entry.data), dict(entry.options)
    picker = _login(cf, flow, client)
    assert picker["step_id"] == "zeekr_pick", picker
    assert client.probes == []
    assert entry.data == before_data and entry.options == before_options
    result = asyncio.run(flow.async_step_zeekr_pick({"vin": FAKE_VIN}))
    _assert_changed_vehicle(entry, client, result)


def test_changed_vin_failed_probe_clears_both_token_sources():
    for probe_result in ("", RuntimeError("offline fake probe failure")):
        for token_source in ("data", "options", "both"):
            cf, flow, entry, client = _setup(token_source=token_source,
                                            result=probe_result)
            result = _login(cf, flow, client)
            assert result["reason"] == "reauth_successful", result
            assert entry.data["vin"] == FAKE_VIN
            assert client.probes == [FAKE_VIN], (token_source, client.probes)
            assert "zeekr_enc_vin" not in entry.data, entry.data
            assert entry.options == {"full_exposure": True}, entry.options


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


def test_wrong_account_does_not_clear_vehicle_tokens_or_change_vin():
    cf, flow, entry, client = _setup()
    before_data, before_options = dict(entry.data), dict(entry.options)
    result = _login(cf, flow, client, email="another@example.com")
    assert result["reason"] == "reauth_account_mismatch", result
    assert entry.data == before_data and entry.options == before_options
