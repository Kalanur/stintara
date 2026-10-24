# V0.1 baseline validation

Date: 2026-10-12. Scope: stabilize the existing prototype; no RaceControl integration, extra simulator or large feature was added. No external deployment, Git commit, push, release or registry publication was performed.

## Previous state and corrections

The repository already contained a Python/FastAPI server, a Python WebSocket agent, PostgreSQL schema, two HTML pages, Docker/Compose configuration, a GHCR workflow and Unraid templates. Files were untracked and no automated test suite existed.

| Finding | Smallest applied correction |
| --- | --- |
| Strategy samples used `bigserial`, contrary to UUIDv7 policy. | Generate UUIDv7 sample IDs; transactional compatibility conversion preserves existing sample data. |
| Session reuse checked only the type in memory; long inactivity never closed sessions. | Persist per-observation activity/session time, check track/type/clock transitions and expire stale observations. |
| One agent disconnect closed the entire shared session. | End that observation; close the session only when no active observations remain. |
| Live state was session-wide, potentially keeping another disconnected driver visible. | Require recent activity from an observation belonging to the displayed driver. |
| Scoring wrote every packet; telemetry relied solely on the agent throttle. | Cache scoring live, checkpoint at most 1 Hz, enforce strategy sampling on the server and check the last stored timestamp after reconnect. |
| Pitwall data reached server storage without a normalized contract. | Add a small agent adapter and server transport validation; domain lifecycle stays provider-neutral. |
| Initial lap snapshots could arrive before scoring and be lost; repeated snapshots overwrote current standings/history. | Buffer the initial snapshot until session/participants exist, insert laps idempotently and keep lap history separate from current standings. |
| Sector 2 was cumulative S1+S2. | Normalize it to the individual sector duration in the adapter. |
| Individual tyre temperatures, brake temperatures and lap damage were omitted. | Add explicit strategy columns and normalized JSON lap snapshots. Preserve remaining-tyre semantics. |
| Default passwords/tokens could start the application. | Remove defaults, require configuration and reject known placeholder tokens. |
| Clean Pitwall closures and loss of the uplink during source retries were not handled consistently. | Back off and reconnect; monitor uplink closure independently of the source loop. |
| Frontpage omitted the car; driver page omitted pressures/temperatures and inserted source strings as HTML. | Complete the functional fields, escape source strings and show API errors/offline state. |
| Compose used an implicit network and the workflow ran only compile checks. | Use `stintara`, PostgreSQL 17, required secrets, and a test job before Docker build/publication. |

The existing driver identity uniqueness constraint and nullable participant `driver_id` were retained and tested. Display names remain presentation data; no name-based matching was introduced. The existing Unraid server template already had correct ports, appdata, network and empty secret fields and needed no edit.

## Verification results (earlier V0.1 baseline run)

| Check | Result |
| --- | --- |
| Python tests | **38 passed, 0 skipped** with the full test database configured. |
| PostgreSQL integration | Passed against isolated portable PostgreSQL **17.11** on localhost; temporary test schemas were removed after each test. |
| Identity | Duplicate provider/external ID rejected; equal names allowed; multiple provider identities per driver; unresolved participants accepted. |
| Sessions | Short reconnect with lost runtime state; track/type/clock changes; timeout finalization; multiple observations; independent disconnect; unrelated agents remain separate; split race IDs distinct. |
| Telemetry/laps | UUIDv7, remaining tyre 0/1, individual temperatures, damage, initial snapshot ordering, lap idempotence and server-side 1 Hz sampling including reconnect guard. |
| Transport | Actual agent/WebSocket/server/PostgreSQL/API path with simulated Pitwall frames; authentication, source reconnect and uplink-loss handling. |
| Frontend tests | **2 passed** using Node's test runner and a DOM/fetch stub: empty/offline/error states, car, tyres, standings, safe links and HTML escaping. These are not browser visual tests. |
| Actual application startup | A separate Uvicorn process completed the real startup/bootstrap against PostgreSQL; `/health` returned `ok`, `/` returned 200 and the driver API returned `live: false`. |
| Python syntax and lint | `compileall` and Ruff passed; no type checker was configured. |
| Dependency consistency | `pip check` passed. |
| Unraid XML | Both templates parsed and deployment settings/empty secrets were asserted by tests. |
| Workflow/Compose YAML | Parsed and contract-checked in tests. |
| Docker build | **Not run successfully:** Docker is not installed on the local machine. |
| `docker compose config --quiet` | **Unavailable locally:** Docker CLI is missing. Static YAML checks passed; CI now includes the real Compose command. |
| GitHub Actions / GHCR | Workflow prepared, not executed or published. |
| Live LMU / the Unraid host | Not exercised; requires the real operator smoke test in [README.md](../README.md). |

