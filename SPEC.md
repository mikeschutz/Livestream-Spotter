# SPEC — iRacing → YouTube Chapter Marker Tool

## Purpose

A lightweight Python sidecar that runs during a live iRacing broadcast, reads telemetry from the iRacing SDK, detects race events worth marking, and timestamps each one against the **OBS stream timeline**. Primary output is a paste-ready `chapters.txt` for the YouTube description. The same event stream is designed to later feed live-chat posts, overlays, or auto-clipping without rework.

This is a small tool. Keep it small. The architecture below exists only so the output side can grow later — it is not a license to build that growth now.

---

## Architecture

Single decoupled pipeline. This separation is the **one structural decision that matters**; everything else is replaceable.

```
[ irsdk poll loop ]  ->  [ detectors ]  ->  [ event bus ]  ->  [ sinks ]
   (60Hz shared mem)      (pure funcs)      (in-proc queue)     file / chat / overlay
        |                                                          |
   [ OBS WebSocket ] --- supplies video_ms timestamp on demand ---/
```

- **Poll loop**: connects to irsdk shared memory, ticks at a configurable rate (default ~15 Hz — fast enough to catch transient flags, far below the 60 Hz the SDK updates at). Each tick snapshots the fields detectors care about.
- **Detectors**: pure functions over (previous snapshot, current snapshot) -> zero or more events. No I/O. **No clock calls inside detectors** — they emit event _intentions_ (type/label/tier/lap/meta); the pipeline attaches `video_ms` from the OBS clock at emit time. This keeps them pure and unit-testable against hand-built snapshot pairs.
- **Event bus**: a plain in-process queue carrying `Event`s. Detectors publish, sinks subscribe. Do not over-engineer — a `queue.Queue` is fine. (Built for real in Phase 2, when Events first exist; the Phase 1 raw-record queue was a no-op placeholder and is retired.)
- **Sinks**: consume `Event`s. The `chapters.txt` writer arrives in Phase 4; `post_to_chat()` is stubbed (Phase 5). Overlay/clip sinks do not exist yet.
- **Raw diagnostic dump** (the Phase 1 instrumentation) is **off by default** and its write cadence is **decoupled from poll rate** — poll at ~15 Hz for detector responsiveness, but never emit a per-tick JSONL row in steady state. A full per-tick dump produced a "huge" file in ~18 min; it is a debugging tool, not a production path. Production emits only Events.

The OBS client is a **service the pipeline calls**, not a stage. When an event is emitted, the pipeline asks OBS for the current video time and stamps it — the detector never touches OBS.

---

## The OBS sync contract (read this twice)

Video timestamps come from **OBS, never from iRacing.**

- On event creation, call OBS WebSocket `GetStreamStatus`. The response field `outputDuration` (milliseconds since the stream output started) **is** the elapsed video time at that instant. Convert to `HH:MM:SS` for the chapter.
- **Never use irsdk `SessionTime` for video timestamps.** It is seconds since _session_ start, it resets at every session transition, and it has no relationship to the OBS timeline. Using it will silently produce wrong chapters. `SessionTime` may be stored on the event for debugging only.
- `outputDuration` is only valid while the stream output is active (`outputActive == true`). If an event fires before the stream is live, hold it or drop it per config — do not stamp it with a stale/zero duration.
- Optional optimization: capture `outputDuration` once at the first event and compute later stamps from a local monotonic clock + that offset, to avoid a WebSocket round-trip per event. Round-tripping per event is also fine at our event frequency. Verify drift over a 2-hour run before trusting the offset approach (see Empirical Risks).

---

## Event schema

One dataclass. Keep it stable — sinks depend on it.

```python
@dataclass
class Event:
    video_ms: int        # from OBS outputDuration — the timeline position
    event_type: str      # machine key, e.g. "overtake", "caution", "pit_in"
    label: str           # human chapter title, e.g. "P7 — pass for position"
    tier: int            # 1, 2, or 3 (see catalog) — used for filtering/verbosity
    lap: int | None      # race lap when known
    session_time: float  # irsdk SessionTime — DEBUG ONLY, never for video_ms
    lead_in_ms: int      # pre-roll subtracted from video_ms (see Lead-in offsets)
    meta: dict           # detector-specific extras (compound, fuel, car idx, etc.)
```

