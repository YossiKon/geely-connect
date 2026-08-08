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
        self.views = []
        self.fail = fail

    def register_view(self, view):
        if self.fail:
            raise OSError("no http server")
        self.views.append(view)


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

    async def async_add_executor_job(self, fn, *args):
        return fn(*args)

    def async_create_task(self, coro, *a, **k):
        # The card self-check is a fire-and-forget diagnostic; the tests do
        # not exercise the network, so close the coroutine cleanly.
        coro.close()
        return None


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



def _expected_url(c, version="9.9.9"):
    """The card URL carries a file timestamp after the version: Home
    Assistant's service worker keeps a CacheFirst copy of every file for 24
    hours, so only a URL it has never seen forces a fetch."""
    return f"{c.CARD_URL}?v={version}&m="


def test_the_cards_register_once_with_a_version_busted_url():
    c = _cards()
    hass = _Hass()
    with _patched(c) as urls:
        asyncio.run(c.async_register_cards(hass))
        asyncio.run(c.async_register_cards(hass))
    assert len(urls) == 1, "a second entry must not re-register"
    assert urls[0].startswith(_expected_url(c)), \
        "without the version query the old card survives every upgrade"
    (view,) = hass.http.views
    assert view.url == c.CARD_URL
    assert view.requires_auth is False,         "script tags carry no auth token - the view must be public"
    assert os.path.isfile(c._card_path()), "the served file must actually exist"


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
    src = open(os.path.join(PKG, "geely-card.js"), encoding="utf-8").read()
    for needle in ('defineOnce("geely-card-compact"',
                   'defineOnce("geely-card"',
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
    assert len(res.items) == 1 and res.items[0]["url"].startswith(_expected_url(c))
    assert res.items[0]["type"] == "module"
    assert res.loaded, "the collection must be loaded before it is read"
    assert urls == [], (
        "with the resource in place the extra-module URL must NOT be added: "
        "as an extra script this file runs before every Lovelace resource, "
        "including the scoped-registry polyfill that hides earlier "
        "registrations")


def test_an_upgrade_moves_the_existing_resource_to_the_new_version():
    """Same URL with a stale version query means the browser keeps serving
    the previous release's card out of cache."""
    c = _cards()
    res = _Resources([{"id": "1", "url": f"{load('cards').CARD_URL}?v=1.0.0",
                       "type": "module"}])
    hass = _Hass(resources=res)
    with _patched(c):
        asyncio.run(c.async_register_cards(hass))
    assert len(res.items) == 1 and res.items[0]["url"].startswith(_expected_url(c))
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
    assert len(res.items) == 1 and res.items[0]["url"].startswith(_expected_url(c))


def test_an_already_current_resource_is_left_alone():
    c = _cards()
    current = f"{c.CARD_URL}?v=9.9.9&m={c._card_mtime(c._card_path())}"
    res = _Resources([{"id": "1", "url": current, "type": "module"}])
    hass = _Hass(resources=res)
    with _patched(c):
        asyncio.run(c.async_register_cards(hass))
    assert res.items == [{"id": "1", "url": current, "type": "module"}]


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
        assert len(urls) == 1 and urls[0].startswith(_expected_url(c)), data


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
    assert len(urls) == 1 and urls[0].startswith(_expected_url(c)), "the fallback must still happen"


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
        assert len(urls) == 1 and urls[0].startswith(_expected_url(c))
        assert EVENT_HOMEASSISTANT_STARTED in hass.bus.listeners,             "no retry was scheduled"
        # Lovelace arrives late; the retry must pick it up.
        res = _Resources()
        hass.data["lovelace"] = types.SimpleNamespace(resources=res)
        hass.bus.fire(EVENT_HOMEASSISTANT_STARTED)
    assert len(res.items) == 1 and res.items[0]["url"].startswith(_expected_url(c))


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


def test_a_missing_card_file_is_an_error_not_a_silent_absence():
    """A partial download leaves the vehicle working and the cards gone, with
    "Custom element not found" in the browser and nothing in the log tying the
    two together."""
    import logging
    c = _cards()
    hass = _Hass()
    seen = []

    class _Grab(logging.Handler):
        def emit(self, record):
            if record.levelno >= logging.ERROR:
                seen.append(record.getMessage())

    logger = logging.getLogger("gc.cards")
    logger.addHandler(_Grab())
    orig = c.os.path.isfile
    c.os.path.isfile = lambda p: False
    try:
        with _patched(c) as urls:
            asyncio.run(c.async_register_cards(hass))
    finally:
        c.os.path.isfile = orig
        logger.handlers = [h for h in logger.handlers if not isinstance(h, _Grab)]
    assert seen and "missing" in seen[0].lower()
    assert urls == [], "nothing may be advertised that cannot be served"
    assert hass.http.views == [], "no route for a file that is not there"
    assert not hass.data.get("geely_connect_cards_registered"),         "a re-download plus reload must be able to retry"


def test_the_card_file_sits_beside_the_python_not_in_a_subdirectory():
    """A subdirectory is one more thing an install path can miss, and when it
    does the only symptom is "Custom element not found" in a browser."""
    if not have_homeassistant():
        skip("homeassistant not installed")
    assert os.path.isfile(os.path.join(PKG, "geely-card.js"))
    assert not os.path.isdir(os.path.join(PKG, "frontend")),         "the card moved to the package root - drop the empty subdirectory"


# ------------------------------------------------------ the served-URL check ---
# "Custom element not found: geely-card" while the file is on disk and the
# resource is registered leaves only one question - does Home Assistant
# actually hand the file over when asked. These pin the answer to a log line.

class _Reply:
    def __init__(self, status=200, ctype="text/javascript", body=b"/* card */"):
        self.status = status
        self.headers = {"Content-Type": ctype}
        self._body = body

    @property
    def content(self):
        parent = self

        class _C:
            async def read(self, n):
                return parent._body[:n]
        return _C()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    def __init__(self, reply=None, boom=None):
        self.reply = reply or _Reply()
        self.boom = boom
        self.urls = []

    def get(self, url, timeout=None):
        self.urls.append(url)
        if self.boom:
            raise self.boom
        return self.reply


def _selfcheck(c, hass, session, url="/geely_connect/geely-card.js?v=1"):
    import homeassistant.helpers.aiohttp_client as ac
    orig = ac.async_get_clientsession
    ac.async_get_clientsession = lambda h, verify_ssl=True: session
    seen = []
    import logging

    class _Grab(logging.Handler):
        def emit(self, record):
            seen.append((record.levelno, record.getMessage()))

    logger = logging.getLogger("gc.cards")
    handler = _Grab()
    logger.addHandler(handler)
    try:
        asyncio.run(c._async_verify_served(hass, url))
    finally:
        ac.async_get_clientsession = orig
        logger.removeHandler(handler)
    return seen


def test_a_served_card_passes_the_self_check_quietly():
    import types
    c = _cards()
    hass = _Hass()
    hass.config = types.SimpleNamespace(internal_url="http://ha.local:8123")
    session = _Session()
    seen = _selfcheck(c, hass, session)
    assert session.urls == ["http://ha.local:8123/geely_connect/geely-card.js?v=1"]
    import logging
    assert not [m for lvl, m in seen if lvl >= logging.ERROR]


def test_the_home_assistant_ui_coming_back_instead_of_the_card_is_an_error():
    """The failure that produces "Custom element not found" in a browser: the
    URL answers with something that is not JavaScript."""
    import logging
    import types
    c = _cards()
    hass = _Hass()
    hass.config = types.SimpleNamespace(internal_url=None)
    hass.http.ssl_certificate = None
    hass.http.server_port = 8123
    session = _Session(_Reply(status=404, ctype="text/html", body=b"<!DOCTYPE html>"))
    seen = _selfcheck(c, hass, session)
    assert session.urls[0].startswith("http://127.0.0.1:8123/")
    errors = [m for lvl, m in seen if lvl >= logging.ERROR]
    assert errors and "Custom element not found" in errors[0]


def test_an_https_only_instance_is_probed_over_https():
    import types
    c = _cards()
    hass = _Hass()
    hass.config = types.SimpleNamespace(internal_url="")
    hass.http.ssl_certificate = "/etc/cert.pem"
    hass.http.server_port = 8123
    session = _Session()
    _selfcheck(c, hass, session)
    assert session.urls[0].startswith("https://127.0.0.1:8123/")


def test_a_self_check_that_cannot_run_stays_silent():
    """It is a diagnostic; a network hiccup must not add noise or raise."""
    import logging
    import types
    c = _cards()
    hass = _Hass()
    hass.config = types.SimpleNamespace(internal_url="http://ha.local:8123")
    seen = _selfcheck(c, hass, _Session(boom=OSError("no route")))
    assert not [m for lvl, m in seen if lvl >= logging.WARNING]


# --------------------------------------------------------------- the view ---
# A static path pins the file location at registration time; a HACS update
# that moves files plus an entry reload left the route serving a deleted path
# while diagnostics truthfully said the (new) file exists. The view resolves
# per request, so these pin its behavior.

class _Req:
    def __init__(self, hass):
        self.app = {"hass": hass}


def test_the_view_serves_the_current_file_with_no_cache():
    c = _cards()
    hass = _Hass()
    resp = asyncio.run(c.GeelyCardView().get(_Req(hass)))
    assert resp.status == 200
    assert resp.content_type == "text/javascript"
    assert resp.headers["Cache-Control"] == "no-cache",         "a cached card survives every upgrade until a hard refresh"
    assert b"geely-card" in resp.body


def test_the_view_reports_a_vanished_file_as_404_not_a_crash():
    """The exact field failure: files replaced underneath a live route."""
    c = _cards()
    hass = _Hass()
    orig = c._card_path
    c._card_path = lambda: orig() + ".definitely-not-there"
    try:
        resp = asyncio.run(c.GeelyCardView().get(_Req(hass)))
    finally:
        c._card_path = orig
    assert resp.status == 404


def test_a_file_with_no_timestamp_still_produces_a_url():
    """A filesystem that refuses stat (odd container mounts) must cost the
    cache-busting suffix, not the card."""
    c = _cards()
    assert c._card_mtime(c._card_path() + ".nope") == 0
    assert c._card_mtime(c._card_path()) > 0


# ------------------------------------- the driving lock, on both sides ------

def test_the_cards_and_the_poller_agree_on_what_driving_means():
    """The card greys out every control while the car is moving, and it decides
    that with its own copy of the rule - a browser cannot import Python. Two
    copies drift, and the failure would be silent in both directions: buttons
    that stay live at 60 km/h, or a card locked solid on a parked car.

    Speed is checked separately in the browser tests; this pins the enum, which
    exists because speed legitimately reads 0 at every red light (#21)."""
    import io
    import re

    src = io.open(os.path.join(PKG, "geely-card.js"), encoding="utf-8").read()
    block = src.split("const DRIVING_STATES = new Set([", 1)[1].split("]", 1)[0]
    in_card = {s for s in re.findall(r'"([^"]+)"', block)}

    body = io.open(os.path.join(PKG, "__init__.py"), encoding="utf-8").read()
    py_block = body.split("_ENGINE_RUNNING = frozenset({", 1)[1].split("}", 1)[0]
    in_python = {s for s in re.findall(r'"([^"]+)"', py_block)}

    assert in_python, "the poller's engine-state set could not be read"
    missing = in_python - in_card
    assert not missing, (
        f"the poller treats {sorted(missing)} as running and the cards do not - "
        "a car in that state would keep every button live while moving"
    )
    # The card is allowed the mapped display value the sensor actually shows.
    extra = in_card - in_python - {"running"}
    assert not extra, f"the cards call {sorted(extra)} driving and the poller does not"


def _card_class_body(src, name):
    """One card class, ending at its own closing brace.

    Slicing to the next `class` keyword instead would run past the last card and
    swallow the registry watchdog that sits between it and the status tile - so a
    check for "this class does not add listeners" would see the watchdog's two
    and fail on the innocent card.
    """
    lines = src.split("\n")
    start = next(i for i, l in enumerate(lines)
                 if l.startswith(f"  class {name} extends"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "  }")
    return "\n".join(lines[start:end + 1])


def test_every_card_class_leaves_the_driving_lock_to_the_base():
    """The banner and the lock are applied in one place for all five cards. A
    card that built its own wiring instead of calling _wire() would draw the
    banner and leave working buttons underneath it."""
    import io

    src = io.open(os.path.join(PKG, "geely-card.js"), encoding="utf-8").read()
    for cls in ("GeelyCard", "GeelyCardTop", "GeelyCardCompact",
                "GeelyCardMini", "GeelyCardStrip"):
        body = _card_class_body(src, cls)
        assert "this._wire()" in body, f"{cls} never calls _wire()"
        assert "addEventListener" not in body, (
            f"{cls} wires its own listeners, bypassing the driving lock")
        # The three larger cards draw the banner; the two one-row cards say it on
        # their status line, because a banner would double their height.
        if cls in ("GeelyCard", "GeelyCardTop", "GeelyCardCompact"):
            assert "_drivingNotice()" in body, f"{cls} has no driving banner"
        else:
            assert "actions locked" in body, f"{cls} never says actions are locked"


# ------------------------------------- the one layer nothing was testing ------
# Fifty-eight browser tests drive geely-card.js and every one of them injects the
# file's contents. Nothing exercised the HTTP route that puts it in front of a
# browser - and if that route fails, the symptom is total and confusing: no
# element defined, every card missing from the picker, and "Configuration error"
# on the dashboard, with the file sitting correctly on disk the whole time.

class _FakeRequest:
    """Enough of an aiohttp request for the view: an app that resolves hass."""

    def __init__(self, hass, key=None):
        self.app = {"hass": hass} if key is None else {key: hass, "hass": hass}


class _ExecutorHass:
    def __init__(self):
        self.data = {}

    async def async_add_executor_job(self, fn, *args):
        return fn(*args)


def test_the_route_serves_the_card_file_itself():
    """Not a static path: the view re-reads the file on every request, so a HACS
    update that replaces it reaches browsers without a restart."""
    cards = _cards()
    import asyncio
    import os
    view = cards.GeelyCardView()
    resp = asyncio.run(view.get(_FakeRequest(_ExecutorHass())))
    on_disk = open(os.path.join(PKG, "geely-card.js"), "rb").read()
    assert resp.status == 200
    assert resp.body == on_disk, "the route served something other than the file"
    assert "javascript" in resp.content_type
    # The frontend's service worker keeps a 24-hour CacheFirst copy of every
    # unmatched path, so the header is not decoration.
    assert resp.headers.get("Cache-Control") == "no-cache"


def test_the_route_still_finds_hass_the_way_home_assistant_stores_it():
    """Home Assistant keeps hass on the aiohttp app under a typed AppKey *and*
    under the plain string "hass", the latter explicitly "for backwards
    compatibility". This view uses the string. If that compatibility shim is ever
    dropped, every request here becomes a 500 and every card vanishes with nothing
    in the log tying it to the frontend - so pin it."""
    cards = _cards()
    import asyncio
    from homeassistant.components.http import KEY_HASS
    view = cards.GeelyCardView()
    # Exactly as the real app is populated.
    resp = asyncio.run(view.get(_FakeRequest(_ExecutorHass(), key=KEY_HASS)))
    assert resp.status == 200 and resp.body


def test_a_missing_file_gives_an_honest_404_rather_than_a_500():
    """Whoever hits the URL by hand while chasing a missing card deserves to be
    told what is wrong, not handed a stack trace."""
    cards = _cards()
    import asyncio
    view = cards.GeelyCardView()
    orig = cards._card_path
    cards._card_path = lambda: "/nowhere/geely-card.js"
    try:
        resp = asyncio.run(view.get(_FakeRequest(_ExecutorHass())))
    finally:
        cards._card_path = orig
    assert resp.status == 404
    assert "missing" in resp.text.lower()


def test_the_route_is_public_because_a_script_tag_carries_no_token():
    cards = _cards()
    assert cards.GeelyCardView.requires_auth is False
    assert cards.GeelyCardView.url == cards.CARD_URL
