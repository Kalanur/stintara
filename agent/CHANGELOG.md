# Stintara Agent changelog

The Agent is versioned independently of the Stintara server. Releases are tagged
`agent-vX.Y.Z`. The wire protocol is `stintara-agent-v1` for all versions below.

## 0.3.1

Windows single-instance protection. No protocol, server, schema or telemetry change: the wire protocol stays
`stintara-agent-v1`, `source_epoch`, Pitwall forwarding and session behaviour are exactly as in 0.3.0. Servers and
other agents need no change; 0.3.1 can replace 0.3.0 in place (keep the old EXE until the new one is verified).

- A second start with the same `server_ws` and token on the same computer exits immediately with **exit code 3** and a clear message, before any network access. The running instance is not signalled or affected. Different tokens or servers can still run in parallel; there is no override switch.
- The lock is a named Windows mutex (`Global\StintaraAgent-<hash>`) derived from a truncated SHA-256 of the server URL and token, so no secret appears in the name, messages or logs. Windows releases it on normal exit, crash or hard kill (no stale lock). In the onefile build only the Python child holds it, so the bootloader parent and child are not duplicates; Task Manager shows two `StintaraAgent.exe` entries per running agent (normal).
- The mutex is opened with `SYNCHRONIZE` only and an explicit DACL, so an elevated first instance is still detected by a normal second start and vice versa. If `Global\` access is denied while the object exists, the start is refused instead of falling back to an independent `Local\` lock; if nothing exists, protection falls back to the logon session. Any other lock error lets the agent start with a warning (fail-open).
- Not covered: the same token on two different computers (the server still answers `agent already connected`); a process that knows the token-derived name could block starts.
- Validation: Windows single-instance protection was successfully tested with the Agent 0.3.1 CI-built Onefile executable on a Windows desktop, including both UAC elevation scenarios (elevated → standard and standard → elevated). The final release executable was built separately from the same source revision and has not undergone this combined UAC test.

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
