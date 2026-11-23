# Danica web frontend

React 19 + TypeScript + Vite UI, with Tauri v2 as the native shell. The Python bridge lives in `web/api.py` and the sidecar entrypoint is `web/sidecar.py`. Tauri manages the compiled sidecar through its v2 shell plugin.

## Development

Install Python dependencies from the repository root with `python -m pip install -e . -r requirements.txt` and web dependencies with `npm install`. Set `DANICA_PYTHON` to that Python interpreter, then run `npm run tauri dev` to launch the native shell, Vite UI, and local API. For browser-only UI work, run `python -m web.sidecar --port 8765` in one terminal and `npm run dev` in another; Vite serves at `http://127.0.0.1:1420` and proxies `/api` to the sidecar.

Build the frontend with `npm run build`. From a source checkout, `danica --web` starts `tauri dev`, including Vite and the API sidecar. In a packaged install it launches the sibling `danica-web.exe`; set `DANICA_TAURI_BIN` to override that path.

## Tauri

Build the native shell with `npm run tauri build`; this runs the Python sidecar build and bundles it alongside the React build. Set `DANICA_PYTHON` to the interpreter with the project and PyInstaller dependencies installed. During development use `npm run tauri dev` (set `DANICA_PYTHON` to the project interpreter if needed).

The bridge exposes health, app metadata, recent sessions, session creation, and chat routes. Web chat uses Danica's headless permission policy: operations that need interactive approval are denied until an approval UI is added.
