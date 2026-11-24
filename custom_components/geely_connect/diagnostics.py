"""Diagnostics support - downloads a full state dump with secrets redacted.

Lets users share a diagnostics report for troubleshooting without exposing
tokens, certificates, VIN or GPS location.
"""
from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Mapping
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .api import _IDENTIFYING_KEYS, _SECRET_KEYS, AnyCase, _norm_key, hide_values, redact, value_mask
from .const import (
    CONF_PLATFORM,
    CONF_VIN,
    CONF_ZEEKR_ENC_VIN,
    DEFAULT_PLATFORM,
    DOMAIN,
    PLATFORM_ZEEKR,
)
from .helpers import api_serves

# Home Assistant's async_redact_data matches key names exactly, so this list
# holds the spellings that appear in the config entry and in GPS payloads.
#
# It is deliberately NOT the whole list: api.redact() runs over the same data
# afterwards and masks everything in its own _SECRET_KEYS / _IDENTIFYING_KEYS,
# matching on a normalised key (lowercased, separators stripped) so spelling
# variants cannot slip through. Anything the two lists disagree about is
# therefore still masked by the second pass - which is the point, because these
# lists drifted apart once already and let the scheduled-charging "pin" field,
# which carries the VIN, into the report.
_REDACT = {
    "token", "cidpsso_token", "accessToken", "access_token", "authCode",
    "cert_path", "key_path", "user_id", "userId", "vin", "device_id",
    "device_idfa", "device_idfv", "email",
    "latitude", "longitude", "lat", "lon", "lng",
    # New Geely EM (Zeekr) platform, forward-support for #33: the account
    # password and session tokens the entry stores. redact() (the first pass)
    # already masks these by their normalised name, so this is the belt to
    # that pass's braces - the two lists exist precisely so neither is the
    # single point that has to be right. `zeekr_hf_expiry` is a timestamp and
    # stays visible on purpose.
    "zeekr_access_token", "zeekr_refresh_token", "zeekr_hf_token", "zeekr_password",
    # The old platform's session exchange also returns a refresh token (#101).
    # Nothing stores it yet; a build that does, under either spelling, must
    # not find this list a step behind redact()'s normalised match.
    "refresh_token", "refreshToken",
    # The new platform's per-vehicle x-vin token - a capability secret that
    # addresses and controls the car. redact() (the first pass) already masks
    # it by normalised name; this is the belt to that pass's braces.
    "zeekr_enc_vin",
    # The same token under its header's name, and two spellings a body might
    # use for it. redact() masks these in any case and with any separator.
    "x-vin", "xVin", "encVin",
    "hmac_access_key", "hmac_secret", "snc_secret", "vin_key", "vin_iv",
    "vin_key_hex", "vin_iv_hex", "eu_app_keys",
    # An original-login region's app id and secret, which an owner may supply
    # locally (region_keys.py, #105). They are never written into the entry;
    # redact() masks them by normalised name, and this is the belt to that
    # pass's braces.
    "app_id", "app_secret",
}


# The charge-server schedule slots, read with a GET each. Three of these are
# known: 4 is the app's Scheduled trip (its departure, Cabin Comfort and the
# Battery Temperature Maintenance toggle as `bw`, read in four states on one
# car in #4), 6 is Scheduled Charging, and 2 is where an E2 keeps its own
# Scheduled Charging instead (#72). The rest of the range is swept for state
# that sits in a slot nobody has located; it began as the hunt for Parking
# Comfort's (#30), which turned up in the status block's parkComfortState
# instead (#45). The sweep is also how
# slot 4 was decoded: the polled copies are in the status block above, and
# the live read here is what an owner's one-toggle-at-a-time test compares.
#
# 7 is left out: it is the rapid-warming write, and its GET returns nothing.
# All of these are GETs against the vehicle's own VIN, they run only when
# someone downloads a report, and a slot that does not exist answers with an
# error that is recorded rather than raised.
_CHARGE_SERVER_SLOTS = ("1", "2", "3", "4", "5", "6", "8")

# The charge limit and the discharge (V2L) floor, read as the app reads them
# (its getChargeSOC, "charging" and "discharging", #107) on the original
# platform. An EU EX5 answered `soc` "1000" and "200", which as tenths of a
# percent would be a 100 % limit and a 20 % floor - no change of the limit
# has been seen to move it yet. An E2's app has no limit among the functions
# captured in #72, and the report asks whether such a car answers (#131).
_CHARGE_SOC_SETTINGS = ("charging", "discharging")

