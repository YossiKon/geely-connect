# -----------------------------------------------------------------------------
# Portions of this file - the reverse-engineered Geely protocol / field mappings
# (the parts that required protocol research) - are derived from
# nitaybz/geely-global-ha, used under the MIT License. See NOTICE.txt.
# Original framework, security hardening and transport are our own work.
# -----------------------------------------------------------------------------
#!/usr/bin/env python3
"""
GeeTest BehaviorVerification v4 slide-puzzle solver - adapted for Geely's
self-hosted deployment at captcha4.geely.com.

CRYPTO SPEC:
  - Algorithm:    SM2 ECIES per GM/T 0003.4-2012 (Chinese national standard)
  - Curve:        sm2p256v1
  - KDF:          SM3-based, 32-bit BE counter starting at 1
  - Output order: C1 || C3 || C2  (gmssl default mode='1')
  - C1 layout:    64 bytes = X || Y, NO 0x04 uncompressed prefix (must strip!)
  - C2:           stream-XOR with KDF, same length as plaintext
  - C3:           SM3(x2 || M || y2), 32 bytes
  - Final w:      hex(C1) + hex(C3) + hex(C2)  - concatenated, no separators

PLAINTEXT FIELDS (the JSON that gets SM2-encrypted into `w`):
  - serial:       passToken from /load  (Geely's /load doesn't expose one - try
                  process_token / payload / lot_number)
  - pow_msg:      proof-of-work message (empty if /load has no pow_detail)
  - pow_sign:     pow hex (empty if no pow_detail)
  - env:          encodeURIComponent(JSON.stringify(fingerprint or -1))
                  Initial value before fingerprint collection: literal number -1
  - geetest:      "captcha" (constant)
  - type:         "slide" / "click" / etc.
  - answer:       slider X-pixel (integer)
  - passtime:     ms taken to solve
  - trackOffset:  string-encoded slide track (slide-only)

Validates against captcha4.geely.com /verify by mirroring the iOS app's request
shape exactly. Once `pass_token` is returned, can be fed straight into
/cidpsso/captcha/v3/getCaptcha to send the email-login code.
"""
from __future__ import annotations

import binascii
import json
import os
import random
import re
import secrets
import sys
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import io

import numpy as np
import requests
from PIL import Image

from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey.RSA import construct as rsa_construct
from Crypto.Util.Padding import pad

# SECURITY: we deliberately do NOT call urllib3.disable_warnings() here.
# The upstream project suppressed InsecureRequestWarning process-wide, which
# would also hide warnings for any *other* integration in the same Home
# Assistant process. This captcha session verifies certificates normally
# (create_urllib3_context() defaults to CERT_REQUIRED + hostname check), so
# there are no warnings to suppress in the first place.


GEELY_CAPTCHA_ID = "4c1ef89633cbab987d1ee170115a8dd4"
GEELY_HOST = "https://captcha4.geely.com"

# Geely uses AES_RSA encryption (per `config.encryption` field at runtime).
# Verified by w-length math: captured /verify w = 1504 hex chars for a 612-byte
# plaintext = 256 (RSA-1024) + 2*pad16(612) = AES_RSA, NOT SM2 ECIES.
#
# **Geely customized the RSA pubkey** - the standard GeeTest one (`c1e3934d...`)
# fails. Geely's actual modulus, extracted via Playwright closure-walking of
# `window.Geetest` -> `un` (jsbn-style RSAKey with `setPublic, encrypt` proto
# methods, deob line 10233):
RSA_N_HEX = (
    "a07fe9d66006cb5ff61b6ab0c77208bca38a4674a96f121f9e8406c019ddd3b4"
    "c2fc0d76e54973328ea5cd08af91ac7cd166a200708f4f5650f405a3ab1d14f9"
    "c2dd6b94d788de87fa2249ff0826c0bb9b9a1d49d5662888afad8e891b235358"
    "7a89175cb4dc215764b067b8e4531414d4efb2d7c3cfe7b1f69355968cd9b2ab"
)
RSA_PUBKEY = rsa_construct((int(RSA_N_HEX, 16), 0x10001))
AES_IV = b"0000000000000000"   # GeeTest standard IV for the AES-CBC body cipher

