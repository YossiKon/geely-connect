"""Small pieces shared across the integration.

These were each copied into six to eight platform modules before this file
existed - `_walk` alone had eight definitions - so a change to any of them
had to be made everywhere or the copies drifted apart silently.

Nothing here talks to the car. It is all shaping of data the coordinator has
already fetched, plus two Home Assistant conveniences.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import math
import os
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any

import homeassistant.util.yaml as yaml_util

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo

from .api import GeelyControlError
from .const import (
    CONF_VEHICLE_COLOR,
    CONF_VEHICLE_MODEL_CODE,
    CONF_VEHICLE_NICKNAME,
    CONF_VEHICLE_POWER_TYPE,
    CONF_VEHICLE_SERIES,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


def vehicle_metadata(vehicle: dict) -> dict[str, str]:
    """The entry-data fields we keep about the car, from one vehicle-list record.

    Config flow writes these when the entry is created and
    `_maybe_refetch_vehicle_metadata` refreshes them later. Both go through here
    because the refresh used to rebuild the list by hand and dropped two of the
    five - and one of the dropped fields, `powerType`, is what decides whether
    the car gets fuel entities.

    Nickname is empty rather than "Geely" when the car has neither a nickname
    nor a model: the device name then falls through to the model code, which is
    more use than the literal brand.
    """
    return {
        CONF_VEHICLE_NICKNAME: (vehicle.get("nickname") or vehicle.get("nickName")
                                or vehicle.get("model") or vehicle.get("modelName")
                                or ""),
        CONF_VEHICLE_SERIES: (vehicle.get("series") or vehicle.get("seriesName")
                              or vehicle.get("appModelCode") or ""),
        CONF_VEHICLE_MODEL_CODE: (vehicle.get("modelCode") or vehicle.get("seriesCode")
                                  or vehicle.get("appModelCode") or ""),
        CONF_VEHICLE_COLOR: vehicle.get("color") or "",
        CONF_VEHICLE_POWER_TYPE: (vehicle.get("powerType") or vehicle.get("engineType")
                                  or ""),
    }


# ---- credential at-rest encryption (defense-in-depth) ----------------------
# The zeekr flow can store the account password so the HF session renews
# itself (the app does the same). .storage is plaintext on disk, so when the
# user provides a key in secrets.yaml ("geely_password_key"), the value is
# AES-256-GCM encrypted ("enc:" + base64). Without a key the value is stored
# plaintext with a warning - identical to the integration's existing posture
# (the legacy flow stores the mTLS private key in .storage). The key lives in
# secrets.yaml, OUTSIDE .storage, so a copy of .storage alone yields nothing.

_ENC_PREFIX = "enc:"
_SECRET_KEY_NAME = "geely_password_key"


def _encryption_key(hass: HomeAssistant) -> bytes | None:
    """32-byte AES key from secrets.yaml, or None when the user has not
    configured one (plaintext fallback applies)."""
    try:
        secrets = yaml_util.load_yaml(hass.config.path("secrets.yaml"))
    except Exception:  # noqa: BLE001 - missing or malformed secrets.yaml means "no key"
        return None
    key = (secrets or {}).get(_SECRET_KEY_NAME)
    if not key:
        return None
    return hashlib.sha256(str(key).encode()).digest()


def password_encrypt(hass: HomeAssistant, plain: str) -> str:
    """Encrypt the zeekr password for at-rest storage when a secrets.yaml
    key exists; otherwise return it plaintext (warning logged once)."""
    if not plain:
        return ""
    key = _encryption_key(hass)
    if key is None:
        _LOGGER.warning(
            "no '%s' key in secrets.yaml - storing the zeekr password "
            "plaintext; add the key to encrypt it at rest",
            _SECRET_KEY_NAME,
        )
        return plain
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, plain.encode(), None)
    return _ENC_PREFIX + base64.b64encode(nonce).decode() + ":" + base64.b64encode(ct).decode()


def password_decrypt(hass: HomeAssistant, stored: str) -> str:
    """Reverse of password_encrypt; plaintext values pass through."""
    if not stored:
        return ""
    if not stored.startswith(_ENC_PREFIX):
        return stored
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        key = _encryption_key(hass)
        if key is None:
            _LOGGER.warning(
                "zeekr password is stored encrypted but no '%s' key in "
                "secrets.yaml - reauthenticate or add the key",
                _SECRET_KEY_NAME,
            )
            return ""
        _, nonce_b64, ct_b64 = stored.split(":", 2)
        nonce = base64.b64decode(nonce_b64)
        ct = base64.b64decode(ct_b64)
        return AESGCM(key).decrypt(nonce, ct, None).decode()
    except Exception:  # noqa: BLE001 - wrong key or tampered value
        _LOGGER.error("zeekr password decrypt failed (key changed?) - reauthenticate")
        return ""


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


# The car reports "no estimate available" for its minute countdowns as 2047,
# which is 0x7FF - every bit of an 11-bit field set. Published verbatim it reads
# as a real 34-hour estimate, so it has to be filtered where it enters.
SIGNAL_UNAVAILABLE_MINUTES = 2047


def minutes_or_none(v: Any) -> float | None:
    """A minute countdown from the car, or None when it has no estimate.

    Covers `timeToFullyCharged` and `timeToTargetDisCharged`. Anything
    non-numeric, non-positive, or equal to the not-available sentinel is
    absent rather than zero - a charger that is not running has no ETA, and
    zero would render as "now".
    """
    try:
        m = float(v)
    except (TypeError, ValueError):
        return None
    # NaN slips through ordinary comparisons (every one is False), and a NaN
    # minute count would blow up in timedelta() inside a state write.
    if not math.isfinite(m) or m <= 0 or m >= SIGNAL_UNAVAILABLE_MINUTES:
        return None
    return m


def steering_wheel_fitted(caps: dict | None, data: Any) -> bool:
    """Whether there is evidence this car has a heated steering wheel.

    Either signal is enough:
      - the capability catalogue advertises `steel_wheel_heating` (parsed
        into `steering_wheel_heat.enabled` since v1.35.1), or
      - `steerWhlHeatingSts` reads the 1/2 on/off convention a fitted wheel
        uses. Three Starrays without the feature read 0 (#4), so 0 and
        absence are "no evidence", not "off".

    Deliberately NOT default-permissive like the other capability gates: the
    catalogue flag was underivable on every car before v1.35.1, so absence
    of the flag is weak, and a pressable control on a car without the
    hardware is the failure #13 was about.
    """
    if (caps or {}).get("steering_wheel_heat.enabled"):
        return True
    return walk(data or {}, (*_CLIMATE_PATH, "steerWhlHeatingSts")) in ("1", 1, "2", 2)


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
    override at the point real state is finally available. It runs even if a
    refresh fails: `after` is what releases the optimistic override, and
    skipping it would pin the entity to a guessed state until the next command.
    Cancellation (config entry unloaded, Home Assistant stopping) is the one
    case where it must not run - there is no entity left to write to.
    """
    async def _run() -> None:
        try:
            for delay in delays:
                await asyncio.sleep(delay)
                await coordinator.async_request_refresh()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a background poll must not raise
            _LOGGER.debug("post-command refresh failed", exc_info=True)
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


def speed_is_stale(basic: dict | None) -> bool:
    """Is `speed` in this basicVehicleStatus a value the car disowns?

    `speedValidity` sits next to `speed`, and when it goes false the number is
    whatever the car last put there. On a parked EX5 the pair reads 0.0 with
    the flag false, so they agree by coincidence - but a stale non-zero reading
    is real motion as far as anything downstream can tell (#44).

    Only an EXPLICIT falsy value counts. A trim that never reports the flag
    behaves exactly as before, which is what keeps this from silently blanking
    the speed on cars nobody has tested.

    One rule, one place, because two callers act on it and they must not
    drift: the sensor publishes unknown, and the poller must not read a
    disowned number as driving - it decides the poll interval and, through
    the driving lock, whether the card's buttons work at all.
    """
    if not isinstance(basic, dict):
        return False
    flag = basic.get("speedValidity")
    return flag is not None and str(flag).strip().lower() in ("false", "0")
