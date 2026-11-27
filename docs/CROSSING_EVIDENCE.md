# Offline crossing-evidence detector (Issue #94, Phase 1)

**Status: diagnostic tooling only.** It reads private RAW captures offline and reports *evidence*. It computes no start/finish anchor, offers no accepted marker, and is wired into nothing: no agent transport or protocol capability, no server ingest, no database or migration, no public API or UI, and `TrackModel.start_finish_marker()` is unchanged (the Daytona-like regression in `tests/test_trackmap.py` still expects `None`).

## Question it answers

For a recorded Pitwall RAW session: *when does the player's lap counter really rise N → N+1, and which telemetry positions enclose that moment in time?* Nothing more. What the spread of those positions means for a start/finish line is a Phase 2 question that this tool deliberately does not answer.

## Layout

| Module | Role |
| --- | --- |
| `tools/crossing/model.py` | Simulator-independent observations (`ScoringObs`, `TelemetryObs`, `SessionMeta`, `PhaseObs`, `LinkObs`). No provider field names, no driver names. |
| `tools/crossing/detector.py` | Streaming detector over those observations. Bounded memory (telemetry ring buffer, pending list, capped record lists). |
| `tools/crossing/lmu_raw.py` | Separate LMU Pitwall RAW adapter: one line at a time, 32 MiB line ceiling, bulk types (`AllDriversUpdate`, `ElectronicsUpdate`) never parsed, names never copied. |
| `tools/crossing/stats.py` | Percentiles and ground-plane dispersion (radial, longitudinal, lateral). |
| `tools/crossing/cli.py` | Local read-only CLI and versioned JSON report. |

```powershell
python -m tools.crossing data/captures/raw/<capture>.jsonl --output data/reports/<new-name>.json
```

Reports for non-synthetic captures must stay under the ignored `data/` directory and are exclusive-create (same boundary as `tools.replay`). The CLI prints a summary; the JSON carries every transition, wrap, segment and rejection reason.

## Clocks are kept apart

* **Recorder time** (`t` in the capture): transport clock. Only used for gaps, ordering and pairing messages of the same instant.
* **Scoring `session_time`**: only moves on a 5 Hz scoring tick (floor to 0.2 s). Several scoring snapshots per tick are normal; their *positions* are live, their counter/lap-start/lap-distance fields are not. Same tick plus identical tick fields is a repeat; contradicting tick fields are a conflict that blocks any transition spanning them.
* **Telemetry `current_et`**: live, about 16 Hz. This is the clock positions are interpolated on.
* **`lap_start_et`**: the crossing time, on the same scale as the two above. It appears in scoring and in telemetry and the two sources can update at different moments (telemetry before the counter, scoring after it). Both are tracked; the one seen first wins unless they disagree by more than 50 ms (`lap_start_et_conflict`).

## Detection rules

* **Wrap candidate**: player `lap_dist` falls by more than half the track length between two scoring ticks. A wrap without counter increase is reported and never becomes a crossing (formation-lap wraps look like this).
* **Transition**: player counter rises by exactly one between two ticks of the same continuity segment. Decreases and jumps are counted as anomalies and ignored.
* **Crossing time** `T` = the new `lap_start_et`. It must lie within ±0.5 s of the scoring tick interval. A missing update is awaited for 5 s of session time; late updates are used, flagged `lap_start_et_late` and reported (not dropped).
* **Position**: linear interpolation of the telemetry position at `T` between the two samples that enclose it, or the sample itself on an exact timestamp. Never extrapolated. Rejected when there is no enclosing sample, when the bracket is wider than 0.5 s, when recorder and ET deltas disagree, when the step implies an implausible speed, or when two contradictory samples share a timestamp.
* **Context filters** (reason is recorded, the transition stays in the report): pit activity, phase other than green/full-course-yellow (unknown phase is not green), `lap_dist` not near the line, speed below 10 m/s, scoring/telemetry clock offset above 0.5 s, repeated lap transition, vehicle binding inconsistent or ambiguous.
* **Continuity segments** end on: recorder gap > 5 s, recorder/scoring/telemetry time going backwards, game disconnect, layout key change, session kind change, player absent or changed. Open transitions fail with `segment_break_before_resolution`. These segments are **derived diagnostics**, not connection epochs.