The test environment used Python 3.12. The Docker image and CI use Python 3.13; those environments still need their first real build/run. The local system Node 14 could not run `node --test`, so the bundled newer Node runtime was used for frontend tests.

## Changed files

- Backend: `server/app/main.py`, `server/app/domain.py`, `server/app/lifecycle.py`, `server/app/protocol.py`, `server/app/migrations.py`, `server/db/schema.sql`.
- Agent: `agent/stintara_agent.py`, `agent/pitwall.py`, `agent/run-agent.ps1`.
- UI: `server/static/index.html`, `server/static/live.html`.
- Deployment: `server/Dockerfile`, `server/.dockerignore`, `docker-compose.yml`, `.env.example`, `unraid/stintara-postgres.xml`, `.github/workflows/docker.yml`.
- Verification: `pyproject.toml`, `requirements-dev.txt`, `tests/__init__.py`, `tests/conftest.py`, `tests/test_database.py`, `tests/test_deployment.py`, `tests/test_domain_adapter.py`, `tests/test_transport.py`, `tests/ui.test.cjs`, `.gitignore`.
- Documentation: `README.md`, `docs/ARCHITECTURE.md`, this file.

## Remaining boundaries and first deployment

The implementation remains a modular monolith with one server worker and PostgreSQL. Multi-agent observations are possible through an internal trusted association function, but no automatic RaceControl association or client-controlled session attachment was added. Local matching cannot conclusively distinguish a same-track/type restart if no disconnect or session-clock reset is observed. Driver swaps and reconciliation of differing per-observer vehicle IDs remain deferred.

Before upgrading an existing database, back it up: legacy numeric strategy-sample IDs will change to UUIDv7 while their telemetry and participant references are retained. This is a targeted transactional compatibility step, not a general migration framework.

For the first start on an Unraid host, follow [README.md](../README.md): build the image locally on the host (or explicitly authorize a later GHCR publication), create network `stintara`, install both templates, configure fresh password/token values, start PostgreSQL and Stintara, verify `/health`, then connect the Windows agent and exercise the real LMU/reconnect test. These external actions remain operator steps.

## Public deployment hygiene and optional proxy follow-up

The follow-up removes installation-specific host references and personal driver/agent defaults from public documentation, examples, templates, server defaults and test fixtures. Generic values are `Stintara Driver`, `driver` and `gaming-pc`; existing installations should retain their own bootstrap values in private configuration. Project registry/repository URLs, loopback test addresses and generic Unraid appdata paths remain valid public configuration.

The README now documents the standalone installation first and optional proxy integration separately. Only the application container may join an existing operator-managed `<reverse-proxy-network>`. PostgreSQL stays on `stintara` with no host port. Both public templates keep a single `stintara` network and empty Post Arguments; Compose has no external network dependency. Port 8080 remains published for V0.1 diagnostics.

Network attachment is a manual operator step with membership inspection before connecting and after container recreation. No proxy network is created automatically. No private files were generated; `deploy/local/` is ignored if operators need local notes or overrides. The hygiene test checks public source paths, literal addresses/URL hosts, common credential signatures and empty deployment secret fields without listing any private values. It is a conservative regression check, not proof that arbitrary private names or every possible secret can be detected automatically.

Follow-up checks:

- Python suite: **25 passed, 17 skipped**. PostgreSQL-dependent tests were explicitly skipped because no test database was configured for this run; the earlier 38-test database run above is historical, not a new result.
- Frontend: **2 passed** with the Node test runner.
- Ruff and Python `compileall`: passed.
- Deployment tests: passed, including both Unraid XML files, Compose/workflow YAML, required secrets, standalone network membership, no database port publication and preserved application port 8080.
- Repository hygiene checks: passed for public source files, including untracked source files awaiting the initial commit. Git metadata, environments, build artifacts and private operator files were excluded.
- Docker was unavailable; no Docker build or live network operation was attempted.
- **Real reverse-proxy integration remains an operator smoke test.** No TLS endpoint, proxy WebSocket or container network attachment was exercised.

Changed in this follow-up: `AGENTS.md`, `README.md`, `docs/ARCHITECTURE.md`, `docs/VALIDATION.md`, `.gitignore`, `.env.example`, `docker-compose.yml`, `unraid/stintara.xml`, `agent/run-agent.ps1`, `server/app/main.py`, `tests/test_deployment.py`, `tests/test_repository_hygiene.py`, `tests/test_transport.py` and `tests/ui.test.cjs`. The PostgreSQL template required no change.

No commit, push, release, deployment, registry publication or external infrastructure change was performed.

## First-commit preparation check

The repository still has no commits and an empty index. The first-commit scope is 37 public source/configuration/documentation files, all currently untracked. No existing ignored files, local environments, database dumps, logs or private operator files were found inside the repository. Standard `git diff --check` and `git diff --stat` are empty because untracked files are not included; the candidate files were additionally checked individually against an empty file without changing the index.

The ignore rules now also cover environment variants (while preserving example templates), `venv`, Node dependencies, build/package output, type-check/coverage caches, root-local logs/temporary output and per-user IDE state. Shared IDE project settings and the SQL schema remain visible. Existing exclusions for `.env`, `.venv`, local data and `deploy/local/` remain in place.

A CI discrepancy was corrected: automatic `latest` generation by the metadata action for SemVer tags is disabled with `flavor: latest=false`. The explicit default-branch `latest` rule remains active. Deployment tests now assert the metadata setting and the PR login/push and branch/tag job gates. The README also states that a later default-branch push will publish after successful checks, and that manual workflow dispatch follows the same gate. No workflow was triggered during this review.

Current precommit validation: **25 Python tests passed, 17 PostgreSQL-dependent cases skipped** (no configured test database); **2 frontend tests passed**; Ruff, `compileall`, Unraid XML parsing, Compose/workflow YAML checks and public-source hygiene checks passed. Skipped cases comprise all 15 cases in `test_database.py` plus database bootstrap/authentication and the database-backed end-to-end transport case in `test_transport.py`. Earlier PostgreSQL results above remain historical. Docker is unavailable, so Compose CLI validation and image building remain unexecuted locally.

No real credentials or private infrastructure values were found in the planned files. Empty deployment secret fields, explicit README placeholders, localhost/bind addresses, public project URLs and clearly named test-only credentials were retained. No `git add`, commit, push, tag, release, registry publication or deployment was performed. The baseline is ready for an initial source commit, with the documented Docker/real-LMU/Unraid/proxy runtime checks still outstanding.


## Dedicated development image channel (2026-10-12)

The workflow now separates `dev` and default-branch `main` publication. Login and push have independent event/ref allowlists; feature pushes and feature dispatches only test, and PRs build without publishing. SemVer tags do not move either channel. The existing complete CI test job is unchanged. The standard Unraid template still uses `:latest`.

Local checks for this change:

- Python `compileall` and Ruff passed.
- Python suite: **41 passed, 17 skipped**. No `TEST_DATABASE_URL` was configured; the PostgreSQL-dependent cases remain skipped, not newly verified.
- Frontend Node test runner: **2 passed**.
- Deployment checks parsed workflow/Compose YAML and both Unraid XML templates. Sixteen event/ref scenarios exercised the declared job, login, push and metadata enable expressions, including dispatch, PRs, tags, branch-name lookalikes and a changed default branch. These evaluate the boolean subset used by this workflow, not the full GitHub Actions runtime or Docker metadata-action implementation.
- Public-source repository hygiene checks passed. No private deployment values were added.
- Docker was unavailable; Docker builds and `docker compose config --quiet` could not be executed locally. CI retains Compose validation and the complete PostgreSQL test suite.

