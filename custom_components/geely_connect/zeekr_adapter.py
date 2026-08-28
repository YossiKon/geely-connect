"""Adapter presenting the new-platform ZeekrClient as the integration's api.

The coordinator and every platform call the legacy GeelyApi surface
(vehicle_status / control / ...). This adapter implements that surface on
top of ZeekrClient so the new platform can slot in behind the CONF_PLATFORM
flag without touching any consumer.

Error mapping:
  - ZeekrAuthError / auth-looking ZeekrApiError  -> one silent HF renewal
    (_renew_hf: re-login from the stored password and re-mint the HF JWT),
    then GeelyAuthError (which drives the HA reauth flow).
  - Non-auth ZeekrApiError propagates (coordinator counts it toward its
    failure tolerance like any transient error).

Known gaps - raise cleanly, and the coordinator already treats
secondary-endpoint failures as non-fatal and carries state forward:
  - vehicle_status_state   (legacy /remote-control/vehicle/status/state/{vin})
  - charge_server_get      (legacy /charge-server/ecarx_charge_set GET)
  - scheduled_charging_set (charge-server write)
  - rapid_climate          (charge-server bizType=7 write)
  - fetch_capabilities     (legacy /geelyTCAccess/tcservices/capability)

Each gap maps to an endpoint family present in the new app's static
analysis (iovif.java lists /remote-control/* and /charge-server/* routes)
but none has been verified against the live new gateway, so they stay
explicitly unimplemented until the primary path is proven live.
"""
# Part of the new Geely EM (Zeekr) platform support first implemented by
# Scott Lorien (@scottaki) in pull request #33. See NOTICE.txt.
from __future__ import annotations

import logging
import time
from typing import Any

from .api import GeelyAuthError, GeelyControlError
from .zeekr_client import (
    GATEWAY, ZeekrApiError, ZeekrAuthError, ZeekrClient, ZeekrIdaas,
)

_LOGGER = logging.getLogger(__name__)

_AUTH_HINTS = (
    "token", "auth", "login", "session", "expired", "unauthor",
    "sign in", "401", "403", "credential",
)


def _looks_authy(text: str) -> bool:
    low = text.lower()
    return any(h in low for h in _AUTH_HINTS)


# Capability codes on the new platform, mapped to the old catalogue's
# functionIds. Every row is justified by the vendor's own `functionName` label
# carried in the same payload, quoted here so the reasoning is checkable
# rather than remembered.
_CAPABILITY_CODES: dict[str, str] = {
    # plainly-named codes the old catalogue also uses
    "remote_control_lock_2":   "remote_control_lock_2",
    "remote_control_unlock_2": "remote_control_unlock_2",
    "remote_charge_2":         "remote_charge_2",
    "honk_flash":              "honk_flash",
    "parking_comfortable_2":   "parking_comfortable_2",
    "remote_purification":     "remote_purification",
    # service-shaped codes, read from their labels
    "C_RDU_2_2": "remote_control_open_2",       # 远程解锁-控制设备_后备箱 (tailgate)
    "C_RDU_2_3": "remote_control_unlock_2",     # 远程解锁-控制设备_车门 (doors)
    "C_RWS_1":   "remote_control_ventilate_2",  # 远程车窗微开 (window vent)
    "C_RWS_1_5": "remote_control_window_2",     # 远程车窗关闭 (windows)
    "C_RHL_1":   "honk_flash",                  # 远程闪灯鸣笛
    "C_RHL_2":   "honk_flash",                  # 单独闪灯
    "C_RHL_3":   "honk_flash",                  # 单独鸣笛
    # climate: several codes all mean "this car has remote climate"
    "remote_climate_control": "remote_climate_control_2",  # ZK空调服务不区分命令
    "C_PAA_1":  "remote_climate_control_2",     # 智能温控_PAA空调
    "C_PAA_12": "remote_climate_control_2",     # 智能温控入口
    "C_ZAF_1":  "remote_climate_control_2",     # 环境调节_空调远控指令
}

# Seat-heat positions, from the codes that name one seat each.
_SEAT_HEAT_POSITIONS: dict[str, str] = {
    "C_PAA_5_1": "front-left",    # …PAA座椅加热支持的位置_主驾
    "C_PAA_5_2": "front-right",   # …PAA座椅加热支持的位置_副驾
    "V_ZYJRZH_1_3": "front-left",   # 远程座椅加热转换-座椅位置_左前
    "V_ZYJRZH_1_4": "front-right",  # 远程座椅加热转换-座椅位置_右前
}

