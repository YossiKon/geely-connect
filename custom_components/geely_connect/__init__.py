"""Geely (international) Home Assistant integration."""
from __future__ import annotations

import asyncio
import functools
import logging
import os
import shutil
import socket
from datetime import timedelta

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers import config_validation as cv, device_registry as dr, entity_registry as er
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from . import api as geely_api
from . import propulsion
from .api import GeelyApi, GeelyAuthError, GeelyControlError, GeelyTLSPinError, redact
from .const import (
    CLIENT_ID,
    VEHICLE_SERIES,
    CONF_COUNTRY_CODE,
    CONF_CERT_PATH,
    CONF_CIDPSSO_TOKEN,
    CONF_DEVICE_ID,
    CONF_DEVICE_IDFA,
    CONF_DEVICE_IDFV,
    CONF_EMAIL,
    CONF_FULL_EXPOSURE,
    CONF_KEY_PATH,
    CONF_POLL_MODE,
    CONF_REGION,
    CONF_USER_ID,
    CONF_VEHICLE_MODEL_CODE,
    CONF_VEHICLE_NICKNAME,
    CONF_VEHICLE_POWER_TYPE,
    CONF_VEHICLE_SERIES,
    CONF_VIN,
    DEFAULT_COUNTRY_CODE,
    DEFAULT_POLL_MODE,
    DOMAIN,
    POLL_PROFILES,
    SCAN_INTERVAL_SECONDS,
    SERIES_TO_FRIENDLY_NAME,
    region_config,
)
from .helpers import vehicle_metadata

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[str] = [
    "sensor", "binary_sensor", "device_tracker",
    "lock", "climate", "switch", "select", "cover", "button", "time",
]


def _resolve_device_name(entry_data: dict) -> str:
    """Friendly name for the HA device record.

    Format: `<base_name> (<last4>)` where:
      * `<base_name>` is the iOS-app nickname if it's distinctive, else
        "Geely <pretty>" (e.g. "Geely EX5"). If the nickname already
        contains the model, the model isn't repeated.
      * `<last4>` is the last 4 characters of the VIN - guarantees that
        users with multiple cars of the same model get distinct device
        names + entity IDs.
    """
    nickname = (entry_data.get(CONF_VEHICLE_NICKNAME) or "").strip()
    vin = entry_data.get(CONF_VIN) or ""
    last4 = vin[-4:] if len(vin) >= 4 else vin
    series_code = (
        entry_data.get(CONF_VEHICLE_MODEL_CODE)
        or entry_data.get(CONF_VEHICLE_SERIES)
        or ""
    )
    pretty = SERIES_TO_FRIENDLY_NAME.get(series_code) or series_code

    # Decide the base name (without VIN suffix yet).
    custom_nickname = (
        nickname
        and nickname.lower() not in {"my geely", "geely", (pretty or "").lower()}
    )
    if custom_nickname:
        if pretty and pretty.lower() in nickname.lower():
            base = nickname
        elif pretty:
            base = f"{nickname} {pretty}"
        else:
            base = nickname
    elif pretty:
        base = f"Geely {pretty}"
    else:
        base = "Geely"

    return f"{base} ({last4})" if last4 else base


