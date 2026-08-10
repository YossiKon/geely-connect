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

# Which backend this entry talks to. "legacy" = the original Geely Global
# platform (Ecarx: ecloudkr/ecloudeu/ecloudus + mTLS certs); "zeekr" = the
# new Geely EM app platform (Zeekr: zeekrlife.com gateways, token auth, no
# mTLS). Absent = legacy, so pre-existing entries are never touched.
CONF_PLATFORM           = "platform"
CONF_ZEEKR_ACCESS_TOKEN = "zeekr_access_token"
CONF_ZEEKR_REFRESH_TOKEN = "zeekr_refresh_token"
CONF_ZEEKR_HF_TOKEN     = "zeekr_hf_token"
CONF_ZEEKR_HF_EXPIRY    = "zeekr_hf_expiry"
CONF_ZEEKR_PASSWORD     = "zeekr_password"
CONF_STORE_PASSWORD     = "store_password"

PLATFORM_LEGACY = "legacy"
PLATFORM_ZEEKR  = "zeekr"
DEFAULT_PLATFORM = PLATFORM_LEGACY
PLATFORM_LABELS: dict[str, str] = {
    PLATFORM_LEGACY: "Existing Geely backend (current integration)",
    PLATFORM_ZEEKR:  "New Geely EM app platform (Zeekr backend) - experimental",
}

# User preferences chosen during setup.
CONF_PRESSURE_UNIT = "pressure_unit"
CONF_LANGUAGE      = "language"
CONF_POLL_MODE     = "poll_mode"
# Opt-in: expose every raw field the server returns as a diagnostic sensor.
# Off by default - it is ~180 entities on an EX5 and buries the useful ones.
CONF_FULL_EXPOSURE = "full_exposure"
# Usable pack size in kWh, for Range At Full Charge. Not in any payload and not
# guessable: the EX5 alone ships 49.52, 60.22 and 68.39 kWh packs, and the WLTP
# figure moves again with the trim's wheels and weight. 0 means "not told", and
# the sensor then extrapolates the car's own estimate as it always did.
CONF_BATTERY_KWH = "battery_capacity_kwh"
# Degrees to add to Exterior Temperature. 0 by default and it must stay that
# way: every synchronised sample anyone has produced reads exactly +10.0 against
# the car's own cluster - three on a Starray, one on an EX5, one after a drive -
# but a sixth, taken on a car parked for hours, read ten degrees the other way.
# So the field is not one thing with an offset, and a constant shipped for
# everyone had to be retracted once already (v1.21.5). This lets an owner who has
# measured their own car against its cluster apply what they measured.
CONF_EXTERIOR_TEMP_OFFSET = "exterior_temp_offset"

