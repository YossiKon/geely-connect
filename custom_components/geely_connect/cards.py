"""Serve and auto-register the dashboard cards that ship with the integration.

The cards live in geely-card.js, beside this file, and appear in the Lovelace card
picker as `custom:geely-card` and `custom:geely-card-compact` the moment the
integration is set up - no HACS frontend package, no manual resource entry.

Delivery is a Lovelace *resource* wherever possible, and only falls back to
`add_extra_js_url`. The distinction matters: Lovelace awaits its resources
before it builds any card, while extra-module URLs are merely injected. A
card whose class is not defined when the picker asks for it loses a 2-second
race inside the frontend's `getCardElementClass`, whose rejected promise
leaves the picker's preview tile spinning forever - and renders as
"Configuration error" on a dashboard. Desktops with a warm cache usually win
that race; phones and fresh WebViews lose it, which is exactly the split the
bug reports showed.
"""
from __future__ import annotations

import logging
import os

from aiohttp import ClientTimeout, web

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import HomeAssistantView
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant
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
        # Deliberately beside this module rather than in a subdirectory:
        # every install path that delivers the .py files delivers a sibling
        # file too, and a card that never arrives is invisible until someone
        # reads a browser console.
        path = _card_path()
        if not await hass.async_add_executor_job(os.path.isfile, path):
            # A partial download (HACS interrupted, a manual copy that missed
            # the subdirectory) leaves the integration working and the cards
            # silently absent - "Custom element not found: geely-card" in the
            # browser, with nothing in the log to connect it to.
            hass.data[_REGISTERED] = False
            _LOGGER.error(
                "The dashboard card file is missing from this installation "
                "(%s). The vehicle works, but the Geely cards cannot load. "
                "Re-download the integration in HACS (or copy the whole "
                "custom_components/geely_connect folder if you installed by "
                "hand) and restart Home Assistant.", path,
            )
            return
        # A view, not a static path, and the difference is not cosmetic: a
        # static path pins the file location as it was at registration time.
        # A HACS update that moves or replaces files, followed by an entry
        # reload rather than a full restart, left the old route serving a
        # deleted path - HTTP 404, "Custom element not found: geely-card" in
        # the browser, while diagnostics truthfully reported the new file
        # present. The view resolves the file on every request, and no-cache
        # means an updated card reaches browsers without any cache dance.
        hass.http.register_view(GeelyCardView())
        integration = await async_get_integration(hass, DOMAIN)
        # The version query busts browser caches on upgrade; without it the
        # previous release's card survives every restart until a hard refresh.
        url = f"{CARD_URL}?v={integration.version}"
        # Both paths, deliberately: the resource is what Lovelace awaits, the
        # extra-module URL covers panels that never read the resource list.
        # They carry the same URL, so the browser's module map runs the file
        # exactly once either way.
        registered = await _async_register_resource(hass, url)
        if not registered:
            # Only as a fallback. As an extra script this file runs before
            # every Lovelace resource - including the scoped-registry
            # polyfill that some popular cards ship, which replaces
            # window.customElements and hides anything registered earlier.
            # Loading as a resource puts us after that swap; the extra-module
            # URL stays for installs whose resource list is read-only.
            add_extra_js_url(hass, url)
        if not registered and hass.state is not CoreState.starting:
            # Nothing else will retry, so say plainly what is missing and how
            # to add it by hand. Without the resource the card can lose the
            # frontend's 2-second lookup race and the picker spins forever.
            _LOGGER.warning(
                "Could not add the Geely cards to Lovelace's resources "
                "(YAML-mode dashboards keep that list read-only). The cards "
                "may not appear in the card picker. Add this to your "
                "configuration.yaml under lovelace: resources: "
                "- url: %s / type: module", url,
            )
        if not registered and hass.state is CoreState.starting:
            # Lovelace may simply not be set up yet - this entry can be
            # restored before it. Try once more when the boot is complete,
            # otherwise the install runs on the racy path until next restart.
            async def _retry(_event) -> None:
                if await _async_register_resource(hass, url):
                    _LOGGER.debug("Card resource registered after startup")
                else:
                    _LOGGER.warning(
                        "Could not add the Geely cards to Lovelace's "
                        "resources (YAML-mode dashboards keep that list "
                        "read-only). The cards may not appear in the card "
                        "picker. Add this to your configuration.yaml under "
                        "lovelace: resources: - url: %s / type: module", url,
                    )

            hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _retry)
        # INFO, not debug: when a card does not show up this one line answers
        # "is it even being served, and from where" without a log-level dance.
        _LOGGER.info("Geely dashboard cards served at %s (Lovelace resource: %s)",
                     url, registered)
        hass.async_create_task(_async_verify_served(hass, url))
    except Exception as e:  # noqa: BLE001
        # Release the claim so a later reload can retry.
        hass.data[_REGISTERED] = False
        _LOGGER.warning("Could not register the dashboard cards (non-fatal): %s", e)


