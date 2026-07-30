# Contributing

Thanks for your interest in improving Geely Connect!

## Reporting issues

Open an issue at
[YossiKon/geely-connect/issues](https://github.com/YossiKon/geely-connect/issues)
and include:

- Home Assistant version and integration version (HACS → Geely Connect)
- Your vehicle model and region/country
- The one-click **diagnostics** download (Settings → Devices & Services →
  Geely Connect → ⋮ → Download diagnostics) — secrets are masked automatically

## Development

The integration lives in `custom_components/geely_connect/` and follows the
[Home Assistant integration layout](https://developers.home-assistant.io/docs/creating_integration_file_structure):
one file per platform, a `config_flow.py`, `strings.json` +
`translations/`, and `manifest.json`.

To test a change, copy (or symlink) `custom_components/geely_connect/` into a
Home Assistant `config/custom_components/` folder and restart.

### Guidelines

- Match Home Assistant core conventions — this repo aims to be ready for a
  future submission as an official integration.
- User-facing text goes through `strings.json` and every file in
  `translations/` (en, he, ar, fr, ru) — never hard-code UI strings.
- Bump `version` in `manifest.json` on every release.
- CI runs [hassfest](https://developers.home-assistant.io/blog/2020/04/16/hassfest)
  and [HACS validation](https://hacs.xyz/docs/publish/action) on every push —
  keep both green.
