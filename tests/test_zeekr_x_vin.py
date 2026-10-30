"""x-vin derivation: VIN -> AES-128-CBC/PKCS7 -> Base64, per known app build,
verified against the gateway before use.

Vectors use an obviously fake VIN only; no real VIN, x-vin, key provenance
beyond the public material, or captured request is in this file.
"""
from __future__ import annotations

import base64
import logging

from conftest import load

zc = load("zeekr_client")

FAKE_VIN = "TESTVIN0000000001"          # 17 chars, not a real VIN
# Pins the two shipped (key, iv) pairs so a silent constant change is caught.
_VECTORS = {
    b"a01a6db985a2f5d4": "NC5s9vGCMCIqeBTHAf2obrtzAkYDBU8zWHPJPQ0/5NM=",
    b"2a25d6c112dcf841": "vXhc1YYp6iF/RD27Bhn5RNwmARwFRTr9A1wiKclIukU=",
}


def test_derive_x_vin_matches_the_pinned_vectors():
    for key, iv, _note in zc._X_VIN_MATERIAL:
        assert zc.derive_x_vin(FAKE_VIN, key, iv) == _VECTORS[key], key


def test_derive_x_vin_is_a_reversible_aes_cbc_pkcs7():
    """Independent of any pinned vector: decrypting with the same material must
    return the VIN, i.e. it really is AES-128-CBC with PKCS7 padding."""
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    for key, iv, _note in zc._X_VIN_MATERIAL:
        ct = base64.b64decode(zc.derive_x_vin(FAKE_VIN, key, iv))
        dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        padded = dec.update(ct) + dec.finalize()
        unpad = padding.PKCS7(128).unpadder()
        assert (unpad.update(padded) + unpad.finalize()).decode() == FAKE_VIN


# The gateway's own answer to a candidate it cannot decrypt, verified live.
_REJECTED = "HTTP 400: 079025 Decrypt X-VIN failed"
# ...its answer once the phone app has taken the session...
_EVICTED = "code=079021 message=The account is currently logged in elsewhere"
# ...and the 401 a decrypted token drew on a live session (#63, as logged).
_REFUSED = "HTTP 401: {'code': '079001', 'msg': '[SDK]此接口未被授权，无法访问!'}"


def _client_with(accepting_x_vin, reject_with=_REJECTED):
    """A logged-in client whose capability call only succeeds for one x-vin."""
    client = zc.ZeekrClient(email="a@b.c", password="")
    client.access_token = "tok"
    def fake_caps():
        if client.enc_vin != accepting_x_vin:
            raise zc.ZeekrApiError(reject_with)
        return [{"functionCode": "x"}]
    client.capabilities_new = fake_caps
    return client


def test_probe_returns_the_value_the_gateway_accepts():
    key, iv, _ = zc._X_VIN_MATERIAL[-1]         # the AU/SEA build
    accepted = zc.derive_x_vin(FAKE_VIN, key, iv)
    client = _client_with(accepted)
    client.enc_vin = "previous-value"
    assert client.probe_x_vin(FAKE_VIN) == accepted
    # the probe must not disturb whatever x-vin was already set
    assert client.enc_vin == "previous-value"


def test_probe_returns_empty_when_no_build_matches():
    client = _client_with("something-no-build-produces")
    client.enc_vin = "keep-me"
    assert client.probe_x_vin(FAKE_VIN) == ""
    assert client.enc_vin == "keep-me"


def test_probe_rejects_an_empty_catalogue_even_without_a_raise():
    """Acceptance is positive: a candidate the gateway answered with an empty
    catalogue (a hypothetical HTTP 200 + no rows, no raise) must NOT be stored,
    so a wrong x-vin can never be false-accepted."""
    client = zc.ZeekrClient(email="a@b.c", password="")
    client.access_token = "tok"
    client.capabilities_new = lambda: []      # never raises, always empty
    client.enc_vin = "keep-me"
    assert client.probe_x_vin(FAKE_VIN) == ""
    assert client.enc_vin == "keep-me"


def test_probe_needs_a_session_and_a_vin():
    client = _client_with("x")
    client.access_token = None
    assert client.probe_x_vin(FAKE_VIN) == ""
    client.access_token = "tok"
    assert client.probe_x_vin("") == ""