# `wz` aka `serial`: a per-deployment constant for Geely's captcha_id.
# Verified constant across 3 fresh widget runs.
GEELY_WZ = "7eb32de3afb26299903b0e5bfaee17ab!!3a304286ca668d724d20f4ae2edf4481"


# ---------- HTTP session (Geely needs OpenSSL legacy renegotiation) ----------

class _LegacyRenegAdapter(requests.adapters.HTTPAdapter):
    def init_poolmanager(self, *a, **kw):
        import ssl as _ssl
        from urllib3.util.ssl_ import create_urllib3_context
        ctx = create_urllib3_context()
        ctx.options |= 0x4
        # Keep certificate verification ON (a bare urllib3 context has no
        # trust store loaded, which would otherwise silently fail-open in
        # some urllib3 versions). Load a CA bundle and require validation.
        try:
            import certifi
            ctx.load_verify_locations(cafile=certifi.where())
        except Exception:  # noqa: BLE001
            pass
        try:
            ctx.load_default_certs()
        except Exception:  # noqa: BLE001
            pass
        ctx.check_hostname = True
        ctx.verify_mode = _ssl.CERT_REQUIRED
        kw["ssl_context"] = ctx
        return super().init_poolmanager(*a, **kw)


def make_session() -> requests.Session:
    s = requests.Session()
    s.mount("https://", _LegacyRenegAdapter())
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) "
                      "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148",
        "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
        "Accept": "*/*",
    })
    return s


# ---------- Slide solver: find gap X with PIL+numpy edge-template-match ----------
#
# Pure-Python replacement for the original cv2-based version (cv2 has no
# Python 3.14 wheel and won't build from source on HA's container).
# Algorithm: gradient-magnitude edges (Sobel approximation) + FFT-based
# normalized cross-correlation. Functionally equivalent to cv2.Canny +
# cv2.matchTemplate(TM_CCOEFF_NORMED) for slide-puzzle inputs.

def _to_grayscale(png: bytes) -> np.ndarray:
    """Load PNG as grayscale, ignoring alpha (cv2.IMREAD_ANYCOLOR behavior).
    Alpha-compositing against black would create spurious edges along the
    slice's transparent boundary and break template matching.

    Palette images are expanded first: a "P"-mode array holds palette indices,
    and their numeric order is arbitrary, so treating them as luminance makes
    the gradient meaningless.
    """
    img = Image.open(io.BytesIO(png))
    if img.mode in ("P", "PA"):
        img = img.convert("RGBA")
    arr = np.asarray(img)
    if arr.ndim == 2:                       # already single-channel ("L", "1", "I")
        return arr.astype(np.float32)
    if arr.shape[-1] in (2, 4):
        arr = arr[..., :-1]                 # drop alpha - do NOT composite
    if arr.shape[-1] == 1:                  # "LA" once alpha is dropped
        return arr[..., 0].astype(np.float32)
    if arr.shape[-1] == 3:
        # BT.601 luma. PIL hands back RGB, so R is index 0 - the cv2 formula
        # this was ported from indexes a BGR array and must not be copied
        # verbatim, or the red and blue weights end up swapped.
        return (0.299 * arr[..., 0] + 0.587 * arr[..., 1] + 0.114 * arr[..., 2]).astype(np.float32)
    return arr[..., 0].astype(np.float32)


