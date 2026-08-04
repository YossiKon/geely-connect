"""What the car is powered by, and therefore which entities it should have.

An EV has no fuel level and a petrol car has no charge level, so a single
entity list serves neither well: a BEV owner given fuel entities sees a row of
`unavailable` tiles, and a PHEV owner given only the EV ones cannot see half
the car.

The account's vehicle list already declares this in `powerType` - captured into
entry data since the first version and, until now, read by nothing. That is the
field we trust, because it is the manufacturer's own answer rather than our
inference. Two things stop it being the only input:

- Plain `混动` means "hybrid" and does not distinguish a plug-in from a
  non-plug hybrid (`插电混动`). Only charge telemetry separates those, so the
  plug is observed rather than declared.
- It is a localised string, and it is empty on entries created before the
  refresh path carried it. An empty field must not be allowed to decide.

So: the declared value decides when we recognise it, telemetry decides when we
do not, and a disagreement is logged. That log is the only way the mapping gets
verified against models nobody here can dump - `configuration.propulsionType`
and `fuelType` ride along in the verdict for the same reason.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .helpers import walk

_LOGGER = logging.getLogger(__name__)

_ADD = ("vehicleStatus", "additionalVehicleStatus")
_EV = (*_ADD, "electricVehicleStatus")
_RUN = (*_ADD, "runningStatus")
_FUEL = (*_ADD, "fuelStatus")
_CONFIG = ("vehicleStatus", "configuration")


class Propulsion(StrEnum):
    """What moves the car."""

    ELECTRIC = "electric"
    HYBRID = "hybrid"
    FUEL = "fuel"
    UNKNOWN = "unknown"


# Short acronyms are matched as whole tokens, never as substrings: "ev" occurs
# inside plenty of ordinary words and "ice" inside "service", so a substring
# rule here would classify a car by accident.
_EXACT: dict[str, Propulsion] = {
    "phev": Propulsion.HYBRID,
    "hev": Propulsion.HYBRID,
    "ev": Propulsion.ELECTRIC,
    "bev": Propulsion.ELECTRIC,
    "ice": Propulsion.FUEL,
}

# Descriptive wordings, checked as substrings and longest-first within a kind so
# a plug-in hybrid is not read as a pure EV by matching "电" too eagerly.
# Chinese leads because that is what the APAC cloud returns for an AU account.
_DECLARED: tuple[tuple[str, Propulsion], ...] = (
    ("插电式混合动力", Propulsion.HYBRID),
    ("插电混动", Propulsion.HYBRID),
    ("插电", Propulsion.HYBRID),
    ("混合动力", Propulsion.HYBRID),
    ("混动", Propulsion.HYBRID),
    ("plug-in hybrid", Propulsion.HYBRID),
    ("plug-in", Propulsion.HYBRID),
    ("hybrid", Propulsion.HYBRID),
    ("纯电动", Propulsion.ELECTRIC),
    ("纯电", Propulsion.ELECTRIC),
    ("电动", Propulsion.ELECTRIC),
    ("battery electric", Propulsion.ELECTRIC),
    ("electric", Propulsion.ELECTRIC),
    ("燃油", Propulsion.FUEL),
    ("汽油", Propulsion.FUEL),
    ("gasoline", Propulsion.FUEL),
    ("petrol", Propulsion.FUEL),
    ("diesel", Propulsion.FUEL),
)


@dataclass(frozen=True, slots=True)
class Verdict:
    """One answer about one car, computed once per config entry.

    `kind` is what the car is. `has_tank` and `has_plug` are what its entities
    should cover, which is not the same question: a hybrid has both, and the
    plug half is observed because `powerType` cannot express it.
    """

    kind: Propulsion
    has_tank: bool
    has_plug: bool
    source: str                  # "declared" or "observed"
    declared_raw: str = ""
    propulsion_type: str = ""    # configuration.propulsionType, corroboration only
    fuel_type: str = ""          # configuration.fuelType, corroboration only

    @property
    def is_hybrid(self) -> bool:
        return self.kind is Propulsion.HYBRID


def declared(power_type: str | None) -> Propulsion:
    """Map the server's `powerType` string onto a propulsion kind."""
    s = (power_type or "").strip().lower()
    if not s:
        return Propulsion.UNKNOWN
    # Whole-token acronyms first: "PHEV", "EV / 混动", "BEV".
    for token in re.split(r"[^0-9a-z\u4e00-\u9fff]+", s):
        if token in _EXACT:
            return _EXACT[token]
    for needle, kind in _DECLARED:
        if needle in s:
            return kind
    return Propulsion.UNKNOWN


