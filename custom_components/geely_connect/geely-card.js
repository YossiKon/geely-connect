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
    "tire_rear_left", "door_rear_left", "combined_range", "charge_complete",
    "charge_voltage", "electric_range", "charge_current", "door_passenger",
    "charger_plug", "total_mileage", "refresh_data", "unlock_trunk",
    "door_driver", "12v_battery", "trip_meter", "fuel_level", "fuel_range",
    "pack_power", "efficiency", "find_car", "connected", "defrost",
    "charging", "climate", "battery", "trunk", "doors", "speed", "hood",
  ].sort((a, b) => b.length - a.length);

  const ACCENT = "var(--geely-accent, #2fd6a4)";
  const AMBER = "var(--geely-warn, #e8a13a)";

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
        <linearGradient id="gp" x1="0" y1="90" x2="0" y2="250" gradientUnits="userSpaceOnUse">
          <stop offset="0" stop-color="#d7dce2"/>
          <stop offset=".45" stop-color="#aab2bc"/>
          <stop offset=".8" stop-color="#848d98"/>
          <stop offset="1" stop-color="#6d7681"/>
        </linearGradient>
        <linearGradient id="gg" x1="0" y1="100" x2="0" y2="150" gradientUnits="userSpaceOnUse">
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
          <path d="M178 122 C210 103 250 92 300 87 C330 84 360 85 386 89
            C420 96 452 112 478 127 L462 131 C400 138 300 136 228 132
            C205 130 188 127 178 122 Z"/>
        </clipPath>
        <mask id="arches">
          <rect x="0" y="0" width="760" height="300" fill="#fff"/>
          <circle cx="192" cy="225" r="50" fill="#000"/>
          <circle cx="581" cy="225" r="50" fill="#000"/>
        </mask>
      </defs>

      <ellipse class="shadow" cx="382" cy="268" rx="296" ry="11" filter="url(#soft)"/>
      <ellipse class="glow" cx="382" cy="263" rx="276" ry="9" filter="url(#soft)"/>

      <g mask="url(#arches)">
        <path class="paint" d="
          M92 250
          C82 244 78 232 79 218
          C80 200 81 178 85 158
          C96 140 108 132 122 127
          C150 112 190 96 240 86
          C270 80 300 79 322 80
          C360 82 390 85 412 90
          C440 98 470 116 496 130
          C510 136 520 139 534 141
          C560 145 600 150 640 156
          C662 159 676 165 679 174
          C682 190 681 210 676 226
          C672 240 664 248 650 251
          C600 255 560 255 520 255
          L250 255
          C190 255 130 253 92 250 Z"/>
        <path class="rocker" d="M252 248 L516 248 L516 255 L252 255 Z"/>
      </g>

      <path class="haunch" d="M128 152 C 170 138 220 136 252 146"/>
      <path class="crease" d="M110 162 C 300 149 500 147 658 163"/>

      <path class="glass" d="M178 122 C210 103 250 92 300 87 C330 84 360 85 386 89
        C420 96 452 112 478 127 L462 131 C400 138 300 136 228 132
        C205 130 188 127 178 122 Z"/>
      <path class="streak" clip-path="url(#gc)" d="M330 76 L 282 138 M 376 74 L 324 140"/>

      <rect class="handle" x="308" y="145" width="26" height="5" rx="2.5"/>
      <rect class="handle" x="440" y="143" width="26" height="5" rx="2.5"/>

      <path class="headlight" d="M624 152 q24 3 33 10 l-5 8 q-15-6-30-8 z"/>
      <path class="taillight" d="M84 160 l34 -3 1 9 -34 3 z"/>
      <rect class="port" x="126" y="146" width="14" height="12" rx="4"/>
      <circle class="portdot" cx="133" cy="152" r="3"/>

      <g class="wheel-g" transform="translate(192 225)">${_WHEEL}</g>
      <g class="wheel-g" transform="translate(581 225)">${_WHEEL}</g>

      <circle class="ind ${open.trunk ? "on" : ""}" cx="110" cy="132" r="7"/>
      <circle class="ind ${open.rear ? "on" : ""}" cx="300" cy="138" r="7"/>
      <circle class="ind ${open.front ? "on" : ""}" cx="440" cy="136" r="7"/>
      <circle class="ind ${open.hood ? "on" : ""}" cx="600" cy="148" r="7"/>
    </svg>`;

  const _WHEEL = `
      <circle class="tire" r="43"/>
      <circle class="rim" r="27" fill="url(#gr)"/>
      <circle class="disc" r="17"/>
      <circle class="hubcap" r="5"/>`;

  /* ----------------------------------------------------------- icons ----- */
  /* One cohesive hand-drawn stroke set - 24px grid, 1.8 stroke, round caps. */

  const ICONS = {
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
    .chip.on { color: ${ACCENT}; border-color: color-mix(in srgb, ${ACCENT} 45%, transparent); }
    .chip.warn { color: ${AMBER}; border-color: color-mix(in srgb, ${AMBER} 45%, transparent); }
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
    .act.on { color: ${ACCENT}; border-color: color-mix(in srgb, ${ACCENT} 45%, transparent); }
    .act.armed { color: ${AMBER}; border-color: ${AMBER}; animation: geely-arm 1s ease infinite; }
    .act.armed span { color: ${AMBER}; }
    .act[disabled] { opacity: .35; pointer-events: none; }
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
          id.startsWith("sensor.") && id.endsWith("_battery"),
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
      return this._watched()
        .map((e) => { const st = this._st(e); return st ? st.state : "-"; })
        .join("|") + `|${this._armed || ""}`;
    }

    _call(domain, service, entitySuffix, data = {}) {
      this._hass.callService(domain, service, {
        entity_id: this._eid(domain, entitySuffix),
        ...data,
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

    _onAction(key) {
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
        case "defrost": this._call("switch", "toggle", "defrost"); break;
        case "vent": this._call("switch", "toggle", "window_ventilation"); break;
        case "find": this._call("button", "press", "find_car"); break;
        case "refresh": this._call("button", "press", "refresh_data"); break;
      }
    }

    _wire() {
      this.shadowRoot.querySelectorAll("[data-act]").forEach((el) =>
        el.addEventListener("click", () => this._onAction(el.dataset.act)));
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
      const charging = conn && conn.state === "Charging";
      const battery = NUM(this._st("sensor.battery"));
      const range = this._st("sensor.electric_range");
      const locked = this._st("lock.doors");
      const climate = this._st("climate.climate");
      const doorsOpen = ["door_driver", "door_passenger", "door_rear_left",
        "door_rear_right", "trunk", "hood"]
        .filter((d) => { const st = this._st(`binary_sensor.${d}`); return st && st.state === "on"; });
      return { conn, charging, battery, range, locked, climate, doorsOpen };
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
      const low = s.battery != null && s.battery <= 20;
      const climateOn = s.climate && s.climate.state !== "off";
      const defrost = this._st("switch.defrost");
      const online = this._st("binary_sensor.connected");

      const chips = [
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
        .carwrap { flex:1; max-width:300px; margin-left:auto; }
        </style>
        <div class="shell">
          <div class="head">
            <div class="title">
              <i class="dot ${online && online.state === "off" ? "off" : ""}"></i>
              ${esc(this._title())}
            </div>
            <span class="micro">${batt}%</span>
          </div>
          <div class="hero">
            <div>
              <div class="num n ${OK(s.range) ? "" : "unavail"}">${range}<span class="u">km</span></div>
              <div class="micro sub">Range</div>
            </div>
            <div class="carwrap">${CAR_SVG(s.charging ? "charging" : "", this._openMap())}</div>
          </div>
          <div class="bar ${low ? "low" : ""} ${s.charging ? "charging" : ""}" style="margin:2px 0 10px">
            <i style="width:${batt === "—" ? 0 : batt}%"></i>
          </div>
          <div class="chips" style="margin-bottom:12px">${chips || '<span class="chip">Parked</span>'}</div>
          <div class="actions">
            ${this._actBtn("lock", "Lock", "lock", { on: s.locked && s.locked.state === "locked" })}
            ${this._actBtn("unlock", "Unlock", "unlock")}
            ${this._actBtn("climate", "Climate", "climate", { on: climateOn })}
            ${this._actBtn("defrost", "Defrost", "defrost", { on: defrost && defrost.state === "on" })}
            ${this._actBtn("trunk", "Trunk", "trunk")}
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
        "time.scheduled_charging_start", "time.scheduled_charging_end",
        "binary_sensor.door_driver", "binary_sensor.door_passenger",
        "binary_sensor.door_rear_left", "binary_sensor.door_rear_right",
        "binary_sensor.trunk", "binary_sensor.hood", "binary_sensor.connected",
        "binary_sensor.charger_plug"];
    }

    getCardSize() { return 9; }

    _row(label, st, opts = {}) {
      if (!st && !opts.value) return "";
      const value = opts.value != null ? opts.value
        : OK(st) ? `${st.state}${UNIT(st) ? " " + UNIT(st) : ""}` : "—";
      return `<div class="row ${opts.accent ? "accent" : ""} ${opts.warn ? "warn" : ""}">
          <span>${esc(label)}</span><b>${esc(value)}</b></div>`;
    }

    _render() {
      if (!this._prefix) return this._missing();
      const s = this._carState();
      const batt = s.battery == null ? "—" : Math.round(s.battery);
      const low = s.battery != null && s.battery <= 20;
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
      const hybrid = !!this._st("sensor.fuel_level");
      const speed = NUM(this._st("sensor.speed"));

      const tire = (suffix) => {
        const st = this._st(`sensor.tire_${suffix}`);
        return OK(st) ? `${Math.round(NUM(st))}<i>${esc(UNIT(st))}</i>` : "—";
      };
      const statusLine = s.charging
        ? `Charging${OK(power) ? " · " + power.state + " kW" : ""}`
        : speed != null && speed > 0 ? `Driving · ${Math.round(speed)} km/h`
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
          <div class="head">
            <div>
              <div class="title">
                <i class="dot ${online && online.state === "off" ? "off" : ""}"></i>
                ${esc(this._title())}
              </div>
              <div class="status ${s.charging ? "charging" : ""}">${esc(statusLine)}</div>
            </div>
            <span class="micro">${batt}%</span>
          </div>

          <div class="hero">
            <div>
              <div class="num n ${OK(s.range) ? "" : "unavail"}">${range}<span class="u">km</span></div>
              <div class="micro" style="margin-top:5px">${hybrid ? "Electric range" : "Range"}</div>
            </div>
            <div class="side">
              ${OK(interior) ? `<div class="num">${Math.round(NUM(interior))}°</div>
                <div class="u2">inside${OK(exterior) ? ` · ${Math.round(NUM(exterior))}° out` : ""}</div>` : ""}
            </div>
          </div>
          <div class="bar ${low ? "low" : ""} ${s.charging ? "charging" : ""}">
            <i style="width:${batt === "—" ? 0 : batt}%"></i>
          </div>

          <div class="carwrap">${CAR_SVG(s.charging ? "charging" : "", this._openMap())}</div>

          <div class="actions" style="margin-top:8px">
            ${this._actBtn("lock", "Lock", "lock", { on: s.locked && s.locked.state === "locked" })}
            ${this._actBtn("unlock", "Unlock", "unlock")}
            ${this._actBtn("climate", "Climate", "climate", { on: climateOn })}
            ${this._actBtn("defrost", "Defrost", "defrost", { on: defrost && defrost.state === "on" })}
            ${this._actBtn("vent", "Vent", "vent", { on: vent && vent.state === "on" })}
            ${this._actBtn("trunk", "Trunk", "trunk")}
            ${this._actBtn("find", "Find", "find")}
            ${this._actBtn("refresh", "Sync", "refresh")}
          </div>

          <hr class="hairline">
          <p class="micro">${icon("charge")} Charging</p>
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

          ${hybrid ? `
          <hr class="hairline">
          <p class="micro">${icon("fuel")} Fuel</p>
          <div class="grid sec">
            ${this._row("Fuel level", this._st("sensor.fuel_level"))}
            ${this._row("Fuel range", fuelRange)}
            ${this._row("Combined range", combined, { accent: true })}
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
            ${this._row("Consumption", this._st("sensor.average_consumption"))}
            ${this._row("Efficiency", this._st("sensor.efficiency"))}
            ${this._row("12 V battery", this._st("sensor.12v_battery"))}
            ${this._row("Service in", days, {
              warn: OK(days) && NUM(days) != null && NUM(days) <= 30,
              value: OK(days) ? `${days.state} d / ${OK(this._st("sensor.distance_to_service"))
                ? this._st("sensor.distance_to_service").state + " km" : "—"}` : "—" })}
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

  // This file is delivered both as a Lovelace resource (module) and as an
  // extra script, so it can execute twice in one page. The guard handles the
  // common case; the try absorbs anything the platform throws anyway, because
  // a failed re-define must never abort the run before customCards is filled.
  const defineOnce = (tag, cls) => {
    try {
      if (!customElements.get(tag)) customElements.define(tag, cls);
    } catch (err) {
      console.warn(`geely-card: define(${tag}) skipped:`, err);
    }
  };
  const registerElements = () => {
    defineOnce("geely-card-compact", GeelyCardCompact);
    defineOnce("geely-card", GeelyCard);
  };
  registerElements();

  // Some cards (anything built on lit's scoped registries - Mushroom,
  // button-card and friends) ship the scoped-custom-element-registry
  // polyfill, which REPLACES window.customElements wholesale. Definitions
  // made on the original registry are invisible to the replacement's get(),
  // so if this file runs first - and as an extra script it usually does -
  // the card picker asks the new registry, finds nothing, and spins forever.
  // Watch for the swap for a while and re-register through whichever
  // registry is current; the polyfill scopes its native names, so the old
  // definition does not block the new one.
  let knownRegistry = window.customElements;
  let watchLeft = 120;                      // 120 x 500 ms = one minute
  const watchdog = setInterval(() => {
    if (window.customElements !== knownRegistry ||
        !window.customElements.get("geely-card")) {
      if (window.customElements !== knownRegistry) {
        console.info("geely-card: custom element registry was replaced - re-registering");
      }
      knownRegistry = window.customElements;
      registerElements();
    }
    if (--watchLeft <= 0) clearInterval(watchdog);
  }, 500);

  // A breadcrumb for support: when a dashboard says "Custom element not
  // found: geely-card", this line's presence (or absence) in the browser
  // console separates "the file never ran" from "it ran and something else
  // is wrong" - the two have opposite fixes.
  const VERSION = document.currentScript && document.currentScript.src
    ? (document.currentScript.src.split("?v=")[1] || "?") : "module";
  console.info(
    `%c GEELY-CARD %c ${VERSION} loaded - geely-card, geely-card-compact registered`,
    "background:#2fd6a4;color:#0b2b22;font-weight:600;border-radius:3px 0 0 3px",
    "background:#0b2b22;color:#2fd6a4;border-radius:0 3px 3px 0");

  window.customCards = window.customCards || [];
  if (!window.customCards.some((c) => c.type === "geely-card")) {
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
        description: "Battery, range and the controls that matter - lock, climate, defrost, trunk.",
        preview: true,
      },
    );
  }
})();