_OBSOLETE_UNIQUE_ID_PATTERNS: tuple[str, ...] = (
    # Old engine switch - replaced by climate entity
    "_sw_engine_pre_conditioning",
    # Old PROBE buttons - replaced by proper entities
    "_btn_probe_",
    # Old confirmed buttons that are now lock / climate / button
    "_btn_RDL_2", "_btn_RDU_2", "_btn_RES", "_btn_RWS_2", "_btn_RHL",
    # Old "Tailgate" button - renamed to Unlock Trunk (different unique_id)
    "_btn_tailgate",
    # Old rapid warming/cooling/g-clean buttons - moved to climate presets / switch
    "_btn_rapid_warming", "_btn_rapid_cooling", "_btn_g_clean",
    # Old gear sensor - gearPosition not in current API response
    "_gear",
    # Removed after EX5 feature audit: no sentry mode (no cabin camera).
    # The rear seat-heat selects are NOT listed here: select.py still creates
    # them whenever the capability catalog reports rear positions, so listing
    # them made every restart delete and re-register a live entity, losing its
    # recorded history and any customisation each time.
    "_sw_sentry_mode",
    # `charge_state` (chargeSts) field is unreliable.
    "_charge_state",
    # Binary sensors made redundant by the new lock/switch/climate entities.
    # The `_bs_` prefix avoids accidentally matching the new switches.
    "_bs_doors_unlocked",      # → lock.doors
    "_bs_defrost_active",      # → switch.defrost
    "_bs_preclimate_active",   # → climate.climate (hvac_mode)
    "_bs_charging",            # → switch.charging
    # Sensor renames (key changed → unique_id changed → entity needs purge)
    "_odometer",               # → renamed to total_mileage
    "_tyre_pressure_fl",       # → tire_pressure_fl  (UK→US spelling)
    "_tyre_pressure_fr",       # → tire_pressure_fr
    "_tyre_pressure_rl",       # → tire_pressure_rl
    "_tyre_pressure_rr",       # → tire_pressure_rr
    # Door binary sensor renames - "Door <Position>" so they group
    # alphabetically in HA's device page.
    "_bs_driver_door_open",    # → door_driver
    "_bs_passenger_door_open", # → door_passenger
    "_bs_rear_left_door_open", # → door_rear_left
    "_bs_rear_right_door_open",# → door_rear_right
)