# The control hosts the read has answered on, and the only ones it is sent
# to: the EU gateway, which #107's EX5 and #72's South African E2 both use.
# Elsewhere the path is unseen, and a 1402 from a gateway that does not know
# it would renew the session - and could sign the phone app out - on every
# report.
_CHARGE_SOC_HOSTS = ("apis.ecloudeu.com",)

# A sweep is worth ~a second per slot on a healthy connection and must not be
# what makes a report impossible to produce on a sick one. Whatever has
# arrived by then is kept. The charge-limit reads come out of the same time.
_SWEEP_TIMEOUT_S = 25


def _clean(data: Any) -> Any:
    """Both redaction passes, ours first.

    api.redact() runs before Home Assistant's so the output reads cleanly: a
    value it shortens to a tail is then replaced outright by the second pass,
    rather than the second pass leaving a tail of the word REDACTED.
    """
    return async_redact_data(redact(data), _REDACT)


def _held(entry: ConfigEntry, api: Any) -> tuple:
    """The values the report leaves out wherever they stand in its text.

    Both redaction passes match on key names, which is enough for structured
    payloads and no help at all inside a sentence: an error's text, an
    answer's own words. Error text is supposed to be free of these already,
    and the clients word theirs without them - but an error raised outside a
    client was never scrubbed, and a report is the wrong place to rely on
    every future `raise` remembering it. A session token holding a line break
    reached the sweep's rows whole: http.client refuses the header and quotes
    the value, escaped, in its error.

    So these are masked in every string of the report (hide_values, which
    matches the quoted forms too): each value the entry keeps under a name
    either pass masks - the VIN, the account's tokens, its e-mail address,
    its user id - and what the client's error text leaves out (hidden()):
    the vehicle token it sends, and the session tokens as they stand now,
    which the entry keeps an older copy of, or none. Read again as each of
    the sweep's reads goes out and as its row is written, and once more
    for the whole report, which is masked with every reading: a slot read
    can renew the session (the Geely EM adapter's _authed_charge), and so
    can a poll meanwhile, so a token one reading names may be gone by the
    next. Not a password: masked by value, a password that is a word would
    be pointed at wherever that word stands in the report.

    The entry's VIN is masked in any case (AnyCase): a gateway may name the
    car in another case than the entry keeps, which setup takes for the
    same car. The original platform's client shortens its VIN rather than
    hiding it, so the entry's copy is the only one the report has there."""
    held = [*_under_masked_names(entry.data), *_under_masked_names(entry.options)]
    vin = entry.data.get(CONF_VIN)
    if isinstance(vin, str) and vin:
        held.append(AnyCase(vin))
    hidden = getattr(api, "hidden", None)
    if callable(hidden):
        try:
            held.extend(hidden())
        except Exception:  # noqa: BLE001 - diagnostics must never raise
            pass
    return tuple(v for v in held
                 if isinstance(v, (str, int)) and not isinstance(v, bool))


def _under_masked_names(obj: Any, masked: bool = False):
    """The leaves of `obj` that sit under a name either redaction pass masks,
    leaving out a password's. An entry's data and options are read-only
    mappings, not dicts."""
    if isinstance(obj, Mapping):
        for key, value in obj.items():
            norm = _norm_key(key)
            if norm is not None and "password" in norm:
                continue
            yield from _under_masked_names(value, masked or key in _REDACT or (
                norm in _SECRET_KEYS or norm in _IDENTIFYING_KEYS))
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            yield from _under_masked_names(value, masked)
    elif masked:
        yield obj


