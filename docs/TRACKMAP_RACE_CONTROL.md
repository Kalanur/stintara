# Track Map and Race Control (Engineer, experimental)

Read-only live view in the Engineer Dashboard: learned track outline, current vehicle positions and a small
race-control state. LMU Pitwall remains the local data source; the domain code is simulator-neutral and
LMU-specific interpretation lives in `agent/pitwall.py`.

## Transport (additive, `stintara-agent-v1`)

- `ScoringUpdate` vehicles may carry `lap_dist`, `pos_x`, `pos_y`, `pos_z` (finite numbers or null).
  Older agents omit them; the server then shows no vehicles and keeps the track in the learning state.
- New message type `race_control` (adapter output of `VehicleStatusUpdate`):
  `phase` (`pre_session|formation|green|full_course_yellow|session_stopped|session_over|unknown`),
  `sector_flags` (3 × `clear|local_yellow|unknown`, index 0 = S1), `safety_car_active` (bool or null),
  `player_sector` (1–3 or null) and `diagnostics` (raw provider values, never shown as state).
- Deploy the server before the rebuilt agent: an older server rejects the unknown message type.

## API

`GET /api/sessions/{id}/live` gains a `track_map` object (no new polling loop; only the closed geometry is persisted, see
"Persistent geometry"):
`geometry` (`state`, `complete`, `closed`, `progress`, `points`, `point_sectors`, `sector_boundaries_m`, `sector_markers`),
`vehicles` (position, pit flag, participant id), `vehicles_age_s`/`vehicles_fresh` (5 s) and `race_control`
(with `age_s`/`fresh`, 5 s). Vehicles and race control are held in bounded runtime memory per agent and are never
written to PostgreSQL. `geometry.source` is `learned` or `stored`; `static_only: true` marks a session without a fresh
observation whose payload contains the persisted geometry only (no vehicles, no race control).

## Geometry learning rules (`server/app/trackmap.py`)

The outline is learned from the player vehicle only. A running attempt starts at the start/finish line and is
**discarded** on: pit context or unknown pit state, non-finite/missing position or lap distance, lap distance
outside the small tolerance around the line, backwards lap-distance steps, steps over 120 m (unobserved
track), position steps inconsistent with the travelled distance (teleports), too many points, session type
change or session clock rewind. Points are spaced at least `max(8 m, length/500)`, at most 700 points.

A lap wrap is necessary but **not sufficient**: the outline is closed only if the last stored point lies shortly
before the lap end, the restart point shortly after the line, all point gaps are ≤ 60 m and the chord between the
last and first point is consistent with the small arc between them. Otherwise nothing is connected; the panel
shows `LEARNING TRACK` and the next clean lap is used. A partial outline never carries a closing edge.

A learned outline is kept per (provider, track name, rounded length) in a bounded in-memory hot cache, so it
survives Practice → Qualifying → Race and simulator reconnects. It is never reused for another track.

## Persistent geometry (V0.2, `server/app/track_repo.py`)

PostgreSQL table `track_geometries` is the durable source; `TrackStore` stays the hot cache and is filled lazily when a
known track is attached (no global preload).

- **Identity:** `UNIQUE(provider, track_name, track_length_key)` where the key is the rounded track length, i.e. exactly
  the in-memory key. Names are never matched fuzzily; a missing or non-finite length means no persistence and no reuse.
- **Stored:** raw simulator `(x, z)` points, matching lap distances, `length_m`, observed sector observations and derived
  boundaries, `schema_version` (currently `1`), `learned_at`, `updated_at`. Never rotated, never vehicle positions,
  never race-control history.
- **When:** only after the learner accepted a closed, plausible lap. Incomplete or rejected attempts (pit, backwards,
  lap-distance reset, gap, teleport, incomplete arc) are never written.
- **Concurrency:** `INSERT .. ON CONFLICT DO NOTHING`; the first closed lap is canonical and a losing writer adopts the
  stored geometry. Persistence inside a process is serialised by a lock. Sector data may be refreshed afterwards
  (`UPDATE` of the sector columns only).
- **Loading is fail-closed:** the row is re-validated with the learner's rules (finite values, equal counts, point count,
  length plausibility and agreement with the key, strictly increasing distances, gap/teleport/closure checks, known
  schema version). A rejected row is ignored and the track is learned again; a corrupt row of the current schema version
  is replaced by the next closed lap, a row of an unknown (newer) schema version is left untouched. Corrupt sector data
  drops only the sector layer.
