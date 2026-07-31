"""Device tracker exposing the Geely vehicle's GPS location.

Coordinate encoding: lat/lon come back as integer strings in arc-milliseconds
(= degrees × 3,600,000). Example: 118442546 → 32.9°N. Field name
`marsCoordinates` says whether they're GCJ-02 ("Mars", Chinese-system) or
WGS-84; we surface that as an attribute and don't transform either way -
HA's map can render either, but distance comparisons against home will be
off by ~50–500 m if mars-coords are reported in non-China territory.
"""
# -----------------------------------------------------------------------------
# Portions of this file — the reverse-engineered Geely protocol / field mappings
# (the parts that required protocol research) — are derived from
# nitaybz/geely-global-ha, used under the MIT License. See NOTICE.txt.
# Original framework, security hardening and transport are our own work.
# -----------------------------------------------------------------------------
from __future__ import annotations

from typing import Any

from homeassistant.components.device_tracker import TrackerEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .helpers import walk as _walk

_POS_PATH = ("vehicleStatus", "basicVehicleStatus", "position")
_ARC_MS_PER_DEGREE = 3_600_000.0




def _decode_coord(raw: Any, *, max_abs: float) -> float | None:
    """Decode a position field. Tries /3.6e6 first (verified format), falls
    back to /1e6 (sometimes seen on other markets) and finally raw degrees.
    Returns None if no interpretation lies within the valid degree range.
    """
    if raw is None or raw == "":
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    for divisor in (_ARC_MS_PER_DEGREE, 1e6, 1.0):
        candidate = v / divisor
        if abs(candidate) <= max_abs:
            return candidate
    return None


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add_entities: AddEntitiesCallback) -> None:
    bundle = hass.data[DOMAIN][entry.entry_id]
    add_entities([GeelyTracker(bundle["coordinator"], bundle["vin"], bundle.get("device_name"))])


class GeelyTracker(CoordinatorEntity, TrackerEntity):
    _attr_has_entity_name = True
    # Don't put the tracker in the device's "Diagnostic" group.
    _attr_entity_category = None

    def __init__(self, coordinator, vin: str, device_name: str | None) -> None:
        super().__init__(coordinator)
        self._vin = vin
        self._attr_unique_id = f"geely_{vin}_tracker"
        self._attr_name = "Location"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, vin)},
            manufacturer="Geely",
            model=None,
            name=device_name or f"Geely ({vin})",
        )

    @property
    def latitude(self) -> float | None:
        pos = _walk(self.coordinator.data or {}, _POS_PATH)
        if isinstance(pos, dict):
            return _decode_coord(pos.get("latitude"), max_abs=90.0)
        return None

    @property
    def longitude(self) -> float | None:
        pos = _walk(self.coordinator.data or {}, _POS_PATH)
        if isinstance(pos, dict):
            return _decode_coord(pos.get("longitude"), max_abs=180.0)
        return None

    @property
    def source_type(self) -> str:
        return "gps"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        pos = _walk(self.coordinator.data or {}, _POS_PATH)
        if not isinstance(pos, dict):
            return {}
        return {
            "altitude_m":          pos.get("altitude"),
            "trusted":             pos.get("posCanBeTrusted"),
            "mars_coords":         pos.get("marsCoordinates"),
            "raw_latitude":        pos.get("latitude"),
            "raw_longitude":       pos.get("longitude"),
        }
