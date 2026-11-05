The `deck_test.py` exposed an issue in `deckgen.py`

Replaced:

```python

        elif b["t"] == "p" and plain(b["runs"]).lower().startswith("notes:"):
            cur["notes"].append(plain(b["runs"])[6:].strip())

```

With:

```python

        elif b["t"] == "p" and re.search(r"(?i)(?:^|\s)Notes:\s*", plain(b["runs"])):
            paragraph = plain(b["runs"])
            markers = list(re.finditer(r"(?i)(?:^|\s)Notes:\s*", paragraph))
            prefix_end = len(paragraph[:markers[0].start()].rstrip())
            if prefix_end:
                prefix_runs = []
                remaining = prefix_end
                for text, style in b["runs"]:
                    if remaining <= 0:
                        break
                    prefix_runs.append((text[:remaining], style))
                    remaining -= len(prefix_runs[-1][0])
                cur["body"].append({"t": "p", "runs": prefix_runs})
            for i, marker in enumerate(markers):
                end = markers[i + 1].start() if i + 1 < len(markers) else len(paragraph)
                note = paragraph[marker.end():end].strip()
                if note:
                    cur["notes"].append(note)

```

And added `import re`