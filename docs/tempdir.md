Temporary directory usage in Lada

Precedence
- GUI Temporary directory setting (Preferences → Export) overrides other settings for GUI exports.
- CLI `--temp-dir` overrides system temp for that invocation.
- Environment variable `TMPDIR` (or platform-specific) is used by Python's `tempfile.gettempdir()` when GUI/CLI do not override it.

Notes
- The temp directory is used for intermediate `.tmp` video files during export. Make sure the chosen directory is writable and has enough free space.
- The GUI validates and attempts to create the directory when you select it. If it's invalid the GUI will show an error dialog and will not save the invalid path.
- The CLI attempts to validate/create the provided `--temp-dir` at startup. If validation fails, the CLI prints a warning and falls back to the system temp directory.

Example CLI usage

```pwsh
lada --input input.mp4 --output out.mp4 --temp-dir "C:\\Temp\\lada" --frame-rate-mode cfr
```

Implementation details
- The GUI exposes the setting at Preferences → Export. The selected path is persisted into the application's config file.
- The CLI provides `--temp-dir` and `--frame-rate-mode` to match GUI behavior. The frame-rate mode values are `auto`, `cfr`, and `vfr`.
