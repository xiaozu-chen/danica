"""
gamerun.py - Run a pygame game WITHOUT a window, play it with scripted input, and take pictures of it.

TWO HALVES in one file (the same idea as shot.py)
  run(...), play(...)   called by Danica. run() starts THIS FILE as a separate Python process, so a crash or an
                        endless loop in the game can never take Danica down, and it is killed after a timeout.
  _child(...)           runs in that process. Self-contained: it imports nothing from danica.

WHAT THE CHILD DOES TO THE GAME (the game's own code is NOT changed)
  * SDL's "dummy" video and audio drivers: no window opens, nothing grabs your keyboard or mouse.
  * A FAKE CLOCK: every frame lasts exactly 1000/fps ms (fps = what the game passes to Clock.tick, default 60)
    and nothing ever sleeps, so a run is fast and gives the same result every time.
    Covered: pygame.time.Clock / get_ticks / wait / delay / set_timer, time.time / perf_counter / sleep.
    NOT covered: time.monotonic.
  * A FRAME is one call to pygame.display.flip() or display.update(). Frame 1 is the first one.
  * SCRIPTED INPUT (`actions`): key presses/holds, mouse clicks and moves, typed text. They arrive as real pygame
    events AND through pygame.key.get_pressed() / pygame.mouse.get_pos() / get_pressed(), so both styles of game work.
  * PICTURES (`shots`): the screen is saved as a PNG at the frames asked for, and once more if the game crashes
    or quits (so there is always a last picture).
  * PROBES: Python expressions (player.rect.y, score, len(enemies)) are evaluated at every picture frame and the
    values are returned. They can see the game's globals AND the local variables of the functions that are running
    (so a `main()` that keeps everything in locals still works). pixel(x, y) gives the colour on screen.
  * NETWORK GUARD: connecting anywhere except this computer raises an error (a guard rail, not a sandbox: the real
    confinement is the Job Object on Windows, the same one run_shell uses).
  * The run stops after `frames` frames (a clean stop) or at the timeout (the process is killed).

SETUP (once):  pip install pygame
"""

from __future__ import annotations

import atexit
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

MAX_FRAMES = 1800                 # 30 seconds of game time at 60 fps
MAX_ACTIONS = 60
MAX_SHOTS = 6
MAX_PROBES = 8
TIMEOUT = 45                      # seconds of REAL time before the game process is killed
LOG_TAIL_LINES = 25
SETUP_HINT = "run: pip install pygame   (in the same Python that runs Danica)"
LAST: dict = {"path": None}       # the game most recently run, so /play with no argument can open it


class GameError(Exception):
    """A problem worded for the model."""


# ------------------------------------------------------------------ the half Danica calls

def _env() -> dict[str, str]:
    """Minimal environment (the same one run_shell uses) plus the switches that make SDL headless."""
    from danica.tools.shell import _clean_env
    env = _clean_env()
    env.update({"SDL_VIDEODRIVER": "dummy", "SDL_AUDIODRIVER": "dummy", "PYGAME_HIDE_SUPPORT_PROMPT": "1"})
    return env


def _spawn(argv: list[str], cwd: Path, log_file, env: dict[str, str]) -> tuple[subprocess.Popen, object | None]:
    """Start confined. Windows: inside a Job Object (fails closed). Elsewhere (tests / Linux): its own process group."""
    kwargs = dict(cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=log_file, stderr=subprocess.STDOUT)
    if os.name == "nt":
        from danica.config import CONFIG
        from danica.sandbox import Job, SandboxError
        limits = CONFIG["sandbox"]
        try:
            job = Job(memory_mb=limits["memory_mb"], max_processes=limits["max_processes"])
            proc = job.popen(argv, creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0), **kwargs)
        except SandboxError as exc:
            raise GameError(f"the sandbox could not start, so nothing was run: {exc}") from None
        return proc, job
    return subprocess.Popen(argv, start_new_session=True, **kwargs), None


def _kill(proc: subprocess.Popen, job) -> None:
    """Stop the process and everything it started."""
    if job is not None:
        job.kill()
        job.close()
    elif proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            proc.kill()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _tail(text: str, lines: int = LOG_TAIL_LINES) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])[-2500:]


