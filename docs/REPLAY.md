# Replay & Scenario Test Harness V0.1

These repository tools use the existing Python 3.13 development environment and `websockets`. They are not bundled in StintaraAgent.exe. The production agent, adapter, server contracts and database schema are unchanged.

## Privacy boundaries

- Raw capture: private, stored under ignored `data/captures/raw/`, never Git.
- Sanitized real capture: still private, stored under `data/captures/sanitized/`, never Git. It is pseudonymized, not certified anonymous or public-safe.
- Compiled real-derived replay: private, under `data/captures/compiled/`, preserving its source classification.
- Public fixtures: only wholly synthetic generator output under `tests/fixtures/replay/`. Hygiene tests reject real, sanitized-real or unknown sources. Do not force-add `data/`.

All tool outputs are exclusive-create: existing files, including raw input, are never overwritten. Choose a new output name on each run. Capture, sanitizer and real-derived compiler CLI output must resolve beneath the repository's `data/` directory, including after resolving symlinks. Protect this directory with appropriate local permissions. No original-to-pseudonym mapping is written. There is no publish, promote, upload or commit command.

The sanitizer replaces `driver_name`, `team_name`, `car_name` and `track_name` recursively with sequential, file-local labels. Equal original values within each field get equal labels; different values get different labels. This is presentation pseudonymization, not driver matching. Numeric timing/gap/lap/fuel/wheel values, IDs, bool/null, finish status, ordering and relative time remain unchanged. Unknown strings remain present in the private output and require review. Audit output contains only fixed schema paths and counts; unknown key names and message types are masked because keys can themselves contain private data. Known substituted fields are omitted from the remaining-string audit. Unknown keys also generate review counts even when their values are numeric. No claim of public safety is made; `public_review_required=true` accompanies sanitized-real data, including compiled output.

## Versioned JSONL

The first record is a strict header with `record_type=header`, `format=stintara-pitwall-capture` or `stintara-protocol-replay`, `version=1`, and `source=synthetic|real|sanitized_real`. Sanitized-real requires the review flag. Extra header metadata is rejected: no absolute recording timestamp, machine/user/path, endpoint or credential is stored there.

Every later record is `{"record_type":"message","t":0.0,"message":{...}}`. Relative seconds must be finite, nonnegative and nondecreasing. Wire messages are parsed JSON objects; original text frames are not duplicated. Non-finite JSON values, malformed envelopes and unsupported versions fail with payload-free errors. Unknown wire messages are retained for diagnosis. A recorder error leaves a valid flushed prefix for inspection; malformed JSON stops recording. Binary frames are ignored. Capture flushes every message and closes on Ctrl+C.

`tools.replay.format` provides pure header/record validation, `loads`/`dumps`, and file `load`/`write`. Offline tools currently load the complete file in memory; record long sessions in manageable files. No arbitrary provider payload is printed by CLI failures.

## Synthetic local demo

From the repository root, using the activated development environment:

```powershell
python -m tools.replay.scenarios demo-race --output data/captures/raw/demo-race.jsonl
python -m tools.replay.compile data/captures/raw/demo-race.jsonl --output data/captures/compiled/demo-race.jsonl
```

The committed `tests/fixtures/replay/demo-race.jsonl` is identical generator output and can instead be used directly. `demo-race` has six invented drivers, position changes, nine lap checkpoints, enough clean samples for the Fuel Foundation, a pit/refuel section, scoring and focus telemetry. It spans 16 relative seconds deliberately, not realistic lap cadence. The smaller optional generator `session-transition` emits Circuit A, empty transient SessionInfo, then Circuit B to exercise the existing normalizer regression; it does not by itself create two complete races.

### Pitwall wire replay through the normal agent

```powershell
python -m tools.replay.pitwall_server tests/fixtures/replay/demo-race.jsonl --speed 1
```

In a separate terminal with a privately provisioned dedicated synthetic test-agent config:

```powershell
$env:PITWALL_WS = 'ws://127.0.0.1:9100?format=json'
python -m agent.stintara_agent --config data/test-agent/stintara-agent.json
```

Default bind is literal loopback `127.0.0.1:9100`, not Pitwall's port 9000. Other literal loopback addresses work. Non-loopback addresses or hostnames require explicit `--allow-remote-bind`; do not expose private captures casually. Every new client starts at the first record and is closed cleanly after the last. Stop the normal agent after a single pass if desired: its unchanged reconnect behavior will start the fixture again.

