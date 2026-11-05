"""
pathsearch.py - Finding programs on PATH WITHOUT looking in the current directory.

shutil.which on Windows (Python 3.11) always looks in the current directory first. In project
mode that directory is the user's project folder, where a model-written "mmdc.cmd" or "git.exe"
could sit and be run without a prompt. find_program() searches only the ABSOLUTE folders listed
in PATH and skips the current directory, even if PATH names it.
"""

import os


def find_program(name: str) -> str | None:
    """The full path of a program, or None. Honors PATHEXT (so "mmdc" finds mmdc.cmd)."""
    has_suffix = bool(os.path.splitext(name)[1])
    exts = [""] if has_suffix else [e.lower() for e in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";") if e]
    cwd = os.path.normcase(os.path.abspath(os.getcwd()))
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        entry = entry.strip().strip('"')
        if not entry or not os.path.isabs(entry):
            continue                                   # "." and relative folders mean "the current directory"
        if os.path.normcase(os.path.abspath(entry)) == cwd:
            continue
        for ext in exts:
            candidate = os.path.join(entry, name + ext)
            if os.path.isfile(candidate):
                return candidate
    return None