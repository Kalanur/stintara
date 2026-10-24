# Stintara V0.1

Simulator-independent live timing and strategy telemetry, starting with LMU:

```text
LMU -> unmodified LMU Pitwall -> local Stintara Agent
    -> Stintara Server -> PostgreSQL 17 -> live website
```

Read [AGENTS.md](AGENTS.md) and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for binding development decisions. [docs/VALIDATION.md](docs/VALIDATION.md) records this baseline's checks and outstanding deployment validation.

## Current scope

- Outbound agent connection; no incoming gaming-PC port forwarding.
- Pitwall JSON WebSocket at `ws://127.0.0.1:9000?format=json`.
- Live scoring received at up to 5 Hz, with in-memory API updates and at most 1 Hz database checkpoints.
- Strategy samples persisted at most once per second per participant, enforced by the server.
- Completed-lap snapshots, fuel, tyre remaining, pressures, individual temperatures, brake temperatures and available lap damage.
- UUIDv7 entity IDs; external driver identities use `UNIQUE(provider, external_id)`.
- Display names never identify a driver. Unresolved field participants remain valid.
- Short reconnects reuse the same agent's session; long inactivity closes observations. RaceControl matching is not implemented.

## Windows test agent

Normal testers do not need Python, Git, a virtual environment or PowerShell:

1. Start LMU Pitwall locally; it must run separately from Stintara.
2. Download the `StintaraAgent-windows` artifact from the **Build Windows test agent** GitHub Actions run for the operator-approved revision and extract it.
3. Place the private `stintara-agent.json` supplied by the operator next to `StintaraAgent.exe`. The included `stintara-agent.example.json` is only a template and cannot authenticate.
4. Double-click `StintaraAgent.exe` or start it from a console. Expect `Connected to Stintara as ...` and `Connected to LMU Pitwall`. Ctrl+C stops the agent cleanly. If a window closes immediately, start it from a console to read the safe configuration error.
5. Keep the token/config private. Each tester needs their own agent and token; never share the bootstrap token. The same agent cannot connect twice simultaneously (`agent already connected`).

The tester folder needs only the EXE and their JSON config. The EXE is currently unsigned; Windows may show a SmartScreen warning. No installer, service, tray application or auto-updater is included.

Configuration is resolved at startup: `STINTARA_WS`, `PITWALL_WS`, `STINTARA_AGENT_TOKEN` environment values override JSON `server_ws`, `pitwall_ws`, `token`. Only Pitwall has a default: `ws://127.0.0.1:9000?format=json`. Server URL and a non-placeholder token are required. Use WSS/TLS for external testing; plain WS is for local development. The example uses the deliberately non-routable `wss://stintara.example.invalid/ws/ingest`.

The default config location is beside the EXE (not its temporary extraction directory), or beside the agent script for Python runs. `--config <path>` selects an explicit file, relative to the current directory if not absolute. An explicit missing file or any unreadable/malformed config fails safely; environment values do not bypass a broken config file. An absent default file is allowed for environment-only development, including `run-agent.ps1`. Empty environment overrides are rejected. `--help` and `--version` exit without reading configuration or connecting. Config files must be UTF-8 JSON objects. Never commit `stintara-agent.json`, which is Git-ignored.

### Developer Windows build

