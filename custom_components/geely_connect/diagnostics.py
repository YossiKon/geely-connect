"""Diagnostics support - downloads a full state dump with secrets redacted.

Lets users share a diagnostics report for troubleshooting without exposing
tokens, certificates, VIN or GPS location.
"""
from __future__ import annotations

import os
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .api import redact
from .const import DOMAIN

# Home Assistant's async_redact_data matches key names exactly, so this list
# holds the spellings that appear in the config entry and in GPS payloads.
#
# It is deliberately NOT the whole list: api.redact() runs over the same data
# afterwards and masks everything in its own _SECRET_KEYS / _IDENTIFYING_KEYS,
# matching on a normalised key (lowercased, separators stripped) so spelling
# variants cannot slip through. Anything the two lists disagree about is
# therefore still masked by the second pass - which is the point, because these
# lists drifted apart once already and let the scheduled-charging "pin" field,
# which carries the VIN, into the report.
_REDACT = {
    "token", "cidpsso_token", "accessToken", "access_token", "authCode",
    "cert_path", "key_path", "user_id", "userId", "vin", "device_id",
    "device_idfa", "device_idfv", "email",
    "latitude", "longitude", "lat", "lon", "lng",
}


def _clean(data: Any) -> Any:
    """Both redaction passes, ours first.

    api.redact() runs before Home Assistant's so the output reads cleanly: a
    value it shortens to a tail is then replaced outright by the second pass,
    rather than the second pass leaving a tail of the word REDACTED.
    """
    return async_redact_data(redact(data), _REDACT)


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    bundle = (hass.data.get(DOMAIN) or {}).get(entry.entry_id) or {}
    coordinator = bundle.get("coordinator")
    return {
        "entry_data": _clean(dict(entry.data)),
        "options": _clean(dict(entry.options)),
        # Capability flags are not user data, but the catalog is echoed from the
        # server and has carried a vin field, so it goes through as well.
        "capabilities": _clean(bundle.get("capabilities") or {}),
        "status": _clean((coordinator.data if coordinator else {}) or {}),
        # Whether the dashboard cards are actually being served, and from
        # where: the first thing to check when a card reads "Custom element
        # not found" but the vehicle itself is fine.
        "cards": _card_status(hass),
    }


def _card_status(hass: HomeAssistant) -> dict[str, Any]:
    from . import cards

    path = os.path.join(os.path.dirname(__file__), "geely-card.js")
    resources = getattr(hass.data.get("lovelace"), "resources", None)
    listed = []
    if resources is not None and hasattr(resources, "async_items"):
        try:
            listed = [i.get("url") for i in resources.async_items() or []
                      if cards.CARD_URL in str(i.get("url", ""))]
        except Exception:  # noqa: BLE001 - diagnostics must never raise
            listed = ["<unreadable>"]
    return {
        "file_present": os.path.isfile(path),
        "url": cards.CARD_URL,
        "registered": bool(hass.data.get(f"{DOMAIN}_cards_registered")),
        "lovelace_resources": listed,
    }
