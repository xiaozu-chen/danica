"""partial_json.py - read string fields out of a JSON object that is still being streamed."""

_ESC = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", '"': '"', "\\": "\\", "/": "/"}


def _read_string(buf: str, i: int) -> tuple[str, int, bool]:
    """Decode a JSON string whose opening quote is just before index i.
    Returns (text so far, next index, closed?). A half-arrived escape is left for next time."""
    out, n = [], len(buf)
    while i < n:
        ch = buf[i]
        if ch == '"':
            return "".join(out), i + 1, True
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        if i + 1 >= n:                       # lone backslash at the end: wait for more
            break
        nxt = buf[i + 1]
        if nxt == "u":
            if i + 6 > n:                    # \uXXXX not complete yet
                break
            try:
                out.append(chr(int(buf[i + 2:i + 6], 16)))
            except ValueError:
                pass
            i += 6
        else:
            out.append(_ESC.get(nxt, nxt))
            i += 2
    return "".join(out), i, False


def partial_fields(buf: str) -> dict[str, str]:
    """{key: value-so-far} for every top-level STRING field in a possibly unfinished JSON object."""
    out: dict[str, str] = {}
    i, n = 0, len(buf)
    while i < n:
        if buf[i] != '"':
            i += 1
            continue
        key, i, closed = _read_string(buf, i + 1)
        if not closed:
            break
        j = i
        while j < n and buf[j] in " \t\r\n":
            j += 1
        if j >= n:
            break
        if buf[j] != ":":
            continue                         # that string was a value, not a key
        j += 1
        while j < n and buf[j] in " \t\r\n":
            j += 1
        if j >= n:
            break
        if buf[j] == '"':
            val, i, closed = _read_string(buf, j + 1)
            out[key] = val
            if not closed:
                break
        else:                                # number / bool / null: skip to the next comma
            k = buf.find(",", j)
            if k == -1:
                break
            i = k + 1
    return out