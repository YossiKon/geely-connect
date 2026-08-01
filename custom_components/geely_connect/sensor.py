"""Sensors for Geely (international).

Reads from coordinator.data, which is the `data` block of
GET /remote-control/vehicle/status/{VIN}. Live keys are nested under
`vehicleStatus.{basicVehicleStatus|additionalVehicleStatus.{...}}`.
The server sends every numeric value as a string - we coerce here.
"""
# -----------------------------------------------------------------------------
# Portions of this file — the reverse-engineered Geely protocol / field mappings
# (the parts that required protocol research) — are derived from
# nitaybz/geely-global-ha, used under the MIT License. See NOTICE.txt.
# Original framework, security hardening and transport are our own work.
# -----------------------------------------------------------------------------
from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    UnitOfElectricPotential,
    UnitOfLength,
    UnitOfPressure,
    UnitOfSpeed,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as _dt_util
from homeassistant.util.unit_conversion import DistanceConverter

from .const import (
    CONF_FULL_EXPOSURE,
    CONF_PRESSURE_UNIT,
    DEFAULT_PRESSURE_UNIT,
    DOMAIN,
)
from .helpers import walk as _walk

# Keys that represent a tire pressure (raw value is kPa; converted per user unit).
_TIRE_KEYS = {"tire_pressure_fl", "tire_pressure_fr", "tire_pressure_rl", "tire_pressure_rr"}

# Our setup-time unit codes -> the constants Home Assistant converts between.
# The strings already match, but going through UnitOfPressure keeps us honest
# if either side ever renames one.
_PRESSURE_UNIT_TO_HA: dict[str, str] = {
    "psi": UnitOfPressure.PSI,
    "bar": UnitOfPressure.BAR,
    "kPa": UnitOfPressure.KPA,
}

# kPa (what the car sends) -> the unit picked at setup, and how many decimals
# that unit deserves.
_PRESSURE_FROM_KPA: dict[str, tuple[float, int]] = {
    "psi": (0.1450377, 1),
    "bar": (0.01, 2),
    "kPa": (1.0, 0),
}

# The four corners, in the order they sit on the car.
_TIRE_CORNERS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("front_left",  "Tire Front-Left",  ("tyreStatusDriver",)),
    ("front_right", "Tire Front-Right", ("tyreStatusPassenger",)),
    ("rear_left",   "Tire Rear-Left",   ("tyreStatusDriverRear",)),
    ("rear_right",  "Tire Rear-Right",  ("tyreStatusPassengerRear",)),
)

# Long-term statistics: measurement for live values, total_increasing for odo.
# Anything not listed here records no statistics at all, so a sensor that holds
# a number belongs in one of these sets. The enum-valued ones (engine_state,
# park_brake, charger_connected) correctly have neither.
_MEASUREMENT_KEYS = {
    "battery", "range", "interior_temp", "exterior_temp", "speed",
    "12v_battery", "12v_voltage", "avg_consumption", "avg_speed",
    "tire_pressure_fl", "tire_pressure_fr", "tire_pressure_rl", "tire_pressure_rr",
    "time_to_full_min",
    # Both count DOWN towards the next service, so they are measurements -
    # total_increasing would read every service reset as a counter rollover.
    "days_to_service", "distance_to_service",
    # Resettable by the driver. Measurement rather than a total: without a
    # last_reset HA would fold the reset into the sum as a negative delta, and
    # total_mileage below already provides the cumulative distance statistic.
    "trip_meter",
}
_TOTAL_INCREASING_KEYS = {"total_mileage"}

# Shorthand for nested status branches
_BASIC  = ("vehicleStatus", "basicVehicleStatus")
_ADD    = ("vehicleStatus", "additionalVehicleStatus")
_MAINT  = (*_ADD, "maintenanceStatus")
_EV     = (*_ADD, "electricVehicleStatus")
_CLIM   = (*_ADD, "climateStatus")
_SAFE   = (*_ADD, "drivingSafetyStatus")
_RUN    = (*_ADD, "runningStatus")

