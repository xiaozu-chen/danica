"""
screenctl.py - the Windows side of computer use: list windows, take a picture of one, move the mouse, click, type.

This file only DOES things. Every decision about what is allowed (approved windows, blocked programs, limits, the Esc
kill switch) lives in computer.py, which talks to a "backend" with the methods of Win32Backend below. Tests plug in a
fake backend, so the rules can be checked without a real desktop.

Everything uses ctypes and Pillow: no extra package to install. The Windows structures are built inside
Win32Backend.__init__ so that importing this file is harmless on other systems.
"""

import os
import time
from dataclasses import dataclass


class ScreenError(Exception):
    """Something the screen layer could not do. The message is safe to show to the user and the model."""


@dataclass
class Win:
    hwnd: int
    title: str
    pid: int
    process: str                         # lower-case program name, for example "notepad.exe"
    rect: tuple[int, int, int, int]      # left, top, right, bottom in screen pixels
    minimized: bool = False


# Key names -> Windows virtual-key codes (letters, digits and single punctuation keys are handled in key_code()).
VK = {
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "escape": 0x1B, "esc": 0x1B, "space": 0x20, "backspace": 0x08,
    "delete": 0x2E, "del": 0x2E, "insert": 0x2D, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    **{f"f{n}": 0x6F + n for n in range(1, 13)},
}
MODIFIERS = {"ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12}
MOUSE = {"left": (0x0002, 0x0004), "right": (0x0008, 0x0010), "middle": (0x0020, 0x0040)}     # (down, up) flags


def key_code(name: str) -> int | None:
    """Virtual-key code for a key name ("enter", "f5", "a", "7"), or None when it is not known."""
    name = name.lower()
    if name in VK:
        return VK[name]
    if len(name) == 1 and (name.isalpha() and name.isascii() or name.isdigit()):
        return ord(name.upper())
    return None


class Win32Backend:
    """The real thing. Only works on Windows (computer use is a Windows feature)."""

    supports_smooth = True

    def __init__(self) -> None:
        if os.name != "nt":
            raise ScreenError("computer use only works on Windows")
        import ctypes
        from ctypes import wintypes
        self.ct, self.wt = ctypes, wintypes
        self.user32, self.kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        self.sleep = time.sleep
        try:                                                  # real pixels, not scaled ones (else clicks land in the wrong place)
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                self.user32.SetProcessDPIAware()
            except Exception:
                pass
        ULONG_PTR = ctypes.c_size_t

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

        class _U(ctypes.Union):
            _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]

        class INPUT(ctypes.Structure):
            _anonymous_ = ("u",)
            _fields_ = [("type", wintypes.DWORD), ("u", _U)]

        class POINT(ctypes.Structure):
            _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

        class GUITHREADINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD), ("hwndActive", wintypes.HWND),
                        ("hwndFocus", wintypes.HWND), ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                        ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND), ("rcCaret", wintypes.RECT)]

        self.INPUT, self.MOUSEINPUT, self.KEYBDINPUT, self.POINT, self.GUITHREADINFO = INPUT, MOUSEINPUT, KEYBDINPUT, POINT, GUITHREADINFO
        self.user32.GetAncestor.restype = wintypes.HWND
        self.user32.WindowFromPoint.restype = wintypes.HWND
        self.user32.GetForegroundWindow.restype = wintypes.HWND
        self.user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        self.user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]

    # ------------------------------------------------------------------ windows
    def _process_name(self, pid: int) -> str:
        ct = self.ct
        handle = self.kernel32.OpenProcess(0x1000, False, pid)             # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ""
        try:
            buf = ct.create_unicode_buffer(520)
            size = self.wt.DWORD(520)
            if self.kernel32.QueryFullProcessImageNameW(handle, 0, buf, ct.byref(size)):
                return os.path.basename(buf.value).lower()
        finally:
            self.kernel32.CloseHandle(handle)
        return ""

    def _rect(self, hwnd: int) -> tuple[int, int, int, int]:
        """The visible frame of the window (without the invisible resize border Windows adds)."""
        ct, wt = self.ct, self.wt
        r = wt.RECT()
        try:
            if ct.windll.dwmapi.DwmGetWindowAttribute(wt.HWND(hwnd), 9, ct.byref(r), ct.sizeof(r)) == 0:     # EXTENDED_FRAME_BOUNDS
                return r.left, r.top, r.right, r.bottom
        except Exception:
            pass
        self.user32.GetWindowRect(wt.HWND(hwnd), ct.byref(r))
        return r.left, r.top, r.right, r.bottom

    def _info(self, hwnd: int) -> Win | None:
        ct, wt = self.ct, self.wt
        n = self.user32.GetWindowTextLengthW(wt.HWND(hwnd))
        if n <= 0:
            return None
        buf = ct.create_unicode_buffer(n + 1)
        self.user32.GetWindowTextW(wt.HWND(hwnd), buf, n + 1)
        pid = wt.DWORD()
        self.user32.GetWindowThreadProcessId(wt.HWND(hwnd), ct.byref(pid))
        return Win(int(hwnd), buf.value, int(pid.value), self._process_name(pid.value), self._rect(hwnd),
                   bool(self.user32.IsIconic(wt.HWND(hwnd))))

    def list_windows(self) -> list[Win]:
        """Visible top-level windows with a title (hidden and 'cloaked' background windows are left out)."""
        ct, wt = self.ct, self.wt
        found: list[Win] = []
        proto = ct.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

        def each(hwnd, _):
            try:
                if not self.user32.IsWindowVisible(hwnd):
                    return True
                cloaked = ct.c_int(0)
                try:
                    ct.windll.dwmapi.DwmGetWindowAttribute(wt.HWND(hwnd), 14, ct.byref(cloaked), 4)         # DWMWA_CLOAKED
                except Exception:
                    pass
                if cloaked.value:
                    return True
                info = self._info(int(hwnd))
                if info:
                    found.append(info)
            except Exception:
                pass
            return True

        self.user32.EnumWindows(proto(each), 0)
        return found

    def get(self, hwnd: int) -> Win | None:
        if not self.user32.IsWindow(self.wt.HWND(hwnd)):
            return None
        return self._info(hwnd)

    def foreground(self) -> int:
        return int(self.user32.GetForegroundWindow() or 0)

    def focus(self, hwnd: int) -> bool:
        """Bring a window to the front (restoring it if minimized). True when it is in front afterwards."""
        wt = self.wt
        if self.user32.IsIconic(wt.HWND(hwnd)):
            self.user32.ShowWindow(wt.HWND(hwnd), 9)                      # SW_RESTORE
        self.user32.SetForegroundWindow(wt.HWND(hwnd))
        if self.foreground() != hwnd:                                     # Windows sometimes refuses: borrow the front thread's input
            ct = self.ct
            front = self.user32.GetWindowThreadProcessId(wt.HWND(self.foreground()), None)
            mine = self.kernel32.GetCurrentThreadId()
            self.user32.AttachThreadInput(mine, front, True)
            self.user32.SetForegroundWindow(wt.HWND(hwnd))
            self.user32.AttachThreadInput(mine, front, False)
        self.sleep(0.15)
        return self.foreground() == hwnd

    def window_at(self, x: int, y: int) -> tuple[int, int] | None:
        """(top-level window, its process id) under a screen point, or None when there is no window there."""
        ct, wt = self.ct, self.wt
        h = self.user32.WindowFromPoint(self.POINT(int(x), int(y)))
        if not h:
            return None
        root = self.user32.GetAncestor(h, 2) or h                         # GA_ROOT
        pid = wt.DWORD()
        self.user32.GetWindowThreadProcessId(root, ct.byref(pid))
        return int(root), int(pid.value)

    def capture(self, rect):
        """A Pillow image of a screen rectangle (left, top, right, bottom)."""
        try:
            from PIL import ImageGrab
            return ImageGrab.grab(bbox=tuple(int(v) for v in rect), all_screens=True).convert("RGB")
        except Exception as exc:
            raise ScreenError(f"could not take the picture ({type(exc).__name__})")

    # ------------------------------------------------------------------ mouse
    def cursor(self) -> tuple[int, int]:
        p = self.POINT()
        self.user32.GetCursorPos(self.ct.byref(p))
        return p.x, p.y

    def set_cursor(self, x: int, y: int) -> None:
        self.user32.SetCursorPos(int(x), int(y))

    def _send(self, *items) -> None:
        arr = (self.INPUT * len(items))(*items)
        if self.user32.SendInput(len(items), arr, self.ct.sizeof(self.INPUT)) != len(items):
            raise ScreenError("Windows did not accept the input (an app running as administrator blocks it)")

    def _mouse(self, flags: int, data: int = 0):
        i = self.INPUT(type=0)
        i.mi = self.MOUSEINPUT(0, 0, data & 0xFFFFFFFF, flags, 0, 0)
        return i

    def mouse_down(self, button: str = "left") -> None:
        self._send(self._mouse(MOUSE[button][0]))

    def mouse_up(self, button: str = "left") -> None:
        self._send(self._mouse(MOUSE[button][1]))

    def wheel(self, clicks: int) -> None:
        """Positive = scroll up, negative = down."""
        self._send(self._mouse(0x0800, int(clicks) * 120))

    # ------------------------------------------------------------------ keyboard
    def _key(self, vk: int = 0, scan: int = 0, flags: int = 0):
        i = self.INPUT(type=1)
        i.ki = self.KEYBDINPUT(vk, scan, flags, 0, 0)
        return i

    def key(self, vk: int, up: bool) -> None:
        extended = 0x0001 if vk in (0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E) else 0
        self._send(self._key(vk, 0, extended | (0x0002 if up else 0)))

    def char(self, ch: str) -> None:
        """Type one character the way a keyboard would (any language, emoji included)."""
        data = ch.encode("utf-16-le")
        for k in range(0, len(data), 2):
            code = int.from_bytes(data[k:k + 2], "little")
            self._send(self._key(0, code, 0x0004), self._key(0, code, 0x0004 | 0x0002))      # KEYEVENTF_UNICODE down, up

    def esc_pressed(self) -> bool:
        """Is the Esc key down right now (anywhere in Windows)? This is the kill switch."""
        return bool(self.user32.GetAsyncKeyState(0x1B) & 0x8000)

    def focus_is_password(self) -> bool:
        """True when the focused control is a classic password box. (Browsers and modern apps do not tell us.)"""
        ct, wt = self.ct, self.wt
        info = self.GUITHREADINFO()
        info.cbSize = ct.sizeof(info)
        if not self.user32.GetGUIThreadInfo(0, ct.byref(info)) or not info.hwndFocus:
            return False
        style = self.user32.GetWindowLongW(wt.HWND(info.hwndFocus), -16) & 0xFFFFFFFF
        name = ct.create_unicode_buffer(64)
        self.user32.GetClassNameW(wt.HWND(info.hwndFocus), name, 64)
        return name.value.lower().startswith("edit") and bool(style & 0x20)                # ES_PASSWORD


_BACKEND = None


def get_backend():
    """The backend in use: the real one on Windows (tests replace it with set_backend)."""
    global _BACKEND
    if _BACKEND is None:
        _BACKEND = Win32Backend()
    return _BACKEND


def set_backend(backend) -> None:
    global _BACKEND
    _BACKEND = backend