_WHEEL_HEAT_CODE = "C_PAA_6"      # …PAA是否支持方向盘加热


def _enabled_row(row: dict) -> bool:
    """`paramValueUse` is the enable flag; "N"/"0"/blank mean not available."""
    use = row.get("paramValueUse")
    if use is None:
        return False
    text = str(use).strip()
    return bool(text) and text.upper() not in ("N", "0", "FALSE", "NO")


def translate_capabilities(rows: list[dict]) -> list[dict]:
    """New-platform catalogue -> the entry shape capabilities.py parses.

    The rule is deliberately asymmetric, because the two platforms describe a
    car at different resolutions:

      * a feature is ENABLED when a code for it is present, and
      * absence means "not fitted" ONLY for the features this catalogue
        actually enumerates.

    The distinction matters. The catalogue lists seat-heat positions one code
    per seat, so no seat-vent code is real evidence the car has none. But the
    climate entry is labelled 空调服务不区分命令 - "the AC service does not
    distinguish commands" - so it cannot say whether defrost exists, and
    reading its silence as "no defrost" would remove a control that works.
    Anything in that second class is left to the permissive default rather
    than being switched off on absence.

    An empty or unrecognised catalogue returns [] - the caller then keeps
    today's all-features behaviour, so this can never be worse than before.
    """
    if not rows:
        return []
    live = {r.get("functionCode") for r in rows if _enabled_row(r)}
    if not live:
        return []

    ids: set[str] = {_CAPABILITY_CODES[c] for c in live if c in _CAPABILITY_CODES}
    entries: list[dict] = []

    seats = sorted({pos for code, pos in _SEAT_HEAT_POSITIONS.items() if code in live})
    if "remote_climate_control_2" in ids:
        params = [
            # The service is undifferentiated, so defrost and AC are asserted
            # rather than discovered - see the docstring.
            {"nameKey": "climate_devices", "config": "AC;defrost"},
        ]
        if seats:
            params.append({"nameKey": "dpt_heat_loc", "config": ",".join(seats)})
        if _WHEEL_HEAT_CODE in live:
            params.append({"nameKey": "steel_wheel_heating", "config": "true"})
        if "remote_control_ventilate_2" in ids:
            params.append({"nameKey": "window_ventilation", "config": "true"})
        entries.append({"functionId": "remote_climate_control_2",
                        "valueEnable": True, "paramsJson": params})
        ids.discard("remote_climate_control_2")

    if "remote_control_unlock_2" in ids:
        targets = ["door"]
        if "C_RDU_2_2" in live:
            targets.append("trunk")
        if "C_RDU_2_1" in live:            # 前备箱 (frunk)
            targets.append("hood")
        entries.append({"functionId": "remote_control_unlock_2", "valueEnable": True,
                        "paramsJson": [{"nameKey": "door", "config": ",".join(targets)}]})
        ids.discard("remote_control_unlock_2")

    entries.extend({"functionId": fid, "valueEnable": True} for fid in sorted(ids))
    return entries


def _wrap_vehicle_status(data: Any) -> Any:
    """Give the new gateway's status payload the old platform's nesting.

    Old platform:  data = {"vehicleStatus": {basicVehicleStatus,
                            additionalVehicleStatus, updateTime}}
    New gateway:   data = {basicVehicleStatus, additionalVehicleStatus,
                            updateTime}

    Every consumer reads through the "vehicleStatus" level, so without this the
    entire entity set reads unknown while the payload sits right there - and
    the few entities that read top-level fields keep working, which makes the
    symptom look like anything but a missing key. The keys are MOVED under
    vehicleStatus rather than copied - see the note at the move below for why a
    copy is worse than it sounds.

    `updateTime` is carried in too, and it is not cosmetic: it is the car's own
    stamp on the snapshot, which Car Reported At walks for at
    vehicleStatus.updateTime. On a real old-platform car it lives INSIDE
    vehicleStatus; the new gateway flattens it to the top level, so leaving it
    out blanked the one sensor whose whole job is to reveal a stale snapshot -
    exactly #24's failure, where a parked Geely stops reporting and the cloud
    keeps serving the last fix. Found on #53: a car that had driven 18 km still
    read `home` with nothing saying the data was old.
    """
    if not isinstance(data, dict) or "vehicleStatus" in data:
        return data
    if "basicVehicleStatus" not in data and "additionalVehicleStatus" not in data:
        return data
    # MOVE the keys rather than copying them. A copy leaves a second path to
    # every field - basicVehicleStatus.* as well as
    # vehicleStatus.basicVehicleStatus.* - and the full-exposure sensor sweep
    # skips only curated paths, which all start "vehicleStatus.". Every
    # duplicate therefore becomes an extra raw diagnostic entity: roughly two
    # hundred of them on this car, each a twin of one already on the list.
    moved = ("basicVehicleStatus", "additionalVehicleStatus", "updateTime")
    wrapped = {k: v for k, v in data.items() if k not in moved}
    wrapped["vehicleStatus"] = {k: v for k, v in data.items() if k in moved}
    return wrapped


