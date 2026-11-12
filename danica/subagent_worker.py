"""Entrypoint for a single supervisor-launched subagent."""

import argparse
import json
import socket
import sys

from danica.subagents import run_worker_connection


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--token", required=True)
    parser.add_argument("--console", action="store_true",
                        help="this process owns a console window: print progress there")
    args = parser.parse_args()
    if args.console:
        # A fresh Windows console is often cp1252: unprintable characters would crash the worker
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(errors="replace")
            except Exception:
                pass
    try:
        run_worker_connection(args.host, args.port, args.token, args.console)
    except (OSError, ValueError, json.JSONDecodeError, socket.timeout) as exc:
        print(f"subagent worker failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        if args.console:                  # don't let the window vanish before the error is read
            import time
            time.sleep(30)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
