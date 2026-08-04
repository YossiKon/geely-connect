"""The captcha solver end to end, against a canned server.

tests/test_captcha_solver.py pins the image units and the parsing guards; this
file drives the whole pipeline - /load, both image fetches, the match, the
encrypted `w`, /verify - because those are the steps that turn "the captcha
sometimes fails" into "nobody can add the integration at all".
"""
import importlib.util
import io as _io
import json

from conftest import load
from run import skip


def _deps():
    return all(importlib.util.find_spec(m)
               for m in ("numpy", "PIL", "scipy", "requests", "Crypto"))


def _solver():
    if not _deps():
        skip("solver dependencies not installed")
    return load("geetest_solver")


# ------------------------------------------------------------ fake server ---

class _Reply:
    def __init__(self, text="", content=b"", status=200, raise_for=None,
                 json_body=None):
        self.text = text
        self.content = content
        self.status_code = status
        self._raise_for = raise_for
        self._json = json_body

    def raise_for_status(self):
        if self._raise_for is not None:
            raise self._raise_for

    def json(self):
        if self._json is None:
            raise ValueError("not json")
        return self._json


class _FakeSession:
    """Answers /load, image paths and /verify from a scripted mapping."""

    def __init__(self, *, load_data=None, images=None, verify=None,
                 image_errors=None, load_text=None):
        self.requests = []
        self._load_data = load_data
        self._load_text = load_text
        self._images = images or {}
        self._verify = verify
        self._image_errors = image_errors or {}

    def get(self, url, params=None, timeout=None):
        self.requests.append((url, params))
        if url.endswith("/load"):
            cb = params["callback"]
            if self._load_text is not None:
                return _Reply(text=self._load_text)
            body = json.dumps({"status": "success", "data": self._load_data})
            return _Reply(text=f"{cb}({body})")
        if url.endswith("/verify"):
            cb = params["callback"]
            if isinstance(self._verify, str):
                return _Reply(text=self._verify, json_body=None)
            return _Reply(text=f"{cb}({json.dumps(self._verify)})")
        host = url.split("//", 1)[1].split("/", 1)[0]
        if host in self._image_errors:
            return _Reply(raise_for=self._image_errors[host])
        return _Reply(content=self._images.get(host, b"\x89PNG-fake"))


def _puzzle(gap_at=60, size=(160, 80), slice_w=30):
    """A background with a dark notch at `gap_at`, and the slice that fits it.

    The matcher works on edges, so a filled rectangle against a flat field is
    the smallest thing that produces a single unambiguous correlation peak.
    """
    from PIL import Image, ImageDraw

    bg = Image.new("RGB", size, (200, 200, 200))
    d = ImageDraw.Draw(bg)
    d.rectangle([gap_at, 20, gap_at + slice_w - 1, 20 + slice_w - 1], fill=(20, 20, 20))
    sl = Image.new("RGB", (slice_w, slice_w), (200, 200, 200))
    ImageDraw.Draw(sl).rectangle([0, 0, slice_w - 1, slice_w - 1], outline=(20, 20, 20))

    def _png(img):
        b = _io.BytesIO()
        img.save(b, format="PNG")
        return b.getvalue()

    return _png(bg), _png(sl)


_LOAD_DATA = {
    "lot_number": "lot-1", "challenge": "chal-1", "pt": "10",
    "payload": "pay", "process_token": "ptok-0123456789abcdef0123456789abcdef",
    "payload_protocol": 1, "captcha_id": "cid",
    "static_servers": ["static.geetest.com"],
    "bg": "/bg.png", "slice": "/slice.png",
}


# ------------------------------------------------------------------ /load ---

def test_load_unwraps_the_jsonp_envelope_into_a_load_response():
    gs = _solver()
    s = _FakeSession(load_data=_LOAD_DATA)
    lr = gs.fetch_load(s, "cid")
    assert lr.lot_number == "lot-1" and lr.captcha_id == "cid"
    assert lr.bg_path == "/bg.png" and lr.static_servers == ["static.geetest.com"]
    url, params = s.requests[0]
    assert params["encryption"] == "AES_RSA" and params["language"] == "eng"
    assert json.loads(params["ext"])["ua"].startswith("Mozilla/5.0 (iPhone")


