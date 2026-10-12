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