def _observed_tank(status: dict[str, Any]) -> bool:
    """True if the car reports anything only a fuel tank can report.

    An empty `fuelStatus` block does not count: presence of the container says
    nothing, and treating it as a tank would put fuel entities on a BEV whose
    payload happens to carry the key.
    """
    if walk(status, _FUEL):
        return True
    return any(walk(status, (*_RUN, k)) not in (None, "")
               for k in ("fuelLevel", "fuelLevelPct", "aveFuelConsumption"))


def _observed_plug(status: dict[str, Any]) -> bool:
    """True if the car reports a traction battery it can charge."""
    return any(walk(status, (*_EV, k)) not in (None, "")
               for k in ("chargeLevel", "statusOfChargerConnection",
                         "distanceToEmptyOnBatteryOnly"))


def _observed(has_tank: bool, has_plug: bool) -> Propulsion:
    if has_tank and has_plug:
        return Propulsion.HYBRID
    if has_tank:
        return Propulsion.FUEL
    if has_plug:
        return Propulsion.ELECTRIC
    return Propulsion.UNKNOWN


def classify(power_type: str | None, status: dict[str, Any] | None) -> Verdict:
    """Decide what the car is, from what the account says and what the car sends.

    The declared value wins when recognised; telemetry fills in the plug, and
    stands in entirely when the declaration is missing or in a wording we have
    not seen. Never raises - a car with an unreadable payload gets UNKNOWN and
    the EV entity set, which is what every install had before this existed.
    """
    status = status or {}
    has_tank = _observed_tank(status)
    has_plug = _observed_plug(status)
    seen = _observed(has_tank, has_plug)
    said = declared(power_type)
    raw = (power_type or "").strip()

    if said is Propulsion.UNKNOWN:
        # No usable declaration: an entry created before the refresh carried
        # powerType, or a wording this table does not know. Log the raw value so
        # it can be added, and let the car speak for itself meanwhile.
        if raw:
            _LOGGER.info(
                "Unrecognised powerType %r; using telemetry instead (%s). "
                "Please report this string so it can be mapped.", raw, seen)
        return Verdict(kind=seen, has_tank=has_tank, has_plug=has_plug,
                       source="observed", declared_raw=raw,
                       propulsion_type=str(walk(status, (*_CONFIG, "propulsionType")) or ""),
                       fuel_type=str(walk(status, (*_CONFIG, "fuelType")) or ""))

    if said is Propulsion.HYBRID and seen is not said:
        # Not a contradiction: `powerType` cannot say whether a hybrid plugs in,
        # so a hybrid seen as fuel-only is an HEV and one seen as electric-only
        # has simply never reported fuel. Both are handled below, not warned
        # about - a warning here would be noise on every HEV, which is exactly
        # how a log stops being read.
        _LOGGER.debug("Declared hybrid, telemetry reports %s (tank=%s plug=%s)",
                      seen, has_tank, has_plug)
    elif seen is not Propulsion.UNKNOWN and seen is not said:
        _LOGGER.warning(
            "powerType %r says %s but the car reports %s (tank=%s plug=%s); "
            "trusting %s. Please report this vehicle so the mapping can be "
            "corrected.", raw, said, seen, has_tank, has_plug, said)

    # A declared hybrid whose payload has no charge block is a non-plug hybrid;
    # believe the telemetry about the plug, since powerType cannot express it.
    return Verdict(
        kind=said,
        has_tank=said in (Propulsion.HYBRID, Propulsion.FUEL),
        has_plug=has_plug if said is Propulsion.HYBRID
        else said is Propulsion.ELECTRIC,
        source="declared",
        declared_raw=raw,
        propulsion_type=str(walk(status, (*_CONFIG, "propulsionType")) or ""),
        fuel_type=str(walk(status, (*_CONFIG, "fuelType")) or ""),
    )
