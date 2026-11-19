"""
pathsearch.py - Finding programs on PATH WITHOUT looking in the current directory.

shutil.which on Windows (Python 3.11) always looks in the current directory first. In project
mode that directory is the user's project folder, where a model-written "mmdc.cmd" or "git.exe"
could sit and be run without a prompt. find_program() searches only the ABSOLUTE folders listed
in PATH and skips the current directory, even if PATH names it.
"""

import os


def find_program(name: str) -> str | None:
    """Return a program's full path, or ``None`` if it is not found.

    Honor ``PATHEXT`` on Windows so a name such as ``mmdc`` can match
    ``mmdc.cmd``.
    """
    has_suffix = bool(os.path.splitext(name)[1])
    extensions = (
        [""]
        if has_suffix
        else [
            extension.lower()
            for extension in os.environ.get(
                "PATHEXT", ".COM;.EXE;.BAT;.CMD"
            ).split(";")
            if extension
        ]
    )
    current_directory = os.path.normcase(os.path.abspath(os.getcwd()))
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        entry = entry.strip().strip('"')
        if not entry or not os.path.isabs(entry):
            continue                                   # "." and relative folders mean "the current directory"
        if os.path.normcase(os.path.abspath(entry)) == current_directory:
            continue
        for extension in extensions:
            candidate = os.path.join(entry, name + extension)
            if os.path.isfile(candidate):
                return candidate
    return None
