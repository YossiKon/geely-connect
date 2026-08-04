"""The captcha solver stands between the user and being able to sign in at all.

Its image pipeline had two real bugs: red and blue swapped in the luma
weights, and diagonal edges suppressed along the wrong axis. Both degraded the
match silently, and one PNG mode crashed the login outright.
"""
import importlib.util
import io as _io

from conftest import load
from run import skip


def _deps():
    return all(importlib.util.find_spec(m) for m in ("numpy", "PIL", "scipy"))


def _png(img):
    b = _io.BytesIO()
    img.save(b, format="PNG")
    return b.getvalue()


def test_luma_weights_are_in_pillow_rgb_order():
    """cv2 indexes BGR; Pillow hands back RGB. Copying the cv2 formula verbatim
    swapped red and blue, weakening exactly the edges the match depends on."""
    if not _deps():
        skip("numpy/Pillow/scipy not installed")
    from PIL import Image
    gs = load("geetest_solver")
    for colour, expected in (((255, 0, 0), 76.245),      # 0.299 * 255
                             ((0, 255, 0), 149.685),     # 0.587 * 255
                             ((0, 0, 255), 29.07)):      # 0.114 * 255
        got = float(gs._to_grayscale(_png(Image.new("RGB", (4, 4), colour)))[0, 0])
        assert abs(got - expected) < 1, f"{colour} -> {got}, expected {expected}"


def test_every_png_mode_yields_a_two_dimensional_array():
    """An LA-mode PNG returned a 3-D array, so the edge detector raised and the
    whole login failed with only a generic 'send_code_failed' on screen."""
    if not _deps():
        skip("numpy/Pillow/scipy not installed")
    from PIL import Image
    gs = load("geetest_solver")
    cases = {
        "L": Image.new("L", (6, 5), 128),
        "RGB": Image.new("RGB", (6, 5), (255, 0, 0)),
        "RGBA": Image.new("RGBA", (6, 5), (255, 0, 0, 128)),
        "LA": Image.new("LA", (6, 5), (128, 255)),
        "P": Image.new("RGB", (6, 5), (255, 0, 0)).convert("P"),
    }
    for mode, img in cases.items():
        arr = gs._to_grayscale(_png(img))
        assert arr.ndim == 2, f"{mode} produced {arr.ndim} dimensions"
        assert arr.shape == (5, 6), f"{mode} -> {arr.shape}"
        gs._canny_edges(arr)     # must not raise


def test_palette_images_are_expanded_not_read_as_indices():
    """A P-mode array holds palette indices whose order is arbitrary, so using
    them as luminance makes the gradient meaningless."""
    if not _deps():
        skip("numpy/Pillow/scipy not installed")
    from PIL import Image
    gs = load("geetest_solver")
    red_p = Image.new("RGB", (4, 4), (255, 0, 0)).convert("P")
    got = float(gs._to_grayscale(_png(red_p))[0, 0])
    assert abs(got - 76.245) < 1, f"palette index leaked through as {got}"


def test_diagonal_edges_are_thinned():
    """Non-maximum suppression paired the 45 and 135 degree bins with each
    other's neighbours, so diagonals came out twice as thick as they should."""
    if not _deps():
        skip("numpy/Pillow/scipy not installed")
    import numpy as np
    gs = load("geetest_solver")
    n = 60
    yy, xx = np.mgrid[0:n, 0:n]
    for name, img in (("45 degrees", np.where(yy > xx, 255.0, 0.0)),
                      ("135 degrees", np.where(yy > (n - 1 - xx), 255.0, 0.0))):
        edges = gs._canny_edges(img, low=20.0, high=40.0)
        strong = edges > 0
        widths = [int(r.sum()) for r in strong[12:n - 12] if r.any()]
        # A one-pixel diagonal ridge covers sqrt(2) per scanline, so 2 is the
        # geometric floor; 4 means a whole axis was not suppressed.
        assert max(widths) <= 2, f"{name}: ridge {max(widths)} px wide"


