# Stintara architecture

## Status and purpose

This document records the agreed architecture for Stintara, a simulator-independent live-timing, telemetry and race-intelligence platform. These decisions guide development; they do not imply that every capability is already implemented. See the V0.1 limits below and [README.md](../README.md) for current operation and setup.

## Providers and boundaries

- Initial simulator: Le Mans Ultimate (LMU), with RaceControl as the intended official identity and race source where stable identifiers are available. Full RaceControl integration is planned.
- Planned providers: iRacing, Assetto Corsa Competizione (ACC), RaceRoom and later other simulators.
- Use a modular monolith. The server and PostgreSQL are initially the only central containers.
- Keep the backend domain model simulator-independent. Provider-specific protocols and fields belong in adapters.
- LMU Pitwall is an initial data source, not a dependency of the domain model. It must work unmodified.
- The local Stintara Agent is replaceable and handles local ingestion and transport, not domain matching or database logic.
- Introduce microservices only when a measured need justifies them.

## Driver identity

- All internal persistent entity IDs use UUIDv7.
- A stable external identity is the pair `(provider, external_id)`, enforced by `UNIQUE(provider, external_id)` in `driver_identities`.
- A driver may own multiple provider identities. Simulator identifiers and identity-provider namespaces must not be conflated: LMU data does not by itself establish a RaceControl identity.
- Display names and handles are presentation data, never identities. Never merge drivers because names are equal or similar.
- `drivers` holds the global internal driver identity; `driver_identities` maps stable provider identities to it.
- `session_participants` describes a participation, including an optional `driver_id`, provider identity if known, local vehicle/session identifier, historical display name, team and vehicle.
- Unresolved participants are valid. Leave `driver_id` unset when a stable association is unavailable; do not invent a global identity from a name or local vehicle ID.
- Preserve the display name at the time of participation even if a driver's current name changes.

## Event, race, session and observation

Keep four concepts separate:

| Concept | Meaning |
| --- | --- |
| Event | The scheduled or logical event, potentially containing multiple splits. |
| Race | One concrete race/split. Each split is a separate race. |
| Session | A practice, qualifying or race session associated with that race where known. |
| Observation | One agent's data stream contributing to a session. |

Prefer official provider event, race and session IDs, scoped by provider. An event ID alone must not collapse separate splits into one race.

Observations from multiple agents identified as belonging to the same official race/session must converge on the same central objects. A new connection is not automatically a new race. Track, time, names or local vehicle IDs alone do not establish shared official race identity. Private/offline sessions do not require perfect global external identity.

Short connection interruptions should preserve the session. Race completion or a sufficiently long disconnect should close it and remove the driver from live listings. V0.1 hides a driver after 15 seconds without scoring from their own observation. It ends stale observations after 120 seconds (configurable via `SESSION_TIMEOUT_SECONDS`, minimum 15); cleanup runs every 5 seconds. A session closes only when no active observations remain.

## LMU telemetry pipeline

```text
LMU -> unmodified LMU Pitwall Bridge -> local Stintara Agent
    -> Stintara Server -> PostgreSQL -> live website
```

The agent connects to the localhost Pitwall WebSocket, initially `ws://127.0.0.1:9000` (the current agent requests `?format=json`). It establishes an outbound connection to the Stintara Server. No incoming port forwarding to the gaming PC is required.

- `ConnectionStatus`: track game connectivity, telemetry validity and version information.
- `SessionInfo`: track, track length, session duration/laps and available weather/temperature context.
- `ScoringUpdate`: live standings at approximately 2-5 Hz; not every update needs permanent storage.
- `AllDriversUpdate`: persist completed-lap snapshots, including timing and available strategy/vehicle data.
- `TelemetryUpdate`: initially persist strategy telemetry for the connected Stintara driver at 1 Hz, including fuel, consumption, estimated remaining laps, speed, RPM, gear, engine temperatures and available tyre data.

LMU `mWear` means tyre remaining: `1.0 = new`, `0.0 = destroyed`. Use names such as `tyre_remaining_fl`, `tyre_remaining_fr`, `tyre_remaining_rl` and `tyre_remaining_rr`. Any derived wear-used value must be explicitly labelled.

## Persistence

PostgreSQL is the central structured database. Core models are `drivers`, `driver_identities`, `events`, `races`, `sessions`, `session_participants`, `laps`, `strategy_samples`, agents and their observations. Use foreign keys, uniqueness constraints and indexes consistent with the identity and session rules.

