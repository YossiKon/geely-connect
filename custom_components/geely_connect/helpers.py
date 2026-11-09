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


class PasswordKeyError(HomeAssistantError):
    """secrets.yaml exists but cannot be read, so whether a key is configured
    is unknown. Treating that as "no key" would store the password in clear
    because of a stray YAML error; the caller refuses instead. The message
    names the file and the failure, never a value."""


def _encryption_key(hass: HomeAssistant) -> bytes | None:
    """32-byte AES key from secrets.yaml, or None when the user has not
    configured one (plaintext fallback applies).

    None means the owner chose not to set a key: no secrets.yaml, an empty
    one, or one without the key. A file that exists but cannot be read or
    parsed raises PasswordKeyError."""
    try:
        secrets = yaml_util.load_yaml(hass.config.path("secrets.yaml"))
    except FileNotFoundError:
        return None
    except Exception as e:  # noqa: BLE001 - YAML errors, permissions, encoding
        raise PasswordKeyError(
            f"secrets.yaml exists but could not be read ({type(e.__cause__ or e).__name__}), "
            f"so whether it holds a '{_SECRET_KEY_NAME}' is unknown"
        ) from e
    if not secrets:
        return None
    if not isinstance(secrets, dict):
        raise PasswordKeyError("secrets.yaml is not a mapping of names to values")
    key = secrets.get(_SECRET_KEY_NAME)
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
    except PasswordKeyError as e:
        _LOGGER.error("zeekr password cannot be decrypted: %s - fix secrets.yaml "
                      "or reauthenticate", e)
        return ""
    except Exception:  # noqa: BLE001 - wrong key or tampered value
        _LOGGER.error("zeekr password decrypt failed (key changed?) - reauthenticate")
        return ""


# The four window corners, in the order the protocol names them.
WINDOW_CORNERS: tuple[str, ...] = ("Driver", "Passenger", "DriverRear", "PassengerRear")

_CLIMATE_PATH = ("vehicleStatus", "additionalVehicleStatus", "climateStatus")
_EV_PATH = ("vehicleStatus", "additionalVehicleStatus", "electricVehicleStatus")


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


def api_serves(api: Any, endpoint: str) -> bool:
    """Whether the car's api can serve one of the secondary endpoints:
    `state` (the *Active flags) or `charge_server` (the schedules).

    The new-platform adapter says no to both until they are mapped, and an
    entity that lives only on one of them can read nothing but unknown there
    and fail when pressed - so it is not created, and the poll does not ask
    (#88). The legacy client does not answer, and serves them all.
    """
    return bool(getattr(api, f"serves_{endpoint}", True))


def api_can_send(api: Any, service_id: str) -> bool:
    """Whether a control with this serviceId would go out at all.

    On the new platform's own route only what has been captured is mapped,
    and an unmapped command fails without reaching the car; every other api
    sends whatever it is given."""
    can_send = getattr(api, "can_send", None)
    return True if can_send is None else bool(can_send(service_id))


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


def slot_2_window(sched: Any) -> dict | None:
    """The charging window an E2 keeps in charge-server slot 2 (#72).

    The slot reads back as {timerId, scheduleList: [{startTime, endtime,
    duration, day, timerActivation}]}. None when the slot holds no window -
    there is then no time of the owner's to send, and none is invented.
    """
    windows = sched.get("scheduleList") if isinstance(sched, dict) else None
    if not (isinstance(windows, list) and windows and isinstance(windows[0], dict)):
        return None
    window = windows[0]
    if not (window.get("startTime") and window.get("endtime")):
        return None
    return window


def slot_2_is_on(data: Any) -> bool | None:
    """Whether an E2's Scheduled Charging is on, from what the car reports.

    `chargeSts` in `electricVehicleStatus` is the state. In ten diagnostics
    files from the one car that has this slot (#72, 16-19 October) it read
    "1" after every start and "0" after every stop, the app's own toggle and
    Home Assistant's alike, whether or not the car happened to be charging:
    "1" while charging at 97 %, "0" while charging at 59 %, "1" plugged in at
    58 % and *not* charging because the window had not opened - the car
    honouring a start sent from here.

    Slot 2's `timerId` ("1" on, "2" off) moved only for the app's own toggle;
    a start from here left it at "2" while `chargeSts` went to "1" within 16
    seconds. So `chargeSts` decides whenever the car sends it, and `timerId`
    only when it does not. The order matters for an app-on followed by a
    stop from here, where `timerId` would still say on.

    None when neither field answers: unknown, not off.
    """
    ev = walk(data or {}, _EV_PATH) or {}
    sts = str(ev.get("chargeSts"))
    if sts in ("0", "1"):
        return sts == "1"
    sched = data.get("_scheduled_charging") if isinstance(data, dict) else None
    sched = sched if isinstance(sched, dict) else {}
    return {"1": True, "2": False}.get(str(sched.get("timerId")))


