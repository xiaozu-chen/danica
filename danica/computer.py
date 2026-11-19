"""
computer.py - computer use: Danica looks at an app and operates it with the mouse and keyboard.

THE RULES (everything here is checked on every single action, not once):
  * It is OFF until the user types /computer on, and the user - never the model - approves each window
    (/computer allow). An approval is tied to that exact window and program, not to a title, so another window
    that merely has the same words in its title does not count.
  * Danica only ever SEES the approved window (the picture is cropped to it) and can only click or drag inside it.
    If another program's window covers the spot, the action is refused.
  * Typing and keys only go to an approved window that is in front. Command prompts, PowerShell, Task Manager,
    password managers, the lock screen and pages titled like a sign-in or a bank are always refused.
  * Esc (pressed anywhere in Windows) is a kill swi`tch: it stops the action in progress and turns computer use off.
  * A limit on actions per request, and a short pause between actions.
  * What is on the screen is UNTRUSTED text (a page can say "ignore your instructions"): looking at it taints the
    conversation exactly like a web page does.

HONEST LIMITS: window titles and program names are a filter, not a guarantee; password boxes are only detected in
classic Windows controls; the approved program decides what its own buttons do.

Pictures come with a labelled grid (A1, B3...) so a model that is not pixel-precise can say "click C4".
"""

import math
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from danica import screenctl
from danica.screenctl import ScreenError, Win

MAX_WIDTH = 1280                 # pictures are scaled down to this width (cheaper for the model, still readable)
MAX_TYPE = 400                   # characters per screen_type call
MAX_LOG = 60
KEEP_SHOTS = 30                  # screens/screen_NNNN.png files kept in the workspace

# Programs that are never driven: shells, system tools, password managers, secure desktops.
BLOCKED_PROGRAMS = {
    "cmd.exe", "powershell.exe", "pwsh.exe", "windowsterminal.exe", "wt.exe", "conhost.exe", "bash.exe", "wsl.exe",
    "mintty.exe", "taskmgr.exe", "regedit.exe", "mmc.exe", "services.msc", "secpol.msc", "gpedit.msc", "msconfig.exe",
    "logonui.exe", "consent.exe", "credentialuibroker.exe", "securityhealthsystray.exe", "windowsdefender.exe",
    "keepass.exe", "keepassxc.exe", "1password.exe", "bitwarden.exe", "lastpass.exe", "dashlane.exe", "enpass.exe",
}
# Window titles that look like a sign-in, a payment or a secret.
BLOCKED_TITLE = re.compile(r"(?i)\b(password|passcode|passphrase|sign[ -]?in|log[ -]?in|login|credential|authenticator|"
                           r"two[- ]factor|2fa|one[- ]time code|bank|banking|paypal|checkout|payment|wallet|"
                           r"private key|seed phrase|vault)\b")
# Key combinations that would leave the approved window or end things.
BLOCKED_COMBOS = ({"win"}, {"alt", "tab"}, {"alt", "f4"}, {"ctrl", "alt", "delete"}, {"ctrl", "shift", "escape"})


class ComputerError(Exception):
    """A refusal or failure whose message is written for the model (and shown to the user)."""


@dataclass
class Approval:
    hwnd: int
    pid: int
    process: str
    title: str                   # the title when it was approved (for the list only)


@dataclass
class Mapping:
    """How a picture maps back to the screen: real = origin + picture * scale."""
    hwnd: int
    rect: tuple[int, int, int, int]      # the window's rectangle when the picture was taken
    left: float
    top: float
    scale: float
    width: int                           # picture size
    height: int
    cols: int
    rows: int
    grid: bool
    rects: tuple = ()                    # every rectangle in the picture: the window and the dialogs it owns


def clean_title(title: str, limit: int = 80) -> str:
    """A window title made safe to show: no control characters, no markup, short."""
    text = re.sub(r"[\x00-\x1f\x7f<>]", " ", title or "")
    return re.sub(r"\s+", " ", text).strip()[:limit]