From a fresh Windows checkout with Python 3.13 available:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r agent/requirements-build.txt
.\agent\build-windows.ps1 -Python (Resolve-Path .\.venv\Scripts\python.exe)
.\dist\StintaraAgent.exe --help
```

The build script resolves source/output paths from its own location, so it also works from another working directory. It produces `dist/StintaraAgent.exe` using PyInstaller Onefile and copies only the public example config alongside it. Generated specifications and intermediate files stay under ignored `build/`; binaries stay under ignored `dist/`. No operator config is embedded. The bounded dependency versions describe a repeatable build procedure, not a guarantee of byte-identical binaries across dependency updates. CI uses Windows and Python 3.13, runs the help smoke test and uploads only these two distributable files as an Actions artifact; it does not create a release.

### Operator provisioning

After deploying this server revision and initializing its existing schema, run the internal CLI inside the server container. Example with generic values only:

```bash
docker exec stintara python -m app.provision_agent --driver-slug test-driver --driver-name "Test Driver" --agent-name tester-pc --server-ws wss://stintara.example.invalid/ws/ingest
```

Replace the URL privately with the operator's actual WSS ingress. On success stdout contains one complete JSON config with the new token exactly once; save it privately as `stintara-agent.json` and transfer it securely to that tester. Without `--server-ws`, stdout contains just the token and stderr explains where to put it. Do not run this command in CI, pipe its output into logs, or share terminal captures. There is no token retrieval or rotation command; a lost token requires a separate operator decision, not an automatic retry that overwrites an agent.

The CLI uses `DATABASE_URL` first, otherwise the existing `POSTGRES_*` settings with `POSTGRES_PASSWORD_FILE` taking priority over `POSTGRES_PASSWORD`. It needs database access only inside the server's network; do not expose PostgreSQL. It does not require a bootstrap agent token and does not create tables or run migrations.

Each successful transaction generates a fresh token from 32 cryptographically random bytes and stores only its SHA-256 hash. New driver/agent IDs are UUIDv7. An exact existing driver slug is reused and its display name can be updated explicitly; equal display names never merge different slugs. Another agent for that driver needs a distinct name. An existing agent name causes an error and rolls back the entire transaction without rotating its token or changing the driver. Avoid reusing the bootstrap agent name, whose credentials remain managed by bootstrap settings. The optional server URL is output only, never stored in PostgreSQL. No admin HTTP endpoint is added.

**Windows packaging/provisioning implementation ready for external multi-observer field testing.** This does not implement session/split convergence or establish that same-split or different-split multi-observer tests have passed.

## Local development and tests

Python 3.11+ is required; use Python 3.13 locally to match CI and the server image. Frontend tests require Node.js 22+.

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
python -m compileall -q server/app agent tools tests
ruff check server/app agent tools tests
python -m pytest -q
node --test tests/ui.test.cjs
```

Without `TEST_DATABASE_URL`, PostgreSQL-dependent tests are explicitly skipped. For the full suite, point it at a dedicated local PostgreSQL 17 test database:

```powershell
$env:TEST_DATABASE_URL = 'postgresql://TEST_USER:TEST_PASSWORD@127.0.0.1:5432/stintara_tests'
python -m pytest -q
```

Replace the placeholders with local test credentials. Tests create and remove their own temporary schemas; never use a production database for tests. The suite includes an actual WebSocket agent/server test using a simulated Pitwall source, not a live LMU session. CI runs the database suite with a dedicated PostgreSQL service.

## Local Docker Compose

Copy `.env.example` to `.env` and set your own random `POSTGRES_PASSWORD` and `BOOTSTRAP_AGENT_TOKEN`. Empty values prevent startup; example/default tokens are rejected. Keep `.env` out of Git.

```powershell
Copy-Item .env.example .env
# Fill in the password and token in .env.
docker compose config --quiet
docker compose up -d --build
curl.exe http://localhost:8080/health
```

Compose creates/uses Docker network `stintara`, runs `stintara-server` and `stintara-postgres`, and stores data under `./data/`. PostgreSQL has no published port. Run one application worker; the V0.1 live cache and ingestion serialization are process-local.

## Secret files for persistent containers

Environment-only configuration remains supported for simple development deployments: set `POSTGRES_PASSWORD` and `BOOTSTRAP_AGENT_TOKEN` to your own secrets. The supplied Compose file and standard Unraid template retain this existing mode.

For persistent container deployments, prefer read-only secret files. Configure the Stintara server with container paths:

```text
POSTGRES_PASSWORD_FILE=/run/secrets/postgres_password
BOOTSTRAP_AGENT_TOKEN_FILE=/run/secrets/agent_token
```

Create the files privately on the host, restrict their permissions, and mount each existing file read-only at the corresponding container path. In Unraid, replace the direct password/token variable entries with the `_FILE` entries and add file-path mappings with read-only access. The container's application user must be able to read them. Do not put secret contents in variable values, build arguments or commands that print them. No Docker Swarm is required; ordinary read-only bind mounts suffice.