async def _maybe_refetch_vehicle_metadata(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """If the entry was created before vehicle metadata was tracked (v1
    entries), or the user renamed the vehicle on iOS, re-fetch from
    /controlCars and update the entry. Best-effort - silently skips on
    error so a hiccup never blocks setup."""
    have = entry.data.get(CONF_VEHICLE_NICKNAME)
    have_series = entry.data.get(CONF_VEHICLE_SERIES) or entry.data.get(CONF_VEHICLE_MODEL_CODE)
    if have and have_series:
        return
    try:
        # The install fingerprint has to go with it. Without idfa/idfv,
        # _ios_headers invents a fresh random device identity, and Geely
        # allows one session per account - so this self-heal call would kick
        # the phone app off, which is the exact thing the v1 -> v2 migration
        # generated a stable fingerprint to avoid.
        all_v = await hass.async_add_executor_job(
            functools.partial(
                geely_api.list_vehicles,
                entry.data.get(CONF_CIDPSSO_TOKEN),
                entry.data.get(CONF_USER_ID),
                entry.data.get(CONF_COUNTRY_CODE, DEFAULT_COUNTRY_CODE),
                idfa=entry.data.get(CONF_DEVICE_IDFA),
                idfv=entry.data.get(CONF_DEVICE_IDFV),
            )
        )
    except Exception as e:  # noqa: BLE001
        _LOGGER.debug("metadata refetch failed (non-fatal): %s", e)
        return
    target_vin = entry.data.get(CONF_VIN)
    match = next((v for v in all_v if v.get("vin") == target_vin), None)
    if not match:
        return
    new_data = {**entry.data, **vehicle_metadata(match)}
    hass.config_entries.async_update_entry(entry, data=new_data)
    # Last 4 VIN characters only, matching _resolve_device_name, so a shared
    # log or screenshot does not reveal the full VIN.
    _LOGGER.info("Refreshed vehicle metadata for ...%s: %s",
                 (target_vin or "")[-4:], new_data[CONF_VEHICLE_NICKNAME])


def _purge_obsolete_entities(hass: HomeAssistant, entry: ConfigEntry) -> int:
    """Remove entities from prior versions of the integration. Walks ALL
    entries (not just config-entry-linked ones) since some orphans get
    detached from the config entry across reloads."""
    registry = er.async_get(hass)
    to_delete = [
        e.entity_id for e in registry.entities.values()
        if e.platform == DOMAIN
        and any(p in e.unique_id for p in _OBSOLETE_UNIQUE_ID_PATTERNS)
    ]
    for eid in to_delete:
        registry.async_remove(eid)
    if to_delete:
        _LOGGER.info("Purged %d obsolete entities: %s", len(to_delete), to_delete)
    return len(to_delete)


def _refresh_device_name(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Push the resolved friendly name into the device registry so the UI
    updates when the user renames the vehicle on iOS or when v1 entries
    self-heal their metadata."""
    device_registry = dr.async_get(hass)
    vin = entry.data.get(CONF_VIN)
    if not vin:
        return
    device = device_registry.async_get_device(identifiers={(DOMAIN, vin)})
    if device is None:
        return
    new_name = _resolve_device_name(entry.data)
    if device.name != new_name and not device.name_by_user:
        device_registry.async_update_device(device.id, name=new_name)
        _LOGGER.info("Updated device name: %r → %r", device.name, new_name)


# --- Efficient polling ------------------------------------------------------
# The Geely backend allows only ONE active session per account, so every poll
# briefly logs the phone app out. Polling is kept as light as possible: few
# calls per cycle and long intervals whenever nothing changes. The user picks a
# profile (Eco / Normal / Live) at setup; see POLL_PROFILES in const.py.
_QUIET_HOURS = range(0, 6)       # local 00:00-05:59 → back off to the cap


def _poll_flags(d: dict) -> tuple[bool, bool]:
    """(charging, driving) from a status dict, tolerant of missing fields."""
    if not isinstance(d, dict):
        return False, False
    vs = d.get("vehicleStatus") or {}
    add = vs.get("additionalVehicleStatus") or {}
    ev = add.get("electricVehicleStatus") or {}
    basic = vs.get("basicVehicleStatus") or {}
    charging = str(ev.get("statusOfChargerConnection")) == "3"
    try:
        driving = float(basic.get("speed")) > 0
    except (TypeError, ValueError):
        driving = False
    return charging, driving


def _poll_signature(d: dict) -> tuple:
    """Small tuple of the fields that matter for 'did anything change?'."""
    vs = (d or {}).get("vehicleStatus") or {}
    add = vs.get("additionalVehicleStatus") or {}
    ev = add.get("electricVehicleStatus") or {}
    safe = add.get("drivingSafetyStatus") or {}
    basic = vs.get("basicVehicleStatus") or {}
    return (
        ev.get("chargeLevel"), ev.get("distanceToEmptyOnBatteryOnly"),
        ev.get("statusOfChargerConnection"), safe.get("centralLockingStatus"),
        basic.get("speed"),
    )


def _adaptive_interval(data: dict, idle_streak: int, profile: dict) -> timedelta:
    """Pick the next poll interval for the chosen profile.

    - charging or driving  → the profile's FAST interval (live when it matters)
    - quiet hours & parked → the profile's idle cap (near-silent overnight)
    - otherwise            → exponential back-off from base up to cap, the
      longer nothing changes. This slashes the number of sessions we open
      (and phone-app logouts) when the car just sits parked.
    """
    charging, driving = _poll_flags(data)
    if charging or driving:
        return timedelta(seconds=profile["fast"])
    try:
        if dt_util.now().hour in _QUIET_HOURS:
            return timedelta(seconds=profile["cap"])
    except Exception:  # noqa: BLE001
        pass
    secs = min(profile["cap"], profile["base"] * (2 ** min(idle_streak, 4)))
    return timedelta(seconds=secs)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Geely (international) from a config entry."""
    await _maybe_refetch_vehicle_metadata(hass, entry)
    _purge_obsolete_entities(hass, entry)
    _refresh_device_name(hass, entry)

    d = entry.data
    series_code = (
        d.get(CONF_VEHICLE_MODEL_CODE)
        or d.get(CONF_VEHICLE_SERIES)
        or VEHICLE_SERIES
    )
    # Entries created before regions were tracked carry no CONF_REGION and
    # resolve to EU, which is the backend they were provisioned against.
    backend = region_config(d.get(CONF_REGION))
    api = GeelyApi(
        app_id=backend["app_id"],
        app_secret=backend["app_secret"],
        user_id=d[CONF_USER_ID],
        vin=d[CONF_VIN],
        cidpsso_token=d[CONF_CIDPSSO_TOKEN],
        client_id=CLIENT_ID,
        vehicle_series=series_code,
        vehicle_model=series_code,
        device_id=d[CONF_DEVICE_ID],
        cert_path=d[CONF_CERT_PATH],
        key_path=d[CONF_KEY_PATH],
        control_host=backend["control_host"],
        email=d.get(CONF_EMAIL),
    )

    _SUCCESS_CODES = {1000, "1000", 10000000, "10000000", None}

    # Transient network errors that warrant retry rather than failing the poll.
    # gaierror = DNS lookup failure (Errno -3 EAI_AGAIN); the rest are typical
    # cloud-API transient hiccups.
    _TRANSIENT_EXC = (socket.gaierror, ConnectionError, TimeoutError, OSError)

    async def _call_with_retry(func, *args, attempts=3, delay=2.0):
        """Run an executor job with retry on transient network errors. Auth
        failures bubble immediately; non-transient exceptions also bubble."""
        last_exc: Exception | None = None
        for i in range(attempts):
            try:
                return await hass.async_add_executor_job(func, *args)
            except GeelyAuthError:
                raise
            except _TRANSIENT_EXC as e:
                last_exc = e
                _LOGGER.debug("transient %s on %s (attempt %d/%d): %s",
                              type(e).__name__, getattr(func, "__name__", "?"),
                              i + 1, attempts, e)
                if i + 1 < attempts:
                    await asyncio.sleep(delay)
        assert last_exc is not None
        raise last_exc

    # Closure state: tolerate up to N consecutive failures before marking
    # entities unavailable. With SCAN_INTERVAL_SECONDS=90 and N=2 we need
    # ~3min of sustained failure before HA reports unavailable.
    _FAILURE_TOLERANCE = 2
    fail_state = {"consecutive": 0}
    # Efficient-polling state: cycle counter, idle streak, last-change signature.
    poll_state = {"cycle": 0, "idle": 0, "sig": None}
    # Chosen polling profile (Eco / Normal / Live) from setup.
    profile = POLL_PROFILES.get(
        # Options win over data: the polling mode can be changed after setup.
        entry.options.get(CONF_POLL_MODE)
        or entry.data.get(CONF_POLL_MODE, DEFAULT_POLL_MODE),
        POLL_PROFILES[DEFAULT_POLL_MODE]
    )
    _SECONDARY_EVERY = profile["secondary_every"]
    _POSITION_EVERY = profile["position_every"]
    # Manual mode: no timer. The coordinator still refreshes on demand - the
    # Refresh Data button, homeassistant.update_entity, and the automatic
    # post-command polls - it just never starts one by itself.
    _MANUAL = bool(profile.get("manual"))

    async def _async_update():
        poll_state["cycle"] += 1
        cyc = poll_state["cycle"]
        prev = coordinator.data if coordinator is not None else None
        prev = prev if isinstance(prev, dict) else {}
        was_charging, was_driving = _poll_flags(prev)

        # Position wake (PAI) is expensive and actually wakes the car, so we do
        # NOT fire it every cycle. Only while driving, or once every Nth cycle
        # when parked. The rest of the time we serve the last-known GPS.
        if was_driving or (cyc % _POSITION_EVERY == 1):
            try:
                await _call_with_retry(api.request_position_refresh)
            except GeelyAuthError as e:
                raise ConfigEntryAuthFailed(str(e)) from e
            except GeelyTLSPinError as e:
                # A pin failure means the server key changed - an active
                # MITM or a legitimate rotation. Never hide it at DEBUG.
                _LOGGER.error("position refresh: TLS pin check failed: %s", e)
            except Exception as e:  # noqa: BLE001
                _LOGGER.debug("position-refresh PAI non-fatal failure: %s", e)

        # Primary status - the one call we always make.
        try:
            resp = await _call_with_retry(api.vehicle_status)
        except GeelyAuthError as e:
            raise ConfigEntryAuthFailed(str(e)) from e
        except Exception as e:  # noqa: BLE001
            fail_state["consecutive"] += 1
            if fail_state["consecutive"] <= _FAILURE_TOLERANCE and prev:
                _LOGGER.warning(
                    "vehicle_status failed (%d/%d consecutive); reusing last "
                    "snapshot: %s", fail_state["consecutive"], _FAILURE_TOLERANCE, e,
                )
                return prev
            raise UpdateFailed(f"vehicle_status: {e}") from e
        code = resp.get("code")
        data = resp.get("data")
        if code not in _SUCCESS_CODES:
            raise UpdateFailed(
                f"vehicle_status code={code!r} msg={resp.get('msg')!r} "
                f"keys={sorted(resp.keys())}"
            )
        if not isinstance(data, dict):
            _LOGGER.debug("vehicle_status returned non-dict data: top-level=%r", redact(resp))
            data = {}

        charging, driving = _poll_flags(data)

        # Secondary endpoints (parking-comfort/sentry flags + scheduled charging)
        # change rarely, so we only fetch them when charging or every Nth cycle;
        # otherwise we carry the previous values forward. This roughly halves the
        # calls per cycle when the car is just parked.
        # Carry the last known values forward FIRST, then overwrite them if a
        # fetch succeeds. Doing it the other way round meant an attempted-but-
        # failed fetch dropped the keys entirely - strictly worse than skipping
        # the call - and because the next cycle reads `prev` from this same
        # damaged snapshot, the loss repaired itself only when a fetch finally
        # succeeded. Parking comfort, scheduled charging and both schedule-time
        # entities read unknown for that whole window.
        if "_state" in prev:
            data["_state"] = prev["_state"]
        if "_scheduled_charging" in prev:
            data["_scheduled_charging"] = prev["_scheduled_charging"]

        if charging or was_charging or (cyc % _SECONDARY_EVERY == 1):
            try:
                state_resp = await _call_with_retry(api.vehicle_status_state)
                if state_resp.get("code") in _SUCCESS_CODES and isinstance(state_resp.get("data"), dict):
                    data["_state"] = state_resp["data"]
            except GeelyAuthError as e:
                raise ConfigEntryAuthFailed(str(e)) from e
            except GeelyTLSPinError as e:
                # A pin failure means the server key changed - an active
                # MITM or a legitimate rotation. Never hide it at DEBUG.
                _LOGGER.error("vehicle state fetch: TLS pin check failed: %s", e)
            except Exception as e:  # noqa: BLE001
                _LOGGER.debug("vehicle_status_state non-fatal failure: %s", e)
            try:
                sc = await _call_with_retry(api.charge_server_get, "6")
                if sc.get("code") in _SUCCESS_CODES and isinstance(sc.get("data"), dict):
                    data["_scheduled_charging"] = sc["data"]
            except GeelyTLSPinError as e:
                # A pin failure means the server key changed - an active
                # MITM or a legitimate rotation. Never hide it at DEBUG.
                _LOGGER.error("scheduled-charging fetch: TLS pin check failed: %s", e)
            except Exception as e:  # noqa: BLE001
                _LOGGER.debug("scheduled-charging fetch non-fatal failure: %s", e)

        fail_state["consecutive"] = 0

        # Idle back-off tracking: if parked and nothing meaningful changed, grow
        # the idle streak so the interval stretches out (see _adaptive_interval).
        sig = _poll_signature(data)
        if not charging and not driving and sig == poll_state["sig"]:
            poll_state["idle"] += 1
        else:
            poll_state["idle"] = 0
        poll_state["sig"] = sig

        if not _MANUAL:
            try:
                coordinator.update_interval = _adaptive_interval(data, poll_state["idle"], profile)
            except Exception:  # noqa: BLE001
                pass
        return data

    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name=DOMAIN,
        config_entry=entry,
        update_method=_async_update,
        # None means "never schedule an update yourself" - manual mode.
        update_interval=None if _MANUAL else timedelta(seconds=profile["base"]),
    )
    await coordinator.async_config_entry_first_refresh()

    # Fetch the per-vehicle capability catalog once at setup. Used by
    # platform setup files to decide which entities to expose. Best-effort:
    # on error we log and proceed with default (all-features-enabled) view.
    capabilities: dict = {}
    try:
        from . import capabilities as cap_parser
        raw = await hass.async_add_executor_job(api.fetch_capabilities)
        capabilities = cap_parser.parse(raw or [])
        _LOGGER.info(
            "Capability catalog parsed: %d raw entries, %d derived flags",
            capabilities.get("raw_count", 0), len(capabilities) - 1,
        )
    except Exception as e:  # noqa: BLE001
        _LOGGER.warning("Capability fetch failed (non-fatal): %s", e)

    # What the car is powered by, decided once from the first refresh. Platforms
    # read this rather than each deciding for itself, so a PHEV cannot end up
    # with fuel sensors but no fuel binary sensor.
    verdict = propulsion.classify(d.get(CONF_VEHICLE_POWER_TYPE), coordinator.data)
    _LOGGER.info("Propulsion: %s (%s, tank=%s plug=%s, powerType=%r)",
                 verdict.kind, verdict.source, verdict.has_tank,
                 verdict.has_plug, verdict.declared_raw)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "api":           api,
        "coordinator":   coordinator,
        "vin":           d[CONF_VIN],
        "device_name":   _resolve_device_name(d),
        "capabilities":  capabilities,
        "propulsion":    verdict,
    }
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Reload when the options flow changes the polling mode / pressure unit /
    # language, so the new choice takes effect without a restart.
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    _register_debug_service(hass)
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    # Turning full exposure off has to clear the entities it created. The
    # sensor platform simply stops adding them, and nothing else removes them,
    # so without this they linger in the registry as ~180 unavailable rows.
    # The migration cannot do it: fresh entries are created at the current
    # VERSION and never run one.
    if not (entry.options.get(CONF_FULL_EXPOSURE)
            or entry.data.get(CONF_FULL_EXPOSURE, False)):
        _purge_raw_exposure_entities(hass, entry)
    await hass.config_entries.async_reload(entry.entry_id)


