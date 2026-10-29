"""The ZeekrAdapter: renewal/retry orchestration in front of ZeekrClient.

The adapter only orchestrates - its client internals are covered by
test_zeekr_client.py - so this file stubs the client methods and the IDaaS
login and drives the adapter's branches directly. Runs without Home
Assistant (api.py is stdlib-only).
"""

import json

from conftest import FAKE_VIN, load

zc = load("zeekr_client")
ad = load("zeekr_adapter")

_ORIGINAL_IDAAS_CLASS = zc.ZeekrIdaas


class _StubIdaas:
    """Stand-in for ZeekrIdaas: 'mock-tv' token, or an auth failure."""

    def __init__(self, country: str = "AU", profile=None):
        self.country = country

    def login_by_email_password(self, email: str, password: str) -> str:
        if password == "wrong-password":
            raise zc.ZeekrAuthError("login rejected")
        return "mock-tv"


def _make_adapter(password: str = "", hf_token: str = "mock-hf",
                  hf_expiry: int = 10 ** 15) -> ad.ZeekrAdapter:
    """Adapter with a stubbed client surface; returns (adapter, client)."""
    a = ad.ZeekrAdapter(
        email="user@example.com", vin=FAKE_VIN, user_id="mock-uid",
        access_token="mock-at", refresh_token="mock-rt",
        hf_token=hf_token, vehicle_model="E245-J1",
        password=password, country_code="AU", timezone="UTC",
        hf_expiry=hf_expiry, gateway="https://unused.invalid")
    c = a._client
    c.hf_token = hf_token or None
    c.vehicle_status_resp = lambda vin, user_id=None: {
        "code": "1000", "data": {"vehicleStatus": {"basicVehicleStatus": {
            "powerLevel": 98}}}}
    c.control_resp = lambda vin, body: {
        "code": "1000", "data": {"result": {"code": 1000}}}
    # The old platform's catalogue, in its own row shape.
    c.fetch_capabilities = lambda vin: [
        {"functionId": "remote_control_lock_2", "valueEnable": True}]
    # _renew_hf calls login_tsp, which re-mints BOTH sessions - the HF JWT and
    # the new-platform access token. Renewing only the HF side left
    # access_token frozen and stranded new-platform users on 079021 (the fix
    # that came with the new-gateway status read). The stub mirrors the real
    # superset: it sets the HF token AND a fresh access/refresh pair, so a test
    # can assert the new-platform tokens actually moved. login_hf stays stubbed
    # too, since login_tsp calls it in production.
    c.login_hf = lambda token_value: setattr(c, "hf_token", "mock-hf-new")

    def _login_tsp(token_value):
        c.hf_token = "mock-hf-new"
        c.access_token = "mock-at-new"
        c.refresh_token = "mock-rt-new"

    c.login_tsp = _login_tsp
    return a, c


def _patch_idaas():
    ad.ZeekrIdaas = _StubIdaas


def _restore_idaas():
    ad.ZeekrIdaas = _ORIGINAL_IDAAS_CLASS


def test_constructor_and_basic_surface():
    a, c = _make_adapter()
    assert a.vin == FAKE_VIN
    assert a.user_id == "mock-uid"
    assert a.hf_expiry == 10 ** 15
    assert a.take_renewed_session() is None, "no renewal yet"
    assert c.timezone == "UTC"

    st = a.vehicle_status()
    assert st["data"]["vehicleStatus"]["basicVehicleStatus"]["powerLevel"] == 98, st
    assert a.request_position_refresh()["code"] == "1000"
    ctl = a.control("AC", [{"key": "ac", "value": "1"}])
    assert ctl["data"]["result"]["code"] == 1000, ctl
    assert a.fetch_capabilities() == [
        {"functionId": "remote_control_lock_2", "valueEnable": True}]


def test_unmapped_endpoints_raise_cleanly():
    a, _ = _make_adapter()
    for call in (a.vehicle_status_state,
                 lambda: a.charge_server_get("7"),
                 lambda: a.scheduled_charging_set(vin=FAKE_VIN)):
        try:
            call()
            assert False, f"expected NotImplementedError from {call}"
        except NotImplementedError:
            pass


def test_the_adapter_says_which_endpoints_it_cannot_serve():
    """Raising is how a call learns it; the platforms have to know before
    they build an entity on one of these, and the poll before it asks (#88)."""
    a, c = _make_adapter()
    assert a.serves_state is False and a.serves_charge_server is False
    # Same on the x-vin route: neither endpoint is mapped on either.
    c.enc_vin = "opaque-token"
    assert a.serves_state is False and a.serves_charge_server is False


