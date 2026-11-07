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

On a car whose catalogue puts the schedule in slot 2 (an E2, #72) both read
the slot's own window, `startTime` and `endtime`, and a change goes out in
the E2 app's body with the same start/stop rule. The E2's app sends its
window only with the toggle; that a stop stores it is shown by the car's
read-back after one, while resending start while on, or stop while off, is
untried there.
"""
# -----------------------------------------------------------------------------
# Portions of this file - the reverse-engineered Geely protocol / field mappings
# (the parts that required protocol research) - are derived from
# nitaybz/geely-global-ha, used under the MIT License. See NOTICE.txt.
# Original framework, security hardening and transport are our own work.
# -----------------------------------------------------------------------------
from __future__ import annotations

import logging
import time as time_mod
from datetime import time as dtime

from homeassistant.components.time import TimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import GeelyControlError, redact, window_length
from .const import DOMAIN
from .helpers import (api_serves, schedule_intent, slot_2_is_on, slot_2_window,
                      truthy as _truthy, schedule_refresh)

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




async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add_entities: AddEntitiesCallback) -> None:
    bundle = hass.data[DOMAIN][entry.entry_id]
    caps = bundle.get("capabilities") or {}
    verdict = bundle.get("propulsion")
    if verdict and not verdict.charges:
        _LOGGER.debug("No socket on this car - skipping the charging schedule")
        return
    if not caps.get("scheduled_charging.enabled", True) and not caps.get("charging.enabled", True):
        _LOGGER.info("Capability says scheduled charging not supported - skipping time entities")
        return
    # Read and written through the charge-server, which the new platform does
    # not serve yet - see helpers.api_serves (#88).
    if not api_serves(bundle.get("api"), "charge_server"):
        _LOGGER.debug("No charge-server on this platform - skipping the charging schedule")
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
        self._slot_2 = bool((bundle.get("capabilities") or {})
                            .get("scheduled_charging.slot_2"))

    def _sched(self) -> dict:
        return (self.coordinator.data or {}).get("_scheduled_charging") or {}

    def _is_on(self, reported: bool) -> bool:
        """The on/off a time change is sent with: what the owner just asked
        the switch for while its 60 s hold stands, else what the slot reads.

        The slot is not enough on its own. A poll between the switch press
        and the time change can put the server's stale answer back, and on
        an E2 that read happens on every poll while the plug is in - so a
        time change a minute after switching on went out as `stop` (#72).
        """
        intent = schedule_intent(self.coordinator, time_mod.time())
        return reported if intent is None else intent

    @property
    def native_value(self) -> dtime | None:
        # Optimistic override - holds for the full 60s. Don't drop early
        # on server match: we patch coordinator.data ourselves after a
        # fire, so the "match" check would always succeed and defeat
        # the guard.
        if (self._optimistic_value is not None
                and time_mod.time() < self._optimistic_until):
            return self._optimistic_value
        if self._slot_2:
            window = slot_2_window(self._sched()) or {}
            return _parse_hhmm(window.get("startTime" if self._kind == "start" else "endtime"))
        return _parse_hhmm(self._sched().get(self._field))

    async def async_set_value(self, value: dtime) -> None:
        if self._slot_2:
            await self._set_slot_2(value)
            return
        sched = self._sched()
        new_start = _fmt_hhmm(value) if self._kind == "start" else (sched.get("rbcStartTime") or "23:00")
        new_end   = _fmt_hhmm(value) if self._kind == "end"   else (sched.get("rbcEndTime")   or "07:00")
        # Preserve current on/off - if schedule is currently active,
        # command=start re-asserts it; if not active, command=stop pushes
        # times without enabling.
        command = "start" if self._is_on(_truthy(sched.get("bcCycleActive"))) else "stop"
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
                      self._kind, _fmt_hhmm(value), redact(resp))

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

        # Server takes about 30s to propagate, so refresh at 15, 35 and 55s.
        # Delays are relative.
        schedule_refresh(self._hass, self.coordinator, 15, 20, 20)

    async def _set_slot_2(self, value: dtime) -> None:
        sched = self._sched()
        window = slot_2_window(sched)
        if window is None:
            raise HomeAssistantError(
                "Geely Scheduled Charging time: the car holds no charging window "
                "yet - set the start and end once in the Geely app")
        start = _fmt_hhmm(value) if self._kind == "start" else window["startTime"]
        end = _fmt_hhmm(value) if self._kind == "end" else window["endtime"]
        if start == end:
            raise HomeAssistantError(
                "Geely Scheduled Charging time: the start and the end can't be the same")
        # `chargeSts` first, `timerId` as the fallback (helpers.slot_2_is_on):
        # a start from here moves the first and not the second, and reading
        # the second sent every later time change out as `stop` (#72).
        command = "start" if self._is_on(slot_2_is_on(self.coordinator.data) is True) else "stop"
        try:
            resp = await self._hass.async_add_executor_job(
                lambda: self._api.scheduled_charging_set_slot_2(
                    command=command, start_time=start, end_time=end,
                    day=window.get("day") or "1,2,3,4,5,6,7",
                )
            )
        except GeelyControlError as e:
            raise HomeAssistantError(f"Geely Scheduled Charging time: {e.message}") from e
        except Exception as e:
            _LOGGER.exception("scheduled-charging set time failed")
            raise HomeAssistantError(f"Geely Scheduled Charging time failure: {e}") from e
        _LOGGER.debug("Set scheduled charging %s=%s response=%s",
                      self._kind, _fmt_hhmm(value), redact(resp))
        self._optimistic_value = value
        self._optimistic_until = time_mod.time() + 60
        window.update(startTime=start, endtime=end, duration=window_length(start, end))
        self.async_write_ha_state()
        schedule_refresh(self._hass, self.coordinator, 15, 20, 20)
