# Geely Connect (Unofficial)

> ⚠️ **Unofficial, community-built integration.** Not affiliated with, endorsed
> by, or supported by Geely. Use at your own risk.

A **security-hardened** [Home Assistant](https://www.home-assistant.io/)
integration for Geely vehicles that use the **Geely Global / International**
mobile app. Tested on the **Geely EX5**; it is capability-driven, so other Geely
models on the same EU/International cloud should work too (only the entities your
specific car reports are created).

It talks directly to Geely's own cloud — the same servers the official app uses
— and adds a hardened transport, full data exposure, efficient polling and a
polished setup on top.

---

## ✨ Highlights

- 🔒 **Security-first** — TLS enforced and fail-closed, MITM-resistant (details
  below).
- 📊 **Full data exposure** — every field the server returns can be shown, not
  just a curated subset.
- 🔋 **Efficient polling** with selectable modes (Eco / Normal / Live).
- 🌍 **Setup choices** — country, tire-pressure unit (PSI / bar / kPa) and
  language.
- 🗣️ **Multi-language** — English, Hebrew, Arabic, Russian, French.
- 🧩 **Ready-made Blueprints** and long-term statistics / Energy dashboard
  support.

---

## 📊 What you can see (sensors)

### Battery & charging
| Entity | Description |
|---|---|
| Battery | State of charge (%) |
| Electric Range | Remaining driving range (km) |
| Charger Connection | Disconnected / Plugged in / Charging |
| Charger Plug | Binary — cable connected |
| Time To Full Charge | Minutes remaining while charging |
| Average Consumption | Energy use (kWh / 100 km) |
| **Efficiency** | Derived km per kWh *(computed by us)* |

### Driving & trip
| Entity | Description |
|---|---|
| Speed / Average Speed | Current & average speed (km/h) |
| Trip Meter | Last-trip distance (km) |
| Total Mileage | Odometer (km) |
| Engine State | Off / Running |
| Park Brake | Engaged / Released |

### Climate
| Entity | Description |
|---|---|
| Interior Temperature | Cabin temp (°C) |
| Exterior Temperature | Outside temp (°C) |

### Tires (unit chosen at setup: PSI / bar / kPa)
Tire Pressure — Front-Left, Front-Right, Rear-Left, Rear-Right.

### Body (open / closed)
Driver door, Passenger door, Rear-Left door, Rear-Right door, Trunk, Hood,
Driver seatbelt.

### Maintenance & health
| Entity | Description |
|---|---|
| 12V Battery / 12V Voltage | Auxiliary battery level & voltage |
| Days To Service / Distance To Service | Maintenance intervals |

### Location & status
| Entity | Description |
|---|---|
| Location (device tracker) | GPS position on the map (with altitude) |
| **Connected** | Is the integration reaching the car right now *(ours)* |
| **Last Updated** | Timestamp of the last successful poll *(ours)* |

### 🔍 Full exposure
Beyond the curated entities above, **every field the server returns** is exposed
as an auto-generated diagnostic sensor (disabled by default — enable the ones you
want). New fields appear automatically, so nothing is hidden.

---

## 🎛️ What you can do (controls)

| Control | Actions |
|---|---|
| **Lock** | Lock / unlock the doors |
| **Trunk** | Unlock the trunk |
| **Climate** | On/off, set temperature (15.5–28.5 °C), Rapid Warming, Rapid Cooling |
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
| 🔋 **Eco** — fewest interruptions | every 90 s | 300 s → 30 min | every 6th cycle | every 12th |
| ⚖️ **Normal** — balanced | every 30 s | 90 s → 15 min | every 4th cycle | every 6th |
| ⚡ **Live** — freshest | every 15 s | 45 s → 5 min | every 3rd cycle | every 3rd |

Everything is per-mode: the active-polling rate, the parked back-off, the cap,
and how often secondary/GPS calls run.

Other smart behaviour: **long-term statistics** (battery, range, consumption,
pressures feed HA statistics + the Energy dashboard) and **ready-made
Blueprints** in `blueprints/` (charging complete, low battery, door/trunk left
open, tire pressure out of range, left unlocked away from home, pre-condition
climate before departure).

---

## 🔒 Security posture (high level)

Security was a first-class goal of this build. Without giving away details that
would help an attacker, here is the posture:

- **Encrypted, authenticated connections only.** Traffic to the vehicle cloud is
  protected end-to-end and the server's identity is verified on every request.
  The connection is **fail-closed** — if the server can't be verified, no
  credentials are ever sent.
- **Man-in-the-middle resistant.** A tampered or impersonated server is detected
  and the connection refused before any sensitive data leaves Home Assistant.
- **Credentials protected at rest.** Vehicle security material is stored with
  owner-only access on disk.
- **No secrets in logs.** Tokens, certificates and other sensitive values are
  masked in logs and in the one-click diagnostics report, so it's safe to share
  for support.
- **Input hardening.** Data received from the backend is strictly validated
  before use, closing common classes of injection/traversal issues.
- **Nothing phones home.** All traffic goes only to the vehicle manufacturer's
  own servers — no telemetry, no third parties, no analytics.

> For safety this README deliberately does **not** describe the specific
> algorithms, keys or internal mechanisms — that detail is kept out of public
> docs so it can't be used as a roadmap for abuse.

---

## 📥 Installation (HACS)

1. HACS → ⋮ → **Custom repositories** → add this repo URL, category
   **Integration**
2. Search **Geely Connect**, download, then **restart Home Assistant**
3. Settings → Devices & Services → **Add Integration** → **Geely Connect
   (Unofficial)**
4. Enter your email, pick **country / tire-pressure unit / language / polling
   mode**, then enter the 6-digit code sent to your inbox

### Manual installation
Copy `custom_components/geely_connect/` into `config/custom_components/` and
restart.

---

## 🖥️ Dashboards

Two ready-made dashboards live in [`dashboards/`](dashboards/):

- **`dashboard-builtin.yaml`** — uses only Home Assistant's built-in cards, so it
  works with **no HACS / custom cards**. Copy-paste and go.
- **`premium-card-hebrew.yaml`** — a styled, dark-themed card (Hebrew). Requires
  the HACS frontend cards `button-card`, `stack-in-card` and `card-mod`.

**To use one:** find your entity suffix (Settings → Devices & Services → Geely
Connect → your car; IDs look like `sensor.my_geely_ex5_battery`, so the suffix is
`my_geely_ex5`). If yours differs, search-replace `my_geely_ex5` in the file.
Then: Settings → Dashboards → Add dashboard → New dashboard from scratch → open
it → ⋮ → Edit → ⋮ → **Raw configuration editor** → paste → Save.

---

## 💡 Usage examples

Beyond the dashboards, here are copy-paste automations. Replace `my_geely_ex5`
with your suffix and `notify.mobile_app_xxxx` with your phone's notify service.
Ready-made **Blueprints** for these live in [`blueprints/`](blueprints/) if you
prefer a UI.

**Notify when charging finishes**
```yaml
automation:
  - alias: Geely — charging complete
    trigger:
      - platform: numeric_state
        entity_id: sensor.my_geely_ex5_battery
        above: 80
    action:
      - service: notify.mobile_app_xxxx
        data:
          title: "🔋 Geely"
          message: "Charging done — battery at {{ states('sensor.my_geely_ex5_battery') }}%."
```

**Warm up the car on weekday mornings**
```yaml
automation:
  - alias: Geely — pre-heat before work
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
  - alias: Geely — lock on leaving home
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
  - alias: Geely — left open
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
  - alias: Geely — low battery
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

## ⚠️ Known limitation — one session per account
When Home Assistant logs in, the phone app is signed out, and vice-versa. If it
happens, HA shows a **Reconfigure** prompt — request a fresh code and
re-authenticate. Tip: run the first setup on a network you trust.

---

## 📜 License & credits

MIT for the original parts (framework, hardening, transport, packaging) — see
`LICENSE`. The reverse-engineered Geely protocol and vehicle field mappings are
derived from [`nitaybz/geely-global-ha`](https://github.com/nitaybz/geely-global-ha)
under the MIT License; that credit and license text are in `NOTICE.txt`. Replace
"YOUR NAME" in `LICENSE` before publishing.

Unofficial, provided "as is" with no warranty. Not affiliated with Geely or
ECARX. Remote commands are used at your own risk.
