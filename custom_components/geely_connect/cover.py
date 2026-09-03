"""Cover entities: sunshade (sun-curtain) and all windows.

Service `RWS_2` with `target=sunshade|window` - same on Geely / Zeekr.

Open and close only, and that is the whole of what the API offers rather than
a gap in this file. The capability catalogue looks like it promises more: an
EX5 declares `remote_control_skylight_2`, `remote_control_curtain_2` and
`remote_control_window_2` with `valueRange: "0|100"` and a step of 4, which
reads exactly like positional control. An owner probed it on a real car (#35)
with the position passed four ways alongside `target` - `position`, `open`,
`rws.position` and the catalogue's own `skylight_open` - at several values.
Every one of them opened the roof or the blind FULLY: the position is ignored
and the command falls back to plain open.

So `valueRange` in that catalogue is declaration-side only, and not evidence
that a command accepts a value. Worth remembering before reading a range in
any other entry as a feature - which is the same lesson the parameter names
taught in #4, where the catalogue's own spellings were not the request's.
"""
# -----------------------------------------------------------------------------
# Portions of this file - the reverse-engineered Geely protocol / field mappings
# (the parts that required protocol research) - are derived from
# nitaybz/geely-global-ha, used under the MIT License. See NOTICE.txt.
# Original framework, security hardening and transport are our own work.
# -----------------------------------------------------------------------------
from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.components.cover import (
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import GeelyControlError, redact
from .const import DOMAIN, SERVICE_WINDOW
from .helpers import walk as _walk, windows_open, windows_position, schedule_refresh

_LOGGER = logging.getLogger(__name__)

_CLIMATE_PATH = ("vehicleStatus", "additionalVehicleStatus", "climateStatus")




async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add_entities: AddEntitiesCallback) -> None:
    bundle = hass.data[DOMAIN][entry.entry_id]
    add_entities([
        GeelySunshade(hass, bundle),
        GeelySunroof(hass, bundle),
        GeelyWindows(hass, bundle),
    ])


class _BaseGeelyCover(CoordinatorEntity, CoverEntity):
    _attr_has_entity_name = True
    _attr_supported_features = CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE
    _target: str = ""   # "sunshade" or "window"
    _device_class: CoverDeviceClass | None = None
    _key: str = ""
    _entity_name: str = ""

    def __init__(self, hass: HomeAssistant, bundle: dict) -> None:
        super().__init__(bundle["coordinator"])
        self._hass = hass
        self._api = bundle["api"]
        self._vin = bundle["vin"]
        self._attr_unique_id = f"geely_{self._vin}_cover_{self._key}"
        self._attr_name = self._entity_name
        if self._device_class:
            self._attr_device_class = self._device_class
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._vin)},
            manufacturer="Geely",
            name=bundle.get("device_name") or f"Geely ({self._vin})",
        )

    async def async_open_cover(self, **_: Any) -> None:
        await self._fire("start")

    async def async_close_cover(self, **_: Any) -> None:
        await self._fire("stop")

    async def _fire(self, command: str) -> None:
        params = [{"key": "target", "value": self._target}]
        try:
            resp = await self._hass.async_add_executor_job(
                self._api.control, SERVICE_WINDOW, params, command,
            )
        except GeelyControlError as e:
            raise HomeAssistantError(f"Geely cover ({self._target}): {e.message}") from e
        except Exception as e:
            _LOGGER.exception("cover %s %s failed", self._target, command)
            raise HomeAssistantError(f"Geely cover failure: {e}") from e
        _LOGGER.debug("Geely cover %s %s response=%s", self._target, command, redact(resp))

        schedule_refresh(self._hass, self.coordinator, 8)


class GeelySunshade(_BaseGeelyCover):
    _target = "sunshade"
    _device_class = CoverDeviceClass.SHADE
    _key = "sunshade"
    _entity_name = "Sunshade"

    @property
    def is_closed(self) -> bool | None:
        climate = _walk(self.coordinator.data or {}, _CLIMATE_PATH) or {}
        # curtainOpenStatus: "1" = closed, "2" = open
        v = climate.get("curtainOpenStatus")
        if v is None:
            return None
        return str(v) == "1"


class GeelySunroof(_BaseGeelyCover):
    _target = "sunroof"
    _device_class = CoverDeviceClass.WINDOW
    _key = "sunroof"
    _entity_name = "Sunroof"

    @property
    def is_closed(self) -> bool | None:
        climate = _walk(self.coordinator.data or {}, _CLIMATE_PATH) or {}
        # sunroofOpenStatus: "1" = closed, "2" = open (mirrors curtain
        # convention)
        v = climate.get("sunroofOpenStatus")
        if v is None:
            return None
        return str(v) == "1"


class GeelyWindows(_BaseGeelyCover):
    _target = "window"
    _device_class = CoverDeviceClass.WINDOW
    _key = "windows"
    _entity_name = "All Windows"

    @property
    def is_closed(self) -> bool | None:
        # Exact complement of the ventilation switch, which reads the same
        # four fields - see helpers.windows_open for why a car that reports
        # none of them is unknown rather than open.
        opened = windows_open(self.coordinator.data)
        return None if opened is None else not opened

    @property
    def current_cover_position(self) -> int | None:
        # The car reports a per-corner winPos (0-100); expose the most-open
        # corner so the % shows how far the windows are down - a vent crack
        # reads ~8, not just "open". None when the car omits winPos entirely.
        return windows_position(self.coordinator.data)
