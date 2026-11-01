"""
secret_scan.py - Finds secrets in text and credential files by path.

Used in three places:
  - file tools refuse to READ sensitive paths (is_sensitive_path)
  - the agent REDACTS tool output before the model sees it (redact)
  - file tools refuse to WRITE text containing secrets (find_secrets)

Pattern matching only catches well-known formats. It is a net, not a wall.
"""

import re
from pathlib import Path

# ---------------------------------------------------------------- by path

SENSITIVE_NAMES = {
    ".netrc", ".npmrc", ".pypirc", ".git-credentials",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    "credentials", "credentials.json",
    "secrets.json", "secrets.yaml", "secrets.yml",
}
SENSITIVE_SUFFIXES = {".pem", ".key", ".pfx", ".p12", ".ppk", ".kdbx", ".keystore", ".jks"}
SENSITIVE_DIRS = {".ssh", ".aws", ".gnupg", ".azure", ".kube"}
# .env.example and friends are meant to be shared and hold no real values
SAFE_ENV_SUFFIXES = (".example", ".sample", ".template", ".dist")


def is_sensitive_path(path: Path) -> str | None:
    """Return a short reason if the path looks like a credential file, else None.
    Pass a path RELATIVE to the workspace so parent folders outside it don't matter."""
    name = path.name.lower()
    if name == ".env" or name.endswith(".env"):
        return "environment file"
    if name.startswith(".env.") and not name.endswith(SAFE_ENV_SUFFIXES):
        return "environment file"
    if name in SENSITIVE_NAMES:
        return "credentials file"
    if path.suffix.lower() in SENSITIVE_SUFFIXES:
        return "key or certificate file"
    if any(part.lower() in SENSITIVE_DIRS for part in path.parts):
        return "credentials folder"
    return None


# ---------------------------------------------------------------- by content

SECRET_PATTERNS = [
    # The whole block, not just the header (\Z handles a truncated key)
    ("private key", re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)")),
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(
        r"\bgh[pousr]_[A-Za-z0-9]{36,}\b|\bgithub_pat_[A-Za-z0-9_]{50,}\b")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("API key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("JWT", re.compile(
        r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    # name = "value" style assignments; group 1 is just the value
    ("credential assignment", re.compile(
        r"""(?i)(?:api[_-]?key|secret|token|passw(?:or)?d)\w*\s*[:=]\s*['"]?([A-Za-z0-9_\-/+=.]{16,})""")),
]

PLACEHOLDER_WORDS = ("example", "your", "changeme", "placeholder", "xxxx", "dummy", "sample")


def _looks_real(value: str) -> bool:
    """Filters out false positives for the generic assignment pattern."""
    low = value.lower()
    if any(w in low for w in PLACEHOLDER_WORDS):
        return False
    # Real secrets mix letters and digits; names like get_password_from_user don't
    return any(c.isdigit() for c in value) and any(c.isalpha() for c in value)


def find_secrets(text: str) -> list[tuple[str, int, int]]:
    """Return non-overlapping (kind, start, end) spans, in text order."""
    hits = []
    for kind, rx in SECRET_PATTERNS:
        for m in rx.finditer(text):
            if kind == "credential assignment":
                if not _looks_real(m.group(1)):
                    continue
                hits.append((kind, m.start(1), m.end(1)))
            else:
                hits.append((kind, m.start(), m.end()))

    hits.sort(key=lambda h: h[1])
    clean, last_end = [], -1
    for h in hits:
        if h[1] >= last_end:  # skip anything overlapping an earlier hit
            clean.append(h)
            last_end = h[2]
    return clean


def redact(text: str) -> tuple[str, list[str]]:
    """Replace every secret with [REDACTED:kind]. Returns (new_text, kinds_found)."""
    hits = find_secrets(text)
    if not hits:
        return text, []
    out, pos = [], 0
    for kind, start, end in hits:
        out.append(text[pos:start])
        out.append(f"[REDACTED:{kind}]")
        pos = end
    out.append(text[pos:])
    return "".join(out), [kind for kind, _, _ in hits]