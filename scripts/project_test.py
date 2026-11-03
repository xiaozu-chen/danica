"""
project_test.py - Checks trust records, the too-broad-folder rules, session
mode files and shadow-git snapshots of a real-looking project folder.
No model needed. Run from the project root:  python -m scripts.project_test
"""

import json
import os
import tempfile
from pathlib import Path

from danica.checkpoints import Checkpoints
from danica.config import APP_DIR
from danica.projects import (ProjectError, check_allowed, list_projects, load_session_meta,
                             lookup, project_id, register, save_session_meta, scan)
from danica.workspace import Workspace


def check(label: str, condition: bool) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    assert condition, label


def refused(path: Path) -> bool:
    try:
        check_allowed(path)
        return False
    except ProjectError:
        return True


with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    store = Path(tmp) / "projects"
    proj = Path(tmp) / "My Project"           # a space on purpose
    (proj / "src").mkdir(parents=True)
    (proj / "src" / "a.py").write_text("print(1)\n", encoding="utf-8")

    # --- ids ---
    check("the id is stable", project_id(proj) == project_id(proj))
    check("the id ignores letter case (Windows)", project_id(proj) == project_id(Path(str(proj).upper())))
    check("different folders get different ids", project_id(proj) != project_id(proj / "src"))

    # --- folders that must never be projects ---
    home = Path.home()
    check("a drive root is refused", refused(Path(home.anchor)))
    check("the home folder is refused", refused(home))
    check("a folder above home is refused", refused(home.parent))
    check("the Windows folder is refused", refused(Path(os.environ.get("SystemRoot", "C:\\Windows"))))
    check("Danica's own data is refused", refused(APP_DIR) and refused(APP_DIR / "sessions"))
    check("a missing folder is refused", refused(Path(tmp) / "nope"))
    check("a file is refused", refused(proj / "src" / "a.py"))
    check("a normal folder is accepted", check_allowed(proj) == proj.resolve())

    # --- trust records ---
    check("never-trusted folders are not found", lookup(proj, store) is None)
    info = register(proj, store)
    check("register creates the record and the memory folder",
          (Path(info["dir"]) / "project.json").exists() and (Path(info["dir"]) / "memory").is_dir())
    check("lookup finds it afterwards", lookup(proj, store)["path"] == str(proj.resolve()))
    check("registering twice keeps the first trust time", register(proj, store)["trusted_at"] == info["trusted_at"])
    check("another folder is still untrusted", lookup(proj / "src", store) is None)
    check("the list has one project", len(list_projects(store)) == 1)

    record = Path(info["dir"]) / "project.json"
    good = record.read_text(encoding="utf-8")
    data = json.loads(good)
    data["path_norm"] = "somewhere-else"
    record.write_text(json.dumps(data), encoding="utf-8")
    check("a record that does not match its folder is rejected", lookup(proj, store) is None)
    record.write_text("{not json", encoding="utf-8")
    check("a damaged record is rejected, not a crash", lookup(proj, store) is None)
    record.write_text(good, encoding="utf-8")

    # --- the dialog's quick scan ---
    (proj / ".git").mkdir()
    (proj / "node_modules").mkdir()
    (proj / "node_modules" / "junk.js").write_text("x", encoding="utf-8")
    files, size, capped, has_git = scan(proj)
    check("scan skips junk folders and notices a git repo", files == 1 and has_git and not capped)

    # --- session mode files ---
    sdir = Path(tmp) / "session"
    sdir.mkdir()
    check("an old session without a mode file is {}", load_session_meta(sdir) == {})
    save_session_meta(sdir, str(proj))
    check("a project session remembers its folder",
          load_session_meta(sdir) == {"mode": "project", "project": str(proj)})
    save_session_meta(sdir, None)
    check("a chat session records no folder", load_session_meta(sdir)["project"] is None)
    (sdir / "session.json").write_text("[1, 2]", encoding="utf-8")
    check("a malformed mode file is {}", load_session_meta(sdir) == {})

    # --- shadow git on a real-looking project ---
    mine = proj / ".git" / "MARKER"
    mine.write_text("mine", encoding="utf-8")                       # stands in for the user's own repo
    (proj / ".gitignore").write_text("secret.log\n", encoding="utf-8")
    (proj / "secret.log").write_text("keep me", encoding="utf-8")   # ignored by the project

    cp = Checkpoints(Workspace(proj), Path(info["dir"]) / "git")
    check("git is available", cp.available)
    cp.begin_request("edit things")
    (proj / "src" / "a.py").write_text("print(2)\n", encoding="utf-8")
    (proj / "new.txt").write_text("x", encoding="utf-8")
    (proj / "secret.log").write_text("changed", encoding="utf-8")
    cp.undo()
    check("undo reverts the edited file", (proj / "src" / "a.py").read_text(encoding="utf-8") == "print(1)\n")
    check("undo removes the new file", not (proj / "new.txt").exists())
    check("the user's own .git is untouched", mine.read_text(encoding="utf-8") == "mine")
    check("files the project ignores are neither saved nor restored",
          (proj / "secret.log").read_text(encoding="utf-8") == "changed")
    check("the shadow repo lives in the project's data folder, not the project",
          (Path(info["dir"]) / "git" / "HEAD").exists() and not (proj / "HEAD").exists())

print("\nAll good.")