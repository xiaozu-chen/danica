"""Isolated subprocess entrypoint for one user-managed Python plugin call."""

import argparse
import contextlib
import importlib.util
import json
import sys


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--module", required=True)
    parser.add_argument("--tool", required=True)
    args = parser.parse_args()
    try:
        request = json.loads(sys.stdin.buffer.read(32_001).decode("utf-8"))
        if not isinstance(request, dict) or not isinstance(request.get("arguments"), dict):
            raise ValueError("invalid plugin request")
        spec = importlib.util.spec_from_file_location("danica_user_plugin", args.module)
        if spec is None or spec.loader is None:
            raise ValueError("plugin module could not be loaded")
        module = importlib.util.module_from_spec(spec)
        with contextlib.redirect_stdout(sys.stderr):
            spec.loader.exec_module(module)
            handler = getattr(module, "run", None)
            if not callable(handler):
                raise ValueError("plugin module must define run(tool_name, arguments, workspace)")
            result = handler(args.tool, request["arguments"], request["workspace"])
        sys.stdout.write(json.dumps({"result": result}, ensure_ascii=False))
    except Exception as exc:
        sys.stdout.write(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
