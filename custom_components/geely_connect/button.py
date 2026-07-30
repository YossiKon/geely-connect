"""Geely one-shot action buttons.

AVD-verified 2026-05-01:
  Find car        → RHL / [{rhl: "horn-light-flash"}]
  Unlock Trunk    → RDU_2 / [{target: "trunk"}]

Rapid warming / cooling are exposed as climate.preset_modes - see
climate.py - because they're a "set the climate to a mode" action, not
a true one-shot, so the preset UX is cleaner.
"""
# -----------------------------------------------------------------------------
# Portions of this file — the reverse-engineered Geely protocol / field mappings
# (the parts that required protocol research) — are derived from
# nitaybz/geely-global-ha, used under the MIT License. See NOTICE.txt.
# Original framework, security hardening and transport are our own work.
# -----------------------------------------------------------------------------
from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import GeelyControlError

from .const import (
    DOMAIN,
    SERVICE_FIND_CAR,
    SERVICE_FIND_CAR_PARAMS,
    SERVICE_TAILGATE,
    SERVICE_TAILGATE_PARAMS,
)

_LOGGER = logging.getLogger(__name__)


# Standard telematics buttons (PUT /remote-control/vehicle/telematics/{VIN})
# (key, name, icon, service_id, params, capability_flag_or_None)
SIMPLE_BUTTONS: list[tuple[str, str, str, str, list[dict], str | None]] = [
    ("find_car",     "Find Car",     "mdi:car-search", SERVICE_FIND_CAR,  SERVICE_FIND_CAR_PARAMS, "find_car.enabled"),
    ("unlock_trunk", "Unlock Trunk", "mdi:car-back",   SERVICE_TAILGATE,  SERVICE_TAILGATE_PARAMS, "tailgate.enabled"),
]

# Note: rapid warming / rapid cooling are exposed as climate presets
# (see climate.py), not as separate buttons. They're a "set the climate
# to a mode" action, not a true one-shot, so the preset UX is cleaner.


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add_entities: AddEntitiesCallback) -> None:
    bundle = hass.data[DOMAIN][entry.entry_id]
    caps = bundle.get("capabilities") or {}

    entities: list[ButtonEntity] = []
    for key, name, icon, sid, params, flag in SIMPLE_BUTTONS:
        if caps and flag and not caps.get(flag, True):
            _LOGGER.debug("button %s skipped (capability flag %s=False)", key, flag)
            continue
        entities.append(GeelyTelematicsButton(hass, bundle, key, name, icon, sid, params))

    # Manual "Refresh now" — forces an immediate poll, bypassing the back-off.
    entities.append(GeelyRefreshButton(hass, bundle))

    add_entities(entities)


class GeelyRefreshButton(ButtonEntity):
    """Fetch fresh vehicle data right now, regardless of the polling interval."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:refresh"

    def __init__(self, hass: HomeAssistant, bundle: dict) -> None:
        self._hass = hass
        self._coordinator = bundle["coordinator"]
        vin = bundle["vin"]
        self._attr_unique_id = f"geely_{vin}_btn_refresh"
        self._attr_name = "Refresh Data"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, vin)},
            manufacturer="Geely",
            name=bundle.get("device_name") or f"Geely ({vin})",
        )

    async def async_press(self) -> None:
        # Immediate refresh; raises to the UI if the poll fails.
        await self._coordinator.async_request_refresh()


class GeelyTelematicsButton(ButtonEntity):
    _attr_has_entity_name = True

    def __init__(self, hass: HomeAssistant, bundle: dict, key: str, name: str,
                 icon: str, service_id: str, params: list[dict]) -> None:
        self._hass = hass
        self._api = bundle["api"]
        self._vin = bundle["vin"]
        self._service_id = service_id
        self._params = params
        self._attr_unique_id = f"geely_{self._vin}_btn_{key}"
        self._attr_name = name
        self._attr_icon = icon
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._vin)},
            manufacturer="Geely",
            name=bundle.get("device_name") or f"Geely ({self._vin})",
        )

    async def async_press(self) -> None:
        try:
            resp = await self._hass.async_add_executor_job(
                self._api.control, self._service_id, self._params,
            )
        except GeelyControlError as e:
            raise HomeAssistantError(f"Geely {self._service_id}: {e.message}") from e
        except Exception as e:
            _LOGGER.exception("Button %s failed", self._service_id)
            raise HomeAssistantError(f"Geely {self._service_id} failure: {e}") from e
        _LOGGER.debug("Geely button %s response: %s", self._service_id, resp)


