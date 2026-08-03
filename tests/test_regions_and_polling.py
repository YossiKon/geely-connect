"""Region resolution and polling profiles.

Region resolution decides which backend signs the control commands, so getting
it wrong is what produced the opaque `1501 geelyos verify error`.
"""
from conftest import load

const = load("const")


# ---------------------------------------------------------------- regions ---

def test_tsp_info_wins_over_everything_else():
    # An EU car that also carries a market code must still resolve EU. This is
    # what keeps the APAC support from changing behaviour for existing users.
    v = {"tspInfo": [{"serviceRegion": "EU"}], "saleMarket": "AP", "tcamMarket": "AP"}
    assert const.resolve_vehicle_region(v) == "EU"


def test_edge_info_wins_over_market_codes():
    v = {"edgeInfo": {"code": "eu"}, "saleMarket": "AP"}
    assert const.resolve_vehicle_region(v) == "EU"


def test_known_shapes_resolve():
    cases = {
        "EU": ({"tspInfo": [{"serviceRegion": "EU"}]},
               {"edgeInfo": {"code": "EU"}}),
        "NA": ({"tspInfo": [{"serviceRegion": "NA"}]},
               {"edgeInfo": {"code": "na"}}),
        "APAC": ({"serviceRegion": "APAC"},
                 {"saleMarket": "AP"},
                 {"tcamMarket": "AP"}),
    }
    for want, vehicles in cases.items():
        for v in vehicles:
            assert const.resolve_vehicle_region(v) == want, (want, v)


def test_an_empty_record_resolves_to_nothing_not_a_guess():
    assert const.resolve_vehicle_region({}) is None
    assert const.resolve_vehicle_region({"vin": "x"}) is None


def test_every_supported_region_is_fully_configured():
    for name, cfg in const.REGIONS.items():
        for key in ("app_id", "app_secret", "cert_host", "control_host"):
            assert cfg.get(key), f"{name} missing {key}"
        assert cfg["control_host"].startswith("apis."), name
        assert cfg["cert_host"].startswith("api."), name


def test_region_config_falls_back_instead_of_raising():
    # A backend that invents a new region code must not take setup down.
    assert const.region_config("NOT_A_REGION") == const.REGIONS[const.DEFAULT_REGION]
    assert const.region_config(None) == const.REGIONS[const.DEFAULT_REGION]


def test_regions_and_unsupported_regions_do_not_overlap():
    assert not set(const.REGIONS) & set(const.UNSUPPORTED_REGIONS)


def test_market_codes_map_somewhere_real():
    for code, region in const.MARKET_TO_REGION.items():
        assert region in const.REGIONS or region in const.UNSUPPORTED_REGIONS, code


def test_only_the_pinned_control_hosts_are_used():
    api = load("api")
    for cfg in const.REGIONS.values():
        assert cfg["control_host"] in api._BUNDLED_TLS_PINS, cfg["control_host"]


# ---------------------------------------------------------------- polling ---

def test_every_poll_mode_has_a_profile_and_vice_versa():
    assert set(const.POLL_MODES) == set(const.POLL_PROFILES)


def test_default_poll_mode_exists_and_is_a_timed_one():
    assert const.DEFAULT_POLL_MODE in const.POLL_PROFILES
    assert not const.POLL_PROFILES[const.DEFAULT_POLL_MODE].get("manual")


def test_manual_is_the_only_profile_without_a_timer():
    flagged = {k for k, v in const.POLL_PROFILES.items() if v.get("manual")}
    assert flagged == {"manual"}


def test_manual_fetches_everything_on_each_sync():
    # Syncs are rare in this mode, so there is no reason to ration endpoints.
    m = const.POLL_PROFILES["manual"]
    assert m["secondary_every"] == 1 and m["position_every"] == 1


def test_timed_profiles_are_ordered_and_sane():
    eco, normal, live = (const.POLL_PROFILES[k] for k in ("eco", "normal", "live"))
    assert eco["base"] > normal["base"] > live["base"]
    assert eco["cap"] > normal["cap"] > live["cap"]
    for p in (eco, normal, live):
        assert p["fast"] < p["base"] <= p["cap"]
        assert p["secondary_every"] >= 1 and p["position_every"] >= 1


def test_country_list_is_sane():
    assert const.DEFAULT_COUNTRY_CODE in const.SUPPORTED_COUNTRIES
    for code in const.SUPPORTED_COUNTRIES:
        assert len(code) == 2 and code.isupper(), code
