"""Streaming removal and workspace validation for generated-artifact tags."""

import re
from pathlib import Path

from danica.workspace import PathJailError, Workspace

PRESENT_TAG = re.compile(r"""<present\s+file=(?:"([^"]+)"|'([^']+)')\s*/>""")
PRESENT_START = "<present"


class PresentationStream:
    """Keep tag fragments out of streamed text while emitting ordinary text promptly."""

    def __init__(self):
        self.pending = ""

    def feed(self, chunk: str) -> str:
        self.pending += chunk
        emitted = []
        while self.pending:
            start = self.pending.find(PRESENT_START)
            if start < 0:
                keep = 0
                for length in range(1, min(len(self.pending), len(PRESENT_START)) + 1):
                    if self.pending.endswith(PRESENT_START[:length]):
                        keep = length
                if keep:
                    emitted.append(self.pending[:-keep])
                    self.pending = self.pending[-keep:]
                else:
                    emitted.append(self.pending)
                    self.pending = ""
                break
            if start:
                emitted.append(self.pending[:start])
                self.pending = self.pending[start:]
            closing = self.pending.find("/>")
            if closing < 0:
                if len(self.pending) <= 512:
                    break
                emitted.append(self.pending[0])
                self.pending = self.pending[1:]
                continue
            end = closing + 2
            candidate = self.pending[:end]
            if PRESENT_TAG.fullmatch(candidate):
                self.pending = self.pending[end:]
            else:
                emitted.append(candidate)
                self.pending = self.pending[end:]
        return "".join(emitted)

    def finish(self) -> str:
        tail, self.pending = self.pending, ""
        return tail


def remove_and_validate(text: str, ws: Workspace) -> tuple[str, list[Path], list[str]]:
    """Remove present tags, returning only existing generated artifact paths."""
    paths: list[Path] = []
    warnings: list[str] = []

    def replace(match: re.Match) -> str:
        raw_path = match.group(1) or match.group(2)
        try:
            resolved = ws.resolve(raw_path)
        except (OSError, PathJailError, RuntimeError, ValueError):
            warnings.append(f"Could not present '{raw_path}': path is outside the workspace.")
            return ""
        display = ws.display(resolved)
        if display not in ws.presentables or not resolved.is_file():
            warnings.append(
                f"Could not present '{raw_path}': only generated HTML/SVG artifacts "
                "from this task can be opened."
            )
            return ""
        if resolved.suffix.casefold() not in (".html", ".svg"):
            warnings.append(f"Could not present '{raw_path}': only HTML/SVG can be opened.")
            return ""
        paths.append(resolved)
        return ""

    cleaned = PRESENT_TAG.sub(replace, text)
    return cleaned, paths, warnings
