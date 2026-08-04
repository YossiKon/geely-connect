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


class _Resources:
    """Stand-in for Lovelace's ResourceStorageCollection."""

    def __init__(self, items=None, fail=None):
        self.items = list(items or [])
        self.fail = fail
        self.loaded = False
        self._next = 100

    async def async_get_info(self):
        self.loaded = True
        return {"resource_count": len(self.items)}

    def async_items(self):
        return self.items

    async def async_create_item(self, data):
        if self.fail:
            raise self.fail
        self._next += 1
        item = {"id": str(self._next), "url": data["url"], "type": data["res_type"]}
        self.items.append(item)
        return item

    async def async_update_item(self, item_id, changes):
        for item in self.items:
            if item["id"] == item_id:
                item.update(changes)
                return item
        raise KeyError(item_id)

    async def async_delete_item(self, item_id):
        self.items = [i for i in self.items if i["id"] != item_id]


class _Bus:
    def __init__(self):
        self.listeners = {}

    def async_listen_once(self, event, cb):
        self.listeners[event] = cb

    def fire(self, event):
        cb = self.listeners.get(event)
        if cb is not None:
            asyncio.run(cb(None))


class _Hass:
    def __init__(self, fail=False, resources=None, state=None):
        self.data = {}
        self.http = _Http(fail=fail)
        self.bus = _Bus()
        from homeassistant.core import CoreState
        self.state = state or CoreState.running
        if resources is not None:
            self.data["lovelace"] = types.SimpleNamespace(resources=resources)


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


# ----------------------------------------------------- lovelace resources ---
# A card delivered only through an extra-module URL is not awaited by
# Lovelace: if its class is not defined when the picker asks, the frontend's
# 2-second lookup rejects and the preview tile spins forever (and dashboards
# show "Configuration error"). Registering a resource is what removes the
# race, so these pin that path.

def test_the_card_is_registered_as_a_lovelace_resource():
    c = _cards()
    res = _Resources()
    hass = _Hass(resources=res)
    with _patched(c) as urls:
        asyncio.run(c.async_register_cards(hass))
    assert [i["url"] for i in res.items] == [f"{c.CARD_URL}?v=9.9.9"]
    assert res.items[0]["type"] == "module"
    assert res.loaded, "the collection must be loaded before it is read"
    assert urls == [f"{c.CARD_URL}?v=9.9.9"],         "the extra-module URL stays as the belt to the resource's braces"


def test_an_upgrade_moves_the_existing_resource_to_the_new_version():
    """Same URL with a stale version query means the browser keeps serving
    the previous release's card out of cache."""
    c = _cards()
    res = _Resources([{"id": "1", "url": f"{load('cards').CARD_URL}?v=1.0.0",
                       "type": "module"}])
    hass = _Hass(resources=res)
    with _patched(c):
        asyncio.run(c.async_register_cards(hass))
    assert [i["url"] for i in res.items] == [f"{c.CARD_URL}?v=9.9.9"]
    assert len(res.items) == 1, "an upgrade must not add a second entry"


def test_duplicate_resources_are_collapsed_to_one():
    """Two entries load the file twice and, worse, one of them can pin an old
    version forever."""
    c = _cards()
    url = load("cards").CARD_URL
    res = _Resources([{"id": "1", "url": f"{url}?v=1.0.0", "type": "module"},
                      {"id": "2", "url": url, "type": "module"}])
    hass = _Hass(resources=res)
    with _patched(c):
        asyncio.run(c.async_register_cards(hass))
    assert [i["url"] for i in res.items] == [f"{c.CARD_URL}?v=9.9.9"]


def test_an_already_current_resource_is_left_alone():
    c = _cards()
    res = _Resources([{"id": "1", "url": f"{load('cards').CARD_URL}?v=9.9.9",
                       "type": "module"}])
    hass = _Hass(resources=res)
    with _patched(c):
        asyncio.run(c.async_register_cards(hass))
    assert res.items == [{"id": "1", "url": f"{c.CARD_URL}?v=9.9.9",
                          "type": "module"}]


def test_yaml_mode_lovelace_falls_back_to_the_module_url():
    """A read-only resource collection must not fail setup - the card still
    loads, just without the resource guarantee."""
    c = _cards()
    for data in (None, {}, types.SimpleNamespace(resources=None),
                 types.SimpleNamespace(resources=object())):
        hass = _Hass()
        if data is not None:
            hass.data["lovelace"] = data
        with _patched(c) as urls:
            asyncio.run(c.async_register_cards(hass))
        assert urls == [f"{c.CARD_URL}?v=9.9.9"], data


