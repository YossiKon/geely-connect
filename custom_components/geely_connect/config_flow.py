"""Config flow for Geely (international).

Multi-step setup:
  1. user enters email + country code → cidpsso captcha + OTP send
  2. user types the 6-digit code → cidpsso login → token
  3. (auto) list_vehicles → if multiple unconfigured vehicles, user picks one
  4. (auto) provision per-device mTLS cert → store paths in ConfigEntry data

Each VIN gets its own ConfigEntry (unique_id = email:vin). The flow can
be re-run to add additional vehicles on the same account; already-
configured VINs are filtered out of the picker.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import entity_registry as er

from . import api as geely_api
from .const import (
    CONF_BATTERY_KWH,
    CONF_EXTERIOR_TEMP_OFFSET,
    CONF_FULL_EXPOSURE,
    CONF_REGION,
    DEFAULT_COUNTRY_CODE,
    DEFAULT_REGION,
    UNSUPPORTED_REGIONS,
    region_config,
    resolve_vehicle_region,
    DEFAULT_LANGUAGE,
    DEFAULT_POLL_MODE,
    DEFAULT_PRESSURE_UNIT,
    LANGUAGES,
    POLL_MODES,
    PRESSURE_UNITS,
    SUPPORTED_COUNTRIES,
    CONF_CERT_PATH,
    CONF_CIDPSSO_TOKEN,
    CONF_COUNTRY_CODE,
    CONF_DEVICE_ID,
    CONF_DEVICE_IDFA,
    CONF_DEVICE_IDFV,
    CONF_EMAIL,
    CONF_KEY_PATH,
    CONF_LANGUAGE,
    CONF_POLL_MODE,
    CONF_PRESSURE_UNIT,
    CONF_USER_ID,
    CONF_VEHICLE_NICKNAME,
    CONF_VIN,
    DOMAIN,
)
from .helpers import vehicle_metadata

_LOGGER = logging.getLogger(__name__)


# SECURITY: the VIN and user_id are taken from the Geely backend's JSON and
# then flow into filesystem paths (cert/key/pin storage) and into the hand-built
# raw HTTP request line. Without validation a malicious or compromised backend
# could return a VIN like "../../config/..." (path traversal / arbitrary file
# write) or one containing CR/LF (HTTP request-line injection). We accept only
# conservative identifier characters and reject anything else.
# \Z, not $: in Python `$` also matches immediately before a trailing newline,
# so "L6T...\n" satisfied `^[A-Za-z0-9]{8,20}$` and passed this gate. The
# transport's CR/LF guard caught it downstream, but this is meant to be the
# first line of defence, not the second.
_VIN_RE = re.compile(r"\A[A-Za-z0-9]{8,20}\Z")
_USER_ID_RE = re.compile(r"\A[A-Za-z0-9._-]{1,64}\Z")

# Unique-id suffixes of the four tire-pressure sensors. Mirrors sensor.py,
# kept here so the options flow does not import a platform module. The unit
# codes ARE Home Assistant's pressure strings, so no translation is needed.
_TIRE_UNIQUE_ID_KEYS = (
    "tire_pressure_fl", "tire_pressure_fr", "tire_pressure_rl", "tire_pressure_rr",
)


def _valid_vin(vin: Any) -> bool:
    return isinstance(vin, str) and bool(_VIN_RE.match(vin))


def _valid_user_id(uid: Any) -> bool:
    return isinstance(uid, str) and bool(_USER_ID_RE.match(uid))


def _storage_paths(hass, vin: str) -> tuple[str, str]:
    if not _valid_vin(vin):
        raise ValueError("refusing to build storage path for invalid VIN")
    base = os.path.join(hass.config.path(".storage"), DOMAIN, vin)
    return os.path.join(base, "cert.pem"), os.path.join(base, "key.pem")


def _already_configured_vins(hass) -> set[str]:
    return {
        e.data.get(CONF_VIN) for e in hass.config_entries.async_entries(DOMAIN)
        if e.data.get(CONF_VIN)
    }


class GeelyIntlConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Geely (international)."""

    VERSION = 6

    def __init__(self) -> None:
        self._email: str | None = None
        self._country_code: str = ""
        self._pressure_unit: str = DEFAULT_PRESSURE_UNIT
        self._poll_mode: str = DEFAULT_POLL_MODE
        self._cidpsso_token: str | None = None
        self._user_id: str | None = None
        self._vehicles: list[dict] = []
        self._idfa: str | None = None
        self._idfv: str | None = None
        # Set when this flow is a re-auth. We update the existing entry's
        # token instead of creating a new one in that case.
        self._reauth_entry: config_entries.ConfigEntry | None = None

    # ---- Step 1: email + send OTP ----

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._email = user_input[CONF_EMAIL].strip()
            self._country_code = user_input[CONF_COUNTRY_CODE].strip().upper()
            self._pressure_unit = user_input.get(CONF_PRESSURE_UNIT, DEFAULT_PRESSURE_UNIT)
            self._poll_mode = user_input.get(CONF_POLL_MODE, DEFAULT_POLL_MODE)
            # Reuse the install's fingerprint on re-auth so the server
            # doesn't see this as a new device on every refresh.
            if self._reauth_entry is not None:
                self._idfa = self._reauth_entry.data.get(CONF_DEVICE_IDFA)
                self._idfv = self._reauth_entry.data.get(CONF_DEVICE_IDFV)
            if not self._idfa or not self._idfv:
                self._idfa, self._idfv = geely_api.make_install_fingerprint()
            try:
                resp = await self.hass.async_add_executor_job(
                    lambda: geely_api.cidpsso_send_otp(
                        self._email, self._country_code,
                        idfa=self._idfa, idfv=self._idfv,
                    )
                )
            except geely_api.GeelyCaptchaUnreachableError:
                # Issue #5: a generic "try again in a minute" on a
                # network-level failure sent users into a useless retry loop.
                _LOGGER.exception("send-otp failed: captcha host unreachable")
                errors["base"] = "captcha_unreachable"
            except Exception:
                _LOGGER.exception("send-otp failed")
                errors["base"] = "send_code_failed"
            else:
                if resp.get("code") and resp.get("code") != 10000000:
                    _LOGGER.warning("OTP send response: %s", geely_api.redact(resp))
                    errors["base"] = "send_code_failed"
                else:
                    return await self.async_step_code()

        # Pre-fill the form. Home Assistant does not carry submitted values
        # across an error re-render, and the captcha solver behind the OTP send
        # is ~85% accurate, so retyping the address on every retry is the
        # normal case rather than the exception.
        defaults: dict[str, Any] = {}
        if self._email:
            defaults[CONF_EMAIL] = self._email
        if self._country_code:
            defaults[CONF_COUNTRY_CODE] = self._country_code
        if self._reauth_entry is not None:
            defaults.setdefault(CONF_EMAIL, self._reauth_entry.data.get(CONF_EMAIL, ""))
            defaults.setdefault(CONF_COUNTRY_CODE, self._reauth_entry.data.get(CONF_COUNTRY_CODE, ""))
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_EMAIL, default=defaults.get(CONF_EMAIL, "")): str,
                vol.Required(
                    CONF_COUNTRY_CODE,
                    default=defaults.get(CONF_COUNTRY_CODE) or DEFAULT_COUNTRY_CODE,
                ): vol.In(SUPPORTED_COUNTRIES),
                vol.Required(CONF_PRESSURE_UNIT, default=DEFAULT_PRESSURE_UNIT): vol.In(PRESSURE_UNITS),
                vol.Required(CONF_POLL_MODE, default=DEFAULT_POLL_MODE): vol.In(POLL_MODES),
            }),
            errors=errors,
        )

    # ---- Step 2: enter OTP, login, fetch vehicles, provision cert ----

    async def async_step_code(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                login_resp = await self.hass.async_add_executor_job(
                    lambda: geely_api.cidpsso_login(
                        self._email, user_input["code"], self._country_code,
                        idfa=self._idfa, idfv=self._idfv,
                    )
                )
            except Exception:
                _LOGGER.exception("login failed")
                errors["code"] = "invalid_code"
            else:
                if login_resp.get("code") != 10000000:
                    _LOGGER.warning("login resp: %s", geely_api.redact(login_resp))
                    errors["code"] = "invalid_code"
                else:
                    data = login_resp.get("data") or {}
                    self._cidpsso_token = data.get("token")
                    self._user_id = data.get("userId") or data.get("id")
                    if not self._cidpsso_token or not self._user_id:
                        errors["base"] = "unknown"
                    elif not _valid_user_id(self._user_id):
                        # SECURITY: user_id is interpolated into request paths;
                        # reject anything outside a strict identifier charset.
                        _LOGGER.error("server returned a malformed user_id; aborting")
                        errors["base"] = "unknown"
                    else:
                        # Fetch vehicles, drop ones already configured. Only keep
                        # entries whose VIN passes strict validation (SECURITY:
                        # the VIN reaches the filesystem and the raw request line).
                        try:
                            all_v_raw = await self.hass.async_add_executor_job(
                                lambda: geely_api.list_vehicles(
                                    self._cidpsso_token, self._user_id,
                                    self._country_code,
                                    idfa=self._idfa, idfv=self._idfv,
                                )
                            )
                            all_v = [v for v in (all_v_raw or []) if _valid_vin(v.get("vin"))]
                            if all_v_raw and not all_v:
                                _LOGGER.error("all vehicles had malformed VINs; aborting")
                        except Exception:
                            _LOGGER.exception("list_vehicles failed")
                            errors["base"] = "no_vehicles"
                        else:
                            # Reauth: update THIS entry's token, don't filter
                            # the entry's own VIN out (otherwise we'd see
                            # "all_configured" and the token never refreshes).
                            if self._reauth_entry is not None:
                                target_vin = self._reauth_entry.data.get(CONF_VIN)
                                matching = next(
                                    (v for v in all_v if v.get("vin") == target_vin),
                                    None,
                                )
                                if matching is None:
                                    errors["base"] = "no_vehicles"
                                else:
                                    return await self._finish_with_vehicle(matching)
                            else:
                                existing = _already_configured_vins(self.hass)
                                self._vehicles = [
                                    v for v in all_v
                                    if v.get("vin") and v.get("vin") not in existing
                                ]
                                if not self._vehicles and not all_v:
                                    errors["base"] = "no_vehicles"
                                elif not self._vehicles:
                                    return self.async_abort(reason="all_configured")
                                elif len(self._vehicles) == 1:
                                    return await self._finish_with_vehicle(self._vehicles[0])
                                else:
                                    return await self.async_step_pick_vehicle()

        return self.async_show_form(
            step_id="code",
            data_schema=vol.Schema({vol.Required("code"): str}),
            errors=errors,
            description_placeholders={"email": self._email or ""},
        )

    # ---- Step 3 (optional): pick vehicle when account has multiple ----

    async def async_step_pick_vehicle(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            chosen = next((v for v in self._vehicles if v.get("vin") == user_input["vin"]), None)
            if chosen is None:
                return self.async_abort(reason="unknown")
            return await self._finish_with_vehicle(chosen)

        options = {
            v["vin"]: f"{v.get('nickname') or v.get('model') or 'Geely'} ({v['vin']})"
            for v in self._vehicles
        }
        return self.async_show_form(
            step_id="pick_vehicle",
            data_schema=vol.Schema({vol.Required("vin"): vol.In(options)}),
        )

    async def _finish_with_vehicle(self, vehicle: dict) -> FlowResult:
        vin = vehicle["vin"]

        # SECURITY: final gate - never build storage paths or a config entry
        # from a VIN / user_id that isn't a strict identifier.
        if not _valid_vin(vin) or not _valid_user_id(self._user_id):
            _LOGGER.error("refusing to finish setup with malformed VIN/user_id")
            return self.async_abort(reason="unknown")

        # On re-auth: update the existing entry's token instead of creating
        # a new entry - preserves entity history, automations, and unique IDs.
        if self._reauth_entry is not None:
            # The entry's unique_id is "email:vin" and is not recomputed here,
            # so re-authenticating as a different account would leave the entry
            # labelled with one address while it operates as another. Requiring
            # the VIN to exist in the new account does not catch this: a second
            # household account normally lists the same car.
            previous_email = self._reauth_entry.data.get(CONF_EMAIL) or ""
            if previous_email and (self._email or "").lower() != previous_email.lower():
                _LOGGER.error(
                    "re-authentication used a different Geely account than the "
                    "one this entry was created with; aborting"
                )
                return self.async_abort(reason="reauth_account_mismatch")

            new_data = dict(self._reauth_entry.data)
            new_data[CONF_CIDPSSO_TOKEN] = self._cidpsso_token
            new_data[CONF_USER_ID] = self._user_id
            new_data[CONF_DEVICE_IDFA] = self._idfa
            new_data[CONF_DEVICE_IDFV] = self._idfv
            self.hass.config_entries.async_update_entry(self._reauth_entry, data=new_data)
            await self.hass.config_entries.async_reload(self._reauth_entry.entry_id)
            return self.async_abort(reason="reauth_successful")

        await self.async_set_unique_id(f"{self._email}:{vin}")
        self._abort_if_unique_id_configured()

        # Which Geely backend this car lives on. It comes from the vehicle
        # record, not from the country the user picked: the two can differ, and
        # signing against the wrong one is what produces the opaque 1501
        # "geelyos verify error".
        # The fields region resolution reads, logged so an unrecognised market
        # can be diagnosed from a normal debug log. Region/market codes only -
        # but the record also carries the VIN and nickname, so it goes through
        # redact() rather than being printed whole.
        _LOGGER.debug(
            "vehicle region fields: %s",
            geely_api.redact({
                k: vehicle.get(k) for k in (
                    "tspInfo", "edgeInfo", "serviceRegion", "saleMarket",
                    "dcCode", "deliveryCountryCode", "tcamMarket",
                )
            }),
        )
        region = resolve_vehicle_region(vehicle) or DEFAULT_REGION
        if region in UNSUPPORTED_REGIONS:
            _LOGGER.error(
                "vehicle %s is registered in the %s region (%s), for which no "
                "app credentials are available", vin[-4:], region,
                UNSUPPORTED_REGIONS[region],
            )
            return self.async_abort(reason="wrong_region")
        backend = region_config(region)
        _LOGGER.debug("provisioning against the %s backend (%s)",
                      region, backend["cert_host"])

        device_id = hashlib.md5(f"ha:{self._user_id}:{vin}".encode()).hexdigest()
        cert_path, key_path = _storage_paths(self.hass, vin)
        try:
            await self.hass.async_add_executor_job(
                lambda: geely_api.provision_user_cert(
                    app_id=backend["app_id"],
                    app_secret=backend["app_secret"],
                    user_id=self._user_id,
                    cidpsso_token=self._cidpsso_token,
                    cert_out_path=cert_path,
                    key_out_path=key_path,
                    cert_host=backend["cert_host"],
                )
            )
        except geely_api.GeelyRegionError as e:
            _LOGGER.error("cert provisioning refused: %s", e)
            return self.async_abort(reason="wrong_region")
        except Exception:
            _LOGGER.exception("cert provisioning failed")
            return self.async_abort(reason="cert_failed")

        metadata = vehicle_metadata(vehicle)
        # The title needs something to show even for a car with neither a
        # nickname nor a model; the stored field stays empty so the device name
        # can fall through to the model code.
        title = f"{metadata[CONF_VEHICLE_NICKNAME] or 'Geely'} ({vin})"
        return self.async_create_entry(
            title=title,
            data={
                CONF_EMAIL:              self._email,
                CONF_COUNTRY_CODE:       self._country_code,
                CONF_REGION:             region,
                CONF_CIDPSSO_TOKEN:      self._cidpsso_token,
                CONF_USER_ID:            self._user_id,
                CONF_VIN:                vin,
                CONF_DEVICE_ID:          device_id,
                CONF_CERT_PATH:          cert_path,
                CONF_KEY_PATH:           key_path,
                CONF_DEVICE_IDFA:        self._idfa,
                CONF_DEVICE_IDFV:        self._idfv,
                **metadata,
                CONF_PRESSURE_UNIT:      self._pressure_unit,
                CONF_POLL_MODE:          self._poll_mode,
            },
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> FlowResult:
        """HA enters this step when the coordinator raises ConfigEntryAuthFailed."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        return await self.async_step_user()

    @staticmethod
    @callback
    def async_get_options_flow(
        entry: config_entries.ConfigEntry,
    ) -> GeelyIntlOptionsFlow:
        return GeelyIntlOptionsFlow()


class GeelyIntlOptionsFlow(config_entries.OptionsFlow):
    """Lets the polling mode, tire-pressure unit and language be changed after
    setup, instead of being frozen at whatever was picked the first time."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        entry = self.config_entry
        current = {**entry.data, **entry.options}

        if user_input is not None:
            # Changing the pressure unit has to reach entities that already
            # exist: Home Assistant only reads suggested_unit_of_measurement
            # when an entity is first registered, so afterwards the display
            # unit lives in the registry and nothing the integration reports
            # will move it.
            new_unit = user_input.get(CONF_PRESSURE_UNIT)
            if new_unit and new_unit != current.get(CONF_PRESSURE_UNIT):
                _apply_pressure_unit(self.hass, entry, new_unit)
            return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required(
                    CONF_POLL_MODE,
                    default=current.get(CONF_POLL_MODE, DEFAULT_POLL_MODE),
                ): vol.In(POLL_MODES),
                vol.Required(
                    CONF_PRESSURE_UNIT,
                    default=current.get(CONF_PRESSURE_UNIT, DEFAULT_PRESSURE_UNIT),
                ): vol.In(PRESSURE_UNITS),
                vol.Required(
                    CONF_FULL_EXPOSURE,
                    default=current.get(CONF_FULL_EXPOSURE, False),
                ): bool,
                # Usable pack size in kWh. 0 leaves Range At Full Charge
                # extrapolating the car's own estimate; a real figure makes it
                # the range at this car's own measured consumption instead. No
                # payload carries it and it cannot be guessed - the EX5 alone
                # ships 49.52, 60.22 and 68.39 kWh packs, and the rated range
                # moves again with the trim's wheels and weight.
                vol.Optional(
                    CONF_BATTERY_KWH,
                    default=float(current.get(CONF_BATTERY_KWH) or 0),
                ): vol.All(vol.Coerce(float), vol.Range(min=0, max=250)),
                # Degrees to add to Exterior Temperature, for an owner who has
                # measured their own car against its cluster. 0 for everyone
                # else, and deliberately not a shipped constant: five
                # synchronised samples read exactly +10, and a sixth, on a car
                # parked for hours, read ten the other way.
                vol.Optional(
                    CONF_EXTERIOR_TEMP_OFFSET,
                    default=float(current.get(CONF_EXTERIOR_TEMP_OFFSET) or 0),
                ): vol.All(vol.Coerce(float), vol.Range(min=-30, max=30)),
            }),
        )


def _apply_pressure_unit(hass, entry: config_entries.ConfigEntry, unit: str) -> None:
    """Re-point this vehicle's tire-pressure entities at a new display unit.

    Same mechanism the per-entity settings dialog uses, so history is kept and
    Home Assistant converts the stored kPa readings itself."""
    registry = er.async_get(hass)
    for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if not any(reg_entry.unique_id.endswith(f"_{k}") for k in _TIRE_UNIQUE_ID_KEYS):
            continue
        registry.async_update_entity_options(
            reg_entry.entity_id, "sensor", {"unit_of_measurement": unit}
        )
