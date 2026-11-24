# Danica

Danica is a local-first terminal assistant for coding, research, planning, and creating documents and presentations. It connects to a local `llama-server` and can work with files in your project.

## Get started

Requires Python 3.11 or newer and a running [llama-server](https://llama.app/docs/serve) with a model loaded.

```powershell
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e .
danica
```

When prompted, enter your server address (default: `http://127.0.0.1:8080`). Start the server with a model first, for example:

```powershell
llama-server -m your-model.gguf -c 32768 --jinja --port 8080
```

Danica saves its settings and data under `%APPDATA%\Danica`.
