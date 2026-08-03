"""Constants for the Geely (international) integration.

ServiceId catalog and parameter shapes are AVD-Frida-verified
(see docs/AVD_CAPTURE_GUIDE.md). They reflect the actual Android Geely
Global app's network calls captured live via OkHttp interception.
"""
# -----------------------------------------------------------------------------
# Portions of this file - the reverse-engineered Geely protocol / field mappings
# (the parts that required protocol research) - are derived from
# nitaybz/geely-global-ha, used under the MIT License. See NOTICE.txt.
# Original framework, security hardening and transport are our own work.
# -----------------------------------------------------------------------------

DOMAIN = "geely_connect"


# ---------------------------------------------------------------------------
# Regional backends
# ---------------------------------------------------------------------------
# Geely runs a separate backend per area, each with its own app credentials.
# The area belongs to the VEHICLE's telematics registration, not to the country
# typed at setup - a Brazilian account can have a car registered in NA - so it
# is read from the /controlCars response (tspInfo[].serviceRegion, falling back
# to edgeInfo.code) rather than from CONF_COUNTRY_CODE.
#
# Login, OTP and the vehicle list are NOT regional in practice: accounts in
# every region reach them through the EU host today, and only certificate
# provisioning and control commands are rejected. So a region swaps just
# `cert_host`, `control_host` and the signing credentials.
REGIONS: dict[str, dict[str, str]] = {
    "EU": {
        "app_id":       "GEELYE245",
        "app_secret":   "48d6fff3ea19447bbf6f3ed76a608ff9",
        "cert_host":    "api.ecloudeu.com",
        "control_host": "apis.ecloudeu.com",
    },
    "NA": {
        "app_id":       "GEELYUS",
        "app_secret":   "cd3a278dc4e844ca8a1c22f7b2447a0e",
        "cert_host":    "api.ecloudus.com",
        "control_host": "apis.ecloudus.com",
    },
    "APAC": {
        "app_id":       "GEELYE245",
        "app_secret":   "5b2e7f2f569e4173a9aea65e0c9133e3",
        "cert_host":    "api.ecloudkr.com",
        "control_host": "apis.ecloudkr.com",
    },
}

# Areas whose hosts are known but whose app credentials have never been
# captured. Listed separately so an account from one of them fails with a clear
# message instead of being silently signed against the European backend, which
# only produces the confusing "geelyos verify error".
UNSUPPORTED_REGIONS: dict[str, str] = {
    "SA":   "tsp-geely-api-sa.xcloudsvc.com",
}

DEFAULT_REGION = "EU"

# Vehicle / client metadata sent in headers during control commands.
CLIENT_ID      = "OOGLE0000APPE64ARM64264T31485278"
VEHICLE_SERIES = "E245-J1"

# Polling cadence
SCAN_INTERVAL_SECONDS = 90

# ConfigEntry data keys
CONF_EMAIL              = "email"
CONF_COUNTRY_CODE       = "country_code"
CONF_REGION             = "region"
CONF_CIDPSSO_TOKEN      = "cidpsso_token"
CONF_USER_ID            = "user_id"
CONF_VIN                = "vin"
CONF_DEVICE_ID          = "device_id"
CONF_CERT_PATH          = "cert_path"
CONF_KEY_PATH           = "key_path"
CONF_DEVICE_IDFA        = "device_idfa"
CONF_DEVICE_IDFV        = "device_idfv"
CONF_VEHICLE_NICKNAME   = "vehicle_nickname"
CONF_VEHICLE_SERIES     = "vehicle_series"
CONF_VEHICLE_MODEL_CODE = "vehicle_model_code"
CONF_VEHICLE_COLOR      = "vehicle_color"
CONF_VEHICLE_POWER_TYPE = "vehicle_power_type"

# User preferences chosen during setup.
CONF_PRESSURE_UNIT = "pressure_unit"
CONF_LANGUAGE      = "language"
CONF_POLL_MODE     = "poll_mode"
# Opt-in: expose every raw field the server returns as a diagnostic sensor.
# Off by default - it is ~180 entities on an EX5 and buries the useful ones.
CONF_FULL_EXPOSURE = "full_exposure"

