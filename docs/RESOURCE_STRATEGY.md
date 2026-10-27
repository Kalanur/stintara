# Resource Strategy V0.1

The Engineer page provides a read-only, experimental **next-stop** resource plan.
It is not a multi-stint optimizer or an optimal pit-window recommendation.
References: #22 (spectator milestone), #9 (finished participants), #8 (field validation).
These issues remain open.

## Domain and API

`app/resource_strategy.py` contains provider-independent resource math. Resources
have a kind, current amount, capacity, consumption per lap, range, units,
sample count, freshness and availability. Energy has a distinct `EnergyKind`:
`NONE`, `VIRTUAL_ENERGY`, `HYBRID`, `ERS`. These are not interchangeable systems.
`PitServiceMode` supports `TARGET_LEVEL`, `ADD_AMOUNT`, `FIXED`, `UNSUPPORTED`;
service capability does not alter resource consumption/range formulas.
Only LMU mapping is implemented, in `app/lmu_resources.py`.

`GET /api/sessions/{session_id}/participants/{participant_id}/strategy`
accepts an optional integer `planned_stop_lap`. It validates UUID membership and
uses a read-only repeatable-read transaction for session, lap and sample reads.
Runtime scoring supplies fresh completed laps and the session clock. It returns:

- `status`, `simulator_provider`, `completed_laps`, `finish_projection`;
- `resources`, `reserve_laps`, `limiting_resource`, `effective_range_laps`;
- `latest_safe_stop_lap`, `planned_stop_lap`, `plan_source`, `planned_stop_safe`;
- `shortfall`, `at_pit`, `next_stint_requirements`, `additional_stop_required`;
- optional `provider_pit_settings` and independent Fuel diagnostics.

The existing Fuel endpoint and Fuel Foundation are unchanged. Fuel uses Stintara
persisted checkpoint transitions, last five valid samples, minimum three, median.
Provider fuel averages/range remain diagnostics only. Current fuel comes from the
latest strategy sample; freshness is 0–15 seconds. Historical numbers may remain
visible, but stale samples do not enable planning. Ended/stale sessions, missing
fresh participant scoring and nonzero finish codes (including unknown codes)
receive no active pit recommendation. FIN, DNF and DSQ remain provider states.

## Energy transport and persistence

The LMU adapter maps scoring `virtual_energy` to `energy_current_fraction` and
telemetry `ve_available`/`ve_history` to `energy_available`, `energy_kind` and
`energy_history`. Finite numbers in [0,1] are accepted; bools, strings, nonfinite
and out-of-range levels become unknown in the adapter and are rejected by the
server contract if sent directly. Capability is tri-state; zero VE never implies
fuel-only. Older agents omit the optional fields and therefore yield unknown
Energy capability, not a complete fuel-only recommendation.

