# Sidecar binaries

Release builds look here for a packaged FastAPI process named:

- Windows: `jarvis-sidecar-x86_64-pc-windows-msvc.exe`
- macOS Intel: `jarvis-sidecar-x86_64-apple-darwin`
- macOS Apple Silicon: `jarvis-sidecar-aarch64-apple-darwin`
- Linux: `jarvis-sidecar-x86_64-unknown-linux-gnu`

In development the shell falls back to `uv run python -m app.main` from the
repo root, so you do not need a packaged binary to iterate.

To produce a Windows sidecar with PyInstaller (after approving that dependency):

```bash
uv run pyinstaller --onefile --name jarvis-sidecar-x86_64-pc-windows-msvc -m app.main
```