def _register_debug_service(hass: HomeAssistant) -> None:
    """Register `geely_connect.fire_control` once. Idempotent.

    Lets you fire any serviceId+params from Developer Tools → Services
    while iterating on un-mapped controls. Logs the response at WARNING
    level so you can see what the server says without redeploying.

    Example service-data YAML:
        service_id: RCT
        command: start
        params:
          - {key: temperature, value: "22.5"}
    """
    if hass.services.has_service(DOMAIN, "fire_control"):
        return

    schema = vol.Schema({
        vol.Required("service_id"): cv.string,
        vol.Optional("command", default="start"): cv.string,
        vol.Optional("params", default=list): vol.All(
            cv.ensure_list, [vol.Schema({
                vol.Required("key"): cv.string,
                vol.Required("value"): cv.string,
            })],
        ),
        vol.Optional("vin"): cv.string,
    })

    async def _handle(call: ServiceCall) -> None:
        sid = call.data["service_id"]
        cmd = call.data.get("command", "start")
        params = call.data.get("params") or []
        target_vin = call.data.get("vin")

        # Find the matching API. Omitting `vin` is only unambiguous with a
        # single vehicle configured - hass.data order is entry-setup order, so
        # picking the first would send a lock or window command to whichever
        # car happened to load first, and that can change across restarts.
        loaded = list((hass.data.get(DOMAIN) or {}).items())
        if not loaded:
            raise ServiceValidationError("No Geely Connect vehicle is loaded")
        if target_vin:
            chosen = next((kv for kv in loaded if kv[1].get("vin") == target_vin), None)
            if chosen is None:
                raise ServiceValidationError(
                    f"No configured Geely vehicle with VIN {target_vin}"
                )
        elif len(loaded) > 1:
            raise ServiceValidationError(
                "vin is required when more than one vehicle is configured"
            )
        else:
            chosen = loaded[0]
        entry_id, bundle = chosen
        api = bundle["api"]

        # Surface failures the way every entity does. Swallowing them here made
        # a rejected command look successful, and hid an expired session from
        # the reauth flow.
        try:
            resp = await hass.async_add_executor_job(api.control, sid, params, cmd)
        except GeelyControlError as e:
            raise HomeAssistantError(e.message) from e
        except GeelyAuthError as e:
            entry = hass.config_entries.async_get_entry(entry_id)
            if entry:
                entry.async_start_reauth(hass)
            raise HomeAssistantError(f"Geely session expired: {e}") from e
        except Exception as e:
            raise HomeAssistantError(f"fire_control {sid} failed: {e}") from e
        _LOGGER.debug(
            "fire_control %s %s params=%s → response=%s",
            sid, cmd, redact(params), redact(resp),
        )

    # Admin-only. The entities are the supported surface and stay available to
    # every Home Assistant user; this one forwards an arbitrary serviceId and
    # parameter list straight to the car, including commands no entity exposes,
    # so it is a raw escape hatch rather than a feature and is gated to
    # administrators the way Home Assistant gates its other raw services.
    async_register_admin_service(hass, DOMAIN, "fire_control", _handle, schema=schema)
    _LOGGER.info("Registered geely_connect.fire_control debug service (admin only)")


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id, None)
        return True
    return False


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete the vehicle's mTLS material when the integration is removed.

    The private key authenticates Home Assistant to Geely as this car's
    controller - whoever holds it can unlock and pre-condition the vehicle.
    Removing the config entry drops the account token, so without this the key
    and certificate would outlive the integration on disk, unreferenced and
    unnoticed, including inside every backup taken afterwards.
    """
    cert_path = entry.data.get(CONF_CERT_PATH)
    if not cert_path:
        return
    vin_dir = os.path.dirname(cert_path)
    # Only ever inside our own storage directory, never a path the server chose.
    expected_root = os.path.join(hass.config.path(".storage"), DOMAIN)
    if os.path.commonpath([os.path.abspath(vin_dir),
                           os.path.abspath(expected_root)]) != os.path.abspath(expected_root):
        _LOGGER.warning("refusing to remove %s: outside the integration's storage",
                        geely_api.mask_path(vin_dir))
        return
    try:
        await hass.async_add_executor_job(
            functools.partial(shutil.rmtree, vin_dir, ignore_errors=True)
        )
    except Exception as e:  # noqa: BLE001 - removal is best-effort
        _LOGGER.warning("could not remove %s (%s); delete it by hand to be sure "
                        "the vehicle key is gone", geely_api.mask_path(vin_dir), e)
    else:
        _LOGGER.info("Removed stored certificate and key for %s",
                     geely_api.mask_path(vin_dir))


# Unique-id fragments of the entities that ship disabled from v3 onwards.
# entity_registry_enabled_default only applies the first time an entity is
# registered, so an install that predates that change keeps them switched on;
# the v3 migration below turns them off once.
_INTEGRATION_DISABLED = er.RegistryEntryDisabler.INTEGRATION


def _reenable_integration_disabled_entities(hass: HomeAssistant, entry: ConfigEntry) -> int:
    """Turn back on everything earlier versions switched off.

    Only entities the integration itself disabled are touched, so anything the
    user turned off by hand stays off."""
    registry = er.async_get(hass)
    restored = 0
    for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if reg_entry.disabled_by is not _INTEGRATION_DISABLED:
            continue
        registry.async_update_entity(reg_entry.entity_id, disabled_by=None)
        restored += 1
    if restored:
        _LOGGER.info("Re-enabled %d entities that earlier versions had switched off", restored)
    return restored


def _purge_raw_exposure_entities(hass: HomeAssistant, entry: ConfigEntry) -> int:
    """Remove the auto-generated full-exposure sensors.

    They are recreated on demand when full exposure is switched back on under
    Configure, so nothing is lost by clearing them: on an EX5 there are around
    180, and even disabled they bury the entities worth looking at."""
    registry = er.async_get(hass)
    stale = [
        e.entity_id
        for e in er.async_entries_for_config_entry(registry, entry.entry_id)
        if "_raw_" in e.unique_id
    ]
    for entity_id in stale:
        registry.async_remove(entity_id)
    if stale:
        _LOGGER.info(
            "Removed %d full-exposure diagnostic sensors; re-enable them under "
            "Configure if you need to inspect a raw field", len(stale),
        )
    return len(stale)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate older entries forward.

    v1 -> v2: generate per-install idfa/idfv so future logins don't kick the
    iPhone off. v2 -> v3: switch off the window, sunroof, sunshade and
    window-ventilation entities, which now ship disabled because opening them
    by accident from a dashboard leaves the car exposed. v3 -> v4: clear the
    ~180 auto-generated full-exposure sensors, now opt-in under Configure.
    v4 -> v6: turn every entity back on - the useful set is now everything the
    car reports plus the computed extras, with the duplicated aggregates
    removed rather than hidden. Only entities the integration disabled are
    restored, so anything switched off by hand stays off. Everything else keeps
    working; missing fields fall back to safe defaults."""
    if entry.version >= 6:
        return True

    new_data = dict(entry.data)
    if entry.version < 2:
        from .api import make_install_fingerprint
        if not new_data.get("device_idfa"):
            idfa, idfv = make_install_fingerprint()
            new_data["device_idfa"] = idfa
            new_data["device_idfv"] = idfv

    _reenable_integration_disabled_entities(hass, entry)
    _purge_raw_exposure_entities(hass, entry)
    hass.config_entries.async_update_entry(entry, data=new_data, version=6)
    _LOGGER.info("Migrated geely_connect entry %s to v6", entry.entry_id)
    return True
