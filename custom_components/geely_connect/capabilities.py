"""Parse the Geely capability catalog into a typed view.

The raw catalog is fetched from
`GET /geelyTCAccess/tcservices/capability/{VIN}` and returns ~65 entries
per vehicle. Each entry has a `functionId` (e.g. `remote_climate_control_2`),
a `valueEnable` boolean, and a `paramsJson` array describing what params
the function accepts and what value ranges are valid.

This module turns that into a small dict the platform setup files can
read to decide which entities to expose and what bounds to use.

Source: AVD packet capture (see docs/AVD_CAPTURE_GUIDE.md). Each function
listed below has been verified to appear in at least one real EX5
capability response.
"""
# -----------------------------------------------------------------------------
# Portions of this file - the reverse-engineered Geely protocol / field mappings
# (the parts that required protocol research) - are derived from
# nitaybz/geely-global-ha, used under the MIT License. See NOTICE.txt.
# Original framework, security hardening and transport are our own work.
# -----------------------------------------------------------------------------
from __future__ import annotations

from typing import Any


def _params_to_dict(entry: dict) -> dict[str, str]:
    """Flatten paramsJson [{nameKey, name, config}, ...] into {nameKey: config}."""
    out: dict[str, str] = {}
    for p in entry.get("paramsJson") or []:
        k = p.get("nameKey")
        v = p.get("config")
        if k and v is not None:
            out[k] = v
    return out


def _by_id(items: list[dict]) -> dict[str, dict]:
    return {x.get("functionId"): x for x in items if x.get("functionId")}


def parse(items: list[dict]) -> dict[str, Any]:
    """Return a flat capability summary derived from the raw catalog list.

    Keys produced (all optional - missing means "not advertised"):
      ac.enabled, ac.min, ac.max, ac.step
      seat.heat.enabled, seat.heat.positions (list of seat names)
      seat.vent.enabled, seat.vent.positions
      seat.alone_select  (bool - per-seat independent control supported)
      defrost.enabled
      gclean.enabled
      window_vent.enabled, window_vent.duration_s
      steering_wheel_heat.enabled
      tailgate.enabled
      lock.enabled, unlock.enabled
      sunroof.enabled, sunshade.enabled, windows.enabled
      find_car.enabled
      charging.enabled, parking_comfort.enabled
      raw_count (int)
    """
    out: dict[str, Any] = {"raw_count": len(items)}
    by_id = _by_id(items)

    def enabled(fid: str) -> bool:
        e = by_id.get(fid)
        return bool(e and e.get("valueEnable") in (True, "true", "True", 1, "1"))

    # --- AC: prefer remote_climate_control_2; fall back to combined_climate_control ---
    rcc2 = by_id.get("remote_climate_control_2") or {}
    ccc = by_id.get("combined_climate_control") or {}
    ac_source = rcc2 if rcc2.get("valueEnable") else ccc
    if ac_source.get("valueEnable"):
        out["ac.enabled"] = True
        # valueRange = "15.5|28.5"
        vr = ac_source.get("valueRange") or ""
        if "|" in vr:
            try:
                lo, hi = vr.split("|", 1)
                out["ac.min"] = float(lo)
                out["ac.max"] = float(hi)
            except ValueError:
                pass
        # Params from BOTH climate entries, the chosen source winning on a clash.
        #
        # These two entries split the climate declaration between them, and
        # reading only the chosen one silently dropped whatever lived in the
        # other. Verified on a real EX5 catalogue (#20): with
        # remote_climate_control_2 enabled - true on every car seen so far - the
        # combined_climate_control params were never read, so `steel_wheel_heating:
        # "true"`, `AC_step: "0.5"` and `window_ventilation_duration: "60"` were
        # all invisible, and `steering_wheel_heat.enabled` could not be derived on
        # ANY car. The comment below this block always said combined_climate_control
        # was where those live; the code only ever looked at one entry.
        #
        # Deliberately NOT merging remote_centra_lockstatus, which carries a copy
        # of this same param list in that car's catalogue: it is a central-lock
        # status entry, and params describing seat heating in it are a vendor data
        # error rather than a third source of truth.
        ps = {**_params_to_dict(ccc), **_params_to_dict(rcc2)} if ac_source is rcc2 \
            else _params_to_dict(ccc)
        # fallback to ad_temp_range param
        if "ac.min" not in out and "ad_temp_range" in ps and "|" in ps["ad_temp_range"]:
            try:
                lo, hi = ps["ad_temp_range"].split("|", 1)
                out["ac.min"] = float(lo)
                out["ac.max"] = float(hi)
            except ValueError:
                pass
        if "AC_step" in ps:
            try:
                out["ac.step"] = float(ps["AC_step"])
            except ValueError:
                pass
        # showType in remote_climate_control_2 sometimes carries step too
        if "ac.step" not in out and ac_source.get("showType"):
            try:
                out["ac.step"] = float(ac_source["showType"])
            except ValueError:
                pass

        # Seat support
        seat_heat_pos = ps.get("dpt_heat_loc") or ps.get("seatheat_area")
        seat_vent_pos = ps.get("dpt_vent_loc") or ps.get("seatvent_area")
        if seat_heat_pos:
            out["seat.heat.enabled"] = True
            out["seat.heat.positions"] = [s.strip() for s in seat_heat_pos.split(",") if s.strip()]
        if seat_vent_pos:
            out["seat.vent.enabled"] = True
            out["seat.vent.positions"] = [s.strip() for s in seat_vent_pos.split(",") if s.strip()]
        if "alone_select" in ps:
            out["seat.alone_select"] = ps["alone_select"] == "support"
        # climate_devices - semicolon-separated list of features
        devs = ps.get("climate_devices") or ""
        if "defrost" in devs:
            out["defrost.enabled"] = True
        if "AC" in devs:
            out["ac.enabled"] = True
        # combined_climate_control adds steel_wheel_heating + window_ventilation
        if ps.get("steel_wheel_heating") in ("true", "True", True):
            out["steering_wheel_heat.enabled"] = True
        wv = ps.get("window_ventilation")
        if wv in ("true", "True", True):
            out["window_vent.enabled"] = True
            try:
                out["window_vent.duration_s"] = int(ps.get("window_ventilation_duration", "60"))
            except ValueError:
                pass

    # --- Bool-only features ---
    if enabled("remote_purification"):
        out["gclean.enabled"] = True
    if enabled("honk_flash"):
        out["find_car.enabled"] = True
    if enabled("door_lock_switch_control") or enabled("remote_control_lock_2"):
        out["lock.enabled"] = True
    if enabled("remote_control_unlock_2"):
        out["unlock.enabled"] = True
    if enabled("remote_control_open_2"):
        out["tailgate.enabled"] = True
    if enabled("remote_control_skylight_2"):
        out["sunroof.enabled"] = True
    if enabled("remote_control_curtain_2"):
        out["sunshade.enabled"] = True
    if enabled("remote_control_window_2"):
        out["windows.enabled"] = True
    if enabled("remote_control_ventilate_2"):
        out["window_vent.enabled"] = True
    if enabled("remote_charge_2"):
        out["charging.enabled"] = True
    if enabled("parking_comfortable_2"):
        out["parking_comfort.enabled"] = True
    if enabled("remote_appointment_charging") or enabled("apt_charging_single_cycle_G2"):
        out["scheduled_charging.enabled"] = True

    return out
