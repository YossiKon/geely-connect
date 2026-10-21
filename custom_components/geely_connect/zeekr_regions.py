"""European routing with app keys supplied locally by the installation owner.

No European app signing or VIN-encryption keys are distributed here. The
existing upstream SEA and legacy implementations retain their own behavior.
"""
from dataclasses import dataclass, field
import json
from pathlib import Path
import re

from .const import ZEEKR_REGION_EU, ZEEKR_REGION_SEA

KEYS_FILENAME = "geely_eu_app_keys.json"
_FIELDS = frozenset({
    "hmac_access_key", "hmac_secret", "snc_secret", "vin_key_hex", "vin_iv_hex",
})


class RegionKeysError(ValueError):
    """Missing or invalid local app keys; messages never contain their values."""


@dataclass(frozen=True)
class ZeekrRegion:
    area: str
    idaas_gateway: str
    idaas_path: str
    hmac_access_key: str = field(repr=False)
    hmac_secret: str = field(repr=False)
    msg_app_id: str
    msg_client_id: str
    tsp_gateway: str
    snc_project_id: str
    snc_secret: str = field(repr=False)
    hf_gateway: str
    vin_key: bytes = field(repr=False)
    vin_iv: bytes = field(repr=False)
    app_version: str


def profile_from_keys(data: object) -> ZeekrRegion:
    """Validate a local JSON object without accepting host or path overrides."""
    if not isinstance(data, dict) or set(data) != _FIELDS:
        raise RegionKeysError("Local EU app keys have missing or unexpected fields")
    for name in _FIELDS:
        value = data[name]
        if not isinstance(value, str) or not re.fullmatch(r"[\x21-\x7e]{1,256}", value):
            raise RegionKeysError("Local EU app keys contain an invalid value")
    try:
        # Hex encoding represents the original bytes, not a text key that merely
        # looks hexadecimal. Reject whitespace and odd-length representations.
        for name in ("vin_key_hex", "vin_iv_hex"):
            if not re.fullmatch(r"(?:[0-9a-fA-F]{2})+", data[name]):
                raise ValueError
        key = bytes.fromhex(data["vin_key_hex"])
        iv = bytes.fromhex(data["vin_iv_hex"])
        if len(key) not in (16, 24, 32) or len(iv) != 16:
            raise ValueError
    except ValueError:
        raise RegionKeysError("Local EU app keys have invalid AES lengths or encoding") from None
    return ZeekrRegion(
        area="EU",
        idaas_gateway="https://gateway-pub-azure.zeekr.eu",
        idaas_path="zeekr-cuc-idaas",
        hmac_access_key=data["hmac_access_key"],
        hmac_secret=data["hmac_secret"],
        msg_app_id="10027",
        msg_client_id="1026",
        tsp_gateway="https://eu-snc-tsp-api-gw.zeekrlife.com",
        snc_project_id="GEELY_EU",
        snc_secret=data["snc_secret"],
        hf_gateway="https://api.ecloudeu.com",
        vin_key=key,
        vin_iv=iv,
        app_version="1.1.1",
    )


def load_region_profile(config_dir: str, region: str) -> ZeekrRegion | None:
    """Load the explicitly selected server profile, never infer it from country."""
    if region == ZEEKR_REGION_SEA:
        return None
    if region != ZEEKR_REGION_EU:
        raise RegionKeysError("Unsupported new-app server region")
    path = Path(config_dir) / KEYS_FILENAME
    try:
        with path.open("rb") as source:
            raw = source.read(4097)
        if len(raw) > 4096:
            raise RegionKeysError("Local EU app keys file is too large")
        data = json.loads(raw)
    except RegionKeysError:
        raise
    except FileNotFoundError:
        raise RegionKeysError(
            f"European setup requires {KEYS_FILENAME} in the Home Assistant "
            "configuration directory; see docs/LOCAL-EU-KEYS.md"
        ) from None
    except (OSError, ValueError):
        raise RegionKeysError(
            f"Cannot read valid JSON from {KEYS_FILENAME}"
        ) from None
    return profile_from_keys(data)
