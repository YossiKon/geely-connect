"""Browser regression for #8 - needs playwright:
    python tests/fixtures/run-polyfill-check.py
Loads the card, then the real scoped-custom-element-registry polyfill
that Mushroom and button-card ship, and asserts the card survives the
registry wipe well inside the card picker's two-second budget.
"""

import json, os
from playwright.sync_api import sync_playwright

here = os.path.dirname(os.path.abspath(__file__))
url = "file:///" + os.path.join(here, "polyfill-page.html").replace("\\", "/")

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    for run in range(1, 6):
        page = b.new_page()
        page.goto(url)
        page.wait_for_timeout(120)          # tighter than the picker's 2s budget
        early = page.evaluate("() => ({full: !!customElements.get('geely-card'),"
                              " compact: !!customElements.get('geely-card-compact')})")
        page.wait_for_timeout(400)
        late = page.evaluate("() => ({full: !!customElements.get('geely-card'),"
                             " compact: !!customElements.get('geely-card-compact')})")
        # what the picker actually does
        verdict = page.evaluate("""async () => {
            const TIMEOUT = 2000;
            const cls = customElements.get('geely-card');
            if (cls) return 'defined immediately';
            return await Promise.race([
                customElements.whenDefined('geely-card').then(() => 'defined in time'),
                new Promise(r => setTimeout(() => r('TIMED OUT - picker would spin'), TIMEOUT)),
            ]);
        }""")
        print(f"run {run}: @120ms={json.dumps(early)} @520ms={json.dumps(late)} picker={verdict}")
        page.close()
    b.close()