def _canny_edges(im: np.ndarray, low: float = 50.0, high: float = 100.0) -> np.ndarray:
    """Canny edge detector in pure numpy + scipy.ndimage. Reproduces
    cv2.Canny well enough for slide-puzzle template matching:
      1. Gaussian smoothing (sigma=1)
      2. Sobel gradient
      3. Non-maximum suppression along gradient direction (1-px-thin edges)
      4. Hysteresis double-threshold via connected-component labeling
    """
    from scipy import ndimage as _ndi

    blurred = _ndi.gaussian_filter(im, sigma=1.0)
    gx = _ndi.sobel(blurred, axis=1)
    gy = _ndi.sobel(blurred, axis=0)
    mag = np.hypot(gx, gy)

    # Quantize gradient direction into 4 bins (0°, 45°, 90°, 135°)
    angle = np.degrees(np.arctan2(gy, gx)) % 180.0
    h, w = mag.shape
    nms = np.zeros_like(mag)
    # Pad to allow neighbor lookup without index errors
    mp = np.pad(mag, 1, mode="constant")
    # Direction bins → neighbor offsets in (mp), accounting for the +1 pad
    # 0°/180°: horizontal neighbors
    bin0 = (angle < 22.5) | (angle >= 157.5)
    bin1 = (angle >= 22.5) & (angle < 67.5)    # 45°
    bin2 = (angle >= 67.5) & (angle < 112.5)   # 90°
    bin3 = (angle >= 112.5) & (angle < 157.5)  # 135°
    center = mp[1:1 + h, 1:1 + w]
    n_h1 = mp[1:1 + h, 0:w]; n_h2 = mp[1:1 + h, 2:2 + w]
    n_v1 = mp[0:h, 1:1 + w]; n_v2 = mp[2:2 + h, 1:1 + w]
    # gy is the derivative along increasing *row* index, so the angle above is
    # measured in a y-points-down frame. 45° therefore runs down-right (the
    # main diagonal) and 135° runs down-left (the anti-diagonal) - the opposite
    # of the y-points-up convention these offsets are usually written for.
    n_m1 = mp[0:h, 0:w];     n_m2 = mp[2:2 + h, 2:2 + w]  # 45°: up-left / down-right
    n_a1 = mp[0:h, 2:2 + w]; n_a2 = mp[2:2 + h, 0:w]      # 135°: up-right / down-left
    keep = np.zeros_like(mag, dtype=bool)
    keep |= bin0 & (center >= n_h1) & (center >= n_h2)
    keep |= bin1 & (center >= n_m1) & (center >= n_m2)
    keep |= bin2 & (center >= n_v1) & (center >= n_v2)
    keep |= bin3 & (center >= n_a1) & (center >= n_a2)
    nms[keep] = mag[keep]

    # Hysteresis: strong = above high; weak = above low and connected to strong
    strong = nms >= high
    weak = (nms >= low) & (nms < high)
    structure = np.ones((3, 3), dtype=bool)
    labeled, _ = _ndi.label(strong | weak, structure=structure)
    # Keep components that contain at least one strong pixel
    has_strong = np.zeros(labeled.max() + 1, dtype=bool)
    has_strong[labeled[strong]] = True
    edges = has_strong[labeled]
    return edges.astype(np.float32)


# Backwards-compat alias for the old name (kept in case anything imports it)
_gradient_edges = _canny_edges


def find_gap_x(bg_png: bytes, slice_png: bytes) -> int:
    """Return the X offset (in puzzle pixels) where the slice fits the gap.
    Uses pure-Python Canny + FFT-based cross-correlation. Equivalent to
    the original cv2.Canny + cv2.matchTemplate(TM_CCOEFF_NORMED) approach.

    Constraint: in slide-puzzle captchas the slice always starts pinned to
    the left edge, so the gap is always right of it - we mask out
    correlation hits at x < slice_width."""
    from scipy.signal import fftconvolve
    bg = _to_grayscale(bg_png)
    sl = _to_grayscale(slice_png)
    e_bg = _canny_edges(bg)
    e_sl = _canny_edges(sl)
    res = fftconvolve(e_bg, e_sl[::-1, ::-1], mode="valid")
    sw = e_sl.shape[1]
    res[:, :sw] = -1.0
    flat_idx = int(res.argmax())
    return int(flat_idx % res.shape[1])


# ---------- Encrypt the w parameter ----------