# Value mappers for sensors that should display a readable label instead
# of the raw numeric/string code from the API.
_CHARGER_CONNECTION_MAP = {
    "0": "Disconnected", 0: "Disconnected",
    "1": "Plugged in",   1: "Plugged in",
    "2": "Plugged in",   2: "Plugged in",
    "3": "Charging",     3: "Charging",
}

_PARK_BRAKE_MAP = {
    "0": "Released", 0: "Released",
    "1": "Engaged",  1: "Engaged",
}

_ENGINE_STATE_MAP = {
    "engine_off":     "Off",
    "engine_running": "Running",
    "running":        "Running",
    "off":            "Off",
    "1":              "Running", 1: "Running",
    "0":              "Off",     0: "Off",
}

# Icon overrides per sensor key (entries with explicit device_class get
# auto-icons from HA, but a few sensors look better with a custom icon).
_SENSOR_ICONS: dict[str, str] = {
    "time_to_full_min":  "mdi:battery-charging",
    "charger_connected": "mdi:ev-plug-type2",
    "12v_battery":       "mdi:car-battery",
    "12v_voltage":       "mdi:car-battery",
    "avg_consumption":   "mdi:lightning-bolt",
    "trip_meter":        "mdi:map-marker-distance",
    "avg_speed":         "mdi:speedometer-medium",
    "engine_state":      "mdi:engine",
    "park_brake":        "mdi:car-brake-parking",
    "tire_pressure_fl":  "mdi:car-tire-alert",
    "tire_pressure_fr":  "mdi:car-tire-alert",
    "tire_pressure_rl":  "mdi:car-tire-alert",
    "tire_pressure_rr":  "mdi:car-tire-alert",
    "days_to_service":     "mdi:calendar-clock",
    "distance_to_service": "mdi:road-variant",
}

# (key, friendly_name, dotted-path, unit, device_class, value_type, value_map?)
SENSOR_SPECS: tuple[tuple, ...] = (
    ("battery",             "Battery",              (*_EV,    "chargeLevel"),                          PERCENTAGE,                       SensorDeviceClass.BATTERY,     "float", None),
    ("range",               "Electric Range",       (*_EV,    "distanceToEmptyOnBatteryOnly"),         UnitOfLength.KILOMETERS,          SensorDeviceClass.DISTANCE,    "int",   None),
    ("total_mileage",       "Total Mileage",        (*_MAINT, "odometer"),                             UnitOfLength.KILOMETERS,          SensorDeviceClass.DISTANCE,    "float", None),
    ("interior_temp",       "Interior Temperature", (*_CLIM,  "interiorTemp"),                         UnitOfTemperature.CELSIUS,        SensorDeviceClass.TEMPERATURE, "float", None),
    ("exterior_temp",       "Exterior Temperature", (*_CLIM,  "exteriorTemp"),                         UnitOfTemperature.CELSIUS,        SensorDeviceClass.TEMPERATURE, "float", None),
    ("speed",               "Speed",                (*_BASIC, "speed"),                                UnitOfSpeed.KILOMETERS_PER_HOUR,  SensorDeviceClass.SPEED,       "float", None),
    ("engine_state",        "Engine State",         (*_BASIC, "engineStatus"),                         None,                             None,                          "map",   _ENGINE_STATE_MAP),
    ("park_brake",          "Park Brake",           (*_SAFE,  "electricParkBrakeStatus"),              None,                             None,                          "map",   _PARK_BRAKE_MAP),
    ("charger_connected",   "Charger Connection",   (*_EV,    "statusOfChargerConnection"),            None,                             None,                          "map",   _CHARGER_CONNECTION_MAP),
    ("time_to_full_min",    "Time To Full Charge",  (*_EV,    "timeToFullyCharged"),                   "min",                            None,                          "int",   None),
    ("12v_battery",         "12V Battery",          (*_MAINT, "mainBatteryStatus", "chargeLevel"),     PERCENTAGE,                       None,                          "float", None),
    ("12v_voltage",         "12V Voltage",          (*_MAINT, "mainBatteryStatus", "voltage"),         UnitOfElectricPotential.VOLT,     SensorDeviceClass.VOLTAGE,     "float", None),
    ("avg_consumption",     "Average Consumption",  (*_EV,    "averPowerConsumption"),                 "kWh/100km",                      None,                          "float", None),
    ("trip_meter",          "Trip Meter",           (*_RUN,   "tripMeter1"),                           UnitOfLength.KILOMETERS,          SensorDeviceClass.DISTANCE,    "float", None),
    ("avg_speed",           "Average Speed",        (*_RUN,   "avgSpeed"),                             UnitOfSpeed.KILOMETERS_PER_HOUR,  SensorDeviceClass.SPEED,       "float", None),
    ("tire_pressure_fl",    "Tire Pressure FL",     (*_MAINT, "tyreStatusDriver"),                     "kPa",                            SensorDeviceClass.PRESSURE,    "float", None),
    ("tire_pressure_fr",    "Tire Pressure FR",     (*_MAINT, "tyreStatusPassenger"),                  "kPa",                            SensorDeviceClass.PRESSURE,    "float", None),
    ("tire_pressure_rl",    "Tire Pressure RL",     (*_MAINT, "tyreStatusDriverRear"),                 "kPa",                            SensorDeviceClass.PRESSURE,    "float", None),
    ("tire_pressure_rr",    "Tire Pressure RR",     (*_MAINT, "tyreStatusPassengerRear"),              "kPa",                            SensorDeviceClass.PRESSURE,    "float", None),
    ("days_to_service",     "Days To Service",      (*_MAINT, "daysToService"),                        "d",                              None,                          "int",   None),
    ("distance_to_service", "Distance To Service",  (*_MAINT, "distanceToService"),                    UnitOfLength.KILOMETERS,          SensorDeviceClass.DISTANCE,    "int",   None),
)