# --------------------------------------------------- #84: say what happened

class _Logs(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


def _capture(fn):
    h = _Logs()
    logger = logging.getLogger(zc.__name__)
    old = logger.level
    logger.addHandler(h)
    logger.setLevel(logging.DEBUG)
    try:
        return fn(), h.records
    finally:
        logger.removeHandler(h)
        logger.setLevel(old)


def test_probe_logs_each_candidate_and_summarises_when_none_matches():
    """An owner on an app build nobody has captured (#84, iOS) saw nothing: the
    flow swallowed every rejection and stored "" in silence. Now each candidate's
    verdict is at debug and the summary at info, and neither line carries the
    VIN or a candidate value.

    The summary gives no advice. It used to warn "paste the token" at every
    start, and for a car the new gateway does not hold, the only kind seen to
    have no token, a pasted one turns every read into 079001 (#106). Setup
    reads the garage after the probe, so the warning is setup's to give."""
    client = _client_with("something-no-build-produces")
    got, records = _capture(lambda: client.probe_x_vin(FAKE_VIN))
    assert got == ""
    assert client.car_refused is False, "every key refused is not the car refused"
    debug = [r.getMessage() for r in records if r.levelno == logging.DEBUG]
    assert len(debug) == len(zc._X_VIN_MATERIAL), debug
    for note_line, (_k, _iv, note) in zip(debug, zc._X_VIN_MATERIAL):
        assert note in note_line and "079025" in note_line, note_line
    assert not [r for r in records if r.levelno >= logging.WARNING], \
        [r.getMessage() for r in records]
    info = [r.getMessage() for r in records if r.levelno == logging.INFO]
    assert len(info) == 1, info
    assert "079025" in info[0] and "retried" in info[0], info[0]
    assert "Configure" not in info[0] and "Vehicle token" not in info[0], info[0]
    # The summary is kept for setup's warning, which a default log shows.
    assert client.probe_summary and client.probe_summary in info[0], client.probe_summary
    for _k, _iv, note in zc._X_VIN_MATERIAL:
        assert note in client.probe_summary, client.probe_summary
    for line in debug + info + [client.probe_summary]:
        assert FAKE_VIN not in line, line
        for key, iv, _n in zc._X_VIN_MATERIAL:
            assert zc.derive_x_vin(FAKE_VIN, key, iv) not in line, line


def test_an_accepted_candidate_is_logged_and_raises_no_warning():
    key, iv, note = zc._X_VIN_MATERIAL[-1]
    client = _client_with(zc.derive_x_vin(FAKE_VIN, key, iv))
    client.probe_summary = "left over from an earlier probe"
    got, records = _capture(lambda: client.probe_x_vin(FAKE_VIN))
    assert got
    assert client.probe_summary == "", "a match leaves nothing to warn about"
    assert not [r for r in records if r.levelno == logging.WARNING]
    accepted = [r.getMessage() for r in records if "accepted" in r.getMessage()]
    assert len(accepted) == 1 and note in accepted[0], accepted


def test_a_dead_session_raises_instead_of_reading_as_no_build_matched():
    """A probe that saw only `079021` has learned nothing about any key - and ""
    would have raised a Repairs issue about a token on an entry whose real
    problem is the session. The adapter's renewal wrapper is what the raise is
    for."""
    client = _client_with("never", reject_with=_EVICTED)
    client.enc_vin = "keep-me"
    try:
        client.probe_x_vin(FAKE_VIN)
    except zc.ZeekrApiError as e:
        assert "079021" in str(e)
    else:
        raise AssertionError("a dead session read as 'no build matched'")
    assert client.enc_vin == "keep-me", "the raise must still restore the x-vin"
    # A ZeekrAuthError is a dead session by definition.
    client2 = zc.ZeekrClient(email="a@b.c", password="")
    client2.access_token = "tok"
    def no_session():
        raise zc.ZeekrAuthError("not logged in (no new-platform session)")
    client2.capabilities_new = no_session
    try:
        client2.probe_x_vin(FAKE_VIN)
    except zc.ZeekrAuthError:
        pass
    else:
        raise AssertionError("an auth error read as 'no build matched'")


def test_a_candidate_that_met_a_dead_session_was_never_judged():
    """#63 logged 079025 for one build and 079021 for the other in one probe: a
    wrong key looks to be refused before the session is checked, so the build
    that met the eviction was never judged. "" there raised a Repairs issue
    about a key nobody had tried; the probe raises instead, so the adapter
    renews and asks again."""
    client = zc.ZeekrClient(email="a@b.c", password="")
    client.access_token = "tok"
    client.enc_vin = "keep-me"
    answers = iter([_REJECTED, _EVICTED])
    def caps():
        raise zc.ZeekrApiError(next(answers))
    client.capabilities_new = caps
    try:
        client.probe_x_vin(FAKE_VIN)
    except zc.ZeekrApiError as e:
        assert "079021" in str(e), "the raise must carry the session's answer"
    else:
        raise AssertionError("a build that met a dead session read as rejected")
    assert client.enc_vin == "keep-me"
    # Both judged - one refused outright, one decrypted and then refused (#63
    # on a live session) - is a verdict, not a raise: "" with the car marked
    # as turned away (the next test has what that changes).
    answers = iter([_REJECTED, _REFUSED])
    assert client.probe_x_vin(FAKE_VIN) == ""
    assert client.car_refused is True
    # And an empty catalogue is a verdict as well.
    client.capabilities_new = lambda: []
    assert client.probe_x_vin(FAKE_VIN) == ""
    assert client.car_refused is False


def test_a_car_the_gateway_turns_away_is_told_no_token_to_paste():
    """#63, #84 and #106: one build drew 079025, the other got past the decrypt
    check and drew 079001 - the gateway turning the CAR away. On the cars seen
    so far the app reads such a car by its plain VIN on the old route, so the
    warning's "paste the token from your app" sent owners after something that
    is not there. The probe says what happened at info instead, with no
    warning, and marks the verdict, which keeps setup from raising Repairs."""
    client = zc.ZeekrClient(email="a@b.c", password="")
    client.access_token = "tok"
    answers = iter([_REJECTED, _REFUSED])

    def caps():
        raise zc.ZeekrApiError(next(answers))

    client.capabilities_new = caps
    got, records = _capture(lambda: client.probe_x_vin(FAKE_VIN))
    assert got == "" and client.car_refused is True
    assert client.probe_summary == "", "setup gets nothing to warn with"
    assert not [r for r in records if r.levelno >= logging.WARNING], records
    info = [r.getMessage() for r in records if r.levelno == logging.INFO]
    assert len(info) == 1, info
    assert "079001" in info[0] and "retried" in info[0], info[0]
    assert "Configure" not in info[0] and "Vehicle token" not in info[0], info[0]
    assert FAKE_VIN not in info[0]
    for key, iv, _n in zc._X_VIN_MATERIAL:
        assert zc.derive_x_vin(FAKE_VIN, key, iv) not in info[0]
    # The mark is the last probe's verdict, not a latch: one that met only
    # wrong keys takes it off, and so does one with no session to ask on.
    def wrong_key():
        raise zc.ZeekrApiError(_REJECTED)

    client.capabilities_new = wrong_key
    assert client.probe_x_vin(FAKE_VIN) == "" and client.car_refused is False
    assert "079025" in client.probe_summary, client.probe_summary
    client.car_refused = True
    client.access_token = None
    assert client.probe_x_vin(FAKE_VIN) == "" and client.car_refused is False
    assert client.probe_summary == "", "a probe that tried no build has no summary"


def test_the_two_verdict_predicates():
    assert zc._x_vin_rejected(zc.ZeekrApiError(_REJECTED))
    assert not zc._session_dead(zc.ZeekrApiError(_REJECTED))
    assert zc._session_dead(zc.ZeekrApiError(_EVICTED))
    assert zc._session_dead(zc.ZeekrAuthError("anything"))
    assert not zc._session_dead(zc.ZeekrApiError("HTTP 500: gateway hiccup"))
    # A 401 is not always the session: 079001 comes as one on a live session.
    assert not zc._session_dead(zc.ZeekrApiError(_REFUSED))
    assert not zc._x_vin_rejected(zc.ZeekrApiError(_REFUSED))
    assert zc._session_dead(zc.ZeekrApiError("HTTP 401: token expired"))
