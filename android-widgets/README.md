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

## Making it actually do something

**A Template widget only displays** — that is a limit of the companion app, not
of the template. Nothing written in the Template field can lock a door. There is
no single widget that shows a rich status *and* carries buttons, so pick one of
these two:

### Option A — one shortcut that opens a full control card

The most capable option, and a single icon on your home screen.

1. Put [`cards/widget-control.yaml`](../cards/widget-control.yaml) on a
   dashboard view of its own — lock with LOCK/UNLOCK buttons, rapid warming and
   cooling, battery, and full status, all interactive.
2. In the companion app: **Settings → Companion app → Manage shortcuts** → add a
   shortcut pointing at that view's path.
3. The shortcut appears on your home screen. One tap opens straight into the
   card — no navigating.

Put the `all-in-one.jinja` Template widget next to it and you have a glanceable
status plus one tap to every control.

### Option B — a row of Button widgets

Long-press the home screen → **Widgets** → **Home Assistant** → **Button**. One
widget per action, each with its own icon and label. Sat in a row under the
Template widget they read as one block.

[`scripts.yaml`](scripts.yaml) here gives each button a proper name, icon and
follow-up refresh. Paste it into your `<config>/scripts.yaml`, reload scripts,
then point each Button widget at **action** `script.turn_on`, **entity**
`script.geely_…`:

| Script | Does |
|---|---|
| `script.geely_lock_toggle` | Locks if unlocked, unlocks if locked — one button for both |
| `script.geely_lock` / `script.geely_unlock` | Separate buttons, if you prefer no ambiguity |
| `script.geely_rapid_warm` | Rapid warming, one press |
| `script.geely_rapid_cool` | Rapid cooling, one press |
| `script.geely_climate_off` | Climate off |
| `script.geely_find_car` | Horn and lights |
| `script.geely_refresh` | Pull fresh data now |

Each one waits 8 seconds after the command and then refreshes, so the Template
widget above catches up instead of showing the old state. The toggle refuses to
act when the lock state is unknown rather than guessing — an unlock you did not
mean is the worse mistake.

## What's here

| File | Shows | Fits |
|---|---|---|
| **`all-in-one.jinja`** | ⭐ Everything below in one widget | 4×3 |
| `status.jinja` | Lock, battery, range, charging, anything open, data age | 4×2 |
| `battery.jinja` | Battery and range, with charging progress and finish time | 2×2 |
| `attention.jinja` | Nothing at all unless something needs you | 4×1 |
| `one-line.jinja` | A single line, for a narrow widget | 2×1 |

Start with **`all-in-one.jinja`** — one widget, everything in it:

```
🔒 Locked · 58% · 247 km
█████░░░░░
⚡ Full at 13:46 · in 1h 35m
✅ All shut
🛞 RR 26.5 psi · 🔧 service in 12d
5 min ago · 4646 km total
```

The warning line only appears when there is something to warn about. Each
section is independent — on a smaller widget, delete a whole `SECTION` block.

## Formatting notes

The companion app renders a small subset of HTML, so `<b>`, `<i>` and `<br>`
work and markdown does not. Every template guards its values, so a car that has
not reported yet is described in words rather than printing `unknown`.

The templates call `now()`, which makes Home Assistant re-render them every
minute — so "in about 40 minutes" and "updated 3 minutes ago" stay honest with
no automation behind them.
