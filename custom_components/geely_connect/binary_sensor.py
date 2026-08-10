"""Binary sensors for Geely (international): door/lock/trunk/hood/seatbelt states."""
# -----------------------------------------------------------------------------
# Portions of this file - the reverse-engineered Geely protocol / field mappings
# (the parts that required protocol research) - are derived from
# nitaybz/geely-global-ha, used under the MIT License. See NOTICE.txt.
# Original framework, security hardening and transport are our own work.
# -----------------------------------------------------------------------------
from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .helpers import walk as _walk

_SAFE = ("vehicleStatus", "additionalVehicleStatus", "drivingSafetyStatus")
_CLIM = ("vehicleStatus", "additionalVehicleStatus", "climateStatus")
_EV   = ("vehicleStatus", "additionalVehicleStatus", "electricVehicleStatus")

# (key, friendly_name, path, device_class, on_when_value_in)
# Friendly names drop the redundant "open" / "unlocked" suffix - HA's
# device-class already shows the on/off labels (e.g. "Open" vs "Closed"
# for door class).
# Per-key icon overrides (some sensors look better with custom icons than
# what the device_class default provides).
_ICONS: dict[str, str] = {
    "hood_open":        "mdi:car",        # there's no proper hood icon in MDI; this is the closest neutral one
    "trunk_open":       "mdi:car-back",
    "driver_seatbelt":  "mdi:seatbelt",
    "charger_plugged_in": "mdi:ev-plug-type2",
    "tank_flap":        "mdi:gas-station",
    "steering_wheel_heating": "mdi:steering",
}


SPECS: tuple[tuple[str, str, tuple[str, ...], BinarySensorDeviceClass | None, tuple[Any, ...]], ...] = (
    # All four doors prefixed "Door" so they group together in HA's
    # alphabetical device-page list. Same for entity_id keys.
    ("door_driver",          "Door Driver",       (*_SAFE, "doorOpenStatusDriver"),         BinarySensorDeviceClass.DOOR,    ("1", 1)),
    ("door_passenger",       "Door Passenger",    (*_SAFE, "doorOpenStatusPassenger"),      BinarySensorDeviceClass.DOOR,    ("1", 1)),
    ("door_rear_left",       "Door Rear-Left",    (*_SAFE, "doorOpenStatusDriverRear"),     BinarySensorDeviceClass.DOOR,    ("1", 1)),
    ("door_rear_right",      "Door Rear-Right",   (*_SAFE, "doorOpenStatusPassengerRear"),  BinarySensorDeviceClass.DOOR,    ("1", 1)),
    ("trunk_open",           "Trunk",             (*_SAFE, "trunkOpenStatus"),              BinarySensorDeviceClass.OPENING, ("1", 1)),
    ("hood_open",            "Hood",              (*_SAFE, "engineHoodOpenStatus"),         None,                            ("1", 1)),
    ("driver_seatbelt",      "Driver Seatbelt",   (*_SAFE, "seatBeltStatusDriver"),         None,                            ("true", True)),
    # `statusOfChargerConnection` - values:
    #   0 = unplugged
    #   1 / 2 / 3 = a cable is present
    # This entity answers only "is a cable present", which is why every
    # non-zero code counts. The codes do NOT reliably say more than that: 3
    # is often called "charging", but a 41-minute DC fast charge at ~92 kW
    # held the field at 1 from plug to unplug (#10). Whether the car is
    # actually charging is sensor._is_charging - the composite that also
    # reads the DC contactor and the sign of the pack current - and that is
    # what switch.charging follows.
    ("charger_plugged_in",   "Charger Plug",      (*_EV,   "statusOfChargerConnection"),    BinarySensorDeviceClass.PLUG,    ("1", 1, "2", 2, "3", 3)),
    # Steering-wheel heat, read side. The command was captured from the
    # official app on 2026-08-10 (#4) and ships as switch.Steering Wheel Heat,
    # gated on evidence of the feature; this sensor predates it and stays
    # until an owner confirms the write path actually moves a wheel - if the
    # switch proves out, the sensor becomes the redundant one here.
    # Measured on a real car by the owner who falsified every command candidate:
    # 1 while the wheel is heating at ANY level, 2 while it is off. Inverted from
    # every other flag here, and the same 1=on / 2=off convention the seat
    # ventilation fields use.
    #
    # 0 is a THIRD state, and it is why this line has a sixth element: three
    # Starray payloads read 0. Reporting that as Off - which is what shipped in
    # v1.27.0 - told most owners their car had a heated steering wheel that
    # happened to be switched off, so 0 now reads "not fitted".
    #
    # The reason first given here was that those cars' capability catalogue did
    # not advertise a heated wheel. That was wrong, and wrong in our favour:
    # `steering_wheel_heat.enabled` could not be derived on ANY car until v1.35.1,
    # because parse() read only one of the two catalogue entries the car splits
    # its climate declaration across. No Starray raw catalogue exists to check.
    #
    # What holds it up instead is a comparison across models: the EX5 whose raw
    # catalogue DOES advertise `steel_wheel_heating` reads 2 with the wheel off,
    # while three Starrays read 0. A car with the feature reports the 1/2
    # convention; 0 is a different case.
    ("steering_wheel_heating", "Steering Wheel Heating", (*_CLIM, "steerWhlHeatingSts"), None, ("1", 1), ("0", 0)),
    # `trunkLockStatus`, beside the open/closed sensor and read by nothing until
    # now. It is the only observable signal that the Unlock Trunk button did
    # anything: on the cars in #20 the command releases the latch without the
    # gate moving, so "the indicators flashed" was all anyone had to go on.
    #
    # The polarity is not a guess. In three real payloads the field tracks
    # `centralLockingStatus` exactly - both 1 while the car was locked, both 0
    # while it was not - and that field is documented in lock.py as 1/2 locked,
    # 0 unlocked. So 0 is the unlocked code here too, which with device_class
    # LOCK is what "on" has to mean.
    ("trunk_unlocked",       "Trunk Lock",        (*_SAFE, "trunkLockStatus"),              BinarySensorDeviceClass.LOCK,    ("0", 0)),
)

