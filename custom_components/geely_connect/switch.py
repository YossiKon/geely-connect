"""Geely switches.

AVD-verified 2026-05-01 - see docs/AVD_CAPTURE_GUIDE.md.

  G-clean              → RCC_2 / [{rcc.ventilation: "cabin"}], duration=6
                         State: airBlowerActive (this trim - verified)
                         Mutex: unavailable when AC or defrost is on
  Charging start/stop  → RCS / [{rcs.restart|terminate: "1"}]
                         State: statusOfChargerConnection
  Window ventilation   → RWS_2 / [{target: ventilate|window}]
                         State: any winStatus* != 2
  Parking Comfort      → RSM start/stop
                         State: _state.parkComfortState

  Defrost is now a CLIMATE PRESET (see climate.py), not a switch - keeps
  preClimateActive + defrost as a unified climate state.
  Scheduled charging - left as legacy switch using rcs.setting.
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

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo, EntityCategory

from .api import GeelyControlError, redact
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    SERVICE_CHARGING_START_PARAMS,
    SERVICE_CHARGING_STOP_PARAMS,
    SERVICE_WINDOW_VENT_PARAMS,
    RCE_KEY_CONDITIONER,
    RCE_KEY_LEVEL,
    RCE_VAL_DEFROST,
    SERVICE_CHARGING,
    SERVICE_CLIMATE,
    SERVICE_GCLEAN,
    SERVICE_GCLEAN_DURATION,
    SERVICE_GCLEAN_PARAMS,
    SERVICE_PARKING_COMFORT,
    SERVICE_WINDOW,
)
from .helpers import walk as _walk, windows_open, schedule_refresh

_LOGGER = logging.getLogger(__name__)

_CLIMATE_PATH = ("vehicleStatus", "additionalVehicleStatus", "climateStatus")
_EV_PATH      = ("vehicleStatus", "additionalVehicleStatus", "electricVehicleStatus")
# Secondary status endpoint (vehicle_status_state) - provides *Active flags.
_STATE_PATH   = ("_state",)




def _state_in(v: Any, on_values: tuple[Any, ...]) -> bool:
    """Membership test against this entity's own ON values.

    NOT helpers.truthy, which accepts "yes"/"TRUE" for every caller. Each
    switch here declares exactly which raw values mean on, so the two must
    stay apart; naming them differently is what stops them being merged
    later. The old default was never used - all call sites pass on_values.
    """
    return v in on_values


# (key, name, icon, service_id, on_params, off_params, command_on, command_off, state_path, on_when_in, capability_flag)
SWITCH_DEFS: list[tuple] = [
    # Parking Comfort - the path below is absent on purpose, so is_on returns
    # None and the switch reports `unknown`.
    #
    # parkComfortState is not the on/off flag it looks like. On a P145-J1 with
    # parking comfort *off* in the car it still reads 1, alongside
    # activateState, inhibitionState, liveDetectionState, positionUploadState
    # and svtState - six `*State` fields at 1 at once, which no car can be
    # doing simultaneously. Reading it as on/off pinned the switch to `on`
    # forever, contradicting the car. Every other switch keys off an `*Active`
    # field; this was the only `*State` one.
    #
    # No replacement field is known, and there may not be one: no
    # parkComfort*Active appears in that car's 35-key payload, and
    # charge_server_get("4") - the AVD-verified parking-comfort endpoint, which
    # nothing currently polls - returns only a schedule (scheduleList,
    # startTime, endTime) with no state field at all. That fits a feature
    # activated from inside the car, where the cloud offers a schedule rather
    # than a toggle. RSM is marked UNVERIFIED on this trim in const.py, so
    # whether the write path does anything is untested either.
    (
        "parking_comfort", "Parking Comfort", "mdi:sleep",
        SERVICE_PARKING_COMFORT, [], [],
        "start", "stop",
        (*_STATE_PATH, "parkComfortActive"),
        (1, "1"),
        "parking_comfort.enabled",
    ),
    # Charging start/stop - AVD-verified 2026-05-01:
    #   start: command="start", [{operation:"1"},{rcs.restart:"1"}]
    #   stop:  command="stop",  [{operation:"0"},{rcs.terminate:"1"}]
    (
        "charging", "Charging", "mdi:ev-station",
        SERVICE_CHARGING,
        SERVICE_CHARGING_START_PARAMS,
        SERVICE_CHARGING_STOP_PARAMS,
        "start", "stop",
        (*_EV_PATH, "statusOfChargerConnection"),
        ("3", 3),
        "charging.enabled",
    ),
    # Scheduled charging is now a dedicated entity - see GeelyScheduledChargingSwitch
    # below (separate class because it uses the charge-server endpoint, not RCS).
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add_entities: AddEntitiesCallback) -> None:
    bundle = hass.data[DOMAIN][entry.entry_id]
    caps = bundle.get("capabilities") or {}
    verdict = bundle.get("propulsion")
    # A car with no socket - a non-plug hybrid or a petrol car - cannot start
    # a charge, so the charging switches would only ever fail. See
    # Verdict.charges; a missing verdict keeps the pre-hybrid entity set.
    charges = verdict.charges if verdict else True
    entities: list[SwitchEntity] = []
    for defn in SWITCH_DEFS:
        if defn[0] == "charging" and not charges:
            continue
        flag = defn[-1]
        if flag and not caps.get(flag, True):
            _LOGGER.debug("switch %s skipped (capability flag %s=False)", defn[0], flag)
            continue
        entities.append(GeelySwitch(hass, bundle, *defn[:-1]))
    if caps.get("windows.enabled", True):
        entities.append(GeelyWindowVentilationSwitch(hass, bundle))
    if caps.get("gclean.enabled", True):
        entities.append(GeelyGCleanSwitch(hass, bundle))
    if caps.get("ac.enabled", True) and caps.get("defrost.enabled", True):
        entities.append(GeelyDefrostSwitch(hass, bundle))
    if charges and (caps.get("scheduled_charging.enabled", True)
                    or caps.get("charging.enabled", True)):
        entities.append(GeelyScheduledChargingSwitch(hass, bundle))
    add_entities(entities)


class GeelySwitch(CoordinatorEntity, SwitchEntity):
    _attr_has_entity_name = True

    def __init__(self, hass: HomeAssistant, bundle: dict, key: str, name: str,
                 icon: str | None, service_id: str,
                 on_params: list[dict], off_params: list[dict],
                 command_on: str, command_off: str,
                 state_path: tuple[str, ...], on_when_in: tuple[Any, ...]) -> None:
        super().__init__(bundle["coordinator"])
        self._hass = hass
        self._api = bundle["api"]
        self._vin = bundle["vin"]
        self._service_id = service_id
        self._on_params = on_params
        self._off_params = off_params
        self._command_on = command_on
        self._command_off = command_off
        self._state_path = state_path
        self._on_when_in = on_when_in
        self._key = key
        self._attr_unique_id = f"geely_{self._vin}_sw_{key}"
        self._attr_name = name
        if icon:
            self._attr_icon = icon
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._vin)},
            manufacturer="Geely",
            name=bundle.get("device_name") or f"Geely ({self._vin})",
        )

    @property
    def is_on(self) -> bool | None:
        v = _walk(self.coordinator.data or {}, self._state_path)
        if v is None:
            return None
        if self._key == "charging":
            # DC fast charge never moves statusOfChargerConnection off 1 on
            # some cars (#10); the composite in sensor._is_charging reads the
            # DC contactor and current sign as well, so the switch shows on
            # for the sessions that field misses.
            from .sensor import _is_charging
            return _is_charging(self.coordinator.data or {})
        return _state_in(v, self._on_when_in)

    async def async_turn_on(self, **_: Any) -> None:
        await self._fire(self._on_params, self._command_on)

    async def async_turn_off(self, **_: Any) -> None:
        await self._fire(self._off_params, self._command_off)

    async def _fire(self, params: list[dict], command: str) -> None:
        try:
            resp = await self._hass.async_add_executor_job(
                self._api.control, self._service_id, params, command,
            )
        except GeelyControlError as e:
            raise HomeAssistantError(f"Geely {self._service_id}: {e.message}") from e
        except Exception as e:
            _LOGGER.exception("switch %s %s failed", self._service_id, command)
            raise HomeAssistantError(f"Geely {self._service_id} failure: {e}") from e
        _LOGGER.debug("Geely switch %s %s params=%s response=%s",
                      self._service_id, command, redact(params), redact(resp))

        schedule_refresh(self._hass, self.coordinator, 8)


class GeelyGCleanSwitch(CoordinatorEntity, SwitchEntity):
    """G-clean (cabin air purification).

    AVD-verified 2026-05-01:
      ON  → RCC_2 / start / [{rcc.ventilation: "cabin"}], duration=6
      OFF → RCC_2 / stop  / [{rcc.ventilation: "cabin"}], duration=6
      State: airBlowerActive (true/false)

    Mutex: G-clean cannot be turned on while AC or defrost is active -
    the car silently rejects the command. Surface this via `available`.
    AC/defrost activating while G-clean is on auto-stops G-clean (server-
    side); HA picks that up on next status poll.
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:leaf"

    def __init__(self, hass: HomeAssistant, bundle: dict) -> None:
        super().__init__(bundle["coordinator"])
        self._hass = hass
        self._api = bundle["api"]
        self._vin = bundle["vin"]
        self._attr_unique_id = f"geely_{self._vin}_sw_gclean"
        self._attr_name = "G-Clean"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._vin)},
            manufacturer="Geely",
            name=bundle.get("device_name") or f"Geely ({self._vin})",
        )

    @property
    def is_on(self) -> bool | None:
        v = _walk(self.coordinator.data or {}, (*_CLIMATE_PATH, "airBlowerActive"))
        if v is None:
            return None
        return _state_in(v, ("true", "True", True, "1", 1))

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        cs = _walk(self.coordinator.data or {}, _CLIMATE_PATH) or {}
        # Disable when AC pre-cond is active.
        if _state_in(cs.get("preClimateActive"), ("true", "True", True, "1", 1)):
            return False
        # Disable when defrost is active.
        if _state_in(cs.get("defrost"), ("true", "True", True, "1", 1)):
            return False
        return True

    async def async_turn_on(self, **_: Any) -> None:
        await self._fire("start")

    async def async_turn_off(self, **_: Any) -> None:
        await self._fire("stop")

    async def _fire(self, command: str) -> None:
        try:
            resp = await self._hass.async_add_executor_job(
                self._api.control, SERVICE_GCLEAN, SERVICE_GCLEAN_PARAMS,
                command, SERVICE_GCLEAN_DURATION,
            )
        except GeelyControlError as e:
            raise HomeAssistantError(f"Geely G-Clean: {e.message}") from e
        except Exception as e:
            _LOGGER.exception("g-clean %s failed", command)
            raise HomeAssistantError(f"Geely G-Clean failure: {e}") from e
        _LOGGER.debug("Geely g-clean %s response=%s", command, redact(resp))

        schedule_refresh(self._hass, self.coordinator, 8)


