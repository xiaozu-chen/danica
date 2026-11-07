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
    args = parser.parse_args()
    try:
        run_worker_connection(args.host, args.port, args.token)
    except (OSError, ValueError, json.JSONDecodeError, socket.timeout) as exc:
        print(f"subagent worker failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
