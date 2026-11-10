# Third-party software

Danica depends on the following packages. Each is distributed under its own license;
see the package's own repository or installed metadata for the full text.
This list covers the direct dependencies known at the time of writing and may be incomplete.

| Package | License | Notes |
|---|---|---|
| Textual | MIT | terminal UI |
| Rich | MIT | text rendering |
| Pygments | BSD-2-Clause | syntax highlighting (via Rich) |
| httpx | BSD-3-Clause | llama-server client |
| requests | Apache-2.0 | context-size lookup |
| textual-image | LGPL-3.0-or-later | optional: inline images. Installed separately; not modified. |

Danica does not include or redistribute any language model. llama-server (llama.cpp, MIT)
and the models you run with it are separate software under their own licenses.