Wire timing sleeps by `(t - previous_t) / speed`, starting from relative zero. `--speed 5` and `--immediate` are supported for functional checks. **Use 1× for fidelity-critical adapter/agent testing.** The real agent throttles by real monotonic time; accelerated or immediate wire replay changes which samples it forwards. Scheduler/network overhead can also affect boundary decisions at 1×.

### Compiled protocol replay

```powershell
python -m tools.replay.compile tests/fixtures/replay/demo-race.jsonl --output data/captures/compiled/demo-compiled.jsonl
python -m tools.replay.stintara_replay data/captures/compiled/demo-compiled.jsonl --config data/test-agent/stintara-agent.json --speed 5 --hold-open 5
```

Use the existing private agent config/environment resolution (`STINTARA_WS`, `STINTARA_AGENT_TOKEN`); do not put credentials into fixtures. Without `--config`, the normal agent-script default config location is used. The client sends normal `agent_hello`, requires `agent_welcome`, then sends the compiled stream and closes. `--immediate` skips replay sleeps; `--hold-open` defaults to zero and adds real seconds after sending. Holding open does not emit heartbeats or refresh scoring; the existing 15-second live freshness limit still applies.

The compiler calls the production `agent.pitwall.normalize()` and simulates the existing throttle with recorded relative time: scoring 0.2 seconds, telemetry 1.0 second, other normalized kinds unthrottled. Unknown/malformed adapter messages and empty SessionInfo are ignored and counted without payload output. The compiler reports only counts, including throttled messages in `ignored`. Source classification is retained. Protocol records contain exactly `protocol`, `message_type`, `payload`, never an authentication hello or added endpoint/token metadata. Explicit token/authorization/server_ws/pitwall_ws fields nested in protocol payloads are rejected. This is not a general secret detector; real-derived payloads remain private.

**Protocol replay writes real rows in the target database.** Use an isolated local/test/dev instance and a dedicated synthetic test agent, never a production/shared driver identity. No remote deployment or test is performed automatically. Open the target instance's home page (for example `http://localhost:8080/`), choose the synthetic session and focus Driver Alpha to inspect Relative, Head-to-Head and Fuel diagnostics. Stale sessions remain accessible via their direct session link.

Compiled replay at 1×/5×/10×/immediate preserves the compiler's logical agent sampling decisions. It does **not** guarantee identical database timing or all strategy rows: the unchanged server still enforces 1 Hz scoring checkpoints and strategy persistence using actual arrival/database time. Immediate playback can persist only the first strategy sample while retaining the lap snapshots. Range then uses that persisted sample, not necessarily the final replay fuel. Use 1× when examining persistence cadence; accelerated replay is for functional reproduction, not physical timing equivalence. New IDs and receipt timestamps also differ per run. Replaying without official session IDs follows existing reconnect/session matching rules and is not guaranteed to create a fresh session; use isolated test state when repeatability requires it.

## Later real MAPC workflow

No real recording has been performed for this iteration.

1. Start Pitwall normally and run the normal Stintara agent.
2. In another terminal start the additional WebSocket client:

   ```powershell
   python -m tools.replay.capture --output data/captures/raw/session-001.jsonl
   ```

   The default is `ws://127.0.0.1:9000?format=json`; `--pitwall-ws` overrides it privately. The recorder needs no Stintara token and connects only to Pitwall. Status contains message counts, known type counts and duration, never URLs or payloads.
3. Drive the session, then stop recording with Ctrl+C. Keep raw data private.
4. Run the sanitizer and inspect its paths/counts audit:

   ```powershell
   python -m tools.replay.sanitize data/captures/raw/session-001.jsonl --output data/captures/sanitized/session-001.jsonl
   python -m tools.replay.compile data/captures/sanitized/session-001.jsonl --output data/captures/compiled/session-001.jsonl
   ```
5. Review remaining strings privately, reproduce locally through wire replay at 1× or compiled replay, and compare the two paths.
6. For a permanent public regression, separately author a wholly synthetic minimal scenario. Never copy a real-derived capture into the public fixture directory.

Validate parallel recorder/agent operation, full-session capture, sanitizer/audit completeness, 1× wire replay, accelerated compiled replay, pitstop/fuel, finish, negative gap, session transition and disconnect/reconnect on real LMU later. Synthetic scenarios prove no real gap/finish/pit timing, fuel checkpoint timing, lap validity or actual provider frequency. Issues #6/#9/#10, convergence, Fuel-to-Finish, Trackmap, RaceControl and livestream remain untouched.
