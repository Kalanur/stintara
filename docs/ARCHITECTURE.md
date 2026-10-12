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

The publishing target is `latest` on the default branch, semantic-version tags and a SHA tag, using `GITHUB_TOKEN` with `packages: write`.

## V0.1 scope and current limits

V0.1 targets a small end-to-end LMU prototype: live-driver listing, a driver live page with standings and strategy data, persisted lap snapshots and strategy samples.

The local prototype already contains server, agent, schema, live pages, Docker configuration, Unraid templates and a GHCR workflow. This records source availability, not a successful deployment or end-to-end validation.

Known limits of that implementation:

- Official RaceControl event/race/split matching is modeled but not implemented. Cross-agent convergence remains a requirement, not a completed feature.
- Reconnect matching reuses a still-active observation for the same authenticated agent, simulator, track and session type, provided the session clock has not moved backwards by more than 2 seconds. These are conservative local heuristics, not official race identity. New same-track/type sessions without an observed clock reset or disconnect remain ambiguous.
- Explicit game disconnects end the agent observation. Track/type changes or session clock resets start a separate session. Timeout cleanup also works after a server restart. Other active observations keep a shared session open. A trusted internal association function supports multiple agents; automatic cross-agent matching is deferred.
- Browser pages poll REST endpoints; browser WebSocket fan-out is deferred.
- The schema is a prototype without Alembic-managed migrations. Startup applies additive schema changes and transactionally converts legacy numeric strategy-sample IDs to UUIDv7. Back up an existing database first; no telemetry rows are intentionally discarded.
- Automated unit, PostgreSQL integration, simulated WebSocket end-to-end and frontend rendering tests are present. See [VALIDATION.md](VALIDATION.md) for verified results and remaining deployment checks. Identity/session matching changes require tests under [AGENTS.md](../AGENTS.md).

Deferred work includes full RaceControl integration, iRacing/ACC/RaceRoom adapters, Results XML reconciliation, historical views, accounts/login, team features, rivals, remote engineer controls, OBS widgets, AI analysis and raw telemetry storage. Redis and microservices are not part of V0.1.

## Adapter protocol reference

The LMU adapter was checked against [Swizzjack/lmu-pitwall, commit a171b80](https://github.com/Swizzjack/lmu-pitwall/tree/a171b80a7c8c1e02a9d029e32fd717c834d26e71), particularly `bridge/src/protocol/messages.rs`, `main.rs`, `lap_tracker.rs` and `websocket/server.rs`.

The bridge supports `?format=json`, sends an initial lap snapshot before regular scoring, forwards native `mWear` unchanged and supplies cumulative S1+S2 for sector 2. The agent normalizes tyre naming, individual sector times, missing telemetry and non-finite values before forwarding. The server validates a provider-neutral transport contract. No Pitwall code is modified or imported into Stintara.

## Maintaining these decisions

Update this document when an architectural decision changes. Keep intended behavior and verified implementation status distinct. For development rules, read [AGENTS.md](../AGENTS.md).
