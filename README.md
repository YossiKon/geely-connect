# Geely Connect

[![Add to HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=YossiKon&repository=geely-connect&category=integration)
[![HACS Default](https://img.shields.io/badge/HACS-Default-41BDF5.svg)](https://github.com/hacs/default)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> ⚠️ **Unofficial, community-built integration.** Not affiliated with, endorsed
> by, or supported by Geely. Use at your own risk.

A **security-hardened** [Home Assistant](https://www.home-assistant.io/)
integration for Geely vehicles that use the **Geely Global / International**
mobile app. Tested on the **Geely EX5**; it is capability-driven, so other Geely
models on the same EU/International cloud should work too (only the entities your
specific car reports are created).

It talks directly to Geely's own cloud - the same servers the official app uses
- and adds a hardened transport, full data exposure, efficient polling and a
polished setup on top.

---

## ✨ Highlights

- 🔒 **Security-first** - verified TLS plus public-key pinning.
- 📊 **Everything enabled** - all 61 entities are on from the start, no
  duplicates, nothing to switch on by hand.
- 🧮 **Computed extras** - charge completion time, range at full charge and
  efficiency, none of which the car reports itself.
- 🔋 **Efficient polling** with selectable modes (Eco / Normal / Live / Manual),
  changeable at any time.
- 🌍 **EU and North-American** backends, detected from the vehicle.
- 🗣️ **Translated setup** - the configuration dialogs are in English, Hebrew,
  Arabic, Russian and French. Entity names follow Home Assistant's own
  language.
- 🖥️ **Ready-made cards, widgets, a four-view dashboard and Blueprints**, all
  built-in Lovelace cards - no extra HACS frontend packages.
- 📈 **Long-term statistics** on every numeric entity, so history survives the
  recorder's purge window.

---

## 📊 What you can see (sensors)

All of these are created **enabled** - nothing to switch on by hand.

### Battery & charging
| Entity | Description |
|---|---|
| Battery | State of charge (%) |
| Electric Range | Remaining driving range (km) |
| Charger Connection | Disconnected / Plugged in / Charging |
| Charger Plug | Binary - cable connected |
| Time To Full Charge | Minutes remaining while charging |
| Average Consumption | Energy use (kWh / 100 km) |

### Driving & trip
| Entity | Description |
|---|---|
| Speed / Average Speed | Current & average speed (km/h) |
| Trip Meter | The car's own trip meter A - the one the driver resets on the dash, not a single journey |
| Total Mileage | Odometer (km) |
| Engine State | Off / Running |
| Park Brake | Engaged / Released |

### Climate
| Entity | Description |
|---|---|
| Interior Temperature | Cabin temp (°C) |
| Exterior Temperature | Outside temp (°C) |

### Tires
Two sets of four, one reading each corner:

| Entity | Unit |
|---|---|
| **Tire Front-Left / Front-Right / Rear-Left / Rear-Right** | Always the unit you picked at setup |
| Tire Pressure FL / FR / RL / RR | Whatever Home Assistant decides |

Use the first set. The second carries `device_class: pressure`, which hands the
display unit to Home Assistant: it reads `suggested_unit_of_measurement` only
when the entity is **first registered** and falls back to the unit system after
that, so an install created before you picked psi keeps showing kPa forever no
matter what the integration reports. The first set has no device class, so
nothing converts it and the setup choice is honoured - on a fresh install, on
an existing one, and again whenever you change the unit under **Configure**.

The originals stay, so anything already pointing at them keeps working.

**All three units are always there.** Each of the four carries `psi`, `bar` and
`kPa` attributes, whatever the state itself is showing - so a card or template
can take whichever it wants without depending on the setup choice:

```yaml
# psi regardless of what the sensor's own unit is
{{ state_attr('sensor.my_geely_ex5_tire_front_left', 'psi') }}
```

### Body (open / closed)
Driver door, Passenger door, Rear-Left door, Rear-Right door, Trunk, Hood,
Driver seatbelt.

### Maintenance & health
| Entity | Description |
|---|---|
| 12V Battery / 12V Voltage | Auxiliary battery level & voltage |
| Days To Service / Distance To Service | Maintenance intervals |

### Location
Location (device tracker) - GPS position on the map, with altitude.

### 🧮 Computed - not reported by the car
| Entity | Description |
|---|---|
| **Efficiency** | km per kWh, derived from average consumption |
| **Charge Complete** | When charging finishes, as a time rather than a minute count - so a notification can fire on it |
| **Range At Full Charge** | Remaining range extrapolated to 100% at the current efficiency, so it's comparable week to week. Blank below 10% charge, where the estimate is mostly noise |
| **Last Trip** | How far the last completed journey went, worked out from the odometer between engine-on and engine-off |
| **Trip In Progress** | How far the current journey has gone; 0 when parked |
| **Connected** | Is the integration reaching the car right now |
| **Last Updated** | Timestamp of the last successful poll |

### 🔍 Full exposure (optional)
Every other field the server returns can be exposed as an auto-generated
diagnostic sensor. It is **off by default** - on an EX5 it is around 180
entities, which buries the ones worth looking at. Turn it on under
**Configure** if you are hunting for a field that isn't exposed yet. Turning it
back off removes the generated entities again.

---

## 🎛️ What you can do (controls)

| Control | Actions |
|---|---|
| **Lock** | Lock / unlock the doors |
| **Trunk** | Unlock the trunk |
| **Climate (remote pre-conditioning)** | Remote pre-heat/pre-cool: on/off, set temperature (15.5-28.5 °C), Rapid Warming, Rapid Cooling. Only reflects remote pre-climate cycles - the cloud does not report manual cabin HVAC |
| **Seat heating** | Driver & passenger (rear if supported): Off/Low/Medium/High |
| **Seat ventilation** | Driver & passenger (rear if supported) |
| **Defrost** | Windscreen defrost on/off |
| **Windows** | Open / close / ventilate |
| **Sunroof / Sunshade** | Open / close |
| **Charging** | Start / stop, plus scheduled charging (start & end time) |
| **Parking Comfort** | On/off |
| **Cabin purge (G-Clean)** | On/off |
| **Find Car** | Horn + lights |

---

## ⚡ Efficient, adaptive polling

Geely's backend allows **one active session per account**, so every poll briefly
logs the phone app out. Polling is therefore kept as light as possible: few
calls per cycle, GPS wake-ups and secondary data fetched sparingly, and long
intervals whenever nothing changes (with an overnight quiet period). You pick a
profile at setup:

| Mode | Charging / driving | Parked (base → cap) | Secondary data | GPS wake |
|---|---|---|---|---|
| 🔋 **Eco** - fewest interruptions | every 90 s | 300 s → 30 min | every 6th cycle | every 12th |
| ⚖️ **Normal** - balanced | every 30 s | 90 s → 15 min | every 4th cycle | every 6th |
| ⚡ **Live** - freshest | every 15 s | 45 s → 5 min | every 3rd cycle | every 3rd |
| ✋ **Manual** - you sync | never | never | on every sync | on every sync |

The mode is **not fixed at setup** - change it any time from **Configure** on
the device page (see *Changing settings later* below).

Everything is per-mode: the active-polling rate, the parked back-off, the cap,
and how often secondary/GPS calls run.

### ✋ Manual mode - sync only when you ask

Manual runs **no timer at all**. Home Assistant never contacts the car on its
own, so the phone app is never signed out by the integration. The trade is
explicit: entity values stay exactly as old as your last sync, and there is no
"is it plugged in yet?" without asking.

A sync happens when - and only when - one of these occurs:

- you press the **Refresh Data** button on the device page
- something calls `homeassistant.update_entity` on any entity of the car
  (a dashboard button, an automation, a schedule you define yourself)
- **you send a command** - lock, climate, charging and the rest still poll
  afterwards on their own, so the entity reflects what the car actually did

Because syncs are rare in this mode, each one fetches **everything**: the full
status, the secondary state and scheduled-charging data, and a fresh GPS
position. The other modes ration those across cycles; Manual does not.

Two things worth knowing before you pick it:

- **Automations that read car state will act on stale data** unless they sync
  first. Call `homeassistant.update_entity` at the start of the automation and
  the data will be fresh for the conditions that follow.
- **A failed sync is shown, not hidden.** The Refresh Data button reports the
  error instead of silently leaving the old values on screen.

Sync on your own schedule - this polls once an hour, and only during the day:

```yaml
automation:
  - alias: Geely - hourly sync
    triggers:
      - trigger: time_pattern
        hours: "/1"
    conditions:
      - condition: time
        after: "07:00:00"
        before: "23:00:00"
    actions:
      - action: homeassistant.update_entity
        target:
          entity_id: sensor.geely_ex5_4143_battery
```

Other smart behaviour: **long-term statistics** (battery, range, consumption,
pressures feed HA statistics + the Energy dashboard) and **ready-made
Blueprints** in `blueprints/` (charging complete, low battery, door/trunk left
open, tire pressure out of range, left unlocked away from home, pre-condition
climate before departure).

---

## 🔒 Security

Security was a first-class goal of this build. Every connection is validated
against the public CAs. The two Geely control gateways that use Geely's own
private CA - `apis.ecloudeu.com` and `apis.ecloudus.com` - are each verified
against a public-key pin that ships with the integration, so they are checked
from the very first connection and a man-in-the-middle is refused rather than
trusted. No other host may use that fallback, and a host that has validated
publicly once can never be pushed onto it. Credentials are stored with owner-only access, secrets are masked in logs
and in the diagnostics report, and all traffic goes only to Geely's own servers
- no telemetry, no third parties.

If Geely ever rotates that gateway's key you will see a `GeelyTLSPinError`
naming the host and the key it presented. That is the pin doing its job - it
cannot tell a legitimate rotation from an attacker, and neither can you from
the error alone. So:

1. **Do not pin the reported key.** If the error was caused by someone
   intercepting your connection, that key is *theirs*, and pinning it would
   hand them exactly the trust the pin exists to withhold.
2. [Open an issue](https://github.com/YossiKon/geely-connect/issues) with the
   host and key from the error. A rotation hits everyone at once, so it is
   quickly confirmed from other reports and networks, and a release with the
   new pin ships. The integration keeps showing the last known data meanwhile
   - you lose remote commands, not the car.
3. Only if you must unblock yourself sooner: confirm the same key is reported
   from a **different network** (e.g. mobile hotspot instead of home Wi-Fi -
   an attacker rarely controls both), and only then add it to the `pins` list
   for that host in `.storage/geely_connect/<VIN>/server_pins.json` and
   restart.

### What "hardened" means here

Because this account can unlock and start your car, the transport gets treated
like a credential path rather than a convenience:

| | How this integration handles it |
|---|---|
| **Certificate validation** | Strict public-CA validation with hostname checking on every connection. The private-CA fallback is allowlisted to two known gateways, requires a private-CA verify code, and is permanently disabled for any host that has ever validated publicly - so a bad certificate cannot force a downgrade |
| **Server identity** | SPKI public-key pins ship with the integration and are remembered on disk, so a swapped server key is caught across restarts, before any credential is sent |
| **Logs & diagnostics** | Tokens, certificates and the captcha secret are masked; VIN, user ID, e-mail and device IDs are reduced to their last four characters. The diagnostics download is redacted separately, so a bug report is safe to attach |
| **Request building** | The VIN, user ID and server-supplied headers are rejected if they contain CR/LF, so a hostile backend value cannot smuggle a second request onto the authenticated socket |
| **Stored secrets** | The mTLS private key is created `0600` from the first byte - never briefly world-readable - inside a `0700` directory |
| **Identifiers** | VIN and user ID must match a strict charset before they reach a filesystem path or a request line |
| **Re-authentication** | Signing in as a different Geely account is refused rather than silently rebinding the entry |
| **Where data goes** | Only Geely's own servers. No telemetry, no analytics, no third-party host |

---

## 📥 Installation (HACS)

Geely Connect is in the **HACS default store**, so there is no repository URL
to add.

**One-click:** [![Add to HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=YossiKon&repository=geely-connect&category=integration)

Or manually:

1. HACS → search **Geely Connect** → open it → **Download**, then **restart
   Home Assistant**
2. Settings → Devices & Services → **Add Integration** → **Geely Connect**
3. Enter your email, pick **country / tire-pressure unit / polling mode**, then
   enter the 6-digit code sent to your inbox

### The repository doesn't show up in HACS?

- Clear any active filters in the HACS search box, and make sure you are
  looking under **Integrations**.
- HACS caches its repository list. Force a refresh with ⋮ → **Reload data** on
  the HACS main page.

> **Added it as a custom repository before?** It still works, but you can tidy
> up: HACS → ⋮ → **Custom repositories** → 🗑 next to this repository. Removing
> the *custom repository entry* does not uninstall the integration - the store
> copy takes over and updates continue normally.

### Manual installation
Copy `custom_components/geely_connect/` into `config/custom_components/` and
restart.

---

## 🔄 Updating

**You never need to remove and re-add the integration.** Your vehicle, history,
automations and dashboards all survive an update.

1. HACS → **Geely Connect** → **Update**
2. **Restart Home Assistant** - always required. Python caches modules it has
   already imported, so *Reload* on the integration rebuilds the entities from
   the code already in memory and will not pick up a new version.

Turn on HACS → ⋮ → **Settings** → *Show notification when a new version is
available* and updates also appear under Settings → **Updates** alongside
everything else.

### No update showing?

- HACS refreshes repository data on a timer. Force it: HACS → **Geely Connect**
  → ⋮ → **Update information**, or ⋮ → **Reload data** on the HACS main page.
- If you installed before the first release existed, HACS is tracking the
  **branch**, not releases, and has no version to compare against. Fix it once:
  ⋮ → **Redownload** → pick a version such as `v1.4.0` from the dropdown. Every
  later release then shows up as a normal update.
- Conversely, choosing **main** in that same dropdown tracks the branch, so
  every push is available immediately without waiting for a release - handy
  while a change is being tested.

---

## 🖥️ Dashboards & cards

First, find your **entity suffix**: Settings → Devices & Services → Geely Connect
→ your car → click any entity. Your ids will look like
`sensor.geely_ex5_4143_battery` - the device name always ends in the last four
VIN characters - so there the suffix is `geely_ex5_4143`.

`my_geely_ex5` in every file below is a **placeholder**, not a real id.
Search-replace it with your own suffix before saving anything.

There are two kinds of file - **use the matching paste location**, or you'll get
a `Cannot read properties of undefined (reading 'startsWith')` error:

### 🃏 Single cards - [`cards/`](cards/)
A **card** starts with `type:` and is added via
**Edit dashboard → Add card → ⤵ Manual → paste → Save** (into any existing view).

- **`cards/card-overview.yaml`** - ⭐ start here. Battery, range, interior temp
  and charger status, plus working lock and climate controls, quick actions, and
  a charging panel that appears only while charging. No HACS needed.
- **`cards/card-builtin.yaml`** - one self-contained card (vertical-stack of
  built-in tiles). **No HACS needed.** Best for dropping into an existing tab.
- **`cards/card-premium-hebrew.yaml`** - styled dark card (Hebrew). Requires the
  HACS frontend cards `button-card`, `stack-in-card`, `card-mod`.

**Widgets** - narrow, single-purpose cards for a sidebar or phone column, all
built-in only:

- **`cards/widget-battery.yaml`** - battery gauge with colour bands, range,
  charging switch, time-to-full while charging.
- **`cards/widget-climate.yaml`** - thermostat, temperatures, defrost, G-Clean
  and the seat heat/vent selects.
- **`cards/widget-security.yaml`** - lock, every door/trunk/hood at a glance, and
  a red warning block when something is open.
- **`cards/widget-tires.yaml`** - the four pressures laid out like the car, with
  a history graph to spot a slow leak.

- **`cards/widget-range.yaml`** - range now, range at full charge and
  efficiency, with a 30-day graph so pack degradation or seasonal loss shows up.
- **`cards/widget-control.yaml`** - ⭐ the four things you reach for most: lock
  and unlock, rapid warming or cooling in one tap, battery percentage, and
  whether the car is shut, plugged in and reachable.
- **`cards/widget-status.yaml`** - the whole state in four sentences: lock,
  charge and range; what charging is doing and when it finishes; which openings
  are open by name; and how old the data is.
- **`cards/widget-attention.yaml`** - silent until something needs you, then
  lists only that: left open, unlocked, low battery, a soft tire, weak 12V,
  service due, or no contact with the car.
- **`cards/widget-template.yaml`** - a starter to copy when building your own.
  A working card that walks through each pattern - heading, tile grid,
  interactive tiles, gauge bands, conditional, glance, history graph and a
  templated markdown card - with a comment over each saying which line to change.

### 📱 Home-screen widgets - [`android-widgets/`](android-widgets/)
Not dashboard cards. These are **Jinja templates** for the companion app's
**Template widget**, which puts a live line of text on your phone's home screen.
Pasting card YAML into that field shows you the YAML as text - different thing
entirely. Start with **`all-in-one.jinja`**: lock, charge bar, charging and its
finish time, what's open, any warning, and data age, all in one widget.

### 📑 Single views - [`views/`](views/)
A **view** is one tab of a dashboard. Each file is a YAML list item starting
with `- title:`, pasted under the `views:` key via **Edit dashboard → ⋮ → Raw
configuration editor** - not via "Add card". Line the `- title:` up with the
views already there.

- **`views/view-car.yaml`** - the car at a glance: key numbers, lock and climate
  controls, every opening, quick actions, and a map.
- **`views/view-charging.yaml`** - battery gauge, charge complete, range at full
  charge, scheduled charging, and a week of battery history.
- **`views/view-mobile.yaml`** - single column with big touch targets, for a
  phone.

### 🤖 Automations - [`automations/`](automations/)
Ready-to-paste automations. Append a file to your `automations.yaml`, or copy
one entry at a time through the automation editor's ⋮ → **Edit in YAML**.
Replace `notify.mobile_app_your_phone` with your own notifier.

- **`automations/charging.yaml`** - charge finished, charge nearly finished
  (uses the Charge Complete timestamp), low battery, and "you forgot to plug in".
- **`automations/security.yaml`** - something left open, left unlocked after
  leaving, windows open with rain forecast, and a bedtime check.
- **`automations/comfort-and-maintenance.yaml`** - weekday pre-heat, hot-cabin
  pre-cool, low tire pressure, service due, and 12V battery health.

> The tire-pressure thresholds are in **psi**, matching the setup default.
> Change them if you picked bar or kPa - the file says where.

Prefer a UI? The same ideas are in [`blueprints/`](blueprints/) as importable
blueprints.

### 🖥️ Full dashboards - [`dashboards/`](dashboards/)
A **dashboard** starts with `title:` / `views:` and is pasted via
**Settings → Dashboards → Add dashboard → New dashboard from scratch → open →
⋮ Edit → ⋮ Raw configuration editor → paste → Save** (NOT "Add card").

- **`dashboards/dashboard-premium.yaml`** - ⭐ four views (Overview with a map,
  Charging, Climate, Trip & Health). Built-in cards only.
- **`dashboards/dashboard-builtin.yaml`** - a complete multi-section dashboard
  using only built-in cards. No HACS needed.

> Any entity your car doesn't report shows as "unavailable" - just delete that
> tile.

---

## 💡 Usage examples

Beyond the dashboards, here are copy-paste automations. Replace `my_geely_ex5`
with your suffix and `notify.mobile_app_xxxx` with your phone's notify service.
Ready-made **Blueprints** for these live in [`blueprints/`](blueprints/) if you
prefer a UI.

**Notify when charging finishes**
```yaml
automation:
  - alias: Geely - charging complete
    trigger:
      - platform: numeric_state
        entity_id: sensor.my_geely_ex5_battery
        above: 80
    action:
      - service: notify.mobile_app_xxxx
        data:
          title: "🔋 Geely"
          message: "Charging done - battery at {{ states('sensor.my_geely_ex5_battery') }}%."
```

**Warm up the car on weekday mornings**
```yaml
automation:
  - alias: Geely - pre-heat before work
    trigger:
      - platform: time
        at: "07:20:00"
    condition:
      - condition: time
        weekday: [mon, tue, wed, thu, fri]
      - condition: state
        entity_id: device_tracker.my_geely_ex5_location
        state: home
    action:
      - service: climate.set_temperature
        target: { entity_id: climate.my_geely_ex5_climate }
        data: { temperature: 22 }
      - service: climate.set_hvac_mode
        target: { entity_id: climate.my_geely_ex5_climate }
        data: { hvac_mode: heat_cool }
```

**Auto-lock when you leave home**
```yaml
automation:
  - alias: Geely - lock on leaving home
    trigger:
      - platform: state
        entity_id: device_tracker.my_geely_ex5_location
        from: home
    condition:
      - condition: state
        entity_id: lock.my_geely_ex5_doors
        state: unlocked
    action:
      - service: lock.lock
        target: { entity_id: lock.my_geely_ex5_doors }
```

**Alert if a door or the trunk is left open**
```yaml
automation:
  - alias: Geely - left open
    trigger:
      - platform: state
        entity_id:
          - binary_sensor.my_geely_ex5_door_driver
          - binary_sensor.my_geely_ex5_trunk
          - binary_sensor.my_geely_ex5_hood
        to: "on"
        for: { minutes: 3 }
    action:
      - service: notify.mobile_app_xxxx
        data:
          title: "⚠️ Geely"
          message: "{{ trigger.to_state.attributes.friendly_name }} has been open for 3 minutes."
```

**Low-battery reminder**
```yaml
automation:
  - alias: Geely - low battery
    trigger:
      - platform: numeric_state
        entity_id: sensor.my_geely_ex5_battery
        below: 20
    action:
      - service: notify.mobile_app_xxxx
        data:
          title: "🔋 Geely"
          message: "Battery low ({{ states('sensor.my_geely_ex5_battery') }}%). Time to charge."
```

Tip: press the **Refresh Data** button (or call `button.press` on
`button.my_geely_ex5_refresh_data`) any time you want an immediate update.

---

## 🌍 Supported regions

Geely runs a separate backend per area, each with its **own app credentials**:

| Area | Backend | Status |
|---|---|---|
| **EU / International** | `api.ecloudeu.com` | ✅ supported |
| **NA** (US, CA, MX - and Brazilian accounts, which resolve here) | `api.ecloudus.com` | ✅ supported |
| **APAC** (AU, NZ, JP, KR, SG, TH…) | `api.ecloudkr.com` | ✅ supported |
| **SA** | `tsp-geely-api-sa.xcloudsvc.com` | ❌ credentials not public |

The area belongs to the **vehicle**, not to the country you pick at setup - the
two can differ - so it is read from the login response
(`tspInfo[].serviceRegion`, falling back to `edgeInfo.code`, then the vehicle
record's top-level `serviceRegion`, then `saleMarket`/`tcamMarket` - APAC
accounts have `"AP"` - and finally a safe EU default) and stored on the
config entry. Login, the email code and the vehicle list are not regional in
practice; only certificate provisioning, the session exchange and control
commands are.

APAC specifics: the session exchange runs on the **public** host
`api.ecloudkr.com` at `/auth-center/account/session` (not on the mTLS control
host), requires `receiverId` (the login email) in the body, an
`Accept: application/json; charset=utf-8` header and **uppercase**
`X-SIGNATURE`/`X-TIMESTAMP` signature headers, and the access code must be
minted by the APAC regional host (`m-lcmsam-kr.geely.com`) - codes from the
EU/global hosts make the APAC session service crash with `8500`. See
`docs/APAC-SUPPORT.md` for the full write-up.

A car in an area whose credentials are unknown stops the setup with a clear
message naming the area, rather than being signed against the European backend
and failing with the opaque `1501 geelyos verify error` that the upstream
project reports. Adding SA needs that area's app id and secret.

---

## 🎚️ Changing settings later

Settings → Devices & Services → **Geely Connect** → **Configure** changes the
**polling mode** (Eco / Normal / Live / Manual) and **tire-pressure unit** at
any time - no reinstall, no restart. Changing the pressure unit also re-points
the four existing tire sensors, so history is kept rather than restarting.

Switching to **Manual** stops the timer immediately, and switching away from it
starts polling again - no entity is created or removed either way, so history
and automations are untouched.

### Which entities appear

**All 61 are on from the start** - nothing is hidden and nothing needs enabling.
Everything the car reports, plus the computed extras above.

The only thing not created is the raw full-exposure pass (see below), because
those are duplicates of the curated entities by definition. Two aggregates that
restated entities already on the list - a "lowest tire pressure" and a "service
due" date - were removed rather than shipped alongside the four pressures and
the two service counters.

> The window, sunroof and sunshade controls are live. They are ordinary
> dashboard controls, so put them somewhere you won't hit them by accident.

---

## ⚠️ Known limitation - one session per account
When Home Assistant logs in, the phone app is signed out, and vice-versa. If it
happens, HA shows a **Reconfigure** prompt - request a fresh code and
re-authenticate. Tip: run the first setup on a network you trust.

---

## 📁 Repository structure

```
custom_components/geely_connect/   the integration itself (what HACS installs)
├── brand/                         icon.png / logo.png shown in the HA UI
├── translations/                  UI translations (en, he, ar, fr, ru)
├── manifest.json                  integration metadata
└── *.py                           platforms, API client, config flow, …
automations/                       ready-to-paste automations
blueprints/automation/             the same ideas as importable blueprints
cards/                             single cards and widgets (paste as "Manual card")
views/                             single dashboard tabs (paste under "views:")
dashboards/                        full dashboards (paste in Raw config editor)
hacs.json                          HACS metadata
```

The icon and logo live in `custom_components/geely_connect/brand/`. Since Home
Assistant 2026.3 a custom integration serves its own brand images from there
and they take priority over the CDN, so no `home-assistant/brands` submission
is needed. On older versions the folder is simply ignored.

---

## 📜 License & credits

The reverse-engineered Geely protocol and vehicle field mappings are derived
from [`nitaybz/geely-global-ha`](https://github.com/nitaybz/geely-global-ha)
under the MIT License - the protocol research that made any of this possible.
That credit and the full license text are in `NOTICE.txt`.

Everything built on top of it is original work under MIT (see `LICENSE`):

- **Transport and security** - strict CA validation with allowlisted public-key
  pinning, a persistent pin store, log and diagnostics redaction, CR/LF request
  guards, owner-only key storage and identifier validation (see
  [Security](#-security))
- **Multi-region support** - EU and North-American backends with per-vehicle
  region detection, and a clear error for regions with no public credentials
- **Adaptive polling** - Eco / Normal / Live profiles with idle back-off and
  quiet hours, which matters because Geely allows one session per account
- **Computed sensors** - efficiency, trip distance, charge-completion time and
  range-at-full-charge, none of which the car reports directly
- **Setup and upkeep** - options flow, config-entry migrations, diagnostics,
  tyre-pressure unit choice, and five translations
- **Ready-made dashboards** - cards, views, blueprints, automations and
  home-screen widgets

Unofficial, provided "as is" with no warranty. Not affiliated with Geely or
ECARX. Remote commands are used at your own risk.
