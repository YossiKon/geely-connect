"""Geely climate entity.

AVD-verified 2026-05-01:
  AC ON     → RCE_2 / start / [{rce.conditioner:"1"}, {rce.temp:"<C>"}], duration:180
  AC OFF    → RCE_2 / stop  / [{rce.conditioner:"1"}, {rce.temp:"<last>"}], duration:0
  Temp set  → same as AC ON (single command both turns on AND sets temp)
  Defrost   → see preset handling below

Status fields (under vehicleStatus.additionalVehicleStatus.climateStatus):
  preClimateActive  → True iff AC pre-cond cycle running (the AC indicator)
  defrost           → "true" iff defrost running
  airBlowerActive   → True iff blower fan running. Specifically G-clean on
                      this trim - does NOT flip for AC or defrost. Don't
                      use for AC mode.
  interiorTemp      → current cabin temp
  exteriorTemp      → outside temp

Target temp: NOT readable from any server endpoint. The Geely cloud's
capability catalog explicitly lists `temperature_2` (which is the only
remote_status temp query) as returning `temperature_in_car` only.
We track the user-set value locally and surface it as `target_temperature`.
"""
# -----------------------------------------------------------------------------
# Portions of this file — the reverse-engineered Geely protocol / field mappings
# (the parts that required protocol research) — are derived from
# nitaybz/geely-global-ha, used under the MIT License. See NOTICE.txt.
# Original framework, security hardening and transport are our own work.
# -----------------------------------------------------------------------------
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from homeassistant.components.climate import (
    ATTR_TEMPERATURE,
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import GeelyControlError

from .const import (
    CLIMATE_MAX_TEMP_C,
    CLIMATE_MIN_TEMP_C,
    CLIMATE_TEMP_STEP_C,
    DOMAIN,
    PRESET_NONE,
    PRESET_RAPID_COOLING,
    PRESET_RAPID_WARMING,
    RCE_AC_DURATION_SEC,
    RCE_KEY_CONDITIONER,
    RCE_KEY_TEMP,
    RCE_VAL_AC,
    RCE_VAL_DEFROST,
    SERVICE_CLIMATE,
)
from .helpers import walk as _walk, truthy as _truthy, schedule_refresh

_LOGGER = logging.getLogger(__name__)

_BASIC_PATH         = ("vehicleStatus", "basicVehicleStatus")
_CLIMATE_PATH       = ("vehicleStatus", "additionalVehicleStatus", "climateStatus")
_INTERIOR_TEMP_PATH = (*_CLIMATE_PATH, "interiorTemp")
_EXTERIOR_TEMP_PATH = (*_CLIMATE_PATH, "exteriorTemp")
_PRECLIMATE_PATH    = (*_CLIMATE_PATH, "preClimateActive")
_DEFROST_PATH       = (*_CLIMATE_PATH, "defrost")






def _to_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add_entities: AddEntitiesCallback) -> None:
    bundle = hass.data[DOMAIN][entry.entry_id]
    caps = bundle.get("capabilities") or {}
    if not caps.get("ac.enabled", True):
        _LOGGER.info("Capability says AC not supported on this trim - skipping climate entity")
        return
    add_entities([GeelyClimate(hass, bundle)])


