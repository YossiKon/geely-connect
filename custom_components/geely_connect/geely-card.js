/* Geely Connect dashboard cards.
 *
 * Two custom Lovelace cards, served and registered automatically by the
 * integration - no HACS frontend package, no resource to add by hand:
 *
 *   type: custom:geely-card           the full cockpit
 *   type: custom:geely-card-compact   key numbers + key actions
 *
 * Zero-config: with a single Geely vehicle the cards find their entities on
 * their own (by the integration's platform). With several cars, point the
 * card at one:
 *
 *   type: custom:geely-card
 *   prefix: my_geely_ex5     # the entity slug, e.g. sensor.<prefix>_battery
 *
 * Design language: automotive instrument cluster. A painted generic-EV
 * illustration (drawn inline - no external assets), ultra-light oversized
 * numerals, letter-spaced micro-labels, hairline dividers, one electric
 * accent that follows the car's state (teal while charging, amber on
 * warnings). Open doors light amber markers on the car itself. Destructive
 * actions (unlock, trunk) arm on first tap and fire on the second, so a
 * stray touch on a wall tablet cannot open the car.
 */
"use strict";

(() => {
  /* Every entity suffix the cards may touch, longest first so a longer
   * suffix always wins the match (_12v_battery before _battery). */
  const SUFFIXES = [
    "scheduled_charging_start", "scheduled_charging_end", "engine_coolant_temperature",
    "range_at_full_charge", "interior_temperature", "exterior_temperature",
    "time_to_full_charge", "average_consumption", "window_ventilation",
    "distance_to_service", "days_to_service", "last_updated",
    "scheduled_charging", "charger_connection", "tire_front_right",
    "tire_front_left", "tire_rear_right", "door_rear_right", "charging_power",
    "seat_heat_passenger", "seat_vent_passenger", "seat_heat_driver",
    "seat_vent_driver", "sunshade", "g_clean", "parking_comfort",
    "steering_wheel_heat",
    "tire_rear_left", "door_rear_left", "combined_range", "charge_complete",
    "charge_voltage", "electric_range", "charge_current", "door_passenger",
    "charger_plug", "total_mileage", "refresh_data", "unlock_trunk",
    "door_driver", "12v_battery", "trip_meter", "fuel_level", "fuel_range",
    "location",
    "fuel_level_pct",
    "pack_power", "efficiency", "find_car", "connected", "defrost", "sunroof",
    // Average Speed and Engine Speed both produce ids ending in "_speed", and
    // Engine State is read for the driving lock, so all three are listed: the
    // sort below is longest-first, which is what stops a renamed trip-average
    // entity resolving as the live speed and pinning every card to "Driving".
    // These are entity-id spellings, not the definition keys - the sensors set
    // has_entity_name, so the id comes from the friendly name ("Average Speed"
    // is keyed `avg_speed` and lands at `..._average_speed`).
    "average_speed", "engine_speed", "engine_state",
    "charging", "climate", "battery", "trunk", "doors", "speed", "hood",
  ].sort((a, b) => b.length - a.length);

  /* Markets that drive on the left put the driver's door on the RIGHT of a
   * top-down drawing. The car's own door sensors are role-named (driver /
   * passenger), so the top view must know which side the role sits on. */
  const RHD_COUNTRIES = new Set([
    "AU", "BD", "BN", "BT", "BW", "CY", "FJ", "GB", "GY", "HK", "ID", "IE",
    "IN", "JM", "JP", "KE", "LK", "LS", "MO", "MT", "MU", "MV", "MW", "MY",
    "MZ", "NA", "NP", "NZ", "PG", "PK", "SG", "SR", "SZ", "TH", "TT", "TZ",
    "UG", "ZA", "ZM", "ZW",
  ]);

  /* Where English-speakers call it a boot rather than a trunk. Not the same
   * list as RHD_COUNTRIES - India drives on the left and says boot, the
   * Philippines drives on the right and says trunk - so it is its own set.
   * Only the card's own wording; the entity keeps its name. */
  const BOOT_COUNTRIES = new Set([
    "AU", "BD", "BW", "CY", "FJ", "GB", "GH", "GY", "HK", "IE", "IN", "JM",
    "KE", "LK", "LS", "MT", "MU", "MW", "MY", "NA", "NG", "NP", "NZ", "PG",
    "PK", "SG", "SZ", "TT", "TZ", "UG", "ZA", "ZM", "ZW",
  ]);

  /* Engine-state readings that mean the car is live. Mirrors _ENGINE_RUNNING in
   * __init__.py, plus "running" as the mapped display value the sensor shows. */
  const DRIVING_STATES = new Set([
    "engine_running", "running", "on", "1", "true",
  ]);

  /* Folded into every card's change signature on top of its own list, because
   * the driving banner is drawn for all of them by the base class. */
  /* Wait after a command before another may be sent, in seconds. Long enough
   * that a second tap does not arrive while the car is still executing the
   * first, short enough that the card does not feel broken. `cooldown:` in the
   * card config overrides it, and 0 turns the wait off. */
  /* Where "navigate to the car" can send you. Requested per app - Apple Maps on
   * #26, HERE WeGo on #27, the latter with the URL its own user had already
   * worked out - so it is a list rather than a fixed pair, and asking for a
   * fifth is a one-line change.
   *
   * No travel mode is sent unless `nav_travel:` asks for one: each app then uses
   * its own default. Guessing is wrong in both directions - you walk to a car
   * parked round the corner and drive to one left at the airport - and the app
   * knows better than the card does.
   */
  const NAV_APPS = {
    maps: {
      label: "Maps",
      url: (at, mode) => "https://www.google.com/maps/dir/?api=1&destination=" + at
        + (mode ? "&travelmode=" + mode : ""),
    },
    waze: {
      // Waze navigates by car by definition; it has no walking mode to pass.
      label: "Waze",
      url: (at) => "https://www.waze.com/ul?ll=" + at + "&navigate=yes",
    },
    apple: {
      label: "Apple Maps",
      url: (at, mode) => "https://maps.apple.com/?daddr=" + at
        + (mode === "walking" ? "&dirflg=w"
          : mode === "transit" ? "&dirflg=r"
          : mode ? "&dirflg=d" : ""),
    },
    here: {
      // share.here.com opens the HERE WeGo app when it is installed and the web
      // map when it is not. `m=w` is its walking flag.
      label: "HERE",
      url: (at, mode) => "https://share.here.com/r/" + at
        + (mode === "walking" ? "?m=w" : mode ? "?m=d" : ""),
    },
  };

  /* Unchanged for anyone who has not asked: two links, not four. */
  const DEFAULT_NAV = ["maps", "waze"];

  /* The car does not report this feature's state - the field that looks like its
   * flag reads 1 with the feature off (#13) - so the switch reads unknown and the
   * button cannot honestly light up. Say that on the control. */
  const PCOMFORT_HINT = "Parking Comfort: keeps the cabin comfortable while the "
    + "car is parked. Your car does not report whether it is on, so this button "
    + "toggles it without showing a state.";

  /* These two buttons fire the car's own Rapid Warming / Rapid Cooling presets,
   * not a plain "start heating". Labelled Heat and Cool they read as the latter -
   * an owner said so: "I initially thought heat just turned on heating, not rapid
   * heating" (#29). So the label says rapid and the tooltip says what that
   * actually does to the car. */
  const RAPID_HEAT_HINT = "Rapid warming: the car's own preset - drives the "
    + "setpoint to its maximum and heats both front seats, plus the steering "
    + "wheel where fitted";
  /* Read off the card by an owner who compared it with his car's screen and
   * reported the integration as wrong by a factor of two (#50). Both numbers
   * were right: this row is the average since the car was new, and a car
   * screen almost always shows the current trip. A real payload has the two
   * 48% apart in the same poll. The label now says which one this is, and the
   * tooltip says where the other one lives. */
  const LIFETIME_HINT = "Average since the car was new. Your car's screen "
    + "usually shows the current trip instead, which reads lower - that is "
    + "the Trip Consumption entity, not this one.";
  const RAPID_COOL_HINT = "Rapid cooling: the car's own preset - drives the "
    + "setpoint to its minimum, ventilates both front seats and cracks the windows";
  /* The steering-wheel command is a capture of the official app's own button,
   * confirmed on a real car to turn the wheel on. Its status field is slow to
   * catch up, so the switch holds its requested state briefly (optimistic). */
  const SWHEEL_HINT = "Steering wheel heating on / off. Confirmed on a real "
    + "car; the reading is slow, so the button holds its state for a moment.";

  const DEFAULT_COOLDOWN_S = 3;
  /* How long the temperature stepper waits for the tapping to stop. */
  const TEMP_SETTLE_MS = 1100;

  const ALWAYS_WATCHED = [
    "sensor.speed", "sensor.engine_state",
    // The propulsion split: _carState() reads these for every card, so a
    // card that did not watch them would freeze a hybrid's fuel bar and
    // headline exactly the way the door count froze once.
    "switch.parking_comfort",
    "sensor.fuel_level", "sensor.fuel_level_pct", "sensor.fuel_range",
    "sensor.combined_range",
  ];

  const ACCENT = "var(--geely-accent, #2fd6a4)";
  const AMBER = "var(--geely-warn, #e8a13a)";
  const ON_TEXT = `color-mix(in srgb, ${ACCENT} 76%, var(--primary-text-color, #1c1c1e))`;
  const WARN_TEXT = `color-mix(in srgb, ${AMBER} 76%, var(--primary-text-color, #1c1c1e))`;

  /* ------------------------------------------------------------ helpers -- */

  const NUM = (st) => {
    if (!st) return null;
    const v = parseFloat(st.state);
    return Number.isFinite(v) ? v : null;
  };
  const OK = (st) => st && st.state !== "unavailable" && st.state !== "unknown";
  const UNIT = (st) => (st && st.attributes.unit_of_measurement) || "";
  const esc = (s) =>
    String(s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));

  /* --------------------------------------------------------- the car ----- */
  /* A generic electric crossover, side profile, drawn here - painted body,
   * glass band with a reflection streak, aero wheels, LED lamps, a charge
   * port that lights while charging, and amber indicator dots over the
   * hood / doors / trunk that appear when something is open.
   * Gradient ids are safe: each card lives in its own shadow root. */
  const CAR_SVG = (cls, open = {}) => `
    <svg class="car ${cls}" viewBox="0 0 760 300" fill="none" aria-hidden="true">
      <defs>
        <linearGradient id="gp" x1="0" y1="29" x2="0" y2="262" gradientUnits="userSpaceOnUse">
          <stop offset="0" stop-color="#dde2e8"/>
          <stop offset=".42" stop-color="#b3bac4"/>
          <stop offset=".78" stop-color="#87909b"/>
          <stop offset="1" stop-color="#6d7681"/>
        </linearGradient>
        <linearGradient id="gg" x1="0" y1="33" x2="0" y2="100" gradientUnits="userSpaceOnUse">
          <stop offset="0" stop-color="#3d4653"/>
          <stop offset="1" stop-color="#1c222b"/>
        </linearGradient>
        <linearGradient id="gr" x1="0" y1="0" x2="1" y2="0">
          <stop offset="0" stop-color="#e7ebef"/>
          <stop offset="1" stop-color="#b9c0c8"/>
        </linearGradient>
        <filter id="soft" x="-40%" y="-40%" width="180%" height="180%">
          <feGaussianBlur stdDeviation="7"/>
        </filter>
        <clipPath id="gc">
          <path d="M149 73 C165 56 190 45 220 40 C280 35 360 34 430 38
            C444 39 452 41 458 44 C478 55 504 74 520 88 L521 97
            C462 96 400 95 340 94 C300 93 260 92 230 90
            C200 88 170 82 149 73 Z"/>
        </clipPath>
        <mask id="arches">
          <rect x="0" y="0" width="760" height="300" fill="#fff"/>
          <circle cx="186" cy="199" r="70" fill="#000"/>
          <circle cx="574" cy="199" r="70" fill="#000"/>
        </mask>
      </defs>

      <ellipse class="shadow" cx="381" cy="268" rx="320" ry="11" filter="url(#soft)"/>
      <ellipse class="glow" cx="381" cy="263" rx="300" ry="9" filter="url(#soft)"/>

      <g mask="url(#arches)">
        <path class="paint" d="
          M70 189
          C62 176 60 170 60 165
          C60 152 60 145 61 138
          C63 128 66 121 70 117
          L80 100
          C84 86 94 64 108 52
          C112 44 114 40 118 36
          C126 32 140 33 159 34
          C210 30 300 29 360 31
          C400 31 435 33 462 38
          C486 50 512 74 536 88
          C566 96 604 103 639 110
          C658 113 676 120 690 130
          C698 137 701 148 701 160
          C701 175 700 185 697 196
          C694 206 688 213 676 217
          C640 220 600 221 560 221
          L240 221
          C180 220 130 218 101 215
          C88 212 76 201 70 189 Z"/>
        <path class="rocker" d="M248 209 L510 209 L510 222 L248 222 Z"/>
      </g>

      <path class="cladding" d="M116 210 A70 70 0 0 1 256 210"/>
      <path class="cladding" d="M504 210 A70 70 0 0 1 644 210"/>

      <path class="paint" d="M466 35 C460 26 450 24 442 27 L443 35 Z"/>

      <path class="crease" d="M121 109 C250 106 500 106 632 111"/>
      <path class="haunch" d="M246 143 C320 158 430 152 502 136"/>

      <path class="glass" d="M149 73 C165 56 190 45 220 40 C280 35 360 34 430 38
        C444 39 452 41 458 44 C478 55 504 74 520 88 L521 97
        C462 96 400 95 340 94 C300 93 260 92 230 90
        C200 88 170 82 149 73 Z"/>
      <path class="streak" clip-path="url(#gc)" d="M380 42 L330 92 M420 41 L368 93"/>

      <path class="mirror" d="M506 90 C502 82 509 76 518 77 C526 78 529 84 526 90
        L524 96 C516 98 509 96 506 90 Z"/>

      <rect class="handle" x="214" y="110" width="26" height="5" rx="2.5"/>
      <rect class="handle" x="350" y="111" width="26" height="5" rx="2.5"/>
      <rect class="handle" x="646" y="192" width="46" height="23" rx="7"/>

      <path class="headlight" d="M634 111 C655 114 672 119 684 126 L679 135
        C665 128 648 123 630 120 Z"/>
      <path class="taillight" d="M63 103 L118 108 L117 117 L64 113 Z"/>
      <rect class="port" x="510" y="118" width="13" height="12" rx="4"/>
      <circle class="portdot" cx="516.5" cy="124" r="3"/>

      <g class="wheel-g" transform="translate(186 199)">${_WHEEL}</g>
      <g class="wheel-g" transform="translate(574 199)">${_WHEEL}</g>

      <circle class="ind ${open.trunk ? "on" : ""}" cx="95" cy="92" r="7"/>
      <circle class="ind ${open.rear ? "on" : ""}" cx="258" cy="120" r="7"/>
      <circle class="ind ${open.front ? "on" : ""}" cx="392" cy="118" r="7"/>
      <circle class="ind ${open.hood ? "on" : ""}" cx="598" cy="116" r="7"/>
    </svg>`;

  const _WHEEL = `
      <circle class="tire" r="62"/>
      <circle class="disc" r="42"/>
      <g class="spokes">
        <path d="M-6 -40 L-1 -41 L3 -12 L-4 -11 Z"/>
        <path d="M2 -41 L7 -40 L6 -32 L4 -13 Z" opacity=".45"/>
        <path d="M-6 -40 L-1 -41 L3 -12 L-4 -11 Z" transform="rotate(72)"/>
        <path d="M2 -41 L7 -40 L6 -32 L4 -13 Z" opacity=".45" transform="rotate(72)"/>
        <path d="M-6 -40 L-1 -41 L3 -12 L-4 -11 Z" transform="rotate(144)"/>
        <path d="M2 -41 L7 -40 L6 -32 L4 -13 Z" opacity=".45" transform="rotate(144)"/>
        <path d="M-6 -40 L-1 -41 L3 -12 L-4 -11 Z" transform="rotate(216)"/>
        <path d="M2 -41 L7 -40 L6 -32 L4 -13 Z" opacity=".45" transform="rotate(216)"/>
        <path d="M-6 -40 L-1 -41 L3 -12 L-4 -11 Z" transform="rotate(288)"/>
        <path d="M2 -41 L7 -40 L6 -32 L4 -13 Z" opacity=".45" transform="rotate(288)"/>
      </g>
      <circle class="rimring" r="42"/>
      <circle class="hubcap" r="8"/>`;


  /* Top view for the cockpit card - the same EX5 proportions (4615 x 1901
   * mm), front pointing up. Every openable part carries its status right
   * where it sits on the body: pressures beside each wheel, doors on their
   * sills, hood / sunroof / trunk on their panels. */
  const CAR_TOP_SVG = (cls, d) => {
    const stat = (open) => (open == null ? "—" : open ? "Open" : "Closed");
    const cl = (open) => (open ? "tv-stat open" : "tv-stat");
    return `
    <svg class="cartop ${cls}" viewBox="0 0 400 720" fill="none" aria-hidden="true">
      <defs>
        <linearGradient id="tp" x1="0" y1="34" x2="0" y2="686" gradientUnits="userSpaceOnUse">
          <stop offset="0" stop-color="#dde2e8"/>
          <stop offset=".45" stop-color="#b6bdc7"/>
          <stop offset="1" stop-color="#7f8894"/>
        </linearGradient>
        <linearGradient id="tg" x1="0" y1="196" x2="0" y2="620" gradientUnits="userSpaceOnUse">
          <stop offset="0" stop-color="#39424f"/>
          <stop offset="1" stop-color="#171c24"/>
        </linearGradient>
        <filter id="tsoft" x="-40%" y="-40%" width="180%" height="180%">
          <feGaussianBlur stdDeviation="8"/>
        </filter>
      </defs>

      <ellipse class="glow" cx="200" cy="368" rx="150" ry="330" filter="url(#tsoft)"/>

      <rect class="tire" x="42" y="105" width="24" height="96" rx="10"/>
      <rect class="tire" x="334" y="105" width="24" height="96" rx="10"/>
      <rect class="tire" x="42" y="486" width="24" height="96" rx="10"/>
      <rect class="tire" x="334" y="486" width="24" height="96" rx="10"/>

      <path class="paint" d="
        M200 34
        C148 34 108 44 92 64
        C78 82 72 112 70 156
        C68 220 68 320 70 400
        C71 470 73 540 80 596
        C88 652 132 686 200 686
        C268 686 312 652 320 596
        C327 540 329 470 330 400
        C332 320 332 220 330 156
        C328 112 322 82 308 64
        C292 44 252 34 200 34 Z"/>

      <path class="mirror" d="M70 214 C52 204 40 203 33 209 C30 216 37 223 50 225 L70 228 Z"/>
      <path class="mirror" d="M330 214 C348 204 360 203 367 209 C370 216 363 223 350 225 L330 228 Z"/>

      <path class="headlight" d="M94 62 q26 -13 50 -15 l1 9 q-23 3 -45 13 z"/>
      <path class="headlight" d="M306 62 q-26 -13 -50 -15 l-1 9 q23 3 45 13 z"/>
      <rect class="lightbar" x="122" y="662" width="156" height="7" rx="3.5"/>

      <path class="seam" d="M118 58 C104 100 98 150 96 192"/>
      <path class="seam" d="M282 58 C296 100 302 150 304 192"/>

      <path class="glass" d="M92 200 C130 190 270 190 308 200 L288 258 C240 250 160 250 112 258 Z"/>
      <rect class="glass" x="116" y="264" width="168" height="308" rx="26"/>
      <path class="glass" d="M116 576 C160 585 240 585 284 576 L296 614 C250 624 150 624 104 614 Z"/>
      <rect class="sunline ${d.sunroof === true ? "open" : ""}" x="124" y="376" width="152" height="2.5" rx="1.25"/>

      <path class="seam" d="M70 262 L94 258 M70 352 L94 350 M70 448 L94 444"/>
      <path class="seam" d="M330 262 L306 258 M330 352 L306 350 M330 448 L306 444"/>

      <rect class="port" x="66" y="552" width="13" height="15" rx="4"/>
      <circle class="portdot" cx="72.5" cy="559.5" r="3"/>

      <circle class="ind ${d.doors.fl ? "on" : ""}" cx="84" cy="306" r="6"/>
      <circle class="ind ${d.doors.rl ? "on" : ""}" cx="84" cy="400" r="6"/>
      <circle class="ind ${d.doors.fr ? "on" : ""}" cx="316" cy="306" r="6"/>
      <circle class="ind ${d.doors.rr ? "on" : ""}" cx="316" cy="400" r="6"/>

      <text class="tv-val" x="42" y="84" text-anchor="middle">${d.tires.fl.split(" ")[0]}</text>
      <text class="tv-lab" x="42" y="100" text-anchor="middle">${esc((d.tires.fl.split(" ")[1] || "").toUpperCase())} FL</text>
      <text class="tv-val" x="358" y="84" text-anchor="middle">${d.tires.fr.split(" ")[0]}</text>
      <text class="tv-lab" x="358" y="100" text-anchor="middle">${esc((d.tires.fr.split(" ")[1] || "").toUpperCase())} FR</text>
      <text class="tv-val" x="42" y="612" text-anchor="middle">${d.tires.rl.split(" ")[0]}</text>
      <text class="tv-lab" x="42" y="628" text-anchor="middle">${esc((d.tires.rl.split(" ")[1] || "").toUpperCase())} RL</text>
      <text class="tv-val" x="358" y="612" text-anchor="middle">${d.tires.rr.split(" ")[0]}</text>
      <text class="tv-lab" x="358" y="628" text-anchor="middle">${esc((d.tires.rr.split(" ")[1] || "").toUpperCase())} RR</text>

      <text class="${cl(d.doors.fl)}" x="58" y="311" text-anchor="end">${stat(d.doors.fl)}</text>
      <text class="${cl(d.doors.rl)}" x="58" y="405" text-anchor="end">${stat(d.doors.rl)}</text>
      <text class="${cl(d.doors.fr)}" x="342" y="311">${stat(d.doors.fr)}</text>
      <text class="${cl(d.doors.rr)}" x="342" y="405">${stat(d.doors.rr)}</text>

      <text class="tv-lab onbody" x="200" y="122" text-anchor="middle">HOOD</text>
      <text class="${cl(d.hood)} onbody" x="200" y="142" text-anchor="middle">${stat(d.hood)}</text>
      ${d.sunroof == null ? "" : `
      <text class="tv-lab onglass" x="200" y="322" text-anchor="middle">SUNROOF</text>
      <text class="${cl(d.sunroof)} onglass" x="200" y="342" text-anchor="middle">${stat(d.sunroof)}</text>`}
      <text class="tv-lab onbody" x="200" y="634" text-anchor="middle">${d.bootWord}</text>
      <text class="${cl(d.trunk)} onbody" x="200" y="654" text-anchor="middle">${stat(d.trunk)}</text>
    </svg>`;
  };

  /* ----------------------------------------------------------- icons ----- */
  /* One cohesive hand-drawn stroke set - 24px grid, 1.8 stroke, round caps. */

  const ICONS = {
    nav: `<path d="M20 4 10.5 20l-1.2-6.3L3 12.5z"/>`,
    // Parking Comfort keeps the cabin liveable while the car sits, which is
    // what mdi:sleep says on the switch itself - so a crescent, not the
    // climate fan it was borrowing.
    sleep: `<path d="M20 14.5A8.5 8.5 0 0 1 9.5 4a7.5 7.5 0 1 0 10.5 10.5z"/>`,
    driving: `<path d="M3.5 14.5h17M5 14.5l1.7-4.6A2 2 0 0 1 8.6 8.5h6.8a2 2 0 0 1 1.9 1.4l1.7 4.6"/>
              <path d="M4.5 14.5v3h3v-3M16.5 14.5v3h3v-3"/>
              <circle cx="7.5" cy="15.6" r="1.1"/><circle cx="16.5" cy="15.6" r="1.1"/>`,
    lock: `<rect x="5" y="10.5" width="14" height="9.5" rx="2.5"/>
           <path d="M8 10.5V7.5a4 4 0 0 1 8 0v3"/><circle cx="12" cy="15.2" r="1.4"/>`,
    unlock: `<rect x="5" y="10.5" width="14" height="9.5" rx="2.5"/>
             <path d="M16 10.5V7.5a4 4 0 0 0-7.6-1.7"/><circle cx="12" cy="15.2" r="1.4"/>`,
    climate: `<circle cx="12" cy="12" r="1.9"/>
              <path d="M12 9.7C12 6.6 10.9 4.6 8.9 4.8 7.2 5 6.9 7 8.3 8.3c.9.9 2.2 1.3 3.7 1.4z"/>
              <path d="M12 9.7C12 6.6 10.9 4.6 8.9 4.8 7.2 5 6.9 7 8.3 8.3c.9.9 2.2 1.3 3.7 1.4z" transform="rotate(120 12 12)"/>
              <path d="M12 9.7C12 6.6 10.9 4.6 8.9 4.8 7.2 5 6.9 7 8.3 8.3c.9.9 2.2 1.3 3.7 1.4z" transform="rotate(240 12 12)"/>`,
    defrost: `<path d="M4.5 12.5c0-4.5 3.4-7.5 7.5-7.5s7.5 3 7.5 7.5"/>
              <path d="M8.3 12c-1 2-1 3.5 0 5.5M12 12c-1 2-1 3.5 0 5.5M15.7 12c-1 2-1 3.5 0 5.5"/>`,
    vent: `<rect x="4.5" y="5" width="15" height="12" rx="2"/>
           <path d="M4.5 9.5h15M12 21v-6.2M9.6 17.2 12 14.8l2.4 2.4"/>`,
    trunk: `<path d="M4.5 19v-6l3-2h9l3 3v5"/><path d="M4.5 16h15"/>
            <path d="M7.5 11 12 4.8l7 3.2"/><circle cx="16.5" cy="18" r="0"/>`,
    find: `<path d="M12 20.5s-6-5.1-6-9.5a6 6 0 0 1 12 0c0 4.4-6 9.5-6 9.5z"/>
           <circle cx="12" cy="10.8" r="2.2"/>`,
    refresh: `<path d="M18.6 9A7 7 0 1 0 19 13"/><path d="M19.2 4.6V9h-4.4"/>`,
    bolt: `<path d="M12.8 3.5 6.5 13h4l-1.3 7.5L15.5 11h-4z"/>`,
    charge: `<path d="M7 20v-9.5a5 5 0 0 1 10 0V20"/><path d="M9.5 6V3.5M14.5 6V3.5"/>
             <path d="M12.7 11.5 10.4 15h3.2l-2.3 3.5"/>`,
    tire: `<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3.2"/>
           <path d="M12 4v2.6M12 17.4V20M4 12h2.6M17.4 12H20"/>`,
    heat: `<path d="M12 4c2 3.2 5 5 5 8.6a5 5 0 0 1-10 0C7 9 10 7.2 12 4z"/>
           <path d="M12 20.5v-3"/>`,
    seatheat: `<path d="M6.5 4.5v7.5a2.5 2.5 0 0 0 2.5 2.5h6"/>
               <path d="M6.5 12h6a3 3 0 0 1 3 3v4.5"/>
               <path d="M13 5c-.8 1.2-.8 2 0 3.2M16.5 5c-.8 1.2-.8 2 0 3.2"/>`,
    seatvent: `<path d="M6.5 4.5v7.5a2.5 2.5 0 0 0 2.5 2.5h6"/>
               <path d="M6.5 12h6a3 3 0 0 1 3 3v4.5"/>
               <path d="M13.5 5l2.5 2.5M16 5l-2.5 2.5"/>`,
    roof: `<path d="M4 14.5c0-5 3.6-8.5 8-8.5s8 3.5 8 8.5"/>
           <path d="M8 10.5h8M9.5 6.9v3.6M14.5 6.9v3.6"/>`,
    shade: `<path d="M4 14.5c0-5 3.6-8.5 8-8.5s8 3.5 8 8.5"/>
            <path d="M6 12h12M8.5 12c0 2.5 1.5 4 3.5 4s3.5-1.5 3.5-4"/>`,
    fresh: `<path d="M5 9.5c2.5 0 2.5-2 5-2s2.5 2 5 2 2.5-2 5-2"/>
            <path d="M5 14c2.5 0 2.5-2 5-2s2.5 2 5 2 2.5-2 5-2"/>
            <path d="M5 18.5c2.5 0 2.5-2 5-2s2.5 2 5 2 2.5-2 5-2"/>`,
    steering: `<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="2"/>
               <path d="M12 4v6M6.2 15l4.4-2.5M17.8 15l-4.4-2.5"/>`,
    minus: `<path d="M6 12h12"/>`,
    plus: `<path d="M12 6v12M6 12h12"/>`,
    cool: `<path d="M12 3.5v17M4.6 7.75l14.8 8.5M19.4 7.75 4.6 16.25"/>`,
    trip: `<path d="M5 19c6 0 3-7 9-7 4.5 0 3.5-5.5 5-7"/>
           <circle cx="5" cy="19" r="1.6"/><circle cx="19" cy="5" r="1.6"/>`,
    fuel: `<path d="M5.5 20V6a2 2 0 0 1 2-2h5a2 2 0 0 1 2 2v14"/><path d="M4 20h10.5"/>
           <path d="M14.5 10h2l2 2v5a1.5 1.5 0 0 1-3 0v-7.5"/><path d="M6.5 7h5v4h-5z"/>`,
  };
  const icon = (name) =>
    `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
          stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name]}</svg>`;
  const iconFilled = (name) =>
    `<svg viewBox="0 0 24 24" fill="currentColor" stroke="none" aria-hidden="true">${ICONS[name]}</svg>`;

  const BASE_CSS = `
    :host { display: block; }
    * { box-sizing: border-box; margin: 0; }
    .shell {
      position: relative; overflow: hidden;
      /* Makes the card itself the thing narrow-width rules are measured
       * against - see the @container rule below. The card is as narrow inside
       * a desktop dashboard column as it is on a phone, and a viewport media
       * query cannot tell the two apart. */
      container-type: inline-size;
      border-radius: var(--ha-card-border-radius, 12px);
      background:
        radial-gradient(120% 90% at 85% -10%,
          color-mix(in srgb, ${ACCENT} 9%, transparent), transparent 60%),
        var(--ha-card-background, var(--card-background-color, #fff));
      border: 1px solid var(--divider-color, rgba(120,130,140,.2));
      color: var(--primary-text-color, #1c1c1e);
      padding: 18px 20px 16px;
      font-family: var(--ha-card-font-family, var(--paper-font-body1_-_font-family, inherit));
    }
    .micro {
      font-size: 10px; font-weight: 600; letter-spacing: .18em;
      text-transform: uppercase; color: var(--secondary-text-color, #7a7f87);
      display: flex; align-items: center; gap: 6px;
    }
    .micro svg { width: 13px; height: 13px; opacity: .8; }
    .hairline { border: 0; border-top: 1px solid var(--divider-color, rgba(120,130,140,.18)); margin: 14px 0 12px; }
    .num { font-weight: 200; font-variant-numeric: tabular-nums; letter-spacing: -0.02em; line-height: 1; }

    .car { width: 100%; height: auto; display: block; }
    .car .shadow { fill: rgba(0,0,0,.28); }
    .car .glow { fill: transparent; transition: fill .5s ease; }
    .car .paint { fill: var(--geely-car-paint, url(#gp)); stroke: rgba(0,0,0,.18); stroke-width: 1.5; }
    .car .rocker { fill: rgba(0,0,0,.18); }
    .car .crease { stroke: rgba(255,255,255,.4); stroke-width: 1.6; fill: none; }
    .car .glass { fill: url(#gg); }
    .car .streak { stroke: rgba(255,255,255,.14); stroke-width: 7; stroke-linecap: round; }
    .car .ducktail { stroke: rgba(0,0,0,.4); stroke-width: 4; stroke-linecap: round; }
    .car .haunch { stroke: rgba(255,255,255,.28); stroke-width: 2.5; fill: none; }
    .car .pillar { stroke: rgba(0,0,0,.4); stroke-width: 4; }
    .car .mirror { fill: #6d7681; stroke: rgba(0,0,0,.2); }
    .car .handle { fill: rgba(0,0,0,.25); }
    .car .headlight { fill: #eef4fa; stroke: rgba(0,0,0,.12); }
    .car .taillight { fill: #d05252; opacity: .9; }
    .car .port { fill: rgba(0,0,0,.22); }
    .car .portdot { fill: rgba(255,255,255,.35); transition: fill .3s ease; }
    .car .tire { fill: #20242a; }
    .car .disc { fill: none; stroke: #4a525b; stroke-width: 9; }
    .car .hubcap { fill: #cfd5db; }
    .car .cladding { stroke: #262b31; stroke-width: 14; fill: none; }
    .car .mirror { fill: #3a424d; }
    .car .spokes path { fill: #cfd5db; }
    .car .disc { fill: #23272d; }
    .car .rimring { fill: none; stroke: #b9c0c8; stroke-width: 2.5; }
    .car .ind { fill: ${AMBER}; opacity: 0; transition: opacity .3s ease; }
    .car .ind.on { opacity: 1; animation: geely-blink 1.4s ease infinite; }
    @keyframes geely-blink { 50% { opacity: .35; } }
    .car.charging .glow { fill: color-mix(in srgb, ${ACCENT} 38%, transparent); }
    .car.charging .portdot { fill: ${ACCENT}; filter: drop-shadow(0 0 4px ${ACCENT}); }

    .chips { display: flex; flex-wrap: wrap; gap: 6px; }
    .chip {
      display: inline-flex; align-items: center; gap: 5px;
      font-size: 11px; font-weight: 500; padding: 4px 10px; border-radius: 999px;
      border: 1px solid var(--divider-color, rgba(120,130,140,.25));
      color: var(--secondary-text-color, #7a7f87); white-space: nowrap;
    }
    .chip svg { width: 12px; height: 12px; }
    .chip.on {
      color: ${ON_TEXT};
      border-color: color-mix(in srgb, ${ACCENT} 55%, transparent);
      background: color-mix(in srgb, ${ACCENT} 15%, transparent);
      font-weight: 600;
    }
    .chip.warn {
      color: ${WARN_TEXT};
      border-color: color-mix(in srgb, ${AMBER} 55%, transparent);
      background: color-mix(in srgb, ${AMBER} 15%, transparent);
      font-weight: 600;
    }
    .actions { display: flex; gap: 8px; justify-content: space-between; }
    .act {
      flex: 1; display: flex; flex-direction: column; align-items: center; gap: 5px;
      padding: 10px 2px 8px; border-radius: 14px; border: 1px solid var(--divider-color, rgba(120,130,140,.2));
      background: transparent; color: var(--primary-text-color); cursor: pointer;
      transition: transform .08s ease, border-color .15s ease, color .15s ease;
      -webkit-tap-highlight-color: transparent;
    }
    .act svg { width: 21px; height: 21px; }
    .act span { font-size: 9.5px; font-weight: 600; letter-spacing: .08em; text-transform: uppercase;
                color: var(--secondary-text-color); }
    .act:active { transform: scale(.94); }
    .act:hover { border-color: color-mix(in srgb, currentColor 35%, transparent); }
    /* Active state. It used to be a 45%-tinted outline and a coloured icon,
     * which on the mini and strip cards - where the labels are hidden - left an
     * "on" button almost indistinguishable from an off one. A state worth
     * showing is worth showing properly: a filled tint so it reads as solid, a
     * firmer edge, a soft lift, and the label joining in rather than staying
     * grey. Same language for amber, so "active" and "needs a look" are told
     * apart by hue and never by intensity. */
    .act.on {
      color: ${ON_TEXT};
      border-color: color-mix(in srgb, ${ACCENT} 60%, transparent);
      background: color-mix(in srgb, ${ACCENT} 16%, transparent);
      box-shadow: inset 0 0 0 1px color-mix(in srgb, ${ACCENT} 22%, transparent),
                  0 3px 14px -8px color-mix(in srgb, ${ACCENT} 90%, transparent);
    }
    .act.on span { color: ${ON_TEXT}; }
    .act.on svg { opacity: 1; }
    .act.on:hover { background: color-mix(in srgb, ${ACCENT} 24%, transparent); }
    .act.armed {
      color: ${WARN_TEXT};
      border-color: ${AMBER};
      background: color-mix(in srgb, ${AMBER} 18%, transparent);
      animation: geely-arm 1s ease infinite;
    }
    .act.armed span { color: ${WARN_TEXT}; }
    .act[disabled] { opacity: .35; pointer-events: none; }

    /* Navigation links. Deliberately quieter than the command buttons beside
     * them: they open a map app rather than doing anything to the car. */
    .cbtn.nav { text-decoration: none; color: var(--secondary-text-color); }
    .cbtn.nav:hover { color: var(--primary-text-color); }

    /* The second bar, on a car with a tank. Deliberately not the accent
     * colour: the two bars must not be mistaken for one another at a glance. */
    .bar.fuel { margin-top: 4px; }
    .bar.fuel i { background: var(--geely-fuel, #8b93a1); }

    /* While the car is moving. Amber rather than red: nothing is wrong, the
     * controls are simply not available yet. */
    .driving {
      display: flex; align-items: center; gap: 10px;
      margin: 0 0 14px; padding: 9px 12px;
      border-radius: 12px;
      border: 1px solid color-mix(in srgb, ${AMBER} 40%, transparent);
      background: color-mix(in srgb, ${AMBER} 12%, transparent);
      color: ${AMBER};
    }
    .driving svg { width: 20px; height: 20px; flex: none; }
    .driving span {
      display: flex; flex-direction: column; gap: 1px; min-width: 0;
      font-size: 11px; line-height: 1.35;
      color: var(--secondary-text-color, #7a7f87);
    }
    .driving b {
      font-size: 10px; font-weight: 700; letter-spacing: .18em;
      text-transform: uppercase; color: ${AMBER};
    }
    /* Every disabled control reads the same, whichever class the card used. */
    .blocked { opacity: .35 !important; pointer-events: none; filter: saturate(.4); }
    @keyframes geely-arm { 50% { border-color: color-mix(in srgb, ${AMBER} 35%, transparent); } }
    .bar { position: relative; height: 5px; border-radius: 999px; overflow: hidden;
           background: color-mix(in srgb, currentColor 12%, transparent); }
    .bar > i { position: absolute; inset: 0 auto 0 0; border-radius: 999px;
               background: ${ACCENT}; transition: width .6s ease; }
    .bar.low > i { background: ${AMBER}; }
    .bar.charging > i::after {
      content: ""; position: absolute; inset: 0;
      background: linear-gradient(90deg, transparent, rgba(255,255,255,.5), transparent);
      animation: geely-shimmer 1.6s linear infinite;
    }
    @keyframes geely-shimmer { from { transform: translateX(-100%);} to { transform: translateX(100%);} }
    .unavail { opacity: .5; }
    .crow { display:flex; gap:8px; align-items:stretch; margin-top:8px; }
    .crow.wrap { flex-wrap: wrap; }
    .temp { display:flex; align-items:center; gap:2px; flex:none;
            border:1px solid var(--divider-color, rgba(120,130,140,.25));
            border-radius:14px; padding:2px; }
    .temp.on {
      border-color: color-mix(in srgb, ${ACCENT} 60%, transparent);
      background: color-mix(in srgb, ${ACCENT} 12%, transparent);
    }
    .temp.on .tval { color: ${ON_TEXT}; }
    .temp .tval { min-width:52px; text-align:center; font-size:16px; font-weight:600;
                  font-variant-numeric: tabular-nums; }
    .csub { margin-top:10px; }
    .cstep { border:none; background:none; color:var(--primary-text-color);
             width:40px; height:40px; border-radius:12px; cursor:pointer;
             display:flex; align-items:center; justify-content:center;
             -webkit-tap-highlight-color: transparent; }
    .cstep:active { transform: scale(.92); }
    .cstep svg { width:16px; height:16px; }
    .cbtn { display:flex; align-items:center; gap:6px; padding:10px 12px;
            border:1px solid var(--divider-color, rgba(120,130,140,.25));
            border-radius:14px; background:none; cursor:pointer;
            color: var(--primary-text-color); font: inherit; font-size:12px;
            min-height:40px; -webkit-tap-highlight-color: transparent;
            transition: transform .1s ease; }
    .cbtn:active { transform: scale(.95); }
    .cbtn svg { width:17px; height:17px; }
    .cbtn b { font-weight:600; font-size:11px; color: var(--secondary-text-color); }
    .cbtn.on {
      color:${ON_TEXT};
      border-color: color-mix(in srgb, ${ACCENT} 60%, transparent);
      background: color-mix(in srgb, ${ACCENT} 16%, transparent);
      box-shadow: 0 3px 14px -8px color-mix(in srgb, ${ACCENT} 90%, transparent);
    }
    /* The level - Low / Medium / High - is the whole point of a seat button, so
     * it gets the strongest treatment on the card. */
    .cbtn.on b { color:${ON_TEXT}; font-weight: 700; }
    @media (hover: hover) {
      .cstep:hover, .cmini:hover { background: rgba(120,130,140,.15); }
    }
    /* The temperature stepper and the two rapid presets are one row, and on a
     * phone the third one dropped to a line of its own, leaving a gap beside
     * the second (#29). The row is pinned to a single line here and the
     * horizontal padding pays for it - nothing vertical changes, so every tap
     * target keeps its height, and the steppers keep their width.
     *
     * Only this row. The seat and air rows carry three to five buttons and are
     * meant to wrap; forcing them onto one line would truncate every label.
     * If a card is narrow enough that these labels still do not fit, they
     * ellipsise rather than wrap, which is the behaviour that was asked for. */
    @container (max-width: 380px) {
      .crow.temprow { flex-wrap: nowrap; gap: 6px; }
      .crow.temprow .cbtn { padding: 10px 9px; min-width: 0; }
      .crow.temprow .cbtn span {
        overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
      }
      .crow.temprow .temp .tval { min-width: 46px; }
    }
    .cpair { display:flex; align-items:center; gap:6px; padding:4px 6px 4px 12px;
             border:1px solid var(--divider-color, rgba(120,130,140,.25));
             border-radius:14px; font-size:12px; }
    .cpair svg { width:17px; height:17px; }
    .cpair.on {
      color:${ON_TEXT};
      border-color: color-mix(in srgb, ${ACCENT} 60%, transparent);
      background: color-mix(in srgb, ${ACCENT} 14%, transparent);
    }
    .ctime { display:flex; align-items:center; gap:7px; padding:0 6px 0 12px;
             border:1px solid var(--divider-color, rgba(120,130,140,.25));
             border-radius:14px; font-size:12px; color: var(--secondary-text-color); }
    .ctime input { border:none; background:none; color: var(--primary-text-color);
                   font: inherit; font-size:13px; font-variant-numeric: tabular-nums;
                   padding:9px 2px; min-height:38px; cursor:pointer; width:74px; }
    .ctime input:focus { outline:none; }
    .ctime input::-webkit-calendar-picker-indicator { filter: invert(.5); cursor:pointer; }
    .cmini { border:1px solid var(--divider-color, rgba(120,130,140,.3));
             background:none; color:var(--primary-text-color); font:inherit;
             font-size:11.5px; padding:8px 11px; min-height:34px; border-radius:10px;
             cursor:pointer; -webkit-tap-highlight-color: transparent; }
    .cmini:active { transform: scale(.95); }
    .cartop { display: block; width: 100%; }
    .cartop .paint { fill: var(--geely-car-paint, url(#tp)); stroke: rgba(0,0,0,.18); stroke-width: 1.5; }
    .cartop .glass { fill: url(#tg); }
    .cartop .tire { fill: #23272e; }
    .cartop .mirror { fill: #3a424d; }
    .cartop .seam { stroke: rgba(0,0,0,.22); stroke-width: 1.6; fill: none; }
    .cartop .headlight { fill: #eef4fa; stroke: rgba(0,0,0,.1); }
    .cartop .lightbar { fill: #d05252; opacity: .85; }
    .cartop .glow { fill: transparent; transition: fill .5s ease; }
    .cartop.charging .glow { fill: color-mix(in srgb, ${ACCENT} 30%, transparent); }
    .cartop.charging .portdot { fill: ${ACCENT}; filter: drop-shadow(0 0 4px ${ACCENT}); }
    .cartop .port { fill: rgba(0,0,0,.28); }
    .cartop .portdot { fill: rgba(255,255,255,.35); }
    .cartop .ind { fill: ${AMBER}; opacity: 0; }
    .cartop .ind.on { opacity: 1; animation: geely-blink 1.4s ease infinite; }
    .cartop .sunline { fill: rgba(255,255,255,.22); }
    .cartop .sunline.open { fill: ${AMBER}; }
    .cartop text { font-family: inherit; }
    .tv-val { font-size: 21px; font-weight: 600; fill: var(--primary-text-color);
              font-variant-numeric: tabular-nums; }
    .tv-lab { font-size: 12.5px; letter-spacing: .1em; fill: var(--secondary-text-color); }
    .tv-stat { font-size: 17px; font-weight: 700; fill: var(--primary-text-color); }
    .tv-lab.onbody { fill: #6b7280; }
    .tv-stat.onbody { fill: #3a4149; }
    .tv-stat.open.onbody { fill: #b97a14; }
    .tv-stat.open { fill: ${AMBER}; font-weight: 600; animation: geely-blink 1.4s ease infinite; }
    .tv-lab.onglass { fill: rgba(255,255,255,.55); }
    .tv-stat.onglass { fill: rgba(255,255,255,.8); }
    .tv-stat.open.onglass { fill: ${AMBER}; }
  `;

  /* ------------------------------------------------------------ base ----- */

  class GeelyCardBase extends HTMLElement {
    static getStubConfig() { return {}; }

    constructor() {
      super();
      this.attachShadow({ mode: "open" });
      // The picker (and some dashboards) set `hass` before `setConfig` -
      // every field the render path touches must already exist.
      this._config = {};
      this._prefix = null;
      this._hass = null;
      this._map = {};
      this._mapFor = null;
      this._armed = null;          // suffix of the armed destructive action
      this._armedTimer = null;
      this._sig = "";
    }

    disconnectedCallback() {
      // A pending arm-timeout must not fire a render on a removed card.
      clearTimeout(this._armedTimer);
      this._armed = null;
    }

    setConfig(config) {
      this._config = config || {};
      this._prefix = this._config.prefix || null;
      this._sig = "";
      if (this._hass) {
        this._prefix = this._prefix || this._detectPrefix(this._hass);
        this._safeRender();
      }
    }

    set hass(hass) {
      this._hass = hass;
      // Let go of a dialled-in temperature once the entity reports it, so the
      // card stops preferring a value that is no longer pending.
      if (this._pendingTemp != null) {
        const a = ((this._st("climate.climate") || {}).attributes) || {};
        if (Number(a.temperature) === this._pendingTemp) this._pendingTemp = null;
      }
      // Nothing in this setter may throw: an exception here happens outside
      // the render safety net and leaves the card picker's preview spinning.
      try {
        if (!this._prefix) this._prefix = this._detectPrefix(hass);
        const sig = this._signature(hass);
        if (sig === this._sig) return;
        this._sig = sig;
      } catch (err) {
        this._sig = "error";
      }
      this._safeRender();
    }

    /* A render that throws would leave the card picker's preview spinning
     * forever - fail visibly instead. */
    _safeRender() {
      try {
        this._render();
      } catch (err) {
        this.shadowRoot.innerHTML = `<style>${BASE_CSS}</style>
          <div class="shell"><p class="micro">Geely Card</p>
          <p style="margin-top:8px;font-size:12px">Render failed: ${esc(err && err.message)}</p></div>`;
      }
    }

    _detectPrefix(hass) {
      // The battery sensor exists on every Geely; its object_id carries the
      // device slug this integration derives every other entity id from.
      const ents = hass.entities || {};
      const fromPlatform = Object.keys(ents).find(
        (id) =>
          ents[id].platform === "geely_connect" &&
          id.startsWith("sensor.") && id.endsWith("_battery") &&
          // The 12V battery also ends in _battery, and whichever the
          // registry lists first would win - binding every card lookup to
          // the wrong prefix while looking perfectly plausible (#16).
          !id.endsWith("_12v_battery"),
      );
      if (fromPlatform) {
        return fromPlatform.slice("sensor.".length, -"_battery".length);
      }
      // Battery renamed? Recover the slug from any platform entity whose id
      // still ends in a known suffix (longest suffix wins, so _12v_battery
      // cannot masquerade as _battery).
      for (const id in ents) {
        if (ents[id].platform !== "geely_connect") continue;
        const object = id.split(".")[1] || "";
        for (const s of SUFFIXES) {
          if (object.length > s.length + 1 && object.endsWith("_" + s)) {
            return object.slice(0, -(s.length + 1));
          }
        }
      }
      const found = Object.keys(hass.states).find(
        (id) => id.startsWith("sensor.") && id.endsWith("_battery") &&
          hass.states[`climate.${id.slice(7, -8)}_climate`],
      );
      return found ? found.slice("sensor.".length, -"_battery".length) : null;
    }

    /* Entity resolution: the fast path joins prefix + suffix; when a user has
     * hand-renamed an entity id, a lazily-built suffix map over the
     * integration's own entities takes over, so one rename never blanks the
     * card. */
    _eid(domain, suffix) {
      const strict = `${domain}.${this._prefix}_${suffix}`;
      if (this._hass.states[strict]) return strict;
      if (this._mapFor !== this._hass.entities) {
        this._mapFor = this._hass.entities;
        this._map = {};
        const ents = this._hass.entities || {};
        for (const id in ents) {
          if (ents[id].platform !== "geely_connect") continue;
          const [dom, object] = id.split(".");
          for (const s of SUFFIXES) {
            if (object === s || object.endsWith("_" + s)) {
              const key = `${dom}.${s}`;
              if (!(key in this._map)) this._map[key] = id;
              break;
            }
          }
        }
      }
      return this._map[`${domain}.${suffix}`] || strict;
    }

    _st(entity) {
      const [domain, suffix] = entity.split(".");
      return this._hass.states[this._eid(domain, suffix)];
    }

    _signature(hass) {
      if (!this._prefix) return "no-prefix";
      // ALWAYS_WATCHED is added to whatever the card listed. The driving banner
      // and the action lock are rendered by this base class for all five cards,
      // so a card that forgot to watch these would freeze its banner on screen
      // - exactly how the door count and the charging label froze before. One
      // rule here cannot be forgotten by the sixth card.
      return [...new Set(this._watched().concat(ALWAYS_WATCHED))]
        .map((e) => {
          const st = this._st(e);
          if (!st) return "-";
          // The climate panel renders two ATTRIBUTES - target temperature
          // and preset - and the entity's state alone never changes when
          // they do, which froze the stepper until something else moved.
          if (e.startsWith("climate.")) {
            const a = st.attributes || {};
            return `${st.state}~${a.temperature}~${a.preset_mode}`;
          }
          return st.state;
        })
        .join("|") + `|${this._armed || ""}|${this._pendingTemp}`
        + `|${this._waiting() ? "wait" : ""}`;
    }

    /* How long to hold the controls after a command, in milliseconds.
     * `cooldown:` in the card config, in seconds; 0 disables the wait. */
    _cooldownMs() {
      const c = this._config.cooldown;
      const secs = c == null ? DEFAULT_COOLDOWN_S : Number(c);
      return isFinite(secs) && secs >= 0 ? secs * 1000 : DEFAULT_COOLDOWN_S * 1000;
    }

    _waiting() {
      return this._holdUntil != null && Date.now() < this._holdUntil;
    }

    /* Does this rejection mean "the car is still busy" rather than "no"? */
    _isBusyError(err) {
      const text = `${(err && (err.message || err.error || err.code)) || err || ""}`;
      return /has not yet been executed|8070/i.test(text);
    }

    _call(domain, service, entitySuffix, data = {}) {
      // notifyOnError false: Home Assistant would otherwise raise its own
      // "Failed to perform the action" toast before we get a chance to see
      // whether the car simply wanted another moment. We surface the failures
      // that matter ourselves, in _send.
      return this._send(() => this._hass.callService(
        domain, service,
        { entity_id: this._eid(domain, entitySuffix), ...data },
        undefined, false));
    }

    /* Write the stepper's number without rebuilding the card. */
    _showPendingTemp() {
      const el = this.shadowRoot && this.shadowRoot.querySelector(".tval");
      if (!el) return;
      const shown = this._pendingTemp != null ? this._pendingTemp
        : Number(((this._st("climate.climate") || {}).attributes || {}).temperature);
      el.textContent = isFinite(shown)
        ? `${String(shown).replace(/\.0$/, "")}°` : "—°";
    }

    /* Tell the user, the way Home Assistant's own cards do. */
    _toast(message) {
      this.dispatchEvent(new CustomEvent("hass-notification", {
        detail: { message: `Geely: ${message}` },
        bubbles: true, composed: true,
      }));
    }

    /* One command at a time, with a gap after it.
     *
     * The car executes one remote command at a time and refuses the next with
     * "the last request has not yet been executed" - and a refused command is
     * dropped, not queued, so the tap is simply lost and the user gets an error
     * toast. Two quick taps on the temperature stepper produced exactly that.
     *
     * There is no way to know when the car has finished: the gateway
     * acknowledges receipt, not execution, and nothing in the payload reports
     * a command completing. So this is a timer, not a status - held for
     * `cooldown` after the call resolves, with the controls visibly waiting.
     *
     * A command the car refused *because it was busy* is retried once, after
     * twice the wait. That is not the double-fire this project reverted in
     * v1.21.5: that one sent a second command while the first was still
     * running, whereas this one never ran at all, and a retry is the only way
     * it happens.
     */
    _send(fire, retried = false) {
      if (this._waiting()) return Promise.resolve();
      const hold = this._cooldownMs();
      const release = () => {
        this._holdUntil = Date.now() + hold;
        clearTimeout(this._holdTimer);
        this._holdTimer = setTimeout(() => {
          this._holdUntil = 0;
          this._applyGate();
        }, hold);
      };
      this._holdUntil = Date.now() + hold;
      this._applyGate();
      // Fired now rather than on a microtask: a command should leave the moment
      // the button is pressed, and only its *result* is handled asynchronously.
      let sent;
      try {
        sent = fire();
      } catch (err) {
        release();
        throw err;
      }
      return Promise.resolve(sent)
        .then(release, (err) => {
          release();
          if (!retried && this._isBusyError(err)) {
            // Retried quietly: the car was busy, the command never ran, and a
            // toast for something that is about to work on its own is noise.
            this._holdUntil = 0;
            return new Promise((r) => setTimeout(r, hold * 2))
              .then(() => this._send(fire, true));
          }
          // Anything else is the car saying no, or saying no twice. Hiding that
          // would leave the owner believing a command worked.
          this._toast(`${(err && (err.message || err.error)) || "command failed"}`);
          throw err;
        });
    }

    /* Destructive actions arm on the first tap and fire on the second. */
    _guarded(key, fire) {
      if (this._armed === key) {
        clearTimeout(this._armedTimer);
        this._armed = null;
        fire();
        this._sig = ""; this._safeRender();
        return;
      }
      this._armed = key;
      clearTimeout(this._armedTimer);
      this._armedTimer = setTimeout(() => {
        this._armed = null; this._sig = ""; this._safeRender();
      }, 3000);
      this._sig = ""; this._safeRender();
    }

    /* Is the car being driven?
     *
     * Deliberately the same test the integration's own poller uses: engine
     * running OR any speed. Speed alone reads 0 at every red light, so a card
     * keyed on it would unlock its buttons at each stop; the ignition state
     * stays on through those, and on a trim that never reports it this reduces
     * to the speed test on its own.
     *
     * `sensor.engine_state` is a display value ("Running"), but a trim whose
     * raw value is not in the map passes it through unchanged, so both
     * spellings count.
     */
    _isDriving() {
      // An escape hatch, because the failure mode is total: this integration
      // already knows the engine flag can stick - `_STUCK_POLLS` in the poller
      // exists for "a stuck flag, a driver sitting in the car with the ignition
      // on for an hour" - and a stuck flag here would grey out every control on
      // every card with no way back. `driving_lock: false` gives that back.
      if (this._config.driving_lock === false) return false;
      const speed = NUM(this._st("sensor.speed"));
      // No unit needed here: "is it moving" is the same question in mph.
      if (speed != null && speed > 0) return true;
      const eng = this._st("sensor.engine_state");
      const raw = eng ? String(eng.state).trim().toLowerCase() : "";
      return DRIVING_STATES.has(raw);
    }

    /* The banner every card shows while the car is moving. Rendered by each
     * card at the top of its shell, so it reads the same everywhere. */
    _drivingNotice() {
      if (!this._isDriving()) return "";
      return `<div class="driving" role="status">${icon("driving")}
        <span><b>Driving</b>Remote actions are unavailable until the car is parked</span>
      </div>`;
    }

    _onAction(key) {
      // The buttons are disabled while driving, but a disabled button is a
      // styling promise, not a lock: keyboard activation and anything that
      // reaches this method another way would still fire a command the car is
      // going to refuse. Refresh is a read, and live data is exactly what is
      // wanted mid-drive, so it stays.
      if (key !== "refresh" && this._isDriving()) return;
      switch (key) {
        case "lock": this._call("lock", "lock", "doors"); break;
        case "unlock": this._guarded("unlock", () => this._call("lock", "unlock", "doors")); break;
        case "trunk": this._guarded("trunk", () => this._call("button", "press", "unlock_trunk")); break;
        case "climate": {
          const c = this._st("climate.climate");
          const on = c && c.state !== "off";
          this._call("climate", on ? "turn_off" : "turn_on", "climate");
          break;
        }
        case "tempdown": case "tempup": {
          // Each tap moves the number on screen at once and the car is told
          // once, after the tapping stops. Sending per tap is what produced
          // "the last request has not yet been executed" while stepping from
          // 22 to 25 - three commands, two of them dropped, one error toast.
          const c = this._st("climate.climate");
          if (!c || !c.attributes) break;
          const a = c.attributes;
          const step = Number(a.target_temp_step) || 0.5;
          const base = this._pendingTemp != null ? this._pendingTemp
            : Number(a.temperature);
          if (!isFinite(base)) break;
          const next = Math.min(Number(a.max_temp) || 30,
            Math.max(Number(a.min_temp) || 16,
              base + (key === "tempup" ? step : -step)));
          this._pendingTemp = Math.round(next * 10) / 10;
          // The number is written straight into the element rather than
          // re-rendering the card, so a run of taps does not make it jump.
          this._showPendingTemp();
          clearTimeout(this._tempTimer);
          this._tempTimer = setTimeout(() => {
            const target = this._pendingTemp;
            if (target == null) return;
            // Held, NOT cleared on firing. Clearing it here made the display
            // fall back to the entity's old target for as long as the round
            // trip took - so the number the user had just dialled in visibly
            // jumped backwards and then forwards again. It is released when the
            // entity catches up, or if the command fails.
            this._call("climate", "set_temperature", "climate",
                       { temperature: target })
              .catch(() => {
                this._pendingTemp = null;
                this._showPendingTemp();
              });
          }, TEMP_SETTLE_MS);
          break;
        }
        case "rapidheat": case "rapidcool": {
          const want = key === "rapidheat" ? "Rapid Warming" : "Rapid Cooling";
          const c = this._st("climate.climate");
          const cur = c && c.attributes && c.attributes.preset_mode;
          if (cur === want) this._call("climate", "turn_off", "climate");
          else this._call("climate", "set_preset_mode", "climate", { preset_mode: want });
          break;
        }
        case "seat_heat_driver": case "seat_heat_passenger":
        case "seat_vent_driver": case "seat_vent_passenger": {
          // Tap cycles Off -> Low -> Medium -> High -> Off.
          const st = this._st(`select.${key}`);
          if (!st || !st.attributes || !Array.isArray(st.attributes.options)) break;
          const opts = st.attributes.options;
          const next = opts[(opts.indexOf(st.state) + 1) % opts.length];
          this._call("select", "select_option", key, { option: next });
          break;
        }
        case "sunroof_open": this._call("cover", "open_cover", "sunroof"); break;
        case "sunroof_close": this._call("cover", "close_cover", "sunroof"); break;
        case "shade_open": this._call("cover", "open_cover", "sunshade"); break;
        case "shade_close": this._call("cover", "close_cover", "sunshade"); break;
        case "gclean": this._call("switch", "toggle", "g_clean"); break;
        case "pcomfort": this._call("switch", "toggle", "parking_comfort"); break;
        case "swheel": this._call("switch", "toggle", "steering_wheel_heat"); break;
        case "charging_sw": this._call("switch", "toggle", "charging"); break;
        case "sched_sw": this._call("switch", "toggle", "scheduled_charging"); break;
        case "defrost": this._call("switch", "toggle", "defrost"); break;
        case "vent": this._call("switch", "toggle", "window_ventilation"); break;
        case "find": this._call("button", "press", "find_car"); break;
        case "refresh": this._call("button", "press", "refresh_data"); break;
      }
    }

    /* What the trunk button actually does, spelled out where it is pressed.
     *
     * The button says BOOT or TRUNK next to a tailgate icon, so anyone would
     * expect it to open one. It does not: it releases the latch, and the gate
     * still has to be lifted by hand. Four owners across three trims have now
     * confirmed the official app does exactly the same - there is no powered
     * open anywhere in this API - so the honest place to set that expectation is
     * on the control itself, not only in the README nobody reads first.
     */
    _bootTitle() {
      return `Unlocks the ${this._bootWord().toLowerCase()} latch - it does not `
        + `open the ${this._bootWord().toLowerCase()} electrically. Lift it by hand, `
        + `or use the key fob, within a few seconds before it re-locks itself.`;
    }

    /* "Boot" or "Trunk"? Home Assistant's country setting decides, and
     * `boot: true/false` in the card config overrides it. Asked for by an
     * Australian owner (#14), and it costs nothing: the label is ours. */
    _bootWord(caps = false) {
      const cfg = this._config.boot;
      const c = this._hass && this._hass.config && this._hass.config.country;
      const boot = typeof cfg === "boolean"
        ? cfg
        : !!c && BOOT_COUNTRIES.has(String(c).toUpperCase());
      if (boot) return caps ? "BOOT" : "Boot";
      return caps ? "TRUNK" : "Trunk";
    }

    /* Right-hand-drive? `rhd: true/false` in the card config overrides;
     * otherwise Home Assistant's own country setting decides. */
    _isRHD() {
      if (typeof this._config.rhd === "boolean") return this._config.rhd;
      const c = this._hass && this._hass.config && this._hass.config.country;
      return !!c && RHD_COUNTRIES.has(String(c).toUpperCase());
    }

    _wire() {
      this.shadowRoot.querySelectorAll("[data-act]").forEach((el) =>
        el.addEventListener("click", () => this._onAction(el.dataset.act)));
      this.shadowRoot.querySelectorAll("input[data-time]").forEach((el) =>
        el.addEventListener("change", () => {
          if (/^\d\d:\d\d$/.test(el.value)) {
            this._call("time", "set_value", el.dataset.time, { time: `${el.value}:00` });
          }
        }));
      // Two reasons the controls stand down, and they read the same: the car is
      // moving, or it is still working through the last command.
      this._applyGate();
    }

    /* Grey out or restore every control, on the DOM that is already there.
     *
     * Two reasons the controls stand down - the car is moving, or it is still
     * working through the last command - and they read the same.
     *
     * Deliberately NOT a re-render. Rebuilding the shadow root replaces
     * everything including the car drawing, and doing that on every tap made the
     * card visibly jump: one press cost two full renders and three taps on the
     * temperature stepper cost five. Toggling attributes in place costs none.
     *
     * Refresh survives the driving lock, because it reads the car rather than
     * commanding it. It does not survive the wait, because the wait blocks every
     * command including that one, and a button that looks live while being
     * silently dropped is worse than one that looks held.
     */
    _applyGate() {
      const driving = this._isDriving();
      const waiting = this._waiting();
      this.shadowRoot.querySelectorAll("[data-act]").forEach((el) => {
        const off = waiting || (driving && el.dataset.act !== "refresh");
        el.toggleAttribute("disabled", off);
        el.classList.toggle("blocked", off);
        if (off) el.setAttribute("aria-disabled", "true");
        else el.removeAttribute("aria-disabled");
      });
      this.shadowRoot.querySelectorAll("input[data-time]").forEach((el) => {
        const off = driving || waiting;
        el.toggleAttribute("disabled", off);
        el.classList.toggle("blocked", off);
      });
    }

    _preset() {
      const c = this._st("climate.climate");
      return (c && c.attributes && c.attributes.preset_mode) || "none";
    }

    /* Charging on/off and the schedule arm - the two switches the charging
     * section reads about but could not touch (#15). Hidden entirely on a
     * car with no charging switches. */
    _chargingControls() {
      const sw = this._st("switch.charging");
      const sched = this._st("switch.scheduled_charging");
      if (!sw && !sched) return "";
      // The start / end editors appear only while the schedule is armed
      // (#15) - native time inputs, so phones get their own picker.
      const timeBox = (suffix, label) => {
        const st = this._st(`time.${suffix}`);
        if (!st || !/^\d\d:\d\d/.test(st.state)) return "";
        return `<label class="ctime">${esc(label)}
          <input type="time" data-time="${suffix}" value="${esc(st.state.slice(0, 5))}"></label>`;
      };
      const schedOn = sched && sched.state === "on";
      return `<div class="crow wrap" style="margin:2px 0 6px">
        ${sw ? `<button class="cbtn ${sw.state === "on" ? "on" : ""}" data-act="charging_sw"
          title="Start / stop charging">${icon("bolt")}<span>Charging</span></button>` : ""}
        ${sched ? `<button class="cbtn ${schedOn ? "on" : ""}" data-act="sched_sw"
          title="Scheduled charging on / off">${icon("charge")}<span>Schedule</span></button>` : ""}
        ${schedOn ? timeBox("scheduled_charging_start", "Start") : ""}
        ${schedOn ? timeBox("scheduled_charging_end", "End") : ""}
      </div>`;
    }

    /* The full climate panel: temperature stepper, the car's own rapid
     * presets, seat heat/vent cycling, cabin air, and the roof. Every block
     * hides itself when its entity does not exist on this trim - the API has
     * no fan-speed control at all, so none is offered. */
    _climatePanel() {
      const c = this._st("climate.climate");
      if (!c) return "";
      const a = c.attributes || {};
      const on = c.state !== "off";
      const target = this._pendingTemp != null ? this._pendingTemp
        : Number(a.temperature);
      const preset = a.preset_mode;
      const seat = (key, label, ic, mode) => {
        const st = this._st(`select.${key}`);
        if (!st) return "";
        const opts = (st.attributes && st.attributes.options) || [];
        // "unavailable"/"unknown" must read as Off, not as an active level.
        const lvl = opts.includes(st.state) ? st.state : "Off";
        return `<button class="cbtn ${lvl !== "Off" ? "on" : ""}" data-act="${key}"
            title="${esc(mode)} - ${esc(label)}: ${esc(lvl)}">${icon(ic)}<span>${esc(label)}</span>
            <b>${esc(lvl)}</b></button>`;
      };
      const cover = (suffix, openKey, closeKey, label, ic) => {
        const st = this._st(`cover.${suffix}`);
        if (!st) return "";
        const isOpen = st.state === "open" || st.state === "opening";
        return `<div class="cpair ${isOpen ? "on" : ""}">
            ${icon(ic)}<span>${esc(label)}</span>
            <button class="cmini" data-act="${openKey}" title="Open ${esc(label)}">Open</button>
            <button class="cmini" data-act="${closeKey}" title="Close ${esc(label)}">Close</button>
          </div>`;
      };
      const gclean = this._st("switch.g_clean");
      const pcomfort = this._st("switch.parking_comfort");
      const swheel = this._st("switch.steering_wheel_heat");
      const heatRow = seat("seat_heat_driver", "Driver", "seatheat", "Seat heat") +
        seat("seat_heat_passenger", "Passenger", "seatheat", "Seat heat");
      const ventRow = seat("seat_vent_driver", "Driver", "seatvent", "Seat cooling") +
        seat("seat_vent_passenger", "Passenger", "seatvent", "Seat cooling");
      /* Parking Comfort keeps the cabin liveable while the car sits, so it belongs
       * beside the cabin-air controls rather than among the one-shot actions.
       *
       * It is wired to the state like every other button here, and in practice it
       * cannot light up: this car does not report the feature's state - the field
       * that looks like its flag reads 1 on a car with it switched off (#13) - so
       * the switch reports unknown and the class never applies. Left as a normal
       * comparison rather than hard-coded off, so a car that does report it would
       * light up correctly; the tooltip covers the ones that do not. */
      const airRow = (gclean ? `<button class="cbtn ${gclean.state === "on" ? "on" : ""}"
            data-act="gclean" title="Fresh air (G-Clean)">${icon("fresh")}<span>Fresh air</span></button>` : "") +
        (pcomfort ? `<button class="cbtn ${pcomfort.state === "on" ? "on" : ""}"
            data-act="pcomfort" title="${esc(PCOMFORT_HINT)}">${icon("sleep")}<span>Parking comfort</span></button>` : "") +
        (swheel ? `<button class="cbtn ${swheel.state === "on" ? "on" : ""}"
            data-act="swheel" title="${esc(SWHEEL_HINT)}">${icon("steering")}<span>Wheel heat</span></button>` : "") +
        cover("sunroof", "sunroof_open", "sunroof_close", "Sunroof", "roof") +
        cover("sunshade", "shade_open", "shade_close", "Shade", "shade");
      return `
        <hr class="hairline">
        <p class="micro">${icon("climate")} Climate</p>
        <div class="crow wrap temprow">
          <div class="temp ${on ? "on" : ""}">
            <button class="cstep" data-act="tempdown" title="Cooler">${icon("minus")}</button>
            <span class="tval">${isFinite(target) ? String(target).replace(/\.0$/, "") : "—"}°</span>
            <button class="cstep" data-act="tempup" title="Warmer">${icon("plus")}</button>
          </div>
          <button class="cbtn ${preset === "Rapid Warming" ? "on" : ""}" data-act="rapidheat"
            title="${esc(RAPID_HEAT_HINT)}">${icon("heat")}<span>Rapid heat</span></button>
          <button class="cbtn ${preset === "Rapid Cooling" ? "on" : ""}" data-act="rapidcool"
            title="${esc(RAPID_COOL_HINT)}">${icon("cool")}<span>Rapid cool</span></button>
        </div>
        ${heatRow ? `<p class="micro csub">${icon("seatheat")} Seat heating</p>
        <div class="crow wrap">${heatRow}</div>` : ""}
        ${ventRow ? `<p class="micro csub">${icon("seatvent")} Seat cooling</p>
        <div class="crow wrap">${ventRow}</div>` : ""}
        ${airRow ? `<div class="crow wrap" style="margin-top:10px">${airRow}</div>` : ""}`;
    }

    /* The value as Home Assistant itself would print it - unit and the user's
     * display precision included.
     *
     * `st.state` is the raw value, which reads fine until Home Assistant
     * converts one: an odometer switched to miles arrives as
     * 7730.479002662467 and the card printed every digit (#42). The chosen
     * precision lives in the entity registry, not on the state, and
     * formatEntityState is the frontend's own way to apply it - so the card
     * asks rather than reimplementing rounding it would get subtly wrong
     * across distances, pressures and temperatures.
     *
     * The raw pair remains the fallback for a Home Assistant too old to offer
     * the helper, which is what the value looked like before this. */
    _fmt(st) {
      const hass = this._hass;
      if (hass && typeof hass.formatEntityState === "function") {
        try {
          const out = hass.formatEntityState(st);
          if (out) return out;
        } catch (err) { /* fall through to the raw pair */ }
      }
      return `${st.state}${UNIT(st) ? " " + UNIT(st) : ""}`;
    }

    _row(label, st, opts = {}) {
      if (!st && !opts.value) return "";
      const value = opts.value != null ? opts.value
        : OK(st) ? this._fmt(st) : "—";
      const title = opts.title ? ` title="${esc(opts.title)}"` : "";
      return `<div class="row ${opts.accent ? "accent" : ""} ${opts.warn ? "warn" : ""}"${title}>
          <span>${esc(label)}</span><b>${esc(value)}</b></div>`;
    }

    _actBtn(key, label, ic, opts = {}) {
      const cls = [
        "act",
        opts.on ? "on" : "",
        this._armed === key ? "armed" : "",
      ].join(" ").trim();
      const text = this._armed === key ? "sure?" : label;
      return `<button class="${cls}" data-act="${key}" ${opts.disabled ? "disabled" : ""}
                title="${esc(opts.title || label)}">${icon(ic)}<span>${esc(text)}</span></button>`;
    }

    _openMap() {
      const on = (d) => { const st = this._st(`binary_sensor.${d}`); return st && st.state === "on"; };
      return {
        hood: on("hood"),
        front: on("door_driver") || on("door_passenger"),
        rear: on("door_rear_left") || on("door_rear_right"),
        trunk: on("trunk"),
      };
    }

    _carState() {
      const conn = this._st("sensor.charger_connection");
      // Real power flowing counts too: DC fast charge can hold the connection
      // field at "Plugged in" for a whole session (#10).
      const chargePower = NUM(this._st("sensor.charging_power"));
      const charging = (conn && conn.state === "Charging") ||
        (chargePower != null && chargePower > 0.3);
      const battery = NUM(this._st("sensor.battery"));
      const electric = this._st("sensor.electric_range");
      const fuelLevel = this._st("sensor.fuel_level");
      const fuelPct = this._st("sensor.fuel_level_pct");
      const fuelRange = this._st("sensor.fuel_range");
      const combined = this._st("sensor.combined_range");
      // A car with a tank is a different car to read, and the integration only
      // creates these entities when the propulsion verdict says the tank is
      // there - so their presence *is* the verdict, and the card needs no second
      // guess at what it is looking at.
      const hybrid = !!(fuelLevel || fuelPct || fuelRange);
      // The headline has to be the number this driver plans a journey with. On a
      // car with a tank the electric range alone understates how far it can go
      // by hundreds of kilometres; on a battery-only car it is the whole story.
      // Falling back through the three keeps a trim that reports only some of
      // them readable instead of showing a dash.
      const range = !hybrid ? electric
        : OK(combined) ? combined
        : OK(electric) ? electric
        : fuelRange;
      const rangeKind = !hybrid ? "electric"
        : OK(combined) ? "combined"
        : OK(electric) ? "electric"
        : "fuel";
      const locked = this._st("lock.doors");
      const climate = this._st("climate.climate");
      const doorsOpen = ["door_driver", "door_passenger", "door_rear_left",
        "door_rear_right", "trunk", "hood"]
        .filter((d) => { const st = this._st(`binary_sensor.${d}`); return st && st.state === "on"; });
      return { conn, charging, battery, range, rangeKind, hybrid, electric,
               fuelLevel, fuelPct, fuelRange, combined, locked, climate, doorsOpen };
    }

    /* "61% · 48% fuel" - the percentage line. On a car with a tank a lone
     * battery percentage reads as the whole story and is not: 8% battery with a
     * full tank is a car that will happily drive you home. */
    _levels(s, batt) {
      if (!s.hybrid || !OK(s.fuelPct)) return `${batt}%`;
      return `${batt}% · ${Math.round(NUM(s.fuelPct))}% fuel`;
    }

    /* Navigate to where the car is parked, in whichever app the owner has.
     *
     * Links, not commands: they open a map and never touch the car, which is
     * also why the driving lock leaves them alone - that disables `[data-act]`
     * controls and these are plain anchors. Rendered only once the car has
     * actually reported a position, because a link to 0,0 is worse than none.
     */
    _navRow() {
      const st = this._st("device_tracker.location");
      const a = (st && st.attributes) || {};
      const lat = Number(a.latitude);
      const lon = Number(a.longitude);
      if (!isFinite(lat) || !isFinite(lon) || (lat === 0 && lon === 0)) return "";
      const at = `${lat.toFixed(6)},${lon.toFixed(6)}`;
      const want = Array.isArray(this._config.nav) ? this._config.nav
        : DEFAULT_NAV;
      const chosen = want
        .map((k) => NAV_APPS[String(k).toLowerCase()])
        .filter(Boolean);
      if (!chosen.length) return "";
      const mode = this._config.nav_travel
        ? String(this._config.nav_travel).toLowerCase() : "";
      const link = (app) =>
        `<a class="cbtn nav" href="${esc(app.url(at, mode))}" target="_blank"
            rel="noopener noreferrer"
            title="Navigate to the car with ${esc(app.label)}">${icon("nav")}<span>${esc(app.label)}</span></a>`;
      return `<p class="micro csub" style="margin-top:12px">${icon("nav")} Navigate to the car</p>
        <div class="crow wrap">${chosen.map(link).join("")}</div>`;
    }

    /* What to call the headline number, so it never means two things. */
    _rangeLabel(s) {
      if (s.rangeKind === "combined") return "Combined range";
      if (s.rangeKind === "fuel") return "Fuel range";
      return s.hybrid ? "Electric range" : "Range";
    }

    /* "212 EV · 480 fuel" - the two halves behind a combined figure. Without it
     * the headline is a number a hybrid driver cannot act on, because it does
     * not say how far the car gets before the engine has to start. */
    _rangeSplit(s) {
      if (!s.hybrid) return "";
      const parts = [];
      if (OK(s.electric)) parts.push(`${Math.round(NUM(s.electric))} EV`);
      if (OK(s.fuelRange)) parts.push(`${Math.round(NUM(s.fuelRange))} fuel`);
      return parts.length > 1 ? parts.join(" · ") : "";
    }

    /* The energy bars: one on a battery-only car, two on a car with a tank.
     * A hybrid driver reading a single bar cannot tell which tank it means, and
     * a plug-in on an empty battery with a full tank is not "nearly out".
     * Drawn here rather than in each card so all five agree. */
    _bars(s, style = "") {
      const clamp = (v) => Math.max(0, Math.min(100, v));
      const bar = (pct, cls) => `<div class="bar ${cls}" style="${style}">
            <i style="width:${pct == null ? 0 : clamp(pct)}%"></i>
          </div>`;
      const batt = s.battery == null ? null : Math.round(s.battery);
      const fuel = NUM(s.fuelPct);
      const low = batt != null && batt <= 20;
      // A hybrid with no traction battery to speak of gets the fuel bar alone,
      // rather than an empty battery bar implying it cannot move.
      if (batt == null && fuel != null) return bar(fuel, "fuel");
      const first = bar(batt, `${low ? "low" : ""} ${s.charging ? "charging" : ""}`);
      return s.hybrid && fuel != null ? first + bar(fuel, "fuel") : first;
    }

    _title() {
      if (this._config.name) return this._config.name;
      const st = this._st("sensor.battery");
      const dev = st && this._hass.entities && this._hass.entities[st.entity_id];
      const device = dev && this._hass.devices && this._hass.devices[dev.device_id];
      return (device && (device.name_by_user || device.name)) ||
        this._prefix.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
    }

    _missing() {
      this.shadowRoot.innerHTML = `<style>${BASE_CSS}</style>
        <div class="shell"><p class="micro">Geely Connect</p>
        <p style="margin-top:8px;font-size:13px">
          No Geely vehicle found. Set <code>prefix:</code> to your entity slug,
          e.g. <code>my_geely_ex5</code> for <code>sensor.my_geely_ex5_battery</code>.
        </p></div>`;
    }
  }

  /* ------------------------------------------------------------ compact -- */

  class GeelyCardCompact extends GeelyCardBase {
    _watched() {
      return ["sensor.battery", "sensor.electric_range", "sensor.charger_connection",
        // Watched because the head row prints it (#52). A value the card
        // draws but does not watch freezes on screen - the render is
        // skipped whenever the signature is unchanged.
        "sensor.interior_temperature",
        "sensor.charging_power", "lock.doors", "climate.climate", "switch.defrost",
        "binary_sensor.door_driver", "binary_sensor.door_passenger",
        "binary_sensor.door_rear_left", "binary_sensor.door_rear_right",
        "binary_sensor.trunk", "binary_sensor.hood", "binary_sensor.connected"];
    }

    getCardSize() { return 4; }

    _render() {
      if (!this._prefix) return this._missing();
      const s = this._carState();
      const power = NUM(this._st("sensor.charging_power"));
      const range = OK(s.range) ? Math.round(NUM(s.range)) : "—";
      const batt = s.battery == null ? "—" : Math.round(s.battery);
      const split = this._rangeSplit(s);
      const driving = this._isDriving();
      // #52: the compact card carried the percentages and no cabin
      // reading; the mini carried the reading and no percentages. Each
      // head row was missing the half the other one had.
      const inTemp = NUM(this._st("sensor.interior_temperature"));
      const climateOn = s.climate && s.climate.state !== "off";
      const defrost = this._st("switch.defrost");
      const online = this._st("binary_sensor.connected");

      // Header customisation (all opt-in; unset keeps today's layout).
      const hideName = !!this._config.hide_name;
      const battSize = Number(this._config.battery_size) || 0;  // px, 0 = off
      const showParked = !!this._config.show_parked;

      const chips = [
        // First, and only while it is true: the compact card falls back to a
        // "Parked" chip when nothing else applies, which on a moving car was the
        // same contradiction as #25 on its bigger siblings.
        driving && `<span class="chip warn">Driving</span>`,
        showParked && !driving && `<span class="chip">Parked</span>`,
        s.locked && `<span class="chip ${s.locked.state === "locked" ? "" : "warn"}">
            ${s.locked.state === "locked" ? "Locked" : "Unlocked"}</span>`,
        s.charging && `<span class="chip on">${iconFilled("bolt")} ${power != null ? power.toFixed(1) + " kW" : "Charging"}</span>`,
        !s.charging && s.conn && s.conn.state === "Plugged in" && `<span class="chip">Plugged in</span>`,
        climateOn && `<span class="chip on">Climate on</span>`,
        s.doorsOpen.length > 0 && `<span class="chip warn">${s.doorsOpen.length} open</span>`,
      ].filter(Boolean).join("");

      this.shadowRoot.innerHTML = `<style>${BASE_CSS}
        .head { display:flex; align-items:baseline; justify-content:space-between; }
        .title { font-size:13px; font-weight:600; letter-spacing:.02em; display:flex; align-items:center; gap:7px; }
        .dot { width:6px; height:6px; border-radius:50%; background:${ACCENT}; }
        .dot.off { background:${AMBER}; }
        .hero { display:flex; align-items:center; gap:14px; margin:8px 0 2px; }
        .hero .n { font-size:44px; }
        .hero .u { font-size:13px; color: var(--secondary-text-color); margin-left:3px; }
        .hero .sub { margin-top:4px; }
        .battpct { font-weight:700; color: var(--primary-text-color);
                   line-height:1.05; margin-bottom:1px; }
        .carwrap { flex:1; max-width:300px; margin-left:auto; }
        </style>
        <div class="shell">
          ${this._drivingNotice()}
          <div class="head">
            <div class="title">
              <i class="dot ${online && online.state === "off" ? "off" : ""}"></i>
              ${hideName ? "" : esc(this._title())}
            </div>
            <span class="micro">${[
              battSize ? "" : this._levels(s, batt),
              inTemp == null ? "" : Math.round(inTemp) + "° in",
            ].filter(Boolean).join(" · ")}</span>
          </div>
          <div class="hero">
            <div>
              ${battSize ? `<div class="battpct" style="font-size:${battSize}px">${batt}%</div>` : ""}
              <div class="num n ${OK(s.range) ? "" : "unavail"}">${range}<span class="u">${esc(UNIT(s.range) || "km")}</span></div>
              <div class="micro sub">${esc(this._rangeLabel(s))}</div>
              ${split ? `<div class="micro sub">${esc(split)}</div>` : ""}
            </div>
            <div class="carwrap">${CAR_SVG(s.charging ? "charging" : "", this._openMap())}</div>
          </div>
          <div style="margin:2px 0 10px">${this._bars(s)}</div>
          <div class="chips" style="margin-bottom:12px">${chips || `<span class="chip">${driving ? "Driving" : "Parked"}</span>`}</div>
          <div class="actions">
            ${this._actBtn("lock", "Lock", "lock", { on: s.locked && s.locked.state === "locked" })}
            ${this._actBtn("unlock", "Unlock", "unlock")}
            ${this._actBtn("rapidheat", "Rapid heat", "heat", { title: RAPID_HEAT_HINT, on: this._preset() === "Rapid Warming" })}
            ${this._actBtn("rapidcool", "Rapid cool", "cool", { title: RAPID_COOL_HINT, on: this._preset() === "Rapid Cooling" })}
            ${this._actBtn("defrost", "Defrost", "defrost", { on: defrost && defrost.state === "on" })}
            ${this._actBtn("trunk", this._bootWord(), "trunk", { title: this._bootTitle() })}
          </div>
        </div>`;
      this._wire();
    }
  }

  /* ------------------------------------------------------------- full ---- */

  class GeelyCard extends GeelyCardBase {
    _watched() {
      return ["sensor.battery", "sensor.electric_range", "sensor.charger_connection",
        "sensor.charging_power", "sensor.charge_voltage", "sensor.charge_current",
        "sensor.time_to_full_charge", "sensor.charge_complete",
        "sensor.range_at_full_charge", "sensor.interior_temperature",
        "sensor.exterior_temperature", "sensor.tire_front_left",
        "sensor.tire_front_right", "sensor.tire_rear_left", "sensor.tire_rear_right",
        "sensor.total_mileage", "sensor.trip_meter", "sensor.average_consumption",
        "sensor.efficiency", "sensor.12v_battery", "sensor.days_to_service",
        "sensor.distance_to_service", "sensor.last_updated", "sensor.speed",
        "sensor.fuel_level", "sensor.fuel_range", "sensor.combined_range",
        "sensor.pack_power",
        "lock.doors", "climate.climate", "switch.defrost", "switch.charging",
        "switch.window_ventilation", "switch.scheduled_charging",
        "select.seat_heat_driver", "select.seat_heat_passenger",
        "select.seat_vent_driver", "select.seat_vent_passenger",
        "cover.sunroof", "cover.sunshade", "switch.g_clean",
        "time.scheduled_charging_start", "time.scheduled_charging_end",
        "binary_sensor.door_driver", "binary_sensor.door_passenger",
        "binary_sensor.door_rear_left", "binary_sensor.door_rear_right",
        "binary_sensor.trunk", "binary_sensor.hood", "binary_sensor.connected",
        "binary_sensor.charger_plug"];
    }

    getCardSize() { return 9; }

    _render() {
      if (!this._prefix) return this._missing();
      const s = this._carState();
      const batt = s.battery == null ? "—" : Math.round(s.battery);
      const split = this._rangeSplit(s);
      const driving = this._isDriving();
      const range = OK(s.range) ? Math.round(NUM(s.range)) : "—";
      const climateOn = s.climate && s.climate.state !== "off";
      const defrost = this._st("switch.defrost");
      const vent = this._st("switch.window_ventilation");
      const online = this._st("binary_sensor.connected");
      const power = this._st("sensor.charging_power");
      const days = this._st("sensor.days_to_service");
      const interior = this._st("sensor.interior_temperature");
      const exterior = this._st("sensor.exterior_temperature");
      const schedOn = this._st("switch.scheduled_charging");
      const schedA = this._st("time.scheduled_charging_start");
      const schedB = this._st("time.scheduled_charging_end");
      const fuelRange = this._st("sensor.fuel_range");
      const combined = this._st("sensor.combined_range");
      const speed = NUM(this._st("sensor.speed"));
      // Home Assistant converts the value when an owner sets this entity to
      // mph; the label has to follow it or the card reads "60 km/h" over a
      // number that is miles (#37).
      const speedUnit = UNIT(this._st("sensor.speed")) || "km/h";

      const tire = (suffix) => {
        const st = this._st(`sensor.tire_${suffix}`);
        return OK(st) ? `${Math.round(NUM(st))}<i>${esc(UNIT(st))}</i>` : "—";
      };
      // Driving comes from the same composite as the banner and the action lock,
      // not from the raw speed. Keyed on speed, this line said "Parked" on a car
      // whose banner two lines above said Driving and whose buttons were greyed
      // out (#25) - and #21 showed why it happens so often: that owner's speed
      // field read 0 for twenty-five minutes of a drive. The speed is still
      // shown when the car actually reports one.
      const statusLine = s.charging
        ? `Charging${OK(power) ? " · " + this._fmt(power) : ""}`
        : driving
        ? `Driving${speed != null && speed > 0 ? ` · ${Math.round(speed)} ${esc(speedUnit)}` : ""}`
        : s.doorsOpen.length ? `${s.doorsOpen.length} opening${s.doorsOpen.length > 1 ? "s" : ""} open`
        : s.locked && s.locked.state === "locked" ? "Parked · Locked" : "Parked";

      const sched = schedA && schedB && OK(schedA) && OK(schedB)
        ? `${schedA.state.slice(0, 5)}–${schedB.state.slice(0, 5)}${schedOn && schedOn.state === "on" ? "" : " (off)"}`
        : null;

      this.shadowRoot.innerHTML = `<style>${BASE_CSS}
        .head { display:flex; align-items:baseline; justify-content:space-between; }
        .title { font-size:14px; font-weight:600; letter-spacing:.02em; display:flex; align-items:center; gap:8px; }
        .dot { width:6px; height:6px; border-radius:50%; background:${ACCENT}; }
        .dot.off { background:${AMBER}; }
        .status { font-size:12px; color: var(--secondary-text-color); margin-top:3px; }
        .status.charging { color:${ACCENT}; }
        .hero { display:flex; align-items:flex-end; gap:16px; margin:14px 0 6px; }
        .hero .n { font-size:62px; }
        .hero .u { font-size:15px; color: var(--secondary-text-color); margin-left:4px; }
        .hero .side { margin-left:auto; text-align:right; }
        .hero .side .num { font-size:26px; }
        .hero .side .u2 { font-size:11px; color: var(--secondary-text-color); }
        .carwrap { margin:10px auto 0; max-width:470px; }
        .actions { display:grid; grid-template-columns:repeat(4, 1fr); }
        .grid { display:grid; grid-template-columns:1fr 1fr; gap:2px 26px; }
        .row { display:flex; justify-content:space-between; align-items:baseline; gap:8px;
               font-size:12.5px; padding:5px 0; color: var(--secondary-text-color); }
        .row span { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
        .row b { font-weight:500; color: var(--primary-text-color);
                 font-variant-numeric: tabular-nums; white-space:nowrap; }
        /* Two columns stop fitting here: measured, the first label and
         * value start ellipsising at 380px and a second pair follows by 340px
         * (#47). One column at full width clips nothing at any width tested,
         * so the pair stacks instead of being shortened. */
        @container (max-width: 380px) {
          .grid { grid-template-columns:minmax(0,1fr); }
          .row b { overflow:hidden; text-overflow:ellipsis; }
        }
        .row.accent b { color:${ACCENT}; }
        .row.warn b { color:${AMBER}; }
        .tires { display:grid; grid-template-columns:repeat(4,1fr); gap:8px; text-align:center; }
        .tires .t { border:1px solid var(--divider-color, rgba(120,130,140,.2));
                    border-radius:12px; padding:8px 4px 6px; }
        .tires .t b { font-size:15px; font-weight:400; font-variant-numeric:tabular-nums; }
        .tires .t i { font-style:normal; font-size:9px; color:var(--secondary-text-color); margin-left:2px; }
        .tires .t .micro { margin-top:3px; font-size:8.5px; justify-content:center; }
        .sec { margin-top:2px; }
        .footer { display:flex; justify-content:space-between; margin-top:10px; }
        .footer .micro { letter-spacing:.1em; }
        </style>
        <div class="shell">
          ${this._drivingNotice()}
          <div class="head">
            <div>
              <div class="title">
                <i class="dot ${online && online.state === "off" ? "off" : ""}"></i>
                ${this._config.hide_name ? "" : esc(this._title())}
              </div>
              <div class="status ${s.charging ? "charging" : ""}">${esc(statusLine)}</div>
            </div>
            <span class="micro">${this._levels(s, batt)}</span>
          </div>

          <div class="hero">
            <div>
              <div class="num n ${OK(s.range) ? "" : "unavail"}">${range}<span class="u">${esc(UNIT(s.range) || "km")}</span></div>
              <div class="micro" style="margin-top:5px">${esc(this._rangeLabel(s))}</div>
              ${split ? `<div class="micro" style="margin-top:3px">${esc(split)}</div>` : ""}
            </div>
            <div class="side">
              ${OK(interior) ? `<div class="num">${Math.round(NUM(interior))}°</div>
                <div class="u2">inside${OK(exterior) ? ` · ${Math.round(NUM(exterior))}° out` : ""}</div>` : ""}
            </div>
          </div>
          ${this._bars(s)}

          <div class="carwrap">${CAR_SVG(s.charging ? "charging" : "", this._openMap())}</div>

          <div class="actions" style="margin-top:8px">
            ${this._actBtn("lock", "Lock", "lock", { on: s.locked && s.locked.state === "locked" })}
            ${this._actBtn("unlock", "Unlock", "unlock")}
            ${this._actBtn("climate", "Climate", "climate", { on: climateOn })}
            ${this._actBtn("defrost", "Defrost", "defrost", { on: defrost && defrost.state === "on" })}
            ${this._actBtn("vent", "Vent", "vent", { on: vent && vent.state === "on" })}
            ${this._actBtn("trunk", this._bootWord(), "trunk", { title: this._bootTitle() })}
            ${this._actBtn("find", "Find", "find")}
            ${this._actBtn("refresh", "Sync", "refresh")}
          </div>
          ${this._navRow()}
          ${this._climatePanel()}

          <hr class="hairline">
          <p class="micro">${icon("charge")} Charging</p>
          ${this._chargingControls()}
          <div class="grid sec">
            ${this._row("Charger", s.conn)}
            ${this._row("Power", power, { accent: s.charging })}
            ${this._row("Time to full", this._st("sensor.time_to_full_charge"))}
            ${this._row("Complete at", this._st("sensor.charge_complete"), {
              value: (() => { const st = this._st("sensor.charge_complete");
                if (!OK(st)) return "—";
                const d = new Date(st.state);
                return isNaN(d) ? st.state : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
              })() })}
            ${this._row("Range at 100%", this._st("sensor.range_at_full_charge"))}
            ${sched ? this._row("Schedule", null, { value: sched }) : ""}
            ${this._row("Pack power", this._st("sensor.pack_power"))}
          </div>

          ${s.hybrid ? `
          <hr class="hairline">
          <p class="micro">${icon("fuel")} Fuel</p>
          <div class="grid sec">
            ${this._row("Fuel level", this._st("sensor.fuel_level"))}
            ${this._row("Fuel range", fuelRange)}
            ${/* The headline already IS the combined figure whenever the car
                  reports one, so repeating it here wastes the row. On a trim
                  that only reports the two halves it is worth showing. */""}
            ${s.rangeKind === "combined" ? "" :
              this._row("Combined range", combined, { accent: true })}
            ${/* Only a car with an engine has an engine to report, and whether
                  it is running is the fact a hybrid owner cannot get anywhere
                  else on these cards. */""}
            ${this._row("Engine", this._st("sensor.engine_state"))}
          </div>` : ""}

          <hr class="hairline">
          <p class="micro">${icon("tire")} Tires</p>
          <div class="tires" style="margin-top:8px">
            <div class="t"><b>${tire("front_left")}</b><div class="micro">FL</div></div>
            <div class="t"><b>${tire("front_right")}</b><div class="micro">FR</div></div>
            <div class="t"><b>${tire("rear_left")}</b><div class="micro">RL</div></div>
            <div class="t"><b>${tire("rear_right")}</b><div class="micro">RR</div></div>
          </div>

          <hr class="hairline">
          <p class="micro">${icon("trip")} Trip &amp; health</p>
          <div class="grid sec">
            ${this._row("Odometer", this._st("sensor.total_mileage"))}
            ${this._row("Trip meter", this._st("sensor.trip_meter"))}
            ${this._row("Lifetime avg", this._st("sensor.average_consumption"), { title: LIFETIME_HINT })}
            ${this._row("Efficiency", this._st("sensor.efficiency"))}
            ${this._row("12 V battery", this._st("sensor.12v_battery"))}
            ${this._row("Service in", days, {
              warn: OK(days) && NUM(days) != null && NUM(days) <= 30,
              value: OK(days) ? `${this._fmt(days)} / ${OK(this._st("sensor.distance_to_service"))
                ? this._fmt(this._st("sensor.distance_to_service")) : "—"}` : "—" })}
          </div>

          <div class="footer">
            <span class="micro">Geely Connect</span>
            <span class="micro">${(() => { const st = this._st("sensor.last_updated");
              if (!OK(st)) return "";
              const d = new Date(st.state);
              return isNaN(d) ? "" : "Synced " + d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
            })()}</span>
          </div>
        </div>`;
      this._wire();
    }
  }

  // As a module there is no document.currentScript, and import.meta is a
  // syntax error in the classic-script fallback - so find this file's own
  // tag in the DOM. The version is what tells a screenshot which build ran.
  const VERSION = (() => {
    let src = (document.currentScript && document.currentScript.src) || "";
    if (!src) {
      const tag = [...document.querySelectorAll('script[src*="geely-card.js"]')].pop();
      src = (tag && tag.src) || "";
    }
    const m = /[?&]v=([^&]+)/.exec(src);
    return m ? decodeURIComponent(m[1]) : "?";
  })();

  const T0 = Date.now();
  const STATUS = {
    version: VERSION, swaps: 0, losses: 0, defineError: "", firstDefine: null,
    lastLossAt: null, lastFixAt: null,
    line() {
      const missing = ["geely-card", "geely-card-compact", "geely-card-top",
        "geely-card-mini", "geely-card-strip"].filter((n) => !window.customElements.get(n));
      const ms = (t) => (t === null ? "?" : `${t}ms`);
      return [
        `v${this.version}`,
        "script ran",
        missing.length ? `MISSING: ${missing.join(", ")}` : "all 5 cards OK",
        `first define ${this.firstDefine === false ? "FAILED" : ms(this.firstDefine)}`,
        this.losses ? `lost x${this.losses} (last at ${ms(this.lastLossAt)})` : "never lost",
        this.swaps ? `registry swapped x${this.swaps}` : "no registry swap",
        this.lastFixAt !== null ? `restored at ${ms(this.lastFixAt)}` : null,
        this.defineError ? `define error: ${this.defineError}` : null,
      ].filter(Boolean).join(" · ");
    },
  };

  // This file is delivered both as a Lovelace resource (module) and as an
  // extra script, so it can execute twice in one page. The guard handles the
  // common case; the try absorbs anything the platform throws anyway, because
  // a failed re-define must never abort the run before customCards is filled.
  const defineOnce = (tag, cls) => {
    try {
      if (!customElements.get(tag)) customElements.define(tag, cls);
    } catch (err) {
      STATUS.defineError = String(err && err.message || err).slice(0, 80);
      console.warn(`geely-card: define(${tag}) skipped:`, err);
    }
  };
  /* ---------------------------------------------------------- strip ----- */

  class GeelyCardStrip extends GeelyCardBase {
    _watched() {
      return ["sensor.battery", "sensor.electric_range", "sensor.charging_power",
        "sensor.charger_connection",
        "lock.doors", "climate.climate", "binary_sensor.connected",
        "binary_sensor.door_driver", "binary_sensor.door_passenger",
        "binary_sensor.door_rear_left", "binary_sensor.door_rear_right",
        "binary_sensor.trunk", "binary_sensor.hood"];
    }

    getCardSize() { return 2; }

    static getGridOptions() {
      return { columns: 12, rows: 2, min_columns: 6, min_rows: 1 };
    }

    _render() {
      if (!this._prefix) return this._missing();
      const s = this._carState();
      const range = OK(s.range) ? Math.round(NUM(s.range)) : "—";
      const batt = s.battery == null ? "—" : Math.round(s.battery);
      const split = this._rangeSplit(s);
      const power = NUM(this._st("sensor.charging_power"));
      const locked = s.locked && s.locked.state === "locked";
      const online = this._st("binary_sensor.connected");
      // Driving outranks everything else on this line. These two cards are one
      // row tall by design, so the banner the larger cards show would double
      // their height: the status line carries the same message instead, and the
      // buttons beside it are greyed out either way.
      const driving = this._isDriving();
      const statusLine = driving
        ? "Driving · actions locked"
        : s.charging
        ? `Charging${power != null ? " · " + power.toFixed(1) + " kW" : ""}`
        : s.doorsOpen.length ? `${s.doorsOpen.length} open`
        : locked ? "Locked" : s.locked ? "Unlocked" : "Parked";

      this.shadowRoot.innerHTML = `<style>${BASE_CSS}
        .shell { padding: 12px 16px 10px; }
        .rowline { display:flex; align-items:center; gap:14px; }
        .left { flex:1; min-width:0; }
        .topline { display:flex; align-items:baseline; gap:8px; min-width:0; }
        .dot { width:5px; height:5px; border-radius:50%; background:${ACCENT};
               flex:none; align-self:center; }
        .dot.off { background:${AMBER}; }
        .rng { font-size:24px; }
        .rng .u { font-size:11px; color: var(--secondary-text-color); margin-left:2px; }
        .pct { font-size:12px; color: var(--secondary-text-color);
               font-variant-numeric: tabular-nums; }
        .status { font-size:11px; color: var(--secondary-text-color); margin-top:1px;
                  overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
        .status.charging { color:${ACCENT}; }
        .status.warn { color:${AMBER}; }
        .actions { display:flex; gap:6px; flex:none; }
        .act { width:44px; padding:8px 0 7px; border-radius:12px; }
        .act span { display:none; }
        .act svg { width:19px; height:19px; }
        .bar { margin-top:9px; }
        </style>
        <div class="shell">
          <div class="rowline">
            <div class="left">
              <div class="topline">
                <i class="dot ${online && online.state === "off" ? "off" : ""}"></i>
                <span class="num rng ${OK(s.range) ? "" : "unavail"}">${range}<span class="u">${esc(UNIT(s.range) || "km")}</span></span>
                <span class="pct">${this._levels(s, batt)}</span>
              </div>
              <div class="status ${driving ? "warn" : s.charging ? "charging" : s.doorsOpen.length ? "warn" : ""}">${esc(statusLine)}</div>
            </div>
            <div class="actions">
              ${locked
                ? this._actBtn("unlock", "Unlock", "unlock")
                : this._actBtn("lock", "Lock", "lock")}
              ${this._actBtn("rapidheat", "Rapid heat", "heat", { title: RAPID_HEAT_HINT, on: this._preset() === "Rapid Warming" })}
              ${this._actBtn("rapidcool", "Rapid cool", "cool", { title: RAPID_COOL_HINT, on: this._preset() === "Rapid Cooling" })}
              ${this._actBtn("trunk", this._bootWord(), "trunk", { title: this._bootTitle() })}
              ${this._actBtn("find", "Find", "find")}
            </div>
          </div>
          ${this._bars(s)}
        </div>`;
      this._wire();
    }
  }

  /* ----------------------------------------------------------- mini ----- */

  class GeelyCardMini extends GeelyCardBase {
    _watched() {
      // Everything _carState() reads has to be here: the render is skipped
      // when the signature is unchanged, so an entity that is drawn but not
      // watched freezes on screen - the door count and the charging label
      // both did.
      return ["sensor.battery", "sensor.electric_range",
        "sensor.interior_temperature", "sensor.charging_power",
        "sensor.charger_connection",
        "lock.doors", "climate.climate", "binary_sensor.connected",
        "binary_sensor.door_driver", "binary_sensor.door_passenger",
        "binary_sensor.door_rear_left", "binary_sensor.door_rear_right",
        "binary_sensor.trunk", "binary_sensor.hood"];
    }

    getCardSize() { return 3; }

    static getGridOptions() {
      return { columns: 6, rows: 3, min_columns: 4, min_rows: 3 };
    }

    _render() {
      if (!this._prefix) return this._missing();
      const s = this._carState();
      const range = OK(s.range) ? Math.round(NUM(s.range)) : "—";
      const temp = NUM(this._st("sensor.interior_temperature"));
      const power = NUM(this._st("sensor.charging_power"));
      const locked = s.locked && s.locked.state === "locked";
      const preset = this._preset();
      const online = this._st("binary_sensor.connected");
      // Driving outranks everything else on this line. These two cards are one
      // row tall by design, so the banner the larger cards show would double
      // their height: the status line carries the same message instead, and the
      // buttons beside it are greyed out either way.
      const driving = this._isDriving();
      const statusLine = driving
        ? "Driving · actions locked"
        : s.charging
        ? `Charging${power != null ? " · " + power.toFixed(1) + " kW" : ""}`
        : s.doorsOpen.length ? `${s.doorsOpen.length} open`
        : locked ? "Locked" : s.locked ? "Unlocked" : "Parked";

      this.shadowRoot.innerHTML = `<style>${BASE_CSS}
        .shell { padding: 14px 16px 12px; }
        .head { display:flex; align-items:center; justify-content:space-between; gap:10px; }
        .title { font-size:12px; font-weight:600; letter-spacing:.02em;
                 display:flex; align-items:center; gap:6px; min-width:0; flex:1; }
        .title em { font-style:normal; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
        .dot { width:5px; height:5px; border-radius:50%; background:${ACCENT}; flex:none; }
        .dot.off { background:${AMBER}; }
        .temp { font-size:12px; color: var(--secondary-text-color); flex:none;
                font-variant-numeric: tabular-nums; }
        .mid { margin:8px 0 2px; }
        .mid .n { font-size:34px; }
        .mid .u { font-size:12px; color: var(--secondary-text-color); margin-left:2px; }
        .status { font-size:11px; color: var(--secondary-text-color); margin-top:1px; }
        .status.charging { color:${ACCENT}; }
        .status.warn { color:${AMBER}; }
        .actions { display:flex; gap:6px; margin-top:10px; }
        .act { flex:1; padding:8px 0 7px; border-radius:12px; }
        .act span { display:none; }
        .act svg { width:19px; height:19px; }
        </style>
        <div class="shell">
          <div class="head">
            <div class="title">
              <i class="dot ${online && online.state === "off" ? "off" : ""}"></i>
              <em>${this._config.hide_name ? "" : esc(this._title())}</em>
            </div>
            <span class="temp">${[
              s.battery == null ? "" : Math.round(s.battery) + "%",
              temp == null ? "" : Math.round(temp) + "° in",
            ].filter(Boolean).join(" · ")}</span>
          </div>
          <div class="mid">
            <span class="num n ${OK(s.range) ? "" : "unavail"}">${range}</span><span class="u">${esc(UNIT(s.range) || "km")}</span>
            <div class="status ${driving ? "warn" : s.charging ? "charging" : s.doorsOpen.length ? "warn" : ""}">${esc(statusLine)}</div>
          </div>
          <div class="actions">
            ${locked
              ? this._actBtn("unlock", "Unlock", "unlock")
              : this._actBtn("lock", "Lock", "lock")}
            ${this._actBtn("rapidheat", "Rapid heat", "heat", { title: RAPID_HEAT_HINT, on: preset === "Rapid Warming" })}
            ${this._actBtn("rapidcool", "Rapid cool", "cool", { title: RAPID_COOL_HINT, on: preset === "Rapid Cooling" })}
          </div>
        </div>`;
      this._wire();
    }
  }

  /* ------------------------------------------------------------ top ----- */

  class GeelyCardTop extends GeelyCardBase {
    _watched() {
      return ["sensor.battery", "sensor.electric_range", "sensor.charger_connection",
        "sensor.charging_power", "sensor.time_to_full_charge", "sensor.charge_complete",
        "sensor.range_at_full_charge", "sensor.interior_temperature",
        "sensor.exterior_temperature", "sensor.tire_front_left",
        "sensor.tire_front_right", "sensor.tire_rear_left", "sensor.tire_rear_right",
        "sensor.total_mileage", "sensor.trip_meter", "sensor.average_consumption",
        "sensor.efficiency", "sensor.12v_battery", "sensor.days_to_service",
        "sensor.distance_to_service", "sensor.last_updated", "sensor.speed",
        "sensor.fuel_level", "sensor.fuel_range", "sensor.combined_range",
        "sensor.pack_power", "lock.doors", "climate.climate", "switch.defrost",
        "switch.charging", "switch.window_ventilation", "switch.scheduled_charging",
        "select.seat_heat_driver", "select.seat_heat_passenger",
        "select.seat_vent_driver", "select.seat_vent_passenger",
        "time.scheduled_charging_start", "time.scheduled_charging_end",
        "cover.sunroof", "cover.sunshade", "switch.g_clean",
        "binary_sensor.door_driver", "binary_sensor.door_passenger",
        "binary_sensor.door_rear_left", "binary_sensor.door_rear_right",
        "binary_sensor.trunk", "binary_sensor.hood", "binary_sensor.connected"];
    }

    getCardSize() { return 10; }

    _render() {
      if (!this._prefix) return this._missing();
      const s = this._carState();
      const batt = s.battery == null ? "—" : Math.round(s.battery);
      const split = this._rangeSplit(s);
      const driving = this._isDriving();
      const range = OK(s.range) ? Math.round(NUM(s.range)) : "—";
      const climateOn = s.climate && s.climate.state !== "off";
      const defrost = this._st("switch.defrost");
      const vent = this._st("switch.window_ventilation");
      const online = this._st("binary_sensor.connected");
      const power = this._st("sensor.charging_power");
      const days = this._st("sensor.days_to_service");
      const interior = this._st("sensor.interior_temperature");
      const exterior = this._st("sensor.exterior_temperature");
      const schedOn = this._st("switch.scheduled_charging");
      const schedA = this._st("time.scheduled_charging_start");
      const schedB = this._st("time.scheduled_charging_end");
      const fuelRange = this._st("sensor.fuel_range");
      const combined = this._st("sensor.combined_range");
      const speed = NUM(this._st("sensor.speed"));
      // Home Assistant converts the value when an owner sets this entity to
      // mph; the label has to follow it or the card reads "60 km/h" over a
      // number that is miles (#37).
      const speedUnit = UNIT(this._st("sensor.speed")) || "km/h";

      const isOpen = (suffix) => {
        const st = this._st(`binary_sensor.${suffix}`);
        return st ? st.state === "on" : null;
      };
      const sunroofSt = this._st("cover.sunroof");
      const tire = (suffix) => {
        const st = this._st(`sensor.tire_${suffix}`);
        return OK(st) ? `${Math.round(NUM(st))} ${UNIT(st)}`.trim() : "—";
      };
      // Driving comes from the same composite as the banner and the action lock,
      // not from the raw speed. Keyed on speed, this line said "Parked" on a car
      // whose banner two lines above said Driving and whose buttons were greyed
      // out (#25) - and #21 showed why it happens so often: that owner's speed
      // field read 0 for twenty-five minutes of a drive. The speed is still
      // shown when the car actually reports one.
      const statusLine = s.charging
        ? `Charging${OK(power) ? " · " + this._fmt(power) : ""}`
        : driving
        ? `Driving${speed != null && speed > 0 ? ` · ${Math.round(speed)} ${esc(speedUnit)}` : ""}`
        : s.doorsOpen.length ? `${s.doorsOpen.length} opening${s.doorsOpen.length > 1 ? "s" : ""} open`
        : s.locked && s.locked.state === "locked" ? "Parked · Locked" : "Parked";
      const sched = schedA && schedB && OK(schedA) && OK(schedB)
        ? `${schedA.state.slice(0, 5)}–${schedB.state.slice(0, 5)}${schedOn && schedOn.state === "on" ? "" : " (off)"}`
        : null;

      // The driver sits left in LHD markets and right in RHD ones; the rear
      // door sensors are side-named already and never swap.
      const rhd = this._isRHD();
      // The tire entities are named from the car's own driver/passenger
      // fields under a left-hand-drive assumption, exactly like the front
      // doors - tyreStatusDriver becomes tire_front_left. So on a
      // right-hand-drive car all four mirror, or the drawing would put the
      // driver's door on the right and the driver's tire on the left.
      const d = {
        tires: rhd
          ? { fl: tire("front_right"), fr: tire("front_left"),
              rl: tire("rear_right"), rr: tire("rear_left") }
          : { fl: tire("front_left"), fr: tire("front_right"),
              rl: tire("rear_left"), rr: tire("rear_right") },
        doors: { fl: isOpen(rhd ? "door_passenger" : "door_driver"),
                 fr: isOpen(rhd ? "door_driver" : "door_passenger"),
                 rl: isOpen("door_rear_left"), rr: isOpen("door_rear_right") },
        hood: isOpen("hood"),
        trunk: isOpen("trunk"),
        bootWord: this._bootWord(true),
        sunroof: sunroofSt ? sunroofSt.state !== "closed" : null,
      };

      this.shadowRoot.innerHTML = `<style>${BASE_CSS}
        .head { display:flex; align-items:baseline; justify-content:space-between; }
        .title { font-size:14px; font-weight:600; letter-spacing:.02em; display:flex; align-items:center; gap:8px; }
        .dot { width:6px; height:6px; border-radius:50%; background:${ACCENT}; }
        .dot.off { background:${AMBER}; }
        .status { font-size:12px; color: var(--secondary-text-color); margin-top:3px; }
        .status.charging { color:${ACCENT}; }
        .hero { display:flex; align-items:flex-end; gap:16px; margin:12px 0 6px; }
        .hero .n { font-size:46px; }
        .hero .u { font-size:14px; color: var(--secondary-text-color); margin-left:4px; }
        .hero .side { margin-left:auto; text-align:right; }
        .hero .side .num { font-size:24px; }
        .hero .side .u2 { font-size:11px; color: var(--secondary-text-color); }
        .carwrap { margin:12px auto 0; max-width:250px; }
        .actions { display:grid; grid-template-columns:repeat(4, 1fr); margin-top:10px; }
        .grid { display:grid; grid-template-columns:1fr 1fr; gap:2px 26px; }
        .row { display:flex; justify-content:space-between; align-items:baseline; gap:8px;
               font-size:12.5px; padding:5px 0; color: var(--secondary-text-color); }
        .row span { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
        .row b { font-weight:500; color: var(--primary-text-color);
                 font-variant-numeric: tabular-nums; white-space:nowrap; }
        /* Two columns stop fitting here: measured, the first label and
         * value start ellipsising at 380px and a second pair follows by 340px
         * (#47). One column at full width clips nothing at any width tested,
         * so the pair stacks instead of being shortened. */
        @container (max-width: 380px) {
          .grid { grid-template-columns:minmax(0,1fr); }
          .row b { overflow:hidden; text-overflow:ellipsis; }
        }
        .row.accent b { color:${ACCENT}; }
        .row.warn b { color:${AMBER}; }
        .sec { margin-top:2px; }
        .footer { display:flex; justify-content:space-between; margin-top:10px; }
        .footer .micro { letter-spacing:.1em; }
        </style>
        <div class="shell">
          ${this._drivingNotice()}
          <div class="head">
            <div>
              <div class="title">
                <i class="dot ${online && online.state === "off" ? "off" : ""}"></i>
                ${this._config.hide_name ? "" : esc(this._title())}
              </div>
              <div class="status ${s.charging ? "charging" : ""}">${esc(statusLine)}</div>
            </div>
            <span class="micro">${this._levels(s, batt)}</span>
          </div>

          <div class="hero">
            <div>
              <div class="num n ${OK(s.range) ? "" : "unavail"}">${range}<span class="u">${esc(UNIT(s.range) || "km")}</span></div>
              <div class="micro" style="margin-top:5px">${esc(this._rangeLabel(s))}</div>
              ${split ? `<div class="micro" style="margin-top:3px">${esc(split)}</div>` : ""}
            </div>
            <div class="side">
              ${OK(interior) ? `<div class="num">${Math.round(NUM(interior))}°</div>
                <div class="u2">inside${OK(exterior) ? ` · ${Math.round(NUM(exterior))}° out` : ""}</div>` : ""}
            </div>
          </div>
          ${this._bars(s)}

          <div class="actions">
            ${this._actBtn("lock", "Lock", "lock", { on: s.locked && s.locked.state === "locked" })}
            ${this._actBtn("unlock", "Unlock", "unlock")}
            ${this._actBtn("climate", "Climate", "climate", { on: climateOn })}
            ${this._actBtn("defrost", "Defrost", "defrost", { on: defrost && defrost.state === "on" })}
            ${this._actBtn("vent", "Vent", "vent", { on: vent && vent.state === "on" })}
            ${this._actBtn("trunk", this._bootWord(), "trunk", { title: this._bootTitle() })}
            ${this._actBtn("find", "Find", "find")}
            ${this._actBtn("refresh", "Sync", "refresh")}
          </div>

          <div class="carwrap">${CAR_TOP_SVG(s.charging ? "charging" : "", d)}</div>
          ${this._navRow()}
          ${this._climatePanel()}

          <hr class="hairline">
          <p class="micro">${icon("charge")} Charging</p>
          ${this._chargingControls()}
          <div class="grid sec">
            ${this._row("Charger", s.conn)}
            ${this._row("Power", power, { accent: s.charging })}
            ${this._row("Time to full", this._st("sensor.time_to_full_charge"))}
            ${this._row("Complete at", this._st("sensor.charge_complete"), {
              value: (() => { const st = this._st("sensor.charge_complete");
                if (!OK(st)) return "—";
                const dte = new Date(st.state);
                return isNaN(dte) ? st.state : dte.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
              })() })}
            ${this._row("Range at 100%", this._st("sensor.range_at_full_charge"))}
            ${sched ? this._row("Schedule", null, { value: sched }) : ""}
            ${this._row("Pack power", this._st("sensor.pack_power"))}
          </div>

          ${s.hybrid ? `
          <hr class="hairline">
          <p class="micro">${icon("fuel")} Fuel</p>
          <div class="grid sec">
            ${this._row("Fuel level", this._st("sensor.fuel_level"))}
            ${this._row("Fuel range", fuelRange)}
            ${/* The headline already IS the combined figure whenever the car
                  reports one, so repeating it here wastes the row. On a trim
                  that only reports the two halves it is worth showing. */""}
            ${s.rangeKind === "combined" ? "" :
              this._row("Combined range", combined, { accent: true })}
            ${/* Only a car with an engine has an engine to report, and whether
                  it is running is the fact a hybrid owner cannot get anywhere
                  else on these cards. */""}
            ${this._row("Engine", this._st("sensor.engine_state"))}
          </div>` : ""}

          <hr class="hairline">
          <p class="micro">${icon("trip")} Trip &amp; health</p>
          <div class="grid sec">
            ${this._row("Odometer", this._st("sensor.total_mileage"))}
            ${this._row("Trip meter", this._st("sensor.trip_meter"))}
            ${this._row("Lifetime avg", this._st("sensor.average_consumption"), { title: LIFETIME_HINT })}
            ${this._row("Efficiency", this._st("sensor.efficiency"))}
            ${this._row("12 V battery", this._st("sensor.12v_battery"))}
            ${this._row("Service in", days, {
              warn: OK(days) && NUM(days) != null && NUM(days) <= 30,
              value: OK(days) ? `${this._fmt(days)} / ${OK(this._st("sensor.distance_to_service"))
                ? this._fmt(this._st("sensor.distance_to_service")) : "—"}` : "—" })}
          </div>

          <div class="footer">
            <span class="micro">Geely Connect</span>
            <span class="micro">${(() => { const st = this._st("sensor.last_updated");
              if (!OK(st)) return "";
              const dte = new Date(st.state);
              return isNaN(dte) ? "" : "Synced " + dte.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
            })()}</span>
          </div>
        </div>`;
      this._wire();
    }
  }

  // The elements this file owns, named once. The watchdog, the banner and the
  // status line all count them, and a list that disagrees with reality is how
  // #12 was misdiagnosed from a console banner claiming two cards.
  const CARD_TAGS = ["geely-card", "geely-card-compact", "geely-card-top",
                     "geely-card-mini", "geely-card-strip"];

  const registerElements = () => {
    defineOnce("geely-card-compact", GeelyCardCompact);
    defineOnce("geely-card-top", GeelyCardTop);
    defineOnce("geely-card-mini", GeelyCardMini);
    defineOnce("geely-card-strip", GeelyCardStrip);
    defineOnce("geely-card", GeelyCard);
  };
  registerElements();
  STATUS.firstDefine = window.customElements.get("geely-card")
    ? Date.now() - T0 : false;

  // Some cards (anything built on lit's scoped registries - Mushroom,
  // button-card and friends) ship the scoped-custom-element-registry
  // polyfill, which REPLACES window.customElements wholesale. Definitions
  // made on the original registry are invisible to the replacement's get(),
  // so if this file runs first - and as an extra script it usually does -
  // the card picker asks the new registry, finds nothing, and spins forever.
  // Watch for the swap for a while and re-register through whichever
  // registry is current; the polyfill scopes its native names, so the old
  // definition does not block the new one.
  /* Reproduced against the real polyfill: after it loads,
   * customElements.get("geely-card") returns undefined even though the
   * definition succeeded moments earlier - it does not carry earlier
   * registrations across. Redefining the same name afterwards is allowed and
   * restores it, so the whole fix is noticing the moment it happens.
   *
   * The card picker gives a custom element two seconds and never retries: a
   * rejected lookup leaves its preview tile spinning until the dialog is
   * reopened. So the check is cheap (one map lookup) and runs often enough
   * that the gap cannot outlast that window. */
  let knownRegistry = window.customElements;
  let knownDefine = window.customElements.define;

  const ensureRegistered = () => {
    const swapped = window.customElements !== knownRegistry ||
      window.customElements.define !== knownDefine;
    const lost = !window.customElements.get("geely-card") ||
      !window.customElements.get("geely-card-compact") ||
      !window.customElements.get("geely-card-top") ||
      !window.customElements.get("geely-card-mini") ||
      !window.customElements.get("geely-card-strip");
    if (swapped || lost) {
      if (lost) {
        STATUS.losses += 1;
        STATUS.lastLossAt = Date.now() - T0;
      }
      if (swapped) {
        STATUS.swaps += 1;
        console.info(
          "geely-card: the custom element registry was replaced (a scoped-registry " +
          "polyfill, shipped by some cards) - re-registering");
      }
      knownRegistry = window.customElements;
      knownDefine = window.customElements.define;
      registerElements();
      if (lost && window.customElements.get("geely-card")) {
        STATUS.lastFixAt = Date.now() - T0;
      }
    }
    if (statusEntry) statusEntry.description = STATUS.line();
  };

  // Fast while the page is still pulling in resources - that is when the
  // polyfill lands - then slow, and stop after a minute.
  let fastLeft = 100;                       // 100 x 50 ms = five seconds
  const fast = setInterval(() => {
    ensureRegistered();
    if (--fastLeft <= 0) {
      clearInterval(fast);
      let slowLeft = 110;                   // + 110 x 500 ms = one minute
      const slow = setInterval(() => {
        ensureRegistered();
        if (--slowLeft <= 0) clearInterval(slow);
      }, 500);
    }
  }, 50);
  // Late-loading resources can still land after that: re-check whenever the
  // page finishes loading and whenever the tab comes back to the foreground.
  window.addEventListener("load", ensureRegistered);
  document.addEventListener("visibilitychange", ensureRegistered);

  // A breadcrumb for support: when a dashboard says "Custom element not
  // found: geely-card", this line's presence (or absence) in the browser
  // console separates "the file never ran" from "it ran and something else
  // is wrong" - the two have opposite fixes.
  console.info(
    `%c GEELY-CARD %c ${VERSION} loaded - ${CARD_TAGS.length} cards registered`,
    "background:#2fd6a4;color:#0b2b22;font-weight:600;border-radius:3px 0 0 3px",
    "background:#0b2b22;color:#2fd6a4;border-radius:0 3px 3px 0");

  window.customCards = window.customCards || [];
  /* A status tile that renders as plain text in the picker (preview: false
   * never creates an element, so it shows even when everything else is
   * broken) - live diagnosis for phone-only users, no console needed. */

  class GeelyCardStatus extends HTMLElement {
    setConfig() {}
    set hass(_h) {
      this.innerHTML = `<ha-card style="padding:12px;font-size:12px">
        Geely card status: ${esc(STATUS.line())}</ha-card>`;
    }
    getCardSize() { return 1; }
  }
  try {
    if (!customElements.get("geely-card-status")) {
      customElements.define("geely-card-status", GeelyCardStatus);
    }
  } catch (err) { /* status must never break the file */ }

  /* Home Assistant's service worker keeps a CacheFirst copy of every file for
   * 24 hours, so a page can genuinely run an older copy of this script beside
   * a newer one - which is how a fixed card keeps behaving like the broken
   * one. Whoever is newer wins: drop the other copy's picker entries instead
   * of skipping ours. */
  const OURS = ["geely-card", "geely-card-compact", "geely-card-top", "geely-card-mini", "geely-card-strip", "geely-card-status"];
  const rank = (v) => String(v || "0").split(".").reduce(
    (acc, part) => acc * 1000 + (parseInt(part, 10) || 0), 0);
  const previous = window.__geelyCardVersion;
  if (previous !== undefined && rank(previous) > rank(VERSION)) {
    console.info(`geely-card: v${previous} already loaded, not downgrading to v${VERSION}`);
  } else {
    if (previous !== undefined) {
      console.info(`geely-card: replacing the v${previous} picker entries with v${VERSION}`);
    }
    window.__geelyCardVersion = VERSION;
    // Mutate in place. Home Assistant imports this array once and keeps that
    // reference, so assigning a new array hands it a list it will never read
    // again - the cards then vanish from the picker entirely. (Caught by the
    // picker test after doing exactly that.)
    for (let i = window.customCards.length - 1; i >= 0; i--) {
      if (OURS.includes(window.customCards[i].type)) window.customCards.splice(i, 1);
    }
    window.customCards.push(
      {
        type: "geely-card",
        name: "Geely Card",
        description: "The full Geely cockpit: battery, charging, climate, tires, trip and one-tap controls.",
        preview: true,
      },
      {
        type: "geely-card-compact",
        name: "Geely Card (compact)",
        description: "Battery, range and the controls that matter - lock, rapid heat / cool, defrost, trunk.",
        preview: true,
      },
      {
        type: "geely-card-top",
        name: "Geely Card (top view)",
        description: "The car from above: tire pressures at each wheel, and live status on every door, the hood, the sunroof and the trunk.",
        preview: true,
      },
      {
        type: "geely-card-mini",
        name: "Geely Card (mini)",
        description: "A small square: range, cabin temperature, status, a lock button that follows the car, and quick heat / cool.",
        preview: true,
      },
      {
        type: "geely-card-strip",
        name: "Geely Card (strip)",
        description: "One row: range, battery, lock state - and lock, rapid heat / cool, trunk and find as icon buttons.",
        preview: true,
      },
      {
        type: "geely-card-status",
        name: `Geely Card (status v${VERSION})`,
        description: STATUS.line(),
        preview: false,
      },
    );
  }

  const statusEntry = window.customCards.find((c) => c.type === "geely-card-status");
})();