The inspected [Pitwall contract at a171b80](https://github.com/Swizzjack/lmu-pitwall/blob/a171b80a7c8c1e02a9d029e32fd717c834d26e71/bridge/src/protocol/messages.rs)
defines oldest-to-newest lap levels with the **last entry as current level**.
The adapter excludes this evolving last entry before computing lap consumption,
retains at most 512 completed checkpoints, and preserves invalid entries as null
to prevent bridging across missing evidence. Positive decreases count; increases
(refill/reset), equal values and invalid adjacent endpoints do not. Use the last
five valid transitions, minimum three, median. They are not called clean laps.

The inspected upstream implementation derives `ve_available` heuristically from
positive virtual energy or battery charge. Stintara follows the explicit signal
but cannot independently prove vehicle capability. This remains a provider
limitation, particularly at zero/depleted readings. No vehicle-name/class inference
or ElectronicsUpdate support is added.

Four nullable columns are added idempotently to existing `strategy_samples`:
`energy_current_fraction` (double precision), `energy_available` (boolean),
`energy_kind` (text), `energy_history` (JSONB). Each accepted 1 Hz sample stores
the same observer's current player scoring level plus normalized telemetry
history/capability. No new table, identity model or history repair is introduced.
Existing rows remain unknown. Repeated history is stored with each sample;
this bounded implementation favors simplicity over history deduplication.
The transport remains `stintara-agent-v1` with additive optional fields.
Deploy the updated server schema before using the rebuilt agent; back up databases
before schema updates. No deployment is performed by this development change.

## Formulas and semantics

All internal Energy quantities/consumption are **fractions**, not percent points.
Display conversion is `fraction × 100`; `.106` consumption is `10.6 % VE/lap`.

- Each usable resource range = current / consumption.
- Effective range = minimum required resource range. A range difference <=0.1 lap
  adds a BALANCED presentation hint; the exact minimum still governs safety.
- Reserve = **1.0 lap**, explicit for every required resource.
- Exact lap race: finish lap = configured lap count. Positive configured duration
  takes precedence if both duration and lap count are supplied.
- Timed race: median of up to five recent positive finite completed lap times,
  minimum three. Remaining seconds = max(0, duration − elapsed). Finish estimate =
  completed laps + ceil(remaining seconds / pace) + 1. It is labelled projected,
  never official remaining laps; without pace/fresh clock it is unavailable.
- Latest resource-safe boundary = completed + floor(effective range − reserve).
  An immediate boundary displays BOX THIS LAP; its projection uses the end of the
  current running lap (completed + 1). No stop is recommended at/after finish.
- Manual plan: pit at the **end of lap N**, integer later than completed and before
  finish. Projected resource at pit = current − (N − completed) × consumption.
  Negative values are retained. Shortfall laps include the 1-lap reserve.
- Post-stop requirement = (finish lap − N + reserve) × consumption.
  Requirements exceeding capacity flag an additional stop, not a feasible one-stop plan.

LMU uses `TARGET_LEVEL`. For confirmed fuel-only vehicles the instruction is
`SET FUEL TO` the required post-stop litres. For VE-enabled vehicles:

- Measured Fuel Ratio = fuel L/lap / (Energy fraction/lap × 100), in **L / 1% VE**.
- Recommended ratio rounds upward to 0.01; no hidden percentage safety factor.
- VE target = ceil(required fraction × 100), at most 100%. A requirement above
  100% explicitly reports ADDITIONAL STOP REQUIRED.
- Resulting fuel carried = VE target percent × recommended ratio. For example,
  51 × 0.86 = 43.86 L. Above tank capacity is explicitly infeasible, not silently clamped.
- Expected added fuel = max(0, resulting target − projected fuel at pit), secondary
  information only. It is not the LMU pit-menu input.

Only binary floating-point noise within 1e-10 of a setting increment boundary is
removed before upward rounding. Unknown tank capacity is labelled unverified.
The UI never claims a known current in-game Fuel Ratio or writes settings to LMU.

## Local Engineer controls and validation

Plans use `stintara:strategy:v1:<session UUID>:<participant UUID>` in localStorage.
Apply validates integer input; AUTO removes the override. Focus changes restore
the correct local plan. The server persists nothing from these GET parameters.
Existing refresh scheduling and late-response guards are reused; no new polling
loop is introduced. Diagnostics stay collapsed by default.

`node tools/engineer_demo.cjs --json` supplies a wholly synthetic 40-car fixture.
With optional local Playwright, `node tools/engineer_demo.cjs --screenshot --browser msedge`
renders the actual template at 1920×1080 and 900×900 into ignored
`build/engineer-resource-strategy-1920.png` and `build/engineer-resource-strategy-900.png`.
The fixture mocks API responses; it is not an end-to-end ingestion replay.

Remaining uncertainties: real LMU VE-history timing and capability signals require
field validation; current in-game Fuel Ratio is unavailable through this WebSocket
path; timed finish is a conservative estimate without within-lap progress; other
simulator mappings remain future work. Tyre/weather/traffic strategy, automation,
Race Control, full ElectronicsUpdate and multi-stint optimization remain outside scope.