# Sensors marked diagnostic appear in HA's collapsed "Diagnostic" section
# on the device page rather than the main entity list.
_DIAGNOSTIC_KEYS: set[str] = {
    "park_brake",
    "12v_battery", "12v_voltage",
    "avg_consumption", "trip_meter", "avg_speed",
    "tire_pressure_fl", "tire_pressure_fr",
    "tire_pressure_rl", "tire_pressure_rr",
    "days_to_service", "distance_to_service",
    # Rarely interesting on their own: engine state duplicates what the climate
    # and charging entities already show, and time-to-full is only meaningful
    # while charging.
    "engine_state", "time_to_full_min",
}




def _coerce(v: Any, kind: str, value_map: dict | None = None) -> Any:
    if v is None or v == "":
        return None
    try:
        if kind == "int":
            return int(float(v))
        if kind == "float":
            return float(v)
        if kind == "map" and value_map is not None:
            return value_map.get(v, value_map.get(str(v), v))
    except (TypeError, ValueError):
        return None
    return v


# Paths already covered by a curated sensor above — skip them in the dynamic
# full-exposure pass so we don't create duplicates.
_CURATED_PATHS: set[str] = {".".join(spec[2]) for spec in SENSOR_SPECS}


_MAX_FLATTEN_DEPTH = 12   # deeper than any real status payload nests
_MAX_LIST_ITEMS = 64      # indexed list entries kept per list


def _flatten(obj: Any, prefix: str = "", depth: int = 0) -> dict[str, Any]:
    """Flatten a nested status dict into {dotted.path: scalar}. Lists are
    indexed. Only scalar leaves (str/int/float/bool) are kept.

    Bounded on purpose: the payload is server JSON, and a few KB of nesting is
    enough to blow CPython's recursion limit. Since this runs during platform
    setup, an unbounded recurse would take every sensor down with it."""
    out: dict[str, Any] = {}
    if depth > _MAX_FLATTEN_DEPTH:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            out.update(_flatten(v, key, depth + 1))
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj[:_MAX_LIST_ITEMS]):
            out.update(_flatten(v, f"{prefix}.{i}", depth + 1))
    else:
        if obj is not None and obj != "":
            out[prefix] = obj
    return out