def note_slot_2_state(data: Any, on: bool) -> None:
    """Write the state just asked for into the coordinator's copy of the car.

    Both fields `slot_2_is_on` reads, so the switch and the time entities
    agree until the next poll replaces the copy with what the car says.
    """
    ev = walk(data or {}, _EV_PATH)
    if isinstance(ev, dict):
        ev["chargeSts"] = "1" if on else "0"
    sched = data.get("_scheduled_charging") if isinstance(data, dict) else None
    if isinstance(sched, dict):
        sched["timerId"] = "1" if on else "2"


def windows_position(data: Any) -> int | None:
    """How far open the windows are, 0-100, as the most-open corner.

    The car reports a per-corner `winPos*` alongside `winStatus*`; the command
    range is 0-100 (step 4), and a vent crack reads ~8. Returns the maximum so
    the aggregate "All Windows" cover reads how open the most-open window is,
    or None when the car publishes no `winPos*` field at all.
    """
    climate = walk(data or {}, _CLIMATE_PATH) or {}
    best: int | None = None
    for corner in WINDOW_CORNERS:
        v = climate.get(f"winPos{corner}")
        if v is None:
            continue
        try:
            n = int(float(v))
        except (TypeError, ValueError):
            continue
        n = max(0, min(100, n))
        best = n if best is None else max(best, n)
    return best


def note_schedule_intent(coordinator, on: bool, until: float) -> None:
    """Remember that the owner just switched Scheduled Charging on or off.

    The switch holds its own optimistic state for 60 s, but the time entities
    read the slot the coordinator holds, and a poll in between can put the
    server's not-yet-propagated answer back there. On an E2 the slot is
    re-read on every poll while the plug is in, so a time change made a
    minute after switching the schedule on went out as `stop` and switched it
    off again (#72). The intent lives on the coordinator, which outlives
    each poll's data dict, and is read by `schedule_intent`.
    """
    coordinator.geely_schedule_intent = (bool(on), float(until))


def schedule_intent(coordinator, now: float) -> bool | None:
    """The on/off the owner last asked for, while that hold still stands."""
    intent = getattr(coordinator, "geely_schedule_intent", None)
    if intent is None or now >= intent[1]:
        return None
    return intent[0]


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


def exterior_temp_is_stale(climate: dict | None) -> bool:
    """Is `exteriorTemp` a reading the car disowns?

    `exteriorTempValidity` sits beside it exactly as `speedValidity` sits
    beside the speed, and it carries the same meaning: when it is false the
    number is whatever was last left in that slot. A Colombian EX2 published
    130 with the flag false while the real air was 25-28 (#24) - and the
    per-entry temperature offset cannot rescue that one, because it is not a
    measurement to correct. Unknown is the honest reading.

    Only an EXPLICIT falsy value counts, so a trim that never reports the flag
    behaves exactly as before - the same rule that keeps the speed gate from
    blanking cars nobody has tested.
    """
    if not isinstance(climate, dict):
        return False
    flag = climate.get("exteriorTempValidity")
    return flag is not None and str(flag).strip().lower() in ("false", "0")


def sunroof_is_stale(climate: dict | None) -> bool:
    """Is `sunroofOpenStatus` a reading the car disowns?

    `sunroofOpenStatusValidity` is the third flag of this family. Two E2s with
    no sunroof (#72) send `sunroofOpenStatus: 1` beside the flag reading
    false - so the cover read "Closed" on a roof the car does not have - while
    an EX5 that has one sends the status with no flag at all. Same contract as
    the two above: only an EXPLICIT falsy value counts, so a car that never
    reports the flag is untouched.
    """
    if not isinstance(climate, dict):
        return False
    flag = climate.get("sunroofOpenStatusValidity")
    return flag is not None and str(flag).strip().lower() in ("false", "0")