class GeelyClimate(CoordinatorEntity, ClimateEntity, RestoreEntity):
    _attr_has_entity_name = True
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT_COOL]

    def __init__(self, hass: HomeAssistant, bundle: dict) -> None:
        super().__init__(bundle["coordinator"])
        self._hass = hass
        self._api = bundle["api"]
        self._vin = bundle["vin"]
        self._caps = bundle.get("capabilities") or {}

        # Capability-driven bounds (fall back to const defaults).
        self._attr_min_temp = float(self._caps.get("ac.min", CLIMATE_MIN_TEMP_C))
        self._attr_max_temp = float(self._caps.get("ac.max", CLIMATE_MAX_TEMP_C))
        self._attr_target_temperature_step = float(self._caps.get("ac.step", CLIMATE_TEMP_STEP_C))

        # Preset support - rapid warming / rapid cooling only (stateless
        # one-shots that bundle AC + seat heat or vent). Defrost has its
        # own switch (real server status), not a preset.
        self._attr_preset_modes = [PRESET_NONE, PRESET_RAPID_WARMING, PRESET_RAPID_COOLING]
        # Optimistic preset: shown briefly after the user picks rapid_*,
        # then auto-clears back to "none" since there's no server flag.
        self._optimistic_preset: str | None = None
        self._optimistic_preset_until: float = 0.0
        # Optimistic hvac_action - set when the user fires temp/AC so the
        # UI doesn't flicker through IDLE while the cabin temp catches up.
        self._optimistic_action: HVACAction | None = None
        self._optimistic_action_until: float = 0.0

        features = (
            ClimateEntityFeature.TARGET_TEMPERATURE
            | ClimateEntityFeature.TURN_ON
            | ClimateEntityFeature.TURN_OFF
        )
        if self._attr_preset_modes:
            features |= ClimateEntityFeature.PRESET_MODE
        self._attr_supported_features = features

        self._attr_unique_id = f"geely_{self._vin}_climate"
        self._attr_name = "Climate"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._vin)},
            manufacturer="Geely",
            name=bundle.get("device_name") or f"Geely ({self._vin})",
        )

        # Local cache: target temp is not server-readable, so we remember
        # the last value the user set and surface it as `target_temperature`.
        self._cached_target_temp: float = max(
            self._attr_min_temp, min(self._attr_max_temp, 22.0)
        )
        self._optimistic_hvac_mode: HVACMode | None = None
        self._optimistic_until: float = 0.0

    async def async_added_to_hass(self) -> None:
        """Restore last target temp from HA's recorder on startup.

        Target temp is local-cache only (the Geely cloud doesn't store it).
        Without RestoreEntity, HA forgets the user's setpoint across
        restarts and shows the default 22°C.
        """
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is None:
            return
        last_temp = last_state.attributes.get("temperature")
        if last_temp is None:
            return
        try:
            t = float(last_temp)
        except (TypeError, ValueError):
            return
        clamped = max(self._attr_min_temp, min(self._attr_max_temp, t))
        self._cached_target_temp = clamped
        _LOGGER.debug("Restored target temp from last state: %.1f", clamped)

    # ---- read state ----

    @property
    def current_temperature(self) -> float | None:
        return _to_float(_walk(self.coordinator.data or {}, _INTERIOR_TEMP_PATH))

    @property
    def target_temperature(self) -> float | None:
        return self._cached_target_temp

    @property
    def hvac_mode(self) -> HVACMode:
        if self._optimistic_hvac_mode is not None and time.time() < self._optimistic_until:
            return self._optimistic_hvac_mode
        if _truthy(_walk(self.coordinator.data or {}, _PRECLIMATE_PATH)):
            return HVACMode.HEAT_COOL
        if _truthy(_walk(self.coordinator.data or {}, _DEFROST_PATH)):
            return HVACMode.HEAT_COOL
        return HVACMode.OFF

    @property
    def hvac_action(self) -> HVACAction | None:
        # Off → OFF. Defrost takes precedence over heat/cool inference.
        if self.hvac_mode == HVACMode.OFF:
            return HVACAction.OFF
        if _truthy(_walk(self.coordinator.data or {}, _DEFROST_PATH)):
            return HVACAction.DEFROSTING
        # Optimistic action while a recent user action is settling - avoids
        # the dropdown flickering between Cooling and Idle as the cabin
        # temp drifts across the dead-band.
        if (self._optimistic_action is not None
                and time.time() < self._optimistic_action_until):
            return self._optimistic_action
        # Compare cached target vs current interior temp. ±1.0°C dead-band
        # → IDLE. Wider than the typical 0.5° AC step so small temp drift
        # doesn't constantly flip the action.
        target = self._cached_target_temp
        current = _to_float(_walk(self.coordinator.data or {}, _INTERIOR_TEMP_PATH))
        if target is not None and current is not None:
            if target > current + 1.0:
                return HVACAction.HEATING
            if target < current - 1.0:
                return HVACAction.COOLING
        return HVACAction.IDLE

    @property
    def preset_mode(self) -> str | None:
        # Rapid warming / cooling are stateless - show optimistically for
        # ~30s after firing, then revert to "none". Defrost is NOT a
        # preset (it has a real server status, exposed as a switch).
        if (self._optimistic_preset is not None
                and time.time() < self._optimistic_preset_until):
            return self._optimistic_preset
        return PRESET_NONE

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "exterior_temperature": _to_float(
                _walk(self.coordinator.data or {}, _EXTERIOR_TEMP_PATH)
            ),
        }

    # ---- writes ----

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        # Fire FIRST, then apply optimistic state - so a server-rejected
        # command doesn't leave the UI showing a state the car never reached.
        if hvac_mode == HVACMode.HEAT_COOL:
            await self._fire(
                "start",
                [
                    {"key": RCE_KEY_CONDITIONER, "value": RCE_VAL_AC},
                    {"key": RCE_KEY_TEMP, "value": _fmt_temp(self._cached_target_temp)},
                ],
                duration=RCE_AC_DURATION_SEC,
            )
            self._set_optimistic(HVACMode.HEAT_COOL)
        elif hvac_mode == HVACMode.OFF:
            await self._fire(
                "stop",
                [
                    {"key": RCE_KEY_CONDITIONER, "value": RCE_VAL_AC},
                    {"key": RCE_KEY_TEMP, "value": _fmt_temp(self._cached_target_temp)},
                ],
                duration=0,
            )
            # hvac_mode reports HEAT_COOL for defrost as well as pre-climate,
            # so stopping only the AC leaves a defrosting car showing Off for
            # the length of the optimistic window and then flipping back on its
            # own. Stop defrost too, but only when the car says it is running -
            # there is no reason to send a second remote command otherwise.
            if _truthy(_walk(self.coordinator.data or {}, _DEFROST_PATH)):
                await self._fire(
                    "stop",
                    [{"key": RCE_KEY_CONDITIONER, "value": RCE_VAL_DEFROST}],
                    duration=0,
                )
            self._set_optimistic(HVACMode.OFF)
        self.async_write_ha_state()

    async def async_turn_on(self) -> None:
        await self.async_set_hvac_mode(HVACMode.HEAT_COOL)

    async def async_turn_off(self) -> None:
        await self.async_set_hvac_mode(HVACMode.OFF)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is None:
            return
        clamped = max(self._attr_min_temp, min(self._attr_max_temp, float(temp)))
        # Fire FIRST - only update cached target + optimistic action if
        # the server accepts. Avoids the UI showing a temp the car never
        # received.
        await self._fire(
            "start",
            [
                {"key": RCE_KEY_CONDITIONER, "value": RCE_VAL_AC},
                {"key": RCE_KEY_TEMP, "value": _fmt_temp(clamped)},
            ],
            duration=RCE_AC_DURATION_SEC,
        )
        # Optimistic action so the UI doesn't flicker through IDLE while
        # the dead-band recomputes against the not-yet-changed cabin temp.
        current = _to_float(_walk(self.coordinator.data or {}, _INTERIOR_TEMP_PATH))
        if current is not None:
            if clamped > current + 1.0:
                self._optimistic_action = HVACAction.HEATING
            elif clamped < current - 1.0:
                self._optimistic_action = HVACAction.COOLING
            else:
                self._optimistic_action = HVACAction.IDLE
            self._optimistic_action_until = time.time() + 60
        self._cached_target_temp = clamped
        self.async_write_ha_state()

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        if preset_mode == PRESET_RAPID_WARMING:
            await self._fire_rapid(warming=True)
        elif preset_mode == PRESET_RAPID_COOLING:
            await self._fire_rapid(warming=False)
        elif preset_mode == PRESET_NONE:
            # User explicitly chose "none" - clear any optimistic preset
            # immediately. There's no server-side action; defrost is its
            # own switch.
            self._optimistic_preset = None
            self._optimistic_preset_until = 0.0
            self.async_write_ha_state()

    async def _fire_rapid(self, *, warming: bool) -> None:
        """Compound rapid warming/cooling - bundles AC + both seat features.

        Fires the charge-server compound POST FIRST. Only on server-accept
        do we update cached temp + optimistic preset/mode/action so the UI
        never shows a state the server didn't accept.
        """
        target = self._attr_max_temp if warming else self._attr_min_temp
        try:
            resp = await self._hass.async_add_executor_job(
                lambda: self._api.rapid_climate(
                    ac=True,
                    temp=_fmt_temp(target),
                    heat_seats=["11", "19"] if warming else None,
                    vent_seats=None if warming else ["11", "19"],
                    vlt=not warming,
                )
            )
        except GeelyControlError as e:
            raise HomeAssistantError(f"Geely rapid {('warming' if warming else 'cooling')}: {e.message}") from e
        except Exception as e:
            _LOGGER.exception("rapid %s failed", "warm" if warming else "cool")
            raise HomeAssistantError(f"Geely rapid failure: {e}") from e
        # Server accepted - apply optimistic state.
        self._cached_target_temp = target
        self._set_optimistic(HVACMode.HEAT_COOL)
        # Optimistic action while cabin temp catches up (won't flicker
        # because the dead-band crossing happens off-screen).
        if warming:
            self._optimistic_action = HVACAction.HEATING
        else:
            self._optimistic_action = HVACAction.COOLING
        self._optimistic_action_until = time.time() + 60
        # Surface the chosen preset briefly, then auto-revert to "none".
        self._optimistic_preset = (
            PRESET_RAPID_WARMING if warming else PRESET_RAPID_COOLING
        )
        self._optimistic_preset_until = time.time() + 30
        _LOGGER.debug("Geely rapid %s response=%s",
                      "warming" if warming else "cooling", resp)

        schedule_refresh(self._hass, self.coordinator, 8)
        self.async_write_ha_state()

    # ---- helpers ----

    def _set_optimistic(self, mode: HVACMode) -> None:
        self._optimistic_hvac_mode = mode
        self._optimistic_until = time.time() + 30

    async def _fire(self, command: str, params: list[dict], duration: int) -> None:
        # Callers set optimistic state AFTER awaiting this method, so on
        # error there's nothing to clean up here - just surface the error.
        try:
            resp = await self._hass.async_add_executor_job(
                self._api.control, SERVICE_CLIMATE, params, command, duration,
            )
        except GeelyControlError as e:
            raise HomeAssistantError(f"Geely climate: {e.message}") from e
        except Exception as e:
            _LOGGER.exception("climate %s failed", command)
            raise HomeAssistantError(f"Geely climate failure: {e}") from e
        _LOGGER.debug(
            "Geely climate %s params=%s duration=%d response=%s",
            command, params, duration, resp,
        )

        schedule_refresh(self._hass, self.coordinator, 8)


def _fmt_temp(t: float) -> str:
    """Format temp as the Geely API expects: '22.0', '15.5', '17.5' etc."""
    return f"{t:.1f}"