_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _prettify(path: str) -> str:
    """Human label from a dotted path: use the last 1-2 segments, spaced."""
    segments = [p for p in path.split(".") if not p.isdigit()]
    tail = segments[-1] if segments else path
    # camelCase / snake_case -> spaced Title-ish, keep it readable but raw.
    spaced = _CAMEL_BOUNDARY.sub(" ", tail).replace("_", " ")
    return spaced[:1].upper() + spaced[1:]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add_entities: AddEntitiesCallback) -> None:
    bundle = hass.data[DOMAIN][entry.entry_id]
    coordinator = bundle["coordinator"]
    vin = bundle["vin"]
    device_name = bundle.get("device_name") or f"Geely ({vin})"
    pressure_unit = (entry.options.get(CONF_PRESSURE_UNIT)
                     or entry.data.get(CONF_PRESSURE_UNIT, DEFAULT_PRESSURE_UNIT))

    # 1) Curated, nicely-named sensors.
    add_entities(GeelySensor(coordinator, vin, device_name, *spec,
                             pressure_unit=pressure_unit) for spec in SENSOR_SPECS)

    # Computed / meta sensors (our own additions).
    add_entities([
        GeelyEfficiencySensor(coordinator, vin, device_name),
        GeelyLastUpdatedSensor(coordinator, vin, device_name),
        GeelyChargeCompleteSensor(coordinator, vin, device_name),
        GeelyFullRangeSensor(coordinator, vin, device_name),
        GeelyLastTripSensor(coordinator, vin, device_name),
        GeelyTripInProgressSensor(coordinator, vin, device_name),
        *(GeelyTireSensor(coordinator, vin, device_name, key, name, path,
                          pressure_unit)
          for key, name, path in _TIRE_CORNERS),
    ])

    # 2) Full exposure: one diagnostic sensor for EVERY field the server
    #    returns that isn't already covered above. Off unless asked for - on an
    #    EX5 it is around 180 entities, which buries the useful ones on the
    #    device page even though they are all disabled. Turn it on under
    #    Configure if you are hunting for a field we do not expose yet.
    if not (entry.options.get(CONF_FULL_EXPOSURE)
            or entry.data.get(CONF_FULL_EXPOSURE, False)):
        return

    known: set[str] = set()

    def _discover_and_add() -> None:
        data = coordinator.data or {}
        flat = _flatten(data)
        new_entities = []
        for path, _val in flat.items():
            if path in known or path in _CURATED_PATHS:
                continue
            known.add(path)
            new_entities.append(GeelyRawSensor(coordinator, vin, device_name, path))
        if new_entities:
            add_entities(new_entities)

    _discover_and_add()
    entry.async_on_unload(coordinator.async_add_listener(_discover_and_add))


class GeelySensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator, vin: str, device_name: str,
                 key: str, friendly_name: str, path: tuple[str, ...],
                 unit: str | None, device_class: SensorDeviceClass | None,
                 kind: str, value_map: dict | None = None,
                 pressure_unit: str = DEFAULT_PRESSURE_UNIT) -> None:
        super().__init__(coordinator)
        self._key = key
        self._path = path
        self._kind = kind
        self._value_map = value_map
        self._pressure_unit = pressure_unit
        self._attr_unique_id = f"geely_{vin}_{key}"
        self._attr_name = friendly_name
        # Tire pressures: report the raw kPa the car sends and let Home
        # Assistant convert. Setting the chosen unit as the NATIVE unit does
        # not work - for a device class Home Assistant knows how to convert,
        # it picks the display unit from suggested_unit_of_measurement and
        # falls back to the unit system's preference, so a metric install
        # re-converted our psi figure straight back to kPa.
        if key in _TIRE_KEYS:
            unit = UnitOfPressure.KPA
            self._attr_suggested_unit_of_measurement = _PRESSURE_UNIT_TO_HA.get(
                pressure_unit, UnitOfPressure.KPA
            )
        if unit is not None:
            self._attr_native_unit_of_measurement = unit
        if device_class is not None:
            self._attr_device_class = device_class
        if key in _MEASUREMENT_KEYS:
            self._attr_state_class = SensorStateClass.MEASUREMENT
        elif key in _TOTAL_INCREASING_KEYS:
            self._attr_state_class = SensorStateClass.TOTAL_INCREASING
        icon = _SENSOR_ICONS.get(key)
        if icon:
            self._attr_icon = icon
        if key in _DIAGNOSTIC_KEYS:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, vin)},
            manufacturer="Geely",
            name=device_name,
        )

    @property
    def native_value(self) -> Any:
        v = _walk(self.coordinator.data or {}, self._path)
        val = _coerce(v, self._kind, self._value_map)
        # Tire pressure stays in its native kPa here; Home Assistant converts
        # it to the unit chosen at setup. Converting it ourselves as well
        # would apply the factor twice.
        return val


