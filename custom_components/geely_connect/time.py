"""Geely time entities - Scheduled Charging start / end times.

Reads from `data["_scheduled_charging"]` (populated by the coordinator
each poll via `api.charge_server_get("6")`):
  rbcStartTime: "23:00"
  rbcEndTime:   "07:00"
  bcCycleActive: "true" (only present when scheduled charging is on)
  rbcTarget:    "2"
  rbcModel:     ""

Writes via `api.scheduled_charging_set()` - sends the full body, keeping
the current `command` (start if scheduled charging is on, stop if off)
so editing the time alone doesn't accidentally toggle the schedule.
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
import time as time_mod
from datetime import time as dtime
from typing import Any

from homeassistant.components.time import TimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import GeelyControlError
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


def _parse_hhmm(s: str | None) -> dtime | None:
    if not s or ":" not in s:
        return None
    try:
        h, m = s.split(":", 1)
        return dtime(int(h), int(m))
    except (TypeError, ValueError):
        return None


def _fmt_hhmm(t: dtime) -> str:
    return f"{t.hour:02d}:{t.minute:02d}"


def _truthy(v: Any) -> bool:
    return str(v).lower() in ("1", "true", "yes")


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add_entities: AddEntitiesCallback) -> None:
    bundle = hass.data[DOMAIN][entry.entry_id]
    caps = bundle.get("capabilities") or {}
    if caps and not caps.get("scheduled_charging.enabled", True) and not caps.get("charging.enabled", True):
        _LOGGER.info("Capability says scheduled charging not supported - skipping time entities")
        return
    add_entities([
        GeelyScheduledChargingTime(hass, bundle, "start"),
        GeelyScheduledChargingTime(hass, bundle, "end"),
    ])


class GeelyScheduledChargingTime(CoordinatorEntity, TimeEntity):
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, hass: HomeAssistant, bundle: dict, kind: str) -> None:
        super().__init__(bundle["coordinator"])
        assert kind in ("start", "end")
        self._hass = hass
        self._api = bundle["api"]
        self._vin = bundle["vin"]
        self._kind = kind
        self._field = "rbcStartTime" if kind == "start" else "rbcEndTime"
        self._attr_unique_id = f"geely_{self._vin}_time_scheduled_charging_{kind}"
        self._attr_name = f"Scheduled Charging {kind.title()}"
        self._attr_icon = "mdi:clock-time-four-outline"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._vin)},
            manufacturer="Geely",
            name=bundle.get("device_name") or f"Geely ({self._vin})",
        )
        # Optimistic value: stays for 60s after a successful set so the
        # slow Geely server propagation (about 30s for scheduled charging)
        # doesn't briefly revert the displayed time.
        self._optimistic_value: dtime | None = None
        self._optimistic_until: float = 0.0

    def _sched(self) -> dict:
        return (self.coordinator.data or {}).get("_scheduled_charging") or {}

    @property
    def native_value(self) -> dtime | None:
        # Optimistic override - holds for the full 60s. Don't drop early
        # on server match: we patch coordinator.data ourselves after a
        # fire, so the "match" check would always succeed and defeat
        # the guard.
        if (self._optimistic_value is not None
                and time_mod.time() < self._optimistic_until):
            return self._optimistic_value
        return _parse_hhmm(self._sched().get(self._field))

    async def async_set_value(self, value: dtime) -> None:
        sched = self._sched()
        new_start = _fmt_hhmm(value) if self._kind == "start" else (sched.get("rbcStartTime") or "23:00")
        new_end   = _fmt_hhmm(value) if self._kind == "end"   else (sched.get("rbcEndTime")   or "07:00")
        # Preserve current on/off - if schedule is currently active,
        # command=start re-asserts it; if not active, command=stop pushes
        # times without enabling.
        is_on = _truthy(sched.get("bcCycleActive"))
        command = "start" if is_on else "stop"
        rbc_target = sched.get("rbcTarget") or "2"
        # GET echoes `rbcModel`; SET writes it as `chargeModel`.
        charge_model = sched.get("rbcModel") or "0"
        try:
            resp = await self._hass.async_add_executor_job(
                lambda: self._api.scheduled_charging_set(
                    command=command,
                    start_time=new_start,
                    end_time=new_end,
                    rbc_target=rbc_target,
                    charge_model=charge_model,
                )
            )
        except GeelyControlError as e:
            raise HomeAssistantError(f"Geely Scheduled Charging time: {e.message}") from e
        except Exception as e:
            _LOGGER.exception("scheduled-charging set time failed")
            raise HomeAssistantError(f"Geely Scheduled Charging time failure: {e}") from e
        _LOGGER.debug("Set scheduled charging %s=%s response=%s",
                      self._kind, _fmt_hhmm(value), resp)

        # Optimistic local value: holds for 60s, until the slow server
        # propagation (about 30s) catches up.
        self._optimistic_value = value
        self._optimistic_until = time_mod.time() + 60
        # Also patch the coordinator's in-memory schedule so the switch
        # entity reads the new value when it builds its body. Without
        # this, a quick "set time, then flip switch on" has the switch
        # read a stale time and overwrite the server update.
        data = self.coordinator.data
        if isinstance(data, dict):
            data.setdefault("_scheduled_charging", {})[self._field] = _fmt_hhmm(value)
        self.async_write_ha_state()

        async def delayed_refresh():
            # Server takes about 30s to propagate. Refresh at 15, 35,
            # 55s so the UI catches up as soon as the real state lands.
            await asyncio.sleep(15)
            await self.coordinator.async_request_refresh()
            await asyncio.sleep(20)
            await self.coordinator.async_request_refresh()
            await asyncio.sleep(20)
            await self.coordinator.async_request_refresh()
        self._hass.async_create_task(delayed_refresh())
