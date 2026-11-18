"""
game_run - play a pygame game headlessly and LOOK at it (see gamerun.py for how it works).

Pair it with the pygame-dev skill. The picture is attached to the model's next step the same way
screenshot's is, and the probe values let a model without vision check the game's state in numbers.
"""

import os
import re
from pathlib import Path

from danica import gamerun, vision
from danica.secret_scan import is_sensitive_path
from danica.tools.files import _write_guard
from danica.tools.registry import tool
from danica.workspace import Workspace

ACTION_KINDS = ("key", "click", "move", "type")


def _check_actions(actions, frames: int) -> str | None:
    """An error message if the action list is malformed, else None (key NAMES are checked by the child)."""
    if not isinstance(actions, list) or len(actions) > gamerun.MAX_ACTIONS:
        return f"ERROR: actions must be a list of at most {gamerun.MAX_ACTIONS} items"
    for n, a in enumerate(actions, start=1):
        if not isinstance(a, dict) or not isinstance(a.get("frame"), int) or a["frame"] < 1:
            return f"ERROR: action {n} needs an integer frame (1 or more): {{\"frame\": 30, \"key\": \"SPACE\"}}"
        kinds = [k for k in ACTION_KINDS if k in a]
        if len(kinds) != 1:
            return f"ERROR: action {n} needs exactly one of: {', '.join(ACTION_KINDS)}"
        if a["frame"] > frames:
            return f"ERROR: action {n} is at frame {a['frame']} but the run only has {frames} frames"
        if kinds[0] in ("click", "move") and not (isinstance(a[kinds[0]], list) and len(a[kinds[0]]) == 2
                                                  and all(isinstance(v, (int, float)) for v in a[kinds[0]])):
            return f"ERROR: action {n}: {kinds[0]} must be [x, y], for example [120, 80]"
        if "hold" in a and (not isinstance(a["hold"], int) or not 1 <= a["hold"] <= 600):
            return f"ERROR: action {n}: hold is a number of frames from 1 to 600"
    return None