The previous feature-commit CI succeeded according to the operator. This does not establish CI success for the new workflow revision. No development branch, release, image publication or deployment was created as part of local validation. Real Dev-container integration and migration tests remain operator steps after the development channel is established.


## First real LMU integration and hardening (2026-10-15)

The operator reported a successful real baseline smoke test:
LMU -> unmodified Pitwall -> Stintara Agent -> WebSocket -> container-hosted Server -> PostgreSQL -> Live API -> Browser.
Track/session, the driver field and player association, position, lap/last/best timing, fuel consumption/range, tyre remaining/pressure/temperatures, brake temperatures, offline detection and reconnect were observed. This is an operator-reported result for the baseline, not a new deployment performed by this change.

Repeated reconnects were observed, but the original close reason and rejected frame were not captured. Code inspection identified a concrete amplification path: any provider-field validation error closed the authenticated socket, and the agent's clean-close path printed no reason before retrying. That path is addressed by dropping isolated invalid data frames, bounded validation diagnostics and explicit disconnect/retry reporting. Tests establish this behavior, not that every real disconnect has the same cause. Repeat the real smoke test with this revision and inspect close codes and validation fields if reconnects continue.

Pre-drive temperatures at or below -200 Celsius now normalize to unknown. Nonpositive pressure with absent/sentinel surface temperatures and no flat/detached flags becomes unknown; real sensor/damage evidence preserves zero pressure. Valid fuel/tyre values and remaining semantics are preserved. Entry/model/manufacturer semantics are separated in the API/UI without rewriting legacy database columns. Vehicle models are not guessed. Position/gap raw values remain unchanged; source findings and limitations are recorded in ARCHITECTURE.md.

Local validation on a source mirror, checked against the final repository before commit:

- Python compileall and Ruff passed.
- Python suite: **66 passed, 18 skipped**. No local TEST_DATABASE_URL was configured. The skipped cases are 16 database cases plus bootstrap/authentication and database-backed transport E2E. The new sentinel-persistence/API regression is among these skipped database cases; CI must exercise it with PostgreSQL 17.
- Three frontend tests passed, including honest vehicle fallback labels, escaped values, unknown tyre values and existing live/offline/error behavior.
- Transport tests without PostgreSQL cover isolated invalid scoring/telemetry/lap payloads followed by valid processing, fatal protocol/handshake errors, rejected tokens, duplicate connections, rolling rejection budget, real socket abort/reconnect, safe close diagnostics, retry messages and clean Ctrl+C.
- Deployment YAML, both Unraid XML templates and public-source hygiene tests passed as part of the suite.
- Starlette emitted a TestClient/httpx deprecation warning; tests passed. No dependency migration is included in this fix.
- Docker was unavailable. No image build, deployment, new real LMU session or reverse-proxy test was performed locally.

No schema migration is required. The baseline's prior CI/database and real-container results above are historical, not evidence that the new revision has already passed CI or a repeat real integration test.

## Repeat real LMU online-race validation

Date: **2026-10-16**. Operator-reported smoke test of development revision `a71e3224f968c0e9256e7dc662d2a4780542fab2`, before the vehicle-identity presentation changes. No new deployment was performed to write this record.

The operator completed a full real LMU online race through LMU -> unmodified LMU Pitwall -> Stintara Agent -> WebSocket -> Stintara Server -> PostgreSQL -> Live API -> Browser.

- Exactly one new `/ws/ingest` connection served the agent run; the agent remained connected throughout the race, with no reconnect storm.
- No ValidationErrors, exceptions or tracebacks were observed during the test window.
- Live timing, fuel, tyres and standings remained functional throughout.
- Ctrl+C stopped the agent cleanly with `Stintara agent stopped.`, without a CancelledError or KeyboardInterrupt traceback.
- The Unraid WebUI container deployment with PostgreSQL 17 and the development image was also verified using `POSTGRES_PASSWORD_FILE`, `BOOTSTRAP_AGENT_TOKEN_FILE` and read-only secret mounts.
- The existing PostgreSQL database was reused, the Stintara healthcheck succeeded, and the agent authenticated successfully against the file-secret-configured server.

