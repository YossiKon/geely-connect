"""Binary sensors for Geely (international): door/lock/trunk/hood/seatbelt states."""
# -----------------------------------------------------------------------------
# Portions of this file — the reverse-engineered Geely protocol / field mappings
# (the parts that required protocol research) — are derived from
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
    #   1 / 2 = plugged but idle
    #   3 = actively drawing current
    # We only expose "plugged in" here (1/2/3). Active-charging state is
    # surfaced by switch.charging.
    ("charger_plugged_in",   "Charger Plug",      (*_EV,   "statusOfChargerConnection"),    BinarySensorDeviceClass.PLUG,    ("1", 1, "2", 2, "3", 3)),
)
# Removed (redundant with proper entities):
#   - doors_unlocked   → lock.<vin>_doors
#   - defrost_active   → switch.<vin>_defrost
#   - preclimate_active → climate.<vin>_climate (hvac_mode)
#   - charging         → switch.<vin>_charging
# These unique_ids are listed in __init__._OBSOLETE_UNIQUE_ID_PATTERNS so
# HA purges them on next reload.


def _walk(d: Any, path: tuple[str, ...]) -> Any:
    cur = d
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
        if cur is None:
            return None
    return cur


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add_entities: AddEntitiesCallback) -> None:
    bundle = hass.data[DOMAIN][entry.entry_id]
    coordinator = bundle["coordinator"]
    vin = bundle["vin"]
    device_name = bundle.get("device_name") or f"Geely ({vin})"
    entities: list[BinarySensorEntity] = [
        GeelyBinarySensor(coordinator, vin, device_name, *s) for s in SPECS
    ]
    # Connectivity: is the integration currently reaching the car's cloud?
    entities.append(GeelyConnectivity(coordinator, vin, device_name))
    add_entities(entities)


class GeelyConnectivity(CoordinatorEntity, BinarySensorEntity):
    """On when the last poll succeeded — a live 'can we reach the car' signal."""

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
                 on_values: tuple[Any, ...]) -> None:
        super().__init__(coordinator)
        self._path = path
        self._on_values = on_values
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
        if v is None:
            return None
        return v in self._on_values