V0.1 persists completed laps and 1 Hz strategy samples. The server enforces sampling, including after reconnects. Current scoring is held in memory at 2-5 Hz and checkpointed at most once per second; it is not stored as a scoring history. Run one server worker for V0.1. Lap snapshot inserts are idempotent and do not overwrite live standings. Wheel temperatures (inner/mid/outer), pressures, brake temperatures and available lap damage are retained. High-frequency raw telemetry is deferred; later storage may use Parquet/Zstandard separately from the structured database.

## Deployment

The primary target is the Unraid host.

| Component | Decision |
| --- | --- |
| Stintara server | Central `stintara-server` role; GHCR image `ghcr.io/kalanur/stintara:latest`. The existing Unraid template names the container `stintara`. |
| Server port | `8080:8080`. |
| Server appdata | `/mnt/user/appdata/stintara/server` mounted at `/data`. |
| Database | PostgreSQL 17; templates use the official `postgres:17` image. |
| Database appdata | `/mnt/user/appdata/stintara/postgres` mounted at `/var/lib/postgresql/data`. |
| Network | Custom Docker network `stintara`, shared by server and PostgreSQL. |
| Database exposure | Do not publish PostgreSQL port 5432 publicly or to the LAN. |
| Distribution | GHCR, built for `linux/amd64` through GitHub Actions. |

The standard application network contains only the Stintara server and PostgreSQL. PostgreSQL remains isolated on `stintara`, without a published host port. "Internal application network" describes this boundary, not a requirement to use Docker's `internal: true` setting.

The application server may additionally join an existing operator-managed `<reverse-proxy-network>`. The proxy and database should not share a network unless a specific technical need requires a reviewed exception. The standard templates and Compose configuration do not attach the proxy to `stintara` or PostgreSQL to the proxy network. Proxy infrastructure belongs to deployment/operator configuration, never to the domain model. Stintara neither creates the optional network nor requires a proxy to run.

Keep the default host mapping `8080:8080` for V0.1 diagnostics and standalone access. Operators may remove that mapping in a verified proxy-only installation. Optional network attachment is an operator step; do not embed a non-idempotent connect command in public Unraid Post Arguments. Private names, addresses, credentials and installation-specific paths belong outside the public repository; `deploy/local/` is ignored for optional local notes/overrides.

Maintain `unraid/stintara.xml` and `unraid/stintara-postgres.xml`. Docker Compose supports local development; preserve Unraid compatibility when changing deployment.

Support `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER` and `POSTGRES_PASSWORD`, with an optional `DATABASE_URL` override. Agent authentication uses `BOOTSTRAP_AGENT_TOKEN`; the deployment timezone is `Europe/Berlin`. Never commit real passwords or tokens, and do not ship usable default secrets.

Container promotion follows feature branch -> PR to `dev` -> CI -> `:dev` -> operator integration test -> PR to `main` -> CI -> `:latest`. Publication uses `GITHUB_TOKEN` with `packages: write` after the complete test job succeeds. Only `dev` can publish `:dev`; only `main`, while it is the default branch, can publish `:latest`. Version tags publish SemVer/SHA references without moving either channel. Pull requests build without publication; feature pushes only test. Manual dispatch follows the same ref restrictions.

Development and stable/production deployments must use separate persistent databases and application data. Test migrations in Dev before promotion. The standard Unraid template remains on `:latest`; an operator can duplicate it locally for `:dev` with isolated data. These are workflow/deployment rules, not confirmation that a Dev branch or deployed integration environment exists.

## V0.1 scope and current limits

V0.1 targets a small end-to-end LMU prototype: live-driver listing, a driver live page with standings and strategy data, persisted lap snapshots and strategy samples.

The local prototype already contains server, agent, schema, live pages, Docker configuration, Unraid templates and a GHCR workflow. The operator subsequently reported a successful baseline LMU-to-live-UI smoke test on a container host; see VALIDATION.md. That result does not validate later revisions automatically.

Known limits of that implementation:

- Official RaceControl event/race/split matching is modeled but not implemented. Conservative LMU cross-agent convergence is implemented below, pending real implementation-specific field validation.
- Reconnect matching reuses a still-active observation for the same authenticated agent, simulator, track and session type, provided the session clock has not moved backwards by more than 2 seconds. These are conservative local heuristics, not official race identity. New same-track/type sessions without an observed clock reset or disconnect remain ambiguous.
- Explicit game disconnects end the agent observation. Track/type changes or session clock resets start a separate session. Timeout cleanup also works after a server restart. Other active observations keep a shared session open. A trusted internal association function supports multiple agents; the bounded LMU convergence matcher below can now select that association.
- Browser pages poll REST endpoints; browser WebSocket fan-out is deferred.
- The schema is a prototype without Alembic-managed migrations. Startup applies additive schema changes and transactionally converts legacy numeric strategy-sample IDs to UUIDv7. Back up an existing database first; no telemetry rows are intentionally discarded.
- Automated unit, PostgreSQL integration, simulated WebSocket end-to-end and frontend rendering tests are present. See [VALIDATION.md](VALIDATION.md) for verified results and remaining deployment checks. Identity/session matching changes require tests under [AGENTS.md](../AGENTS.md).

Deferred work includes full RaceControl integration, iRacing/ACC/RaceRoom adapters, Results XML reconciliation, historical views, accounts/login, team features, rivals, remote engineer controls, OBS widgets, AI analysis and raw telemetry storage. Redis and microservices are not part of V0.1.

## Adapter protocol reference

