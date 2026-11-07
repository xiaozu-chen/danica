"""Headless/export bundle boundary tests; no model required."""

import tempfile
import zipfile
from pathlib import Path

from danica.bundle import export_bundle
from danica.permissions import PermissionPolicy
from danica.workspace import Workspace


def check(label: str, condition: bool) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    assert condition, label


with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    root = Path(tmp) / "workspace"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8")
    (root / ".env").write_text("TOKEN=not-for-export\n", encoding="utf-8")
    (root / "credentials.txt").write_text("AKIAIOSFODNN7EXAMPLE\n", encoding="utf-8")
    (root / "image.bin").write_bytes(b"\xff\x00\xfe")
    ws = Workspace(root)
    result = export_bundle(ws, Path("dist/project.zip"))
    with zipfile.ZipFile(result["path"]) as archive:
        names = set(archive.namelist())
        readme = archive.read("README.md").decode("utf-8")
    check("bundle contains eligible source and generated README",
          "src/app.py" in names and "README.md" in names and "src/app.py" in readme)
    check("secrets, binaries, and the archive itself are excluded",
          ".env" not in names and "credentials.txt" not in names and "image.bin" not in names
          and "dist/project.zip" not in names and len(result["skipped"]) >= 3)

policy = PermissionPolicy()
check("headless policy fails closed for interactive shell and delegate requests",
      not policy.check("run_shell", {"command": "python app.py"})[0]
      and not policy.check("delegate", {"task": "research"})[0])

print("\nAll good.")
