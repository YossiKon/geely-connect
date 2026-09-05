"""Lock entity for the Geely vehicle.

State source: vehicleStatus.additionalVehicleStatus.drivingSafetyStatus.centralLockingStatus
Lock action  : RDL_2 with door=all
Unlock action: RDU_2 with door=all

UX:
  * Optimistic state - the entity flips to the requested state immediately
    so HA's lock-card animation is responsive (HA defaults are slow when a
    command takes 5-10 s to round-trip).
  * Transitional state - `is_locking` / `is_unlocking` are True while we
    wait for the next poll to confirm. HA shows a spinner during this.
  * On polling refresh (~8 s after fire) we drop the optimistic flag and
    show whatever the server actually reports.
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
import time
from typing import Any

from homeassistant.components.lock import LockEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import GeelyControlError, redact

from .const import (
    DOMAIN,
    SERVICE_LOCK,
    SERVICE_LOCK_PARAMS,
    SERVICE_UNLOCK,
)
from .helpers import walk as _walk, schedule_refresh

SERVICE_UNLOCK_PARAMS = SERVICE_LOCK_PARAMS

_LOGGER = logging.getLogger(__name__)

_LOCK_STATE_PATH = (
    "vehicleStatus", "additionalVehicleStatus", "drivingSafetyStatus",
    "centralLockingStatus",
)

# The fallback for a trim that never sends the central field. A South African
# E2 reports all four per-door locks and no `centralLockingStatus` at all, so
# this entity sat at Unknown through every lock and unlock while the four door
# sensors beside it read Locked correctly - the owner ended up rebuilding the
# aggregate himself in a template (#72).
#
# Sound because these are the same four fields the door-lock sensors already
# publish, and they were captured moving in lockstep with `centralLockingStatus`
# on a car that sends both (1/1/1/1 locked, 0/0/0/0 unlocked). Used only when
# the central field is missing, so a car that sends it is untouched.
_DOOR_LOCK_PATH = (
    "vehicleStatus", "additionalVehicleStatus", "drivingSafetyStatus",
)
_DOOR_LOCK_FIELDS = (
    "doorLockStatusDriver", "doorLockStatusPassenger",
    "doorLockStatusDriverRear", "doorLockStatusPassengerRear",
)

# How long to show the locking/unlocking spinner before falling back to
# whatever the server reports.
#
# Was 12s, sized to "one fresh poll" - but the poll 8s after a command very
# often re-reads the snapshot from BEFORE the command, because the gateway
# acknowledges long before the car's telemetry re-uploads. An owner pressed
# Lock, watched it show locked, and then watched it snap back to unlocked at
# t=8 while the car outside was actually locking - the release below used to
# fire on that stale poll unconditionally. The transition now ends only on
# agreement or on this timeout, so the window is sized to how long a slow car
# takes to report, not to how soon we ask. 40s matches the 45/60s holds the
# switches use.
_TRANSITION_TIMEOUT_S = 40.0




async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add_entities: AddEntitiesCallback) -> None:
    bundle = hass.data[DOMAIN][entry.entry_id]
    add_entities([GeelyLock(hass, bundle)])


class GeelyLock(CoordinatorEntity, LockEntity):
    _attr_has_entity_name = True

    def __init__(self, hass: HomeAssistant, bundle: dict) -> None:
        super().__init__(bundle["coordinator"])
        self._hass = hass
        self._api = bundle["api"]
        self._vin = bundle["vin"]
        self._attr_unique_id = f"geely_{self._vin}_lock"
        self._attr_name = "Doors"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._vin)},
            manufacturer="Geely",
            name=bundle.get("device_name") or f"Geely ({self._vin})",
        )
        # Optimistic state: target lock state and the timestamp when we
        # started the operation. While `time.time() < _started + timeout`,
        # HA shows the spinner via is_locking/is_unlocking and reports the
        # target as is_locked.
        self._pending_target_locked: bool | None = None
        self._pending_started_at: float = 0.0

    # ---- helpers ----

    def _api_is_locked(self) -> bool | None:
        v = _walk(self.coordinator.data or {}, _LOCK_STATE_PATH)
        if v is not None:
            # 1 / 2 = locked (2 occasionally seen for double-locked); 0 = unlocked
            return v in ("1", 1, "2", 2)
        # No central field on this trim - fall back to the four door locks,
        # which use the same codes. Locked only when every door that reports
        # says locked; unknown while none of them do, so a car that sends
        # neither still reads Unknown rather than a confident "unlocked".
        safety = _walk(self.coordinator.data or {}, _DOOR_LOCK_PATH)
        if not isinstance(safety, dict):
            return None
        seen = [safety.get(f) for f in _DOOR_LOCK_FIELDS]
        seen = [s for s in seen if s is not None]
        if not seen:
            return None
        return all(s in ("1", 1, "2", 2) for s in seen)

    def _is_in_transition(self) -> bool:
        if self._pending_target_locked is None:
            return False
        if time.time() - self._pending_started_at > _TRANSITION_TIMEOUT_S:
            return False
        # Still in transition unless the API has already caught up.
        api = self._api_is_locked()
        if api is None:
            return True
        return api != self._pending_target_locked

    # ---- HA properties ----

    @property
    def is_locked(self) -> bool | None:
        if self._is_in_transition():
            return self._pending_target_locked
        return self._api_is_locked()

    @property
    def is_locking(self) -> bool:
        return self._is_in_transition() and self._pending_target_locked is True

    @property
    def is_unlocking(self) -> bool:
        return self._is_in_transition() and self._pending_target_locked is False

    # ---- writes ----

    async def async_lock(self, **_: Any) -> None:
        await self._fire(SERVICE_LOCK, SERVICE_LOCK_PARAMS, target_locked=True)

    async def async_unlock(self, **_: Any) -> None:
        await self._fire(SERVICE_UNLOCK, SERVICE_UNLOCK_PARAMS, target_locked=False)

    async def _fire(self, service_id: str, params: list[dict], *,
                    target_locked: bool) -> None:
        # Fire FIRST. Only set the optimistic transition if the server
        # accepts - otherwise the user gets a misleading "locking…" spinner
        # for a command that was actually rejected.
        try:
            resp = await self._hass.async_add_executor_job(
                self._api.control, service_id, params,
            )
        except GeelyControlError as e:
            raise HomeAssistantError(f"Geely {service_id}: {e.message}") from e
        except Exception as e:
            _LOGGER.exception("lock %s failed", service_id)
            raise HomeAssistantError(f"Geely {service_id} failure: {e}") from e
        _LOGGER.debug("Geely lock %s response: %s", service_id, redact(resp))
        self._pending_target_locked = target_locked
        self._pending_started_at = time.time()
        self.async_write_ha_state()
        # Two polls, and NO forced release. The release used to run after the
        # first poll whether or not it brought post-command data, and the 8s
        # snapshot is routinely the pre-command one - which snapped the lock
        # back to its old state on screen while the car was executing the
        # command. _is_in_transition already ends the hold the moment a poll
        # AGREES with the target, and the timeout above ends it if nothing
        # ever does; a poll that still shows the old state is not evidence
        # the command failed, only that the car has not reported yet.
        schedule_refresh(self._hass, self.coordinator, 8, 12)
