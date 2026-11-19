"""
sandbox.py - Windows Job Object wrapper (sandbox level 2).

A Job Object is an OS-level container for processes. Everything started
inside it shares the limits below, and children inherit membership, so a
command cannot escape by spawning a helper.

WHAT IT LIMITS: memory, number of processes, lifetime, a few UI powers.
WHAT IT DOES NOT: file access outside the workspace, network access, CPU use.
Say this plainly in the UI (see /sandbox).
"""

import ctypes
import subprocess
from ctypes import wintypes
from typing import Any

# --- Win32 constants (values from the Windows SDK headers) ---
_LIMIT_ACTIVE_PROCESS = 0x00000008          # enforce ActiveProcessLimit
_LIMIT_DIE_ON_UNHANDLED_EXC = 0x00000400    # crash silently instead of showing a dialog
_LIMIT_JOB_MEMORY = 0x00000200              # enforce JobMemoryLimit
_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000       # closing the job handle kills everything in it

# UI restrictions: clipboard (read+write), system parameters, display settings,
# global atoms, desktop switching, logoff/shutdown. (0x1 = USER handles; skipped
# because it is the one most likely to break ordinary console programs.)
_UI_FLAGS = 0x2 | 0x4 | 0x8 | 0x10 | 0x20 | 0x40 | 0x80

_INFO_ACCOUNTING = 1         # JobObjectBasicAccountingInformation
_INFO_UI_RESTRICTIONS = 4    # JobObjectBasicUIRestrictions
_INFO_EXTENDED_LIMIT = 9     # JobObjectExtendedLimitInformation

_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_SUSPEND_RESUME = 0x0800
CREATE_SUSPENDED = 0x00000004


class SandboxError(Exception):
    """The job could not be created or configured. The message is safe to show."""


# --- C structures, laid out exactly like the Windows ones ---
class _BasicLimit(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _ExtendedLimit(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimit),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _UiRestrictions(ctypes.Structure):
    _fields_ = [("UIRestrictionsClass", wintypes.DWORD)]


class _Accounting(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_int64),
        ("TotalKernelTime", ctypes.c_int64),
        ("ThisPeriodTotalUserTime", ctypes.c_int64),
        ("ThisPeriodTotalKernelTime", ctypes.c_int64),
        ("TotalPageFaultCount", wintypes.DWORD),
        ("TotalProcesses", wintypes.DWORD),
        ("ActiveProcesses", wintypes.DWORD),
        ("TotalTerminatedProcesses", wintypes.DWORD),
    ]


# --- function signatures. Without these, ctypes treats handles as 32-bit
# ints and silently corrupts them on 64-bit Windows. ---
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_ntdll = ctypes.WinDLL("ntdll")

_k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
_k32.CreateJobObjectW.restype = wintypes.HANDLE
_k32.SetInformationJobObject.argtypes = [
    wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD
]
_k32.SetInformationJobObject.restype = wintypes.BOOL
_k32.QueryInformationJobObject.argtypes = [
    wintypes.HANDLE,
    ctypes.c_int,
    wintypes.LPVOID,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
]
_k32.QueryInformationJobObject.restype = wintypes.BOOL
_k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
_k32.AssignProcessToJobObject.restype = wintypes.BOOL
_k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
_k32.TerminateJobObject.restype = wintypes.BOOL
_k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_k32.OpenProcess.restype = wintypes.HANDLE
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_k32.CloseHandle.restype = wintypes.BOOL
_ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]


def _fail(what: str) -> SandboxError:
    """Build an error that includes Windows' own explanation of what went wrong."""
    return SandboxError(f"{what}: {ctypes.WinError(ctypes.get_last_error())}")


class Job:
    """One job per shell command. Use it as a context manager:

        with Job(memory_mb=2048, max_processes=64) as job:
            proc = job.popen([...])
            ...
        # leaving the block kills anything still alive inside the job
    """

    def __init__(self, memory_mb: int = 2048, max_processes: int = 64):
        self.handle = _k32.CreateJobObjectW(None, None)
        if not self.handle:
            raise _fail("could not create job object")
        try:
            self._configure(memory_mb, max_processes)
        except SandboxError:
            self.close()
            raise

    def _set_info(self, info_class: int, struct) -> None:
        if not _k32.SetInformationJobObject(
            self.handle,
            info_class,
            ctypes.byref(struct),
            ctypes.sizeof(struct),
        ):
            raise _fail("could not apply sandbox limits")

    def _configure(self, memory_mb: int, max_processes: int) -> None:
        ext = _ExtendedLimit()
        basic = ext.BasicLimitInformation   # a view into ext: setting fields here sets them in ext
        basic.LimitFlags = (_LIMIT_ACTIVE_PROCESS | _LIMIT_JOB_MEMORY
                            | _LIMIT_KILL_ON_JOB_CLOSE | _LIMIT_DIE_ON_UNHANDLED_EXC)
        basic.ActiveProcessLimit = max_processes
        ext.JobMemoryLimit = memory_mb * 1024 * 1024   # bytes, shared by ALL processes in the job
        self._set_info(_INFO_EXTENDED_LIMIT, ext)

        ui = _UiRestrictions()
        ui.UIRestrictionsClass = _UI_FLAGS
        self._set_info(_INFO_UI_RESTRICTIONS, ui)

    def _adopt(self, pid: int) -> None:
        """Put a SUSPENDED process into the job, then let it run."""
        handle = _k32.OpenProcess(
            _PROCESS_SET_QUOTA | _PROCESS_TERMINATE | _PROCESS_SUSPEND_RESUME,
            False,
            pid,
        )
        if not handle:
            raise _fail("could not open the new process")
        try:
            if not _k32.AssignProcessToJobObject(self.handle, handle):
                raise _fail("could not put the process into the sandbox")
            status = _ntdll.NtResumeProcess(handle)
            if status != 0:
                raise SandboxError(f"could not resume the process (NTSTATUS {status & 0xFFFFFFFF:#x})")
        finally:
            _k32.CloseHandle(handle)

    def popen(self, args: list[str], **kwargs: Any) -> subprocess.Popen:
        """Like subprocess.Popen, but the process is confined from its very first instruction.
        Starting it suspended closes the gap in which it could spawn children
        before the job existed."""
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | CREATE_SUSPENDED
        proc = subprocess.Popen(args, **kwargs)
        try:
            self._adopt(proc.pid)
        except SandboxError:
            # Fail closed: a process we could not confine must not run
            proc.kill()
            proc.communicate()
            raise
        return proc

    def active_processes(self) -> int:
        """How many processes are alive inside the job right now."""
        info = _Accounting()
        if not _k32.QueryInformationJobObject(
            self.handle,
            _INFO_ACCOUNTING,
            ctypes.byref(info),
            ctypes.sizeof(info),
            None,
        ):
            raise _fail("could not query the job")
        return info.ActiveProcesses

    def kill(self) -> None:
        """Terminate every process in the job, including grandchildren."""
        if self.handle:
            _k32.TerminateJobObject(self.handle, 1)

    def close(self) -> None:
        """Close the job handle and release sandbox resources."""
        if self.handle:
            _k32.CloseHandle(self.handle)   # KILL_ON_JOB_CLOSE also fires here
            self.handle = None

    def __enter__(self) -> "Job":
        return self

    def __exit__(self, *exc) -> None:
        self.kill()
        self.close()
