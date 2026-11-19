"""Read string fields from an incomplete JSON object during streaming."""

from __future__ import annotations

_WHITESPACE = " \t\r\n"

_ESC = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", '"': '"', "\\": "\\", "/": "/"}


def _read_string(buf: str, i: int) -> tuple[str, int, bool]:
    """Decode a JSON string beginning at ``i - 1``.

    Return decoded text, the next unread index, and whether the string closed.
    An incomplete escape remains unread so the caller can retry with more input.
    """
    output: list[str] = []
    length = len(buf)
    while i < length:
        ch = buf[i]
        if ch == '"':
            return "".join(output), i + 1, True
        if ch != "\\":
            output.append(ch)
            i += 1
            continue
        if i + 1 >= length:  # A trailing backslash may be completed by the next chunk.
            break
        nxt = buf[i + 1]
        if nxt == "u":
            if i + 6 > length:  # Wait until the full Unicode escape has arrived.
                break
            try:
                output.append(chr(int(buf[i + 2:i + 6], 16)))
            except ValueError:
                pass
            i += 6
        else:
            output.append(_ESC.get(nxt, nxt))
            i += 2
    return "".join(output), i, False


def partial_fields(buf: str) -> dict[str, str]:
    """Return decoded top-level string fields from possibly incomplete JSON."""
    out: dict[str, str] = {}
    i = 0
    length = len(buf)
    while i < length:
        if buf[i] != '"':
            i += 1
            continue
        key, i, closed = _read_string(buf, i + 1)
        if not closed:
            break
        j = i
        while j < length and buf[j] in _WHITESPACE:
            j += 1
        if j >= length:
            break
        if buf[j] != ":":
            continue                         # that string was a value, not a key
        j += 1
        while j < length and buf[j] in _WHITESPACE:
            j += 1
        if j >= length:
            break
        if buf[j] == '"':
            val, i, closed = _read_string(buf, j + 1)
            out[key] = val
            if not closed:
                break
        else:  # Primitive values cannot contribute a string field.
            k = buf.find(",", j)
            if k == -1:
                break
            i = k + 1
    return out
