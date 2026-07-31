"""Small pieces shared by every entity platform.

These were each copied into six to eight platform modules before this file
existed - `_walk` alone had eight definitions - so a change to any of them
had to be made everywhere or the copies drifted apart silently.

Nothing here talks to the car. It is all shaping of data the coordinator has
already fetched, plus two Home Assistant conveniences.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo

from .api import GeelyControlError
from .const import DOMAIN

# The four window corners, in the order the protocol names them.
WINDOW_CORNERS: tuple[str, ...] = ("Driver", "Passenger", "DriverRear", "PassengerRear")

_CLIMATE_PATH = ("vehicleStatus", "additionalVehicleStatus", "climateStatus")


def walk(d: Any, path: tuple[str, ...]) -> Any:
    """Follow a tuple of dict keys, returning None the moment one is missing.

    Note this cannot distinguish "key absent" from "key present and null";
    both are None. Every caller so far treats those the same.
    """
    cur = d
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
        if cur is None:
            return None
    return cur


def truthy(v: Any) -> bool:
    """The server's several spellings of yes.

    Deliberately NOT the same as switch.py's `_state_in`, which tests
    membership in a per-entity tuple. They disagree: truthy("yes") is True
    here and False there, so the two must not be merged.
    """
    return str(v).lower() in ("1", "true", "yes")


def device_info(vin: str, device_name: str | None = None) -> DeviceInfo:
    """The one device every entity of a vehicle belongs to."""
    return DeviceInfo(
        identifiers={(DOMAIN, vin)},
        manufacturer="Geely",
        name=device_name or f"Geely ({vin})",
    )


def windows_open(data: Any) -> bool | None:
    """True if any window the car reports is not closed.

    None when the car reports no `winStatus*` field at all - a trim that does
    not publish them must read unknown, not "open", or every "left open"
    automation fires forever.
    """
    climate = walk(data or {}, _CLIMATE_PATH) or {}
    any_seen = False
    for corner in WINDOW_CORNERS:
        v = climate.get(f"winStatus{corner}")
        if v is None:
            continue
        any_seen = True
        if str(v) != "2":           # "2" = closed
            return True
    return False if any_seen else None


def schedule_refresh(hass: HomeAssistant, coordinator, *delays: float,
                     after: Callable[[], None] | None = None) -> None:
    """Poll the car again a little after a command, without blocking the call.

    Delays are RELATIVE and cumulative: schedule_refresh(hass, c, 15, 20, 20)
    refreshes at t=15, t=35 and t=55. The car acknowledges a command to the
    gateway well before it acts on it, so an immediate refresh would just
    re-read the old state.

    `after` runs once the last refresh is done - used to drop an optimistic
    override at the point real state is finally available.
    """
    async def _run() -> None:
        for delay in delays:
            await asyncio.sleep(delay)
            await coordinator.async_request_refresh()
        if after is not None:
            after()

    hass.async_create_task(_run())


@contextmanager
def translate_control_errors(logger, label: str, log_msg: str = "", *log_args):
    """Turn a rejected command into something the user sees.

    Without this a GeelyControlError - rate limit, wrong parameters, feature
    unavailable - propagates as an unhandled exception and the user is told
    nothing. HomeAssistantError surfaces as a toast.
    """
    try:
        yield
    except GeelyControlError as e:
        raise HomeAssistantError(f"Geely {label}: {e.message}") from e
    except Exception as e:
        if log_msg:
            logger.exception(log_msg, *log_args)
        raise HomeAssistantError(f"Geely {label} failure: {e}") from e
