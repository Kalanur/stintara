# Stintara development rules

These rules apply throughout this repository. Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) before making architectural changes and [README.md](README.md) for setup and current limitations.

## Identity and matching

- Never identify drivers by display name or merge drivers because names are equal or similar.
- All internal persistent entity IDs use UUIDv7.
- Stable provider identity is `(provider, external_id)`. Enforce `UNIQUE(provider, external_id)` and allow a driver to have multiple provider identities.
- Unresolved session participants are valid. A missing stable identity must not be replaced with a fabricated identity based on a name or local vehicle ID.
- Preserve historical display names per participation.
- Prefer official provider event/race/session IDs. Each split is a separate race.
- Keep event, race, session and agent observation distinct. Observations of the same identified official race/session must converge on the same central objects.
- Short reconnects must not create duplicate sessions. Do not merge separate races using only track, time or display names.

## Boundaries and scope

- Keep the backend simulator-independent. LMU Pitwall is an adapter/data source, not a dependency of the domain model.
- Use LMU Pitwall unmodified initially and keep provider-specific data handling within adapter boundaries.
- Keep the local agent replaceable; domain matching and database logic belong on the server.
- Keep a modular monolith. Do not introduce microservices without a measured need.
- Keep V0.1 focused on the LMU end-to-end pipeline. Planned providers and future features are not automatically part of the current task.
- Initially use PostgreSQL, 1 Hz strategy persistence, 2-5 Hz live scoring and persisted lap snapshots. Defer high-frequency raw storage.
- Preserve LMU tyre semantics: `mWear` is remaining tyre, `1.0 = new`, `0.0 = destroyed`. Use `tyre_remaining_*` naming.

## Deployment and secrets

- Preserve Unraid compatibility and the two central containers: Stintara server and PostgreSQL 17.
- Preserve the custom Docker network `stintara`, persistent appdata mounts and GHCR distribution for `linux/amd64`.
- Keep agent connections outbound; do not require incoming port forwarding to the gaming PC.
- Do not publish PostgreSQL port 5432 publicly or to the LAN.
- Never commit real credentials or tokens. Do not introduce usable default secrets.
- Keep deployment configuration, Unraid templates and setup documentation consistent when changing deployment behavior.

- Keep public files free of private hostnames, addresses, domains, network names, credentials and personal filesystem paths. Use generic placeholders for operator infrastructure.
- Keep PostgreSQL exclusively on the standard `stintara` network. Only the application server may optionally join an existing operator-managed proxy network; standalone operation must remain supported.
- Keep private overrides outside public sources; `deploy/local/` is ignored. Do not add automatic proxy-network creation or non-idempotent Unraid Post Arguments hooks.

## Changes and verification

- Inspect the existing implementation before editing. If it conflicts with these rules, describe the discrepancy and choose the smallest clean correction within the task's scope.
- Identity or session matching changes require automated tests. Cover the relevant uniqueness, unresolved-participant, split-separation, reconnect and multi-agent cases, including cases that must not merge.
- Changes to tyre conversion or persistence require tests for remaining-tyre semantics.
- Run tests and checks appropriate to the change; report what ran, what passed and any validation that could not be performed. Validate Unraid XML syntax when changing templates.
- Update docs/ARCHITECTURE.md when architectural decisions change. Distinguish requirements from implemented and verified behavior; do not present planned provider integrations as working features.