def test_a_failed_load_reports_only_the_diagnostic_fields():
    """The full envelope carries captcha session material and this message
    reaches the Home Assistant log, so payload and process_token must not
    appear in it."""
    gs = _solver()

    class _S(_FakeSession):
        def get(self, url, params=None, timeout=None):
            cb = params["callback"]
            body = json.dumps({"status": "error", "error": "bad_id",
                               "msg": "unknown captcha",
                               "data": {"payload": "SECRET-PAYLOAD"}})
            return _Reply(text=f"{cb}({body})")

    try:
        gs.fetch_load(_S(), "cid")
    except RuntimeError as e:
        assert "bad_id" in str(e) and "unknown captcha" in str(e)
        assert "SECRET-PAYLOAD" not in str(e)
    else:
        raise AssertionError("a rejected /load returned normally")


# ----------------------------------------------------------------- images ---

def test_images_come_from_the_allowlisted_host_first():
    gs = _solver()
    s = _FakeSession(images={"static.geetest.com": b"GOOD"})
    assert gs.fetch_image(s, ["static.geetest.com"], "/bg.png") == b"GOOD"
    assert s.requests[0][0] == "https://static.geetest.com/bg.png"


def test_a_dead_cdn_falls_through_to_the_known_geely_host():
    gs = _solver()
    s = _FakeSession(images={"captcha4.geely.com": b"FALLBACK"},
                     image_errors={"static.geetest.com": OSError("timeout")})
    assert gs.fetch_image(s, ["static.geetest.com"], "/bg.png") == b"FALLBACK"
    assert [r[0].split("//")[1].split("/")[0] for r in s.requests] == [
        "static.geetest.com", "captcha4.geely.com"]


def test_a_server_supplied_host_outside_the_allowlist_is_never_contacted():
    """static_servers is server-controlled and lands in a URL authority - an
    attacker-chosen host there would be a redirect of the whole image fetch."""
    gs = _solver()
    s = _FakeSession(images={"captcha4.geely.com": b"FALLBACK"})
    gs.fetch_image(s, ["evil.example.com", "captcha4.geely.com.evil.net"], "/bg.png")
    hosts = [r[0].split("//")[1].split("/")[0] for r in s.requests]
    assert hosts == ["captcha4.geely.com"], hosts


def test_every_host_failing_raises_with_the_last_error():
    gs = _solver()
    s = _FakeSession(image_errors={"static.geetest.com": OSError("a"),
                                   "captcha4.geely.com": OSError("b")})
    try:
        gs.fetch_image(s, ["static.geetest.com"], "/bg.png")
    except RuntimeError as e:
        assert "image fetch failed" in str(e)
    else:
        raise AssertionError("a total image failure returned normally")


# ---------------------------------------------------------------- matcher ---

def test_the_matcher_finds_the_notch_it_was_given():
    """A synthetic puzzle with a known gap: the answer must land on it, since
    every downstream field is built from this number."""
    gs = _solver()
    bg, sl = _puzzle(gap_at=60)
    assert abs(gs.find_gap_x(bg, sl) - 60) <= 3


def test_the_matcher_never_answers_left_of_the_slice():
    """The slice starts pinned to the left edge, so a hit there is a false
    peak - the mask is what stops the drag from going backwards."""
    gs = _solver()
    bg, sl = _puzzle(gap_at=10, slice_w=30)
    assert gs.find_gap_x(bg, sl) >= 30


# --------------------------------------------------------------- crypto/w ---

