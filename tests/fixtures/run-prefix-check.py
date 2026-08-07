"""Browser regression for #16 - needs playwright:
    python tests/fixtures/run-prefix-check.py

The card finds its vehicle by looking for a geely_connect sensor whose id ends
in _battery. sensor.<prefix>_12v_battery ends in _battery too, so whichever the
entity registry happened to list first won - and a card bound to the 12V
auxiliary battery looks entirely plausible, because a 12V battery sits at a
rock-steady 97-100%. This feeds the registry the 12V entity FIRST and asserts
the card still binds to the pack and shows the pack's percentage.
"""
import json, os
from playwright.sync_api import sync_playwright
here = os.path.dirname(os.path.abspath(__file__))
CARD = os.path.join(here, "..", "..", "custom_components", "geely_connect", "geely-card.js")
html = """<!DOCTYPE html><html><body>
<geely-card id="c"></geely-card>
<script src="../../custom_components/geely_connect/geely-card.js"></script>
<script>
const S = {};
const put = (id, state, attrs = {}) => { S[id] = { entity_id: id, state, attributes: attrs }; };
put("sensor.my_car_12v_battery", "99", { unit_of_measurement: "%" });
put("sensor.my_car_battery", "61", { unit_of_measurement: "%" });
put("sensor.my_car_electric_range", "256", { unit_of_measurement: "km" });
const hass = { states: S, entities: {
  "sensor.my_car_12v_battery": { platform: "geely_connect", device_id: "d1" },
  "sensor.my_car_battery": { platform: "geely_connect", device_id: "d1" },
}, devices: { d1: { name: "Geely EX5" } }, callService: () => {} };
const el = document.getElementById("c");
el.setConfig({}); el.hass = hass;
window.__prefix = el._prefix;
</script></body></html>"""
open(os.path.join(here, "prefix-12v.html"), "w", encoding="utf-8").write(html)
with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    page = b.new_page()
    page.goto("file:///" + os.path.join(here, "prefix-12v.html").replace("\\", "/"))
    page.wait_for_timeout(800)
    prefix = page.evaluate("() => window.__prefix")
    pct = page.evaluate("() => { const el = document.getElementById('c').shadowRoot.querySelector('.head .micro'); return el && el.textContent.trim(); }")
    print("prefix:", prefix, "| header pct:", pct)
    assert prefix == "my_car", f"bound to wrong prefix: {prefix}"
    assert pct == "61%", f"hero shows the wrong battery: {pct}"
    print("PASS: card binds to the pack, not the 12V battery")
    b.close()
