"""
safety_test.py - Checks the audit chain, the scanner and the guarded file tools.
No model needed. Run from the project root:  python -m scripts.safety_test
"""

import tempfile
from pathlib import Path

from danica.audit import AuditLog, verify
from danica.secret_scan import find_secrets, is_sensitive_path, redact
from danica.tools import run_tool
from danica.workspace import Workspace


def check(label: str, condition: bool) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    assert condition, label


# Fake secrets, split so this file itself stays clean
FAKE_AWS = "AKIA" + "IOSFODNN7EXAMPLE"
FAKE_GH = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
FAKE_PASS = 'password = "Sup3r' + 'S3cretValue99"'
FAKE_KEY = "-----BEGIN RSA " + "PRIVATE KEY-----\nMIIEabc123\n-----END RSA " + "PRIVATE KEY-----"

# --- scanner: detection ---
kinds = {k for k, _, _ in find_secrets(f"{FAKE_AWS}\n{FAKE_GH}\n{FAKE_PASS}\n{FAKE_KEY}")}
check("detects AWS key", "AWS access key" in kinds)
check("detects GitHub token", "GitHub token" in kinds)
check("detects password assignment", "credential assignment" in kinds)
check("detects whole private key block", "private key" in kinds)

# --- scanner: false positives ---
for harmless in [
    "password = get_password_from_user()",
    "token_count = 5",
    'api_key = "YOUR_API_KEY_HERE_1234"',
    "def main():\n    print('hello world')",
]:
    check(f"no false positive: {harmless[:30]!r}", not find_secrets(harmless))

# --- redaction ---
text, found = redact(f"key is {FAKE_AWS} ok")
check("redact removes the secret", FAKE_AWS not in text and "[REDACTED:AWS access key]" in text)
check("redact leaves clean text alone", redact("nothing here") == ("nothing here", []))
text, _ = redact(FAKE_KEY)
check("private key body is redacted too", "MIIEabc123" not in text)

# --- sensitive paths ---
for p, expected in [(".env", True), ("config/prod.env", True), (".env.local", True),
                    (".env.example", False), ("id_rsa", True), (".ssh/config", True),
                    ("server.pem", True), ("src/app.py", False), (".venv/lib/x.py", False)]:
    check(f"is_sensitive_path({p!r}) == {expected}", bool(is_sensitive_path(Path(p))) == expected)

# --- guarded tools ---
with tempfile.TemporaryDirectory() as tmp:
    ws = Workspace(Path(tmp) / "ws")

    check("writing a .env with harmless content is allowed",
          run_tool("write_file", {"path": ".env", "content": "MARKER_LINE=1\n"}, ws).startswith("OK"))
    check("read_file blocks .env", "blocked" in run_tool("read_file", {"path": ".env"}, ws))
    check("edit_file blocks .env",
          "blocked" in run_tool("edit_file", {"path": ".env", "old_string": "1", "new_string": "2"}, ws))
    check("grep skips .env", "No matches" in run_tool("grep", {"pattern": "MARKER_LINE"}, ws))

    check("write_file refuses secrets",
          "refused" in run_tool("write_file", {"path": "cfg.py", "content": FAKE_PASS}, ws))
    check("refused file was not created", not (ws.root / "cfg.py").exists())
    check("write_file allows placeholders",
          run_tool("write_file", {"path": "cfg.py", "content": 'API_KEY = "YOUR_API_KEY"\n'}, ws).startswith("OK"))
    check("edit_file refuses secrets in new_string",
          "refused" in run_tool("edit_file", {"path": "cfg.py", "old_string": "YOUR_API_KEY",
                                              "new_string": FAKE_GH}, ws))

# --- audit chain ---
with tempfile.TemporaryDirectory() as tmp:
    path = Path(tmp) / "logs" / "audit.jsonl"
    log = AuditLog(path, session="t1")
    log.append("session_start")
    log.append("tool_call", tool="ls")
    log.append("tool_call", tool="cat")
    ok, count, _ = verify(path)
    check("fresh chain verifies", ok and count == 3)

    AuditLog(path, session="t2").append("session_start")  # simulates a restart
    ok, count, _ = verify(path)
    check("chain continues across restarts", ok and count == 4)

    good = path.read_text(encoding="utf-8")

    lines = good.splitlines()
    lines[1] = lines[1].replace('"ls"', '"rm"')  # edit an entry
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ok, _, problem = verify(path)
    check("editing an entry is detected", not ok and "edited" in problem)

    lines = good.splitlines()
    del lines[1]  # delete an entry
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ok, _, problem = verify(path)
    check("deleting an entry is detected", not ok and "chain broken" in problem)

print("\nAll good.")