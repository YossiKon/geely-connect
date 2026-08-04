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
 * Design language: automotive instrument cluster. Ultra-light oversized
 * numerals, letter-spaced micro-labels, hairline dividers, one electric
 * accent that follows the car's state (teal while charging, amber on
 * warnings). Destructive actions (unlock, trunk) arm on first tap and fire
 * on the second, so a stray touch on a wall tablet cannot open the car.
 */
"use strict";

(() => {
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

  /* A clean generic EV side profile - drawn here, no external assets. */
  const CAR_SVG = (cls) => `
    <svg class="car ${cls}" viewBox="0 0 720 240" fill="none" aria-hidden="true">
      <path class="body" d="M78 178c-20-2-34-10-36-26-2-14 4-24 18-30 10-40 34-64 92-72 64-9 148-10 216 2 40 7 74 24 100 48 56 6 96 16 118 30 12 8 16 18 12 30-3 10-12 16-26 18l-36 2"/>
      <path class="glass" d="M170 62c50-14 150-16 224-4 26 4 52 16 74 34l-118 4c-40 1-98 0-132-4-22-3-40-12-48-30z"/>
      <line class="hair" x1="288" y1="64" x2="286" y2="100"/>
      <circle class="wheel" cx="182" cy="182" r="40"/>
      <circle class="wheel" cx="548" cy="182" r="40"/>
      <circle class="hub" cx="182" cy="182" r="14"/>
      <circle class="hub" cx="548" cy="182" r="14"/>
      <path class="ground" d="M96 226h530"/>
    </svg>`;

  const ICONS = {
    lock: "M12 17a2 2 0 0 0 2-2 2 2 0 0 0-2-2 2 2 0 0 0-2 2 2 2 0 0 0 2 2m6-9a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V10a2 2 0 0 1 2-2h1V6a5 5 0 0 1 5-5 5 5 0 0 1 5 5v2h1m-6-5a3 3 0 0 0-3 3v2h6V6a3 3 0 0 0-3-3z",
    unlock: "M12 17a2 2 0 0 0 2-2 2 2 0 0 0-2-2 2 2 0 0 0-2 2 2 2 0 0 0 2 2m6-9a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V10a2 2 0 0 1 2-2h9V6a3 3 0 0 0-3-3 3 3 0 0 0-3 3H7a5 5 0 0 1 5-5 5 5 0 0 1 5 5v2h1z",
    climate: "M6.59 0.66c2.34-1.81 4.88.4 5.45 3.84.43 2.54-.42 4.72-2.04 5.5 1.63.78 2.47 2.96 2.04 5.5-.57 3.44-3.11 5.65-5.45 3.84C4.25 17.53 4 14.16 4 10S4.25 2.47 6.59.66M12 10c0-1.47.5-4.13 1.63-6.1a7.98 7.98 0 0 1 6.32 6.19c-2.02.35-4.5.35-5.95-.09zm7.95 2.08a7.98 7.98 0 0 1-6.32 6.19C12.5 16.3 12 13.5 12 12l2-.17c1.45.44 3.93.6 5.95.25z",
    defrost: "M7 20a1 1 0 0 1-1 1 1 1 0 0 1-1-1c0-1.5 1.5-2.5 1.5-4S5 13.5 5 12s1.5-2.5 1.5-4S5 5.5 5 4a1 1 0 0 1 1-1 1 1 0 0 1 1 1c0 1.5-1.5 2.5-1.5 4S7 10.5 7 12s-1.5 2.5-1.5 4S7 18.5 7 20m6 0a1 1 0 0 1-1 1 1 1 0 0 1-1-1c0-1.5 1.5-2.5 1.5-4s-1.5-2.5-1.5-4 1.5-2.5 1.5-4S11 5.5 11 4a1 1 0 0 1 1-1 1 1 0 0 1 1 1c0 1.5-1.5 2.5-1.5 4s1.5 2.5 1.5 4-1.5 2.5-1.5 4 1.5 2.5 1.5 4m6 0a1 1 0 0 1-1 1 1 1 0 0 1-1-1c0-1.5 1.5-2.5 1.5-4s-1.5-2.5-1.5-4 1.5-2.5 1.5-4S17 5.5 17 4a1 1 0 0 1 1-1 1 1 0 0 1 1 1c0 1.5-1.5 2.5-1.5 4s1.5 2.5 1.5 4-1.5 2.5-1.5 4 1.5 2.5 1.5 4",
    trunk: "M3 13v7h2v-2h14v2h2v-7L19 5H5l-2 8m4.5-6H12v4H6l1.5-4M14 7h3.5l1.5 4h-5V7z",
    vent: "M4 5h16v2H4V5m0 4h16v2H4V9m8 4 4 4h-3v4h-2v-4H8l4-4z",
    find: "M12 4a8 8 0 0 1 8 8c0 3.5-2.3 6.5-5.5 7.6L12 22l-2.5-2.4A8.01 8.01 0 0 1 4 12a8 8 0 0 1 8-8m0 3a5 5 0 0 0-5 5 5 5 0 0 0 5 5 5 5 0 0 0 5-5 5 5 0 0 0-5-5m0 2a3 3 0 0 1 3 3 3 3 0 0 1-3 3 3 3 0 0 1-3-3 3 3 0 0 1 3-3z",
    refresh: "M17.65 6.35A7.96 7.96 0 0 0 12 4a8 8 0 0 0-8 8 8 8 0 0 0 8 8c3.73 0 6.84-2.55 7.73-6h-2.08A5.99 5.99 0 0 1 12 18a6 6 0 0 1-6-6 6 6 0 0 1 6-6c1.66 0 3.14.69 4.22 1.78L13 11h7V4l-2.35 2.35z",
    bolt: "M11 15H6l7-14v8h5l-7 14v-8z",
  };
  const icon = (name) =>
    `<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="${ICONS[name]}"/></svg>`;

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
    }
    .hairline { border: 0; border-top: 1px solid var(--divider-color, rgba(120,130,140,.18)); margin: 14px 0 12px; }
    .num { font-weight: 200; font-variant-numeric: tabular-nums; letter-spacing: -0.02em; line-height: 1; }
    .car { width: 100%; height: auto; display: block; }
    .car .body { stroke: currentColor; stroke-width: 5; stroke-linecap: round; opacity: .85; }
    .car .glass { stroke: currentColor; stroke-width: 3.5; opacity: .35; }
    .car .hair  { stroke: currentColor; stroke-width: 2.5; opacity: .3; }
    .car .wheel { stroke: currentColor; stroke-width: 5; opacity: .85; }
    .car .hub   { stroke: currentColor; stroke-width: 3; opacity: .45; }
    .car .ground{ stroke: currentColor; stroke-width: 2; opacity: .12; stroke-dasharray: 2 10; stroke-linecap: round; }
    .car.charging .body, .car.charging .wheel { stroke: ${ACCENT}; filter: drop-shadow(0 0 6px color-mix(in srgb, ${ACCENT} 55%, transparent)); }
    .chips { display: flex; flex-wrap: wrap; gap: 6px; }
    .chip {
      display: inline-flex; align-items: center; gap: 5px;
      font-size: 11px; font-weight: 500; padding: 4px 10px; border-radius: 999px;
      border: 1px solid var(--divider-color, rgba(120,130,140,.25));
      color: var(--secondary-text-color, #7a7f87); white-space: nowrap;
    }
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
      this._armed = null;          // suffix of the armed destructive action
      this._armedTimer = null;
      this._sig = "";
    }

    setConfig(config) {
      this._config = config || {};
      this._prefix = this._config.prefix || null;
      this._sig = "";
      if (this._hass) this._render();
    }

    set hass(hass) {
      this._hass = hass;
      if (!this._prefix) this._prefix = this._detectPrefix(hass);
      const sig = this._signature(hass);
      if (sig !== this._sig) {
        this._sig = sig;
        this._render();
      }
    }

    _detectPrefix(hass) {
      // The battery sensor exists on every Geely; its object_id carries the
      // device slug this integration derives every other entity id from.
      const fromPlatform = Object.keys(hass.entities || {}).find(
        (id) =>
          hass.entities[id].platform === "geely_connect" &&
          id.startsWith("sensor.") && id.endsWith("_battery"),
      );
      const found = fromPlatform ||
        Object.keys(hass.states).find(
          (id) => id.startsWith("sensor.") && id.endsWith("_battery") &&
            hass.states[`climate.${id.slice(7, -8)}_climate`],
        );
      return found ? found.slice("sensor.".length, -"_battery".length) : null;
    }

    _st(entity) {
      const [domain, suffix] = entity.split(".");
      return this._hass.states[`${domain}.${this._prefix}_${suffix}`];
    }

    _signature(hass) {
      if (!this._prefix) return "no-prefix";
      return this._watched()
        .map((e) => { const st = this._st(e); return st ? st.state : "-"; })
        .join("|") + `|${this._armed || ""}`;
    }

    _call(domain, service, entitySuffix, data = {}) {
      this._hass.callService(domain, service, {
        entity_id: `${domain}.${this._prefix}_${entitySuffix}`,
        ...data,
      });
    }

    /* Destructive actions arm on the first tap and fire on the second. */
    _guarded(key, fire) {
      if (this._armed === key) {
        clearTimeout(this._armedTimer);
        this._armed = null;
        fire();
        this._sig = ""; this._render();
        return;
      }
      this._armed = key;
      clearTimeout(this._armedTimer);
      this._armedTimer = setTimeout(() => {
        this._armed = null; this._sig = ""; this._render();
      }, 3000);
      this._sig = ""; this._render();
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
        s.charging && `<span class="chip on">${icon("bolt")} ${power != null ? power.toFixed(1) + " kW" : "Charging"}</span>`,
        !s.charging && s.conn && s.conn.state === "Plugged in" && `<span class="chip">Plugged in</span>`,
        climateOn && `<span class="chip on">Climate on</span>`,
        s.doorsOpen.length > 0 && `<span class="chip warn">${s.doorsOpen.length} open</span>`,
      ].filter(Boolean).join("");

      this.shadowRoot.innerHTML = `<style>${BASE_CSS}
        .head { display:flex; align-items:baseline; justify-content:space-between; }
        .title { font-size:13px; font-weight:600; letter-spacing:.02em; display:flex; align-items:center; gap:7px; }
        .dot { width:6px; height:6px; border-radius:50%; background:${ACCENT}; }
        .dot.off { background:${AMBER}; }
        .hero { display:flex; align-items:center; gap:18px; margin:10px 0 4px; }
        .hero .n { font-size:44px; }
        .hero .u { font-size:13px; color: var(--secondary-text-color); margin-left:3px; }
        .hero .sub { margin-top:4px; }
        .carwrap { flex:1; max-width:270px; margin-left:auto; opacity:.9; }
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
            <div class="carwrap">${CAR_SVG(s.charging ? "charging" : "")}</div>
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
        .carwrap { margin:8px auto 2px; max-width:430px; }
        .grid { display:grid; grid-template-columns:1fr 1fr; gap:2px 26px; }
        .row { display:flex; justify-content:space-between; align-items:baseline;
               font-size:12.5px; padding:5px 0; color: var(--secondary-text-color); }
        .row b { font-weight:500; color: var(--primary-text-color); font-variant-numeric: tabular-nums; }
        .row.accent b { color:${ACCENT}; }
        .row.warn b { color:${AMBER}; }
        .tires { display:grid; grid-template-columns:repeat(4,1fr); gap:8px; text-align:center; }
        .tires .t { border:1px solid var(--divider-color, rgba(120,130,140,.2));
                    border-radius:12px; padding:8px 4px 6px; }
        .tires .t b { font-size:15px; font-weight:400; font-variant-numeric:tabular-nums; }
        .tires .t i { font-style:normal; font-size:9px; color:var(--secondary-text-color); margin-left:2px; }
        .tires .t .micro { margin-top:3px; font-size:8.5px; }
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

          <div class="carwrap">${CAR_SVG(s.charging ? "charging" : "")}</div>

          <div class="actions" style="margin-top:6px">
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
          <p class="micro">Charging</p>
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
          <p class="micro">Fuel</p>
          <div class="grid sec">
            ${this._row("Fuel level", this._st("sensor.fuel_level"))}
            ${this._row("Fuel range", fuelRange)}
            ${this._row("Combined range", combined, { accent: true })}
          </div>` : ""}

          <hr class="hairline">
          <p class="micro">Tires</p>
          <div class="tires" style="margin-top:8px">
            <div class="t"><b>${tire("front_left")}</b><div class="micro">FL</div></div>
            <div class="t"><b>${tire("front_right")}</b><div class="micro">FR</div></div>
            <div class="t"><b>${tire("rear_left")}</b><div class="micro">RL</div></div>
            <div class="t"><b>${tire("rear_right")}</b><div class="micro">RR</div></div>
          </div>

          <hr class="hairline">
          <p class="micro">Trip &amp; health</p>
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

  if (!customElements.get("geely-card-compact")) {
    customElements.define("geely-card-compact", GeelyCardCompact);
  }
  if (!customElements.get("geely-card")) {
    customElements.define("geely-card", GeelyCard);
  }

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
