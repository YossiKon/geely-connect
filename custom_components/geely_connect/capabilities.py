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

import math
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


def _split(raw: Any) -> set[str]:
    """A comma-separated catalog value as a set. Accepts a real list too."""
    if raw is None:
        return set()
    parts = raw if isinstance(raw, (list, tuple)) else str(raw).split(",")
    return {str(p).strip() for p in parts if str(p).strip()}


def _targets(entry: dict) -> set[str]:
    """What an entry says its command accepts, from `valueEnum` and its params.

    `valueEnum` is carried by 19 of an EX5's 62 entries and nothing here read
    it until #20 - which is how `remote_control_unlock_2`'s own declaration
    that it accepts `door,trunk` sat unread in an attached catalog while the
    thread argued about whether the boot could be opened at all. The same list
    usually appears again as a param (`door: "door,trunk"`), so both are read
    and merged rather than picking a favourite.
    """
    return _split(entry.get("valueEnum")) | _split(_params_to_dict(entry).get("door"))


# Rows only the 1.0 scheme carries - how two real E2 catalogues (#72) declare
# unlock, charging and the scheduled trip. Their presence is positive evidence
# that the catalogue is a 1.0 one, which is what lets an absent 2.0 row be
# read as "not fitted" rather than "not declared".
_SCHEME_1_0_ROWS = ("remote_charge_1", "remote_lock_unlock", "booking_travel_1")


def _speaks_1_0(by_id: dict[str, dict]) -> bool:
    return any(fid in by_id for fid in _SCHEME_1_0_ROWS)