## Provenance (what is deliberately *not* claimed)

The Codex review found two P1 risks. They are modelled as separate dimensions and are never converted into trust:

1. **Vehicle binding.** `TelemetryUpdate` has no vehicle id and Pitwall may fall back to another telemetry car. The only evidence available offline is positional: scoring snapshots and telemetry samples stamped with the same recorder instant are compared (the player's scoring position equals its telemetry position). Result per crossing: `position_consistent`, `position_inconsistent`, `ambiguous_multiple_vehicles` (another car within 1 m) or `not_comparable`. Inconsistent and ambiguous exclude the crossing from the filtered population. Even *consistent* only means "same place at the same instant"; report status stays `unverified_position_correlation_only`.
2. **Layout identity.** The track key (name + rounded length from `SessionInfo`) does not prove a layout. A change of the key is a continuity break; an unchanged key is reported as `unverified_track_key_only`.
3. **Source identity.** A RAW file carries no authenticated source and no `source_epoch`. The report states `source_epoch: null`, `unknown_not_recorded_in_history`, `authentication: none_offline_file` and `segments_are_connection_epochs: false`.

## Report (`crossing-evidence-diagnostic-v1`, wrapped as `crossing-evidence-report-v1`)

`counts` (wrap candidates, with/without counter increase, counter increases, anomalies, diagnostic candidates, distinct candidate lap crossings, late updates), `input` (message and duplicate/conflict/regression counters), `rejections` (first reason and any reason), `interval` (width of the interpolated time intervals, distance travelled inside them, lateness), `dispersion` (unfiltered and filtered: radial, longitudinal, lateral and direction spread), `clock_alignment`, `provenance`, `segments`, `wraps`, `crossings`. Fixed fields: `evidence_class: diagnostic_only`, `anchor.computed: false`, `anchor.production_accepted: false`. Crossing statuses are only `diagnostic_candidate` and `diagnostic_rejected`.

Dispersion is spread about the sample's own median centre and mean direction of travel. It is not an error bound for a line: the longitudinal part reflects timing precision, the lateral part mostly the racing line the driver took.

## Tests

`tests/test_crossing_detector.py` and `tests/test_crossing_lmu_raw.py` are wholly synthetic (generated in `tests/crossing_synthetic.py`); no private capture data is committed or derived. They cover wraps without counter increase, N → N+1, duplicates, contradicting same-time samples, late metadata, exact hits, missing enclosing samples, backward time, gaps and reconnects, ambiguous player/vehicle, layout change, pits and formation, bounded memory and the CLI boundary.

## Private Daytona acceptance (local, aggregate numbers only)

Both private RAW captures were evaluated locally with the final code (`python -m tools.crossing ...`, reports kept under the ignored `data/` directory). Raw files, per-lap positions and driver data are not committed.

### Championship capture

| Quantity | Reference | This tool |
| --- | --- | --- |
| Wrap candidates | 43 | 46 (the reference list excluded the three pit-context transitions below; this tool lists every `lap_dist` wrap) |
| Wrap candidates with counter increase | 42 | 45 |
| Wrap candidate without counter increase | 1 (formation) | 1 |
| Counter increases outside the wrap list | 3 (pit context) | 0 outside, but the same 3 are rejected as `pit_context` (45 = 42 + 3 in total, identical to the reference) |
| Rejected | | 3 `pit_context` (one also `lap_dist_not_near_line` and `bracket_position_jump`, the car being in the pit lane), 1 `phase_session_over` (lap completed after the session ended) |
| Diagnostic candidates | 38 | **41**, all `position_consistent` with the scoring player at the same recorder instant (0.0 m deviation), none `ambiguous` |
| Radial spread of the candidates | median 0.990 m, p90 4.245 m, max 4.815 m | median 1.006 m, p90 4.266 m, max 4.836 m (n = 41) |
| Longitudinal / lateral spread (absolute) | n/a | longitudinal median 0.013 m, p90 0.045 m, max 0.068 m; lateral median 1.006 m, p90 4.266 m, max 4.836 m |
| Interpolation interval | n/a | median 0.070 s, p90 0.090 s, max 0.120 s (at most 9.0 m travelled inside an interval) |
| `lap_start_et` arriving after the counter message | 7 | 10 in total (8 among the candidates), all from telemetry; scoring always carried the new value with the counter. Used, flagged and not dropped. |

Why 41 and not 38: this tool applies only the filters listed above and has no access to the earlier pre-filters. A sensitivity check on the report shows the difference is explained by crossings that complete the out-lap after a pit visit: the three candidates 102-110 s after the player was last in the pit (lap 1 of the session and two stop out-laps) are excluded by `pit_cooldown_s` of 120 s (a configuration option, **off by default**). With it the population is 38 with radial median 0.947 m, p90 2.851 m, max 4.815 m. Because the p90 differs from the reference (4.245 m), the two 38-populations are not identical and the reference pre-filters should be treated as unreproduced, not confirmed. The four largest radial offsets (4.3-4.8 m) are **not** pit-related: they lie at least 26 minutes after the last pit presence. The fifth largest (4.27 m) is one of the out-laps mentioned above.

The data is dominated by the lateral component: the longitudinal spread is at most 7 cm, i.e. the crossing *time* given by `lap_start_et` is consistent, while the lateral spread (about 1 m typical) is the line the driver took across the strip. Reading lateral scatter as measurement error would be a mistake; reading it as evidence of the line's lateral position would also be unproven.

Continuity: five derived diagnostic segments, split by two backward telemetry-clock jumps and two session-kind changes; no recorder gap, no layout change, one track key. Counter increases occurred only in the two segments that contained race running. Nothing was lost to a segment break.

### Open-practice capture

9 counter increases, 7 wrap candidates (all with increase), 2 rejected as `pit_context` (the same pit-lane signature as above), 7 diagnostic candidates (`position_consistent`), radial median 1.347 m, p90 1.937 m, max 2.371 m, interval at most 0.09 s, 3 telemetry `lap_start_et` arriving after the counter. Three derived segments (one recorder gap, one session-kind change). A sample of 7 is too small for any conclusion about spread.

### Validated versus diagnostic

*Validated against the data (as observation, not as a claim about the track):* counter transitions are separable from `lap_dist` wraps; scoring ticks at 5 Hz while telemetry is live; the scoring `lap_start_et` accompanies the counter in every case seen and telemetry sometimes follows it later; telemetry and the scoring player position are identical at the same recorder instant in every comparison (44 of 44).

*Diagnostic only, nothing accepted:* every crossing position and every dispersion figure; the vehicle binding (positional agreement is not a vehicle id); layout identity (track name and length only); source continuity (derived segments, `source_epoch` unknown).

### Open provenance and layout risks

1. Telemetry has no vehicle id. In these captures the player's telemetry coincides with the scoring player's position, but a different telemetry vehicle would only be detected when it moves differently from the player; a replay from a spectator or a driver swap with identical positions could not be told apart.
2. The Daytona track key is a name plus a length. A different Daytona layout with the same name and length would not be detected. No layout version or checksum exists in the wire data.
3. Historical captures carry no `source_epoch`; segments here are not connection epochs and cannot be mapped to agent connections.
4. Only one driver's car on one layout in one simulator version was observed.
5. The race-end crossing after `session_over` and pit-lane counter increases show the counter also moves for non-line reasons; the filters are tuned on 45 + 9 events.

### Prerequisites before Phase 2

* Agent/transport must provide an authenticated source identity and a `source_epoch`, plus the vehicle id the telemetry belongs to, so binding is a fact and not an inference.
* A layout identity stronger than name + length (or an explicit statement that it is out of scope), including how a layout change within one session is signalled.
* A decision which physical quantity defines the marker (crossing point on the racing line, or a line fitted across laps) and how lateral line variation is treated; in this sample the time component agrees to within 7 cm along the direction of travel, while the lateral component is not an error figure.
* Captures from more than one driver/car and at least one more circuit to see that the filter set generalises, with synthetic regression cases derived for each new failure.
* An independent review of this PR and of the pre-filters behind the reference numbers.
* The existing `start_finish_marker()` behaviour (still `None` for a Daytona-like arc contradiction) must stay unchanged until those are met.
