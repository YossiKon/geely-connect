"""Seat heat / ventilation selects.

AVD-verified 2026-05-01:
  Seat heat ON  → RCE_2 / start / [{rce.level: "1|2|3"}, {rce.heat: "<seat>"}]
  Seat heat OFF → RCE_2 / stop  / [{rce.level: "0"},     {rce.heat: "<seat>"}]
  Seat vent ON  → RCE_2 / start / [{rce.level: "1|2|3"}, {rce.ventilation: "<seat>"}]
  Seat vent OFF → RCE_2 / stop  / [{rce.level: "0"},     {rce.ventilation: "<seat>"}]
  duration: 90s for ON, 90s for OFF (per AVD app)

Seat names (this API): "front-left" (driver), "front-right" (passenger),
"rear-left", "rear-right". Capability advertises which positions actually
exist on this trim - we only create entities for those.

State source (under additionalVehicleStatus.climateStatus):
  drvHeatSts / drvHeatDetail              - driver seat heat
  passHeatingSts / passHeatingDetail      - passenger seat heat
  drvVentSts / drvVentDetail              - driver seat vent
  passVentSts / passVentDetail            - passenger seat vent

State decoding (AVD-verified 2026-05-01):
  *Sts:     "0" → off, "1"/"2"/"3" → on at that level
  *Detail:  meaning not fully understood - stays "1" while feature is
            "armed" / has been used recently, "2" before first use.
            We ignore it - *Sts alone is enough.

Empirical proof: across the AVD session, drvHeatSts went
  '0' (initial) → '3' (after level=3 fire) → '1' (after level=1 fire) → '0' (after stop fire)
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

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import GeelyControlError, redact

from .const import (
    CLIMATE_SEAT_LEVELS,
    DOMAIN,
    RCE_DURATION_SECONDS,
    RCE_KEY_HEAT,
    RCE_KEY_LEVEL,
    RCE_KEY_VENT,
    SEAT_FRONT_LEFT,
    SEAT_FRONT_RIGHT,
    SEAT_REAR_LEFT,
    SEAT_REAR_RIGHT,
    SERVICE_CLIMATE,
)
from .helpers import walk as _walk, schedule_refresh

_LOGGER = logging.getLogger(__name__)

_CLIMATE_PATH = ("vehicleStatus", "additionalVehicleStatus", "climateStatus")

# (key, name, icon, mode "heat|vent", seat_name, sts_key, detail_key)
# Naming format: "Seat Heat - <Position>" / "Seat Vent - <Position>" so
# heat-driver / heat-passenger / vent-driver / vent-passenger appear next
# to each other in HA's alphabetically-sorted device page.
SEAT_DEFS: list[tuple] = [
    ("seat_heat_driver",     "Seat Heat - Driver",     "mdi:car-seat-heater", "heat", SEAT_FRONT_LEFT,  "drvHeatSts",     "drvHeatDetail"),
    ("seat_heat_passenger",  "Seat Heat - Passenger",  "mdi:car-seat-heater", "heat", SEAT_FRONT_RIGHT, "passHeatingSts", "passHeatingDetail"),
    ("seat_heat_rear_left",  "Seat Heat - Rear-Left",  "mdi:car-seat-heater", "heat", SEAT_REAR_LEFT,   "rlHeatingSts",   "rlHeatingDetail"),
    ("seat_heat_rear_right", "Seat Heat - Rear-Right", "mdi:car-seat-heater", "heat", SEAT_REAR_RIGHT,  "rrHeatingSts",   "rrHeatingDetail"),
    ("seat_vent_driver",     "Seat Vent - Driver",     "mdi:car-seat-cooler", "vent", SEAT_FRONT_LEFT,  "drvVentSts",     "drvVentDetail"),
    ("seat_vent_passenger",  "Seat Vent - Passenger",  "mdi:car-seat-cooler", "vent", SEAT_FRONT_RIGHT, "passVentSts",    "passVentDetail"),
    ("seat_vent_rear_left",  "Seat Vent - Rear-Left",  "mdi:car-seat-cooler", "vent", SEAT_REAR_LEFT,   "rlVentSts",      "rlVentDetail"),
    ("seat_vent_rear_right", "Seat Vent - Rear-Right", "mdi:car-seat-cooler", "vent", SEAT_REAR_RIGHT,  "rrVentSts",      "rrVentDetail"),
]




async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add_entities: AddEntitiesCallback) -> None:
    bundle = hass.data[DOMAIN][entry.entry_id]
    caps = bundle.get("capabilities") or {}

    # Capability tells us which seat positions exist + whether heat/vent
    # are supported separately. Default permissively if no capability data.
    heat_seats: set[str] = set(caps.get("seat.heat.positions") or
                                [SEAT_FRONT_LEFT, SEAT_FRONT_RIGHT])
    vent_seats: set[str] = set(caps.get("seat.vent.positions") or
                                [SEAT_FRONT_LEFT, SEAT_FRONT_RIGHT])
    heat_supported = caps.get("seat.heat.enabled", True)
    vent_supported = caps.get("seat.vent.enabled", True)

    entities: list[SelectEntity] = []
    for key, name, icon, mode, seat_name, sts_key, detail_key in SEAT_DEFS:
        if mode == "heat":
            if not heat_supported or seat_name not in heat_seats:
                continue
        else:
            if not vent_supported or seat_name not in vent_seats:
                continue
        entities.append(GeelySeatLevel(
            hass, bundle, key, name, icon, mode, seat_name, sts_key, detail_key,
        ))
    add_entities(entities)


class GeelySeatLevel(CoordinatorEntity, SelectEntity):
    _attr_has_entity_name = True
    _attr_options = CLIMATE_SEAT_LEVELS

    def __init__(self, hass: HomeAssistant, bundle: dict, key: str, name: str,
                 icon: str, mode: str, seat_name: str,
                 sts_key: str, detail_key: str) -> None:
        super().__init__(bundle["coordinator"])
        self._hass = hass
        self._api = bundle["api"]
        self._vin = bundle["vin"]
        self._mode = mode  # "heat" or "vent"
        self._seat_name = seat_name
        self._sts_key = sts_key
        self._detail_key = detail_key
        self._attr_unique_id = f"geely_{self._vin}_sel_{key}"
        self._attr_name = name
        self._attr_icon = icon
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._vin)},
            manufacturer="Geely",
            name=bundle.get("device_name") or f"Geely ({self._vin})",
        )
        # Optimistic state - shown briefly after a fire so the UI doesn't
        # flicker through stale server status during the ~10–30s the car
        # takes to actually update.
        self._optimistic_option: str | None = None
        self._optimistic_until: float = 0.0

    @property
    def current_option(self) -> str | None:
        # Optimistic override during the post-fire window - avoids UI
        # flicker through stale server status. Cleared once the timeout
        # passes OR the server status matches the fired option.
        if (self._optimistic_option is not None
                and time.time() < self._optimistic_until):
            srv = self._read_server_option()
            if srv == self._optimistic_option:
                # Server caught up - drop the override.
                self._optimistic_option = None
                self._optimistic_until = 0.0
                return srv
            return self._optimistic_option
        return self._read_server_option()

    def _read_server_option(self) -> str:
        # AVD-verified state encoding (different per mode!):
        #   HEAT:  *HeatSts = "0" → off, "1"/"2"/"3" → on at that level.
        #          *HeatDetail is sub-status (ignored).
        #   VENT:  *VentSts = "1" → on, "2" → off.
        #          *VentDetail = "0" off, "1"/"2"/"3" → level when on.
        # Empirical baseline: heat off=Sts=0,Detail=2 | vent off=Sts=2,Detail=0.
        climate = _walk(self.coordinator.data or {}, _CLIMATE_PATH) or {}
        sts = climate.get(self._sts_key)
        if sts is None:
            return CLIMATE_SEAT_LEVELS[0]
        if self._mode == "heat":
            try:
                level = int(sts)
            except (TypeError, ValueError):
                return CLIMATE_SEAT_LEVELS[0]
        else:
            # vent
            if str(sts) != "1":
                return CLIMATE_SEAT_LEVELS[0]
            detail = climate.get(self._detail_key)
            try:
                level = int(detail) if detail is not None else 0
            except (TypeError, ValueError):
                return CLIMATE_SEAT_LEVELS[0]
        if level < 0 or level >= len(CLIMATE_SEAT_LEVELS):
            return CLIMATE_SEAT_LEVELS[0]
        return CLIMATE_SEAT_LEVELS[level]

    async def async_select_option(self, option: str) -> None:
        try:
            level = CLIMATE_SEAT_LEVELS.index(option)
        except ValueError:
            _LOGGER.warning("Invalid seat level: %s", option)
            return
        seat_param_key = RCE_KEY_HEAT if self._mode == "heat" else RCE_KEY_VENT
        params = [
            {"key": RCE_KEY_LEVEL, "value": str(level)},
            {"key": seat_param_key, "value": self._seat_name},
        ]
        command = "stop" if level == 0 else "start"
        # Fire FIRST - only apply optimistic state if the server accepts.
        # This avoids the UI lying when a command is rate-limited (8070)
        # or otherwise rejected.
        try:
            resp = await self._hass.async_add_executor_job(
                self._api.control, SERVICE_CLIMATE, params, command,
                RCE_DURATION_SECONDS,
            )
        except GeelyControlError as e:
            raise HomeAssistantError(
                f"Geely seat {self._mode} ({self._seat_name}): {e.message}"
            ) from e
        except Exception as e:
            _LOGGER.exception("seat %s seat=%s level=%d failed",
                              self._mode, self._seat_name, level)
            raise HomeAssistantError(
                f"Geely seat {self._mode} failure: {e}"
            ) from e
        _LOGGER.debug(
            "Geely seat %s seat=%s level=%d response=%s",
            self._mode, self._seat_name, level, redact(resp),
        )
        # Server accepted - set optimistic so the UI doesn't flicker
        # while the next status poll catches up to the real state.
        self._optimistic_option = option
        self._optimistic_until = time.time() + 30
        self.async_write_ha_state()

        # Delayed refresh + a follow-up at 25s in case the first poll
        # caught the still-stale server state.
        schedule_refresh(self._hass, self.coordinator, 8, 17)