def _masked(obj: Any, hide: Callable[[str], str]) -> Any:
    """`obj` with `hide` (value_mask) run over every string it holds, a
    key's included. A number is left as it is."""
    if isinstance(obj, str):
        return hide(obj)
    if isinstance(obj, Mapping):
        return {_masked(k, hide): _masked(v, hide) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_masked(v, hide) for v in obj]
    return obj


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    bundle = (hass.data.get(DOMAIN) or {}).get(entry.entry_id) or {}
    coordinator = bundle.get("coordinator")
    api = bundle.get("api")
    held = _held(entry, api)
    seen = list(held)       # and what each of the sweep's reads went out with
    deadline = asyncio.get_running_loop().time() + _SWEEP_TIMEOUT_S
    report = {
        "entry_data": _clean(dict(entry.data)),
        "options": _clean(dict(entry.options)),
        # Whether the new platform's vehicle token is set - never its value.
        # Both passes mask the stored field whatever it holds, so a report could
        # not tell a token from none (#63), and that decides the route every
        # read, command and catalogue request takes.
        "vehicle_token": _clean(_vehicle_token(entry, bundle.get("api"))),
        # Why the data in this report is as old as it is, and whether the
        # integration is talking to the car at all. A stale reading and a failing
        # fetch look identical in `status` alone, which is how #21 stayed open.
        "polling": _polling(bundle, coordinator, held),
        # The last commands sent and what came back. A command rejected because
        # the car was still busy is dropped, not retried, and leaves no trace
        # anywhere else unless debug logging happened to be on beforehand.
        "recent_commands": _clean(list(
            getattr(bundle.get("api"), "command_trail", None) or [])),
        # So that "I turned debug logging on and there was nothing" has an
        # answer other than guesswork.
        "logging": _logging(),
        # Capability flags are not user data, but the catalog is echoed from the
        # server and has carried a vin field, so it goes through as well.
        "capabilities": _clean(bundle.get("capabilities") or {}),
        # The catalog verbatim, not just the flags we know how to derive. The
        # parser keeps about a dozen keys and drops the rest, so a report could
        # not answer "does this trim advertise a blower level, or seat
        # positions by name?" - the questions that decide whether a missing
        # control is missing from the car or only from this integration.
        "capabilities_raw": _clean(bundle.get("capabilities_raw") or []),
        # The new gateway's catalogue before translation, on a Geely EM entry
        # read through its token; [] everywhere else. The translation keeps
        # only the codes it has a rule for, and the question #63 asks is
        # precisely which codes a car with vented seats or a sunroof carries.
        "capabilities_new_raw": _clean(bundle.get("capabilities_new_raw") or []),
        "status": _clean((coordinator.data if coordinator else {}) or {}),
        # The charge-server schedule slots, which nothing polls - see
        # _CHARGE_SERVER_SLOTS for the two open questions this answers.
        "charge_server": await _charge_server(hass, entry, api, seen, deadline),
        # The charge limit and the discharge floor - see _CHARGE_SOC_SETTINGS.
        "charge_soc": await _charge_soc(hass, entry, api, seen, deadline),
        # Whether the dashboard cards are actually being served, and from
        # where: the first thing to check when a card reads "Custom element
        # not found" but the vehicle itself is fine.
        "cards": _card_status(hass),
    }
    return _masked(report, value_mask((*seen, *_held(entry, api))))


async def _charge_server(hass: HomeAssistant, entry: ConfigEntry, api: Any,
                         seen: list, deadline: float | None = None) -> dict[str, Any]:
    """Read every charge-server slot once, recording failures as results.

    A slot that does not exist is expected to answer with an error, and that
    error is the finding - so nothing here raises, and a slot that times out
    does not cost the report the slots already read.

    An EM (Zeekr) entry without a vehicle token has no such endpoint. Its
    adapter carries the method and says it does not serve it - asked
    anyway, every slot came back as a NotImplementedError row that read
    like seven failures. With a token the sweep runs: the schedule slots
    answer from the gateway's charge-manage service and the rest say they
    are not mapped (#114). An entry that failed setup before building a
    client has no method at all.

    An error's text is masked before the row is cut short - a cut through a
    value leaves a prefix no mask can match - with every value read so far
    (`seen`, which the whole report is masked with too) and those read as
    the row is written. Each read's values are taken as it goes out: a
    renewal, this sweep's or a poll's, can replace the token a read carries
    before its error comes back.
    """
    get = getattr(api, "charge_server_get", None)
    if get is None or not api_serves(api, "charge_server"):
        return {}
    return await _reads(hass, entry, api, seen, get, _CHARGE_SERVER_SLOTS,
                        deadline, "the slots above")


async def _charge_soc(hass: HomeAssistant, entry: ConfigEntry, api: Any,
                      seen: list, deadline: float | None = None) -> dict[str, Any]:
    """The charge limit and the discharge floor as the app reads them
    (_CHARGE_SOC_SETTINGS), each read once the way a charge-server slot is,
    in what is left of the sweep's time. Only the original platform's client
    has the read, and it is sent only to the hosts it has answered on
    (_CHARGE_SOC_HOSTS); a Geely EM entry, or a car on another gateway, gets
    nothing."""
    get = getattr(api, "charge_soc_get", None)
    if get is None or getattr(api, "control_host", None) not in _CHARGE_SOC_HOSTS:
        return {}
    return await _reads(hass, entry, api, seen, get, _CHARGE_SOC_SETTINGS,
                        deadline, "the settings above")


