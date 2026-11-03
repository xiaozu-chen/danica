# Phase 7: Output systems

Phase 7 lets Danica turn structured data and Markdown into offline-first
artifacts using bundled templates. User/model content is treated as data and
escaped before it is inserted into HTML or SVG. The renderer does not execute
content from input files or fetch remote charting libraries.

## What changed

- `create_chart` builds bar, line, scatter, area, pie, and table HTML views from
  bounded JSON input. `create_dashboard` composes several of these views into
  one offline HTML page.
- `create_document` converts Markdown into Markdown, DOCX, or PDF. It preserves
  citation text as written; citation validation remains the responsibility of
  the web research and agent citation checks.
- `create_diagram` renders Mermaid flowcharts and a Graphviz DOT node/edge
  subset into SVG without an external executable.
- `create_slides` creates a themed PPTX deck and a self-contained HTML
  slideshow with keyboard and button navigation.
- Generated artifact paths are collected after a task. `/artifacts` lists
  non-HTML files with clickable local links and visible paths that can be
  copied. HTML is excluded from that list and is opened only through a valid
  `<present file="relative/path.html"/>` tag for a generated file.
- For the deferred real-model workflow, see the Phase 7 checklist in
  [LIVE_TEST.md](../LIVE_TEST.md).

## Input formats

Charts use a JSON object with labels and named series:

```json
{
  "labels": ["Jan", "Feb", "Mar"],
  "series": [{"name": "Sales", "values": [12, 18, 15]}]
}
```

Dashboards use a `charts` array. Each entry has a supported `type`, a `title`,
and the same chart JSON object as `data`.

Documents read a Markdown source file from the workspace. The DOCX/PDF
converters handle headings, paragraphs, and unordered list items; other
Markdown syntax is emitted as plain text.

Diagrams read a `.mmd` or `.dot` text file. Mermaid rendering supports
`flowchart`/`graph` direction declarations, simple node shapes, labeled nodes,
and `-->`/`--` edges with optional edge labels. Graphviz rendering supports
simple node labels and `->`/`--` edges. These are intentionally bounded
subsets, not full Mermaid or Graphviz implementations. SVG is the output
format; Graphviz/DOT is rendered by the local template engine and does not
require Graphviz to be installed.

Slide JSON contains a `slides` array of objects with `title` and optional
`bullets`. Each deck has a title slide and one content slide per JSON entry.

## Limits and safety

Source files are limited to 1 MB, output files to 20 MB, chart inputs to 100
labels and 12 series, dashboards to 12 charts, diagrams to 80 nodes and 120
edges, and decks to 40 slides with at most 20 bullets per slide. Paths must
remain inside the workspace. Credential files and secret-like source text are
rejected. HTML/SVG content and labels are escaped; the templates contain no
remote assets.

In project mode, a task tainted by fetched web content requires fresh
confirmation before an artifact renderer writes into the project, just like
other project writes.

## Verification

Run the offline Phase 7 exit test from the repository root:

```powershell
.\.venv\Scripts\python.exe -m scripts.phase7_exit
```

The test validates all output formats, generated HTML/SVG, presentation-tag
filtering, and task artifact reporting without a live model or browser.