class GeelyRawSensor(CoordinatorEntity, SensorEntity):
    """Auto-generated diagnostic sensor for a single raw status field.

    Created dynamically for every field the server returns that isn't already
    exposed by a curated sensor, so nothing is hidden. Numeric-looking values
    are shown as numbers; everything else as text.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False  # available but off by default

    def __init__(self, coordinator, vin: str, device_name: str, path: str) -> None:
        super().__init__(coordinator)
        self._path = path
        self._attr_unique_id = f"geely_{vin}_raw_{path}"
        self._attr_name = _prettify(path)
        self._attr_icon = "mdi:information-outline"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, vin)},
            manufacturer="Geely",
            name=device_name,
        )

    @property
    def native_value(self) -> Any:
        cur: Any = self.coordinator.data or {}
        for seg in self._path.split("."):
            if isinstance(cur, list):
                try:
                    cur = cur[int(seg)]
                except (ValueError, IndexError):
                    return None
            elif isinstance(cur, dict):
                cur = cur.get(seg)
            else:
                return None
            if cur is None:
                return None
        # Coerce numeric strings to numbers for nicer history graphs.
        if isinstance(cur, str):
            s = cur.strip()
            try:
                return int(s)
            except ValueError:
                try:
                    return float(s)
                except ValueError:
                    return cur[:255]
        if isinstance(cur, (int, float, bool)):
            return cur
        return str(cur)[:255]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"field_path": self._path}


# ---------------------------------------------------------------------------
# Computed / meta sensors (our own additions — not raw server fields)
# ---------------------------------------------------------------------------


class GeelyEfficiencySensor(CoordinatorEntity, SensorEntity):
    """Driving efficiency in km per kWh, derived from average consumption
    (server reports kWh/100km)."""

    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = "km/kWh"
    _attr_icon = "mdi:leaf"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, vin: str, device_name: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"geely_{vin}_efficiency"
        self._attr_name = "Efficiency"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, vin)}, manufacturer="Geely", name=device_name)

    @property
    def native_value(self):
        v = _walk(self.coordinator.data or {}, (*_EV, "averPowerConsumption"))
        try:
            c = float(v)
        except (TypeError, ValueError):
            return None
        if c <= 0:
            return None
        return round(100.0 / c, 2)


class GeelyLastUpdatedSensor(CoordinatorEntity, SensorEntity):
    """Timestamp of the last successful poll — HA shows it as a relative age."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:clock-check-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, vin: str, device_name: str) -> None:
        super().__init__(coordinator)
        self._ts = None
        self._attr_unique_id = f"geely_{vin}_last_updated"
        self._attr_name = "Last Updated"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, vin)}, manufacturer="Geely", name=device_name)

    def _stamp(self) -> None:
        if self.coordinator.last_update_success:
            self._ts = _dt_util.utcnow()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._stamp()

    def _handle_coordinator_update(self) -> None:
        self._stamp()
        super()._handle_coordinator_update()

    @property
    def native_value(self):
        return self._ts