This repeat test verifies the original reconnect symptom as fixed for the observed full race and confirms the file-secret deployment path. It is an operator smoke test, not a formal completeness proof or validation of every LMU session type, network failure, driver swap or future revision. Vehicle model/manufacturer accuracy is not established by working standings. Private infrastructure values and credentials are intentionally omitted.

## Vehicle identity follow-up checks (2026-10-16)

- Source investigation and its exact upstream revision are recorded in ARCHITECTURE.md, including the unexposed `mVehicleModel` candidate and the separate post-race `CarType` path.
- Python `compileall` and Ruff passed on a source mirror checked against the repository before commit.
- Python suite: **109 passed, 18 skipped** without `TEST_DATABASE_URL`. Skips comprise 16 database cases plus bootstrap/authentication and database-backed transport E2E. The extended legacy-car-number API/storage regression is among the skipped cases and still requires PostgreSQL in CI.
- **4 frontend tests passed**: semantic fallback priority, blank values, HTML escaping, no entry-based model guesses, and existing live/offline/error behavior. These use a DOM/fetch stub, not a visual browser test.
- Adapter/protocol tests confirm that Pitwall's slot stays in `id` while `car_number` becomes null. API projection tests keep manufacturer/model unknown even for recognizable entry keywords.
- The existing Starlette TestClient/httpx deprecation warning remains; no dependency changes were made.
- No schema changes or migration. No new Docker build, deployment or real LMU drive was performed for this follow-up.


## Transient SessionInfo follow-up (2026-10-18, Issue #11)

The operator reported a real LMU Championship test in which Practice -> Qualifying -> Race worked. After session end, repeated empty `SessionInfo.track_name` values caused a reconnect storm. Server diagnostics identified the reproducible failure path: `session_info` failed the required track-name validation and closed ingestion with code 1008; reconnecting delivered the same transient provider state again.

The local Pitwall adapter now drops a SessionInfo snapshot when its track name is missing, not a string, empty or whitespace-only. Valid track names are preserved verbatim. Dropped snapshots cannot overwrite the last valid metadata or trigger a session change. Server validation, authentication, envelope handling and the existing invalid-payload budget remain unchanged; the fix requires the updated agent. No post-race status/UI, gap or multi-agent changes are included.

This is **implementation ready for repeat real-session validation**, not a claim that the fix has already passed a real Championship/session-end test.

Local checks on the source mirror: compileall and Ruff passed; 120 Python tests passed and 19 database-dependent cases skipped without TEST_DATABASE_URL (17 database cases, bootstrap/authentication and database-backed transport E2E). The new database preservation test is among the skipped cases and requires CI with PostgreSQL. Four frontend tests passed. The forwarding/ingestion regression processed valid metadata, 30 empty snapshots, continued scoring/telemetry/lap data and later valid metadata on one WebSocket; only an explicitly injected invalid protocol closed it. The existing Starlette TestClient/httpx deprecation warning remains. No new real-session validation or deployment was performed.


## Windows packaging and provisioning (Issue #5)

The operator reports that the pre-packaging Python agent already passed a real public WSS/TLS reverse-proxy smoke test, with one stable ingest connection and no validation/error/exception entries. This is historical single-observer evidence, not a test of the new EXE or cross-observer session matching.

Local checks on the source mirror, compared with the repository before commit:

- Compileall and Ruff passed; **156 Python tests passed, 20 skipped** without TEST_DATABASE_URL. Skips comprise 17 database cases, the new provisioning integration, bootstrap/authentication and database-backed transport E2E. The provisioning transaction/hash/UUID/driver-reuse regression still requires PostgreSQL CI validation.
- **4 frontend tests passed**. Existing reconnect, duplicate-agent, safe diagnostics, transient SessionInfo filtering and clean Ctrl+C coverage remains passing after explicit config injection.
- Config tests cover JSON, environment precedence, required values, safe parse errors, explicit paths, Python/frozen location and network-free help. Provisioning unit tests cover validation, file-secret/DSN precedence and one-time output with safe failure diagnostics.
- Windows workflow YAML and its exact two-file artifact allowlist passed checks. The existing Linux/container workflow is unchanged.
- A real local Windows Onefile console build passed with PyInstaller **6.22.3**, Python **3.12.10** (CI targets Python **3.13**). The unsigned EXE is **10,157,293 bytes**; both --help and --version returned exit code 0 without configuration or networking. The server module's --help also passed from the server working directory.
- The distribution directory contains only StintaraAgent.exe and the public example JSON. The embedded archive contains no JSON, secret or environment files. No private config was used or bundled, and no binary is committed.
- The existing Starlette TestClient/httpx deprecation warning remains. No new deployment, real EXE drive, same-split/different-split test or automatic session convergence was performed.

Windows packaging/provisioning implementation ready for external multi-observer field testing. Database-backed provisioning and the Windows/Python 3.13 workflow must also pass CI; local build success is not a claim that the remote workflow has already run.


## Session spectator iteration (2026-10-23)

Local development used an additional Python 3.13.15 installation and an isolated `.venv`; existing Python installations were retained. Installed development and agent-build requirements. Windows application control rejected the asyncpg 0.32.0 wheel DLL; local tests use compatible asyncpg 0.31.0 within the existing requirement range. No security policy or repository dependency range was changed.

- Python suite: **160 passed, 21 skipped**. All skips require `TEST_DATABASE_URL`: 17 database cases, provisioning, spectator database integration, bootstrap/authentication and database-backed simulated WebSocket E2E. No local PostgreSQL service was configured. Existing Starlette/httpx deprecation warning remains.
- Compileall, Ruff and `pip check` passed. Node 24.22.1: **5 frontend tests passed**, including a synthetic 60-participant field, separate session links, escaping, null timing, unknown gaps, empty/stale/error states and existing driver/tyre rendering.
- API/projection tests cover UUID validation, 404, offline retained participants, unresolved same-name participants, tied positions, newest fresh observation selection and exclusion of other/stale sessions. PostgreSQL integration coverage was added for session listing, field completeness, separation and stale/ended feeds; its local execution is explicitly skipped.
- Windows Onefile build passed using Python 3.13.15 / PyInstaller 6.22.3. EXE `--help` and `--version` passed without config or network. Generated files remain ignored.
- Browser visual inspection passed with a local synthetic 20-driver fixture. This exercises rendering, not the database-backed API or real ingestion.
- Docker CLI is unavailable; Docker build and real `docker compose config` were not run locally. Existing static deployment/Unraid checks pass within pytest. Deployment configuration is unchanged.

Required real LMU/MAPC checks: full-field comparison with Pitwall across practice/qualifying/race, transient disconnect and reconnect, session transitions, stale-feed display, and post-finish behavior. Capture same-split and different-split observations before designing convergence; verify raw gap/interval and finish signals separately under Issues #9/#10. No real LMU/MAPC run, merge, deployment or image publication was performed for this iteration.


## Spectator Relative / Head-to-Head (2026-10-23)

Based on dev `7d01b1f4e63894d596c61c2012d3d341f28d9692`. Only synthetic participants and sessions were used. Existing local Python 3.13 environment and dependencies were reused; no Docker/PostgreSQL installation or agent change was needed.

- Compileall, Ruff, `pip check` and `git diff --check`: passed.
- Local pytest: **160 passed, 22 skipped** without `TEST_DATABASE_URL`. Skips are 17 core database cases, provisioning, two spectator database cases, bootstrap/authentication and database-backed simulated transport E2E. The existing Starlette/httpx deprecation warning remains.
- Node frontend suite: **13 passed**. Direct pure-helper tests cover UUID-only focus resolution, unresolved/equal-name participants, default ambiguity, relative windows and boundaries/ties, and unique exact neighboring positions (including invalid, missing, duplicated and reordered positions).
- Stateful rendering tests exercise query parameters, selector changes, URL replacement/removal, polling persistence through position/name/order changes, disappeared focus without replacement, highlighting, interval placeholders, preserved horizontal scroll, XSS escaping and unique own-participant driver links.
- Added PostgreSQL regression checks actual participant primary keys and unchanged legacy API fields for same-name resolved/unresolved participants. Locally skipped, required in CI.
- Browser checked with ten synthetic participants: focused P5 has P4/P6 comparison cards and a seven-entry relative window. Narrow viewport checked at 390 px: comparison cards stack in one column, table overflow stays inside its scroll container, and the document does not overflow horizontally. This is UI evidence only.
- CI uses the existing PostgreSQL 17 test/Compose/container workflow and Windows agent build/smoke workflow. Their status and exact final commit are recorded on the Draft PR; green local non-database checks alone do not establish database/CI success. PR container builds must not publish images.