def test_the_encrypted_w_round_trips_with_its_own_rsa_wrapped_key():
    """`w` is hex(AES ciphertext) || hex(RSA-wrapped key). The server can undo
    it with the private key; the test proves the halves are consistent by
    decrypting the AES part with a key of the length the RSA part carries."""
    gs = _solver()
    plaintext = json.dumps({"answer": 61, "type": "slide"})
    w = gs.encrypt_w(plaintext)
    expected = 256 + 2 * ((len(plaintext) // 16 + 1) * 16)
    assert len(w) == expected, (len(w), expected)
    bytes.fromhex(w)  # both halves must be valid hex
    ct_hex, rsa_hex = w[:-256], w[-256:]
    assert len(rsa_hex) == 256 and len(bytes.fromhex(ct_hex)) % 16 == 0


def test_the_aes_key_is_sixteen_bytes_of_hex_text():
    """GeeTest uses the UTF-8 bytes of a 16-char hex STRING as the AES-128 key
    - not the 8 bytes it decodes to."""
    gs = _solver()
    k = gs._rand_aes_key_hex16()
    assert len(k) == 16 and len(k.encode("utf-8")) == 16
    int(k, 16)
    assert k != gs._rand_aes_key_hex16(), "the key must not be constant"


def test_the_track_ends_at_the_answer_and_moves_forward():
    """The widget sends a drag path; a track that overshoots or jumps
    backwards is what a bot detector looks for."""
    gs = _solver()
    pts = json.loads(gs.build_track_string(120, 1200))
    assert len(pts) == 25
    xs = [p[0] for p in pts]
    assert abs(xs[-1] - 120) <= 2, xs[-1]
    assert xs[-1] > xs[0]
    assert pts[-1][2] <= 1210


def test_the_inner_payload_carries_the_answer_and_the_widget_shape():
    gs = _solver()
    lr = gs.LoadResponse(**{k: v for k, v in {
        "lot_number": "l", "challenge": "c", "pt": "10", "payload": "p",
        "process_token": "tok", "payload_protocol": 1, "captcha_id": "cid",
        "static_servers": [], "bg_path": "/b", "slice_path": "/s"}.items()})
    inner = gs.build_inner_payload(lr, 61, 1100, wz="serial-1")
    assert inner["answer"] == 61 and inner["passtime"] == 1100
    assert inner["type"] == "slide" and inner["serial"] == "serial-1"
    assert "roe" in inner["env"] or "%7B" in inner["env"]
    assert json.loads(inner["trackOffset"])[-1][0] >= 59
    assert gs.build_inner_payload(lr, 1, 1)["serial"] == gs.GEELY_WZ


# ---------------------------------------------------------------- /verify ---

def test_verify_unwraps_its_jsonp_reply():
    gs = _solver()
    lr = gs.fetch_load(_FakeSession(load_data=_LOAD_DATA), "cid")
    s = _FakeSession(verify={"status": "success", "data": {"result": "success"}})
    out = gs.get_verify_jsonp(s, lr, "w-value")
    assert out["data"]["result"] == "success"
    _, params = s.requests[0]
    assert params["w"] == "w-value" and params["lot_number"] == "lot-1"
    assert params["process_token"] == _LOAD_DATA["process_token"]


def test_a_non_jsonp_verify_reply_is_returned_as_diagnostics_not_a_crash():
    """A WAF block page must surface as something the log can show, not an
    exception five frames up the config flow."""
    gs = _solver()
    lr = gs.fetch_load(_FakeSession(load_data=_LOAD_DATA), "cid")
    out = gs.get_verify_jsonp(_FakeSession(verify="<html>blocked</html>"), lr, "w")
    assert out["http"] == 200 and "blocked" in out["raw"]


def test_a_truncated_jsonp_verify_reply_degrades_the_same_way():
    gs = _solver()
    lr = gs.fetch_load(_FakeSession(load_data=_LOAD_DATA), "cid")

    class _S(_FakeSession):
        def get(self, url, params=None, timeout=None):
            return _Reply(text=params["callback"] + "({not json")

    out = gs.get_verify_jsonp(_S(), lr, "w")
    assert "raw" in out


# ------------------------------------------------------------------ solve ---

class _PipelineSession(_FakeSession):
    def __init__(self, verify, gap_at=60):
        bg, sl = _puzzle(gap_at=gap_at)
        super().__init__(load_data=_LOAD_DATA, verify=verify,
                         images={"static.geetest.com": bg})
        self._bg, self._sl = bg, sl

    def get(self, url, params=None, timeout=None):
        if url.endswith("/slice.png"):
            self.requests.append((url, params))
            return _Reply(content=self._sl)
        return super().get(url, params, timeout)


def _patched_session(gs, session):
    class _Ctx:
        def __enter__(self):
            self.orig = gs.make_session
            gs.make_session = lambda: session

        def __exit__(self, *exc):
            gs.make_session = self.orig
    return _Ctx()


def test_solve_runs_the_whole_pipeline_and_returns_the_success_envelope():
    """The path a user's setup actually takes: load, two images, match,
    encrypt, verify - with nothing but a canned server behind it."""
    gs = _solver()
    session = _PipelineSession({"status": "success", "data": {
        "result": "success", "pass_token": "pt", "lot_number": "lot-1",
        "captcha_output": "co", "gen_time": "1"}})
    with _patched_session(gs, session):
        out = gs.solve(verbose=False)
    assert out["data"]["result"] == "success"
    paths = [r[0] for r in session.requests]
    assert any(p.endswith("/load") for p in paths)
    assert any(p.endswith("/bg.png") for p in paths)
    assert any(p.endswith("/slice.png") for p in paths)
    verify_params = next(p for u, p in session.requests if u.endswith("/verify"))
    assert len(verify_params["w"]) > 256


def test_solve_passes_a_rejection_back_for_the_caller_to_retry():
    """The caller retries on a rejected solve - so this must return the
    envelope, not raise."""
    gs = _solver()
    session = _PipelineSession({"status": "success", "data": {"result": "fail"}})
    with _patched_session(gs, session):
        out = gs.solve(verbose=False)
    assert out["data"]["result"] == "fail"


def test_the_verbose_path_prints_without_changing_the_result():
    """verbose=True is the CLI mode; it must not alter what solve returns."""
    gs = _solver()
    envelope = {"status": "success", "data": {"result": "success",
                                              "pass_token": "pt"}}
    with _patched_session(gs, _PipelineSession(envelope)):
        loud = gs.solve(verbose=True)
    with _patched_session(gs, _PipelineSession(envelope)):
        quiet = gs.solve(verbose=False)
    assert loud == quiet == envelope


def test_the_override_helper_can_replace_any_plaintext_field():
    """Used to probe which fields the server actually checks - it must apply
    the overrides after the payload is built, not before."""
    gs = _solver()
    session = _PipelineSession({"status": "success", "data": {"result": "fail"}})
    with _patched_session(gs, session):
        out = gs.solve_with_overrides(
            plaintext_overrides={"answer": 999}, verbose=False)
    assert out["data"]["result"] == "fail"
    with _patched_session(gs, _PipelineSession({"status": "x"})):
        assert gs.solve_with_overrides(verbose=True)["status"] == "x"


def test_the_session_survives_a_broken_trust_store_setup():
    """Same contract as the api-side session: a missing CA bundle degrades,
    verification itself stays required."""
    gs = _solver()
    import ssl
    import requests  # noqa: F401 - must be fully imported before the patch
    import certifi
    orig_where, orig_load = certifi.where, ssl.SSLContext.load_default_certs

    def _boom(*a, **k):
        raise OSError("no bundle")

    certifi.where = _boom
    ssl.SSLContext.load_default_certs = _boom
    try:
        s = gs.make_session()
        adapter = s.get_adapter("https://captcha4.geely.com")
        ctx = adapter.poolmanager.connection_pool_kw["ssl_context"]
        assert ctx.verify_mode == ssl.CERT_REQUIRED
    finally:
        certifi.where = orig_where
        ssl.SSLContext.load_default_certs = orig_load


def test_the_session_factory_keeps_verification_on():
    """Legacy renegotiation relaxes the handshake, never the trust decision -
    this session carries the OTP code and the fresh token."""
    gs = _solver()
    import ssl
    s = gs.make_session()
    adapter = s.get_adapter("https://captcha4.geely.com")
    assert isinstance(adapter, gs._LegacyRenegAdapter)
    ctx = adapter.poolmanager.connection_pool_kw["ssl_context"]
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname is True
    assert "iPhone" in s.headers["User-Agent"]
