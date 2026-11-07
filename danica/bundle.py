"""Build a text-only, secret-scanned ZIP export of a workspace."""

import zipfile
import secrets
from pathlib import Path

from danica.secret_scan import find_secrets, is_sensitive_path
from danica.workspace import Workspace

IGNORED_DIRS = {".git", ".venv", "__pycache__", "node_modules", ".danica"}
MAX_FILES = 2000
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 100 * 1024 * 1024


class BundleError(Exception):
    """A rejected or failed export bundle operation."""


def export_bundle(ws: Workspace, output: Path) -> dict:
    target = ws.resolve(str(output))
    if target.suffix.lower() != ".zip":
        raise BundleError("bundle output must use the .zip extension")
    if target.exists() and target.is_dir():
        raise BundleError("bundle output points to a directory")
    files = []
    total = 0
    skipped = []
    for path in ws.root.rglob("*"):
        if not path.is_file() or any(part in IGNORED_DIRS for part in path.relative_to(ws.root).parts):
            continue
        if path.is_symlink() or path.resolve() == target.resolve():
            continue
        if is_sensitive_path(path):
            skipped.append(ws.display(path))
            continue
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise BundleError(f"could not read {ws.display(path)}: {exc}") from exc
        if len(data) > MAX_FILE_BYTES or total + len(data) > MAX_TOTAL_BYTES:
            skipped.append(ws.display(path))
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            skipped.append(ws.display(path))
            continue
        if find_secrets(text):
            skipped.append(ws.display(path))
            continue
        files.append((path.relative_to(ws.root).as_posix(), data))
        total += len(data)
        if len(files) > MAX_FILES:
            raise BundleError(f"workspace exceeds the {MAX_FILES}-file bundle limit")

    readme = next((data.decode("utf-8") for name, data in files
                   if name.lower() == "readme.md"), None)
    if readme is None:
        listing = "\n".join(f"- `{name}`" for name, _ in files) or "- (no eligible text files)"
        readme = ("# Workspace export\n\n"
                  "This ZIP contains eligible UTF-8 text files from the workspace. "
                  "Sensitive files and files that look like they contain secrets were excluded.\n\n"
                  "## Included files\n\n" + listing + "\n")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{secrets.token_hex(6)}.tmp")
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in files:
                archive.writestr(name, data)
            if not any(name.lower() == "readme.md" for name, _ in files):
                archive.writestr("README.md", readme.encode("utf-8"))
        temporary.replace(target)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise BundleError(f"could not create export bundle: {exc}") from exc
    return {"path": target, "files": len(files), "bytes": total, "skipped": skipped}
