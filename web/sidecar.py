"""Entrypoint used by the Tauri desktop shell for its API sidecar."""

from __future__ import annotations

import os
import argparse
import socket
import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=int(os.environ.get("DANICA_WEB_PORT", "8765")))
    args = parser.parse_args()
    run(args.port, args.port == 0)


def run(port: int, choose_port: bool = False) -> None:
    if choose_port:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
    os.environ["DANICA_API_URL"] = f"http://127.0.0.1:{port}"
    uvicorn.run("web.api:app", host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
