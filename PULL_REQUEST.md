Title: GUI: Add Temporary directory setting, Debug Mode, and frame-rate mode (CFR/VFR); CLI parity

Description:
- Add a Preferences → Export setting to choose a Temporary directory for intermediate export files. If left empty, system temp is used.
- Add a Debug Mode toggle which opens a Debug Console window showing application logs and optional system/GPU stats (psutil and GPUtil).
- Add a Frame rate mode selector (Auto / Constant (CFR) / Variable (VFR)) and ensure the final ffmpeg step uses `-vsync cfr` when CFR is selected.
- Add CLI flags `--temp-dir` and `--frame-rate-mode` so CLI exports can use the same options as the GUI.
- Validate temp directory on selection (GUI) and on CLI startup; show an error dialog in GUI or print a warning in CLI and fall back to system temp when invalid.

Testing steps:
1. GUI: Preferences → Export: choose a temp dir and enable Debug Mode. Start an export and confirm a `.tmp` file appears in the chosen folder and Debug Console shows logs.
2. CLI: `lada --input input.mp4 --output out.mp4 --temp-dir "C:\\Temp\\lada" --frame-rate-mode cfr` — ensures temp dir is used and ffmpeg is called with `-vsync cfr`.

Notes:
- Debug Console requires optional dependencies `psutil` and `GPUtil` for stats. Include them in the packaged exe if you need GPU stats available.
- CLI push/PR may need to be created from your machine; remote push from the automation environment failed earlier (exit status 128).