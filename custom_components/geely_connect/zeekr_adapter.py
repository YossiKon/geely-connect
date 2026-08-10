"""Adapter presenting the new-platform ZeekrClient as the integration's api.

The coordinator and every platform call the legacy GeelyApi surface
(vehicle_status / control / ...). This adapter implements that surface on
top of ZeekrClient so the new platform can slot in behind the CONF_PLATFORM
flag without touching any consumer.

Error mapping:
  - ZeekrAuthError / auth-looking ZeekrApiError  -> one refresh_session()
    retry, then GeelyAuthError (which drives the HA reauth flow).
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
from __future__ import annotations

import time
from typing import Any

from .api import GeelyAuthError, GeelyControlError
from .zeekr_client import (
    GATEWAY, ZeekrApiError, ZeekrAuthError, ZeekrClient, ZeekrIdaas,
)

_AUTH_HINTS = (
    "token", "auth", "login", "session", "expired", "unauthor",
    "sign in", "401", "403", "credential",
)


def _looks_authy(text: str) -> bool:
    low = text.lower()
    return any(h in low for h in _AUTH_HINTS)


class ZeekrAdapter:
    """GeelyApi-shaped wrapper around ZeekrClient for the new platform."""

    def __init__(self, *, email: str, vin: str, user_id: str,
                 access_token: str, refresh_token: str,
                 hf_token: str = "", vehicle_model: str = "",
                 password: str = "", country_code: str = "AU",
                 timezone: str = "UTC", hf_expiry: int = 0,
                 gateway: str = GATEWAY) -> None:
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
        client-2 tspCode -> old-platform session/secure -> new HF JWT."""
        if not self._password:
            raise ZeekrAuthError(
                "no stored password for HF renewal - reauthenticate")
        token_value = ZeekrIdaas(country=self._country_code).login_by_email_password(
            self._email, self._password)
        self._client.login_hf(token_value)
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
        check works unchanged against the real gateway codes."""
        return self._authed(self._client.vehicle_status_resp,
                            self.vin, self.user_id)

    def request_position_refresh(self) -> dict:
        """PAI wake - the same RemoteControlRequest family the legacy client
        fires (serviceId PAI / operation 4 / pai 1)."""
        body = {
            "command": "start",
            "creator": "tc",
            "latest": True,
            "serviceId": "PAI",
            "serviceParameters": [
                {"key": "operation", "value": "4"},
                {"key": "pai", "value": "1"},
            ],
            "timestamp": str(int(time.time() * 1000)),
            "userId": str(self.user_id),
        }
        return self._authed(self._client.control_resp, self.vin, body)

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
        # Legacy /geelyTCAccess/tcservices/capability has a new-platform
        # sibling but its shape is unverified; an empty catalog is the
        # documented best-effort default (all-features view).
        return []

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
