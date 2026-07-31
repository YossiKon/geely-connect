# Home-screen widgets (Android / iOS companion app)

These are **not** dashboard cards. They go in the Home Assistant **companion
app's Template widget**, which puts a live line of text on your phone's home
screen.

Everything in [`cards/`](../cards/) is Lovelace YAML and belongs on a dashboard.
Pasting that YAML into a Template widget shows you the YAML itself as text —
the widget field takes a **Jinja template**, nothing else.

## Adding one

1. Long-press your Android home screen → **Widgets** → **Home Assistant** →
   **Template**.
2. Drop it where you want it and pick your Home Assistant server.
3. Open one of the `.jinja` files here, copy **the whole file**, and paste it
   into the **Template** field.
4. Search-replace `my_geely_ex5` with your own entity suffix — find it under
   Settings → Devices & Services → Geely Connect → your car; ids look like
   `sensor.my_geely_ex5_battery`.
5. Set **text size** (12–16 reads well) and a theme, then **UPDATE WIDGET**.

Tap the widget to open Home Assistant. To make it *do* something instead, use
the app's **Button widget** and point it at `button.my_geely_ex5_find_car`,
`lock.my_geely_ex5_doors` or similar — the Template widget only displays.

## What's here

| File | Shows |
|---|---|
| `status.jinja` | Lock, battery, range, charging, anything open, data age |
| `battery.jinja` | Battery and range, with charging progress and finish time |
| `attention.jinja` | Nothing at all unless something needs you |
| `one-line.jinja` | A single line, for a narrow 1×1 widget |

## Formatting notes

The companion app renders a small subset of HTML, so `<b>`, `<i>` and `<br>`
work and markdown does not. Every template guards its values, so a car that has
not reported yet is described in words rather than printing `unknown`.

The templates call `now()`, which makes Home Assistant re-render them every
minute — so "in about 40 minutes" and "updated 3 minutes ago" stay honest with
no automation behind them.
