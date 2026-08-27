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
# The secondary status endpoint, where the *Active flags live.
_STATE = ("_state",)

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
    "battery_temp_maintenance": "mdi:battery-heart-variant",
}


SPECS: tuple[tuple[str, str, tuple[str, ...], BinarySensorDeviceClass | None, tuple[Any, ...]], ...] = (
    # All four doors prefixed "Door" so they group together in HA's
    # alphabetical device-page list. Same for entity_id keys.
    ("door_driver",          "Door Driver",       (*_SAFE, "doorOpenStatusDriver"),         BinarySensorDeviceClass.DOOR,    ("1", 1)),
    ("door_passenger",       "Door Passenger",    (*_SAFE, "doorOpenStatusPassenger"),      BinarySensorDeviceClass.DOOR,    ("1", 1)),
    ("door_rear_left",       "Door Rear-Left",    (*_SAFE, "doorOpenStatusDriverRear"),     BinarySensorDeviceClass.DOOR,    ("1", 1)),
    ("door_rear_right",      "Door Rear-Right",   (*_SAFE, "doorOpenStatusPassengerRear"),  BinarySensorDeviceClass.DOOR,    ("1", 1)),
    ("trunk_open",           "Trunk",             (*_SAFE, "trunkOpenStatus"),              BinarySensorDeviceClass.OPENING, ("1", 1)),
    # OPENING rather than no class, which is what the trunk beside it uses.
    # Without one, Home Assistant has no labels to show and falls back to
    # On/Off - so this one panel read "Off" in a row of things reading
    # "Closed" (#40). The state itself is unchanged: a binary sensor is always
    # on/off underneath, and the device class only decides what that is called
    # on screen, so nothing keyed on the state moves.
    ("hood_open",            "Hood",              (*_SAFE, "engineHoodOpenStatus"),         BinarySensorDeviceClass.OPENING, ("1", 1)),
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
    # Steering-wheel heat, read side. The command was captured from the official
    # app (#4) and ships as switch.Steering Wheel Heat, gated on evidence of the
    # feature; an owner has since confirmed the switch turns the wheel on, so
    # this read-only sensor is now redundant with it and kept only for the
    # dashboards that already reference it - a candidate to retire.
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
    # The four individual door locks, on exactly the footing trunkLockStatus
    # was added on. In a capture across a real lock/unlock cycle all four moved
    # with `centralLockingStatus` and with each other - 1/1/1/1 while the car
    # was locked, 0/0/0/0 while it was not - so 0 is the unlocked code here
    # too, which with device_class LOCK is what "on" has to mean.
    #
    # The aggregate lock entity says whether the car is locked; these say
    # which door is not, which is the question after a "car unlocked" alert.
    # In the same capture the tailgate command moved trunkLockStatus 1 -> 0 -> 1
    # while all four of these stayed 1, so they are genuinely per-door and not
    # four copies of one flag.
    ("door_lock_driver",     "Door Lock Driver",    (*_SAFE, "doorLockStatusDriver"),        BinarySensorDeviceClass.LOCK,    ("0", 0)),
    ("door_lock_passenger",  "Door Lock Passenger", (*_SAFE, "doorLockStatusPassenger"),     BinarySensorDeviceClass.LOCK,    ("0", 0)),
    ("door_lock_rear_left",  "Door Lock Rear Left", (*_SAFE, "doorLockStatusDriverRear"),    BinarySensorDeviceClass.LOCK,    ("0", 0)),
    ("door_lock_rear_right", "Door Lock Rear Right", (*_SAFE, "doorLockStatusPassengerRear"), BinarySensorDeviceClass.LOCK,   ("0", 0)),
    # The car's own booleans for charging and plugged-in, beside the entities
    # derived from `statusOfChargerConnection` rather than replacing them. That
    # field is the one that never reaches 3 on a DC fast charge (#10), so a
    # plain bool from the same payload is worth having next to it - and being
    # a JSON boolean there is nothing to decode and no code set to enumerate.
    ("is_charging",          "Charging (reported)", (*_EV, "isCharging"),                    BinarySensorDeviceClass.BATTERY_CHARGING, (True, "true", "True")),
    ("is_plugged_in",        "Plugged In (reported)", (*_EV, "isPluggedIn"),                 BinarySensorDeviceClass.PLUG,    (True, "true", "True")),
    # Park brake, on/off beside the mapped text sensor that names the code -
    # the same pairing `statusOfChargerConnection` already has (Charger Plug
    # here, Charger Connection there), and for the same reason: an automation
    # wants on/off, a dashboard wants a word.
    #
    # This one is the reason `off_values` exists. Every other entity above
    # treats "not an on value" as off, which cannot be said here: 3 (engaged)
    # and 9 (released) are the only codes any car has been seen sending, out of
    # a set nobody has enumerated, so a fourth code has to read unknown rather
    # than be filed as released. That was the reporter's own design (#41) and
    # it is better than the answer given first, which was to refuse the entity.
    # 0/1 ride along from the original mapping - unproven, not disproven.
    ("park_brake_engaged",   "Park Brake Engaged", (*_SAFE, "electricParkBrakeStatus"),     None,                            ("3", 3, "1", 1), (), ("9", 9, "0", 0)),
    # Battery Temperature Maintenance, the half of #4 that had been open since
    # 4 August. It is the app's "Scheduled trip -> Battery Temperature
    # Maintenance" toggle, and the read side needs no new request: the field is
    # already in the secondary status block this integration polls.
    #
    # Identified by three sources agreeing on one car (#4), rather than by the
    # flag-watching test an owner was asked to run:
    #   - the app screenshot shows the toggle ON;
    #   - `_state.btTempActive` reads 1 on that car;
    #   - the vendor's own schedule endpoint (charge-server bizType 4, read
    #     into the diagnostics report since v1.40.0) returns
    #     `btTempActive: "true"` alongside `scheduledTime` decoding to exactly
    #     the 22:30 the app displays.
    # A Starray reads 0 here, which is the off half of the pair.
    #
    # Then the entity was watched changing: the same owner switched the
    # scheduled trip off and it followed to Off. That is worth more than the
    # three snapshots above, because a field can agree with a setting once by
    # coincidence and cannot track it by coincidence.
    #
    # `btActive` is deliberately NOT this field - it reads 0/false on the car
    # whose maintenance is on, so whatever it is, it is something else.
    ("battery_temp_maintenance", "Battery Temperature Maintenance", (*_STATE, "btTempActive"), None, ("1", 1, "true", True)),
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
                 absent_values: tuple[Any, ...] = (),
                 off_values: tuple[Any, ...] = ()) -> None:
        super().__init__(coordinator)
        self._path = path
        self._on_values = on_values
        # Values that mean "this car does not have the feature" rather than
        # "the feature is off". Without this the entity reads a confident Off on
        # hardware that is not fitted.
        self._absent_values = absent_values
        # When set, the ONLY values that mean off - everything unrecognised
        # then reads unknown instead. For a field whose full code set nobody
        # has enumerated, "not on" is not the same claim as "off". Empty for
        # every entity that can say what off is, which keeps their behaviour
        # exactly as it was.
        self._off_values = off_values
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
        if v in self._on_values:
            return True
        if self._off_values:
            return False if v in self._off_values else None
        return False
