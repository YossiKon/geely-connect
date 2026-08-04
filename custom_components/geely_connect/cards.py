"""Serve and auto-register the dashboard cards that ship with the integration.

The cards live in frontend/geely-card.js and appear in the Lovelace card
picker as `custom:geely-card` and `custom:geely-card-compact` the moment the
integration is set up - no HACS frontend package, no manual resource entry.
The mechanism is the one card_mod and browser_mod use: serve the file from
the integration's own directory, then ask the frontend to load it on every
dashboard.
"""
from __future__ import annotations

import logging
import os

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

CARD_URL = f"/{DOMAIN}/geely-card.js"
_REGISTERED = f"{DOMAIN}_cards_registered"


async def async_register_cards(hass: HomeAssistant) -> None:
    """Idempotent and best-effort: a frontend hiccup, or a headless install
    with no frontend at all, must never block the vehicle from setting up."""
    if hass.data.get(_REGISTERED):
        return
    # Claim before the first await: two vehicles setting up concurrently must
    # not both try to register the same static path.
    hass.data[_REGISTERED] = True
    try:
        path = os.path.join(os.path.dirname(__file__), "frontend", "geely-card.js")
        await hass.http.async_register_static_paths(
            [StaticPathConfig(CARD_URL, path, cache_headers=True)]
        )
        integration = await async_get_integration(hass, DOMAIN)
        # The version query busts browser caches on upgrade; without it the
        # previous release's card survives every restart until a hard refresh.
        add_extra_js_url(hass, f"{CARD_URL}?v={integration.version}")
        _LOGGER.debug("Dashboard cards registered at %s", CARD_URL)
    except Exception as e:  # noqa: BLE001
        # Release the claim so a later reload can retry.
        hass.data[_REGISTERED] = False
        _LOGGER.warning("Could not register the dashboard cards (non-fatal): %s", e)