def _rand_aes_key_hex16() -> str:
    """Random 16-character hex string. UTF-8 bytes of this string == 16 bytes,
    used as the AES-128 key. Matches GeeTest BV4 reference impl."""
    return secrets.token_hex(8)


def encrypt_w(plaintext: str) -> str:
    """AES_RSA encryption per GeeTest BV4 reference:
      - AES-128-CBC encrypt the plaintext with a random 16-char hex key
      - RSA-1024 PKCS1v1.5 encrypt that key (UTF-8 bytes of hex string)
      - Output: hex(AES-ciphertext) || hex(RSA-encrypted-key)
    """
    aes_key = _rand_aes_key_hex16()
    cipher = AES.new(aes_key.encode("utf-8"), AES.MODE_CBC, AES_IV)
    ct = cipher.encrypt(pad(plaintext.encode("utf-8"), AES.block_size))
    rsa_cipher = PKCS1_v1_5.new(RSA_PUBKEY)
    rsa_ct = rsa_cipher.encrypt(aes_key.encode("utf-8"))
    return ct.hex() + rsa_ct.hex()


# ---------- Track-string for slide answer ----------

def build_track_string(target_x: float, total_ms: int) -> str:
    """The widget's slide event includes a `trackOffset` - a string-encoded
    series of mouse-move samples during the drag. Synthesize a humanlike
    track that ends at `target_x`.

    Format observed in deob source (line ~21420): the widget records (x, y, t)
    triples and serializes them. JSON form is fine; string-form is also seen.
    Easiest: use a minimal JSON array.
    """
    pts = []
    n = 25
    for i in range(1, n + 1):
        progress = i / n
        eased = 1 - (1 - progress) ** 3
        x = round(target_x * eased + random.uniform(-1.0, 1.0), 0)
        y = random.randint(-2, 2)
        t = round(total_ms * eased + random.uniform(-3, 3))
        pts.append([int(x), int(y), int(t)])
    return json.dumps(pts, separators=(",", ":"))


# ---------- /load + /verify orchestration ----------

@dataclass
class LoadResponse:
    lot_number: str
    challenge: str
    pt: str
    payload: str
    process_token: str
    payload_protocol: int
    captcha_id: str
    static_servers: list[str]
    bg_path: str
    slice_path: str
    raw: dict = field(default_factory=dict)


def fetch_load(s: requests.Session, captcha_id: str, *, client_type: str = "web", encryption: str = "AES_RSA") -> LoadResponse:
    """GET /load (JSONP wrapper). Mirrors iOS app shape exactly:
    `encryption=AES_RSA` URL param, `ext={ua}`, `client_type=ios`.
    """
    challenge = str(uuid4())
    callback = f"geetest_{int(time.time() * 1000)}"
    ua = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) "
          "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148")
    params = {
        "captcha_id": captcha_id,
        "challenge": challenge,
        "client_type": client_type,
        "language": "eng",
        "encryption": encryption,
        "ext": json.dumps({"ua": ua}, separators=(",", ":")),
        "callback": callback,
    }
    r = s.get(f"{GEELY_HOST}/load", params=params, timeout=15)
    r.raise_for_status()
    text = r.text
    inner = text.split(f"{callback}(", 1)[1].rsplit(")", 1)[0]
    j = json.loads(inner)
    if not j.get("status") == "success":
        raise RuntimeError(f"/load failed: {j}")
    d = j["data"]
    return LoadResponse(
        lot_number=d["lot_number"],
        challenge=d["challenge"],
        pt=str(d.get("pt", "10")),
        payload=d["payload"],
        process_token=d["process_token"],
        payload_protocol=int(d.get("payload_protocol", 1)),
        captcha_id=d["captcha_id"],
        static_servers=d.get("static_servers", []),
        bg_path=d["bg"],
        slice_path=d["slice"],
        raw=d,
    )


_CAPTCHA_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]{0,252}$")
_CAPTCHA_HOST_SUFFIXES = (".geely.com", ".geetest.com")

_FALLBACK_CAPTCHA_HOST = "captcha4.geely.com"


