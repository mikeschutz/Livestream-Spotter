==============================================================
 Livestream Spotter  v0.1.0
 Race-event timestamps for your iRacing YouTube streams
==============================================================

WHAT THIS DOES
--------------
While you stream an iRacing race in OBS, this tool watches the race and
writes down the time of every notable event (green flag, your pit stops,
incidents, battles, checkered, etc.) measured against your OBS stream
timeline. When the race is over, you get a "timestamps.txt" file you can
paste straight into your YouTube video description so viewers (and you)
can jump to the action.

This is an early version. It works, but battle detection and a few other
thresholds are still being tuned. Please report anything odd.


WHAT'S IN THIS FOLDER
---------------------
  livestream-spotter.exe   The program. Double-click or run from a terminal.
  config.toml              Settings. EDIT THIS before first run (see below).
  README.txt               This file.

Keep these three together. The program reads config.toml from the same
folder it runs in.


ONE-TIME SETUP
--------------
1. In OBS, go to:  Tools  ->  WebSocket Server Settings
2. Check "Enable WebSocket server".
3. Note the Server Port (default 4455) and set/Show a password if you use one.
4. Open config.toml in any text editor (Notepad is fine).
5. In the [obs] section, set the port and password to match OBS.
   If you use no password, leave it blank.
6. Save config.toml.


RUNNING IT
----------
1. Start your OBS stream OR recording. (Timestamps only advance while OBS
   is actively streaming or recording — that's where the clock comes from.)
2. Run livestream-spotter.exe. It will connect to iRacing and OBS and print
   events as they happen.
3. Drive your race.
4. When finished, stop the program (close the window or press Ctrl+C).
5. Open timestamps.txt (created next to the exe) and copy it into your
   YouTube description.

Tip: the program waits for both an active iRacing session and an active OBS
output. If it says it's waiting, make sure iRacing is in a session and OBS
is streaming/recording.


A NOTE ON THE WINDOWS WARNING
-----------------------------
This exe isn't code-signed yet, so Windows SmartScreen may warn you when you
first run it. Click "More info" -> "Run anyway". (Signing is on the roadmap.)


TROUBLESHOOTING
---------------
"Waiting for an active iRacing session"  ->  Get into a session in iRacing.
"Could not connect to OBS"               ->  Check the WebSocket server is
                                              enabled and the port/password in
                                              config.toml match OBS.
No timestamps.txt appears                 ->  Make sure OBS was actually
                                              streaming/recording while you ran it.


Report problems at:
  https://github.com/mikeschutz/Livestream-Spotter/issues

License: MIT