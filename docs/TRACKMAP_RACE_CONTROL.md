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
`geometry` (`state`, `complete`, `closed`, `progress`, `points`, `point_sectors`, `sector_boundaries_m`),
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
- **Database errors** never break ingest: the geometry stays in memory and persistence is retried after 30 s.
- **Historical sessions:** without a fresh observation the session payload is `static_only` with the stored geometry.
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

## UI

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

`node tools/engineer_demo.cjs --viewports --browser msedge` measures the real browser viewports recorded on 2026-11-07,
1920×911 (1080p display, compact desktop path) and 2560×1271 (1440p display, normal desktop path), plus the stress
sizes 1920×900, 1920×800 and 2560×1300, for a horizontal, a vertically encoded and a stored-only scenario (no document
scrolling, no overlapping panels, Head-to-Head/Weather not clipped, PIT settings visible, AUTO orientation, ORIGINAL
toggle and its persistence). It needs Playwright and Edge/Chrome locally and is not part of CI;
`tests/ui.test.cjs` only proves which CSS layout path a viewport takes.

`node tools/engineer_demo.cjs --screenshot --track-scenarios --browser msedge` renders synthetic Green, Local Yellow,
Learning and FCY/SC scenarios at 1920×1080, 1920×900, 1920×800 and 2560×1300 into the ignored `build/` folder.

## Known limitations

- Screen orientation (north-up, y = −z) is an assumption; a mirrored map is possible and unverified.
- Full course yellow and session stopped are mapped from the documented enum only (never observed).
- Local yellow does not say whether a car is stopped. Not in scope.
- A stored geometry is canonical: a wrong but plausible lap that was stored first is not replaced automatically (there is
  no editor or invalidation tool yet; delete the row to relearn).
- At 1920×911 the Strategy panel scrolls internally by about 46 px with the PIT settings kept sticky and visible.
- Per-track orientation lives in the browser only.
- Not yet verified in a live LMU session.
