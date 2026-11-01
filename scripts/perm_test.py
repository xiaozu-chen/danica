"""
perm_test.py - Checks tier classification and the allow/ask/deny logic.
No model or server needed. Run:  python -m scripts.perm_test
"""

from danica.permissions import PermissionPolicy, Tier, classify


def check(label: str, condition: bool) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    assert condition, label


def tier_of(command: str) -> Tier:
    return classify("run_shell", {"command": command})[0]


# --- classification ---
check("read_file is READ", classify("read_file", {})[0] == Tier.READ)
check("write_file is WRITE", classify("write_file", {})[0] == Tier.WRITE)
check("python app.py is EXECUTE", tier_of("python app.py") == Tier.EXECUTE)
check("Remove-Item is DESTRUCTIVE", tier_of("Remove-Item foo.txt") == Tier.DESTRUCTIVE)
check("case does not matter", tier_of("REMOVE-ITEM foo.txt") == Tier.DESTRUCTIVE)
check("destructive hidden in a chain is caught", tier_of("echo hi; rm x") == Tier.DESTRUCTIVE)
check("curl is flagged", tier_of("curl http://example.com") == Tier.DESTRUCTIVE)
check("drive path is OUTSIDE", tier_of("type C:\\Windows\\win.ini") == Tier.OUTSIDE)
check("parent dir is OUTSIDE", tier_of("cd ..\\..") == Tier.OUTSIDE)
check("env var read is OUTSIDE", tier_of("echo $env:APPDATA") == Tier.OUTSIDE)
check("a URL is NOT mistaken for a drive path", tier_of("echo http://example.com") == Tier.EXECUTE)
check("str.format in python is not a disk format", tier_of("python -c \"print('{}'.format(1))\"") == Tier.EXECUTE)
check("unknown tool name is harmless READ", classify("nonsense", {})[0] == Tier.READ)

# --- policy: a scripted fake user ---
answers: list[str] = []
asked: list[str] = []


def fake_ask(tool, summary, tier, reason, can_remember):
    asked.append(summary)
    return answers.pop(0)


p = PermissionPolicy(ask=fake_ask)

check("READ never asks", p.check("read_file", {"path": "x"})[0] and not asked)

answers[:] = ["once"]
check("EXECUTE asks and 'once' allows", p.check("run_shell", {"command": "python a.py"})[0] and len(asked) == 1)
answers[:] = ["once"]
p.check("run_shell", {"command": "python a.py"})
check("'once' is not remembered (asked again)", len(asked) == 2)

answers[:] = ["session"]
p.check("run_shell", {"command": "python a.py"})
n = len(asked)
check("'session' remembers the program", p.check("run_shell", {"command": "python b.py"})[0] and len(asked) == n)

answers[:] = ["deny"]
check("a chain with an unapproved program asks",
      not p.check("run_shell", {"command": "python a.py; node x.js"})[0] and len(asked) == n + 1)

answers[:] = ["session"]
p.check("run_shell", {"command": "Remove-Item x"})
n = len(asked)
answers[:] = ["deny"]
ok, msg = p.check("run_shell", {"command": "Remove-Item x"})
check("DESTRUCTIVE is never remembered", len(asked) == n + 1 and not ok and msg.startswith("ERROR"))

answers[:] = ["deny"]
check("session-approved program + destructive tail still asks",
      not p.check("run_shell", {"command": "python a.py; Remove-Item x"})[0])

check("no ask callback = deny", not PermissionPolicy(ask=None).check("run_shell", {"command": "python a.py"})[0])

full = PermissionPolicy(ask=None)
full.full_access = True
check("full access allows everything", full.check("run_shell", {"command": "Remove-Item x"})[0])

check("reading .env in the shell is OUTSIDE", tier_of("type .env") == Tier.OUTSIDE)
check(".venv is not mistaken for .env", tier_of("python -m venv .venv") == Tier.EXECUTE)

print("\nAll good.")