class GeelyDefrostSwitch(CoordinatorEntity, SwitchEntity):
    """Front defrost.

    AVD-verified 2026-05-01:
      ON  → RCE_2 / start / [{rce.conditioner:"2"}, {rce.level:"2"}], duration=90
      OFF → RCE_2 / stop  / [{rce.conditioner:"2"}, {rce.level:"2"}], duration=0
      State: climateStatus.defrost ("true"/"false")
    """
    _attr_has_entity_name = True
    _attr_icon = "mdi:car-defrost-front"

    def __init__(self, hass: HomeAssistant, bundle: dict) -> None:
        super().__init__(bundle["coordinator"])
        self._hass = hass
        self._api = bundle["api"]
        self._vin = bundle["vin"]
        self._attr_unique_id = f"geely_{self._vin}_sw_defrost"
        self._attr_name = "Defrost"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._vin)},
            manufacturer="Geely",
            name=bundle.get("device_name") or f"Geely ({self._vin})",
        )

    @property
    def is_on(self) -> bool | None:
        v = _walk(self.coordinator.data or {}, (*_CLIMATE_PATH, "defrost"))
        if v is None:
            return None
        return _state_in(v, ("true", "True", True, "1", 1))

    async def async_turn_on(self, **_: Any) -> None:
        await self._fire("start", 90)

    async def async_turn_off(self, **_: Any) -> None:
        await self._fire("stop", 0)

    async def _fire(self, command: str, duration: int) -> None:
        params = [
            {"key": RCE_KEY_CONDITIONER, "value": RCE_VAL_DEFROST},
            {"key": RCE_KEY_LEVEL, "value": "2"},
        ]
        try:
            resp = await self._hass.async_add_executor_job(
                self._api.control, SERVICE_CLIMATE, params, command, duration,
            )
        except GeelyControlError as e:
            raise HomeAssistantError(f"Geely Defrost: {e.message}") from e
        except Exception as e:
            _LOGGER.exception("defrost %s failed", command)
            raise HomeAssistantError(f"Geely Defrost failure: {e}") from e
        _LOGGER.debug("Geely defrost %s response=%s", command, redact(resp))

        schedule_refresh(self._hass, self.coordinator, 8)


