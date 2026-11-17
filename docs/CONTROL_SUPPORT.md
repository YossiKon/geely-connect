# Remote controls and evidence

Controls depend on the car, fitted equipment and regional backend. A server
acknowledgement means the request was accepted; confirm the resulting vehicle
state or check the official app. A visible control alone does not establish
that equipment is fitted.

The full Geely card exposes the existing seat and steering-wheel controls in
its climate panel. The native departure action accepts a vehicle entity
selector, and the script blueprint provides a Start/Stop workflow using the
existing vehicle request.

Lights-only flashing and EM frunk release remain unexposed. Captured mappings
suggest `RHL` with `light-flash` and `RDU start` with target `hood`, respectively,
but neither has an owner-confirmed physical result. Catalogue declarations
alone do not establish safe command direction: EM doors and boot use `stop` to
open. Each needs an owner to run the action and report what the car actually
does before it becomes a button. The E2/EX2's existing frunk button is unchanged.

## Control inventory

“Original” refers to the original Geely backend. “EM” refers to the new app's
vehicle-token route; its older fallback route has different capabilities.
“Mapped” describes software/request support, not a guarantee across models.

| Control | Available surface | Evidence and limits |
| --- | --- | --- |
| Door lock / unlock | Lock entity, cards | Mapped on both routes; EM direction checked by two owners. |
| Climate on/off, setpoint | Climate entity, cards | Both routes. E2/EX2 1.0 uses its captured request and whole-degree setpoints. |
| Rapid warming | Climate preset, cards | Compound request; seat effects vary by car. A spaced follow-up script is supplied for cars that ignore the compound seat request. |
| Rapid cooling | Climate preset, cards | May open windows. EM compound seat ventilation is omitted pending a fitted-car capture. |
| Seat heat | Per-seat select, full card climate panel | Both front seats verified on Low and restored on one original-APAC EX5. EM front-seat heat mapped; rear seats require catalogue/hardware support. |
| Seat ventilation | Per-seat select, full card climate panel | Both front seats verified on Low and restored on that EX5. EM standalone translation is inferred; physical support remains unverified ([#63](https://github.com/YossiKon/geely-connect/issues/63)). |
| Steering-wheel heat | Switch, full card climate panel | On/Off report verified and restored on that EX5. EM mapping exists. |
| Windscreen defrost | Switch, cards | Both routes mapped; not actuated in this audit. |
| Cabin purge / G-Clean | Switch, full card | Both routes mapped, different request shapes; not actuated in this audit. |
| All windows | Cover, full card | Group open/close; mapped on both routes. E2 has a separate captured request. |
| Window ventilation | Switch, cards | Mapped on both routes; E2 motion owner-confirmed, read-back still a separate issue. |
| Sunroof | Cover, full card | Fitted hardware required; EM catalogue gating remains unresolved (#63). |
| Sunshade | Cover, full card | Same fitted-feature limitation. |
| Boot unlatch | Button, cards | Releases a latch; does not power the gate open. Several owners confirmed operation, others report latch failures. |
| Powered EX5 boot | No supported control | Inspected EX5 firmware lacks a remote open handler ([#20](https://github.com/YossiKon/geely-connect/issues/20)). |
| Frunk release | Existing E2/EX2 button | Owner-confirmed E2/EX2 request preserved. EM hood mapping remains unexposed pending owner confirmation. |
| Charging start/stop | Switch, cards | Both routes mapped where supported. E2 1.0 catalogues without an actual charge switch are excluded. |
| Charging schedule | Switch and start/end time entities, full card | Original slots 6/2 and EM mappings. Off-state edits can be discarded on original APAC ([#123](https://github.com/YossiKon/geely-connect/issues/123)); do not assume an acknowledgement persisted. |
| Charge connector release | No independent control | The reported latch follows door locks; stopping charge is not a separate release command. |
| Native one-off departure | Administrator action and [script blueprint](../blueprints/script/geely_connect/native_departure.yaml) | Original 2.0 request only. Reads current settings before writing. EM and E2 1.0 writes remain refused. |
| Native weekly departure | Official app only | The one-off action does not edit or re-arm the app's weekly schedule. |
| Independent battery conditioning | No standalone setter | Native departure preserves the existing battery setting. It does not expose a new independent toggle. |
| HA-timed preconditioning | Existing automation blueprints | HA calls the climate entity at the chosen time. Distinct from a departure stored in the car. |
| Camping / Parking Comfort | Switch where available | State reading demonstrated; accepted remote writes have not demonstrated a vehicle effect ([#45](https://github.com/YossiKon/geely-connect/issues/45)). EM token route suppresses the unmapped switch. |
| Find Car | Button, cards | Horn and flashing lights; not actuated in this audit. |
| Flash Lights | No exposed control | Captured lights-only mapping remains pending owner confirmation; Find Car also sounds the horn. |
| Horn only | No dedicated control | Catalogue lead; exact supported request not established here. |
| Cabin fan speed | No setter | Rapid climate uses the car's own programme; no captured remote fan-speed parameter. |
| Individual windows / percentage | No setter | Covers support group open/close, not set-position. A position reading does not establish a writable position. |
| AC charging current limit | No setter | Measured-current sensor exists; no captured cloud setter ([#80](https://github.com/YossiKon/geely-connect/issues/80)). |
| Charge target / V2L floor | No supported setter or curated limit sensor | EU legacy read reported, conversion still needs change-and-read evidence; original setter reports `notSupport` ([#107](https://github.com/YossiKon/geely-connect/issues/107)). |
| Driver profiles / seat position / mirrors | No supported controls | Await official-app controls and captures ([#89](https://github.com/YossiKon/geely-connect/issues/89)). |
| Data / position refresh | Refresh Data button | Gateway contact and actual car/GPS freshness are distinct. Check Car Reported At, not just Last Updated. |
| Raw development actions | Administrator actions | Known requests only; arbitrary service names and accepted responses do not establish vehicle support. |

## Native departure setup

Import `blueprints/script/geely_connect/native_departure.yaml` as a script
blueprint. Select any entity belonging to the intended Geely and an
`input_datetime` helper with **both date and time** enabled. Set a future date
and time, then run the script as an administrator. Start is the default;
choose Stop to cancel using the departure already stored in the car, even if
the helper is unavailable. Dates use Home Assistant's time zone.

The script can be placed on a dashboard alongside the helper. It calls
`geely_connect.set_scheduled_trip` immediately to store the one-off trip; it
is not a timer in HA. The action also accepts `entity_id` directly, and keeps
the existing optional `vin` argument. Supplying both requires them to identify
the same loaded vehicle. Multiple vehicles with neither selector are refused.

Cabin Comfort and Battery Temperature Maintenance retain the values read from
the car (the action's captured defaults apply when a field is absent). Use the
official app to choose those settings. This blueprint does not offer weekly
schedule editing or an independent battery-conditioning control.

## Live evidence from 17 November 2026

Quiet checks on one original-APAC EX5 confirmed new vehicle reports for Low
then Off on each front-seat heater and ventilator, and On then Off for the
heated wheel. Remote cabin climate and blower remained off in sampled reports.

With scheduled charging enabled, changing its start from 23:00 to 23:05 was
visible in the first raw schedule read, completed 7.98 seconds after the time
service returned, and persisted through the last sample at 102.94 seconds.
That is an observed upper bound for this run, **not a maximum propagation
latency**. The disabled 23:00–07:00 schedule and all comfort settings were
restored. No candidate code was deployed for these checks, so this evidence
does not validate candidate schedule changes. EM and slower
propagation still need measurements; wake battery cost remains unmeasured.

The initial Refresh reached the gateway while the car's own report stayed
about 85 minutes old; a seat command subsequently produced a new report.
No horn, lock/unlock or opening commands were sent. The public schedule
[evidence report](https://github.com/YossiKon/geely-connect/pull/121#issuecomment-6944715025)
records the measurement limits.