def test_a_dict_shaped_lovelace_store_is_still_understood():
    """Older Home Assistant kept the collection in a plain dict."""
    c = _cards()
    res = _Resources()
    hass = _Hass()
    hass.data["lovelace"] = {"resources": res}
    with _patched(c):
        asyncio.run(c.async_register_cards(hass))
    assert len(res.items) == 1


def test_a_collection_that_refuses_the_write_degrades_quietly():
    c = _cards()
    res = _Resources(fail=RuntimeError("read-only"))
    hass = _Hass(resources=res)
    with _patched(c) as urls:
        asyncio.run(c.async_register_cards(hass))
    assert res.items == []
    assert urls == [f"{c.CARD_URL}?v=9.9.9"], "the fallback must still happen"


def test_a_collection_without_get_info_is_loaded_the_old_way():
    c = _cards()

    class _Old:
        """No async_get_info at all - the pre-2023 collection shape."""

        def __init__(self):
            self.items = []
            self.loaded = False

        async def async_load(self):
            self.loaded = True

        def async_items(self):
            return self.items

        async def async_create_item(self, data):
            self.items.append({"id": "1", "url": data["url"],
                               "type": data["res_type"]})

    res = _Old()
    hass = _Hass(resources=res)
    with _patched(c):
        asyncio.run(c.async_register_cards(hass))
    assert res.loaded, "the old collection was never loaded"
    assert len(res.items) == 1


def test_a_boot_that_beats_lovelace_retries_when_home_assistant_starts():
    """A config entry can be restored before lovelace is up. Without the
    retry the install stays on the racy path until the next restart."""
    from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
    from homeassistant.core import CoreState
    c = _cards()
    hass = _Hass(state=CoreState.starting)
    with _patched(c) as urls:
        asyncio.run(c.async_register_cards(hass))
        assert urls == [f"{c.CARD_URL}?v=9.9.9"]
        assert EVENT_HOMEASSISTANT_STARTED in hass.bus.listeners,             "no retry was scheduled"
        # Lovelace arrives late; the retry must pick it up.
        res = _Resources()
        hass.data["lovelace"] = types.SimpleNamespace(resources=res)
        hass.bus.fire(EVENT_HOMEASSISTANT_STARTED)
    assert [i["url"] for i in res.items] == [f"{c.CARD_URL}?v=9.9.9"]


def test_yaml_mode_says_out_loud_what_to_add_by_hand():
    """A silent debug line strands YAML-mode users on the racy path with no
    idea why the picker spins."""
    import logging
    c = _cards()
    hass = _Hass()   # no lovelace resources, already running
    seen = []

    class _Grab(logging.Handler):
        def emit(self, record):
            if record.levelno >= logging.WARNING:
                seen.append(record.getMessage())

    logger = logging.getLogger("gc.cards")
    h = _Grab()
    logger.addHandler(h)
    try:
        with _patched(c):
            asyncio.run(c.async_register_cards(hass))
    finally:
        logger.removeHandler(h)
    assert seen, "no warning for a read-only resource list"
    assert "resources" in seen[0] and c.CARD_URL in seen[0]


def test_the_startup_retry_warns_when_it_also_comes_up_empty():
    from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
    from homeassistant.core import CoreState
    import logging
    c = _cards()
    hass = _Hass(state=CoreState.starting)
    seen = []

    class _Grab(logging.Handler):
        def emit(self, record):
            if record.levelno >= logging.WARNING:
                seen.append(record.getMessage())

    logger = logging.getLogger("gc.cards")
    h = _Grab()
    logger.addHandler(h)
    try:
        with _patched(c):
            asyncio.run(c.async_register_cards(hass))
            assert seen == [], "a starting instance must wait for the retry"
            hass.bus.fire(EVENT_HOMEASSISTANT_STARTED)
    finally:
        logger.removeHandler(h)
    assert seen and c.CARD_URL in seen[0]


def test_a_running_instance_does_not_schedule_a_retry():
    from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
    c = _cards()
    hass = _Hass()
    with _patched(c):
        asyncio.run(c.async_register_cards(hass))
    assert EVENT_HOMEASSISTANT_STARTED not in hass.bus.listeners