# Polling profiles. The Geely backend allows one session per account, so each
# poll briefly logs the phone app out - the mode lets the user trade freshness
# for fewer interruptions. All values are seconds / cycle-counts.
#   base            = interval when parked and something recently changed
#   fast            = interval while charging or driving
#   cap             = max interval when parked and nothing changes (idle back-off)
#   secondary_every = fetch state + scheduled-charging every Nth cycle
#   position_every  = wake the car for fresh GPS every Nth cycle when parked
#   manual          = no timer at all; data is fetched only when you ask
POLL_PROFILES: dict[str, dict] = {
    "eco":    {"base": 300, "fast": 90, "cap": 1800, "secondary_every": 6, "position_every": 12},
    "normal": {"base": 90,  "fast": 30, "cap": 900,  "secondary_every": 4, "position_every": 6},
    "live":   {"base": 45,  "fast": 15, "cap": 300,  "secondary_every": 3, "position_every": 3},
    # Manual sync: the coordinator gets no update_interval, so nothing is
    # fetched on a timer. Every refresh is one the user (or an automation)
    # asked for, and because those are rare there is no reason to ration the
    # secondary endpoints - every sync fetches everything, position included.
    "manual": {"base": 0, "fast": 0, "cap": 0, "secondary_every": 1,
               "position_every": 1, "manual": True},
}
POLL_MODES: dict[str, str] = {
    "eco":    "🔋 Eco (fewest interruptions)",
    "normal": "⚖️ Normal (balanced)",
    "live":   "⚡ Live (freshest, polls most)",
    "manual": "✋ Manual (no automatic polling - you sync)",
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
    "JP": "🇯🇵 Japan (JP)",
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
#
# On the "AVD-verified" labels below: they mark commands taken from an Android
# app capture that predates this repository's public history, so the capture
# itself cannot be produced. Treat the label as "worked when someone tried it",
# not as proof. Where owners have since confirmed the behaviour - the climate
# and seat commands, G-Clean, the charge-server family - it has earned its
# keep. Where owners contradict it, it has been removed: the tailgate entry
# below carried the same label and two owners report the app only unlocking
# and the key fob doing the opening.
# Rapid warm/cool is the exception - see SERVICE_RAPID_*_PATH below.

# --- Lock (verified live) ---
SERVICE_LOCK         = "RDL_2"
SERVICE_UNLOCK       = "RDU_2"
SERVICE_LOCK_PARAMS  = [{"key": "door", "value": "all"}]

# --- Find car (verified live) ---
SERVICE_FIND_CAR        = "RHL"
SERVICE_FIND_CAR_PARAMS = [{"key": "rhl", "value": "horn-light-flash"}]

# --- Tailgate UNLOCK ---
# Uses RDU_2 (door-unlock service) with target=trunk - NOT a separate RTB
# service.
#
# FOUR owners now report that the official app only ever *unlocks* the tailgate
# and never opens it, across three trims - EX5 Inspire, EX5 Tech and P145 PHEV -
# with the powered open coming from the key fob long-press or the car's own
# screen (#14, #20). One owner reports otherwise; his is the single dissenting
# account and his issue stays open for it.
#
# So this is not a missing feature but the same action the app performs: the
# powered open appears not to be exposed by this cloud API at all, and the fob
# talks to the car over a channel nothing here can reach.
#
# Separately, and still open: the latch is confirmed RELEASING on three of those
# four cars (EX5 Tech, EX5 Inspire standard range, P145 PHEV). On the fourth the
# indicators flash and the latch does not move. That is a different fault from
# the powered-open question - the command not reaching the latch at all - and
# drivingSafetyStatus.trunkLockStatus (binary_sensor.<vin>_bs_trunk_unlocked) is
# what distinguishes the two.
#
# AND THE CATALOGUE SAYS AN OPEN COMMAND EXISTS, at least on the EX5. A real
# E245-J1 catalogue read in full for the first time (#20, 2026-08-09) declares an
# internally consistent triple:
#
#     remote_control_lock_2     valueEnum: door
#     remote_control_unlock_2   valueEnum: door,trunk
#     remote_control_open_2     valueEnum: trunk        <- valueEnable true
#
# A command named *open* whose only target is the trunk is a powered tailgate
# release, and that car advertises it as enabled. The three Starray (P145-J1)
# dumps on #11 do NOT carry it: `tailgate.enabled` is derived from that entry
# alone (capabilities.py) and is absent in all three, on integration versions
# 1.16.1 / 1.17.2 / 1.21.3 which all contained the derivation. So the two models
# genuinely differ, and it is the only capability flag that differs between them.
#
# Which leaves this integration in an odd position worth stating plainly: the
# button below is ENABLED BY the open capability and SENDS the unlock command.
# "It unlocks but does not open" is a description of our own code, not only of
# the car. By the RDL_2 / RDU_2 pattern the open service would be RDO_2, and
# nobody has fired it yet - no code change is needed to try, since fire_control
# takes any serviceId:
#
#     service_id: RDO_2
#     params: [{key: target, value: trunk}]
#
# Judge it by the gate, never by the response: RDU_2 answers code 1000 too.
#
# A powered tailgate is also usually an option rather than standard, which would
# let every account here be true at once - but that part is unproven and should
# stay labelled as such.
#
# No capture backs the serviceId spelling, and none backs the "~45s" figure that
# used to be stated here as fact either.
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

# --- Steering wheel heat (captured from the official app, #4, 2026-08-10) ---
# An owner recorded the app's own steering-wheel button against a real car,
# after two rounds of guessed candidates had all been accepted and done
# nothing. The app sends, to the same telematics path this integration uses:
#   ON  → RCE_2 / start / [{rce.heat: "steering_wheel"}], scheduling duration 48
#   OFF → RCE_2 / stop  / [{rce.heat: "steering_wheel"}], no scheduling block
# Two details the guesses got wrong: the value is "steering_wheel" with an
# underscore, unlike the hyphenated seat names on the same key - and no
# rce.level travels with it, which matches the status field never reporting a
# level. The app's scheduling block also differs from control()'s in flags we
# do not replicate (recurrentOperation 1, occurs 0, a "latest" field); every
# verified RCE_2 seat command already works through our shape, but nobody has
# yet confirmed this one moves a wheel through OUR body - judge it by
# steerWhlHeatingSts, never by the response.
#
# The same capture's rapid-warming body (bizType=7) carries the wheel as
# "sw": "true" - the field the "bw" probe candidates were guessing at.
SERVICE_STEERING_HEAT_KEY = "steerWhlHeatingSts"  # status: 1 heating, 2 off, 0 not fitted
RCE_VAL_STEERING_WHEEL    = "steering_wheel"
RCE_STEERING_DURATION_SEC = 48   # what the app sent; meaning of 48 unknown

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