class GeelyChargeCompleteSensor(CoordinatorEntity, SensorEntity):
    """When charging is expected to finish, as a timestamp.

    The server reports minutes remaining, which the UI renders as a bare
    number; a timestamp reads as "in 2 hours" and can drive a notification.
    Unknown while the car is not drawing current."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:battery-clock"

    def __init__(self, coordinator, vin: str, device_name: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"geely_{vin}_charge_complete"
        self._attr_name = "Charge Complete"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, vin)}, manufacturer="Geely", name=device_name)

    @property
    def native_value(self):
        status = _walk(self.coordinator.data or {}, (*_EV, "statusOfChargerConnection"))
        if str(status) != "3":          # 3 = actively drawing current
            return None
        try:
            minutes = float(_walk(self.coordinator.data or {}, (*_EV, "timeToFullyCharged")))
        except (TypeError, ValueError):
            return None
        if minutes <= 0:
            return None
        # Rounded to the minute so a stable estimate does not rewrite itself
        # every poll with a few seconds of drift.
        done = _dt_util.utcnow() + timedelta(minutes=minutes)
        return done.replace(second=0, microsecond=0)


class GeelyFullRangeSensor(CoordinatorEntity, SensorEntity):
    """Range the car would show on a full battery, at the current efficiency.

    Remaining range on its own says nothing about whether the pack is ageing or
    the weather is costing you; extrapolated to 100% it is comparable week to
    week. Unknown below 10% charge, where the estimate is mostly noise."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.DISTANCE
    _attr_native_unit_of_measurement = UnitOfLength.KILOMETERS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:map-marker-path"

    def __init__(self, coordinator, vin: str, device_name: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"geely_{vin}_full_range"
        self._attr_name = "Range At Full Charge"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, vin)}, manufacturer="Geely", name=device_name)

    @property
    def native_value(self):
        data = self.coordinator.data or {}
        try:
            charge = float(_walk(data, (*_EV, "chargeLevel")))
            rng = float(_walk(data, (*_EV, "distanceToEmptyOnBatteryOnly")))
        except (TypeError, ValueError):
            return None
        if charge < 10 or rng <= 0:
            return None
        return round(rng * 100.0 / charge)


class GeelyTireSensor(CoordinatorEntity, SensorEntity):
    """A tire pressure already converted to the unit picked at setup.

    The four "Tire Pressure FL/FR/RL/RR" sensors carry
    device_class: pressure, which means Home Assistant owns their display unit:
    it takes suggested_unit_of_measurement at first registration and the unit
    system's preference after that, so an install created before the unit was
    chosen keeps showing kPa no matter what the integration reports.

    These four sidestep that by having NO device_class. With no converter in
    play Home Assistant shows exactly the number and unit given here, so the
    setup choice is honoured on a fresh install and on an existing one alike,
    and it follows a later change under Configure because the entry reloads.

    The originals are left in place - anything already pointing at them keeps
    working."""

    _attr_has_entity_name = True
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:car-tire-alert"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, vin: str, device_name: str, key: str,
                 friendly_name: str, field: tuple[str, ...],
                 pressure_unit: str = DEFAULT_PRESSURE_UNIT) -> None:
        super().__init__(coordinator)
        self._path = (*_MAINT, *field)
        self._factor, self._digits = _PRESSURE_FROM_KPA.get(
            pressure_unit, _PRESSURE_FROM_KPA["kPa"])
        self._attr_unique_id = f"geely_{vin}_tire_{key}"
        self._attr_name = friendly_name
        self._attr_native_unit_of_measurement = _PRESSURE_UNIT_TO_HA.get(
            pressure_unit, UnitOfPressure.KPA)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, vin)}, manufacturer="Geely", name=device_name)

    def _kpa(self) -> float | None:
        try:
            kpa = float(_walk(self.coordinator.data or {}, self._path))
        except (TypeError, ValueError):
            return None
        return kpa if kpa > 0 else None   # a sleeping TPMS reports 0

    @property
    def native_value(self):
        kpa = self._kpa()
        return None if kpa is None else round(kpa * self._factor, self._digits)

    @property
    def extra_state_attributes(self):
        """Every unit, always - so a card or template can pick one without
        depending on what was chosen at setup."""
        kpa = self._kpa()
        if kpa is None:
            return None
        return {
            unit: round(kpa * factor, digits)
            for unit, (factor, digits) in _PRESSURE_FROM_KPA.items()
        }


def _engine_running(data: dict) -> bool | None:
    """True while the car is on, None when it has not said.

    The server reports this field in several shapes across firmware, so it
    goes through the same map the Engine State sensor uses."""
    raw = _walk(data or {}, (*_BASIC, "engineStatus"))
    if raw is None:
        return None
    mapped = _ENGINE_STATE_MAP.get(raw)
    if mapped is None and isinstance(raw, str):
        mapped = _ENGINE_STATE_MAP.get(raw.strip().lower())
    if mapped is None:
        return None
    return mapped == "Running"