def run(game: Path, out_base: Path, frames: int = 120, actions: list[dict] | None = None,
        shots: list[int] | None = None, probes: list[str] | None = None, timeout: int = TIMEOUT) -> dict:
    """Run `game` headless. Returns the child's result dict plus "log" (what the game printed); on a failure to even
    run it, {"ok": False, "error": "..."}. Never raises. Pictures are saved as <out_base stem>_f0040.png."""
    import importlib.util
    if importlib.util.find_spec("pygame") is None:
        return {"ok": False, "error": "pygame is not installed. " + SETUP_HINT}
    LAST["path"] = game
    work = Path(tempfile.mkdtemp(prefix="danica_game_"))
    args = {"game": str(game), "out_base": str(out_base), "frames": frames, "actions": actions or [],
            "shots": shots or [], "probes": probes or [], "result": str(work / "result.json"),
            "heartbeat": str(work / "frame.txt")}
    (work / "args.json").write_text(json.dumps(args), encoding="utf-8")
    log_path = work / "game.log"
    try:
        with open(log_path, "wb") as log_file:
            # -I: ignore the user's site-packages tricks; -B: no .pyc files; -X utf8: printing any text can't crash
            proc, job = _spawn([sys.executable, "-I", "-B", "-X", "utf8", str(Path(__file__).resolve()),
                                str(work / "args.json")], game.parent, log_file, _env())
            timed_out = False
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill(proc, job)
            else:
                if job is not None:
                    job.close()
    except (GameError, OSError) as exc:
        return {"ok": False, "error": str(exc)}
    log = _tail(log_path.read_text(encoding="utf-8", errors="replace")) if log_path.exists() else ""
    try:
        result = json.loads((work / "result.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        result = {}
    if timed_out:
        try:
            reached = int((work / "frame.txt").read_text())
        except (OSError, ValueError):
            reached = 0
        why = ("It reached frame %d, so the game is running but too slowly or the run is too long." % reached
               if reached else "It never finished a single frame: the code probably loops forever before the first "
               "pygame.display.flip(), or waits for input (pygame.event.wait() never returns here).")
        return {"ok": False, "timeout": True, "error": f"the game did not finish within {timeout}s and was killed. {why}",
                "log": log, "frames": reached}
    if not result:
        return {"ok": False, "error": "the game process ended without a report. Last output:\n" + (log or "(none)"), "log": log}
    result["log"] = log
    return result


# ------------------------------------------------------------------ contact sheet (called by tools/game_run.py)

def make_sheet(frames: list[tuple[int, Path]], out: Path, cell_width: int = 420) -> None:
    """One picture with several frames side by side (3 per row), each labelled with its frame number."""
    from PIL import Image, ImageDraw
    cells = []
    for number, path in frames:
        with Image.open(path) as im:
            im = im.convert("RGB")
            scale = min(1.0, cell_width / im.width)
            im = im.resize((max(1, int(im.width * scale)), max(1, int(im.height * scale))), Image.LANCZOS)
            cells.append((number, im))
    cols = min(3, len(cells))
    rows = -(-len(cells) // cols)
    label_h, gap = 18, 6
    cw = max(im.width for _, im in cells)
    ch = max(im.height for _, im in cells) + label_h
    sheet = Image.new("RGB", (cols * cw + (cols + 1) * gap, rows * ch + (rows + 1) * gap), (24, 24, 28))
    draw = ImageDraw.Draw(sheet)
    for n, (number, im) in enumerate(cells):
        x = gap + (n % cols) * (cw + gap)
        y = gap + (n // cols) * (ch + gap)
        draw.text((x + 2, y + 2), f"frame {number}", fill=(235, 235, 235))
        sheet.paste(im, (x, y + label_h))
    sheet.save(out, "PNG", optimize=True)


# ------------------------------------------------------------------ /play: open the game in a REAL window for the user

PLAYING: dict = {"proc": None, "log": None, "game": None}


def _play_env() -> dict[str, str]:
    from danica.tools.shell import _clean_env
    env = _clean_env()
    env["PYGAME_HIDE_SUPPORT_PROMPT"] = "1"
    return env


def play_log_path() -> Path:
    folder = Path(os.environ.get("APPDATA") or tempfile.gettempdir()) / "Danica" / "run"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / "play.log"


def play(game: Path) -> subprocess.Popen:
    """Start the game in a normal window. Not sandboxed: it is the user's own explicit command, like typing
    `python game.py` themselves. Output goes to a log file. Replaces a game that is already open."""
    stop_play()
    log = play_log_path()
    with open(log, "wb") as log_file:
        kwargs = dict(cwd=game.parent, env=_play_env(), stdin=subprocess.DEVNULL, stdout=log_file, stderr=subprocess.STDOUT)
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen([sys.executable, "-X", "utf8", str(game)], **kwargs)
    PLAYING.update(proc=proc, log=log, game=game)
    LAST["path"] = game
    return proc


def play_running() -> bool:
    proc = PLAYING["proc"]
    return proc is not None and proc.poll() is None


def play_output() -> str:
    log = PLAYING["log"]
    try:
        return _tail(log.read_text(encoding="utf-8", errors="replace")) if log else ""
    except OSError:
        return ""


def stop_play() -> bool:
    """Close the game window. True if one was open."""
    proc = PLAYING["proc"]
    was = proc is not None and proc.poll() is None
    if was:
        _kill(proc, None)
    PLAYING["proc"] = None
    return was


atexit.register(stop_play)                 # a game window never outlives Danica


# ------------------------------------------------------------------ the half in the other process

class _Done(BaseException):
    """Raised inside the game when `frames` is reached. BaseException so a game's `except Exception` can't swallow it."""


# Names a person would type -> pygame's constant name
KEY_ALIASES = {"enter": "RETURN", "esc": "ESCAPE", "spacebar": "SPACE", "ctrl": "LCTRL", "control": "LCTRL",
               "shift": "LSHIFT", "alt": "LALT", "del": "DELETE", "backspace": "BACKSPACE", "tab": "TAB",
               "pageup": "PAGEUP", "pagedown": "PAGEDOWN"}


def _key_constant(pygame, name: str) -> int:
    """pygame.K_* for a name like SPACE, left, a, 7, F1, escape. Raises ValueError for an unknown name."""
    text = str(name).strip()
    text = KEY_ALIASES.get(text.lower(), text)
    # a single letter or digit is K_a / K_7 (lower case); everything else is upper case: K_SPACE, K_LEFT, K_F1
    attr = "K_" + (text.lower() if len(text) == 1 else text.upper())
    value = getattr(pygame, attr, None)
    if not isinstance(value, int):
        raise ValueError(f"unknown key '{name}' (use names like SPACE, LEFT, UP, RETURN, ESCAPE, a, 7, F1, LSHIFT)")
    return value


def _child(args_path: str) -> None:
    import socket
    import traceback
    import types
    from collections import defaultdict
    from types import SimpleNamespace

    args = json.loads(Path(args_path).read_text(encoding="utf-8"))
    result: dict = {"ok": False}
    result_path = Path(args["result"])

    def finish() -> None:
        result_path.write_text(json.dumps(result), encoding="utf-8")

    # These must be set BEFORE pygame is imported
    os.environ["SDL_VIDEODRIVER"] = "dummy"
    os.environ["SDL_AUDIODRIVER"] = "dummy"
    os.environ["PYGAME_HIDE_SUPPORT_PROMPT"] = "1"
    try:
        import pygame
    except ImportError:
        result["error"] = "pygame is not installed. " + SETUP_HINT
        finish()
        return

    game = Path(args["game"]).resolve()
    game_dir = str(game.parent)
    os.chdir(game_dir)
    sys.path.insert(0, game_dir)                 # so `import player` finds the game's own modules (-I removes it)

    # ---- the whole state of the run in one place
    real_perf = time.perf_counter                # the harness's own stopwatch must stay real
    S = SimpleNamespace(
        frame=0, ticks=0.0, frame_ms=1000.0 / 60, last_tick_ms=16, max_frames=int(args["frames"]),
        held=set(), mouse_pos=(0, 0), buttons=set(), rel=(0, 0), timers=[], shots=set(args["shots"]),
        taken=[], size=None, caption="", last_real=real_perf(), total=0.0, counted=0, worst=0.0,
        worst_frame=0, over=0, captured_frame=-1, probes=args["probes"],
        base_wall=time.time(), base_perf=real_perf())

    # ---- scripted input, checked BEFORE the game starts so a typo is reported at once
    schedule: dict[int, list] = defaultdict(list)
    problems = []
    for n, a in enumerate(args["actions"], start=1):
        try:
            f = max(1, int(a["frame"]))
            if "key" in a:
                key = _key_constant(pygame, a["key"])
                hold = max(1, int(a.get("hold", 2)))
                schedule[f].append((1, "down", key))
                schedule[f + hold].append((0, "up", key))              # 0 = releases happen before presses in a frame
            elif "click" in a:
                x, y = (int(v) for v in a["click"])
                button = int(a.get("button", 1))
                schedule[f].append((1, "mdown", (x, y), button))
                schedule[f + max(1, int(a.get("hold", 1)))].append((0, "mup", (x, y), button))
            elif "move" in a:
                x, y = (int(v) for v in a["move"])
                schedule[f].append((1, "move", (x, y)))
            elif "type" in a:
                schedule[f].append((1, "type", str(a["type"])))
            else:
                raise ValueError("needs one of key, click, move, type")
        except (KeyError, ValueError, TypeError) as exc:
            problems.append(f"action {n}: {exc}")
    if problems:
        result["error"] = "bad actions: " + "; ".join(problems)
        finish()
        return

    # ---- the fake clock
    class FakeClock:
        """pygame.time.Clock that never sleeps: tick() only records how long a frame is meant to last."""
        def tick(self, framerate=0):
            if framerate:
                S.frame_ms = 1000.0 / framerate
            S.last_tick_ms = int(S.frame_ms)
            return S.last_tick_ms
        tick_busy_loop = tick
        def get_time(self): return S.last_tick_ms
        def get_rawtime(self): return S.last_tick_ms
        def get_fps(self): return 1000.0 / S.frame_ms

    def fake_wait(ms):
        S.ticks += max(0, ms)
        return int(ms)

    def fake_set_timer(event, millis, loops=0):
        kind = getattr(event, "type", event)
        S.timers = [t for t in S.timers if t["kind"] != kind]          # a new timer for the same event replaces the old one
        if millis > 0:
            S.timers.append({"kind": kind, "event": event, "ms": millis, "next": S.ticks + millis, "loops": loops})

    pygame.time.Clock = FakeClock
    pygame.time.get_ticks = lambda: int(S.ticks)
    pygame.time.wait = pygame.time.delay = fake_wait
    pygame.time.set_timer = fake_set_timer
    time.time = lambda: S.base_wall + S.ticks / 1000
    time.perf_counter = lambda: S.base_perf + S.ticks / 1000
    time.sleep = lambda seconds: fake_wait(seconds * 1000)

    # ---- input state (what get_pressed / get_pos report), kept in step with the events we post
    class Held:
        """Stands in for the object pygame.key.get_pressed() returns: keys[pygame.K_LEFT] -> True / False."""
        def __init__(self, keys): self._keys = keys
        def __getitem__(self, key): return key in self._keys
        def __len__(self): return 512

    def mods() -> int:
        value = 0
        for keys, flag in (((pygame.K_LSHIFT, pygame.K_RSHIFT), pygame.KMOD_SHIFT),
                           ((pygame.K_LCTRL, pygame.K_RCTRL), pygame.KMOD_CTRL),
                           ((pygame.K_LALT, pygame.K_RALT), pygame.KMOD_ALT)):
            if any(k in S.held for k in keys):
                value |= flag
        return value

    def get_rel():
        rel, S.rel = S.rel, (0, 0)
        return rel

    pygame.key.get_pressed = lambda: Held(frozenset(S.held))
    pygame.key.get_mods = mods
    pygame.key.get_focused = pygame.mouse.get_focused = lambda: True
    pygame.mouse.get_pos = lambda: S.mouse_pos
    pygame.mouse.get_pressed = lambda num_buttons=3, **kw: tuple((i + 1) in S.buttons for i in range(num_buttons))
    pygame.mouse.get_rel = get_rel

    def post(kind, **attrs):
        pygame.event.post(pygame.event.Event(kind, **attrs))

    def apply(entry) -> None:
        """Turn one scheduled action into real pygame events and input state."""
        what = entry[1]
        if what == "down":
            S.held.add(entry[2])
            post(pygame.KEYDOWN, key=entry[2], mod=mods(), unicode=chr(entry[2]) if 32 <= entry[2] < 127 else "", scancode=0)
        elif what == "up":
            S.held.discard(entry[2])
            post(pygame.KEYUP, key=entry[2], mod=mods(), scancode=0)
        elif what == "move":
            old, S.mouse_pos = S.mouse_pos, entry[2]
            S.rel = (S.mouse_pos[0] - old[0], S.mouse_pos[1] - old[1])
            post(pygame.MOUSEMOTION, pos=S.mouse_pos, rel=S.rel, buttons=tuple((i + 1) in S.buttons for i in range(3)))
        elif what == "mdown":
            apply((1, "move", entry[2]))
            S.buttons.add(entry[3])
            post(pygame.MOUSEBUTTONDOWN, pos=entry[2], button=entry[3])
        elif what == "mup":
            S.buttons.discard(entry[3])
            post(pygame.MOUSEBUTTONUP, pos=entry[2], button=entry[3])
        elif what == "type":
            for ch in entry[2]:
                code = ord(ch.lower()) if ch.isascii() else 0
                post(pygame.KEYDOWN, key=code, mod=0, unicode=ch, scancode=0)
                post(pygame.TEXTINPUT, text=ch)
                post(pygame.KEYUP, key=code, mod=0, scancode=0)

    # ---- no network except this computer
    local_hosts = {"127.0.0.1", "::1", "localhost", "0.0.0.0"}
    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex

    def guard(real):
        def checked(self, address, *rest):
            host = address[0] if isinstance(address, tuple) else address
            if host not in local_hosts:
                raise PermissionError("network access is blocked while Danica tests a game")
            return real(self, address, *rest)
        return checked

    socket.socket.connect, socket.socket.connect_ex = guard(real_connect), guard(real_connect_ex)

    # ---- probes: expressions evaluated against the game's state
    def namespace(main_module):
        """Globals of the game + locals of every game function currently running (innermost wins)."""
        ns = dict(main_module.__dict__)
        running = []
        frame = sys._getframe(1)
        while frame is not None:
            if os.path.abspath(frame.f_code.co_filename).startswith(game_dir) and frame.f_code.co_filename != "<probe>":
                running.append(frame)
            frame = frame.f_back
        for frame in reversed(running):
            ns.update(frame.f_locals)
        screen = pygame.display.get_surface()
        ns.setdefault("screen", screen)
        ns["pixel"] = lambda x, y: tuple(screen.get_at((int(x), int(y))))[:3]     # colour on screen at x, y
        return ns

    def short(value) -> str:
        if isinstance(value, float):
            value = round(value, 3)
        text = repr(value)
        return text if len(text) <= 120 else text[:117] + "..."

    def evaluate() -> dict:
        values = {}
        if not S.probes:
            return values
        ns = namespace(sys.modules["__main__"])
        for expr in S.probes:
            try:
                values[expr] = short(eval(compile(expr, "<probe>", "eval"), ns))
            except Exception as exc:                          # a bad expression is reported, never fatal
                values[expr] = f"<{type(exc).__name__}: {exc}>"
        return values

    # ---- pictures
    def capture(frame: int, with_probes: bool = True) -> None:
        surface = pygame.display.get_surface()
        if surface is None or S.captured_frame == frame:
            return
        path = Path(f"{args['out_base'][:-4] if args['out_base'].endswith('.png') else args['out_base']}_f{frame:04d}.png")
        path.parent.mkdir(parents=True, exist_ok=True)
        pygame.image.save(surface, str(path))
        S.captured_frame = frame
        S.size = list(surface.get_size())
        S.taken.append({"frame": frame, "path": str(path), "values": evaluate() if with_probes else {}})

    # ---- the frame boundary: everything the harness does happens here
    def on_frame() -> None:
        S.frame += 1
        now = real_perf()
        if S.frame > 1:                                      # frame 1 includes loading, so it is not "a frame's cost"
            cost = (now - S.last_real) * 1000
            S.total += cost
            S.counted += 1
            if cost > S.worst:
                S.worst, S.worst_frame = cost, S.frame
            if cost > S.frame_ms:
                S.over += 1
        S.ticks += S.frame_ms
        if not S.caption:
            S.caption = pygame.display.get_caption()[0]
        # fake timers (pygame.time.set_timer) that came due
        for timer in list(S.timers):
            while S.ticks >= timer["next"]:
                event = timer["event"]
                post(event.type, **event.dict) if hasattr(event, "type") else post(event)
                timer["next"] += timer["ms"]
                if timer["loops"]:
                    timer["loops"] -= 1
                    if timer["loops"] == 0:
                        S.timers.remove(timer)
                        break
        if S.frame in S.shots or S.frame >= S.max_frames:
            capture(S.frame)                                 # the picture shows frame N as drawn, BEFORE its input is applied
        for entry in sorted(schedule.pop(S.frame, []), key=lambda e: e[0]):
            apply(entry)
        if S.frame % 10 == 0:
            Path(args["heartbeat"]).write_text(str(S.frame))
        if S.frame >= S.max_frames:
            raise _Done()
        S.last_real = real_perf()                            # time spent in here is not the game's cost

    real_flip, real_update = pygame.display.flip, pygame.display.update
    real_quit, real_display_quit = pygame.quit, pygame.display.quit

    def flip():
        real_flip()
        on_frame()

    def update(*a, **k):
        real_update(*a, **k)
        on_frame()

    def last_picture_then(real):
        """A game that quits on its own still leaves a final picture behind."""
        def wrapper(*a, **k):
            if S.frame > 0:
                capture(S.frame, with_probes=False)
            return real(*a, **k)
        return wrapper

    pygame.display.flip, pygame.display.update = flip, update
    pygame.quit, pygame.display.quit = last_picture_then(real_quit), last_picture_then(real_display_quit)

    # ---- run the game as if it were `python game.py`
    module = types.ModuleType("__main__")
    module.__file__ = str(game)
    sys.modules["__main__"] = module
    sys.argv = [str(game)]
    status, problem = "limit", ""
    try:
        code = compile(game.read_text(encoding="utf-8"), str(game), "exec")
        exec(code, module.__dict__)
        status = "ended"                                     # the script finished by itself before `frames`
    except _Done:
        status = "limit"
    except SystemExit as exc:
        status = "ended"
        if exc.code not in (None, 0):
            status, problem = "crash", f"the game exited with code {exc.code!r}"
    except BaseException as exc:                              # a crash in the GAME: report it with a clean traceback
        status = "crash"
        tb = [f for f in traceback.extract_tb(exc.__traceback__) if f.filename != __file__]
        text = "".join(traceback.format_list(tb)) + "".join(traceback.format_exception_only(type(exc), exc))
        problem = text.replace(game_dir + os.sep, "").replace(game_dir, "")
        try:
            capture(S.frame, with_probes=False)               # what the screen looked like when it died
        except Exception:
            pass

    perf = {"avg_ms": round(S.total / S.counted, 2) if S.counted else 0.0, "worst_ms": round(S.worst, 1),
            "worst_frame": S.worst_frame, "over_budget": S.over, "budget_ms": round(S.frame_ms, 1)}
    result.update(ok=status != "crash", status=status, error=problem, frames=S.frame, size=S.size,
                  caption=S.caption, shots=S.taken, perf=perf, fps=round(1000.0 / S.frame_ms))
    finish()


if __name__ == "__main__":
    _child(sys.argv[1])
