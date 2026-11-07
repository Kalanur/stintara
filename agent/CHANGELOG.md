# Stintara Agent changelog

The Agent is versioned independently of the Stintara server. Releases are tagged
`agent-vX.Y.Z`. The wire protocol is `stintara-agent-v1` for all versions below.

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