# Polling profiles. The Geely backend allows one session per account, so each
# poll briefly logs the phone app out - the mode lets the user trade freshness
# for fewer interruptions. All values are seconds / cycle-counts.
#   base            = interval when parked and something recently changed
#   fast            = interval while charging or driving
#   cap             = max interval when parked and nothing changes (idle back-off)
#   secondary_every = fetch state + scheduled-charging every Nth cycle
#   position_every  = wake the car for fresh GPS every Nth cycle when parked
POLL_PROFILES: dict[str, dict] = {
    "eco":    {"base": 300, "fast": 90, "cap": 1800, "secondary_every": 6, "position_every": 12},
    "normal": {"base": 90,  "fast": 30, "cap": 900,  "secondary_every": 4, "position_every": 6},
    "live":   {"base": 45,  "fast": 15, "cap": 300,  "secondary_every": 3, "position_every": 3},
}
POLL_MODES: dict[str, str] = {
    "eco":    "🔋 Eco (fewest interruptions)",
    "normal": "⚖️ Normal (balanced)",
    "live":   "⚡ Live (freshest, polls most)",
}
DEFAULT_POLL_MODE = "normal"

DEFAULT_COUNTRY_CODE = "GB"

# Tire-pressure display units, rendered as a dropdown in the config flow. The
# sensors report the car's native kPa and Home Assistant does the conversion,
# so these are display choices only - no conversion factors live here.
PRESSURE_UNITS: dict[str, str] = {
    "psi": "PSI",
    "bar": "bar",
    "kPa": "kPa",
}
DEFAULT_PRESSURE_UNIT = "psi"

# Entity display language chosen at setup ("auto" follows Home Assistant's UI).
LANGUAGES: dict[str, str] = {
    "auto": "Automatic (Home Assistant language)",
    "he": "עברית",
    "en": "English",
}
DEFAULT_LANGUAGE = "auto"

# Countries whose accounts reach a backend this integration has credentials
# for (EU or NA). Rendered as a dropdown in the config flow. code -> display
# label. The vehicle's actual area still comes from the login response, so a
# country here is a starting point, not a guarantee.
SUPPORTED_COUNTRIES: dict[str, str] = {
    # EU / International backend, alphabetical by country name.
    "AT": "🇦🇹 Austria (AT)",
    "BE": "🇧🇪 Belgium (BE)",
    "BG": "🇧🇬 Bulgaria (BG)",
    "HR": "🇭🇷 Croatia (HR)",
    "CY": "🇨🇾 Cyprus (CY)",
    "CZ": "🇨🇿 Czechia (CZ)",
    "DK": "🇩🇰 Denmark (DK)",
    "EE": "🇪🇪 Estonia (EE)",
    "FI": "🇫🇮 Finland (FI)",
    "FR": "🇫🇷 France (FR)",
    "DE": "🇩🇪 Germany (DE)",
    "GR": "🇬🇷 Greece (GR)",
    "HU": "🇭🇺 Hungary (HU)",
    "IS": "🇮🇸 Iceland (IS)",
    "IE": "🇮🇪 Ireland (IE)",
    "IL": "🇮🇱 Israel (IL)",
    "IT": "🇮🇹 Italy (IT)",
    "LV": "🇱🇻 Latvia (LV)",
    "LT": "🇱🇹 Lithuania (LT)",
    "LU": "🇱🇺 Luxembourg (LU)",
    "MT": "🇲🇹 Malta (MT)",
    "NL": "🇳🇱 Netherlands (NL)",
    "NO": "🇳🇴 Norway (NO)",
    "PL": "🇵🇱 Poland (PL)",
    "PT": "🇵🇹 Portugal (PT)",
    "RO": "🇷🇴 Romania (RO)",
    "SK": "🇸🇰 Slovakia (SK)",
    "SI": "🇸🇮 Slovenia (SI)",
    "ES": "🇪🇸 Spain (ES)",
    "SE": "🇸🇪 Sweden (SE)",
    "CH": "🇨🇭 Switzerland (CH)",
    "GB": "🇬🇧 United Kingdom (GB)",
    # North-American backend (api.ecloudus.com). Brazilian accounts have been
    # reported to resolve to this area too, despite the SA hosts existing.
    "BR": "🇧🇷 Brazil (BR)",
    "CA": "🇨🇦 Canada (CA)",
    "MX": "🇲🇽 Mexico (MX)",
    "US": "🇺🇸 United States (US)",
    # APAC backend (api.ecloudkr.com). The vehicle's actual area still comes
    # from the login response; these countries are the common APAC ones.
    "AU": "🇦🇺 Australia (AU)",
    "HK": "🇭🇰 Hong Kong (HK)",
    "ID": "🇮🇩 Indonesia (ID)",
    "KR": "🇰🇷 South Korea (KR)",
    "MY": "🇲🇾 Malaysia (MY)",
    "NZ": "🇳🇿 New Zealand (NZ)",
    "PH": "🇵🇭 Philippines (PH)",
    "SG": "🇸🇬 Singapore (SG)",
    "TH": "🇹🇭 Thailand (TH)",
    "VN": "🇻🇳 Vietnam (VN)",
}


