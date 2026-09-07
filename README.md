# Gwendolyn Signal Lab

Gwendolyn Signal Lab is a Windows two-lane audio-signal generator, JSON session
player and localhost Director bridge. It can run by itself or be directed by
[Gwendolyn Direct](https://github.com/Nephet2/gwendolyn-direct).

The left and right lanes are independently synthesized. Each lane supports
carrier frequency, primary amplitude modulation, secondary amplitude
modulation, frequency modulation, level and gradual transitions. Operator-owned
presets bound every Director command.

## Important scope

Signal Lab sends stereo audio to an explicitly selected Windows playback
device. It does not connect directly to ReStim or an e-stim device, and it does
not determine whether downstream equipment is suitable. Begin with an ordinary
speaker or headphones, verify routing and signal behaviour, and configure any
downstream equipment independently.

The localhost Director API binds to `127.0.0.1` and is not intended for network
exposure.

## Windows setup

1. Install Python 3.11 or newer.
2. Run `SETUP_SIGNAL_LAB.bat` once.
3. Run `START_SIGNAL_LAB.bat`.
4. Select and latch the intended playback device before enabling monitoring.

Run `RUN_TESTS.bat` to execute the included test suite.

`heart-phase-lab\START_HEART_PHASE_LAB.bat` opens the optional read-only
heart-phase commissioning page using the same virtual environment. A compatible
browser and heart-rate source are still required; the page does not produce
physical output.

## Main features

- Continuous stereo synthesis with independent left and right states.
- Exact Windows audio-device latching; a missing device is not silently replaced.
- Editable frequency, volume, modulation and transition envelopes.
- Import and playback of Electron-style JSON sessions.
- Gradual, one-parameter-at-a-time Director transitions.
- Local Director bridge at `http://127.0.0.1:18766`.
- Optional heart-tempo input supporting 90%, 100% and 200% pacing.
- Headphone-only blinded pacing comparisons.

## Director API

- `GET /health`
- `GET /v1/state`
- `POST /v1/director/command`
- `POST /v1/director/neutral`
- `POST /v1/heart-tempo`

Director commands use semantic values from 0 to 100 within the selected preset.
They may provide a shared `both` state or separate `left` and `right` states.
The live state returned by the bridge is authoritative.

## Reference sessions and privacy

Place compatible JSON files under `reference-sessions`. Imported files may
contain author names, comments or other identifying metadata, so that directory
is ignored by Git except for the neutral example supplied here.

Local audio-device selections, previews, test results and logs are also ignored.

## Related projects

- [Gwendolyn Direct](https://github.com/Nephet2/gwendolyn-direct) provides the optional conversational Director.
- [Vector 1A](https://github.com/Nephet2/restim-vector-live) provides the separate ReStim/Vector control path.

## Status

This is an experimental Windows alpha. Interfaces and JSON contracts may change.

## License

MIT
