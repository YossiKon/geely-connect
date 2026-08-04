"""The auto-registered dashboard cards.

The promise on the box is "install the integration, get the cards" - so the
registration must happen exactly once, survive a broken frontend, and bust
the browser cache when the version changes.
"""
import asyncio
import os
import types

from conftest import PKG, have_homeassistant, load
from run import skip


def _cards():
    if not have_homeassistant():
        skip("homeassistant not installed")
    return load("cards")


class _Http:
    def __init__(self, fail=False):
        self.paths = []
        self.fail = fail

    async def async_register_static_paths(self, configs):
        if self.fail:
            raise OSError("static path route taken")
        self.paths.extend(configs)


class _Hass:
    def __init__(self, fail=False):
        self.data = {}
        self.http = _Http(fail=fail)


def _patched(c, version="9.9.9"):
    urls = []

    async def _integration(hass, domain):
        return types.SimpleNamespace(version=version)

    class _Ctx:
        def __enter__(self):
            self.orig = (c.add_extra_js_url, c.async_get_integration)
            c.add_extra_js_url = lambda hass, url, es5=False: urls.append(url)
            c.async_get_integration = _integration
            return urls

        def __exit__(self, *exc):
            c.add_extra_js_url, c.async_get_integration = self.orig
    return _Ctx()


def test_the_cards_register_once_with_a_version_busted_url():
    c = _cards()
    hass = _Hass()
    with _patched(c) as urls:
        asyncio.run(c.async_register_cards(hass))
        asyncio.run(c.async_register_cards(hass))
    assert len(urls) == 1, "a second entry must not re-register"
    assert urls[0] == f"{c.CARD_URL}?v=9.9.9", \
        "without the version query the old card survives every upgrade"
    (cfg,) = hass.http.paths
    assert cfg.url_path == c.CARD_URL
    assert os.path.isfile(cfg.path), "the served file must actually exist"


def test_a_broken_frontend_never_blocks_the_vehicle_setup():
    """Headless installs have no frontend; a card is decoration, the car is
    the point."""
    c = _cards()
    hass = _Hass(fail=True)
    with _patched(c) as urls:
        asyncio.run(c.async_register_cards(hass))
    assert urls == []
    assert not hass.data.get("geely_connect_cards_registered")
    with _patched(c) as urls:
        asyncio.run(c.async_register_cards(_Hass()))
    assert len(urls) == 1, "a later healthy setup must still register"


def test_the_shipped_card_defines_both_elements_and_the_picker_entries():
    """The JS is served verbatim - a rename there silently breaks every
    dashboard using the documented type names."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    src = open(os.path.join(PKG, "frontend", "geely-card.js"),
               encoding="utf-8").read()
    for needle in ('customElements.define("geely-card-compact"',
                   'customElements.define("geely-card"',
                   'type: "geely-card"', "window.customCards",
                   "_battery"):
        assert needle in src, needle
    assert "geely_connect" in src, "auto-detection must key off the platform"