def blocked_reason(win: Win) -> str:
    """Why this window must never be driven, or '' when it may be."""
    if win.process in BLOCKED_PROGRAMS:
        return f"{win.process} is a program Danica never controls (shells, system tools, password managers)"
    if BLOCKED_TITLE.search(win.title or ""):
        return "the window looks like a sign-in, payment or secret page: do that part yourself"
    return ""


def smooth_path(x0: float, y0: float, x1: float, y1: float, min_seconds: float = 0.0) -> list[tuple[int, int, float]]:
    """Mouse positions from (x0,y0) to (x1,y1) as (x, y, pause_after) steps: slow start, fast middle, slow end.
    Short moves take about 0.15 s, long ones up to 0.6 s; min_seconds makes drags slower."""
    dist = math.hypot(x1 - x0, y1 - y0)
    total = max(min_seconds, min(0.6, 0.12 + dist / 3000))
    steps = max(4, int(total / 0.012))
    out = []
    for i in range(1, steps + 1):
        t = i / steps
        e = t * t * (3 - 2 * t)                                  # smoothstep easing
        point = (round(x0 + (x1 - x0) * e), round(y0 + (y1 - y0) * e), total / steps)
        if out and point[:2] == out[-1][:2]:                      # same pixel as before: wait longer there instead
            out[-1] = (out[-1][0], out[-1][1], out[-1][2] + point[2])
        else:
            out.append(point)
    return out


def parse_keys(spec) -> list[list[str]]:
    """'ctrl+s', 'enter', 'ctrl+a ctrl+c' or a list of those -> [['ctrl','s'], ...]. Raises ComputerError for anything odd."""
    parts = spec if isinstance(spec, list) else re.split(r"[\s,]+", str(spec or "").strip())
    combos = []
    for part in parts:
        keys = [k.strip().lower() for k in str(part).split("+") if k.strip()]
        if not keys:
            continue
        keys = ["win" if k in ("windows", "meta", "super", "cmd") else "escape" if k == "esc" else "delete" if k == "del" else k for k in keys]
        if any(set(block) <= set(keys) for block in BLOCKED_COMBOS):
            raise ComputerError(f"'{part}' is not allowed (it would leave the approved window)")
        mods = [k for k in keys[:-1] if k in screenctl.MODIFIERS]
        if len(mods) != len(keys) - 1 or screenctl.key_code(keys[-1]) is None:
            raise ComputerError(f"unknown key '{part}'. Use names like enter, tab, escape, f5, ctrl+s, shift+tab, a, 7")
        combos.append(keys)
    if not combos or len(combos) > 10:
        raise ComputerError("give 1 to 10 key presses, for example \"ctrl+s\" or \"ctrl+a ctrl+c\"")
    return combos


def draw_grid(img, cols: int, rows: int):
    """Draw labelled cells (A1, B1 ...) on a copy of the picture so a model can name a place instead of measuring it."""
    from PIL import Image, ImageDraw, ImageFont
    base = img.convert("RGBA")
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    try:
        font = ImageFont.load_default(size=13)
    except TypeError:                                              # older Pillow: fixed small font
        font = ImageFont.load_default()
    w, h = base.size
    for c in range(cols + 1):
        x = min(w - 1, round(c * w / cols))
        d.line([(x, 0), (x, h)], fill=(255, 0, 80, 110), width=1)
    for r in range(rows + 1):
        y = min(h - 1, round(r * h / rows))
        d.line([(0, y), (w, y)], fill=(255, 0, 80, 110), width=1)
    for r in range(rows):
        for c in range(cols):
            label = f"{chr(65 + c)}{r + 1}"
            x, y = round(c * w / cols) + 3, round(r * h / rows) + 2
            d.text((x, y), label, font=font, fill=(255, 235, 0, 255), stroke_width=2, stroke_fill=(0, 0, 0, 255))
    return Image.alpha_composite(base, layer).convert("RGB")


