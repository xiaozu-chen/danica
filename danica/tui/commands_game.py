"""
commands_game.py - /play: open the game Danica built in a REAL window so you can play it.

    /play              open the game Danica most recently ran (or main.py / game.py in the workspace)
    /play <file.py>    open that game (a folder works too: its main.py or game.py)
    /play log          show what the game printed (errors appear here if it crashed)
    /play stop         close the game window

It runs detached: Danica keeps working while you play, and the window is closed when Danica exits.
Danica's own tests use the game_run tool instead (headless, no window).

Imported at the bottom of commands.py, like commands_vision.py.
"""

from pathlib import Path

from danica import gamerun
from danica.tui.commands import DESCRIPTIONS, command

DESCRIPTIONS["/play"] = "play the game in a real window"
CHECK_AFTER = 2.0          # seconds: if the game has already died by then, show why (a crash on start-up)


def _find_game(app, raw: str) -> Path | None:
    """The .py file the user means. No argument = the game Danica last ran, else main.py / game.py in the workspace."""
    if raw:
        path = Path(raw.strip('"').strip("'")).expanduser()
        path = path if path.is_absolute() else app.ws.root / path          # relative paths start at the workspace
    elif gamerun.LAST["path"]:
        path = Path(gamerun.LAST["path"])
    else:
        path = app.ws.root
    if path.is_dir():
        path = next((path / n for n in ("main.py", "game.py") if (path / n).is_file()), path / "main.py")
    return path if path.is_file() and path.suffix.lower() == ".py" else None


@command("/play")
def _play(app, args):
    """/play [file.py | log | stop]   plays the game in a real window."""
    word = args.strip()
    if word == "stop":
        app.ui.log("info", "the game window was closed." if gamerun.stop_play() else "no game is open.")
        return
    if word == "log":
        text = gamerun.play_output()
        state = "running" if gamerun.play_running() else "not running"
        app.ui.log("info", f"game ({state}). Output:\n{text or '(nothing printed)'}")
        return
    path = _find_game(app, word)
    if path is None:
        app.ui.log("error", "no game found. Use /play <file.py>, for example /play games/snake/main.py")
        return
    try:
        proc = gamerun.play(path)
    except OSError as e:
        app.ui.log("error", f"could not start the game: {e}")
        return
    app.ui.log("info", f"playing {path.name} in its own window. /play stop closes it, /play log shows its output.")

    def check() -> None:
        """A game that is already gone after a moment crashed on start: show why."""
        if proc.poll() is not None and proc.returncode not in (0, None):
            app.ui.log("error", f"{path.name} stopped with an error:\n{gamerun.play_output() or '(no output)'}")
    app.set_timer(CHECK_AFTER, check)
