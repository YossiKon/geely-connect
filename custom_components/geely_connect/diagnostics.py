"""Diagnostics support - downloads a full state dump with secrets redacted.

Lets users share a diagnostics report for troubleshooting without exposing
tokens, certificates, VIN or GPS location.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN

# Keys (config-entry data + status fields) to mask in the report.
_REDACT = {
    "token", "cidpsso_token", "accessToken", "access_token", "authCode",
    "cert_path", "key_path", "user_id", "userId", "vin", "device_id",
    "device_idfa", "device_idfv", "email",
    "latitude", "longitude", "lat", "lon", "lng",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    bundle = (hass.data.get(DOMAIN) or {}).get(entry.entry_id) or {}
    coordinator = bundle.get("coordinator")
    return {
        "entry_data": async_redact_data(dict(entry.data), _REDACT),
        "options": async_redact_data(dict(entry.options), _REDACT),
        "capabilities": bundle.get("capabilities", {}),
        "status": async_redact_data(
            (coordinator.data if coordinator else {}) or {}, _REDACT
        ),
    }
