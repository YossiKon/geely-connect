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
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    UnitOfElectricPotential,
    UnitOfLength,
    UnitOfSpeed,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as _dt_util

from .const import (
    CONF_PRESSURE_UNIT,
    DEFAULT_PRESSURE_UNIT,
    DOMAIN,
    PRESSURE_FACTORS,
)

# Keys that represent a tire pressure (raw value is kPa; converted per user unit).
_TIRE_KEYS = {"tire_pressure_fl", "tire_pressure_fr", "tire_pressure_rl", "tire_pressure_rr"}

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
}


def _walk(d: Any, path: tuple[str, ...]) -> Any:
    cur = d
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
        if cur is None:
            return None
    return cur


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

# Internal keys we merge into the status dict ourselves (not from the car).
_SKIP_TOP_KEYS: set[str] = set()


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
    pressure_unit = entry.data.get(CONF_PRESSURE_UNIT, DEFAULT_PRESSURE_UNIT)

    # 1) Curated, nicely-named sensors.
    add_entities(GeelySensor(coordinator, vin, device_name, *spec,
                             pressure_unit=pressure_unit) for spec in SENSOR_SPECS)

    # Computed / meta sensors (our own additions).
    add_entities([
        GeelyEfficiencySensor(coordinator, vin, device_name),
        GeelyLastUpdatedSensor(coordinator, vin, device_name),
    ])

    # 2) Dynamic full exposure: one diagnostic sensor for EVERY field the server
    #    returns that isn't already covered above. New fields that appear on a
    #    later poll are added automatically.
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
        # Tire pressures: display in the unit the user picked at setup.
        if key in _TIRE_KEYS:
            unit = pressure_unit
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
        # Convert tire pressure from the raw kPa reading to the chosen unit.
        if self._key in _TIRE_KEYS and isinstance(val, (int, float)):
            factor = PRESSURE_FACTORS.get(self._pressure_unit, 1.0)
            return round(val * factor, 1)
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
