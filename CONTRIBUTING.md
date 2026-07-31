# Contributing

Thanks for your interest in improving Geely Connect!

## Reporting issues

Open an issue at
[YossiKon/geely-connect/issues](https://github.com/YossiKon/geely-connect/issues)
and include:

- Home Assistant version and integration version (HACS → Geely Connect)
- Your vehicle model and country
- The one-click **diagnostics** download (Settings → Devices & Services →
  Geely Connect → ⋮ → Download diagnostics) — secrets are masked automatically

If setup stops with *"this Geely account is not served by the EU/International
cloud"*, see [Supported regions](README.md#-supported-regions) first: adding a
region needs that region's app credentials, which is not a code change.

## Development

The integration lives in `custom_components/geely_connect/` and follows the
[Home Assistant integration layout](https://developers.home-assistant.io/docs/creating_integration_file_structure):
one file per platform, a `config_flow.py`, `strings.json` + `translations/`,
`manifest.json`, and a `brand/` folder with the icon and logo (Home Assistant
2026.3 and later serve those directly, so no `home-assistant/brands` PR is
needed).

To test a change, copy or symlink `custom_components/geely_connect/` into a
Home Assistant `config/custom_components/` folder and **restart** — Python
caches imported modules, so *Reload* on the integration will not pick up new
code.

### Guidelines

- Match Home Assistant core conventions — this repo aims to be ready for a
  future submission as an official integration.
- User-facing text goes through `strings.json` and every file in
  `translations/` (en, he, ar, fr, ru) — never hard-code UI strings. The five
  files must have identical key sets.
- Every numeric sensor needs a `state_class`, or Home Assistant records no
  long-term statistics for it.
- New entities ship **enabled**. If something is only worth having
  occasionally, it belongs behind the full-exposure option rather than
  disabled-by-default.
- Prefer removing a duplicate over hiding it. An aggregate that restates
  entities already on the list is a duplicate.
- Changing the set of entities an existing install has usually needs an entry
  migration in `async_migrate_entry` — bump `VERSION` in `config_flow.py` to
  match. Migrations must never delete anything a user could have history on.
- Bump `version` in `manifest.json` and cut a matching GitHub release, or HACS
  has nothing to offer users as an update.
- CI runs [hassfest](https://developers.home-assistant.io/blog/2020/04/16/hassfest)
  and [HACS validation](https://hacs.xyz/docs/publish/action) on every push —
  keep both green.

### Things that are easy to get wrong

- **Entity ids come from the friendly name, not the internal key.** The key
  `range` becomes `sensor.<car>_electric_range`. Check any card or blueprint
  against a real device page.
- **`LICENSE` must stay byte-faithful to the MIT text.** Editing it breaks
  GitHub's license detection, and HACS validation fails on
  `SPDX: NOASSERTION`. Attribution belongs in `NOTICE.txt`.
- **Home Assistant converts pressure itself.** Report the car's native kPa and
  set `suggested_unit_of_measurement`; setting the user's unit as the native
  one gets converted straight back.
- **`entity_registry_enabled_default` only applies at first registration.** It
  does nothing for an install that already has the entity.