def test_axis_aligned_edges_are_unaffected():
    if not _deps():
        skip("numpy/Pillow/scipy not installed")
    import numpy as np
    gs = load("geetest_solver")
    n = 60
    yy, xx = np.mgrid[0:n, 0:n]
    vertical = np.where(xx > n // 2, 255.0, 0.0)
    strong = gs._canny_edges(vertical, low=20.0, high=40.0) > 0
    widths = [int(r.sum()) for r in strong[12:n - 12] if r.any()]
    assert max(widths) == 1, f"vertical edge {max(widths)} px wide"


def test_a_non_jsonp_reply_fails_readably():
    """A captive portal or WAF page used to raise IndexError five frames down,
    surfacing as a bare send_code_failed with nothing to act on."""
    if not _deps():
        skip("numpy/Pillow/scipy not installed")
    gs = load("geetest_solver")

    class Resp:
        text = "<html>Access Denied</html>"

        def raise_for_status(self):
            pass

    class Sess:
        def get(self, *a, **k):
            return Resp()

    try:
        gs.fetch_load(Sess(), "cid")
    except RuntimeError as e:
        assert "JSONP" in str(e)
        return
    except IndexError:
        raise AssertionError("still raises the opaque IndexError")
    raise AssertionError("did not raise")


def test_the_server_cannot_redirect_image_fetches_anywhere_it_likes():
    """static_servers is server-supplied and lands in a URL authority."""
    if not _deps():
        skip("numpy/Pillow/scipy not installed")
    gs = load("geetest_solver")
    allowed = gs._allowed_captcha_hosts([
        "static.geely.com",              # fine
        "static.geetest.com",            # fine
        "evil.example.com",              # wrong domain
        "evil.com/static.geely.com",     # path smuggling
        "user@evil.com",                 # userinfo
        "static.geely.com:8080",         # port
        "static.geely.com\n",            # trailing newline
        "static.geely.com\r\nHost: evil",  # header injection
        "", None, 42, [],                # junk
        "-leading-dash.geely.com",       # must start alphanumeric
    ])
    assert allowed == ["static.geely.com", "static.geetest.com"], allowed


def test_host_filter_anchors_reject_a_trailing_newline():
    if not _deps():
        skip("numpy/Pillow/scipy not installed")
    gs = load("geetest_solver")
    # `$` would have matched here; `\Z` does not.
    assert not gs._CAPTCHA_HOST_RE.match("static.geely.com\n")


def test_an_error_envelope_does_not_log_the_session_material():
    """The /load body carries payload and process_token; the message reaches
    the Home Assistant log."""
    if not _deps():
        skip("numpy/Pillow/scipy not installed")
    gs = load("geetest_solver")
    SECRET = "SECRET_PROCESS_TOKEN"

    class Sess:
        def get(self, url, params=None, timeout=None):
            cb = params["callback"]
            body = (cb + '({"status":"error","error":"bad","payload":"' + SECRET
                    + '","process_token":"' + SECRET + '"})')
            return type("R", (), {"text": body, "raise_for_status": lambda s: None})()

    try:
        gs.fetch_load(Sess(), "cid")
    except RuntimeError as e:
        assert SECRET not in str(e), "captcha session material reached the error text"
        return
    raise AssertionError("did not raise")


def test_an_unreachable_captcha_host_fails_fast_not_five_times():
    """Issue #5: with the host blocked, every attempt burned ~45 s of connect
    timeouts (15 s per resolved address) and the loop ran all five before the
    user saw a generic "try again in a minute". Network-level failure is not
    solver inaccuracy - one attempt, then a distinct exception naming the
    host so there is something to act on."""
    if importlib.util.find_spec("requests") is None:
        skip("requests not installed")
    import requests
    api = load("api")
    gs = load("geetest_solver")
    calls = []

    def _unreachable(**kw):
        calls.append(1)
        raise requests.exceptions.ConnectTimeout(
            "Connection to the captcha host timed out")

    orig = gs.solve
    gs.solve = _unreachable
    try:
        try:
            api.cidpsso_send_otp("user@example.com", "AU")
            raised = None
        except api.GeelyCaptchaUnreachableError as e:
            raised = e
    finally:
        gs.solve = orig
    assert raised is not None, "no distinct exception for the unreachable host"
    assert "captcha4.geely.com" in str(raised), raised
    assert len(calls) == 1, f"retried {len(calls)} times against a dead network"
