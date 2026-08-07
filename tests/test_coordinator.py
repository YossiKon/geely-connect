"""Coordinator behaviour: what survives a partial failure.

The secondary endpoints (parking comfort, scheduled charging) are fetched only
every Nth cycle. Everything here is about not losing what we already knew.
"""
import ast
import io
import os
import os

from conftest import PKG, load


def _update_body() -> str:
    """Source of the coordinator's _async_update, for structural assertions."""
    tree = ast.parse(io.open(os.path.join(PKG, "__init__.py"), encoding="utf-8").read())
    lines = io.open(os.path.join(PKG, "__init__.py"), encoding="utf-8").read().splitlines()
    for n in ast.walk(tree):
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_async_update":
            return "\n".join(lines[n.lineno - 1:n.end_lineno])
    raise AssertionError("_async_update not found")


def test_last_known_secondary_data_is_carried_forward_unconditionally():
    """A fetch that is attempted and fails must not be worse than one skipped.

    Previously the carry-forward lived in the `else` of the skip gate, so a
    failed fetch dropped `_state` / `_scheduled_charging` entirely - and the
    next cycle read `prev` from that damaged snapshot, so the loss persisted
    until a fetch finally succeeded.
    """
    body = _update_body()
    carry = body.index('data["_state"] = prev["_state"]')
    gate = body.index("charging or was_charging or")
    assert carry < gate, (
        "the carry-forward must run BEFORE the fetch gate, so it also covers "
        "the case where the fetch is attempted and fails"
    )


def test_the_carry_forward_is_not_inside_an_else_branch():
    body = _update_body()
    # There must be no `else:` that only re-instates the previous values.
    assert 'else:\n            if "_state" in prev' not in body


def test_secondary_data_is_only_accepted_when_the_response_is_sane():
    body = _update_body()
    for guard in ("_SUCCESS_CODES", "isinstance(state_resp.get(\"data\"), dict)"):
        assert guard in body, f"missing guard: {guard}"


def test_auth_failure_escalates_but_a_pin_failure_is_loud_not_fatal():
    body = _update_body()
    assert "ConfigEntryAuthFailed" in body
    # A pin failure means a changed server key; it must never be hidden at DEBUG.
    assert body.count("TLS pin check failed") >= 2


def test_poll_signature_ignores_volatile_fields():
    """Otherwise the idle back-off never engages and we poll at full rate."""
    init = None
    tree = ast.parse(io.open(os.path.join(PKG, "__init__.py"), encoding="utf-8").read())
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == "_poll_signature":
            init = n
    assert init is not None, "_poll_signature not found"


def test_manual_mode_gives_the_coordinator_no_update_interval():
    body = io.open(os.path.join(PKG, "__init__.py"), encoding="utf-8").read()
    assert "update_interval=None if _MANUAL else" in body, (
        "manual mode must pass update_interval=None so nothing is scheduled"
    )
    assert "if not _MANUAL:" in body, (
        "the adaptive interval must not be re-applied in manual mode"
    )


def test_removing_the_entry_deletes_the_vehicle_key():
    body = io.open(os.path.join(PKG, "__init__.py"), encoding="utf-8").read()
    assert "async def async_remove_entry" in body, (
        "without this the mTLS private key outlives the integration on disk"
    )
    assert "commonpath" in body, (
        "removal must refuse any path outside the integration's own storage"
    )


def test_fire_control_is_admin_only():
    body = io.open(os.path.join(PKG, "__init__.py"), encoding="utf-8").read()
    assert "async_register_admin_service(hass, DOMAIN, \"fire_control\"" in body, (
        "fire_control forwards an arbitrary serviceId to the car and must be "
        "gated to administrators"
    )


def test_a_manual_refresh_forces_the_secondary_endpoints():
    """The Refresh Data button means "everything, now". Without the force flag
    the cycle counter decided, so three presses in four re-fetched only the
    main status - which made hunting an unmapped field in the vehicle-state
    block a matter of luck (#4)."""
    body = _update_body()
    assert 'poll_state.pop("force_secondary"' in body, (
        "a forced fetch must be able to bypass the cycle counter"
    )
    forced = body.index('poll_state.pop("force_secondary"')
    gate = body.index("charging or was_charging or")
    assert forced < gate, "the flag has to be read before the gate uses it"
    btn = io.open(os.path.join(PKG, "button.py"), encoding="utf-8").read()
    assert '"force_secondary"] = True' in btn, (
        "the Refresh Data button is what sets the flag"
    )