class ZeekrAdapter:
    """GeelyApi-shaped wrapper around ZeekrClient for the new platform."""

    def __init__(self, *, email: str, vin: str, user_id: str,
                 access_token: str, refresh_token: str,
                 hf_token: str = "", vehicle_model: str = "",
                 password: str = "", country_code: str = "AU",
                 timezone: str = "UTC", hf_expiry: int = 0,
                 gateway: str = GATEWAY, enc_vin: str = "") -> None:
        self.vin = vin
        self.user_id = user_id
        self._email = email
        self._password = password
        self._country_code = country_code
        # HF JWT expiry as an absolute epoch, from the entry (0 = unknown).
        self._hf_expiry_ts: int = int(hf_expiry) if hf_expiry else 0
        self._client = ZeekrClient(email=email, password="", gateway=gateway,
                                   vehicle_model=vehicle_model)
        self._client.timezone = timezone
        self._client.country_code = country_code
        self._client.access_token = access_token
        self._client.refresh_token = refresh_token
        self._client.user_id = user_id
        self._client.hf_token = hf_token or None
        # Set only for vehicles that live on the new platform; selects the
        # new-gateway status path in vehicle_status() below.
        self._client.enc_vin = enc_vin or ""
        # Set when a silent HF renewal happened; the coordinator persists the
        # new token + expiry into the config entry after the next poll.
        self.hf_dirty = False

    @property
    def hf_expiry(self) -> int:
        """Absolute epoch the current HF JWT expires at (0 = unknown)."""
        return self._hf_expiry_ts

    def take_renewed_hf_token(self) -> tuple[str, int] | None:
        """Return (hf_token, expiry_ts) when a silent renewal happened since
        the last poll (clearing the dirty flag), else None. The coordinator
        persists the fresh session once per poll; HA drives polls
        sequentially, so the plain flag needs no locking."""
        if not self.hf_dirty:
            return None
        self.hf_dirty = False
        return self._client.hf_token or "", self._hf_expiry_ts

    # ---- helpers ----------------------------------------------------------

    def _renew_hf(self) -> None:
        """The app's silent renewal, mirrored: password -> IDaaS tokenValue ->
        tspCode -> both sessions re-minted.

        This re-mints the new-platform access token as well as the HF JWT.
        Renewing only the HF side leaves `access_token` frozen at whatever
        the config entry holds, and that token is single-session
        server-side: once the phone app signs in, the stored one is
        superseded and every new-gateway call answers `079021 The account
        is currently logged in elsewhere` permanently, because nothing
        ever replaces it. login_tsp re-mints the snc session and calls
        login_hf itself, so one call recovers both.
        """
        if not self._password:
            raise ZeekrAuthError(
                "no stored password for HF renewal - reauthenticate")
        token_value = ZeekrIdaas(country=self._country_code).login_by_email_password(
            self._email, self._password)
        self._client.login_tsp(token_value)
        self._hf_expiry_ts = int(time.time()) + self._client.hf_expires_in
        self.hf_dirty = True

    def _hf_expired(self) -> bool:
        if not self._client.hf_token:
            return True
        # Renew an hour early so a poll never rides an expiring token.
        return self._hf_expiry_ts > 0 and time.time() > self._hf_expiry_ts - 3600

    def _authed(self, fn, *args):
        """Run one authenticated call. Ensures a live HF JWT first (silent
        renewal from the stored password when near expiry), and on an
        auth-looking failure renews once and retries. A renewal failure or a
        second auth-y failure raises GeelyAuthError -> the HA reauth flow."""
        try:
            if self._hf_expired():
                self._renew_hf()
            return fn(*args)
        except (ZeekrAuthError, ZeekrApiError) as exc:
            if isinstance(exc, ZeekrApiError) and not _looks_authy(str(exc)):
                raise
            try:
                self._renew_hf()
                return fn(*args)
            except (ZeekrAuthError, ZeekrApiError) as exc2:
                # A non-authy failure on the retried call is a transient
                # gateway error, not a credential problem - let it propagate
                # (the coordinator counts it toward its failure tolerance).
                if isinstance(exc2, ZeekrApiError) and not _looks_authy(str(exc2)):
                    raise
                raise GeelyAuthError(str(exc2)) from exc2

    # ---- coordinator surface ----------------------------------------------

    def vehicle_status(self) -> dict:
        """Full response (code/data/...) so the coordinator's success-code
        check works unchanged against the real gateway codes.

        A vehicle with an `x-vin` token lives on the new platform, where
        the old one answers 8060 (VIN unknown); read it from the new
        gateway instead, then normalise the envelope so nothing
        downstream has to know which platform answered.
        """
        if self._client.enc_vin:
            resp = self._authed(self._client.vehicle_status_new_resp)
            if isinstance(resp, dict):
                # This gateway reports success as "000000", not 1000.
                if str(resp.get("code")) in ("000000", "0"):
                    resp = {**resp, "code": 1000}
                resp = {**resp, "data": _wrap_vehicle_status(resp.get("data"))}
            return resp
        return self._authed(self._client.vehicle_status_resp,
                            self.vin, self.user_id)

    def request_position_refresh(self) -> dict:
        """PAI wake (serviceId PAI / operation 4 / pai 1) on the legacy client.

        Deliberately a no-op on the new platform: the coordinator fires this
        automatically on the first poll and on every driving cycle, and the
        control write path here is NOT live-verified on this backend (only door
        lock/unlock is). Auto-sending an unproven command to the car on a timer
        is exactly what the maintainer avoids, so until the PAI write is
        confirmed we serve the position already in the status payload and skip
        the active wake. The body it would send is kept for when it is proven:

          {command: start, serviceId: PAI, serviceParameters:
             [{operation: 4}, {pai: 1}], userId, timestamp}
        """
        return {}

    def vehicle_status_state(self) -> dict:
        raise NotImplementedError(
            "new-platform status/state endpoint not mapped yet"
        )

    def charge_server_get(self, biz_type: str) -> dict:
        raise NotImplementedError(
            "new-platform charge-server GET not mapped yet"
        )

    def scheduled_charging_set(self, **kwargs: Any) -> dict:
        raise NotImplementedError(
            "new-platform charge-server write not mapped yet"
        )

    def rapid_climate(self, **kwargs: Any) -> dict:
        raise NotImplementedError(
            "new-platform rapid-climate write not mapped yet"
        )

    def fetch_capabilities(self) -> list[dict]:
        """The catalogue, translated into the shape capabilities.py parses.

        Only vehicles addressed by an x-vin token can be asked; everything
        else keeps the previous behaviour of an empty catalogue, which
        capabilities.py reads as the permissive all-features view. A failure
        here is deliberately swallowed for the same reason: losing the
        catalogue must cost a car its feature *filtering*, never its entities.
        """
        if not self._client.enc_vin:
            return []
        try:
            rows = self._authed(self._client.capabilities_new)
        except (ZeekrAuthError, ZeekrApiError, GeelyAuthError) as err:
            _LOGGER.debug("new-platform capability fetch failed: %s", err)
            return []
        entries = translate_capabilities(rows or [])
        _LOGGER.debug("new-platform capabilities: %d row(s) -> %d entry/entries",
                      len(rows or []), len(entries))
        return entries

    # ---- platform surface --------------------------------------------------

    def control(self, service_id: str, parameters: list[dict] | None = None,
                command: str = "start", duration: int = 0) -> dict:
        """PUT /remote-control/vehicle/telematics/{vin} with the same body
        the legacy client sends (operationScheduling included)."""
        body = {
            "command": command,
            "creator": "tc",
            "operationScheduling": {
                "duration": duration, "interval": 0, "occurs": 1,
                "recurrentOperation": False,
            },
            "serviceId": service_id,
            "serviceParameters": parameters or [],
            "timestamp": str(int(time.time() * 1000)),
            "userId": str(self.user_id),
        }
        try:
            return self._authed(self._client.control_resp, self.vin, body)
        except GeelyAuthError:
            raise
        except ZeekrApiError as e:
            raise GeelyControlError("8500", str(e)) from e