- **Resolution:** accepted geometry is quantised to the persisted 0.1 m resolution *before* it is judged; a point whose
  rounded distance reaches the track length or does not advance is dropped, and `save_geometry` re-validates the record
  with `validate_record` so nothing is written that a reload would reject.
- **Database errors** never break ingest: the geometry stays in memory and persistence is retried after 30 s. A row of
  another schema version (or a model that fails its own validation) is a stable `persist_blocked` state: no further
  database work per message, live geometry keeps working, the row is never overwritten. A concurrent change is only a
  short backoff (5 s), not a block. The block lasts until the model leaves the in-memory store (restart).
- **Historical sessions:** without a fresh observation the session payload is `static_only` with the geometry read from
  PostgreSQL and validated; a complete geometry that only exists in the hot cache (unsaved or blocked) is never shown as
  stored.
  The UI shows `STORED TRACK` and `POSITIONS UNAVAILABLE`; it never shows vehicles, flags or a race-control state.
  An unknown track stays unknown.

Sector boundaries are medians of observed forward transitions S1→S2 and S2→S3 of the learning vehicle. At least two
agreeing observations and a majority of consistent ones are required; otherwise the sector layer is unknown and
nothing is invented. S3→S1 is the start/finish line and is not used.

## Race-control mapping and evidence

Verified in two private championship captures (no raw data in this repository):

| Provider value | Meaning used | Evidence |
| --- | --- | --- |
| sector flag `11` | clear | dominant state in all sessions |
| sector flag `1` | local yellow | short, per-sector phases during the green phase; stationary cars were located in the flagged sector |
| sector flag `2`, `3` | **unknown** (never yellow) | occurred only when the session was over |
| `sector_flags[i]` | sector `i+1` | stationary cars under a flag were in sector 1/3 for index 0/2 (100 %) and sector 2 for index 1 (78 %) |
| `player_sector` 1, 2, 0 | S1, S2, S3 | transitions 1→2→0→1 at stable lap distances over 45 laps |
| game phase 0, 3, 5, 8 | pre-session, formation, green, session over | observed |
| game phase 6, 7 | full course yellow, session stopped | **documented provider enum only, never observed** |
| `yellow_flag_state`, `player_under_yellow` | not used | constant while sector flags changed |

Unknown values degrade to `unknown`, are shown only as a muted "flag unrecognized" hint and are kept in
`diagnostics`. A stale race-control feed (>5 s) is shown as stale, never as GREEN. No stopped-car or incident
classification is made.

`geometry.sector_markers` (Track Map V2) is `null` unless the closed geometry has two observed sector boundaries
(`sector_boundaries_m`, the same agreeing-observation rule as before). Each marker is
`{boundary: 1|2, point: [x, z], direction: [dx, dz]}` in raw simulator coordinates: the point on the learned centerline at
that lap distance and the unit direction of the centerline segment there. A boundary that cannot be placed on the sampled
arc yields no markers at all; nothing is guessed from fractions of the track length. The markers are derived, not persisted.

## UI