For a private Compose configuration, replace the direct password/token environment entries with the `_FILE` entries and add read-only mounts. The supplied Compose file requires the direct variables through interpolation: simply adding `_FILE` in an override does not remove those requirements. Prepare a separate local configuration with those direct entries removed; do not satisfy them with dummy secrets.

Stintara gives a configured `_FILE` variable priority over its direct environment value. A broken file configuration fails startup instead of falling back. Leave unused `_FILE` variables entirely unset. Files are decoded as UTF-8; only trailing CR/LF characters are removed. Other whitespace, embedded newlines and characters are preserved. Empty, missing, unreadable, non-UTF-8 files and directories fail with diagnostics that omit contents and paths. Empty agent tokens and `change-`/`replace-` placeholders remain rejected. Environment-only agent tokens retain their existing whitespace trimming.

`DATABASE_URL` still overrides the separate PostgreSQL connection fields. When it is set, Stintara does not read or require `POSTGRES_PASSWORD_FILE`. Agent-token validation still applies. File secrets are read during startup; restart the application after an operator-managed secret change. File contents are not written back into the process environment.

The unchanged official `postgres:17` image already supports `POSTGRES_PASSWORD_FILE`; use its own implementation, not Stintara's helper. Unlike Stintara's file-priority rule, the official PostgreSQL entrypoint rejects simultaneous nonempty `POSTGRES_PASSWORD` and `POSTGRES_PASSWORD_FILE`. Remove the direct variable in file mode. Its initialization setting does not rotate a password in an existing database; preserve the current database password when changing only the delivery method. See the [official PostgreSQL entrypoint](https://github.com/docker-library/postgres/blob/master/docker-entrypoint.sh).

Never commit or log secret files. Local `secrets/` directories and `*.secret` files are ignored, but do not force-add them. Host administrators/root can still read mounted secrets; this reduces exposure in container environment configuration, not access by host root. Use matching database credentials for the server and PostgreSQL, and configure the agent with the matching token privately. No real secret files or host-specific paths are supplied by this repository.

## Standard installation on Unraid

The standard installation runs independently of a reverse proxy:

```text
stintara-server (Unraid template container name: stintara)
      |
   stintara (application network)
      |
stintara-postgres
```

Only the application server and PostgreSQL belong to the standard `stintara` network. PostgreSQL is accessed by the server at `stintara-postgres:5432`, with no published database host port. Here "internal" describes the application network's role; it does not require Docker's separate `--internal` network flag.


These are operator steps; creating or validating the repository does not deploy anything. No GHCR image is assumed to exist yet.

### 1. Make a server image available

For the first test without publishing to a registry, copy this repository to the Unraid host and build from its root:

```bash
docker build --platform linux/amd64 -f server/Dockerfile -t ghcr.io/kalanur/stintara:latest server
```

This tags a local image on the Unraid host; it does not push it. The alternative is a separately authorized GitHub/GHCR publication followed by pulling that image. Do not select an Unraid image-update/pull operation for a local-only image.

### 2. Create the network and install the templates

```bash
docker network inspect stintara >/dev/null 2>&1 || docker network create stintara
cp unraid/stintara-postgres.xml /boot/config/plugins/dockerMan/templates-user/my-stintara-postgres.xml
cp unraid/stintara.xml /boot/config/plugins/dockerMan/templates-user/my-stintara.xml
```

In Unraid, use **Docker -> Add Container** and choose the templates.

### 3. Start PostgreSQL first

| Setting | Value |
| --- | --- |
| Container | `stintara-postgres` |
| Image | `postgres:17` |
| Network | `stintara` |
| Host appdata | `/mnt/user/appdata/stintara/postgres` |
| Container data | `/var/lib/postgresql/data` |
| Database / user | `stintara` / `stintara` |
| Password | Your own random password |
| Timezone | `Europe/Berlin` |
| Published ports | None |

```bash
docker exec stintara-postgres pg_isready -U stintara -d stintara
```

### 4. Start the Stintara server

| Setting | Value |
| --- | --- |
| Container (Unraid template) | `stintara` |
| Image | `ghcr.io/kalanur/stintara:latest` |
| Network | `stintara` |
| Port | `8080:8080` |
| Host appdata | `/mnt/user/appdata/stintara/server` |
| Container data | `/data` |
| `POSTGRES_HOST` / `POSTGRES_PORT` | `stintara-postgres` / `5432` |
| `POSTGRES_DB` / `POSTGRES_USER` | `stintara` / `stintara` |
| `POSTGRES_PASSWORD` | Same password as PostgreSQL |
| `DATABASE_URL` | Leave empty unless explicitly overriding the connection fields |
| `BOOTSTRAP_DRIVER_NAME` / `BOOTSTRAP_DRIVER_SLUG` | `Stintara Driver` / `driver` |
| `BOOTSTRAP_AGENT_NAME` | `gaming-pc` |
| `BOOTSTRAP_AGENT_TOKEN` | Your own random token, distinct from the database password |
| `TZ` | `Europe/Berlin` |

For an existing installation, retain your previously configured `BOOTSTRAP_DRIVER_NAME`, `BOOTSTRAP_DRIVER_SLUG` and `BOOTSTRAP_AGENT_NAME` in private configuration. The generic defaults are for new installations; changing the slug/name keys can create a different bootstrap driver/agent.

The server retries database connection for up to about one minute. Slugs allow lowercase letters, digits and hyphens. `/data` remains reserved for later artifacts; structured telemetry is stored in PostgreSQL.

```bash
docker logs --tail 100 stintara
curl http://localhost:8080/health
```

Expect `{"status":"ok","version":"0.1.0"}`. Open `http://<unraid-host>:8080/` and `http://<unraid-host>:8080/drivers/driver/live`. Initially the page should show no active drivers.

### 5. Run the Windows agent

With Python 3.11+ on the gaming PC:

```powershell
cd agent
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
$env:STINTARA_WS = 'ws://<unraid-host>:8080/ws/ingest'
$env:STINTARA_AGENT_TOKEN = '<same random token configured on the server>'
.\run-agent.ps1
```

Replace the address and token placeholders. No script edit is needed. `PITWALL_WS` may override the local source address; the default requests JSON from the unchanged Pitwall bridge. Use `wss://` through a TLS endpoint for an Internet uplink; plain `ws://` is intended for the local test network.

### 6. Exercise the real LMU data path

1. Start Pitwall and join an LMU session.
2. After SessionInfo and ScoringUpdate arrive, the configured driver should appear under **LIVE NOW**.
3. Check standings, vehicle, fuel and tyre data on the driver page.
4. Complete a lap and verify the database checks below.
5. Interrupt the agent uplink briefly, reconnect, and confirm the session ID is unchanged.
6. A driver disappears from LIVE after 15 seconds without scoring from their observation. After 120 seconds, the observation expires and the session closes if no other observer is active. Cleanup runs every 5 seconds.
7. A game-disconnect message closes the agent observation immediately; a track/type change or session-clock reset starts a separate session.

`SESSION_TIMEOUT_SECONDS` may override the 120-second timeout (minimum 15). Explicit game disconnects intentionally finalize immediately; a transport-only interruption does not.

### 7. Check persistence

```bash
docker exec -it stintara-postgres psql -U stintara -d stintara
```

```sql
select id, session_type, track_name, status, started_at, ended_at
from sessions order by started_at desc;
select session_id, agent_id, last_seen_at, ended_at from session_observations;
select display_name, driver_id, position, total_laps from session_participants;
select lap_number, lap_time, sector1, sector2, sector3, damage from laps;
select recorded_at, fuel_l, tyre_remaining_fl, tyre_pressure_fl_kpa,
       tyre_temp_inner_fl_c, tyre_brake_temp_fl_c
from strategy_samples order by recorded_at desc limit 20;
```

## Optional reverse proxy integration

A reverse proxy is optional. It and the Stintara server must share an existing, operator-managed Docker network. Connect only the application server to that second network; keep PostgreSQL exclusively on `stintara`. Do not attach the reverse proxy to the database/application network.

```text
Reverse Proxy
      |
<reverse-proxy-network> (existing, operator-managed)
      |
stintara (application container; stintara-server in Compose)
      |
stintara (application network)
      |
stintara-postgres
```

The operator supplies and manages `<reverse-proxy-network>`; it is not a Stintara resource or a dependency of the standard templates or Compose file. Inspect the existing network and container memberships first:

```bash
docker network inspect '<reverse-proxy-network>'
docker inspect --format '{{json .NetworkSettings.Networks}}' stintara
```

If the running Unraid application container is not yet attached, connect it:

```bash
docker network connect '<reverse-proxy-network>' stintara
```

Replace the quoted placeholder with the actual existing network name. For Compose, use container `stintara-server` instead of `stintara` in these commands. Stintara does not create or manage the proxy network; the operator command only attaches the application container. Never connect `stintara-postgres` to it.

Example upstream for the Unraid template:

```text
Scheme: http
Hostname: stintara
Port: 8080
```

With Compose, use hostname `stintara-server`. TLS can terminate at any suitable reverse proxy. Enable WebSocket forwarding for `/ws/ingest`, and configure the remote agent with `wss://<your-domain>/ws/ingest`. No particular proxy product is required. See Docker's [network connect reference](https://docs.docker.com/reference/cli/docker/network/connect/) for connecting and inspecting container networks.

Treat this as an operator step, not an Unraid Post Arguments hook: an unconditional repeated `docker network connect` can fail when the endpoint already exists. A normal container restart retains its network membership; a container recreation during an update can require reattaching it. Inspect membership after updates and reconnect only if missing. The public template keeps `PostArgs` empty and does not suppress connection errors with `|| true`.

V0.1 keeps host port `8080:8080` for direct access, `/health` and troubleshooting. Once a proxy-only installation has been verified, the operator may remove the application host-port mapping from their local template/Compose configuration. The proxy continues to use container port 8080; PostgreSQL still has no host-port mapping.

Keep installation-specific notes and overrides outside the public repository. If an operator chooses to keep temporary local files under `deploy/local/`, that directory is Git-ignored; no private files are supplied or generated there. Never force-add local configuration. A private Compose override must declare an existing proxy network as external and attach only the application service.

Operator smoke test: verify the standard direct health endpoint first, then verify HTTPS and an agent WebSocket through the proxy. Inspect memberships to confirm that PostgreSQL belongs only to `stintara`, and repeat the membership check after recreating the application container. A real proxy integration has not been tested by this repository change.

## Existing prototype databases

Back up an existing database before starting this revision. Startup adds the missing observation, weather and tyre fields and converts legacy `strategy_samples.id` values from `bigserial` to newly generated UUIDv7 values inside a transaction. Sample contents and participant references are retained. Custom foreign keys to the old sample ID can block the conversion; failure rolls back rather than dropping dependent objects. This is a small V0.1 compatibility step, not a general migration framework.

## GitHub Actions and GHCR

The workflow runs Python checks, PostgreSQL tests, frontend tests and `docker compose config` before its Docker job. The image target is `ghcr.io/kalanur/stintara`; it builds for `linux/amd64` and uses `GITHUB_TOKEN` with `packages: write`.

### Image channels and promotion

| Image tag | Source and purpose |
| --- | --- |
| `:dev` | Development/integration image built exclusively from branch `dev`. |
| `:latest` | Stable image built exclusively from `main`, provided it is the repository default branch. |
| `:<semver>` | Versioned release images from valid SemVer `v*` tags; `v1.2.3` produces `1.2.3` and `1.2`. |
| `:sha-...` | Commit-specific build reference for traceability, emitted with each published image. |

SHA tags identify source revisions rather than moving channels. Registry tags can still be overwritten by rebuilding a revision; pin the image digest when immutable image bytes are required.

```text
feature branch -> PR to dev -> CI -> merge -> :dev
    -> operator/integration test -> PR dev to main -> CI -> merge -> :latest
```

The `dev` channel allows real container integration tests before promotion to `main`. Both branches run the complete CI suite; `main` never bypasses tests. Pull requests run tests and build without logging in or pushing. Feature-branch pushes run tests only. Publishing requires successful tests and explicit event/ref checks on both the Docker job and login/push steps.

Manual dispatch on `dev` or default-branch `main` may republish its own channel. Dispatch on other branches runs tests only. `v*` tag runs publish SHA tags and, for valid SemVer versions, version tags; they never create `dev` or `latest`. Automatic metadata-action `latest` generation is disabled. There is no channel override input. Concurrent runs of the same workflow/ref cancel older runs; different version tags use separate groups.

Development images must not use the same persistent database or application data as stable/production deployments. Use separate databases and appdata, and test migrations in Dev first. Never point `:dev` at the stable/production PostgreSQL database.

The standard Unraid template continues to use `ghcr.io/kalanur/stintara:latest`. For integration testing, the operator may duplicate it locally, change the image tag to `:dev`, and configure separate database/appdata resources. No additional public Dev template is needed.

This workflow change does not create `dev`, merge code or deploy an image. Publication begins only after an authorized push/dispatch on an eligible ref containing this workflow. After publication, check package visibility or configure registry authentication before pulling.

## Known limits

- No official RaceControl matching, automatic cross-agent race association or other simulator adapters yet. Multiple observations are supported through an internal trusted association function; there is no public endpoint to arbitrarily attach agents to sessions.
- Reconnect matching is local to an authenticated agent. Same-track/type restarts with no observed clock reset or disconnect remain ambiguous without official session IDs. Driver swaps and per-observer vehicle-ID reconciliation are deferred.
- A driver is associated through the authenticated agent and the simulator's player vehicle ID, never through a display name. No public account/login system is implemented.
- Session end is inferred from source disconnect, transition or inactivity; no official results finalization yet.
- The live pages poll REST. A single server worker is required for the current in-memory live cache.
- No raw high-frequency storage, Parquet, historical UI, Results XML reconciliation, Redis or microservices.
- The operator has completed an initial real LMU/container-host smoke test of the baseline. This hardening revision still requires a repeat real test; optional reverse-proxy integration remains unverified. See docs/VALIDATION.md for the distinction.

LMU `mWear` is stored as `tyre_remaining_*`: `1.0 = new`, `0.0 = consumed`. Sector times in persisted lap snapshots are individual sectors, not cumulative S1+S2.


## Real-world ingestion diagnostics

The agent reports Stintara disconnect codes, safe known close reasons and clean/abnormal/exception outcomes, then announces its reconnect delay. Unknown peer reason text is deliberately omitted. Normal Ctrl+C prints `Stintara agent stopped.` without a traceback. Connection/handshake failures report their exception class without raw exception text or credentials.

The server logs structured `invalid_ingest_message` events with internal agent ID, recognized message type, error class, up to five validation field/type summaries, and close code/reason when applicable. It never logs payloads, validation input/context or tokens. One invalid authenticated scoring, telemetry or lap-snapshot payload is discarded without closing the connection. Ten such failures within a rolling 60 seconds close it with code 1008; valid messages do not reset this budget. Invalid handshake/authentication, protocol, message type, JSON or payload envelope remains fatal. Investigate repeated warnings rather than treating ongoing rejected data as successful ingestion.

The adapter maps wheel/engine temperatures at or below -200 degrees Celsius to unknown. Nonpositive pressure is unknown when all surface temperatures are absent/sentinel and no explicit flat/detached flag is present. Zero pressure with real surface temperatures or damage flags is preserved. Live Pitwall TireData does not carry these flags, so plausible temperature evidence also prevents hiding a potential puncture. Unknown values render as an em dash. Tyre remaining values are unchanged.

Vehicle identity is exposed separately as `entry_name`, `vehicle_class`, `vehicle_model` and `manufacturer`, alongside existing `team_name` and `car_number`. Legacy `car_name`/`car_class` storage and API keys remain compatible; model and manufacturer are currently null. No schema migration is needed. The live standings use Model -> Manufacturer -> Entry -> Class -> unknown, with a semantic label beside each value. No team/livery-to-manufacturer guessing is performed.

Position and gap values remain provider values. Repeated positions are not deduplicated or renumbered. Practice/qualifying gaps can still be zero because the API forwards provider race-gap fields; class-relative/best-lap gap calculations are not introduced in this fix.


## Session spectator

The home page lists sessions with scoring received within the last 15 seconds. Open a session to watch its complete recorded participant field at `/sessions/{session_id}/live`; registered drivers also have links to their existing strategy pages. Separate session IDs remain separate cards, including sessions on the same track. Automatic multi-observer convergence is still deferred.

- `GET /api/sessions/live`: session ID, provider, track, type, start time and recorded participant count; each session appears once.
- `GET /api/sessions/{session_id}/live`: session metadata, feed freshness (`live`) and every recorded participant, including unresolved drivers. Unknown UUID returns 404; malformed UUID returns 422.
- Positions are provider positions, with no renumbering or derived class ranks. Race gaps and intervals use the conservative Timing Pass 1 rules below; unsupported cases remain an em dash. Missing model, manufacturer, team and race number are not inferred.
- A stopped feed retains the last stored field on its direct page, explicitly labelled as stale. Receiving scoring does not mean that the race is still running; FINISHED/DNF/DQ behavior remains Issue #9.
- Existing driver endpoints remain compatible. Full race Fuel Calculator and Trackmap are deferred; experimental fuel validation is described below.

The table includes position, historical participant name, class, typed vehicle/entry label, team, verified number where available, laps, last/best lap and pit indication. It scrolls horizontally on narrow screens. No live LMU/MAPC validation of this iteration has been performed.


### Participant focus, Relative and Head-to-Head

Choose any participant in the spectator selector, including an unresolved participant, or share `/sessions/<session-id>/live?focus=<participant-id>`. Selection uses only the internal session participant UUID. Without the parameter, exactly one registered Stintara participant may be selected automatically; otherwise choose explicitly. Invalid or foreign UUIDs leave focus unset. Manual changes replace the URL without reloading. Polling preserves the UUID; a missing participant clears focus and removes the parameter without selecting a replacement. The existing Driver-Live link supplies focus only when its own participant association is unique.

**Relative** is a relative standings window: focus plus up to three preceding and three following entries in the existing API order. Position ties stay tied. It does not show physical track order or verified time gaps.

**Head-to-Head** shows Ahead / Focus / Behind by provider position only. For a positive integer focus position, each neighbor must be the unique participant at exactly one position lower/higher. Missing, invalid or duplicate neighbor positions produce an unknown slot. Duplicate focus positions do not trigger alternative ranking. Signed H2H intervals use only the validated adjacent-position relationship described below; other cases remain unknown.

Real physical relative, unresolved gap edge cases (#10), finish semantics (#9), full race Fuel Calculator, Trackmap and cross-agent convergence remain unimplemented or unverified. These widgets have synthetic regression coverage, not LMU/MAPC validation.


### Fuel estimate · Validation foundation

The focused participant can be inspected through `GET /api/sessions/{session_id}/participants/{participant_id}/fuel`. Both keys are UUIDs; missing sessions/participants or a participant from another session return 404. Unresolved participants are supported without creating a global driver identity.

Stintara forms **checkpoint consumption samples** from persisted **lap fuel checkpoints**, sorted by lap number: previous fuel minus current fuel. Only consecutive lap numbers, finite nonnegative fuel, unchanged pitstop counters, neither endpoint in pits and a strictly positive finite delta qualify. There is no outlier/MAD filtering or value cutoff. Exclusions never clear earlier valid history. The estimate is the median of the last up to five valid samples, requiring at least three. `sample_count` counts samples actually used in the median (zero while collecting); `available_sample_count` supplies the collecting progress (0–2). Diagnostics mark samples used only once an estimate exists.

Exclusion priority and stable reasons: `non_consecutive_laps`, `missing_or_invalid_fuel`, `pitstop_transition`, `pit_context`, `fuel_not_decreasing`. The API sends only the last twelve examined transitions, with delta, included/used flags and reason. Samples used in the estimate can precede this diagnostic window after a long run of excluded transitions.

Current fuel/capacity/timestamp come exclusively from the participant's latest strategy sample (timestamp, then UUID tie-breaker). There is no lap-fuel fallback. A sample age of 0–15 seconds is fresh; future timestamps are not fresh. Range is current fuel divided by the independent consumption estimate only with fresh, finite, nonnegative current fuel and sufficient consumption samples. Otherwise range is null. Consumption can still be displayed while current fuel is stale. Non-finite values are returned as null, not invalid JSON.

The experimental widget shows collecting/ready/stale states, a separate **Provider comparison**, and expandable diagnostics. Provider average consumption, range, stint number/laps/consumption are comparison values from the same latest strategy sample, never calculator inputs or fallbacks. Focus changes immediately clear the previous fuel display; obsolete responses cannot replace the current focus. All estimates are rounded only for display.

Unverified: exact LMU checkpoint timing, inlap/outlap timing, pitstop-counter timing and physically exact per-lap consumption. Not implemented: fuel to finish, remaining race distance, reserve, refuel recommendations, pit strategy, yellow-flag filtering, outlier filtering or adaptive weighting. Real LMU/MAPC validation remains required.


## Replay and synthetic scenarios

[Replay & Scenario Harness](docs/REPLAY.md) documents the separate private Pitwall recorder, file-local pseudonymizer/audit, loopback wire replay, logical-throttle compiler and compiled protocol replay. The committed demo is wholly synthetic. Raw and sanitized-real captures remain private under ignored `data/`; only synthetic fixtures may enter Git. No production agent or protocol changes are required. Use a dedicated synthetic agent against test/dev infrastructure: protocol replay creates real database rows, and accelerated replay still obeys server-side persistence limits.


### Spectator Timing Pass 1

For `Race` only, Gap is leader-relative: a positive integer provider lap deficit displays as `1 L`, `2 L`, etc.; otherwise a finite nonnegative provider time gap can display in seconds. Invalid positions/deficits, negative/non-finite/missing time values and contradictory leader values remain unknown. Leader zero is accepted only when supplied; raw provider storage is never rewritten or converted with absolute value. Non-Race gaps remain unknown. Zero gaps for nonleaders are accepted in this bounded pass; their real-world edge cases still need validation under #10.

Full Field Interval is current gap minus the immediately preceding position's gap, only for unique consecutive positive positions, equal known completed-lap counts, explicit zero lap deficits and finite nonnegative nondecreasing gaps. Missing lap-deficit metadata is insufficient evidence for an interval. H2H uses that exact UUID-linked relationship: Ahead negative, Behind positive; the redundant Focus value is omitted. These are provider-position-based relationships, not physical track order. Relative remains a standings window without physical timing inference.

The header shows configured duration, or positive configured Race laps when no positive duration is present. When both are positive, duration takes precedence: the lap value may be projected or a sentinel. No remaining-lap or remaining-time estimate is calculated. Elapsed session time comes exclusively from the newest fresh scoring observation of the exact session, without client extrapolation; stale/no-runtime clocks show `—`. One observation supplies the overlay; recorded cars missing from it retain their row but cannot contribute old gaps to live intervals.

Provider `total_laps` is labelled **Completed laps**, without adding one or changing persisted values. Focused S1/S2/S3 are finite positive values from the latest persisted completed-lap checkpoint, with its lap number shown. Missing/invalid sectors stay unknown; older laps are not used to fill holes. S2 is already individual sector time after adapter normalization. Numeric validity does not establish official lap validity (#6).

This is a partial implementation of #10, #20 and #22; all remain open. Native remaining time, current/best live sectors, finish-state behavior and ambiguous provider timing still need later work and real LMU validation. No schema, protocol, agent or dependency changes are required.