Real LMU/MAPC validation is absent. Later compare the complete field with Pitwall; keep an unresolved and a registered focus through real polling, position changes, ties and pit phases; check exact-position Head-to-Head selection and relative ordering; exercise session transitions and verify focus cannot move to a similarly named participant in another session. These synthetic checks establish selection behavior, not real physical order, timing-gap semantics, finish status or cross-agent convergence. Issues #9/#10, Fuel Calculator and Trackmap remain out of scope.


## Fuel estimate validation foundation (2026-10-23)

Base: dev `30e060e98f3e6cc9a3c088d71c5e2e9510ec65c2`. All new data and fixtures are synthetic; no LMU/MAPC access or real fuel history was used.

- Existing Python 3.13 environment: compileall, Ruff, pip check and diff whitespace checks passed. No dependencies, schema, protocol or agent changes.
- Local Python suite: **194 passed, 23 skipped** without `TEST_DATABASE_URL`. The new PostgreSQL fuel API regression is skipped locally, alongside the existing 22 database-dependent cases. The existing Starlette/httpx warning remains.
- Pure calculation tests cover threshold 0–3, sorted consecutive checkpoints, exclusions and their priority, both pit-context endpoints, unchanged history across stops/long invalid runs, rolling last five, median rather than mean, unfiltered large valid consumption, finite values, diagnostic limits, freshness boundary/future timestamps, zero fuel, range overflow, no lap fallback and independent provider comparison.
- PostgreSQL regression covers UUID/session membership and 404s, reversed insert order, newest strategy selection, unresolved participant without strategy, no older-row/lap fallback, provider projection, stale fuel and unchanged persisted rows. CI must execute it against the isolated PostgreSQL service; local skips are not successful DB validation.
- Frontend: **17 tests passed**, including existing focus/Relative/H2H coverage. New cases cover no-focus requests, UUID routes, switching/disappearing focus, collecting 0/1/2, ready/stale rendering, separate provider data, diagnostics escaping/expansion, error reset and delayed A→B→A response rejection. No fuel-to-finish or refuel output is introduced.
- Browser visual inspection used synthetic fixture API responses generated by the pure calculator, showing independent Stintara/provider values and experimental labeling. This does not validate the database-backed server or provider checkpoint timing.
- CI status for the exact committed revision is recorded on the Draft PR: existing PostgreSQL/Compose/container checks and Windows agent build/smoke workflow, with no PR image publication. Local Docker/PostgreSQL installation was not attempted.

Real LMU/MAPC validation still required: checkpoint timing at lap change; normal, fuel-saving and high-consumption laps; inlap, refueling stop, non-refueling stop if available and outlap; pitstop counter and `in_pits` timing; estimate versus actual tank history and versus Provider/Pitwall estimates; disconnect/reconnect and session transitions. Synthetic arithmetic/selection tests prove none of these physical/provider semantics. Finish distance, reserve, refuel/pit strategy, yellow-flag/outlier filtering and adaptive weighting remain unimplemented; Issues #9/#10 are untouched.


## Replay & Scenario Test Harness V0.1 (2026-10-23)

Base: dev `ceb1735b23c3e1a6da9d446f3ee9902d98bba2fd`. All harness data is wholly synthetic. No LMU/MAPC access, real capture, deployment or remote ingest test was performed. Existing Python 3.13 environment/dependencies were reused.