`label` is what becomes the YouTube chapter title, so detectors own phrasing.

### Lead-in offsets (a marker is a point minus a pre-roll)

A chapter should rarely point at the exact instant a detector fires — the viewer wants the moment to _open just before_ the thing happens, with enough context to make sense of it. So the stamp written to the file is `max(0, video_ms - lead_in_ms)`, and **`lead_in_ms` is per-event-type**, not global:

- **Green / start**: small pre-roll (~2 s) so the chapter opens just before lights-out, not mid-launch.
- **Wreck / incident**: enough lead-in to show the _cause_, not just the bang.
- **Pit in, session transition, checkered**: little or none — the marker is the thing itself.

Defaults live in config per type. Clamp at 0 so an early event never goes negative, and respect the 10 s min-spacing rule _after_ the offset is applied (an offset can pull two markers closer together).

---

## Detector catalog

Selection is filtered by one rule: **this is a cockpit-only stream, so prefer events where the player is the subject or a car right next to the player is.** Distant action that isn't on camera is noise unless it produces a visible consequence (a caution, a restart).

iRacing field references use pyirsdk access (`ir['FieldName']`). Per-car arrays are indexed by car; the player index is `ir['DriverInfo']['DriverCarIdx']`. Class info per driver lives in the `DriverInfo` / `SessionInfo` YAML (`CarClassID`), not in a telemetry array.

### Gap signal (resolved Phase 1)

Inter-car time gap = `CarIdxF2Time[other] - CarIdxF2Time[player]`. Sign convention confirmed against replay: **car ahead → negative, car behind → positive.** Valid **only under green-flag racing** (`SessionState == Racing`); during pace/formation/grid (and replay scrubs) F2 freezes and the value is meaningless — guard on session state before trusting it. The `CarIdxLapDistPct * estLapTime` estimate is a fallback only and is currently suspect (it came out identical to the raw pct delta in the Phase 1 dump — formula bug, verify when the battle detector is built). **Lapped/multiclass caveat, untested:** F2 is time-behind-leader-style, so a lapped neighbor can read as a small F2 gap while being nowhere near on track — the battle detector must cross-check same-lap (`CarIdxLap`) and/or `CarIdxLapDistPct` proximity before declaring a battle.

### Tier 1 — player is the subject, always on camera

|Event|Signal|Fire condition|
|---|---|---|
|Overtake / overtaken|`CarIdxPosition` / `PlayerCarPosition`; multiclass: `PlayerCarClassPosition`|player's position changes **and** neither car is in a pit cycle (see pit-shuffle risk)|
|Battle|**gap = difference of `CarIdxF2Time` values** (resolved in Phase 1; see Gap signal below)|within ~0.5–0.7 s of car directly ahead/behind for > 8–10 s; **throttled** so one scrap = one event|
|Spin / contact / incident|`PlayerCarMyIncidentCount` delta|+1/+2/+4 by point value. **Label by severity, not raw mechanic**: 1x → "Track limits / minor", 2x → "Contact", 4x → "Major incident". **Best-effort contact inference**: on a 2x/4x, if another car is within proximity (reuse battle F2/lap-dist adjacency), label "Contact with [driver]"; else generic. Imperfect — the SDK exposes incident _points_ only, NOT iRacing's race-control message detail ("Car #21 Contact 2x→4x"), which lives in the message/chat stream, not telemetry (see Future directions).|
|Towed (big crash)|`PlayerCarTowTime` > 0|rising edge|
|Pit stop|`OnPitRoad` (player) rising/falling edge|entry and exit; annotate compound/fuel from pit service fields into `meta`|

### Tier 2 — visible, sets the scene