class GeelyScheduledChargingSwitch(CoordinatorEntity, SwitchEntity):
    """Scheduled Charging on/off (charge-server bizType=6).

    Reads `bcCycleActive` from data["_scheduled_charging"]. Writes a full
    body POST that preserves the current rbcStartTime / rbcEndTime /
    rbcTarget / rbcModel - only `command` flips. Use the time entities
    to change the schedule window.

    Note: the Geely server takes ~30 seconds to reflect a scheduled-
    charging change. We use an optimistic `is_on` for 60s after a fire
    so the UI doesn't flicker through the stale server state.
    """
    _attr_has_entity_name = True
    _attr_icon = "mdi:clock-time-four"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, hass: HomeAssistant, bundle: dict) -> None:
        super().__init__(bundle["coordinator"])
        self._hass = hass
        self._api = bundle["api"]
        self._vin = bundle["vin"]
        self._attr_unique_id = f"geely_{self._vin}_sw_scheduled_charging"
        self._attr_name = "Scheduled Charging"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._vin)},
            manufacturer="Geely",
            name=bundle.get("device_name") or f"Geely ({self._vin})",
        )
        self._optimistic_on: bool | None = None
        self._optimistic_until: float = 0.0

    def _sched(self) -> dict:
        return (self.coordinator.data or {}).get("_scheduled_charging") or {}

    @property
    def is_on(self) -> bool | None:
        # Optimistic override - stays for the full 60s after a fire so
        # the slow server propagation (about 30s) doesn't flip the UI
        # back. Don't try to "drop early on server match" - we patch
        # coordinator.data ourselves after a fire, which would always
        # match and defeat the override.
        if (self._optimistic_on is not None
                and time.time() < self._optimistic_until):
            return self._optimistic_on
        sched = self._sched()
        if not sched:
            return None
        v = sched.get("bcCycleActive")
        # Server only includes bcCycleActive when the schedule is
        # active. If schedule data is present but bcCycleActive is
        # missing, the schedule is OFF, not "unknown".
        if v is None:
            return False
        return _state_in(v, ("true", "True", True, "1", 1))

    async def async_turn_on(self, **_: Any) -> None:
        await self._fire("start")

    async def async_turn_off(self, **_: Any) -> None:
        await self._fire("stop")

    async def _fire(self, command: str) -> None:
        sched = self._sched()
        start = sched.get("rbcStartTime") or "23:00"
        end = sched.get("rbcEndTime") or "07:00"
        rbc_target = sched.get("rbcTarget") or "2"
        # The GET echoes the model as `rbcModel`; the SET writes it back as
        # `chargeModel` (see api.scheduled_charging_set). "0" is the value
        # the EX5 reports.
        charge_model = sched.get("rbcModel") or "0"
        try:
            resp = await self._hass.async_add_executor_job(
                lambda: self._api.scheduled_charging_set(
                    command=command,
                    start_time=start,
                    end_time=end,
                    rbc_target=rbc_target,
                    charge_model=charge_model,
                )
            )
        except GeelyControlError as e:
            raise HomeAssistantError(f"Geely Scheduled Charging: {e.message}") from e
        except Exception as e:
            _LOGGER.exception("scheduled charging %s failed", command)
            raise HomeAssistantError(f"Geely Scheduled Charging failure: {e}") from e
        _LOGGER.debug("Geely scheduled-charging %s response=%s", command, redact(resp))

        # Set the optimistic flag so is_on returns the new state for up
        # to 60s. The Geely server takes about 30s to propagate scheduled
        # charging changes, so a short refresh window won't see the
        # update yet.
        self._optimistic_on = (command == "start")
        self._optimistic_until = time.time() + 60
        # Patch the coordinator's in-memory schedule too so peer entities
        # (the time entities) see the same state.
        data = self.coordinator.data
        if isinstance(data, dict):
            sched = data.setdefault("_scheduled_charging", {})
            sched["bcCycleActive"] = "true" if command == "start" else "false"
        self.async_write_ha_state()

        # First peek at 15s. Real confirmation usually lands at 30-35s, so
        # three tries catch it as soon as it appears without waiting out the
        # full coordinator interval. Delays are relative: 15, 35, 55.
        schedule_refresh(self._hass, self.coordinator, 15, 20, 20)