def _odometer(data: dict) -> float | None:
    try:
        km = float(_walk(data or {}, (*_MAINT, "odometer")))
    except (TypeError, ValueError):
        return None
    return km if km > 0 else None


class _GeelyTripBase(CoordinatorEntity, RestoreSensor):
    """Shared bookkeeping for the two trip sensors.

    The car's own tripMeter1 is the trip meter A on the dash - the driver
    resets it by hand and it has nothing to do with a single journey, which
    is why it can read 0 while you are driving. These two work the distance
    out from the odometer instead: note where it stood when the engine came
    on, and read it again when the engine goes off.

    State is restored across restarts, so a Home Assistant reboot mid-journey
    does not lose the trip that was in progress."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.DISTANCE
    _attr_native_unit_of_measurement = UnitOfLength.KILOMETERS

    def __init__(self, coordinator, vin: str, device_name: str) -> None:
        super().__init__(coordinator)
        self._start_km: float | None = None
        self._last_trip: float | None = None
        self._was_running: bool | None = None
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, vin)}, manufacturer="Geely", name=device_name)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            attrs = last.attributes or {}
            self._start_km = attrs.get("trip_start_odometer")
            self._was_running = attrs.get("engine_was_running")

        # Read the distance back through the sensor channel, not from
        # `last.state`. A DISTANCE sensor's state is written in the display
        # unit, so on a miles install restoring it as kilometres shrank the
        # trip by a factor of 1.609 on every restart, compounding.
        self._last_trip = None
        if (stored := await self.async_get_last_sensor_data()) is not None:
            value, unit = stored.native_value, stored.native_unit_of_measurement
            if value is not None:
                try:
                    km = float(value)
                except (TypeError, ValueError):
                    km = None
                else:
                    if unit and unit != UnitOfLength.KILOMETERS:
                        km = DistanceConverter.convert(
                            km, unit, UnitOfLength.KILOMETERS
                        )
                self._last_trip = km

    def _advance(self) -> None:
        """Fold the newest poll into the trip bookkeeping.

        Called from both sensors, so whichever updates first does the work
        and the other sees the same numbers."""
        data = self.coordinator.data or {}
        running = _engine_running(data)
        km = _odometer(data)
        if running is None or km is None:
            return                       # nothing to learn from this poll
        if running and not self._was_running:
            self._start_km = km          # journey started
        elif not running and self._was_running and self._start_km is not None:
            covered = km - self._start_km
            # A negative or absurd delta means the odometer was misreported,
            # not that the car drove backwards - keep the previous trip.
            if 0 <= covered < 2000:
                self._last_trip = round(covered, 1)
            self._start_km = None        # journey over
        self._was_running = running


class GeelyLastTripSensor(_GeelyTripBase):
    """How far the last completed journey went.

    Unknown until the car has been driven once with the integration running -
    there is no way to learn a journey that finished before we were watching."""

    _attr_icon = "mdi:map-marker-path"
    # Without this Home Assistant records no long-term statistics at all, so a
    # graph of trip lengths would empty out at the recorder purge window.
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, vin: str, device_name: str) -> None:
        super().__init__(coordinator, vin, device_name)
        self._attr_unique_id = f"geely_{vin}_last_trip"
        self._attr_name = "Last Trip"

    @property
    def native_value(self):
        self._advance()
        return self._last_trip

    @property
    def extra_state_attributes(self):
        return {
            "trip_start_odometer": self._start_km,
            "engine_was_running": self._was_running,
        }


class GeelyTripInProgressSensor(_GeelyTripBase):
    """How far the current journey has gone so far. Zero when parked."""

    _attr_icon = "mdi:car-arrow-right"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, vin: str, device_name: str) -> None:
        super().__init__(coordinator, vin, device_name)
        self._attr_unique_id = f"geely_{vin}_trip_in_progress"
        self._attr_name = "Trip In Progress"

    @property
    def native_value(self):
        self._advance()
        if not self._was_running or self._start_km is None:
            return 0.0
        km = _odometer(self.coordinator.data or {})
        if km is None:
            return None
        return round(max(0.0, km - self._start_km), 1)

    @property
    def extra_state_attributes(self):
        return {
            "trip_start_odometer": self._start_km,
            "engine_was_running": self._was_running,
        }
