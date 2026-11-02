"""App credentials for an original-login region, supplied by the owner (#105).

The loader is the only way such a region's app id and secret reach the
integration, so it is held to the European loader's standard: a closed set of
fields, printable values, a size cap, servers from code only, and no value in
any message it raises. Offline - nothing here opens a socket.
"""
import json
import os
import tempfile

from conftest import load

rk = load("region_keys")
const = load("const")

FAKE = {"region": "AE", "app_id": "FAKEAPPID01", "app_secret": "fakesecret0123456789abcdef"}


def _dir_with(content):
    d = tempfile.mkdtemp()
    if content is not None:
        mode = "wb" if isinstance(content, bytes) else "w"
        with open(os.path.join(d, rk.KEYS_FILENAME), mode) as f:
            f.write(content)
    return d


def _raises(fn, *needles):
    try:
        fn()
    except rk.LocalRegionKeysError as e:
        text = str(e)
        for needle in needles:
            assert needle in text, (needle, text)
        for value in FAKE.values():
            if len(value) > 4:
                assert value not in text, ("a message carried a value", text)
        return text
    raise AssertionError("no LocalRegionKeysError")


def test_a_valid_file_gives_the_areas_servers_from_code():
    backend = rk.load_region_backend(_dir_with(json.dumps(FAKE)), "AE")
    hosts = const.LOCAL_KEY_REGIONS["AE"]
    assert backend == {
        "app_id": FAKE["app_id"], "app_secret": FAKE["app_secret"],
        "cert_host": hosts["cert_host"], "control_host": hosts["control_host"],
        "mint_host": hosts["mint_host"]}


def test_the_region_in_the_file_is_read_case_insensitively():
    data = dict(FAKE, region="ae")
    assert rk.load_region_backend(_dir_with(json.dumps(data)), "AE")["app_id"] == FAKE["app_id"]


def test_a_missing_file_names_the_file_and_the_doc():
    _raises(lambda: rk.load_region_backend(_dir_with(None), "AE"),
            rk.KEYS_FILENAME, "docs/LOCAL-REGION-KEYS.md", "AE")


def test_an_area_without_local_keys_is_refused_before_any_file_read():
    for region in ("EU", "NA", "APAC", "SA", ""):
        _raises(lambda r=region: rk.load_region_backend(_dir_with(json.dumps(FAKE)), r),
                "no local keys")


def test_unreadable_or_oversized_files_are_refused_without_their_content():
    _raises(lambda: rk.load_region_backend(_dir_with("{not json"), "AE"), "valid JSON")
    _raises(lambda: rk.load_region_backend(_dir_with(b"\xff\xfe\x00"), "AE"), "valid JSON")
    big = json.dumps(dict(FAKE, app_secret="x" * 5000))
    _raises(lambda: rk.load_region_backend(_dir_with(big), "AE"), "too large")
    # A directory where the file should be is an OSError, not a crash.
    d = tempfile.mkdtemp()
    os.mkdir(os.path.join(d, rk.KEYS_FILENAME))
    _raises(lambda: rk.load_region_backend(d, "AE"), "valid JSON")


def test_the_field_set_is_closed():
    for data in ([FAKE], "AE", {k: v for k, v in FAKE.items() if k != "app_secret"},
                 dict(FAKE, cert_host="evil.example.com"),
                 dict(FAKE, control_host="evil.example.com")):
        _raises(lambda d=data: rk.backend_from_keys(d, "AE"), "exactly")


def test_values_must_be_short_printable_strings():
    for name in ("region", "app_id", "app_secret"):
        for bad in ("", " padded", "tab\there", "x" * 257, 12345, None, "שלום"):
            data = dict(FAKE, **{name: bad})
            _raises(lambda d=data: rk.backend_from_keys(d, "AE"), f"invalid {name}")


def test_keys_for_another_area_are_not_used():
    _raises(lambda: rk.backend_from_keys(dict(FAKE, region="SA"), "AE"), "another region")


def test_every_local_key_area_names_all_three_servers_and_none_elsewhere():
    for region, hosts in const.LOCAL_KEY_REGIONS.items():
        assert set(hosts) == {"cert_host", "control_host", "mint_host"}, region
        assert region not in const.REGIONS and region not in const.UNSUPPORTED_REGIONS, region
