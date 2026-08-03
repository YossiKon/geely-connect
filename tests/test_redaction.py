"""Nothing identifying may reach a log, an exception, or the re-auth card.

These are the tests that guard the work in 1.8.1, 1.10.1 and 1.10.2. Every one
of them corresponds to something that actually leaked at some point.
"""
from conftest import FAKE_VIN, load

api = load("api")

TOKEN = "eyJhbGciOiJIUzI1NiJ9.header.signature"
EMAIL = "owner@example.com"
USER_ID = "8817263412"


def test_secrets_are_removed_entirely():
    out = api.redact({
        "accessToken": TOKEN, "cidpsso_token": TOKEN, "passToken": TOKEN,
        "appSecret": "48d6fff3ea19447bbf6f3ed76a608ff9", "captchaOutput": "x",
    })
    for k, v in out.items():
        assert v == "***redacted***", f"{k} survived as {v!r}"


def test_identifiers_are_shortened_not_removed():
    out = api.redact({"vin": FAKE_VIN, "userId": USER_ID, "email": EMAIL})
    assert out["vin"] == "...0000", out["vin"]
    assert out["userId"] == "...3412", out["userId"]
    assert FAKE_VIN not in str(out) and USER_ID not in str(out)


def test_redaction_reaches_nested_lists_and_dicts():
    out = api.redact({"data": {"list": [{"vin": FAKE_VIN, "token": TOKEN}]}})
    blob = str(out)
    assert FAKE_VIN not in blob and TOKEN not in blob


def test_pin_field_is_masked_because_it_carries_the_vin():
    # The scheduled-charging body sends the VIN in "pin".
    out = api.redact({"pin": FAKE_VIN})
    assert out["pin"] == "***redacted***"


def test_control_parameter_names_stay_readable():
    # "key" is a secret in provisioning but a parameter NAME in control calls.
    # Masking it made the command logs useless, which is a real regression.
    out = api.redact([{"key": "operation", "value": "1"},
                      {"key": "rcs.restart", "value": "1"}])
    assert out[0]["key"] == "operation"
    assert out[1]["key"] == "rcs.restart"


def test_key_holding_pem_material_is_still_masked():
    pem = "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkq\n-----END PRIVATE KEY-----"
    assert api.redact({"key": pem})["key"] == "***redacted***"
    assert api.redact({"key": "A" * 64})["key"] == "***redacted***"


def test_redact_does_not_mutate_its_input():
    src = {"vin": FAKE_VIN, "nested": {"token": TOKEN}}
    api.redact(src)
    assert src["vin"] == FAKE_VIN
    assert src["nested"]["token"] == TOKEN


def test_redact_survives_odd_shapes():
    for value in (None, "", [], {}, 0, {1: "x", None: "y"}):
        api.redact(value)          # must not raise


def test_vin_is_masked_in_urls_and_storage_paths():
    # GeelyAuthError text is shown on Home Assistant's re-auth card, so a VIN
    # inside a request path was on screen, not just in the log.
    for path in (
        f"/remote-control/vehicle/telematics/{FAKE_VIN}",
        f"/charge-server/ecarx_charge_set/{FAKE_VIN}?bizType=6",
        f"/config/.storage/geely_connect/{FAKE_VIN}/key.pem",
        f"C:/ha/.storage/geely_connect/{FAKE_VIN}/server_pins.json",
    ):
        masked = api.mask_path(path)
        assert FAKE_VIN not in masked, masked
        assert "...0000" in masked, masked


def test_mask_path_keeps_real_path_components_readable():
    masked = api.mask_path(f"/config/.storage/geely_connect/{FAKE_VIN}/server_pins.json")
    for part in ("geely_connect", "server_pins.json", ".storage"):
        assert part in masked, f"{part} was mangled: {masked}"


def test_every_secret_key_list_entry_is_normalised():
    # redact() compares against lowercased, separator-free keys. An entry with
    # an underscore or a capital would silently never match.
    for k in api._SECRET_KEYS | api._IDENTIFYING_KEYS:
        assert k == k.lower().replace("_", "").replace("-", ""), k
