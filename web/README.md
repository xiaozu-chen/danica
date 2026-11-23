# Danica web frontend

React 19 + TypeScript + Vite UI, with Tauri v2 as the native shell. The Python bridge lives in `web/api.py` and the sidecar entrypoint is `web/sidecar.py`. Tauri manages the compiled sidecar through its v2 shell plugin.

## Development

Install Python dependencies from the repository root and web dependencies with `npm install`. Set `DANICA_PYTHON` to the Python interpreter with project dependencies installed, then run `npm run tauri dev` to launch the native shell, Vite UI, and local API. `npm run dev` alone serves the React view at `http://127.0.0.1:1420`.

Build the frontend with `npm run build`. `danica --web` launches the Tauri desktop executable (set `DANICA_TAURI_BIN` to choose its path, or let it find `web/src-tauri/target/{debug,release}/danica-web.exe`).

## Tauri

Build the native shell with `npm run tauri build`; this runs the Python sidecar build and bundles it alongside the React build. `danica --web` launches the installed `danica-web` Tauri desktop app. During development use `npm run tauri dev` (set `DANICA_PYTHON` to the project interpreter if needed).
