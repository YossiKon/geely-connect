# Test fixtures

`scoped-custom-element-registry.js` is the real
[@webcomponents/scoped-custom-element-registry](https://github.com/webcomponents/polyfills)
polyfill, vendored here because several popular Lovelace cards ship it and
loading it makes `customElements.get()` forget every element registered
earlier. That is what turned this integration's cards into permanent spinners
in the card picker (issue #8), and the watchdog that recovers from it is pinned
by `tests/test_cards_in_a_browser.py`.

It is a fixture rather than a dependency on purpose: the test has to reproduce
the exact behaviour that shipped in the wild, not whatever the polyfill does
next.

Two standalone runner scripts used to live here as well. They are gone: one had
no assertion at all, and neither was ever executed, because `tests/run.py`
collects only `test_*.py`. Their cases now live in the module above, which the
normal test command runs.