def _allowed_captcha_hosts(static_servers: list[str]) -> list[str]:
    """Filter the CDN hosts the captcha /load response asks us to use.

    The list is server-supplied and gets interpolated straight into a URL
    authority, so without a check the response could point Home Assistant at
    any host, port or userinfo it liked. Accept only bare hostnames under the
    expected domains; port 443 is implied because ':' is rejected."""
    hosts = []
    for host in static_servers:
        if not isinstance(host, str) or not _CAPTCHA_HOST_RE.match(host):
            continue
        if not host.lower().endswith(_CAPTCHA_HOST_SUFFIXES):
            continue
        hosts.append(host)
    return hosts


def fetch_image(s: requests.Session, static_servers: list[str], path: str) -> bytes:
    last_err: Exception | None = None
    for host in _allowed_captcha_hosts(static_servers) + [_FALLBACK_CAPTCHA_HOST]:
        url = f"https://{host}/{path.lstrip('/')}"
        try:
            r = s.get(url, timeout=15)
            r.raise_for_status()
            return r.content
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise RuntimeError(f"image fetch failed: {last_err}")


def build_inner_payload(load: LoadResponse, set_left: int, passtime_ms: int, *, wz: str | None = None) -> dict:
    """The JSON object that gets SM2-encrypted into `w`.

    Field order, names and shape are taken from a real captured plaintext
    produced by the actual widget:

        {"serial":"<wz value>",
         "env":"<URI-encoded JSON of {roe: {...}}>",
         "geetest":"captcha",
         "gct":"<random int as string>",
         "bjpr":"zc1h7e6d",            # constant from string table
         "em":{"ph":0,"cp":0,"ek":"11","wd":1,"nt":0,"si":0,"sc":0},
         "type":"slide",
         "answer":<int>,
         "passtime":<int>,
         "trackOffset":"<JSON-stringified array>"}

    `wz` (the `serial` field) comes from the widget's `Ce.load()` Promise.
    When unknown, we fall back to `process_token` of /load - server may or
    may not accept; tests show /verify rejects without a real wz.
    """
    track = build_track_string(set_left, passtime_ms)
    env_obj = {"roe": {
        "aup": "3", "sep": "3", "egp": "3", "auh": "3",
        "rew": "3", "snh": "1", "res": "3", "cdc": "3",
    }}
    env_str = quote(json.dumps(env_obj, separators=(",", ":")))
    return {
        "serial":      wz or GEELY_WZ,
        "env":         env_str,
        "geetest":     "captcha",
        "gct":         str(random.randint(-2147483648, 2147483647)),
        "bjpr":        "zc1h7e6d",
        "em":          {"ph": 0, "cp": 0, "ek": "11", "wd": 1, "nt": 0, "si": 0, "sc": 0},
        "type":        "slide",
        "answer":      int(set_left),
        "passtime":    int(passtime_ms),
        "trackOffset": track,
    }


def get_verify_jsonp(s: requests.Session, load: LoadResponse, w: str, *, client_type: str = "web") -> dict:
    """GET /verify with URL params - JSONP-style, NOT POST.

    The widget's verify uses dynamic <script src=/verify?...&callback=...> to
    bypass CORS. The server replies with `geetest_NNN({...})`.
    """
    callback = f"geetest_{int(time.time() * 1000)}"
    params = {
        "lot_number": load.lot_number,
        "captcha_id": load.captcha_id,
        "client_type": client_type,
        "challenge": load.challenge,
        "pt": load.pt,
        "payload": load.payload,
        "payload_protocol": load.payload_protocol,
        "process_token": load.process_token,
        "w": w,
        "callback": callback,
    }
    r = s.get(f"{GEELY_HOST}/verify", params=params, timeout=15)
    text = r.text
    # Unwrap JSONP: callback({...})
    if text.startswith(callback + "("):
        try:
            inner = text[len(callback) + 1:-1]  # strip "callback(" prefix and ")" suffix
            return json.loads(inner)
        except Exception:
            return {"http": r.status_code, "raw": text[:500]}
    try:
        return r.json()
    except Exception:
        return {"http": r.status_code, "raw": text[:500]}


