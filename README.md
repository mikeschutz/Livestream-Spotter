# Livestream Spotter

A lightweight sidecar that watches iRacing telemetry during a live broadcast, detects race events, and timestamps them against your OBS stream timeline — producing a paste-ready list of timestamps for your YouTube description. No more scrubbing back through a multi-hour VOD with a stopwatch to find where the green flag dropped or where you got into a battle.

It reads the iRacing SDK and OBS (via WebSocket) live, and writes a `timestamps.txt` you drop straight into your video description. The goal isn't to write your description for you — it's to hand you a correct, complete skeleton so all that's left is your own commentary.

> **Status: v0.1.0 — early.** Feature-complete and validated against real races, but thresholds (especially battle detection) are still being tuned. Expect rough edges and please report them via Issues.

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

1. Download the latest `.zip` from [Releases](https://claude.ai/releases).
2. Unzip it anywhere.
3. In OBS: **Tools → WebSocket Server Settings → Enable**. Note the port and password.
4. Open `config.toml` next to the exe and set the OBS port/password to match.
5. Start your OBS stream (or recording), then run `livestream-spotter.exe`.
6. Race. When you're done, open `timestamps.txt` — paste it into your YouTube description.

See the README inside the zip for more detail.

## Run from source

```bash
git clone https://github.com/mikeschutz/Livestream-Spotter.git
cd Livestream-Spotter
pip install -r requirements.txt
python main.py
```

## How it works

A single decoupled pipeline: a poll loop reads the iRacing SDK, pure detector functions emit events, and sinks render them. Video timestamps come from OBS's `outputDuration` (never iRacing's session clock), so markers line up with your actual stream. Two output renderers share one event stream — plain timestamps (default) and stricter YouTube chapters. See [SPEC.md](https://claude.ai/chat/SPEC.md) for the design.

## License

MIT — see [LICENSE](https://claude.ai/chat/LICENSE).