def test_can_send_is_what_control_would_actually_send():
    """Without an x-vin every command goes the legacy route, which takes any
    serviceId. With one, only the captured mappings go out - and control()
    refuses the rest, so can_send must say no to exactly those."""
    a, c = _make_adapter()
    for sid in ("RSM", "RCS", "RDL_2", "RHL"):
        assert a.can_send(sid) is True, sid
    c.enc_vin = "opaque-token"
    assert a.can_send("RDL_2") is True and a.can_send("RHL") is True
    for sid in ("RSM", "RCS"):
        assert a.can_send(sid) is False, sid
        try:
            a.control(sid, [])
            assert False, f"control() sent {sid}, which can_send refused"
        except ad.GeelyControlError:
            pass


def test_hf_expiry_math():
    a, c = _make_adapter(hf_expiry=0)
    assert a._hf_expired() is False, "unknown expiry defers to the authy heuristic"
    c.hf_token = None
    assert a._hf_expired() is True, "no token counts as expired"
    c.hf_token = "mock-hf"
    a._hf_expiry_ts = 10 ** 15  # far future
    assert a._hf_expired() is False
    a._hf_expiry_ts = 1  # long past
    assert a._hf_expired() is True


def test_a_200_wrapped_auth_error_triggers_a_silent_renewal():
    """#33 review C1 payoff: the fix is only real if a 200-wrapped auth error
    actually re-arms renewal. With the clock saying the token is still valid, an
    authy ZeekrApiError from the HF call must drive one _renew_hf + retry, not
    surface as a transient failure."""
    _patch_idaas()
    try:
        a, c = _make_adapter(password="hunter2", hf_token="mock-hf",
                             hf_expiry=10 ** 15)  # clock: NOT expired
        calls = {"n": 0}

        def _status(vin, user_id=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise zc.ZeekrApiError("code=401 message=token expired")
            return {"code": "1000", "data": {"ok": True}}

        c.vehicle_status_resp = _status
        st = a.vehicle_status()
        assert st["data"]["ok"] is True, st
        assert calls["n"] == 2, "the HF call should be retried after renewal"
        assert c.hf_token == "mock-hf-new", "the authy 200-error did not renew"
    finally:
        _restore_idaas()


def test_the_single_session_eviction_now_counts_as_an_auth_error():
    """The adapter's own docstring names `079021 The account is currently logged
    in elsewhere` as the error a renewal exists to recover from - and none of
    the hints matched its text, so it was the one error never retried on."""
    assert ad._looks_authy("code=079021 message=The account is currently logged in elsewhere")
    assert not ad._looks_authy("HTTP 400: 079025 Decrypt X-VIN failed")


def test_probe_x_vin_renews_a_dead_session_and_retries():
    """The client's probe raises on a session-shaped failure precisely so this
    wrapper gets its turn: one renewal, then the probe again with the fresh
    access token, and the derived value comes back (#84)."""
    _patch_idaas()
    try:
        a, c = _make_adapter(password="hunter2", hf_token="mock-hf",
                             hf_expiry=10 ** 15)
        calls = {"n": 0}

        def _probe(vin):
            calls["n"] += 1
            assert vin == FAKE_VIN
            if c.access_token == "mock-at":
                raise zc.ZeekrApiError(
                    "code=079021 message=The account is currently logged in elsewhere")
            return "DERIVED-XVIN=="

        c.probe_x_vin = _probe
        assert a.enc_vin == ""
        assert a.probe_x_vin(FAKE_VIN) == "DERIVED-XVIN=="
        assert calls["n"] == 2, "the probe should be retried after renewal"
        assert c.access_token == "mock-at-new", "renewal did not re-mint the session"
        # A genuine "no build matched" passes through as "".
        c.probe_x_vin = lambda vin: ""
        assert a.probe_x_vin(FAKE_VIN) == ""
        # And adoption puts the token where every new-gateway call reads it.
        a.adopt_enc_vin("DERIVED-XVIN==")
        assert a.enc_vin == "DERIVED-XVIN==" and c.enc_vin == "DERIVED-XVIN=="
        a.adopt_enc_vin("")
        assert a.enc_vin == ""
    finally:
        _restore_idaas()


def test_a_stale_session_at_start_no_longer_hides_a_working_build():
    """The real client probe this time. At start it runs on the token stored in
    the entry; a wrong key still draws 079025 on a dead session, so only the
    right build met the 079021 - and the probe answered "" without this wrapper
    ever renewing (#63 logged exactly that pair). Now one renewal, a second
    pass, and the build is found."""
    _patch_idaas()
    try:
        key, iv, _note = zc._X_VIN_MATERIAL[-1]
        right = zc.derive_x_vin(FAKE_VIN, key, iv)
        refused = ("HTTP 401: {'code': '079001', "
                   "'msg': '[SDK]此接口未被授权，无法访问!'}")

        def _gateway(c, live_answer):
            def caps():
                if c.enc_vin != right:
                    raise zc.ZeekrApiError("HTTP 400: 079025 Decrypt X-VIN failed")
                if c.access_token == "mock-at":         # the stored, dead one
                    raise zc.ZeekrApiError("HTTP 401: code=079021 message="
                                           "The account is currently logged in elsewhere")
                return live_answer()
            return caps

        a, c = _make_adapter(password="hunter2")
        c.capabilities_new = _gateway(c, lambda: [{"functionCode": "C_RDL_2"}])
        assert a.probe_x_vin(FAKE_VIN) == right
        assert c.access_token == "mock-at-new"
        assert c.enc_vin == "", "the probe only adopts through adopt_enc_vin"
        assert a.car_refused is False

        # sabbrowne's car: once the session is live, the same token draws
        # 079001. That is a verdict, not the session, so "" comes back after
        # the one renewal - not a second one and a re-auth prompt.
        a, c = _make_adapter(password="hunter2")

        def _refuse():
            raise zc.ZeekrApiError(refused)

        c.capabilities_new = _gateway(c, _refuse)
        renewals = []
        login_tsp = c.login_tsp
        c.login_tsp = lambda tv: (renewals.append(tv), login_tsp(tv))
        assert a.probe_x_vin(FAKE_VIN) == ""
        assert renewals == ["mock-tv"]
        # The verdict reaches setup through the adapter, and rapid climate
        # goes the way the app sends it to such a car: the charge-server
        # write on the old route, with no token at all (#106).
        assert a.car_refused is True
        sent = []
        c.charge_server_post = lambda vin, body: (
            sent.append(body["bizType"]) or {"code": "1000"})
        assert a.rapid_climate(ac=True, temp="22.0")["code"] == "1000"
        assert sent == [7], sent
    finally:
        _restore_idaas()


def test_silent_renewal_chain_and_token_take():
    _patch_idaas()
    try:
        a, c = _make_adapter(password="hunter2", hf_token="mock-hf",
                             hf_expiry=1)  # expired
        st = a.vehicle_status()
        assert st["data"]["vehicleStatus"]["basicVehicleStatus"]["powerLevel"] == 98, st
        assert c.hf_token == "mock-hf-new", "silent renewal did not run"
        taken = a.take_renewed_session()
        assert taken is not None and taken[0] == "mock-hf-new", taken
        assert taken[1] > 0, "renewal should stamp a fresh expiry"
        # The same login re-minted the new-platform pair; handing back the
        # setup pair would leave the entry on tokens HA has replaced.
        assert taken[2:] == ("mock-at-new", "mock-rt-new"), taken
        assert a.take_renewed_session() is None, "dirty flag cleared"
    finally:
        _restore_idaas()


def test_renewal_requires_the_stored_password():
    a, _ = _make_adapter(password="", hf_expiry=1)  # expired, no password
    try:
        a.vehicle_status()
        assert False, "expected GeelyAuthError without a stored password"
    except ad.GeelyAuthError as e:
        assert "password" in str(e), f"unexpected message: {e}"


def test_authed_retries_once_on_authy_failure():
    _patch_idaas()
    try:
        a, c = _make_adapter(password="hunter2", hf_expiry=10 ** 15)
        calls = {"n": 0}

        def flaky(_vin, _uid=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise zc.ZeekrApiError("code=401 token expired, please login")
            return {"code": "1000", "data": {"ok": True}}

        c.vehicle_status_resp = flaky
        out = a.vehicle_status()
        assert out["data"]["ok"] is True
        assert calls["n"] == 2, "expected exactly one retry"
    finally:
        _restore_idaas()


def test_authed_non_authy_failure_propagates_without_renewal():
    _patch_idaas()
    try:
        a, c = _make_adapter(password="hunter2", hf_expiry=10 ** 15)

        def boom(_vin, _uid=None):
            raise zc.ZeekrApiError("code=8500 server internal error")

        c.vehicle_status_resp = boom
        try:
            a.vehicle_status()
            assert False, "expected ZeekrApiError to propagate"
        except zc.ZeekrApiError:
            pass
    finally:
        _restore_idaas()


def test_authed_renewal_failure_raises_geely_auth_error():
    _patch_idaas()
    try:
        a, c = _make_adapter(password="wrong-password", hf_expiry=10 ** 15)

        def always_authy(_vin, _uid=None):
            raise zc.ZeekrApiError("token expired")

        c.vehicle_status_resp = always_authy
        try:
            a.vehicle_status()
            assert False, "expected GeelyAuthError after a failed renewal"
        except ad.GeelyAuthError:
            pass
    finally:
        _restore_idaas()


def test_authed_retry_non_authy_failure_propagates_not_reauth():
    """A transient failure on the retried call must not trigger reauth."""
    _patch_idaas()
    try:
        a, c = _make_adapter(password="hunter2", hf_expiry=10 ** 15)
        calls = {"n": 0}

        def flaky(_vin, _uid=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise zc.ZeekrApiError("token expired")
            raise zc.ZeekrApiError("code=8500 server hiccup")

        c.vehicle_status_resp = flaky
        try:
            a.vehicle_status()
            assert False, "expected the transient error to propagate"
        except zc.ZeekrApiError as e:
            assert "8500" in str(e), f"unexpected error: {e}"
        assert calls["n"] == 2, "renewal happened, retry hit the gateway error"
    finally:
        _restore_idaas()


def test_control_error_mapping():
    a, c = _make_adapter()
    # ZeekrApiError from the control call becomes GeelyControlError.
    c.control_resp = lambda vin, body: (_ for _ in ()).throw(
        zc.ZeekrApiError("code=8500 control rejected"))
    try:
        a.control("AC")
        assert False, "expected GeelyControlError"
    except ad.GeelyControlError as e:
        assert "8500" in str(e), f"unexpected error: {e}"

    # GeelyAuthError passes straight through (drives the HA reauth flow).
    c.control_resp = lambda vin, body: (_ for _ in ()).throw(
        ad.GeelyAuthError("reauth needed"))
    try:
        a.control("AC")
        assert False, "expected GeelyAuthError"
    except ad.GeelyAuthError:
        pass


def test_a_vehicle_without_the_new_token_reads_the_old_path_unchanged():
    """The safety invariant for every existing user: no x-vin token means the
    new-gateway status read is never touched. `enc_vin` is empty for every
    entry created before this field existed, so vehicle_status() must fall
    straight through to the old-platform call, byte-for-byte as before -
    including the old success code and the old envelope nesting."""
    a, c = _make_adapter()
    assert c.enc_vin == "", "a fresh adapter must default to the old path"
    seen = {"old": 0, "new": 0}

    def _old(vin, user_id=None):
        seen["old"] += 1
        return {"code": "1000", "data": {"vehicleStatus": {"basicVehicleStatus": {
            "powerLevel": 77}}}}

    def _new():
        seen["new"] += 1
        raise AssertionError("the new-gateway read must not run without a token")

    c.vehicle_status_resp = _old
    c.vehicle_status_new_resp = _new
    st = a.vehicle_status()
    assert seen == {"old": 1, "new": 0}
    assert st["code"] == "1000", "the old success code was rewritten"
    assert st["data"]["vehicleStatus"]["basicVehicleStatus"]["powerLevel"] == 77


def test_a_vehicle_with_the_new_token_reads_the_new_gateway():
    """The other side of the gate: once the owner supplies the token, the
    new-gateway read is used, its "000000" is translated to 1000, and its
    flattened payload is re-nested so every downstream consumer is unchanged."""
    a, c = _make_adapter()
    c.enc_vin = "opaque-token"
    c.vehicle_status_new_resp = lambda: {"code": "000000", "data": {
        "basicVehicleStatus": {"powerLevel": 55},
        "additionalVehicleStatus": {"electricVehicleStatus": {}}}}
    st = a.vehicle_status()
    assert st["code"] == 1000, "000000 was not translated"
    assert st["data"]["vehicleStatus"]["basicVehicleStatus"]["powerLevel"] == 55, st


def test_the_capability_catalogue_is_fetched_and_translated():
    a, c = _make_adapter()
    c.enc_vin = "opaque-token"
    c.capabilities_new = lambda: [
        {"functionCode": "remote_climate_control", "paramValueUse": "Y"},
        {"functionCode": "C_PAA_5_1", "paramValueUse": "Y"},
        {"functionCode": "C_PAA_6", "paramValueUse": "Y"},
    ]
    entries = a.fetch_capabilities()
    climate = [e for e in entries if e["functionId"] == "remote_climate_control_2"]
    assert climate, entries
    params = {p["nameKey"]: p["config"] for p in climate[0]["paramsJson"]}
    assert params["dpt_heat_loc"] == "front-left"
    assert params["steel_wheel_heating"] == "true"


def test_a_catalogue_that_cannot_be_fetched_keeps_every_entity():
    """Losing the catalogue must cost a car its feature *filtering*, never its
    entities - an empty list is capabilities.py's permissive all-features view."""
    a, c = _make_adapter()
    c.enc_vin = "opaque-token"

    def _boom():
        raise zc.ZeekrApiError("code=8500 server internal error")

    c.capabilities_new = _boom
    assert a.fetch_capabilities() == []


def test_an_entry_without_a_token_reads_the_old_platform_catalogue():
    """Without an x-vin the car is read from the old platform, and its catalogue
    now is too - in that platform's own row shape, which capabilities.py parses
    as it stands. Before, this entry got no catalogue and every feature was
    created whether the car has it or not (#63)."""
    a, c = _make_adapter()
    assert c.enc_vin == ""
    asked = []
    rows = [{"functionId": "remote_climate_control_2", "valueEnable": True,
             "paramsJson": [{"nameKey": "dpt_vent_loc", "config": "front-left"}]},
            {"functionId": "remote_control_skylight_2", "valueEnable": True},
            {"valueEnable": True}]

    def _old(vin):
        asked.append(vin)
        return rows

    def _new():
        raise AssertionError("the new gateway must not be asked without a token")

    c.fetch_capabilities = _old
    c.capabilities_new = _new
    got = a.fetch_capabilities()
    assert asked == [FAKE_VIN], asked
    assert got == rows[:2], "the rows must pass untranslated, minus the unnamed one"


def test_an_old_platform_catalogue_that_cannot_be_read_keeps_every_entity():
    """A migrated car is unknown to the old platform; that error is not a
    session problem and must not cost a renewal - just the empty catalogue such
    an entry always had. An auth-looking failure rides the same renewal wrapper
    as every other call."""
    _patch_idaas()
    try:
        a, c = _make_adapter(password="hunter2")
        renewals = []
        c.login_tsp = lambda tv: renewals.append(tv)

        def _unknown(vin):
            raise zc.ZeekrApiError(
                "code=8060 message=The vehicle corresponding to the VIN does not exist")

        c.fetch_capabilities = _unknown
        assert a.fetch_capabilities() == []
        assert renewals == [], "a car the platform does not know is not a session problem"

        answers = iter([zc.ZeekrApiError("HF HTTP 401: token expired"),
                        [{"functionId": "remote_control_lock_2", "valueEnable": True}]])

        def _expired_then_fine(vin):
            nxt = next(answers)
            if isinstance(nxt, Exception):
                raise nxt
            return nxt

        c.fetch_capabilities = _expired_then_fine
        assert a.fetch_capabilities() == [
            {"functionId": "remote_control_lock_2", "valueEnable": True}]
        assert renewals == ["mock-tv"], renewals
    finally:
        _restore_idaas()


def test_position_refresh_sends_the_pai_wake_on_the_new_platform():
    """With an x-vin token, request_position_refresh fires the PAI locator
    through the control transport - serviceId PAI, pai=1 - which is what makes
    a new-platform car's map refresh at all."""
    a, c = _make_adapter()
    c.enc_vin = "opaque-token"
    sent = []
    c.control_new_resp = lambda sid, cmd, params=None: (
        sent.append((sid, cmd, params)) or {"code": "000000", "data": {}})
    a.request_position_refresh()
    assert sent == [("PAI", "start", [{"key": "pai", "value": "1"}])], sent


def test_position_refresh_without_a_token_sends_the_captured_old_route_wake():
    """A car the new gateway does not list is read on the old route, and the
    iOS app wakes it there: PUT telematics/<vin> with latest=false, pai=1 alone
    and no operationScheduling (captured 2026-10-28). Nothing goes out on the
    new route without a token."""
    a, c = _make_adapter()   # enc_vin defaults to ""
    called, sent = [], []
    c.control_new_resp = lambda *a_, **k: called.append(1) or {}
    c.control_resp = lambda vin, body: (
        sent.append((vin, body)) or {"code": "1000", "data": {}})
    assert a.request_position_refresh()["code"] == "1000"
    assert called == [], "must not send a new-platform command without a token"
    assert len(sent) == 1, sent
    vin, body = sent[0]
    assert vin == FAKE_VIN
    assert body["serviceId"] == "PAI" and body["command"] == "start"
    assert body["creator"] == "tc" and body["latest"] is False
    assert body["serviceParameters"] == [{"key": "pai", "value": "1"}]
    assert body["userId"] == "mock-uid"
    assert "operationScheduling" not in body


def test_rapid_warm_and_cool_build_the_captured_setsmarttemp_body():
    """Rapid climate uses the setSmartTemp endpoint (serviceId PAA), not the
    control route. Warm carries the seat-heat block and sw=true; cool carries
    an empty ventilation list and sw=false. Both drive the cabin via ac+temp."""
    a, c = _make_adapter()
    c.enc_vin = "opaque-token"
    seen = []
    c.set_smart_temp_new = lambda setting, command="immediately": (
        seen.append((command, setting)) or {"code": "000000", "data": {}})

    resp = a.rapid_climate(ac=True, temp="28.5", heat_seats=["11", "19"],
                           vent_seats=None, vlt=False, sw=True)
    cmd, warm = seen[-1]
    assert cmd == "immediately"
    assert warm["ac"] == "true" and warm["temp"] == "28.5" and warm["sw"] == "true"
    assert warm["heat"] == [{"level": "3", "pos": "11"}, {"level": "3", "pos": "19"}]
    assert "ventilation" not in warm
    assert resp["code"] == 1000, "000000 was not translated"

    a.rapid_climate(ac=True, temp="15.5", heat_seats=None,
                    vent_seats=["11", "19"], vlt=True, sw=None)
    _, cool = seen[-1]
    assert cool["temp"] == "15.5" and cool["sw"] == "false"
    assert cool["ventilation"] == [] and "heat" not in cool


def test_rapid_climate_without_a_token_sends_the_captured_charge_server_body():
    """A car the new gateway does not list takes rapid climate on the old
    route: POST charge-server bizType 7, in the bodies the iOS app was
    captured sending (2026-10-29). Nothing goes to setSmartTemp."""
    a, c = _make_adapter()   # enc_vin defaults to ""
    sent, new_route = [], []
    c.set_smart_temp_new = lambda *a_, **k: new_route.append(1) or {}
    c.charge_server_post = lambda vin, body: (
        sent.append((vin, body)) or {"code": "1000", "success": True})

    resp = a.rapid_climate(ac=True, temp="28.5", heat_seats=["11", "19"],
                           vent_seats=None, vlt=False, sw=True)
    assert resp["code"] == "1000"
    vin, warm = sent[-1]
    assert vin == FAKE_VIN
    assert {k: v for k, v in warm.items() if k != "timestamp"} == {
        "sw": "true", "ac": "true", "ventilation": [], "scheduleList": [],
        "temp": "28.5", "bizType": 7, "paa": "0", "duration": "180",
        "command": "immediately",
        "heat": [{"pos": "11", "level": "3"}, {"pos": "19", "level": "3"}],
    }

    a.rapid_climate(ac=True, temp="15.5", heat_seats=None,
                    vent_seats=["11", "19"], vlt=True, sw=None)
    _, cool = sent[-1]
    assert {k: v for k, v in cool.items() if k != "timestamp"} == {
        "sw": "false", "ac": "true",
        "ventilation": [{"pos": "11", "level": "3"}, {"pos": "19", "level": "3"}],
        "scheduleList": [], "temp": "15.5", "bizType": 7, "paa": "0",
        "duration": "180", "command": "immediately", "heat": [],
    }
    assert "vlt" not in cool, "the app sends no window-vent keys"
    assert new_route == [], "must not use setSmartTemp without a token"
    assert [(n["command"], n["outcome"]) for n in a.command_trail] == [
        ("rapid_climate", "accepted"), ("rapid_climate", "accepted")]


def test_a_safe_command_is_translated_and_its_code_rewritten():
    """control() on the new platform runs the request through the translator
    and rewrites the gateway's 000000 to the 1000 the coordinator expects."""
    a, c = _make_adapter()
    c.enc_vin = "opaque-token"
    sent = []
    c.control_new_resp = lambda sid, cmd, params=None: (
        sent.append((sid, cmd, params)) or {"code": "000000", "data": {}})
    c.control_resp = lambda vin, body: {"code": "1000", "legacy": True}
    resp = a.control("RHL", [{"key": "rhl", "value": "horn-light-flash"}])
    assert sent == [("RHL", "start", [{"key": "rhl", "value": "horn-light-flash"}])]
    assert resp["code"] == 1000 and "legacy" not in resp


def test_the_latest_shape_rides_the_legacy_route_and_not_the_new_one():
    """Without a vehicle token the adapter sends the legacy telematics body, so
    `latest` swaps its scheduling block the way GeelyApi.control does (#72).
    The new route has a body of its own, which the flag leaves alone."""
    a, c = _make_adapter()
    sent = []
    c.control_resp = lambda vin, body: sent.append(body) or {"code": "1000"}
    params = [{"key": "operation", "value": "1"}, {"key": "rlu.target", "value": "1"}]
    a.control("rlu", params, latest=True)
    a.control("rlu", params)
    assert sent[0]["latest"] is True and "operationScheduling" not in sent[0]
    assert sent[0]["serviceId"] == "rlu" and sent[0]["serviceParameters"] == params
    assert "latest" not in sent[1] and sent[1]["operationScheduling"]["duration"] == 0

    c.enc_vin = "opaque-token"
    new = []
    c.control_new_resp = lambda sid, cmd, params=None: (
        new.append((sid, cmd, params)) or {"code": "000000"})
    a.control("RHL", [{"key": "rhl", "value": "horn-light-flash"}], latest=True)
    assert new == [("RHL", "start", [{"key": "rhl", "value": "horn-light-flash"}])]
    assert len(sent) == 2, "a tokened car must not fall back to the legacy body"


def test_control_refuses_an_unmapped_new_platform_service():
    """A service the translator does not know must fail loudly, never send a
    guessed body to the car."""
    a, c = _make_adapter()
    c.enc_vin = "opaque-token"
    c.control_new_resp = lambda *a_, **k: {"code": "000000"}
    try:
        a.control("SOMETHING_NEW", [])
    except ad.GeelyControlError as err:
        assert "not mapped" in str(err)
    else:  # pragma: no cover - only reached on a regression
        raise AssertionError("expected GeelyControlError for an unmapped service")


def test_new_gateway_lists_vehicle_reads_the_garage_for_this_vin():
    """An empty garage, or one holding only other cars, means this car is not
    on the new gateway; records with no recognisable VIN cannot rule it out."""
    a, c = _make_adapter()
    for records, expected in (
            ([], False),
            ([{"vin": "L6T11111111111111"}], False),
            ([{"vin": FAKE_VIN}], True),
            ([{"vehicleInfo": {"vin": FAKE_VIN}}], True),
            ([{"nickname": "no vin here"}], True)):
        c.list_vehicles_bff = lambda r=records: r
        assert a.new_gateway_lists_vehicle() is expected, records


# ------------------------------------------------------ the command trail ---
# What the diagnostics download reads as `recent_commands`. GeelyApi alone
# kept one, so a Geely EM entry's came back empty whatever was pressed - on an
# owner's car, after a climate press that did nothing and one that worked
# (#104).

def test_a_command_on_the_old_route_is_recorded_as_on_the_old_login():
    a, c = _make_adapter()
    sent = []
    c.control_resp = lambda vin, body: sent.append(body) or {"code": "1000"}
    a.control("RCE_2", [{"key": "rce.temp", "value": "22.0"}], "start", 180)
    assert sent, "the command itself must still go out"
    note, = a.command_trail
    assert note["command"] == "RCE_2 start"
    assert note["outcome"] == "accepted" and note["code"] == "1000"
    assert note["detail"] == [{"key": "rce.temp", "value": "22.0"}]
    assert isinstance(note["ms"], int) and note["at"].endswith("Z")


def test_a_refusal_keeps_the_code_the_server_gave():
    """A refusal used to become 8500 whatever the server said, which the trail
    would have repeated: the code is the part worth reading."""
    a, c = _make_adapter()

    def busy(vin, body):
        raise zc.ZeekrApiError(
            "code=8070 message=The last request has not yet been executed")

    c.control_resp = busy
    try:
        a.control("RCE_2", [], "start")
    except ad.GeelyControlError as e:
        assert e.code == "8070"
        assert "has not yet been executed" in e.message
    else:  # pragma: no cover - only reached on a regression
        raise AssertionError("expected GeelyControlError")
    note, = a.command_trail
    assert note["outcome"] == "rejected" and note["code"] == "8070"
    assert "has not yet been executed" in note["message"]

    # An HTTP failure carries no code of its own: the old 8500 stands.
    def bad_gateway(vin, body):
        raise zc.ZeekrApiError("HF HTTP 502: <non-JSON body omitted>")

    c.control_resp = bad_gateway
    try:
        a.control("RDL_2", [], "start")
    except ad.GeelyControlError as e:
        assert e.code == "8500"
    assert a.command_trail[-1]["code"] == "8500"

    # Nor does a refusal the server sent without one: the client words it
    # `code=None`, which is not a code to record.
    def no_code(vin, body):
        raise zc.ZeekrApiError("code=None message=unknown")

    c.control_resp = no_code
    try:
        a.control("RDL_2", [], "start")
    except ad.GeelyControlError as e:
        assert e.code == "8500"
    assert a.command_trail[-1]["code"] == "8500"


def test_a_refused_rapid_climate_keeps_its_code_too():
    """Rapid climate goes out on its own endpoint, not through control(), and
    a refusal there passed through as a bare ZeekrApiError: the trail noted an
    "error" with no code, and the climate entity logged a traceback for a car
    that was only busy. It is a refusal like any other now (#104)."""
    a, c = _make_adapter()
    c.enc_vin = "opaque-token"

    def busy(setting, command="immediately"):
        raise zc.ZeekrApiError(
            "code=8070 message=The last request has not yet been executed")

    c.set_smart_temp_new = busy
    try:
        a.rapid_climate(ac=True, temp="28.5", heat_seats=["11"], sw=True)
    except ad.GeelyControlError as e:
        assert e.code == "8070"
        assert "has not yet been executed" in e.message
    else:  # pragma: no cover - only reached on a regression
        raise AssertionError("expected GeelyControlError")
    note, = a.command_trail
    assert note["command"] == "rapid_climate"
    assert note["outcome"] == "rejected" and note["code"] == "8070"

    # A dead session still goes through the renewal first and comes out as
    # the re-auth error it always was, not as a refusal.
    def dead(setting, command="immediately"):
        raise zc.ZeekrApiError("code=079021 message=logged in elsewhere")

    c.set_smart_temp_new = dead
    try:
        a.rapid_climate(ac=True, temp="28.5", heat_seats=["11"], sw=True)
    except ad.GeelyAuthError:
        pass
    else:  # pragma: no cover - only reached on a regression
        raise AssertionError("expected GeelyAuthError")
    assert a.command_trail[-1]["outcome"] == "session-expired"


def test_a_dead_session_is_recorded_as_one():
    a, c = _make_adapter()

    def dead(vin, body):
        raise ad.GeelyAuthError("reauth needed")

    c.control_resp = dead
    try:
        a.control("RDL_2", [], "start")
    except ad.GeelyAuthError:
        pass
    note, = a.command_trail
    assert note["outcome"] == "session-expired"


def test_the_new_route_and_rapid_climate_are_recorded_too():
    a, c = _make_adapter()
    c.enc_vin = "opaque-token"
    c.control_new_resp = lambda sid, cmd, params=None: {"code": "000000"}
    c.set_smart_temp_new = lambda setting, command="immediately": {"code": "000000"}
    a.control("RDL_2", [], "start")
    a.rapid_climate(ac=True, temp="28.5", heat_seats=["11"], sw=True)
    try:
        a.control("SOMETHING_NEW", [])
    except ad.GeelyControlError:
        pass
    assert [(n["command"], n["outcome"], n["code"]) for n in a.command_trail] == [
        ("RDL_2 start", "accepted", "1000"),
        ("rapid_climate", "accepted", "1000"),
        ("SOMETHING_NEW start", "rejected", "unsupported"),
    ]
    assert a.command_trail[1]["detail"] == {
        "temp": "28.5", "ac": "true", "heat": [{"level": "3", "pos": "11"}],
        "ventilation": None, "sw": "true"}


def test_what_never_reaches_the_server_leaves_no_note():
    """The position wake is the poller's, not anyone's press - the same reason
    the old login leaves it out of its trail - on either route."""
    a, c = _make_adapter()
    a.request_position_refresh()
    c.enc_vin = "opaque-token"
    c.control_new_resp = lambda sid, cmd, params=None: {"code": "000000"}
    a.request_position_refresh()
    assert not getattr(a, "command_trail", None)


def test_neither_the_vin_nor_the_vehicle_token_reaches_the_trail():
    a, c = _make_adapter()
    token = "c2VjcmV0LXZlaGljbGUtdG9rZW4tdmFsdWU="
    c.enc_vin = token

    def refused(sid, cmd, params=None):
        raise zc.ZeekrApiError(f"code=079025 message=bad x-vin {token} for {FAKE_VIN}")

    c.control_new_resp = refused
    try:
        a.control("RDL_2", [], "start")
    except ad.GeelyControlError:
        pass
    blob = json.dumps(list(a.command_trail))
    assert token not in blob and FAKE_VIN not in blob, blob
    note, = a.command_trail
    assert note["code"] == "079025"
    assert "***redacted***" in note["message"]
    assert f"...{FAKE_VIN[-4:]}" in note["message"], "the tail keeps it readable"


def test_a_refused_rapid_climate_without_a_token_keeps_its_code():
    """The charge-server write a tokenless car's rapid climate goes out on
    answers a busy car the same way, and is a refusal like the token route's."""
    a, c = _make_adapter()   # enc_vin defaults to ""

    def busy(vin, body):
        raise zc.ZeekrApiError(
            "code=8070 message=The last request has not yet been executed")

    c.charge_server_post = busy
    try:
        a.rapid_climate(ac=True, temp="28.5", heat_seats=["11"], sw=True)
    except ad.GeelyControlError as e:
        assert e.code == "8070"
        assert "has not yet been executed" in e.message
    else:  # pragma: no cover - only reached on a regression
        raise AssertionError("expected GeelyControlError")
    note, = a.command_trail
    assert note["command"] == "rapid_climate"
    assert note["outcome"] == "rejected" and note["code"] == "8070"