def parse(items: list[dict]) -> dict[str, Any]:
    """Return a flat capability summary derived from the raw catalog list.

    Keys produced (all optional - missing means "not advertised"; an explicit
    False means the catalogue declared the 1.0 scheme of a feature whose
    entity here speaks the 2.0 one, see charging below):
      ac.enabled, ac.min, ac.max, ac.step
      ac.rce_1_0  (bool - the AC speaks the 1.0 request, RCE with `rce`)
      seat.heat.enabled, seat.heat.positions (list of seat names)
      seat.vent.enabled, seat.vent.positions
      seat.alone_select  (bool - per-seat independent control supported)
      defrost.enabled
      gclean.enabled
      window_vent.enabled, window_vent.duration_s
      steering_wheel_heat.enabled
      tailgate.enabled, tailgate.rtu (bool - unlock the trunk with RTU, not RDU_2)
      frunk.enabled  (bool - set only where the catalogue names a front trunk)
      lock.enabled, unlock.enabled, unlock.targets (list of accepted targets)
      sunroof.enabled, sunshade.enabled, windows.enabled
      windows.rws_1_0  (bool - windows and ventilation speak RWS with `rws`)
      find_car.enabled
      charging.enabled, parking_comfort.enabled
      scheduled_charging.enabled, scheduled_trip.enabled
      scheduled_charging.slot_2  (bool - the schedule lives in charge-server
                                  slot 2, in the E2 app's body, not slot 6)
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
        # remote_climate_control_2 enabled - true on every car seen so far but
        # the E2 and the EX2 Max, which declare the 1.0 row below instead (#72) -
        # the combined_climate_control params were never read, so
        # `steel_wheel_heating: "true"`, `AC_step: "0.5"` and
        # `window_ventilation_duration: "60"` were all invisible, and
        # `steering_wheel_heat.enabled` could not be derived on ANY car. The
        # comment below this block always said combined_climate_control was
        # where those live; the code only ever looked at one entry.
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
    # The AC in the 1.0 scheme. An E2 declares neither climate row above, only
    # `remote_air_condition_switch` with valueRange 15.5|28.5, and its own app
    # sends RCE with the setpoint as a whole number (#72). So the range is cut
    # to the whole degrees inside it, a degree at a time, and `ac.rce_1_0`
    # tells the climate entity which request to send.
    elif enabled("remote_air_condition_switch"):
        out["ac.enabled"] = True
        out["ac.rce_1_0"] = True
        out["ac.step"] = 1.0
        vr = by_id["remote_air_condition_switch"].get("valueRange") or ""
        try:
            lo, hi = (float(x) for x in vr.split("|"))
        except ValueError:
            pass
        else:
            out["ac.min"] = float(math.ceil(lo))
            out["ac.max"] = float(math.floor(hi))

    # --- Bool-only features ---
    if enabled("remote_purification"):
        out["gclean.enabled"] = True
    if enabled("honk_flash"):
        out["find_car.enabled"] = True
    if enabled("door_lock_switch_control") or enabled("remote_control_lock_2"):
        out["lock.enabled"] = True
    if enabled("remote_control_unlock_2"):
        out["unlock.enabled"] = True
        targets = _targets(by_id.get("remote_control_unlock_2") or {})
        if targets:
            out["unlock.targets"] = sorted(targets)
    # Stays keyed to remote_control_open_2 alone, and the reason is now the
    # opposite of what it looks like. #20 ended at the TCAM's own vehicleCtrl
    # binary: RDO_TRUNK is provisioned in configuration with no handler behind
    # it, so this entry declares a command the car cannot execute - the powered
    # open is not reachable from any cloud channel. What the Unlock Trunk
    # button actually sends is RDU_2 target=trunk, whose declaration is
    # unlock.targets above.
    #
    # So the flag is not read as "this car can power its tailgate open"; it is
    # the one catalog entry known to differ between an EX5 and a Starray, and
    # #20's evidence rests on it being absent from all three Starray dumps.
    # Widening it to accept the unlock entry would make every car look alike
    # and cost that finding - see the test that pins this.
    if enabled("remote_control_open_2"):
        out["tailgate.enabled"] = True
    # Which request the Unlock Trunk button sends. The 2.0 unlock row keeps
    # RDU_2 target=trunk, as on every EX5 and Starray. A catalogue without it
    # and with the 1.0 trunk row gets `RTU`, which is what an E2's own app
    # sends (#72); there, RDU_2 unlocks the whole car whatever the target.
    if not enabled("remote_control_unlock_2") and enabled("remote_trunk_unlock"):
        out["tailgate.rtu"] = True
    # The front trunk. An E2's catalogue names it as a device of its 1.0 unlock
    # row - `remote_lock_unlock`, valueEnum and control_devices `Front_trunk`
    # (#72) - and an EX2 Max's names it the same way; the button has opened its
    # frunk. The new platform lists one only as the unlock target `hood` (#88),
    # which has no entity yet. Unlike the flags around it this one is read on
    # presence only: no EX5 or Starray has a frunk, so a catalogue that does not
    # name it, or no catalogue at all, gets no Unlock Frunk button rather than
    # one that cannot work.
    lu = by_id.get("remote_lock_unlock") or {}
    devices = _targets(lu) | _split(_params_to_dict(lu).get("control_devices"))
    if enabled("remote_lock_unlock") and "front_trunk" in {d.lower() for d in devices}:
        out["frunk.enabled"] = True
    if enabled("remote_control_skylight_2"):
        out["sunroof.enabled"] = True
    if enabled("remote_control_curtain_2"):
        out["sunshade.enabled"] = True
    if enabled("remote_control_window_2"):
        out["windows.enabled"] = True
    if enabled("remote_control_ventilate_2"):
        out["window_vent.enabled"] = True
    # The windows in the 1.0 scheme: no 2.0 window row, and one of the 1.0
    # ones an E2 declares. Its app sends RWS with an `rws` parameter (#72),
    # where RWS_2 was forwarded and never carried out.
    if not (enabled("remote_control_window_2") or enabled("remote_control_ventilate_2")) \
            and any(enabled(f) for f in ("remote_window_open", "remote_window_close",
                                         "remote_window_ventilate")):
        out["windows.rws_1_0"] = True
    # Charging start/stop, and the one place this parser says "no" from a
    # catalogue that names a feature rather than from one that omits it.
    #
    # An EX5 declares `remote_charge_2` with `charging_switch:
    # end_charge_switch,start_charge_switch` - a switch, which is what the
    # Charging entity sends (RCS start/stop). A South African E2 and a
    # Colombian one (#72) declare the 1.0 scheme instead: `remote_charge_1`,
    # whose only enum is `remote_charge_1_chargestart` with a
    # `remote_charge_1_chargestarttime` param - a start *time*, not a switch -
    # and no `_2` row anywhere in their 31 entries. The owner confirmed the
    # official app offers no start/stop on that car, and pressing the switch
    # here did nothing. So a catalogue that spells out the 1.0 charge entry and
    # not the 2.0 one is read as "no switch", while a catalogue that names
    # neither stays permissive exactly as before - absence alone is not
    # evidence (#63), a present 1.0 declaration is.
    if enabled("remote_charge_2"):
        out["charging.enabled"] = True
    elif "remote_charge_1" in by_id:
        out["charging.enabled"] = False
        # The same car's `statusOfChargerConnection` reads 3 whenever the
        # cable is in: ten diagnostics files from the South African E2 (#72)
        # carry 3 at idle on 100 %, at idle on 58 % with the schedule holding
        # the charge off, and while actually charging at 59 %. What told the
        # three apart was `isCharging` (false, false, true) with
        # `timeToFullyCharged` (0, 0, 155). On an EX5 the code is the word
        # (#10, #17), and on a token-served Geely EM car `isCharging` read
        # false through a real charge (#88), so this reading applies only
        # where the catalogue declares the 1.0 charge scheme - the poller and
        # sensor._is_charging read the flag.
        out["charging.code_3_is_plugged"] = True
    # Parking Comfort, by the same evidence standard. The 1.0 catalogue
    # enumerates its remote controls one by one (lock, trunk, windows, window
    # vent, AC, charge, scheduled trip) and has no parking-comfort row in any
    # spelling; the E2's own app has no such option (#72); and the RSM start
    # sent from here sat unexecuted at the gateway for two minutes (8070 on
    # every retry of the stop) on a car that answers lock commands within a
    # second. So a catalogue that names the 1.0 scheme and no
    # `parking_comfortable_2` gets no switch. Absent from a catalogue that
    # names neither scheme stays permissive, as everywhere else here.
    if enabled("parking_comfortable_2"):
        out["parking_comfort.enabled"] = True
    elif _speaks_1_0(by_id):
        out["parking_comfort.enabled"] = False
    # Scheduled charging, by the same rule. The switch and the two time
    # entities write charge-server slot 6 with the rbc* body, which is
    # `apt_charging_single_cycle_G2` (GEEA 2.0, startTime/endTime/Cycle). The
    # same two E2s carry `remote_appointment_charging` with the 1.0 params
    # (`booking_travel_1_*`, `remote_charge_1_chargestarttime`) and no G2 row;
    # on both, slot 6 answers the generic empty envelope, and the 23:00-07:00
    # schedule one owner set in the app sits in slot 2 in a different shape
    # (scheduleList / timerActivation / day). He reported the switch here had
    # no effect - it was writing a slot his car does not use. So that
    # declaration gets `scheduled_charging.slot_2`: the switch and the time
    # entities read and write slot 2 in the body the E2's own app sends,
    # captured in both directions (#72). The Colombian EX2 declares the same
    # rows, so it gets the same, untried there. A bare
    # `remote_appointment_charging` with no params, which is how an EX5
    # declares it beside the G2 row, keeps the permissive reading.
    apt = by_id.get("remote_appointment_charging") or {}
    if enabled("apt_charging_single_cycle_G2"):
        out["scheduled_charging.enabled"] = True
    elif "remote_charge_1_chargestarttime" in _params_to_dict(apt):
        # Declared is enough to rule slot 6 out; it takes an enabled row to
        # put a switch on slot 2.
        out["scheduled_charging.enabled"] = enabled("remote_appointment_charging")
        if out["scheduled_charging.enabled"]:
            out["scheduled_charging.slot_2"] = True
    elif enabled("remote_appointment_charging"):
        out["scheduled_charging.enabled"] = True
    # The scheduled trip, by the same rule. set_scheduled_trip sends the body
    # an EX5 owner captured (#4), and an EX5 declares the feature as
    # `booking_travel_2`. The E2s carry `booking_travel_1`, whose slot 4
    # answers in another shape ({bw: "false", timerId: "0"}), so the EX5 body
    # is not theirs to receive.
    if enabled("booking_travel_2"):
        out["scheduled_trip.enabled"] = True
    elif "booking_travel_1" in by_id:
        out["scheduled_trip.enabled"] = False

    return out
