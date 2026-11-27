# Signal Lab v0.8.0-alpha1 (standalone)

A user-controlled standalone edition built from the current Balance Trial player.

New:

- Funscript channel balance: Equal power, Original balance, or a custom A/B crossover you draw, with Linear or Gradient motion.
- Manual texture mode, with per-lane sliders and exact values, needing no JSON.
- Follow video: MPC-HC Web Interface or HereSphere Timestamp Server, with automatic same-name funscript loading.
- Reference traces tab with per-parameter overlays and click-to-seek.
- Analyser tab, channel trims that persist, and a smoothed 0–50% monitor level.
- Neutral `Balance-Demo.funscript` example.

Changed:

- No Director bridge: the app opens no network port and needs no companion app.
- Heart-tempo input, the blinded A/B/C test and the Heart Phase Lab page are removed, since they depended on the bridge.
- `numpy` is now required (Analyser).
