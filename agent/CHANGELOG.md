# Stintara Agent changelog

The Agent is versioned independently of the Stintara server. Releases are tagged
`agent-vX.Y.Z`. The wire protocol is `stintara-agent-v1` for all versions below.

## Unreleased

- Single-instance protection (Windows): a second start with the same `server_ws` and token on the same computer exits immediately with exit code 3 and a clear message, before any network access. The running instance is not affected. The lock is a named Windows mutex derived from a hash of server URL and token (no secret in the name or output); Windows releases it automatically on normal exit, crash or hard kill. Other configurations (different token or server) can run in parallel. If the lock cannot be created the agent starts anyway with a warning. No change to the protocol, `source_epoch` or forwarding. No version bump yet; the version is set when the release is prepared.

## 0.3.0

Lap tyre/fuel provenance and compatibility reporting. **Agents older than 0.3.0 do not send `source_epoch`**; a current
server therefore stores no lap tyre/fuel checkpoints and no measured lapped intervals for them (live timing, strategy
samples and virtual energy are unaffected). Replace older Windows builds, including any unversioned field-test build.

- Adds `source_epoch` (random per Pitwall socket connection, not per Stintara uplink) to the envelope of every forwarded message, so the server can tell a Pitwall restart from a continuous stream (#59). Payloads and the protocol name `stintara-agent-v1` are unchanged.
- `agent_hello` additionally carries `agent` (version, build channel, full revision) and `capabilities` (`source_epoch`). The fields are additive; older servers ignore them.
- A server with compatibility reporting answers in `agent_welcome.compat`; the agent prints it (`compatible` = declaration accepted, not a verification, or `limited` with the restricted functions). Older servers send nothing, which is reported as unknown. Only fixed texts are printed.
- Prints version, build channel and revision at startup (`--version` unchanged in form).
- Compatibility: old servers ignore the new fields and the `source_epoch` key; a server with this change treats lap tyre/fuel data of agents without `source_epoch` as unverified (stored as unknown). Update the server first, then the agents. A mixed fleet stays supported.

## 0.2.0

First Track Map / Race Control agent.

- Spatial scoring fields (`lap_dist`, `pos_x`, `pos_y`, `pos_z`) and `VehicleStatusUpdate`
- Track Map source data
- Normalized `race_control` message transport (forwarded at up to 10 Hz)
- Local Yellow sector state (`clear`, `local_yellow`, `unknown`)
- Safety Car and game phase state
- Player-sector transport
- Explicit identity: `--version` reports agent version, build channel, full git revision and protocol
- Windows file/product version metadata; release ZIP with SHA256 checksum
- Compatibility: existing `stintara-agent.json` remains valid. A Stintara server with PR #39 or newer is required **before** upgrading the Agent, because older servers do not know the `race_control` message type.

## 0.1.0 – Historical baseline

- Initial Windows field-test agent
- LMU Pitwall bridge
- Scoring, lap snapshot and telemetry forwarding
- Existing strategy telemetry (fuel, tyres, virtual energy)
- Original field-test distribution through a GitHub Actions artifact

0.1.0 was published retroactively as a historical baseline. The original CI-built binary is used; no source changes were introduced for that historical release.
