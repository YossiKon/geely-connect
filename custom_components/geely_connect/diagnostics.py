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
    # New Geely EM (Zeekr) platform, forward-support for #33: the account
    # password and session tokens the entry stores. redact() (the first pass)
    # already masks these by their normalised name, so this is the belt to
    # that pass's braces - the two lists exist precisely so neither is the
    # single point that has to be right. `zeekr_hf_expiry` is a timestamp and
    # stays visible on purpose.
    "zeekr_access_token", "zeekr_refresh_token", "zeekr_hf_token", "zeekr_password",
}


def _clean(data: Any) -> Any:
    """Both redaction passes, ours first.

    api.redact() runs before Home Assistant's so the output reads cleanly: a
    value it shortens to a tail is then replaced outright by the second pass,
    rather than the second pass leaving a tail of the word REDACTED.
    """
    return async_redact_data(redact(data), _REDACT)


def _scrub(text: Any, vin: str | None) -> str:
    """Take the VIN out of a free-text field.

    Both redaction passes match on key names, which is enough for structured
    payloads and no help at all inside a sentence. Exception messages are
    supposed to be VIN-free already - that was fixed deliberately - but a
    report is the wrong place to rely on every future `raise` remembering it.
    """
    out = "" if text is None else str(text)
    return out.replace(vin, "***redacted***") if vin and vin in out else out


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    bundle = (hass.data.get(DOMAIN) or {}).get(entry.entry_id) or {}
    coordinator = bundle.get("coordinator")
    vin = entry.data.get("vin")
    return {
        "entry_data": _clean(dict(entry.data)),
        "options": _clean(dict(entry.options)),
        # Why the data in this report is as old as it is, and whether the
        # integration is talking to the car at all. A stale reading and a failing
        # fetch look identical in `status` alone, which is how #21 stayed open.
        "polling": _polling(bundle, coordinator, vin),
        # The last commands sent and what came back. A command rejected because
        # the car was still busy is dropped, not retried, and leaves no trace
        # anywhere else unless debug logging happened to be on beforehand.
        "recent_commands": _clean(list(
            getattr(bundle.get("api"), "command_trail", None) or [])),
        # So that "I turned debug logging on and there was nothing" has an
        # answer other than guesswork.
        "logging": _logging(),
        # Capability flags are not user data, but the catalog is echoed from the
        # server and has carried a vin field, so it goes through as well.
        "capabilities": _clean(bundle.get("capabilities") or {}),
        # The catalog verbatim, not just the flags we know how to derive. The
        # parser keeps about a dozen keys and drops the rest, so a report could
        # not answer "does this trim advertise a blower level, or seat
        # positions by name?" - the questions that decide whether a missing
        # control is missing from the car or only from this integration.
        "capabilities_raw": _clean(bundle.get("capabilities_raw") or []),
        "status": _clean((coordinator.data if coordinator else {}) or {}),
        # Whether the dashboard cards are actually being served, and from
        # where: the first thing to check when a card reads "Custom element
        # not found" but the vehicle itself is fine.
        "cards": _card_status(hass),
    }


def _polling(bundle: dict, coordinator: Any, vin: str | None) -> dict[str, Any]:
    """How the poller is doing, in the terms the adaptive logic thinks in."""
    poll = dict(bundle.get("poll_state") or {})
    # The change signature is an opaque hash of the fields that decide whether
    # anything moved. The streak counted from it is the readable part.
    poll.pop("sig", None)
    interval = getattr(coordinator, "update_interval", None)
    return {
        "cycle": poll.get("cycle"),
        "unchanged_polls": poll.get("idle"),
        "force_secondary_pending": bool(poll.get("force_secondary")),
        "interval_seconds": (interval.total_seconds()
                             if interval is not None else None),
        "last_update_success": getattr(coordinator, "last_update_success", None),
        "last_exception": _scrub(getattr(coordinator, "last_exception", None),
                                 vin)[:300],
    }


def _logging() -> dict[str, Any]:
    import logging

    logger = logging.getLogger("custom_components.geely_connect")
    return {
        "effective_level": logging.getLevelName(logger.getEffectiveLevel()),
        "debug_enabled": logger.isEnabledFor(logging.DEBUG),
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