async def _async_register_resource(hass: HomeAssistant, url: str) -> bool:
    """Add (or update) the card as a Lovelace resource. False if impossible.

    Impossible means YAML-mode Lovelace, whose resource list is read-only, or
    a Home Assistant that stores resources somewhere this code does not know -
    both fall back to the extra-module URL rather than failing setup.
    """
    data = hass.data.get("lovelace")
    resources = getattr(data, "resources", None)
    if resources is None and isinstance(data, dict):
        resources = data.get("resources")
    if resources is None or not hasattr(resources, "async_create_item"):
        return False
    try:
        if hasattr(resources, "async_get_info"):
            await resources.async_get_info()
        elif getattr(resources, "loaded", True) is False:
            await resources.async_load()

        existing = [
            item for item in resources.async_items() or []
            if str(item.get("url", "")).split("?")[0] == CARD_URL
        ]
        if not existing:
            await resources.async_create_item({"res_type": "module", "url": url})
            _LOGGER.info("Registered the Geely dashboard cards as a Lovelace resource")
            return True
        # Keep exactly one, and keep its version query current so an upgrade
        # is not served from the browser cache.
        keep, *duplicates = existing
        for dupe in duplicates:
            await resources.async_delete_item(dupe["id"])
        if keep.get("url") != url:
            await resources.async_update_item(keep["id"], {"url": url})
            _LOGGER.debug("Updated the card resource to %s", url)
        return True
    except Exception as e:  # noqa: BLE001
        _LOGGER.debug("Lovelace resource registration unavailable (%s); "
                      "falling back to an extra module URL", e)
        return False


async def _async_verify_served(hass: HomeAssistant, url: str) -> None:
    """Fetch our own card URL and say what came back.

    "Custom element not found: geely-card" in a browser has several possible
    causes and the user cannot see any of them. This turns the important half
    into one log line: either Home Assistant serves the file to itself, or it
    does not and the reason is right there.
    """
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    try:
        base = (hass.config.internal_url or "").rstrip("/")
        if not base:
            scheme = "https" if getattr(hass.http, "ssl_certificate", None) else "http"
            base = f"{scheme}://127.0.0.1:{hass.http.server_port}"
        session = async_get_clientsession(hass, verify_ssl=False)
        async with session.get(f"{base}{url}", timeout=ClientTimeout(total=10)) as resp:
            body = await resp.content.read(64)
            ctype = resp.headers.get("Content-Type", "?")
            if resp.status == 200 and "javascript" in ctype:
                _LOGGER.debug("Card self-check OK (%s, %s)", resp.status, ctype)
                return
            _LOGGER.error(
                "The Geely card is registered but Home Assistant serves %s "
                "as HTTP %s (%s) instead of JavaScript, so the browser cannot "
                "load it and dashboards will say \"Custom element not found: "
                "geely-card\". First bytes: %r", url, resp.status, ctype, body,
            )
    except Exception as e:  # noqa: BLE001 - a diagnostic must never break setup
        _LOGGER.debug("Card self-check could not run (%s)", e)


def _card_path() -> str:
    return os.path.join(os.path.dirname(__file__), "geely-card.js")


class GeelyCardView(HomeAssistantView):
    """Serve geely-card.js, resolved fresh on every request.

    No auth: the frontend fetches Lovelace resources as plain script tags,
    which carry no token. The file is public UI code.
    """

    url = CARD_URL
    name = f"{DOMAIN}:card"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        def _read() -> bytes | None:
            try:
                with open(_card_path(), "rb") as fh:
                    return fh.read()
            except OSError:
                return None

        body = await request.app["hass"].async_add_executor_job(_read)
        if body is None:
            return web.Response(status=404, text="geely-card.js is missing "
                                "from the integration directory")
        return web.Response(
            body=body,
            content_type="text/javascript",
            charset="utf-8",
            headers={"Cache-Control": "no-cache"},
        )