|Event|Signal|Fire condition|
|---|---|---|
|Rain onset / dry-out|`TrackWetness` (and `WeatherDeclaredWet` if present)|state change across threshold (debounce chatter)|
|Day → night|`SessionTimeOfDay` / sun position|crossing dusk/dawn band (enduro only)|
|Restart|`SessionFlags` `greenHeld` / one-to-green, **after a caution**|rising edge; **must NOT fire at/before the initial start** (else the start mislabels as a restart). Gate on a "race has started" latch + a caution having occurred.|
|Personal best lap|`LapBestLapTime` improves / `LapDeltaToSessionBestLap`|new personal best posted|
|Lapped / lapping (multiclass)|`CarIdxLap` deltas filtered by `CarClassID`|faster-class leader passes player, or player laps a backmarker|

### Tier 3 — narrative glue

|Event|Signal|Fire condition|
|---|---|---|
|Caution (offscreen-wreck proxy)|`SessionFlags` caution bit|rising edge|
|White flag / last lap|`SessionFlags` **white bit** (rising edge), race session only|once. Use the white bit, NOT `SessionLapsRemain == 1` — laps-remain is unreliable for timed races (stays large until the timer nearly expires), while the white bit sets when the leader starts the final lap and behaves identically for timed and lap-limited races. Global "race's last lap" semantic, which is what a "last lap" chapter wants.|
|Fuel critical|`FuelLevel` vs laps remaining|projected short with < N laps to go|
|Black / meatball on player|`CarIdxSessionFlags[playerIdx]`|rising edge — **higher priority than its tier suggests**: real user descriptions mark these often (pit-lane speeding black flags etc.); clean, cheap signal, worth surfacing in Phase 3|

### Structural (always logged, low effort, very reliable)

- Session transitions (practice → qual → race): `SessionNum` change + `SessionInfo.Sessions[].SessionType`.
- Green / race start: the **`not-racing → racing` `SessionState` transition is the single anchor**. Do NOT also key on the `SessionFlags` green bit — it races the state flip and double-fires ~1 tick apart. One start = one event. (Replay verification: confirm the marker lands at the actual launch, not grid placement, on both a rolling and a standing start.)
- Checkered: `SessionState == StateCheckered` / checkered bit — **rising edge, fire ONCE** (latched). The bit can set again on later line crossings (e.g. while in the pits); without the latch it double-fires, as seen in live testing.

---

## Output contract — renderers over one event stream

There are **two output formats, both sinks reading the same events** — same `video_ms`, same labels, same tier. They differ only in their rules. The detectors and event layer are identical for both.

**Architectural rule (protect this): detectors stay format-agnostic; ALL format rules live in sinks.** No YouTube-chapter rule may leak upstream into a detector. As long as this holds, output formats can be added, swapped, or run side-by-side at zero cost to the event layer.

### Plain timestamps — the v1 default (`timestamps.txt`)

This is the format actually shipped first, matching how real stream descriptions are written (timestamps need not start at 0:00; YouTube renders them as inline clickable seek links, not the chapter UI). Permissive:

- **No `00:00` seed required**, no minimum count. The writer never fabricates entries.
- **Spacing is a cosmetic dedupe knob**, NOT a YouTube constraint — a tunable "don't list events closer than ~N s" with the tier-tiebreak resolving collisions. Default small; tune to taste.
- Format `H:MM:SS` (or `MM:SS` under an hour). One `TIMESTAMP Label` per line.
- Lead-in still applies: `max(0, video_ms - lead_in_ms)`.

Goal framing: the tool produces a **correct, complete skeleton** (every pit, green, caution, incident, stamped to the right second) that the user then edits and annotates in their own voice. Not "auto-write the final description" — the human commentary ("cold tires, cold tires!") is the point and the tool shouldn't try to generate it. The win is eliminating stopwatch-scrubbing, leaving only the fun part.

**Sink-layer cleanup (published output only; event log stays faithful).** The principle: the raw dump and the event log show events exactly as detected (full driver names, discrete pit_in / pit_out / tow). The published `timestamps.txt` applies presentation cleanup. This keeps granularity available to every other renderer (chapters, post-processing, overlays). Two such behaviors, both living in the timestamps sink:

- **Privacy name abbreviation**: full name in the event log; published output shows first name + last initial ("Seth W."). Pragmatic rule: first token = first name, last token = last name → initial. Imperfect on compound/multi-token names; good enough for privacy, predictable.
- **Pit consolidation (stateful sink)**: collapse a pit_in → … → pit_out sequence into one published line ("Pit stop N", incrementing counter), absorbing a tow inside the window as an annotation ("Pit stop N (towed)"). The sink buffers an open pit sequence until it closes. **End-of-race edge: if the session ends mid-sequence with no closing pit_out, flush the buffered sequence** rather than dropping it. This makes the timestamps sink the first genuinely stateful renderer — justified, but the buffer/flush boundary needs explicit handling.

### Strict YouTube chapters — PARKED (kept, not v1)

The stricter renderer (for the titled scrubber-segment chapter UI). Built earlier; retained as a second sink, not the default. Its rules, when used:

- First line must be `00:00` (seed `00:00 Stream start`); ≥3 entries; each chapter ≥10 s after the previous (hard rule here, not cosmetic); ascending.
- Use when contiguous titled segments are wanted rather than loose jump links.

### Sprint vs enduro tuning (config-driven, not separate code)

- **40-min GT3 sprint**: lean on Tier 1 (overtakes, battles, incidents). Keep it tight; a sprint with 30 markers is noise.
- **2-hour multiclass**: weight toward slower-cadence markers (pit windows, weather, day/night, class-position swings) so the list reads as a race arc instead of 60 near-identical "battle" lines. Raise battle throttle, raise dedupe window.

---

## Live-publish contract

Determined by how YouTube actually behaves on live streams:

- **Description timestamps do NOT become clickable chapters during a live broadcast** — only after the stream is archived and processed. So real-time description editing buys the live audience almost nothing.
- **Live-chat timestamps DO convert to clickable chapters in the replay.** That is the correct mechanism for live jump-back → this is what `post_to_chat()` is for (later phase).
- `videos.update` costs ~50 quota units against a 10k/day default; event frequency makes quota a non-issue. The real cost is OAuth setup, so this is deferred, not core.

**Default workflow:** `chapters.txt` is the source of truth, written live. One description push at stream end (optional, later) means the description is done the instant the checkered flies — no post-production chore. Skip per-event description thrashing; it gains nothing.

---

## Config surface (single file, e.g. `config.toml`)

- poll rate (Hz)
- **per-event-type lead-in offsets (s)** — e.g. `green = 2`, `wreck = 6`, `pit_in = 0` (see Lead-in offsets)
- battle: gap threshold (s), min duration (s), throttle window (s)
- dedupe / min-chapter spacing (s)
- incident tiers to log (e.g. only +4 and above, or all)
- enabled detectors + per-tier verbosity
- output path; profile preset (`sprint` | `enduro`)
- OBS WebSocket host/port/password
- "hold events until stream active" (bool)

---

## Phase 1 gate findings (recorded — real data, not guesses)

- **F2 gap**: resolved — see Gap signal above.
- **`outputDuration` monotonic**: 16,570 samples over ~18 min, **zero regressions**, ~1:1 with wall clock. Safe to stamp events from OBS time. _Still open, deferred to before Phase 4 (writer):_ full 2-hour drift, and the deliberate stream stop/start — a restart begins a new output and `outputDuration` is expected to reset; the writer must not treat that reset as one timeline.
- **`TrackWetness`**: present in build; dry = `1` (matches enum). Wet values / chatter untested (no wet replay) — verify when the rain detector (Tier 2) is built.

## Empirical risks (cannot be specified — must be observed against real runs)

These are the things to watch when validating each phase. Do not pre-solve them blind; instrument, run a real/replay session, then tune.