# Back-compat alias - old `post_verify` -> new `get_verify_jsonp`
post_verify = get_verify_jsonp


# ---------- top-level driver ----------

def solve(captcha_id: str = GEELY_CAPTCHA_ID, verbose: bool = True) -> dict:
    s = make_session()

    if verbose: print("[1] GET /load ...")
    load = fetch_load(s, captcha_id)
    if verbose:
        print(f"    lot_number={load.lot_number}")
        print(f"    challenge ={load.challenge}")
        print(f"    pt={load.pt}  process_token={load.process_token[:32]}...")
        print(f"    bg={load.bg_path}")
        print(f"    slice={load.slice_path}")

    if verbose: print("[2] download bg + slice ...")
    bg = fetch_image(s, load.static_servers, load.bg_path)
    sl = fetch_image(s, load.static_servers, load.slice_path)
    if verbose: print(f"    bg={len(bg)} bytes, slice={len(sl)} bytes")

    if verbose: print("[3] OpenCV gap detection ...")
    gap_x = find_gap_x(bg, sl)
    if verbose: print(f"    answer (raw px) = {gap_x}")

    if verbose: print("[4] build plaintext + AES_RSA encrypt ...")
    passtime = random.randint(900, 1500)
    inner = build_inner_payload(load, gap_x, passtime)
    plaintext = json.dumps(inner, separators=(",", ":"))
    if verbose:
        print(f"    plaintext fields: {list(inner.keys())}")
        print(f"    plaintext bytes: {len(plaintext)}")
    w = encrypt_w(plaintext)
    expected = 256 + 2 * ((len(plaintext) // 16 + 1) * 16)
    if verbose: print(f"    w len = {len(w)} chars (expected {expected} for AES_RSA)")

    if verbose: print("[5] GET /verify (JSONP) ...")
    resp = get_verify_jsonp(s, load, w)
    if verbose: print(f"    response: {json.dumps(resp, indent=2, ensure_ascii=False)[:600]}")

    return resp


def solve_with_overrides(*, plaintext_overrides: dict | None = None, client_type: str = "web", verbose: bool = True) -> dict:
    """Test a single attempt with custom plaintext fields. For matrix testing."""
    s = make_session()
    load = fetch_load(s, GEELY_CAPTCHA_ID)
    bg = fetch_image(s, load.static_servers, load.bg_path)
    sl = fetch_image(s, load.static_servers, load.slice_path)
    gap_x = find_gap_x(bg, sl)
    passtime = random.randint(900, 1500)
    inner = build_inner_payload(load, gap_x, passtime)
    if plaintext_overrides:
        inner.update(plaintext_overrides)
    plaintext = json.dumps(inner, separators=(",", ":"))
    w = encrypt_w(plaintext)
    if verbose:
        print(f"  [variant] fields={list(inner.keys())} pt_len={len(plaintext)} w_len={len(w)}")
    resp = post_verify(s, load, w, client_type=client_type)
    if verbose:
        print(f"            -> {json.dumps(resp, ensure_ascii=False)[:300]}")
    return resp


if __name__ == "__main__":
    result = solve()
    if isinstance(result, dict) and result.get("status") == "success":
        data = result.get("data", {})
        if data.get("result") == "success":
            print("\n[+] CAPTCHA SOLVED")
            print(f"   lot_number      = {data.get('lot_number')}")
            print(f"   pass_token      = {data.get('pass_token')}")
            print(f"   captcha_output  = {data.get('captcha_output', '')[:60]}...")
            print(f"   gen_time        = {data.get('gen_time')}")
            sys.exit(0)
        else:
            print(f"\n[!] /verify returned status=success but data.result={data.get('result')}")
            sys.exit(2)
    else:
        print("\n[!] /verify rejected - see response above for diagnostics.")
        sys.exit(3)