async def _reads(hass: HomeAssistant, entry: ConfigEntry, api: Any, seen: list,
                 get: Callable[[str], Any], keys: tuple[str, ...],
                 deadline: float | None, kept: str) -> dict[str, Any]:
    """`get(key)` for each of `keys`, a failure recorded as its row and
    masked before its cut (see _charge_server), until `deadline` on the
    loop's clock - _SWEEP_TIMEOUT_S from now when there is none. With no
    time left nothing is asked at all, and the row says so."""
    out: dict[str, Any] = {}
    cut = (f"the sweep passed {_SWEEP_TIMEOUT_S}s and was cut short; "
           f"{kept} are what came back in time")

    async def _sweep() -> None:
        for key in keys:
            seen.extend(_held(entry, api))
            try:
                out[key] = _clean(await hass.async_add_executor_job(get, key))
            except Exception as e:  # noqa: BLE001 - diagnostics must never raise
                said = hide_values(str(e), (*seen, *_held(entry, api)))
                out[key] = {"error": f"{type(e).__name__}: {said}"[:300]}

    loop = asyncio.get_running_loop()
    left = (_SWEEP_TIMEOUT_S if deadline is None else deadline - loop.time())
    if left <= 0:
        out["truncated"] = (f"the sweep passed {_SWEEP_TIMEOUT_S}s before "
                            "these were asked")
        return out
    try:
        await asyncio.wait_for(_sweep(), left)
    except (TimeoutError, asyncio.TimeoutError):
        out["truncated"] = cut
    return out


def _vehicle_token(entry: ConfigEntry, api: Any) -> dict[str, Any] | None:
    """Where the new platform's vehicle token is set, as three booleans, and
    whether the start-up probe found the car turned away.

    Not "pasted" or "derived": the options form used to pre-fill from the
    entry data, so a token in the options may be a derived one saved back. `in_use` is
    what the running client sends, None when setup never built one. With no
    token, `car_refused` True is the probe's verdict that the new gateway
    turns the car away (079001; on the cars seen so far such a car has no
    token at all, #106), which it says only at info, so a report could not
    tell it from the rest (#63, #84). False is no verdict of that kind: no
    known build matched, or the probe never got an answer. Like `in_use`, it
    is None when setup never built a client. A legacy entry has no such
    token.
    """
    if entry.data.get(CONF_PLATFORM, DEFAULT_PLATFORM) != PLATFORM_ZEEKR:
        return None
    return {
        "in_entry_data": bool(entry.data.get(CONF_ZEEKR_ENC_VIN)),
        "in_options": bool(entry.options.get(CONF_ZEEKR_ENC_VIN)),
        "in_use": bool(api.enc_vin) if hasattr(api, "enc_vin") else None,
        "car_refused": (bool(api.car_refused)
                        if hasattr(api, "car_refused") else None),
    }


def _polling(bundle: dict, coordinator: Any, held: tuple) -> dict[str, Any]:
    """How the poller is doing, in the terms the adaptive logic thinks in.
    The last error is masked (_held) before it is cut short."""
    poll = dict(bundle.get("poll_state") or {})
    # The change signature is an opaque hash of the fields that decide whether
    # anything moved. The streak counted from it is the readable part.
    poll.pop("sig", None)
    interval = getattr(coordinator, "update_interval", None)
    last = getattr(coordinator, "last_exception", None)
    return {
        "cycle": poll.get("cycle"),
        "unchanged_polls": poll.get("idle"),
        "force_secondary_pending": bool(poll.get("force_secondary")),
        "interval_seconds": (interval.total_seconds()
                             if interval is not None else None),
        "last_update_success": getattr(coordinator, "last_update_success", None),
        "last_exception": (hide_values(str(last), held) if last else "")[:300],
    }


def _logging() -> dict[str, Any]:
    import logging

    logger = logging.getLogger("custom_components.geely_connect")
    return {
        "effective_level": logging.getLevelName(logger.getEffectiveLevel()),
        "debug_enabled": logger.isEnabledFor(logging.DEBUG),
    }


def _card_status(hass: HomeAssistant) -> dict[str, Any]:
    from . import cards

    path = os.path.join(os.path.dirname(__file__), "geely-card.js")
    # Both shapes Lovelace has stored itself in, as cards.py reads it: on a
    # release that keeps a dict here the list came back empty, which reads
    # as "not registered" in exactly the report this block exists for.
    lovelace = hass.data.get("lovelace")
    resources = getattr(lovelace, "resources", None)
    if resources is None and isinstance(lovelace, dict):
        resources = lovelace.get("resources")
    listed = []
    if resources is not None and hasattr(resources, "async_items"):
        try:
            listed = [i.get("url") for i in resources.async_items() or []
                      if cards.CARD_URL in str(i.get("url", ""))]
        except Exception:  # noqa: BLE001 - diagnostics must never raise
            listed = ["<unreadable>"]
    return {
        "file_present": os.path.isfile(path),
        "url": cards.CARD_URL,
        "registered": bool(hass.data.get(f"{DOMAIN}_cards_registered")),
        "lovelace_resources": listed,
    }
