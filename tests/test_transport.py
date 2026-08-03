"""Transport invariants: pinning, host allowlisting, request building.

Offline - nothing here opens a socket.
"""
import json
import threading
import time

from conftest import FAKE_VIN, load

api = load("api")


# ---------------------------------------------------------------- pinning ---

def test_every_private_pki_host_ships_a_pin():
    # If a host is allowlisted for the pinning fallback but has no bundled pin,
    # the first connection would trust-on-first-use whatever answered.
    for host in api._PRIVATE_PKI_HOSTS:
        assert host in api._BUNDLED_TLS_PINS, f"{host} is allowlisted with no pin"


def test_public_hosts_are_not_allowlisted_for_pinning():
    # These must validate strictly against public CAs and can never downgrade.
    for host in ("api.ecloudeu.com", "api.ecloudus.com", "api.ecloudkr.com",
                 "m-lcmsam-eu.geely.com", "m-lcmsam-kr.geely.com",
                 "captcha4.geely.com", "access-app-global.geely.com"):
        assert host not in api._PRIVATE_PKI_HOSTS, host


def test_only_private_ca_verify_codes_may_fall_back():
    # 18/19/20 mean "chain is fine, we just don't know this CA". An expired
    # certificate or a hostname mismatch must never reach the pinning path.
    assert api._PRIVATE_CA_VERIFY_CODES == frozenset({18, 19, 20})


def test_pins_are_sha256_base64():
    import base64
    for host, pins in api._BUNDLED_TLS_PINS.items():
        assert pins, host
        for p in pins:
            assert len(base64.b64decode(p)) == 32, f"{host}: {p} is not a SHA-256"


# ------------------------------------------------------------- pin store ---

def test_pin_store_fails_closed_on_a_corrupt_file(tmp_path_factory=None):
    import os, tempfile
    d = tempfile.mkdtemp()
    p = os.path.join(d, "server_pins.json")
    for bad in ("{not json", "[]", '{"h": 5}'):
        open(p, "w").write(bad)
        try:
            api._load_pin_store(p)
        except api.GeelyTLSPinError:
            continue
        raise AssertionError(f"accepted a corrupt store: {bad!r}")


def test_pin_store_missing_file_is_an_empty_store():
    assert api._load_pin_store("/nonexistent/server_pins.json") == {}
    assert api._load_pin_store(None) == {}


def test_pin_store_round_trips_and_migrates_the_old_list_format():
    import os, tempfile
    p = os.path.join(tempfile.mkdtemp(), "sub", "server_pins.json")
    api._save_pin_store(p, {"h": {"pins": ["AAA="], "strict": True}})
    got = api._load_pin_store(p)
    assert got["h"]["pins"] == ["AAA="] and got["h"]["strict"] is True
    # 0.9.x wrote a bare list per host
    open(p, "w").write(json.dumps({"h": ["BBB="]}))
    got = api._load_pin_store(p)
    assert got["h"]["pins"] == ["BBB="] and got["h"]["strict"] is False


# ------------------------------------------------------------- injection ---

def test_crlf_is_rejected_everywhere_it_could_be_injected():
    for bad in ("a\rb", "a\nb", "a\r\nb", f"{FAKE_VIN}\r\nX-Evil: 1"):
        try:
            api._no_crlf(bad, "test")
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad!r}")
    assert api._no_crlf("harmless/path", "test") == "harmless/path"


# ------------------------------------------------------------------ JWT ---

def test_a_real_client_constructs():
    """__new__ in the other tests skips __init__, which hides missing imports.

    A missing `import threading` survived a passing JWT-lock test for exactly
    that reason: the lock is created in __init__, which those tests never run.
    """
    a = api.GeelyApi(
        app_id="X", app_secret="Y", user_id="1", vin=FAKE_VIN,
        cidpsso_token="t", client_id="c", vehicle_series="s",
        vehicle_model="s", device_id="d", cert_path="", key_path="",
    )
    assert a._jwt is None and a._jwt_exp == 0
    assert hasattr(a._jwt_lock, "acquire"), "no usable lock"


def test_no_module_references_an_unimported_name():
    """Catches what py_compile cannot: a name used but never imported."""
    import ast, builtins, glob, io, os
    from conftest import PKG
    problems = {}
    for f in sorted(glob.glob(os.path.join(PKG, "*.py"))):
        tree = ast.parse(io.open(f, encoding="utf-8").read())
        defined = set(dir(builtins))
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                for a in n.names:
                    defined.add((a.asname or a.name).split(".")[0])
            elif isinstance(n, ast.ImportFrom):
                for a in n.names:
                    defined.add(a.asname or a.name)
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                defined.add(n.name)
            elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                defined.add(n.id)
            elif isinstance(n, ast.arg):
                defined.add(n.arg)
            elif isinstance(n, ast.ExceptHandler) and n.name:
                defined.add(n.name)
        used = {n.id for n in ast.walk(tree)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        missing = sorted(used - defined)
        if missing:
            problems[os.path.basename(f)] = missing
    assert not problems, problems


def _api_with(jwt, exp):
    a = api.GeelyApi.__new__(api.GeelyApi)
    a._jwt, a._jwt_exp, a._jwt_lock = jwt, exp, threading.Lock()
    return a


def test_concurrent_callers_trigger_exactly_one_jwt_refresh():
    # Geely allows one session per account: a duplicate refresh signs the
    # owner's phone app out a second time.
    a = _api_with(None, 0)
    calls = []

    def refresh():
        calls.append(1)
        time.sleep(0.05)
        a._jwt, a._jwt_exp = "fresh", int(time.time()) + 7200

    a.refresh_jwt = refresh
    got = []
    ts = [threading.Thread(target=lambda: got.append(a._ensure_jwt())) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(calls) == 1, f"{len(calls)} refreshes"
    assert set(got) == {"fresh"}


def test_a_valid_token_is_reused_without_refreshing():
    a = _api_with("cached", int(time.time()) + 3600)
    a.refresh_jwt = lambda: (_ for _ in ()).throw(AssertionError("refreshed needlessly"))
    assert a._ensure_jwt() == "cached"


def test_a_refresh_that_produces_no_token_raises():
    # Otherwise None is formatted into an Authorization header.
    a = _api_with(None, 0)
    a.refresh_jwt = lambda: None
    try:
        a._ensure_jwt()
    except api.GeelyAuthError:
        return
    raise AssertionError("returned without a token")


# ------------------------------------------------------- auth escalation ---

def test_transient_apac_failures_do_not_force_a_reauth():
    # GeelyAuthError is never retried and becomes ConfigEntryAuthFailed, which
    # costs the user a captcha and a fresh email code. 8500/1445 are server
    # faults, not dead credentials.
    for code in ("8500", "1445", "9999"):
        assert not api._is_auth_failure({"code": code}), code
    for code in ("60000000", 1402, "1402"):
        assert api._is_auth_failure({"code": code}), code