- **Pit-cycle position shuffle**: running order reshuffles as cars pit, faking overtakes. Gate overtake events on neither car being in a pit cycle; ideally confirm with on-track adjacency via `CarIdxLapDistPct`.
- **Incident-counter noise**: confirm whether minor +1s are worth logging or just clutter.
- **Battle-detector spam**: one long fight must collapse to one event — verify the throttle holds across position swaps within the fight.
- **`outputDuration` behavior**: check it never goes backwards, and confirm what it does if the stream is paused/reconnected; decide offset-vs-round-trip after measuring drift over a full 2-hour run.
- **Lead-in offset tuning**: the right pre-roll per event type can't be reasoned out blind — especially wrecks, where "the moment it became inevitable" is ~2 s before contact in one case and ~8 s in another. Start from config defaults, watch how the resulting chapters open on replays, and adjust per type. The detector fires at the _instant_; choosing how far back to point is a tuning exercise, not a detection problem. Consider whether a couple of incident severities (e.g. spin vs. heavy hit) deserve different lead-ins.
- **`TrackWetness` threshold chatter**: may oscillate near the boundary — needs debounce.
- **Invalid car-array entries**: cars in garage / not in session report 0 or -1 positions; filter before computing deltas.
- **Multiclass indexing**: position arrays are overall; class results require filtering by `CarClassID` from the YAML.

---

## Non-goals (YAGNI — do not build until explicitly requested)

- Live overlay rendering.
- Auto-clip extraction.
- YouTube OAuth / live description push (stub the seam only).
- GUI. A console log + the output file is the interface.
- Detecting "great 3-wide moment" or anything requiring scene understanding — cautions and battles are the pragmatic proxies.

---

## Future directions (captured, intentionally not in v1)

Ideas worth preserving so the v1 architecture doesn't accidentally foreclose them. Do not build these yet; just don't design in a way that blocks them.

- **Auto sprint/enduro profile.** The preset is config-driven for now. Later, read scheduled race length from the session YAML (`SessionInfo.Sessions[].SessionLaps` / `SessionTime`, plus `WeekendInfo`) and pick `sprint` vs `enduro` automatically — falling back to config when ambiguous. Self-explanatory once the YAML is already being parsed for class data, so the cost is low when the time comes.
- **Always-on background daemon.** Eventually run continuously and self-arm: capture only when it detects _both_ an active iRacing session **and** OBS streaming, then stand down when either drops. This is a supervisor wrapped around the existing pipeline (the pipeline already needs both connections), so the v1 design supports it for free as long as connect/disconnect are clean, idempotent, and the loop tolerates either source appearing/disappearing mid-run. Build the connection layer with that lifecycle in mind even though the daemon itself is later.
- **Manual marker via keybind.** A hotkey the user hits to mark "remember this" in the moment — for things no telemetry signal can catch (the great battle, the "cold tires!" moment, a funny radio call). Architecturally trivial: it is just _another detector / event source_ — a keypress emits an event (likely Tier 1, placeholder label the user renames later) onto the same bus, gets the same OBS stamp, flows to whichever sink. Only new plumbing is a hotkey listener; note iRacing does NOT expose custom binds to the SDK, so this is a global key hook or a Stream Deck button, not an in-sim bind. Everything downstream already exists.
- **Strict duration-scaled chapters.** Use the parked strict-chapter renderer to emit a coarse contiguous chapter track — practice / qual / green-first-lap / white-flag-last-lap as segment boundaries, granularity scaling with scheduled race length (feeds directly off the auto sprint/enduro YAML read above). Additive: a second sink + a coarse segmenter, not a rewrite. Both renderers run over the same event stream.
- **Race-control message stream as a richer incident source.** iRacing logs exact incident detail to its in-sim text/race-control log ("Car #21 Contact 2x→4x Total 14/17x"), confirmed visible in testing. This is NOT in the telemetry SDK the tool polls — it lives in the message/chat stream (and the replay/session file). v1 infers contact attribution from car proximity instead. If exact incident attribution ever justifies it, the known path is parsing the message/chat channel or the replay file — a separate integration with its own timing/reliability, not a telemetry field. Banked so it isn't rediscovered.
- **Complex pit/tow/repair sequences.** v1 consolidates a normal pit_in→pit_out into one line and annotates an embedded tow. Edge cases left for later: a long repair during which other events occur (may warrant breaking the single line back out), and unusual multi-stage sequences. Keep the simple consolidation until a real case demands more.
- **Post-checkered pit / finishing in pit lane.** A pit_out after the checkered produced a stray published line in testing. Rare (finishing a race in the pits is uncommon), so deferred — suppress or specially handle post-checkered pit events if it ever matters.