@tool(
    "game_run",
    "Run a pygame game HEADLESS (no window opens), play it with scripted input, and see it. Use this instead of "
    "run_shell to test a game: `python main.py` would open a window and never exit. game = the .py file (or its "
    "folder). frames = how many frames to run (60 = one second, default 120, max 1800). A frame is one "
    "pygame.display.flip() / update(); frame 1 is the first. actions = what the player does, a list of "
    "{frame, key|click|move|type}: {\"frame\":10,\"key\":\"SPACE\"} presses a key (hold = frames it stays down, "
    "default 2; use hold 30 to walk for half a second), {\"frame\":20,\"click\":[400,300]} clicks (button 1-3), "
    "{\"frame\":5,\"move\":[10,10]} moves the mouse, {\"frame\":8,\"type\":\"Ana\"} types text. Key names: "
    "SPACE LEFT RIGHT UP DOWN RETURN ESCAPE TAB LSHIFT a-z 0-9 F1-F12. shots = frame numbers to take a picture at "
    "(up to 6; default 3 spread over the run); with several you get ONE labelled contact sheet at out. "
    "probes = Python expressions about the game state (player.rect.y, score, len(enemies)) evaluated at each "
    "picture frame; pixel(x, y) gives the colour on screen. They see globals and the locals of running functions. "
    "Time is fake and fast: every frame lasts exactly 1/fps seconds. The run is deterministic if the game seeds "
    "random. Reports crashes with a traceback, printed text, and how slow the game is.",
    {
        "game": {"type": "string", "description": "The game's .py file, for example games/snake/main.py (or its folder)"},
        "frames": {"type": "integer", "description": "Frames to run (60 = one second)", "default": 120},
        "actions": {"type": "array", "default": [], "description": "Scripted input, see the tool description",
                    "items": {"type": "object", "properties": {
                        "frame": {"type": "integer"}, "key": {"type": "string"}, "click": {"type": "array", "items": {"type": "number"}},
                        "move": {"type": "array", "items": {"type": "number"}}, "type": {"type": "string"},
                        "hold": {"type": "integer"}, "button": {"type": "integer"}}, "required": ["frame"]}},
        "shots": {"type": "array", "default": [], "items": {"type": "integer"},
                  "description": "Frame numbers to take a picture at, for example [1, 60, 120]"},
        "probes": {"type": "array", "default": [], "items": {"type": "string"},
                   "description": "Expressions to evaluate at each picture frame, for example [\"player.y\", \"score\"]"},
        "out": {"type": "string", "description": "Picture to save, for example shots/game.png", "default": "shots/game.png"},
    },
)
def game_run(ws: Workspace, game: str, frames: int = 120, actions: list | None = None, shots: list | None = None,
             probes: list | None = None, out: str = "shots/game.png") -> str:
    actions, shots, probes = actions or [], shots or [], probes or []
    # ---- the game file
    path = ws.resolve(game)
    if path.is_dir():
        path = next((path / n for n in ("main.py", "game.py") if (path / n).is_file()), path / "main.py")
    if not path.is_file() or path.suffix.lower() != ".py":
        return f"ERROR: '{game}' is not a .py file (give the game's main file, for example games/snake/main.py)"
    if is_sensitive_path(ws.rel(path)):
        return "ERROR: that file is blocked: it looks like a credentials file"
    # ---- the numbers
    try:
        frames = int(frames)
    except (TypeError, ValueError):
        return "ERROR: frames must be a whole number"
    frames = max(1, min(frames, gamerun.MAX_FRAMES))
    bad = _check_actions(actions, frames)
    if bad:
        return bad
    if not isinstance(shots, list) or not all(isinstance(s, int) and 1 <= s <= frames for s in shots):
        return f"ERROR: shots must be frame numbers between 1 and {frames}"
    shots = sorted(set(shots))
    if len(shots) > gamerun.MAX_SHOTS:
        return f"ERROR: at most {gamerun.MAX_SHOTS} pictures per run"
    if not shots:                                                  # default: three evenly spread, the last one at the end
        shots = sorted({max(1, frames // 3), max(1, 2 * frames // 3), frames})
    if not isinstance(probes, list) or len(probes) > gamerun.MAX_PROBES or not all(isinstance(p, str) and 0 < len(p) <= 200 for p in probes):
        return f"ERROR: probes must be at most {gamerun.MAX_PROBES} short expressions (strings)"
    # ---- where the picture goes
    target = ws.resolve(out)
    if target.suffix.lower() != ".png":
        return "ERROR: out must end in .png, for example shots/game.png"
    guarded = _write_guard(ws, target)
    if guarded:
        return guarded
    target.parent.mkdir(parents=True, exist_ok=True)
    for old in target.parent.glob(f"{target.stem}_f*.png"):         # pictures of an earlier run must not be mistaken for this one
        if re.fullmatch(rf"{re.escape(target.stem)}_f\d{{4}}\.png", old.name):
            old.unlink(missing_ok=True)

    result = gamerun.run(path, target, frames=frames, actions=actions, shots=shots, probes=probes)
    if result.get("error") and not result.get("status"):          # the run itself failed: no report exists
        log = f"\nThe game printed:\n{result['log']}" if result.get("log") else ""
        return f"ERROR: {result['error']}{log}"

    # ---- pictures: one contact sheet when there are several frames
    taken = result.get("shots") or []
    pictures = [(s["frame"], Path(s["path"])) for s in taken if Path(s["path"]).is_file()]
    shown, how = "", ""
    if len(pictures) == 1:
        os.replace(pictures[0][1], target)                          # a single picture IS the output file
        shown = ws.display(target)
        how = f"frame {pictures[0][0]}"
    elif pictures:
        try:
            gamerun.make_sheet(pictures, target)
            shown = ws.display(target)
            how = "frames " + ", ".join(str(n) for n, _ in pictures) + " side by side; to inspect one closely use view_image on " + \
                  ws.display(pictures[0][1].with_name(f"{target.stem}_f{pictures[0][0]:04d}.png"))
        except Exception as exc:                                    # a missing PIL must not hide the probe values
            shown, how = "", f"(contact sheet failed: {type(exc).__name__})"

    status = result.get("status")
    if status == "crash":
        lines = [f"ERROR: the game crashed at frame {result.get('frames', 0)}:\n{result['error']}"]
        if shown:
            lines.append(f"(the screen when it crashed: {shown})")
    else:
        w, h = (result.get("size") or ["?", "?"])
        perf = result.get("perf") or {}
        lines = [f"OUTPUT: ran {ws.display(path)} for {result.get('frames', 0)} frames ({w}x{h}, {result.get('fps', 60)} fps, "
                 f"\"{result.get('caption', '')}\"). Cost per frame: avg {perf.get('avg_ms', 0)} ms, worst {perf.get('worst_ms', 0)} ms "
                 f"(frame {perf.get('worst_frame', 0)}); a frame has {perf.get('budget_ms', 16.7)} ms."]
        if perf.get("over_budget") and perf["over_budget"] > 0.1 * max(1, result.get("frames", 1)):
            lines.append(f"SLOW: {perf['over_budget']} frames took longer than their budget: look for work done every frame "
                         "that could be done once (loading images or fonts inside the loop, loops over every pixel).")
        if status == "ended" and result.get("frames", 0) < frames:
            lines.append(f"The game ended BY ITSELF at frame {result.get('frames', 0)} of {frames} (it quit or its loop finished). "
                         "If that is not intended, check the quit logic: one of your own actions (ESCAPE, a click on Quit) may have triggered it.")
        if shown:
            attached = ""
            if vision.available():
                queue = getattr(ws, "pending_images", None)
                if queue is None:
                    queue = ws.pending_images = []
                if shown not in queue:
                    queue.append(shown)
                attached = " It is attached to your next step: look at it, list what is wrong, fix the biggest problems."
            else:
                attached = " This model cannot see pictures: judge the game from the probe values and pixel() colours."
            lines.append(f"Picture: {shown} ({how}).{attached}")
    if probes:
        for s in taken:
            if s.get("values"):
                lines.append(f"frame {s['frame']}: " + " | ".join(f"{k} = {v}" for k, v in s["values"].items()))
    if result.get("log"):
        lines.append("The game printed:\n" + result["log"])
    return "\n".join(lines)