The LMU adapter was checked against [Swizzjack/lmu-pitwall, commit a171b80](https://github.com/Swizzjack/lmu-pitwall/tree/a171b80a7c8c1e02a9d029e32fd717c834d26e71), particularly `bridge/src/protocol/messages.rs`, `main.rs`, `lap_tracker.rs` and `websocket/server.rs`.

The bridge supports `?format=json`, sends an initial lap snapshot before regular scoring, forwards native `mWear` unchanged and supplies cumulative S1+S2 for sector 2. The agent normalizes tyre naming, individual sector times, missing telemetry and non-finite values before forwarding. The server validates a provider-neutral transport contract. No Pitwall code is modified or imported into Stintara.

## Agent distribution and operator provisioning

V0.1 tester distribution uses the existing Python agent packaged as an unsigned Windows Onefile console executable. Startup resolves immutable config from environment overrides, then private JSON beside the executable, with a default only for local Pitwall. No secrets or production endpoints are embedded in the public artifact. Explicit config paths are supported; the PyInstaller extraction directory is never the operator-config location.

Operator provisioning is an internal `python -m app.provision_agent` CLI, not an HTTP API. A transaction reuses an explicitly supplied driver slug or creates a UUIDv7 driver, and creates a uniquely named UUIDv7 agent with a fresh 32-byte random token. Only SHA-256 is persisted; plaintext is emitted once on successful CLI completion. Duplicate agent names reject the transaction without implicit rotation. Display names do not establish identity. Each tester receives a distinct agent/token; bootstrap credentials are not shared. Existing database settings and password-file semantics apply, without schema changes. Existing duplicate-connection protection, simulator boundaries and session association rules remain unchanged. Packaging and provisioning enable field-data collection, not automatic cross-agent matching or race/split convergence.

## Maintaining these decisions

Update this document when an architectural decision changes. Keep intended behavior and verified implementation status distinct. For development rules, read [AGENTS.md](../AGENTS.md).


## Real-world V0.1 hardening

After authentication and envelope validation, invalid scoring/telemetry/lap snapshots are discarded before domain handling. A connection-local rolling budget closes a client after ten validation failures in 60 seconds. Protocol/authentication errors remain fatal. Validation logs contain only internal agent ID, known message type, error class and bounded field/type summaries; no input values, tokens or raw payloads. The operator's repeat full online race on 2026-10-16 verified the reconnect fix in that test window (see VALIDATION.md); it does not retrospectively establish the cause of every earlier disconnect.

Provider sentinel normalization belongs in the Pitwall adapter, before transport and persistence. Temperatures <= -200 Celsius become null; nonpositive wheel pressure becomes null only with unavailable surface temperatures and no explicit flat/detached flags. Real temperature or damage evidence preserves zero pressure. Tyre remaining semantics stay unchanged despite an inconsistent external wheel-snapshot comment.

Vehicle identity currently uses an additive API projection: legacy persisted `car_name` maps to `entry_name`, `car_class` to `vehicle_class`, while `vehicle_model` and `manufacturer` remain null. Team and car-number fields stay separate. No database schema or stored history is rewritten; future trustworthy model/manufacturer data will require an explicit persistence change.

Source investigation used [Pitwall a171b80](https://github.com/Swizzjack/lmu-pitwall/tree/a171b80a7c8c1e02a9d029e32fd717c834d26e71): `bridge/src/main.rs`, `bridge/src/protocol/messages.rs`, and `dashboard/src/components/widgets/Standings.tsx`. Scoring `car_name` comes from `mVehicleName`, team is empty, and `car_number` comes from the vehicle slot ID, not a proven race number. Lap snapshots expose `vehicle_name`/`class_name`; these must not be treated as evidence of a real model/manufacturer. The operator observed entry/livery strings, contradicting the protocol comment's model claim. Live TireData lacks flat/detached flags, while wheel snapshots have them.

Scoring forwards `mPlace`, `mTimeBehindLeader` and `mLapsBehindLeader`. Pitwall's own UI derives class positions and uses best-lap differences for practice/qualifying, with configurable reference selection. This establishes a difference from Stintara's raw display, but does not explain repeated raw positions without captured scoring. This fix preserves position/gap values rather than choosing an unverified ranking/reference policy.

## Live vehicle identity investigation (2026-10-16)

Source inspection pinned the upstream default branch to [a171b80a7c8c1e02a9d029e32fd717c834d26e71](https://github.com/Swizzjack/lmu-pitwall/tree/a171b80a7c8c1e02a9d029e32fd717c834d26e71). This is a source-level finding for that revision, not proof about every future Pitwall release or every LMU session.

| Input | Verified origin and current Stintara path |
| --- | --- |
| ScoringUpdate `car_name` | `main.rs` copies scoring `mVehicleName`. The adapter and `Vehicle` contract retain it; participant upserts persist legacy `car_name`; the API projects `entry_name`. Operator-observed entry/livery names do not establish a model despite the upstream protocol comment. |
| ScoringUpdate `vehicle_class` | Scoring `mVehicleClass`, retained through the adapter/contract, stored as `car_class`, projected as `vehicle_class`. |
| ScoringUpdate `team_name` | Upstream explicitly constructs an empty string. The contract/storage can carry a team, but this live source does not supply reliable team data. No team is inferred from the entry. |
| ScoringUpdate `car_number` | Upstream copies `mID`, the local slot ID, not a verified visible race number. The adapter now sets `car_number` to null and preserves `id` for local association. The participant API also suppresses legacy LMU `car_number` values; stored history is not rewritten. `local_vehicle_id` remains available under its correct name. |
| AllDriversUpdate `vehicle_name` / `class_name` | `lap_tracker.rs` copies the same scoring `mVehicleName` / `mVehicleClass`. The adapter does not map these aliases; the `Lap` contract ignores these extra fields. Lap persistence uses local `id` to associate timing/strategy snapshots with participants already established by scoring; it does not overwrite their vehicle metadata. This is not an independent model source. |

### Available interfaces versus internal candidates

- [Live construction](https://github.com/Swizzjack/lmu-pitwall/blob/a171b80a7c8c1e02a9d029e32fd717c834d26e71/bridge/src/main.rs), [lap snapshots](https://github.com/Swizzjack/lmu-pitwall/blob/a171b80a7c8c1e02a9d029e32fd717c834d26e71/bridge/src/lap_tracker.rs) and [WebSocket contracts](https://github.com/Swizzjack/lmu-pitwall/blob/a171b80a7c8c1e02a9d029e32fd717c834d26e71/bridge/src/protocol/messages.rs) expose no separate reliable live model/manufacturer. The reference [Standings UI](https://github.com/Swizzjack/lmu-pitwall/blob/a171b80a7c8c1e02a9d029e32fd717c834d26e71/dashboard/src/components/widgets/Standings.tsx) displays `car_name` and prefixes `car_number` with `#`; presentation is not evidence of those semantics.
- [Shared-memory types](https://github.com/Swizzjack/lmu-pitwall/blob/a171b80a7c8c1e02a9d029e32fd717c834d26e71/bridge/src/shared_memory/types.rs) do define telemetry `mVehicleModel` (30 bytes), alongside `mVehicleName` and class/championship enums. This is a concrete internal candidate, not a field Stintara can currently consume: it is not read into outgoing messages anywhere in the inspected bridge. Its population, truncation and availability across the field would need real verification and upstream exposure. Do not claim that LMU has no model field at all. No separate manufacturer field was found in the inspected live structures.
- [Pitwall HTTP](https://github.com/Swizzjack/lmu-pitwall/blob/a171b80a7c8c1e02a9d029e32fd717c834d26e71/bridge/src/http_server.rs) serves assets, WebSocket upgrades, network/version information and process configuration/control. It has no vehicle identity endpoint. The implemented [LMU REST polling](https://github.com/Swizzjack/lmu-pitwall/blob/a171b80a7c8c1e02a9d029e32fd717c834d26e71/bridge/src/garage_api.rs) covers strategy usage, repair/refuel wearables and weather; none exposes a model through Pitwall. `rest_api/client.rs` is only a stub. This is not a claim that every undocumented LMU REST endpoint has been exhaustively tested.
- Post-race WebSocket session-detail responses and fuel-calculator options can contain model-like `car_type` values already in Pitwall's process/database. They originate from imported results, not a verified mapping to the current live participants. Stintara does not request/import them.

Decision: keep `vehicle_model` and `manufacturer` null. No substring, team/livery or fixed-table guesses. Standings show a separate semantic label and value, with Model -> Manufacturer -> Entry -> Class -> unknown priority; legacy API aliases retain Entry/Class semantics. Blank values are skipped and all displayed values are escaped. No GT3/spec-class presentation policy is enabled without reliable structured models. No schema change, migration, Pitwall modification or direct shared-memory reader is introduced.

### Future post-race enrichment (not implemented)

The [XML parser](https://github.com/Swizzjack/lmu-pitwall/blob/a171b80a7c8c1e02a9d029e32fd717c834d26e71/bridge/src/post_race/xml_parser.rs) reads separate `CarType`, `CarClass`, `CarNumber` and `TeamName` values; the [post-race database](https://github.com/Swizzjack/lmu-pitwall/blob/a171b80a7c8c1e02a9d029e32fd717c834d26e71/bridge/src/post_race/database.rs) stores them as `car_type`, `car_class`, `car_number`, `team_name`. The reference's `Porsche 963 LMDh` example illustrates model semantics. `CarType` appears suitable for provenance-preserving archive model enrichment after a trustworthy participant match. Manufacturer is not a separate parsed field and must remain unknown without another structured source; do not split strings to infer it.

No stable provider external driver ID or live-slot-to-result key is exposed by `ParsedDriver`. Pitwall SQLite driver/session IDs are local import IDs, not Stintara UUIDs or provider identities. Event `driver_id_xml` is not a demonstrated live-slot mapping. Prefer a future verified `(provider, external_id)` association plus an explicit race/session link. A real car number, class, lap timeline and session metadata can support reconciliation only after their scope and uniqueness are established; the current live `mID` must never be equated with XML `CarNumber`. Names are display data, never matching keys. Online `isPlayer` marks humans and cannot uniquely identify the operator.

Outstanding ambiguities include duplicate/missing car numbers, repeated sessions/splits at the same track, driver swaps, slot reuse, incomplete or late XML, and independent observer/import IDs. Unresolved matches must stay unresolved; manual confirmation or new structured keys may be needed. No result import, automatic matching or archive enrichment is implemented in this branch.


## Session spectator projection

The home page and `/api/sessions/live` select central sessions directly, once per UUID, using the existing live status plus an active observation seen within 15 seconds. `/api/sessions/{session_id}/live` reads all persisted participants without requiring a global driver identity; its HTML view polls every 500 ms. Existing identity, observation matching, ingestion and database schema are unchanged.

A pure projection overlays timing/position/pit fields from the newest fresh runtime observation of that exact session. It never overlays another session or combines observations by names. The existing session-local vehicle association is retained; reconciliation of differing observer-local IDs is not solved here. Stored participants absent from the newest packet remain visible as part of the recorded field; this is not a claim that every recorded car is still connected. Historical names are preserved. Tied source positions remain tied, with participant UUID as deterministic ordering tie-breaker.

Session `live` describes feed freshness only. Ended or stale sessions retain their last database checkpoint on the direct page, with an explicit stale label; they disappear from the live index. Checkpoint timestamps are not timestamps of individual overlaid packets. The API applies the conservative race timing projection described below without rewriting raw stored values. It uses the existing vehicle identity projection and LMU race-number suppression. No finish state, class ranking, best-lap validity or automatic split convergence is inferred.


### Participant-focused spectator widgets

Focus is client-side state keyed only by `session_participants.id` within the current API response. The optional `focus` query parameter must be a UUID belonging to that response; unknown/invalid values never fall back to names. Only an absent parameter permits initial auto-selection of a unique participant with a driver slug. Manual selection updates history without reload. Subsequent polls preserve membership by UUID or clear the focus and URL parameter when absent; they do not choose a replacement.

Pure JavaScript selection helpers reuse the deterministic API order for a window of at most three entries on either side of focus. Head-to-Head uses unique exact adjacent positive integer provider positions, independently of array order. Neither widget derives physical order; H2H can display the validated race interval relationship described below. The full field and focus cards highlight by UUID. The existing driver API adds `participants[].participant_id` directly from the persisted participant primary key, allowing the driver page to link a uniquely associated own participant. No identity association, session lifecycle, schema, migration or provider normalization changes are involved.


### Fuel validation foundation

`app/fuel.py` contains pure Python checkpoint selection and median calculation. It consumes existing persisted lap checkpoints only, not provider fuel averages/range, session length or leader progress. Consecutive, finite nonnegative checkpoints with unchanged pitstop counters and neither endpoint in pits yield a positive consumption sample. Deterministic exclusion priority is non-consecutive laps, invalid fuel, pitstop transition, pit context, then non-decreasing fuel. No outlier filter or stint reset is applied. The last five valid samples remain available across excluded transitions, with three required for the estimate. Diagnostics are capped at twelve transitions; selected history may be older than that diagnostic window. Before three samples, `sample_count` is zero, `available_sample_count` reports collecting-window availability and `used_for_estimate` remains false.

The new UUID-scoped fuel endpoint verifies participant/session membership and reads narrow lap fields plus the latest strategy row inside a read-only repeatable-read transaction. Python calculates the report against the database clock. Current fuel is never taken from a lap checkpoint. Freshness is 0–15 seconds inclusive; range requires fresh valid fuel and an available positive consumption estimate. Non-finite outputs become null; no mathematical rounding or reserve is applied. Provider comparison fields are projected separately after estimation and cannot change it. No persistence, schema, protocol, agent or dependency changes are required.

The spectator keeps fuel requests scoped to its participant focus UUID. A monotonically increasing request token rejects late responses, including A→B→A switches. No focus means no fuel request. Selection changes clear the prior participant's display immediately. Fuel errors remain separate from standings; diagnostics expansion survives polling. This validation feature does not establish exact start/finish-line checkpoint timing, inlap/outlap behavior or pit counter timing, and does not implement finish-distance, reserve or strategy recommendations. Issues #9/#10 and observer matching remain unchanged.


### Replay and scenario harness V0.1

`tools/replay` is separate developer/operator tooling, excluded from agent packaging and production services. A second Pitwall WebSocket client records parsed messages with relative monotonic time into versioned JSONL. The wire replay server defaults to literal loopback and restarts the stream per connection; 1× is required for fidelity-critical normal-agent tests. The offline compiler reuses the production adapter and applies existing scoring (0.2 s) / telemetry (1.0 s) throttle decisions against recorded time. Compiled replay authenticates through the unchanged agent protocol and can accelerate sending without changing those logical decisions. The server still persists using actual arrival/database time, so accelerated replay is not persistence-equivalent.

Raw, sanitized-real and compiled real-derived captures remain private under ignored `data/`. Sanitization uses in-memory per-file sequential labels for known identity presentation fields; unknown text remains private and an audit emits only fixed/masked paths and counts. The header requires public review for sanitized-real sources. Only wholly synthetic fixtures are permitted in Git, enforced by hygiene tests. No domain identity matching, session lifecycle, schema, production agent behavior or payload contract changes are involved. See [REPLAY.md](REPLAY.md) for safe operation and outstanding real-provider validation.


### Spectator Timing Pass 1

The Pit / Finish State presentation follow-up uses the existing participant and lap columns. After existing AllDrivers session/type/clock validation, participant `pitstops` is updated atomically with `GREATEST`, while `finish_status` and `has_telemetry` take the last accepted snapshot values. This occurs before the valid-completed-lap guard, so duplicate or uninsertable lap snapshots can still carry current state. It does not update scoring position, pit occupancy or freshness timestamps, and does not change session lifecycle. Existing non-null zero defaults are retained; unknown numeric finish codes stay raw and have no guessed FIN/DNF/DQ label. No migration or protocol changes are involved.

The session participant query exposes these fields and adds one lateral aggregate over the participant's indexed lap history. `last_pit_lap` is the greatest completed lap number whose nonnegative counter exceeds the maximum earlier persisted nonnegative counter. The first known count has no earlier baseline and cannot prove a stop lap. A running maximum prevents cached counter dips/recoveries from inventing later stops. Skipped laps remain skipped; the result is first observable counter increase, not exact pit entry/exit. There are still two API database round trips regardless of field size, with no per-participant API requests; history scan cost grows with persisted laps. UI state is separate from Gap/Interval, terminal provider codes take precedence over PIT, and no state is inferred from feed age. This does not solve the remaining post-race strategy/lifecycle work in #9 or complete #22.

`app/timing.py` contains pure gap selection, adjacent-position interval and session-context helpers. The session projection overlays provider gaps and lap deficits from the same newest fresh observation used for positions/lap counts, and clears old timing for participants absent from that packet. No cross-observation timing assembly or persistence changes occur. Race time gaps must be finite/nonnegative; positive lap deficits take presentation precedence. Invalid position/deficit or contradictory leader timing remains unknown. Intervals require unique consecutive positions, equal known completed laps, explicit zero deficits and nondecreasing valid gaps. Each interval includes `interval_preceding_id`, allowing H2H to display its sign relative to focus by participant UUID without repeating arithmetic in JavaScript. Non-Race timing remains unknown.

The API adds a `timing` object with configured minutes/laps and fresh elapsed seconds. A positive configured duration suppresses configured lap-distance presentation when both exist. No native/derived remainder or current-lap number is supplied. Labels preserve the provider's completed-lap semantics. Fresh clocks require session feed freshness and a matching runtime observation aged 0–less-than-15 seconds; no stored-clock fallback or browser extrapolation is used.

The existing participant query adds one lateral latest-lap lookup per row within the same SQL statement, using the existing unique `(session_participant_id, lap_number)` index and descending limit-one lookup. API round trips remain two regardless of field size; no lap-history scan, new polling endpoint, cache invalidation scheme or schema migration is introduced. Sectors are finite positive values from that single latest lap; invalid/missing components remain null rather than combining laps. Sector validity here is numeric availability only. #10/#20/#22 are partially addressed, not completed; native remaining time, live/best sectors and provider lifecycle semantics remain deferred.

### Weather presentation and single-class fields

`app/weather.py` is a read-only presentation boundary: it decodes JSONB strings or dictionaries and maps the known LMU/Pitwall weather fields into a provider-neutral whitelist. Unknown providers produce unavailable values until an explicit unit mapping exists. Both live APIs expose the same `session.weather` object, replacing the Driver Live API's previous raw weather JSON. Current keys are `air_temp_c`, `track_temp_c`, `rain_intensity`, `cloudiness`, `wetness_avg/min/max` and `wind_speed_mps`. Forecast contains `timeline_complete` and `nodes`, each with `position_percent`, `temperature_c`, `rain_chance`, `humidity_percent`, `wind_speed_mps` and cardinal `wind_direction`. Invalid fields become null, not clamped. No sky code, dark cloud, observer-relative wind or arbitrary provider key is leaked.

The existing session row supplies the latest persisted SessionInfo snapshot. Queries, ingestion and raw storage are unchanged apart from selecting weather in the spectator query; there are no extra database round trips or polling loops. Session `live` governs current versus last-stored labelling, not independent weather freshness. No weather history or measurement timestamp is invented.

Pitwall's filtered forecast list lacks node IDs. Only exactly five source nodes, each with at least one valid understood measurement, establish 0/25/50/75/100 positions. Malformed, partial or oversized lists preserve valid nodes in source order with null positions. Missing points are never interpolated. Shared `static/weather.js` is included into both HTML templates by the server and formats only the normalized contract; Driver Live collapses forecast details.

The pure frontend `singleClass` helper checks the entire recorded field, trims strings and requires exact case-sensitive equality of every nonempty class. It controls header context and repeated class presentation in Full Field, Relative and H2H, without modifying API or stored data. Unknown classes remain unknown. This bounded work references #19/#21/#22; other spectator, provider and lifecycle work remains separate.


### Conservative same-LMU-session convergence, Pass 1 (#24)

The existing canonical `sessions` row and `UNIQUE(session_id, agent_id)` observations remain the model. No new table/migration or historical repair is introduced. Same-agent active/fresh observations and existing `same_session` metadata/reset rules take priority. An observed backwards jump over 2 seconds (persisted or current runtime clock) blocks cross-agent fallback for that resolution, including jumps inside the broader cross-agent jitter tolerance.

`app/convergence.py` contains pure provider-aware evidence rules, not persistent identity matching. LMU candidates require the same provider/track/type and live status. Use the freshest suitable other-agent active observation with finite clock and age in [0,15) seconds. Compare incoming clock against stored clock plus wall-clock age with a 5-second tolerance. A newer valid but incompatible clock cannot be bypassed for an older matching one. No started-at equality or event/split inference is used.

LMU evidence is an exact slot ID plus trimmed `(driver_name, team_name, car_name, vehicle_class)` tuple, using the corresponding persisted fields. Empty/missing optional strings normalize to empty. Comparable entries require a nonempty non-fallback driver name plus some team/entry/class context. Duplicate/invalid slots invalidate the roster. At least 5 shared matching slots, at least 80% overlap relative to the smaller comparable roster, and zero conflicting comparable slots are required. All thresholds are named and tested. Only one matching candidate permits association; none/multiple retain separation. No names become driver identities and no local slot is treated as globally stable identity.

Resolution and participant persistence now share one short database transaction. A deterministic signed 64-bit SHA-256-derived PostgreSQL transaction advisory lock covers the provider/track/type namespace before resolution and remains held until the initial roster is committed. Namespace collisions would only over-serialize, not cause matches. Same-agent locking remains; runtime session state changes only after commit. No external network calls occur in the transaction, and no new global application mutex is introduced. Existing single-worker ingestion serialization remains; concurrency tests bypass it and prove DB locking at the former resolve/upsert visibility gap. Session-row locking coordinates attach/end/expiry decisions, including simultaneous final disconnects.

Participant upserts retain `(session_id, local_vehicle_id)`, existing UUIDs, historical display names and the existing COALESCE guard on resolved driver IDs. Additional vehicles extend the union; each authenticated agent can fill its unresolved player row. Observer-specific scoring projections and 1 Hz persistence continue unchanged. The existing schema stores no per-observer roster history, so stale/changed participant metadata can conservatively prevent later convergence. Once attached, normal same-agent lifecycle resolution remains first; continuous split reclassification is not introduced.

Public aggregate evidence in #24 showed a compatible 45/49 same-split roster with 45 matching slots and disjoint 19/20 different-split fields despite close clocks. This supports provisional thresholds, not universal LMU slot stability. Real validation of this implementation remains required. False negatives are preferred to false merges. RaceControl identity, human-readable split numbers, local Results XML, same-agent transient fragment repair and historical backfill remain outside this pass.

### Bounded transition convergence retry (#24)

A newly resolving LMU observer still tries same-agent resolution first, then the unchanged strict cross-agent clock and roster rules. When strict matching fails but exactly one live candidate has a fresh suitable observation and satisfies the existing roster rules, publication may wait for up to `TRANSITION_GRACE_SECONDS = 10.0`. The bound covers the reported roughly six-second transition skew with a small margin; it does not increase the normal five-second clock tolerance. Freshness means observation age in [0,15) seconds, not equality of session start times.

Each incoming scoring frame retries under the same namespace advisory lock and session-row locks. Actual attachment still requires strict clock plus roster agreement. The runtime-only pending record contains the namespace, candidate UUID, original deadline origin and last incoming clock. Repeated mismatched frames never extend the window. While pending, this observer has no published session/scoring or persisted telemetry; existing observers remain available. Initial candidate selection never uses track/type/time alone or fuzzy names.

Expiry (including an aligned frame at or after ten seconds), a stale/closed/replaced candidate, conflicting or insufficient roster evidence, multiple plausible roster candidates, invalid clock, changed provider/track/type, or a further backwards reset causes conservative independent creation. During retry, even one clock-compatible candidate among multiple roster-compatible candidates is ambiguous. Successful attachment, independent creation and finalization clear pending state. Socket disconnect clears it without ending existing persisted observations. Runtime resolution changes become visible only after the database transaction commits, including rollback on participant persistence failure.

Restart/disconnect loses the pending deadline; the next connection resolves afresh with all normal safety checks and may start a new bounded wait or remain separate. There is no durable grace guarantee across restarts and no later merge of already-created sessions. No schema, protocol, agent, frontend or historical repair changes are introduced. Synthetic regression tests cover the delayed transition and safety boundaries; real Driver/Race Engineer transition validation remains necessary, so #24 remains open.
