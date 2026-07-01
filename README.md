# Livestream Spotter

A lightweight sidecar that watches iRacing telemetry during a live broadcast, detects race events, and timestamps them against your OBS stream timeline — producing a paste-ready list of timestamps for your YouTube description. No more scrubbing back through a multi-hour VOD with a stopwatch to find where the green flag dropped or where you got into a battle.

It reads the iRacing SDK and OBS (via WebSocket) live, and writes a `timestamps.txt` you drop straight into your video description. The goal isn't to write your description for you — it's to hand you a correct, complete skeleton so all that's left is your own commentary.

> **Status: v0.1.x — early.** Feature-complete and validated against real races, but thresholds (especially battle detection) are still being tuned. Expect rough edges and please report them via Issues.

## What it captures

- Green flag / race start, checkered, session transitions
- Your pit stops (consolidated, with tow annotation)
- Your incidents, by severity (track limits, contact, major)
- Battles and overtakes — with driver names
- White flag / last lap
- Black & meatball flags

Because it's a cockpit-view tool, it focuses on events where you or a car right next to you is involved — the things actually on your stream.

## Requirements

- Windows (iRacing + OBS)
- OBS 28+ with the WebSocket server enabled
- iRacing

## Quick start (built release)

1. Download the latest `.zip` from [Releases](https://github.com/mikeschutz/Livestream-Spotter/releases).
2. Unzip it anywhere.
3. In OBS: **Tools → WebSocket Server Settings → Enable**. Note the port and password.
4. Open `config.toml` next to the exe and set the OBS port/password to match.
5. Run `livestream-spotter.exe`. Start OBS and enter an iRacing session in either order; the app auto-arms when both are available.
6. Race. When you're done, open `timestamps.txt` — paste it into your YouTube description.

See the README inside the zip for more detail.

## Run from source

```bash
git clone https://github.com/mikeschutz/Livestream-Spotter.git
cd Livestream-Spotter
pip install -r requirements.txt
python main.py
```

## Configuration

The release includes a commented `config.toml`. The main runtime options are:

```toml
[runtime]
poll_hz = 15.0
timestamp_source = "auto" # "auto", "stream", or "record"
hold_until_output_active = false
race_only_player_events = true
```

`timestamp_source = "auto"` prefers streaming when both OBS outputs are active, then falls back to recording. Choose `"stream"` or `"record"` to require that specific output.

When no selected OBS output is active, `hold_until_output_active = true` buffers events until one starts. `false` emits them immediately with a `00:00` timestamp. The legacy name `hold_until_stream_active` remains supported in v0.1.x but is deprecated and will be removed in v0.2.0.

## How it works

A single decoupled pipeline: a poll loop reads the iRacing SDK, pure detector functions emit events, and sinks render them. Video timestamps come from OBS's `outputDuration` (never iRacing's session clock), so markers line up with your actual stream. Two output renderers share one event stream — plain timestamps (default) and stricter YouTube chapters.

Livestream Spotter can run continuously: it idles cheaply, notices when iRacing enters a session, connects or reconnects to OBS, and captures events only while both are available. See the [application lifecycle](SPEC.md#application-lifecycle) and the rest of [SPEC.md](SPEC.md) for the detailed design.

## Troubleshooting

If Livestream Spotter cannot reach OBS, open **Tools → WebSocket Server Settings** in OBS, enable the WebSocket server, and confirm its port and password match `config.toml`.

## License

MIT — see [LICENSE](LICENSE).
