# PLAN — phased build

Build in order. **Each phase ends with output you can eyeball against a real or
replayed session. Do not start a phase until the previous one is verified.** The
hard problems here are empirical (see SPEC → Empirical Risks); phasing exists so
you discover them with instrumentation in hand, not all at once.

---

## Testing strategy (applies to every phase)
- **Develop against iRacing replays, not live races.** Replay playback
  repopulates the SDK shared memory, so detectors see realistic
  position/flag/lap data without you needing to drive a live event. This is the
  primary iteration loop.
- **Mock the OBS clock in test mode.** Replays have no live OBS stream, so behind
  a flag, substitute a synthetic `video_ms` (monotonic from "test start") for
  the real `GetStreamStatus` call. Detector logic is identical; only the
  timestamp source swaps.
- Keep detectors pure (snapshot in → events out) so they can be unit-tested with
  hand-built snapshot pairs, no sim required.

---

## Phase 0 — scaffolding
- Repo layout: `pipeline/` (poll loop, bus), `detectors/`, `sinks/`,
  `obs/`, `config.toml`, `main.py`.
- Config loader. Logging to console.
- **Verify:** runs, loads config, prints a heartbeat tick. Nothing else.

## Phase 1 — connections + raw dump (de-risks the unknowns)
- Connect to irsdk; connect to OBS WebSocket.
- Each tick, dump the fields detectors will need (positions, flags, incident
  count, pit state, lap, `SessionTime`) and the OBS `outputDuration`.
- **Resolve open questions here:** confirm `CarIdxF2Time` semantics vs. computing
  gap from `CarIdxLapDistPct`; confirm `outputDuration` is monotonic; confirm
  `TrackWetness` is present in the build.
- **Verify:** raw values look sane against a replay; OBS time advances 1:1 with
  wall clock while streaming.

## Phase 2 — event bus + reliable structural/flag detectors
- Implement `Event` schema and the bus.
- Detectors: session transitions, green/race start, caution, restart, checkered,
  pit in/out. These are the cleanest signals — build the debounce layer here.
- **Verify:** replay a race; bus emits correct flag/pit/transition events at the
  right moments with correct `video_ms`.

## Phase 3 — Tier 1 player detectors
- Overtakes (with pit-cycle gating), incidents (tiered by delta), towed, battles
  (with throttle).
- Tune thresholds against a recorded session — this is where the empirical risks
  bite. Expect to iterate.
- **Verify:** counts and labels match what you see watching the replay; no pit
  shuffle false-positives; one fight = one battle event.

## Phase 4 — `chapters.txt` sink
- Writer enforcing the YouTube rules: seeded `00:00`, ascending, ≥10 s spacing,
  dedupe/merge, `H:MM:SS` formatting. Sprint/enduro presets.
- **Verify:** paste output into a real YouTube description on a test/unlisted
  upload; confirm chapters render and jump correctly.

## Phase 5 — future seams (stub only)
- `post_to_chat()` stub wired to the bus (no network yet).
- Document the OAuth/`videos.update` path for an end-of-stream description push
  but **do not implement** unless requested.
- **Verify:** stub receives events with correct shape; nothing else.

---

## Definition of done (v1)
Run the tool alongside OBS during a league race; at checkered, `chapters.txt`
contains an accurate, correctly-formatted, paste-ready chapter list with zero
manual editing. That's the whole win. Everything past it is optional.