**Track Map V2 additions.** The focus vehicle gets a `P<position>` label and keeps its ring; the direct opponents are the
adjacent provider positions with a unique match, exactly the Head-to-Head rule (`uniquePositionNeighbor`), never spatial
proximity, so lapped cars, duplicate or unknown positions cannot select the wrong car. They get a dimmer blue ring and a
`P<position>` label; all other vehicles stay unlabelled dots. Identity is `participant_id` only (an unresolved vehicle is
never labelled). Labels are placed greedily (focus first, then ahead, then behind) at the position that collides with the
fewest labels and vehicle dots, with a leader line to their dot; a rival without a free spot is dropped, the focus is always
drawn. Labels appear only while vehicle positions are fresh. Text size is derived from the measured map area (about 12 px);
the page re-renders once when that area changes (window resize, layout settling). Two short ticks across the centerline mark
the observed sector boundaries (`sector_markers`), projected with the same AUTO/ORIGINAL transformation as the outline;
without valid markers nothing is drawn. Class-based opponents are deliberately not part of this (Multiclass, issue #53).

SVG without external libraries, fitted with preserved aspect ratio. World plane (x, z); elevation is ignored.
Focus vehicle in cyan, pit vehicles hollow and dimmed, stale positions hollow gray with `POSITIONS STALE`.
Local-yellow sectors are highlighted on the outline; without a known sector layer the yellow sector is reported by
badge and a note only. A complete centerline can be shown `Orientation: AUTO` (default) or `ORIGINAL`, chosen per track
name in `localStorage` (`stintara:trackmap:orientation:v1`; no server preference). AUTO is presentation only: it takes the
principal axis of the centerline points (centroid and covariance, eigenvector angle `½·atan2(2·sxy, sxx−syy)` in the screen
plane `(x, −z)`) and applies one proper rotation (no mirroring) to centerline, sector overlays, vehicles, focus and pit
markers. Fewer than 8 valid points, no spread, a near-isotropic shape (minor/major standard deviation ≥ 0.9), a rotation
below 0.5° or a partial/open geometry fall back to ORIGINAL. The remaining 180° ambiguity is not resolved. On desktop the Focus panel moved above the Full Field as a slim strip so the map can use
the freed height; the stacked fallback is unchanged.

`node tools/engineer_demo.cjs --viewports --browser msedge` (Track Map V2 additionally: two sector ticks within 4 px of the centerline,
focus label `P3`, no label overlapping a label or a vehicle dot, labels at least 9 px high, no labels on a stored track) measures the real browser viewports recorded on 2026-11-07,
1920×911 (1080p display, compact desktop path) and 2560×1271 (1440p display, normal desktop path), plus the stress
sizes 1920×900, 1920×800 and 2560×1300, for a horizontal, a vertically encoded and a stored-only scenario (no document
scrolling, no overlapping panels, Head-to-Head/Weather not clipped, PIT settings visible, AUTO orientation, ORIGINAL
toggle and its persistence). It needs Playwright and Edge/Chrome locally and is not part of CI;
`tests/ui.test.cjs` only proves which CSS layout path a viewport takes.

`node tools/engineer_demo.cjs --screenshot --track-scenarios --browser msedge` renders synthetic Green, Local Yellow,
Learning and FCY/SC scenarios at 1920×1080, 1920×900, 1920×800 and 2560×1300 into the ignored `build/` folder.

## Latency probe

`node tools/trackmap_latency.cjs --base http://127.0.0.1:8099 --session <id> --seconds 40` drives a headless Chrome against a
local server that is replaying the synthetic `latency-probe` scenario (`python -m tools.replay.scenarios latency-probe
--output data/captures/raw/latency-probe.jsonl`, compile with `tools.replay.compile`, replay with `tools.replay.stintara_replay
--speed 1`). It records, per session poll, the request time, the server-reported `vehicles_age_s`, and the first DOM mutation
and animation frame after the response. Only differences of the same clock are used; `vehicles_age_s` is a server-side
duration and is never subtracted from a browser timestamp, so the probe is also valid against a remote server for age and
render figures. The scoring cadence itself is not measurable from a 2 Hz poll; it is the compiler's agent throttle (0.2 s).

First local measurement (2026-11-09, Chrome 1920×911, one machine, synthetic 14 Hz source, 4.7 Hz forwarded): server age of
the newest position at each poll mean 117 ms, max 220 ms (one scoring interval); poll interval mean 554 ms (500 ms timer plus
the fetch and the sequential fuel request); fetch 10 ms median (p95 38 ms); DOM update 6 ms and paint 11 ms median after the
response (p95 17 ms). Age of the position when painted: mean 130 ms, max 233 ms. Because the page shows each poll result until
the next one, the expected age at a random instant is about 0.4 s and the worst case about 0.8 s. Interpretation: the delay is
dominated by the browser polling interval, not by the agent cadence or rendering. Interpolating between 5 Hz samples would not
help while a new sample reaches the page only about every 0.55 s; a faster, vehicles-only refresh (or push) is the lever and
would be a separate change. No interpolation was implemented.

## Known limitations

- Screen orientation (north-up, y = −z) is an assumption; a mirrored map is possible and unverified.
- Full course yellow and session stopped are mapped from the documented enum only (never observed).
- Local yellow does not say whether a car is stopped. Not in scope.
- A stored geometry is canonical: a wrong but plausible lap that was stored first is not replaced automatically (there is
  no editor or invalidation tool yet; delete the row to relearn).
- At 1920×911 the Strategy panel scrolls internally by about 46 px with the PIT settings kept sticky and visible.
- Per-track orientation lives in the browser only.
- Not yet verified in a live LMU session.
