"""App credentials for an original-login region this integration does not ship,
supplied locally by the installation owner (#105).

A regional backend signs every request with its own app id and secret. The UAE
pair ships inside the official app, decrypted at run time by a white-box native
library, and is published nowhere. An owner who has it can put it in a file
beside configuration.yaml; it is read when the entry is set up and kept in
memory, and is never written into the config entry, a log line or a
diagnostics download. The servers come from code only: the file names a
region, never a host. See docs/LOCAL-REGION-KEYS.md.
"""
import json
from pathlib import Path
import re

from .const import LOCAL_KEY_REGIONS

KEYS_FILENAME = "geely_region_keys.json"
_FIELDS = frozenset({"region", "app_id", "app_secret"})
_MAX_BYTES = 4096
_VALUE = re.compile(r"[\x21-\x7e]{1,256}")


class LocalRegionKeysError(ValueError):
    """Missing or invalid local region keys; messages never contain their values."""


def backend_from_keys(data: object, region: str) -> dict[str, str]:
    """Validate the file's object for `region` and return the backend config,
    in the shape const.REGIONS uses plus the area's own login-code host."""
    if not isinstance(data, dict) or set(data) != _FIELDS:
        raise LocalRegionKeysError(
            f"{KEYS_FILENAME} must hold exactly region, app_id and app_secret")
    for name in sorted(_FIELDS):
        value = data[name]
        if not isinstance(value, str) or not _VALUE.fullmatch(value):
            raise LocalRegionKeysError(f"{KEYS_FILENAME} has an invalid {name}")
    if data["region"].upper() != region:
        raise LocalRegionKeysError(
            f"{KEYS_FILENAME} holds keys for another region, not {region}")
    hosts = LOCAL_KEY_REGIONS[region]
    return {
        "app_id": data["app_id"],
        "app_secret": data["app_secret"],
        "cert_host": hosts["cert_host"],
        "control_host": hosts["control_host"],
        "mint_host": hosts["mint_host"],
    }


def load_region_backend(config_dir: str, region: str) -> dict[str, str]:
    """The backend for an area in const.LOCAL_KEY_REGIONS, from the owner's file.

    Blocking file I/O: call it from an executor job."""
    if region not in LOCAL_KEY_REGIONS:
        raise LocalRegionKeysError(f"no local keys are read for region {region}")
    path = Path(config_dir) / KEYS_FILENAME
    try:
        with path.open("rb") as source:
            raw = source.read(_MAX_BYTES + 1)
        if len(raw) > _MAX_BYTES:
            raise LocalRegionKeysError(f"{KEYS_FILENAME} is too large")
        data = json.loads(raw)
    except LocalRegionKeysError:
        raise
    except FileNotFoundError:
        raise LocalRegionKeysError(
            f"region {region} needs its app credentials in {KEYS_FILENAME} in "
            "the Home Assistant configuration directory; see "
            "docs/LOCAL-REGION-KEYS.md") from None
    except (OSError, ValueError):
        raise LocalRegionKeysError(
            f"cannot read valid JSON from {KEYS_FILENAME}") from None
    return backend_from_keys(data, region)