# Removed (redundant with proper entities):
#   - doors_unlocked   → lock.<vin>_doors
#   - defrost_active   → switch.<vin>_defrost
#   - preclimate_active → climate.<vin>_climate (hvac_mode)
#   - charging         → switch.<vin>_charging
# These unique_ids are listed in __init__._OBSOLETE_UNIQUE_ID_PATTERNS so
# HA purges them on next reload.


# Only for a car with a tank - see propulsion.py. `tankFlapStatus` reads 2 when
# the flap is shut, matching the window/charge-lid convention elsewhere in this
# payload, so 1 is open.
HYBRID_SPECS: tuple[tuple[str, str, tuple[str, ...], BinarySensorDeviceClass | None, tuple[Any, ...]], ...] = (
    ("tank_flap",            "Fuel Flap",         (*_SAFE, "tankFlapStatus"),               BinarySensorDeviceClass.OPENING, ("1", 1)),
)




async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add_entities: AddEntitiesCallback) -> None:
    bundle = hass.data[DOMAIN][entry.entry_id]
    coordinator = bundle["coordinator"]
    vin = bundle["vin"]
    device_name = bundle.get("device_name") or f"Geely ({vin})"
    verdict = bundle.get("propulsion")
    specs = SPECS + (HYBRID_SPECS if verdict and verdict.has_tank else ())
    charges = verdict.charges if verdict else True
    entities: list[BinarySensorEntity] = [
        GeelyBinarySensor(coordinator, vin, device_name, *s) for s in specs
        # No socket, no plug sensor - see Verdict.charges.
        if charges or s[0] != "charger_plugged_in"
    ]
    # Connectivity: is the integration currently reaching the car's cloud?
    entities.append(GeelyConnectivity(coordinator, vin, device_name))
    add_entities(entities)


class GeelyConnectivity(CoordinatorEntity, BinarySensorEntity):
    """On when the last poll succeeded - a live 'can we reach the car' signal."""

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator, vin: str, device_name: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"geely_{vin}_bs_connected"
        self._attr_name = "Connected"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, vin)},
            manufacturer="Geely",
            name=device_name,
        )

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.last_update_success)

    @property
    def available(self) -> bool:
        # Always available so it can report "disconnected".
        return True


class GeelyBinarySensor(CoordinatorEntity, BinarySensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator, vin: str, device_name: str, key: str,
                 friendly_name: str, path: tuple[str, ...],
                 device_class: BinarySensorDeviceClass | None,
                 on_values: tuple[Any, ...],
                 absent_values: tuple[Any, ...] = ()) -> None:
        super().__init__(coordinator)
        self._path = path
        self._on_values = on_values
        # Values that mean "this car does not have the feature" rather than
        # "the feature is off". Without this the entity reads a confident Off on
        # hardware that is not fitted.
        self._absent_values = absent_values
        self._attr_unique_id = f"geely_{vin}_bs_{key}"
        self._attr_name = friendly_name
        if device_class is not None:
            self._attr_device_class = device_class
        icon = _ICONS.get(key)
        if icon:
            self._attr_icon = icon
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, vin)},
            manufacturer="Geely",
            name=device_name,
        )

    @property
    def is_on(self) -> bool | None:
        v = _walk(self.coordinator.data or {}, self._path)
        if v is None or v in self._absent_values:
            return None
        return v in self._on_values