def region_config(region: str | None) -> dict[str, str]:
    """Resolve an area code to its backend config.

    Unknown areas fall back to EU, which is right for a blank or unrecognised
    value; areas in UNSUPPORTED_REGIONS are rejected by the config flow before
    they get here, so this never silently signs a foreign account with European
    credentials."""
    return REGIONS.get((region or DEFAULT_REGION).upper(), REGIONS[DEFAULT_REGION])


# Market-area codes seen on /controlCars records that do NOT match the
# backend region codes used elsewhere (REGIONS keys). saleMarket/tcamMarket
# on APAC-market cars report "AP" (Asia-Pacific) while the backend key is
# "APAC"; EU/NA records report codes that already match, so they pass
# through unchanged.
MARKET_TO_REGION: dict[str, str] = {
    "AP": "APAC",
}


def resolve_vehicle_region(vehicle: dict) -> str | None:
    """Read the telematics area out of a /controlCars vehicle record.

    `tspInfo` is a list of per-service entries that each carry a
    `serviceRegion`; `edgeInfo.code` carries the same area on some accounts.
    Some backends (e.g. APAC/Korea) return a top-level `serviceRegion` field
    instead of either of those, so fall back to it, and finally to the
    `saleMarket` / `tcamMarket` market codes (e.g. "AP" -> APAC).
    """
    tsp = vehicle.get("tspInfo")
    if isinstance(tsp, list):
        for entry in tsp:
            if isinstance(entry, dict) and entry.get("serviceRegion"):
                return str(entry["serviceRegion"]).upper()
    edge = vehicle.get("edgeInfo")
    if isinstance(edge, dict) and edge.get("code"):
        return str(edge["code"]).upper()
    top = vehicle.get("serviceRegion")
    if top:
        return str(top).upper()
    for key in ("saleMarket", "tcamMarket"):
        mkt = vehicle.get(key)
        if mkt:
            code = str(mkt).upper()
            return MARKET_TO_REGION.get(code, code)
    return None


SERIES_TO_FRIENDLY_NAME: dict[str, str] = {
    "E245-J1": "EX5",
}

# === serviceId catalog ===
# Most controls fire `PUT /remote-control/vehicle/telematics/{VIN}` with
# `{serviceId, command, serviceParameters: [{key, value}, ...]}`.
# Rapid warm/cool is the exception - see SERVICE_RAPID_*_PATH below.

# --- Lock (verified live) ---
SERVICE_LOCK         = "RDL_2"
SERVICE_UNLOCK       = "RDU_2"
SERVICE_LOCK_PARAMS  = [{"key": "door", "value": "all"}]

# --- Find car (verified live) ---
SERVICE_FIND_CAR        = "RHL"
SERVICE_FIND_CAR_PARAMS = [{"key": "rhl", "value": "horn-light-flash"}]

# --- Tailgate UNLOCK (AVD-verified 2026-05-01) ---
# Uses RDU_2 (door-unlock service) with target=trunk - NOT a separate RTB
# service. Auto-relocks ~45s if not physically opened.
SERVICE_TAILGATE        = "RDU_2"
SERVICE_TAILGATE_PARAMS  = [{"key": "target", "value": "trunk"}]