class GeelyWindowVentilationSwitch(CoordinatorEntity, SwitchEntity):
    """Cracks all four windows for fresh air. Verified ON via
    `RWS_2 target=ventilate`. OFF closes windows via `target=window` stop."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:car-door"

    def __init__(self, hass: HomeAssistant, bundle: dict) -> None:
        super().__init__(bundle["coordinator"])
        self._hass = hass
        self._api = bundle["api"]
        self._vin = bundle["vin"]
        self._attr_unique_id = f"geely_{self._vin}_sw_window_ventilation"
        self._attr_name = "Window Ventilation"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._vin)},
            manufacturer="Geely",
            name=bundle.get("device_name") or f"Geely ({self._vin})",
        )

    @property
    def is_on(self) -> bool | None:
        # Ventilation is "on" whenever any window is off its closed stop -
        # the same four fields the windows cover reads, uninverted.
        return windows_open(self.coordinator.data)

    async def async_turn_on(self, **_: Any) -> None:
        await self._fire("start", [{"key": "target", "value": "ventilate"}])

    async def async_turn_off(self, **_: Any) -> None:
        await self._fire("stop", [{"key": "target", "value": "window"}])

    async def _fire(self, command: str, params: list[dict]) -> None:
        try:
            resp = await self._hass.async_add_executor_job(
                self._api.control, SERVICE_WINDOW, params, command,
            )
        except GeelyControlError as e:
            raise HomeAssistantError(f"Geely Window Ventilation: {e.message}") from e
        except Exception as e:
            _LOGGER.exception("vent switch %s failed", command)
            raise HomeAssistantError(f"Geely Window Ventilation failure: {e}") from e
        _LOGGER.debug("Geely vent switch %s params=%s response=%s",
                      command, redact(params), redact(resp))

        schedule_refresh(self._hass, self.coordinator, 8)