- Local compileall (including tools), Ruff, pip check and diff-check passed. Local pytest: **243 passed, 24 skipped**, with the existing Starlette/httpx deprecation warning. All skips require PostgreSQL/TEST_DATABASE_URL, including the new compiled replay ingest E2E; none count as successful local database validation.
- Existing frontend tests: **17 passed**. No UI/product logic changed.
- All six CLI help smokes passed. Synthetic CLI generation, format load/validation, compilation and protocol validation passed (29 input / 29 compiled messages). The committed demo matches generator output byte-for-byte and is under 40 KB.
- Pure tests cover strict version/source/envelope validation, malformed JSON, non-finite/negative/decreasing time, exclusive output writes, deterministic file-local pseudonyms, unchanged numeric/pit/fuel/finish/gap values, masked audit keys, and private output path enforcement. Explicit connection/credential fields are rejected in compiled payloads without printing values.
- Compiler tests compare against the actual production agent forwarder with an injected logical clock, including scoring/telemetry throttles, unthrottled connection messages, unknown/invalid messages and empty SessionInfo. The entire demo passes the real adapter and existing server payload models and supplies five valid Fuel Foundation samples.
- Real loopback WebSocket tests cover the fake Pitwall server, order, a fresh stream per client, recording its parsed frames, and the compiled file replay client's hello/welcome and send sequence. Injected sleep tests cover 1x/scaled/immediate timing, hold-open and rejected handshakes without real timing delays. No external service is contacted.
- Hygiene checks require synthetic source for every tracked replay JSONL and every fixture in the permitted directory, reject real/sanitized-real/unknown sources, connection metadata and mapping/config files, and prohibit tracked private captures. Existing public-source hygiene now includes tools/.
- A PostgreSQL CI E2E sends the compiled synthetic demo through the real WebSocket ingest path and checks session, six participants, nine laps, strategy persistence, spectator API and independent fuel estimate. It does not bypass server clocks or claim identical accelerated strategy history. CI run IDs and results for the final commit are recorded on the Draft PR. Existing Compose/container and Windows agent build/smoke checks must pass; PR builds do not publish images.

Still required on real LMU/MAPC: recorder alongside the normal agent, full-session capture and interruption, sanitizer/audit on real private data, 1x wire replay through the unchanged agent, accelerated compiled replay, pitstop/fuel and checkpoint timing, finish signals, negative-gap anomalies, session transitions and disconnect/reconnect. Synthetic tests establish no real provider frequency, lap validity, gap/finish semantics or physical fuel timing. Server receipt-time persistence limits continue to apply, even after deterministic logical agent sampling.


## Spectator Timing Pass 1 (2026-10-24)

Base: `1d81386d47d516560d3e6e123736042a947a61a1`. All new fixtures are synthetic; no real tester data or private infrastructure is included. Local Python tests: **271 passed, 25 database tests skipped** without TEST_DATABASE_URL. Frontend: **19 passed**. Compileall, Ruff, pip check and diff-check are required before commit; final CI results are recorded in the Draft PR.

Coverage includes zero/positive/negative/non-finite gaps, lap-deficit precedence, contradictory leader timing, duplicate/non-consecutive positions, mismatched/missing completed laps, missing lap-deficit metadata, non-Race fallback, and non-mutation of source rows. Clock tests cover newest fresh matching observation, future/stale/no runtime, fixed-lap and timed configuration (including conflicting configuration), and zero elapsed time. UI tests cover signed H2H, Full Field timing, configured distance/duration, completed-lap labels, sector formatting and unknown fallbacks while retaining all prior focus/fuel/escaping tests.

The added PostgreSQL integration test checks latest persisted sectors despite reversed insertion order, missing components/participants, fresh interval/clock projection and unchanged stored lap rows. It is skipped locally and must execute in CI. A query-count regression enforces two API database round trips with a lateral sector lookup; real load benchmarking is not claimed.

Real LMU validation remains required for positive/zero gaps during green-flag and finish transitions, lap deficits and crossing-line lap-counter differences, configured duration/lap sentinel behavior, session clock reset/staleness and latest-sector checkpoints. No official countdown, remaining-lap forecast, current/best sector extension, finish semantics or Fuel-to-Finish is implemented. #10, #20 and #22 remain open.
