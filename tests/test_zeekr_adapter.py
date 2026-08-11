"""The ZeekrAdapter: renewal/retry orchestration in front of ZeekrClient.

The adapter only orchestrates - its client internals are covered by
test_zeekr_client.py - so this file stubs the client methods and the IDaaS
login and drives the adapter's branches directly. Runs without Home
Assistant (api.py is stdlib-only).
"""

from conftest import FAKE_VIN, load

zc = load("zeekr_client")
ad = load("zeekr_adapter")

_ORIGINAL_IDAAS_CLASS = zc.ZeekrIdaas


class _StubIdaas:
    """Stand-in for ZeekrIdaas: 'mock-tv' token, or an auth failure."""

    def __init__(self, country: str = "AU"):
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
    c.login_hf = lambda token_value: setattr(c, "hf_token", "mock-hf-new")
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
    assert a.take_renewed_hf_token() is None, "no renewal yet"
    assert c.timezone == "UTC"

    st = a.vehicle_status()
    assert st["data"]["vehicleStatus"]["basicVehicleStatus"]["powerLevel"] == 98, st
    # Position wake is a no-op on the new platform until the PAI control write
    # is live-verified - the coordinator fires it automatically, and an unproven
    # auto-write to the car is deliberately withheld.
    assert a.request_position_refresh() == {}
    ctl = a.control("AC", [{"key": "ac", "value": "1"}])
    assert ctl["data"]["result"]["code"] == 1000, ctl
    assert a.fetch_capabilities() == []


def test_unmapped_endpoints_raise_cleanly():
    a, _ = _make_adapter()
    for call in (a.vehicle_status_state,
                 lambda: a.charge_server_get("7"),
                 lambda: a.scheduled_charging_set(vin=FAKE_VIN),
                 a.rapid_climate):
        try:
            call()
            assert False, f"expected NotImplementedError from {call}"
        except NotImplementedError:
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


def test_silent_renewal_chain_and_token_take():
    _patch_idaas()
    try:
        a, c = _make_adapter(password="hunter2", hf_token="mock-hf",
                             hf_expiry=1)  # expired
        st = a.vehicle_status()
        assert st["data"]["vehicleStatus"]["basicVehicleStatus"]["powerLevel"] == 98, st
        assert c.hf_token == "mock-hf-new", "silent renewal did not run"
        taken = a.take_renewed_hf_token()
        assert taken is not None and taken[0] == "mock-hf-new", taken
        assert taken[1] > 0, "renewal should stamp a fresh expiry"
        assert a.take_renewed_hf_token() is None, "dirty flag cleared"
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