def draw_marker(img, points, left: float, top: float, scale: float):
    """Green circle + cross where the last click landed, so the model can SEE whether it hit the button."""
    from PIL import ImageDraw
    d = ImageDraw.Draw(img)
    w, h = img.size
    for rx, ry in points:
        x, y = (rx - left) / scale, (ry - top) / scale
        if not (0 <= x < w and 0 <= y < h):
            continue
        for width, color in ((4, (0, 0, 0)), (2, (0, 255, 0))):          # black outline first, then green
            d.ellipse([x - 14, y - 14, x + 14, y + 14], outline=color, width=width)
            d.line([(x - 22, y), (x + 22, y)], fill=color, width=width)
            d.line([(x, y - 22), (x, y + 22)], fill=color, width=width)
    return img


def draw_ruler(img):
    """Tick labels 0..1000 along the top and left edges, so a model that thinks in thousandths can read a position off the picture."""
    from PIL import ImageDraw, ImageFont
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default(size=11)
    except TypeError:
        font = ImageFont.load_default()
    w, h = img.size
    for v in range(0, 1001, 100):
        x, y = min(w - 1, round(v * w / 1000)), min(h - 1, round(v * h / 1000))
        d.line([(x, 0), (x, 6)], fill=(255, 235, 0), width=1)
        d.line([(0, y), (6, y)], fill=(255, 235, 0), width=1)
        d.text((min(max(x + 2, 0), w - 26), 7), str(v), font=font, fill=(255, 235, 0), stroke_width=1, stroke_fill=(0, 0, 0))
        d.text((8, min(max(y - 5, 0), h - 12)), str(v), font=font, fill=(255, 235, 0), stroke_width=1, stroke_fill=(0, 0, 0))
    return img