# --- Climate (AVD-verified 2026-05-01) ---
# Master serviceId for AC, defrost, seat heat, seat vent on the Android app.
SERVICE_CLIMATE = "RCE_2"
# Param key + values inside RCE_2:
RCE_KEY_CONDITIONER = "rce.conditioner"   # value 1=AC, 2=defrost
RCE_VAL_AC          = "1"
RCE_VAL_DEFROST     = "2"
RCE_KEY_TEMP        = "rce.temp"          # AC target temp, "15.5".."28.5" str
RCE_KEY_LEVEL       = "rce.level"         # level "1"|"2"|"3" (or "0" with stop)
RCE_KEY_HEAT        = "rce.heat"          # value = seat name (front-left/right)
RCE_KEY_VENT        = "rce.ventilation"   # value = seat name OR "cabin" (RCC_2)
SEAT_FRONT_LEFT     = "front-left"
SEAT_FRONT_RIGHT    = "front-right"
SEAT_REAR_LEFT      = "rear-left"
SEAT_REAR_RIGHT     = "rear-right"
RCE_DURATION_SECONDS = 90    # default duration for seat features (matches AVD)
RCE_AC_DURATION_SEC = 180    # default duration for AC (matches AVD)

# --- G-clean (AVD-verified) - ventilation pulse, ~6s/burst ---
# Reuses serviceId RCC_2 (different from RCE_2). Param value "cabin".
SERVICE_GCLEAN          = "RCC_2"
SERVICE_GCLEAN_PARAMS   = [{"key": "rcc.ventilation", "value": "cabin"}]
SERVICE_GCLEAN_DURATION = 6

# --- Engine pre-conditioning (verified earlier; not used by Android app) ---

# --- Parking Comfort (UNVERIFIED on this trim) ---
SERVICE_PARKING_COMFORT = "RSM"

# --- charge-server family (AVD-verified) ---
# All these go to POST /charge-server/ecarx_charge_set/{VIN}.
# bizType picks the feature; the body shape varies per bizType.
CHARGE_SERVER_PATH = "/charge-server/ecarx_charge_set"
BIZ_TYPE_PARKING_COMFORT  = "4"   # GET to read schedule, POST to set
BIZ_TYPE_SCHED_CHARGING   = "6"   # rbc fields (rbcStartTime, rbcEndTime, rbcTarget, rbc, rbcModel, pin)
BIZ_TYPE_RAPID            = "7"   # ac+heat[]/ventilation[]+temp+vlt
RAPID_DEFAULT_DURATION    = "180"
RAPID_DEFAULT_VLT_POS     = "12"
RAPID_DEFAULT_VLT_DUR     = "60"
# Legacy aliases
SERVICE_RAPID_PATH      = CHARGE_SERVER_PATH
SERVICE_RAPID_BIZ_TYPE  = BIZ_TYPE_RAPID

# --- Steering wheel heat (read field exists, command unverified on this trim) ---
SERVICE_STEERING_HEAT_KEY = "steerWhlHeatingSts"  # status field

# --- Window / sunshade / sunroof / ventilate (verified) ---
SERVICE_WINDOW = "RWS_2"
SERVICE_WINDOW_VENT_PARAMS = [{"key": "target", "value": "ventilate"}]

# --- Charging (AVD-verified 2026-05-01) ---
SERVICE_CHARGING            = "RCS"
SERVICE_CHARGING_START_PARAMS = [
    {"key": "operation",   "value": "1"},
    {"key": "rcs.restart", "value": "1"},
]
SERVICE_CHARGING_STOP_PARAMS = [
    {"key": "operation",     "value": "0"},
    {"key": "rcs.terminate", "value": "1"},
]
# Legacy aliases (kept for old switch.py import path)

# --- Capability discovery endpoint ---
# GET /geelyTCAccess/tcservices/capability/{VIN}?pageSize=2000&pageIndex=1&vehicleType=0
# Returns the per-vehicle feature catalog. Used to build dynamic entities.
CAPABILITY_PATH = "/geelyTCAccess/tcservices/capability"

# === Climate entity defaults (overridden by capability if available) ===
CLIMATE_MIN_TEMP_C  = 15.5
CLIMATE_MAX_TEMP_C  = 28.5
CLIMATE_TEMP_STEP_C = 0.5
CLIMATE_SEAT_LEVELS = ["Off", "Low", "Medium", "High"]   # index = level

# === Climate preset names ===
# HA uses the preset_mode value as-is in the dropdown UI, so user-facing
# labels go directly here. PRESET_NONE stays lowercase because HA's
# frontend has built-in "None" rendering for the "no preset" state.
PRESET_NONE          = "none"
PRESET_RAPID_WARMING = "Rapid Warming"
PRESET_RAPID_COOLING = "Rapid Cooling"
