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

`GET /api/sessions/{id}/live` gains a `track_map` object (nothing persisted, no new polling loop):
`geometry` (`state`, `complete`, `closed`, `progress`, `points`, `point_sectors`, `sector_boundaries_m`),
`vehicles` (position, pit flag, participant id), `vehicles_age_s`/`vehicles_fresh` (5 s) and `race_control`
(with `age_s`/`fresh`, 5 s). State is held in bounded runtime memory per agent; nothing is written to PostgreSQL.

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

A learned outline is kept per (provider, track name, rounded length) in a bounded in-memory store, so it
survives Practice → Qualifying → Race and simulator reconnects while the server process runs. It is lost on
server restart and never reused for another track.

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
badge and a note only. On desktop the Focus panel moved above the Full Field as a slim strip so the map can use
the freed height; the stacked fallback is unchanged.

`node tools/engineer_demo.cjs --screenshot --track-scenarios --browser msedge` renders synthetic Green, Local Yellow,
Learning and FCY/SC scenarios at 1920×1080, 1920×900, 1920×800 and 2560×1300 into the ignored `build/` folder.

## Known limitations

- Screen orientation (north-up, y = −z) is an assumption; a mirrored map is possible and unverified.
- Full course yellow and session stopped are mapped from the documented enum only (never observed).
- Local yellow does not say whether a car is stopped. Not in scope.
- Geometry is not persisted; a server restart relearns it on the first clean lap.
- Not yet verified in a live LMU session.
