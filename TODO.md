The lightest and easiest way to host **SearXNG** locally for personal use or AI tool integration (like Ollama or Open WebUI) is using **Docker**.

---

### Step 1: Create a Project Directory

Open your terminal and create a dedicated folder for SearXNG:

```bash
mkdir -p ~/searxng/searxng
cd ~/searxng

```

---

### Step 2: Download Configuration & Compose Files

Pull the standard environment files from the official SearXNG repository:

```bash
curl -fsSL -O https://raw.githubusercontent.com/searxng/searxng/master/container/docker-compose.yml \
-O https://raw.githubusercontent.com/searxng/searxng/master/container/.env.example

mv .env.example .env

```

---

### Step 3: Configure Settings for Local Use

1. **Generate a Secret Key:**
SearXNG requires a secret key for session encryption. Run this command to generate one:
```bash
openssl rand -hex 32

```


*Copy the output string.*
2. **Create `searxng/settings.yml`:**
Create a light configuration file inside the `searxng` directory:
```bash
nano searxng/settings.yml

```


Paste the following lightweight configuration (replace `YOUR_SECRET_KEY` with the output from step 1):
```yaml
use_default_settings: true
server:
  secret_key: "YOUR_SECRET_KEY"
  limiter: false       # Disable rate limiting for purely local/personal use
  image_proxy: true
search:
  safe_search: 0
search_engines:
  - name: google
    engine: google
    shortcut: g

```


*Save and close (`Ctrl + O`, `Enter`, `Ctrl + X`).*

---

### Step 4: Start SearXNG

Launch the container stack in detached mode:

```bash
docker compose up -d

```

* **Verification:** Open your web browser and go to `http://localhost:8080`. You should see your local SearXNG search interface.
* **Status Check:** Run `docker compose ps` to verify the containers are healthy.

---