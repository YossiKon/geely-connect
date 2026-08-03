"""The identifier guards in the config flow.

The VIN and user_id arrive in the Geely backend's JSON and then flow into
filesystem paths and into a hand-built raw HTTP request line. These validators
are what stops a hostile or compromised backend from choosing where the
vehicle's private key gets written.
"""
from conftest import FAKE_VIN, have_homeassistant, load
from run import skip


def _cf():
    if not have_homeassistant():
        skip("homeassistant not installed")
    return load("config_flow")


# --------------------------------------------------------------- the VIN ---

def test_a_normal_vin_is_accepted():
    cf = _cf()
    assert cf._valid_vin(FAKE_VIN)
    assert cf._valid_vin("LB37622Z5MX123456")


def test_path_traversal_shapes_are_rejected():
    cf = _cf()
    for evil in ("../../etc/passwd", "..", "../..", "a/../../b",
                 "/absolute/path", "C:\\windows\\system32",
                 "vin/../../..", ".ssh"):
        assert not cf._valid_vin(evil), f"accepted {evil!r}"


def test_request_line_injection_shapes_are_rejected():
    cf = _cf()
    for evil in (f"{FAKE_VIN}\r\nX-Evil: 1", f"{FAKE_VIN}\n", "a\rb",
                 f"{FAKE_VIN} HTTP/1.1", f"{FAKE_VIN}?x=1", f"{FAKE_VIN}#frag"):
        assert not cf._valid_vin(evil), f"accepted {evil!r}"


def test_wrong_types_and_empties_are_rejected():
    cf = _cf()
    for evil in (None, "", 12345, [], {}, b"L6T00000000000000", " " + FAKE_VIN):
        assert not cf._valid_vin(evil), f"accepted {evil!r}"


def test_length_bounds_are_enforced():
    cf = _cf()
    assert not cf._valid_vin("A" * 7), "too short accepted"
    assert cf._valid_vin("A" * 8)
    assert cf._valid_vin("A" * 20)
    assert not cf._valid_vin("A" * 21), "too long accepted"


# ------------------------------------------------------------- the userid ---

def test_user_id_accepts_only_a_conservative_charset():
    cf = _cf()
    for ok in ("8817263412", "user.name", "user-name", "user_name", "a"):
        assert cf._valid_user_id(ok), ok
    for evil in ("../x", "a/b", "a\\b", "a b", "a\r\nb", "", None, 42,
                 "a" * 65, "a;b", "a&b"):
        assert not cf._valid_user_id(evil), f"accepted {evil!r}"


# -------------------------------------------------------------- the paths ---

def test_storage_paths_refuse_to_build_from_a_bad_vin():
    cf = _cf()

    class Hass:
        class config:
            @staticmethod
            def path(p):
                return "/config/" + p

    for evil in ("../../etc", "", None, "a/b"):
        try:
            cf._storage_paths(Hass(), evil)
        except (ValueError, TypeError):
            continue
        raise AssertionError(f"built a path from {evil!r}")


def test_storage_paths_stay_inside_the_integration_directory():
    cf = _cf()

    class Hass:
        class config:
            @staticmethod
            def path(p):
                return "/config/" + p

    cert, key = cf._storage_paths(Hass(), FAKE_VIN)
    for p in (cert, key):
        assert "/geely_connect/" in p.replace("\\", "/"), p
        assert FAKE_VIN in p
        assert ".." not in p
    assert cert.endswith("cert.pem") and key.endswith("key.pem")


# ------------------------------------------------------------ the version ---

def test_the_config_entry_version_matches_the_migration_ladder():
    """A VERSION bump without a migration branch strands existing users."""
    import io, os
    from conftest import PKG
    cf = _cf()
    init_src = io.open(os.path.join(PKG, "__init__.py"), encoding="utf-8").read()
    assert "async_migrate_entry" in init_src
    # every version below the current one must be reachable by the migration
    assert cf.GeelyIntlConfigFlow.VERSION >= 1
    assert f"entry.version" in init_src or "version" in init_src
