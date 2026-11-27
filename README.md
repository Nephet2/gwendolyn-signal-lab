# Signal Lab

Signal Lab is a Windows two-lane audio-signal player. It plays Electron-style
JSON sessions, or textures you set by hand, on independent left (A) and right
(B) lanes. A single-axis funscript can move the balance between the two lanes,
and the playhead can follow a video player.

This standalone edition is fully user-controlled: you choose the texture, the
balance script, the level and the transport. It needs no AI director or other
companion app, and it does not open any network port.

## Important scope

Signal Lab sends stereo audio to a Windows playback device you explicitly
select. It does not connect directly to ReStim or an e-stim device, and it does
not determine whether downstream equipment is suitable. Begin with ordinary
speakers or headphones, check the routing and the signal's behaviour, and
configure any downstream equipment independently.

## Windows setup

1. Install Python 3.11 or newer, with the Tcl/Tk option ticked (it is by default).
2. Run `SETUP_SIGNAL_LAB.bat` once. It creates `.venv` and installs `sounddevice` and `numpy`.
3. Run `START_SIGNAL_LAB.bat`.

Run `RUN_TESTS.bat` to execute the included test suite.

## Getting sound out

1. In **Signal output**, select the playback device and click **Confirm device**. A device that later disappears is never silently replaced.
2. Set **Monitor level**. It is limited to 0–50%, and changes are smoothed.
3. Click **Arm monitor**, then **Play**. The banner at the top shows **DISARMED**, **PAUSED** or **PLAYING**.

**Channel trims** attenuate the left and right lanes separately and are remembered between launches. **Stop** resets the playhead; the scrub bar seeks.

## Texture source

- **Reference JSON** plays the selected session's per-channel texture events: carrier, AM, secondary AM and FM. Put your JSON files in `reference-sessions` (subfolders are fine), or use **Open…** to pick one anywhere. Events marked inactive silence their lane. Authored volume and ramps are replaced by one common level, the active preset's starting volume, so loudness is always set by the monitor level and trims.
- **Manual** sets each lane's texture with sliders on the **Settings** tab, or exact values with **Manual texture…**. **Link channels** mirrors one lane's slider to the other. Manual mode needs no JSON.

The **Reference traces** tab plots the session's carrier, AM, FM and authored volume fields; tick the parameters to overlay. Click the graph to seek. **Settings → Signal preset** chooses the bounds used for the common level and the manual sliders; **Edit / Save As** creates your own.

## Channel balance from a funscript

Click **Open balance script…** and choose a `.funscript`. Position **0** is left only, **100** is right only. `reference-sessions\Balance-Demo.funscript` sweeps from centre to left, then right, and back over eight seconds.

- **Equal power** (default): position 50 gives about 71% amplitude on each lane.
- **Original balance**: both lanes stay at 100% at position 50.
- **Custom**: **Edit crossover…** lets you draw separate A and B gain curves against position. Drag points, double-click to add one, right-click to remove one, and choose linear or smooth interpolation per lane. Tick **Live apply** to hear edits as you make them, or click **Use custom crossover**. Profiles you save are stored in `crossover_profiles.json`.
- **Motion**: **Linear** joins the actions with straight lines; **Gradient** uses a smooth curve that never overshoots the authored positions. **Compare motion** shows both.

The graph shows the two lane gains, or switch it to **Funscript position**. Positions hold before the first and after the last action. Changes ramp smoothly over each 100 ms audio block. **Clear script** removes the balance; changing JSON keeps it loaded.

## Follow a video player

Choose the player and click **Follow video**. The video player then owns Play, Pause and Seek, and the same playhead drives both the JSON and the balance script. If the player reports a file path Windows can open, Signal Lab loads the same-name funscript beside it, for example `clip.name.mp4` → `clip.name.funscript`. **Open balance script…** overrides that or covers streamed and headset-only files.

- **MPC-HC**: enable *Options → Player → Web Interface*. Defaults `127.0.0.1`, port `13579`.
- **HereSphere**: enable its Timestamp Server, port `23554`. Use `127.0.0.1` for PC VR, or the headset's IP address for standalone playback.

If the clock drops out, the monitor pauses. If the video changes, Signal Lab looks for the new script and holds; click **Follow video** again. Bluetooth audio latency is not compensated.

## Other tools

- **Analyser** tab: a live view of the two output lanes.
- **Settings → Render … 10s WAV**: renders previews at 5% gain into `previews\`. They never autoplay.

## Privacy

JSON sessions can contain author names and comments, so `reference-sessions` is ignored by Git except for the two neutral examples. Your device choice, levels, trims, crossover profiles, renders and logs are stored beside the app and are also ignored.

## Status

This is an experimental Windows alpha. Interfaces and JSON contracts may change.
Earlier releases also had a localhost Director bridge for
[Gwendolyn Direct](https://github.com/Nephet2/gwendolyn-direct); this
standalone edition does not start it.

## License

MIT