@dataclass
class Computer:
    """One per session (stored on the workspace). The tools and the /computer command both use it."""
    backend_getter: object = screenctl.get_backend
    enabled: bool = False
    smooth: bool = True
    max_actions: int = 25
    settle: float = 0.35
    grid: bool = True
    coords: str = "normalized"                        # x,y mean 0-1000 across the picture (what Qwen-VL models think in); "pixels" = picture pixels
    approvals: dict = field(default_factory=dict)      # hwnd -> Approval
    mapping: Mapping | None = None
    actions: int = 0
    log: list = field(default_factory=list)
    shot_counter: int = 0
    marker: list = field(default_factory=list)         # real screen points of the last click/drag, drawn once on the next picture
    expect_change: bool = False                        # an action just happened: the next picture should look different
    _thumb: tuple | None = None

    # ------------------------------------------------------------------ state
    @property
    def backend(self):
        try:
            return self.backend_getter()
        except ScreenError as exc:
            raise ComputerError(str(exc))

    def _note(self, text: str) -> None:
        self.log.append(f"{time.strftime('%H:%M:%S')}  {text}")
        del self.log[:-MAX_LOG]

    def enable(self) -> None:
        _ = self.backend                                  # raises when this is not Windows
        self.enabled = True
        self.actions = 0
        self._note("computer use ON")

    def disable(self, why: str = "turned off") -> None:
        if self.enabled:
            self._note(f"computer use OFF ({why})")
        self.enabled, self.mapping = False, None

    def new_request(self) -> None:
        """Called when the user sends a new message: the action budget starts again."""
        self.actions = 0

    def approve(self, win: Win) -> None:
        reason = blocked_reason(win)
        if reason:
            raise ComputerError(f"'{clean_title(win.title)}' cannot be approved: {reason}")
        self.approvals[win.hwnd] = Approval(win.hwnd, win.pid, win.process, clean_title(win.title))
        self._note(f"approved '{clean_title(win.title)}' ({win.process})")

    def revoke(self, hwnd: int | None = None) -> int:
        gone = list(self.approvals) if hwnd is None else [hwnd] if hwnd in self.approvals else []
        for h in gone:
            self._note(f"revoked '{self.approvals[h].title}'")
            del self.approvals[h]
        if self.mapping and (hwnd is None or self.mapping.hwnd == hwnd):
            self.mapping = None
        return len(gone)

    # ------------------------------------------------------------------ checks (run before EVERY action)
    def _kill_check(self) -> None:
        if self.backend.esc_pressed():
            self.disable("Esc pressed")
            raise ComputerError("STOPPED: the user pressed Esc. Computer use is now OFF. Do not try again; tell the user "
                                "where you stopped and wait for them to run /computer on.")

    def _ready(self) -> None:
        if not self.enabled:
            raise ComputerError("computer use is off. Ask the user to type /computer on, then /computer allow <window>.")
        self._kill_check()

    def _spend(self) -> None:
        if self.actions >= self.max_actions:
            raise ComputerError(f"action limit reached ({self.max_actions} per request). Stop, tell the user what is done "
                                "and what is left, and let them send another message to continue.")
        self.actions += 1

    def window(self, hwnd: int) -> Win:
        """The live window for an approval, or a ComputerError when it closed, was replaced, or became blocked."""
        ok = self.approvals.get(hwnd)
        if not ok:
            raise ComputerError("that window is not approved. Ask the user to run /computer allow.")
        win = self.backend.get(hwnd)
        if not win or win.pid != ok.pid or win.process != ok.process:
            self.revoke(hwnd)
            raise ComputerError(f"the approved window '{ok.title}' is gone (closed or replaced). Ask the user to approve it again.")
        reason = blocked_reason(win)
        if reason:
            raise ComputerError(f"stopped: {reason}.")
        return win

    def approved_windows(self) -> list[Win]:
        out = []
        for hwnd in list(self.approvals):
            try:
                out.append(self.window(hwnd))
            except ComputerError:
                continue
        return out

    def pick(self, title: str | None = None) -> Win:
        """Which approved window a tool call means: by title part, else the one from the last picture, else the only one."""
        wins = self.approved_windows()
        if not wins:
            raise ComputerError("no approved window is open. Ask the user to run /computer allow <window>.")
        if title:
            hits = [w for w in wins if title.lower() in w.title.lower() or title.lower() in w.process]
            if len(hits) == 1:
                return hits[0]
            names = ", ".join(f"'{clean_title(w.title, 40)}'" for w in (hits or wins))
            raise ComputerError((f"'{title}' matches several approved windows: {names}" if hits else
                                 f"no approved window matches '{title}'. Approved: {names}"))
        if self.mapping:
            for w in wins:
                if w.hwnd == self.mapping.hwnd:
                    return w
        if len(wins) == 1:
            return wins[0]
        raise ComputerError("several windows are approved: say which one (window=...). Approved: "
                            + ", ".join(f"'{clean_title(w.title, 40)}'" for w in wins))

    def _owned(self, hwnd: int, owner: int) -> bool:
        """Is `hwnd` the window `owner` itself, or a dialog/popup that belongs to it (Save As, Open, a menu)?"""
        for _ in range(6):
            if hwnd == owner:
                return True
            hwnd = self.backend.owner_of(hwnd)
            if not hwnd:
                return False
        return False

    def _frame(self, win: Win) -> tuple[tuple, list]:
        """(bounding rectangle, [rectangles]) of the approved window plus the dialogs it owns, so a Save As box that sits
        partly outside the window is in the picture too. Other windows of the same program are NOT included."""
        rects = [tuple(win.rect)]
        for w in self.backend.list_windows():
            if w.hwnd == win.hwnd or w.minimized or blocked_reason(w) or not self._owned(w.hwnd, win.hwnd):
                continue
            l, t, r, b = w.rect
            if r - l >= 40 and b - t >= 30:
                rects.append(tuple(w.rect))
            if len(rects) >= 5:
                break
        union = (min(r[0] for r in rects), min(r[1] for r in rects), max(r[2] for r in rects), max(r[3] for r in rects))
        if union[2] - union[0] > 4000 or union[3] - union[1] > 3000:          # absurdly large: just the window
            return tuple(win.rect), [tuple(win.rect)]
        return union, rects

    def _front(self, win: Win) -> Win:
        """Make sure the approved window (or a dialog of it) is in front, raising it if needed, and return it fresh."""
        fg = self.backend.foreground()
        if win.minimized or not (fg == win.hwnd or self._owned(fg, win.hwnd)):
            if not self.backend.focus(win.hwnd):
                fg = self.backend.foreground()
                if not self._owned(fg, win.hwnd):
                    raise ComputerError(f"could not bring '{clean_title(win.title)}' to the front. Ask the user to click on it.")
        return self.window(win.hwnd)

    def _covered(self, win: Win) -> float:
        """The share of sample points of the window that show the window itself (1.0 = nothing in front of it)."""
        l, t, r, b = win.rect
        pts = [(l + (r - l) * (i + 0.5) / 6, t + (b - t) * (j + 0.5) / 4) for i in range(6) for j in range(4)]
        seen = same = 0
        for x, y in pts:
            hit = self.backend.window_at(int(x), int(y))
            if hit is None:
                continue
            seen += 1
            same += self._owned(hit[0], win.hwnd)
        return same / seen if seen else 1.0

    # ------------------------------------------------------------------ the picture
    def shot(self, title: str | None, folder: Path, region=None, grid: bool | None = None, spend: bool = False) -> tuple[Path, str]:
        """Take a picture of an approved window (or of a part of the last picture). Returns (file, description)."""
        self._ready()
        win = self.pick(title)
        win = self._front(win)
        if spend:
            self._spend()
        share = self._covered(win)
        if share < 0.8:
            raise ComputerError(f"'{clean_title(win.title)}' is partly hidden behind another program's window. Ask the user to "
                                "move that window away; Danica never shows anything outside the approved window.")
        union, rects = self._frame(win)
        l, t, r, b = union
        left, top, right, bottom = float(l), float(t), float(r), float(b)
        if region is not None:                                           # zoom: a part of the last picture
            m = self.mapping
            if not m or m.hwnd != win.hwnd or m.rect != union or m.rects != tuple(rects):
                raise ComputerError("zoom needs a recent full picture of this window first: call screen_shot without region.")
            try:
                x, y, w, h = (float(v) for v in region)
            except (TypeError, ValueError):
                raise ComputerError("region must be [x, y, width, height] in the same units as positions (0-1000 across the picture)")
            if self.coords == "normalized":                              # thousandths of the picture -> picture pixels
                x, w, y, h = x * m.width / 1000, w * m.width / 1000, y * m.height / 1000, h * m.height / 1000
            x, y = max(0.0, x), max(0.0, y)
            w, h = min(w, m.width - x), min(h, m.height - y)
            if w < 20 or h < 20:
                raise ComputerError("region is too small or outside the picture")
            left, top = m.left + x * m.scale, m.top + y * m.scale
            right, bottom = left + w * m.scale, top + h * m.scale
        img = self.backend.capture((round(left), round(top), round(right), round(bottom)))
        if len(rects) > 1:                                                # blank out the gaps between the window and its dialogs
            from PIL import Image
            clean = Image.new("RGB", img.size, (0, 0, 0))
            for rl, rt, rr, rb in rects:
                box = (max(0, rl - round(left)), max(0, rt - round(top)), min(img.size[0], rr - round(left)), min(img.size[1], rb - round(top)))
                if box[2] > box[0] and box[3] > box[1]:
                    clean.paste(img.crop(box), (box[0], box[1]))
            img = clean
        real_w, real_h = img.size
        target_w = min(MAX_WIDTH, real_w * 3) if region is not None else min(MAX_WIDTH, real_w)
        pic_w = max(1, int(target_w))
        pic_h = max(1, round(real_h * pic_w / real_w))
        if (pic_w, pic_h) != img.size:
            from PIL import Image
            img = img.resize((pic_w, pic_h), Image.LANCZOS)
        notes = []
        small = img.convert("L").resize((96, 96))                          # did anything change since the last picture?
        key = (win.hwnd, union)
        if self.expect_change and self._thumb and self._thumb[0] == key:
            from PIL import ImageChops
            changed = sum(1 for v in ImageChops.difference(self._thumb[1], small).getdata() if v > 24) / (96 * 96)
            notes.append("WARNING: the screen did NOT change after the last action. If you expected a change, the action probably "
                         "missed or was ignored: do not assume it worked; re-aim and try again." if changed < 0.002 else
                         f"The screen changed ({changed:.0%} of the window).")
        self.expect_change, self._thumb = False, (key, small)
        use_grid = self.grid if grid is None else bool(grid)
        cols = 12 if region is None else 8
        rows = max(3, min(12, round(cols * pic_h / pic_w)))
        if use_grid:
            img = draw_grid(img, cols, rows)
            if self.coords == "normalized":
                img = draw_ruler(img)
        scale = (right - left) / pic_w
        if self.marker:
            img = draw_marker(img, self.marker, left, top, scale)
            notes.append("The green circle marks where the last click landed: if it is not on the thing you meant, aim again.")
            self.marker = []
        self.mapping = Mapping(win.hwnd, union, left, top, scale, pic_w, pic_h, cols, rows, use_grid, tuple(rects))
        folder.mkdir(parents=True, exist_ok=True)
        self.shot_counter += 1
        path = folder / f"screen_{self.shot_counter:04d}.png"
        img.save(path)
        for old in sorted(folder.glob("screen_????.png"))[:-KEEP_SHOTS]:          # only the latest ones stay in the workspace
            old.unlink(missing_ok=True)
        text = (f"'{clean_title(win.title)}' {pic_w}x{pic_h}"
                + (f", grid {cols}x{rows} (cells A1..{chr(64 + cols)}{rows})" if use_grid else "")
                + (". Give positions as x,y from 0 to 1000 across the picture (0,0 = top-left, 1000,1000 = bottom-right; the ruler "
                   "on the edges shows it) or as a grid cell like C4." if self.coords == "normalized" else
                   ". Give positions as x,y in this picture's pixels or as a grid cell like C4.")
                + (f" It also shows {len(rects) - 1} dialog window(s) of this program." if len(rects) > 1 else "")
                + ("".join(" " + n for n in notes)))
        self._note(f"picture of '{clean_title(win.title)}'")
        return path, text

    # ------------------------------------------------------------------ positions
    def point(self, x=None, y=None, cell=None) -> tuple[float, float]:
        """A picture position (x,y or a grid cell like 'C4') -> real screen coordinates. Checks it is inside the window,
        that the picture is still current, and that nothing else is on top of that spot."""
        m = self.mapping
        if not m:
            raise ComputerError("take a screen_shot first: positions are given in the picture")
        if cell:
            hit = re.fullmatch(r"([A-Za-z])(\d{1,2})", str(cell).strip())
            if not hit or not m.grid:
                raise ComputerError("a cell like C4 needs a picture with the grid (screen_shot with grid=true)")
            col, row = ord(hit.group(1).upper()) - 65, int(hit.group(2)) - 1
            if not (0 <= col < m.cols and 0 <= row < m.rows):
                raise ComputerError(f"cell {cell} is outside the grid (A1..{chr(64 + m.cols)}{m.rows})")
            x, y = (col + 0.5) * m.width / m.cols, (row + 0.5) * m.height / m.rows
        try:
            x, y = float(x), float(y)
        except (TypeError, ValueError):
            raise ComputerError("give x and y (numbers in the picture) or a cell like C4")
        if self.coords == "normalized" and not cell:
            if not (0 <= x <= 1000 and 0 <= y <= 1000):
                raise ComputerError(f"({x:.0f}, {y:.0f}) is outside the picture: x and y go from 0 to 1000 (0,0 = top-left, "
                                    "1000,1000 = bottom-right). Look at the ruler on the picture's edges, or use a grid cell like C4.")
            x, y = min(x * m.width / 1000, m.width - 1), min(y * m.height / 1000, m.height - 1)      # thousandths -> picture pixels
        elif not (0 <= x < m.width and 0 <= y < m.height):
            raise ComputerError(f"({x:.0f}, {y:.0f}) is outside the picture, which is {m.width}x{m.height}")
        rx, ry = m.left + x * m.scale, m.top + y * m.scale
        win = self.window(m.hwnd)
        union, rects = self._frame(win)
        if union != m.rect or tuple(rects) != m.rects:
            raise ComputerError("the window (or one of its dialogs) moved, resized, opened or closed since the last picture: "
                                "take a new screen_shot")
        if not any(l <= rx < r and t <= ry < b for l, t, r, b in rects):
            raise ComputerError("that position is outside the approved window")
        hit = self.backend.window_at(int(rx), int(ry))
        if hit is not None and not self._owned(hit[0], win.hwnd):
            raise ComputerError("another program's window is on top of that spot: nothing was clicked")
        return rx, ry

    def _move(self, rx: float, ry: float, slow: float = 0.0) -> None:
        """Move the mouse there: smoothly when enabled (and supported), else in one jump. Esc stops it."""
        be = self.backend
        if self.smooth and getattr(be, "supports_smooth", False):
            x0, y0 = be.cursor()
            for n, (x, y, pause) in enumerate(smooth_path(x0, y0, rx, ry, slow)):
                if n % 4 == 0:
                    self._kill_check()
                be.set_cursor(x, y)
                be.sleep(pause)
        be.set_cursor(round(rx), round(ry))

    def _after(self) -> None:
        self.expect_change = True                         # the next picture is compared with the one before
        self.backend.sleep(self.settle)                   # let the app react before the next picture

    # ------------------------------------------------------------------ actions (each returns a one-line description)
    def click(self, x=None, y=None, cell=None, button: str = "left", double: bool = False) -> str:
        self._ready()
        if button not in screenctl.MOUSE:
            raise ComputerError("button must be left, right or middle")
        rx, ry = self.point(x, y, cell)
        win = self._front(self.window(self.mapping.hwnd))
        self._spend()
        self._move(rx, ry)
        for _ in range(2 if double else 1):
            self.backend.mouse_down(button)
            self.backend.sleep(0.04)
            self.backend.mouse_up(button)
            self.backend.sleep(0.07)
        self.marker = [(rx, ry)]
        what = f"{'double-' if double else ''}{button} click at ({rx:.0f}, {ry:.0f}) in '{clean_title(win.title)}'"
        self._note(what)
        self._after()
        return what

    def drag(self, start: dict, end: dict, button: str = "left") -> str:
        """Press at the start, move smoothly to the end, release. start/end are {x, y} or {cell}."""
        self._ready()
        if button not in screenctl.MOUSE:
            raise ComputerError("button must be left, right or middle")
        sx, sy = self.point(**start)
        ex, ey = self.point(**end)
        if (round(sx), round(sy)) == (round(ex), round(ey)):
            raise ComputerError("the drag starts and ends at the same place")
        win = self._front(self.window(self.mapping.hwnd))
        self._spend()
        self._move(sx, sy)
        be = self.backend
        be.mouse_down(button)
        try:
            be.sleep(0.12)                                              # many apps need a moment before they see a drag
            self._move(ex, ey, slow=0.35)                               # drags are never instant
            be.sleep(0.12)
        finally:
            be.mouse_up(button)                                         # ALWAYS release, even when Esc stopped it
        self.marker = [(sx, sy), (ex, ey)]
        what = f"dragged ({sx:.0f}, {sy:.0f}) -> ({ex:.0f}, {ey:.0f}) in '{clean_title(win.title)}'"
        self._note(what)
        self._after()
        return what

    def scroll(self, clicks: int, x=None, y=None, cell=None) -> str:
        self._ready()
        try:
            clicks = int(clicks)
        except (TypeError, ValueError):
            raise ComputerError("clicks must be a whole number (positive = up, negative = down)")
        if clicks == 0 or abs(clicks) > 30:
            raise ComputerError("clicks must be between -30 and 30, and not 0")
        if x is None and y is None and not cell:                         # default: the middle of the window
            m = self.mapping
            if not m:
                raise ComputerError("take a screen_shot first")
            x, y = (500, 500) if self.coords == "normalized" else (m.width / 2, m.height / 2)
        rx, ry = self.point(x, y, cell)
        self._front(self.window(self.mapping.hwnd))
        self._spend()
        self._move(rx, ry)
        self.backend.wheel(clicks)
        what = f"scrolled {'up' if clicks > 0 else 'down'} {abs(clicks)}"
        self._note(what)
        self._after()
        return what

    def _keys_ready(self, title: str | None = None) -> Win:
        """The approved window that will receive the keys, in front, with no password box focused."""
        self._ready()
        win = self._front(self.pick(title))
        if self.backend.focus_is_password():
            raise ComputerError("a password box is focused: Danica never types into those. The user must type it.")
        return win

    def type_text(self, text: str, window: str | None = None) -> str:
        text = str(text)
        if not text or len(text) > MAX_TYPE:
            raise ComputerError(f"text must be 1 to {MAX_TYPE} characters")
        if re.search(r"[\x00-\x08\x0b-\x1f\x7f]", text):
            raise ComputerError("text contains control characters")
        win = self._keys_ready(window)
        self._spend()
        be = self.backend
        for n, ch in enumerate(text):
            if n % 20 == 0:
                self._kill_check()
            if ch == "\n":
                be.key(screenctl.VK["enter"], False); be.key(screenctl.VK["enter"], True)
            elif ch == "\t":
                be.key(screenctl.VK["tab"], False); be.key(screenctl.VK["tab"], True)
            else:
                be.char(ch)
            be.sleep(0.012)
        what = f"typed {len(text)} characters into '{clean_title(win.title)}'"
        self._note(what)
        self._after()
        return what

    def press(self, keys, window: str | None = None) -> str:
        combos = parse_keys(keys)
        win = self._keys_ready(window)
        self._spend()
        be = self.backend
        for combo in combos:
            if combo[-1] != "escape":                                   # sending Escape ourselves must not look like the kill switch
                self._kill_check()
            mods = [screenctl.MODIFIERS[k] for k in combo[:-1]]
            code = screenctl.key_code(combo[-1])
            for m in mods:
                be.key(m, False)
            be.key(code, False); be.sleep(0.03); be.key(code, True)
            for m in reversed(mods):
                be.key(m, True)
            be.sleep(0.08)
        what = f"pressed {' then '.join('+'.join(c) for c in combos)} in '{clean_title(win.title)}'"
        self._note(what)
        self._after()
        return what

    def focus(self, title: str | None) -> str:
        self._ready()
        win = self.pick(title)
        self._spend()
        if not self.backend.focus(win.hwnd):
            raise ComputerError(f"could not bring '{clean_title(win.title)}' to the front. Ask the user to click on it.")
        self._note(f"focused '{clean_title(win.title)}'")
        return f"'{clean_title(win.title)}' is in front"


def computer_for(ws) -> Computer:
    """The session's Computer (created on first use, OFF). It lives on the workspace, so a new session starts OFF."""
    comp = getattr(ws, "computer", None)
    if comp is None:
        from danica.config import CONFIG
        cfg = CONFIG.get("computer", {}) or {}
        comp = Computer(smooth=bool(cfg.get("smooth", True)), max_actions=int(cfg.get("max_actions", 25)),
                        settle=float(cfg.get("settle", 0.35)), grid=bool(cfg.get("grid", True)),
                        coords="pixels" if cfg.get("coords") == "pixels" else "normalized")
        ws.computer = comp
    return comp
