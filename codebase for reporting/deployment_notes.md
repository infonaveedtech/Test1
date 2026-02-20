# Deployment Guide – NL→SQL Assistant (High-Level, A→Z)

This document explains **how to deploy** the NL→SQL system in a production-ish way.

It assumes:

* You already have:

  * `api.py` → FastAPI backend
  * `frontend.py` or `app.py` → UI (Streamlit or similar)
  * RAG (FAISS indexes) in a folder (e.g. `data/faiss/` or `docs/faiss/`)
  * Oracle DB connection via a helper (e.g. `oracle_exec.py`)
  * Local LLM currently (e.g. Qwen 30B) running on your machine

The goal is to run this as a **real service**:

* Backend running in a **Docker container**
* LLM running on a **GPU machine**
* RAG indexes available inside the backend container
* Backend securely connecting to Oracle

---

## 0. Overview Architecture

Target architecture (conceptual):

```text
[User Browser]
    |
  HTTPS
    |
[Frontend UI]  (Streamlit / web app)
    |
  HTTPS (internal)
    |
[Backend API Service]  (FastAPI in api.py, running in Docker)
    |
    |---> [LLM Server]   (Qwen 30B on GPU VM, HTTP API)
    |
    |---> [Oracle DB]    (production database)
    |
    '---> [RAG/FAISS]    (index files inside the container)
```

The rest of this doc explains how to get there.

---

## 1. Pre-Deployment Decisions

Before touching Docker or servers, decide:

1. **Who will use this?**

   * Internal users only? (easier: can be on VPN / internal network)
   * Public/general users? (requires strong auth, rate limiting, etc.)

2. **Where will the LLM run?**

   * **Option A (self-hosted)**: GPU VM you manage (Qwen 30B server)
   * **Option B (managed)**: external API provider (OpenAI, etc.)
     For this doc we assume **Option A: GPU VM with Qwen**.

3. **Where will the backend run?**

   * A cloud VM
   * Or container platform (ECS, Kubernetes, etc.)
     Technically it’s just: “run this Docker image on a server.”

---

## 2. Prepare the Code for Configurable LLM

Right now, your code calls the LLM **locally**, something like:

```python
# Example / pseudo-code
resp = requests.post(
    "http://localhost:11434/v1/chat/completions",
    json={"model": "qwen-30b", "messages": messages}
)
```

You want to move to **config-based** LLM settings.

### 2.1. Add environment-based config

Somewhere central (e.g. `llm_client.py`), create a helper:

```python
# llm_client.py (example)
import os
import requests

LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://localhost:11434")
LLM_API_KEY = os.getenv("LLM_API_KEY")   # optional
LLM_MODEL_NAME = os.getenv("LLM_MODEL_NAME", "qwen-30b")

def call_llm(messages, temperature=0.1, **extra_params):
    headers = {}
    if LLM_API_KEY:
        headers["Authorization"] = f"Bearer {LLM_API_KEY}"

    payload = {
        "model": LLM_MODEL_NAME,
        "messages": messages,
        "temperature": temperature,
        **extra_params,
    }

    resp = requests.post(
        f"{LLM_BASE_URL}/v1/chat/completions",
        json=payload,
        headers=headers,
        timeout=1200,
    )
    resp.raise_for_status()
    return resp.json()
```

Then update all agents to use `call_llm(...)` instead of directly calling `localhost`.

### 2.2. Dev vs Prod

* **Dev (on your laptop)**:

  * `LLM_BASE_URL=http://localhost:11434`
* **Prod (server)**:

  * `LLM_BASE_URL=https://llm.yourcompany.com`
  * `LLM_API_KEY=<secret>`
  * `LLM_MODEL_NAME=qwen-30b` (or whatever you deploy)

---

## 3. Set Up the LLM Server (GPU VM)

**Goal:** Qwen 30B runs on a **GPU VM** and exposes an HTTP endpoint.

High-level steps (details depend on serving stack: vLLM, Ollama, LM Studio, etc.):

1. **Provision a GPU VM** (example spec for a 30B model):

   * GPU: 1 × A100 40GB / L40S 48GB (or similar)
   * CPU: 8–16 vCPUs
   * RAM: 64–128 GB
   * Disk: 200+ GB

2. **Install dependencies** on the VM:

   * Python / Docker (if needed)
   * Your chosen serving tool (e.g. vLLM, Ollama, etc.)

3. **Download / load Qwen 30B model** on this machine.

4. **Run the model server**, something that exposes an endpoint like:

   `POST /v1/chat/completions`

   Example (pseudo):

   ```bash
   # This depends on the serving project you use. Concept only.
   llm-server \
     --model /models/qwen-30b \
     --host 0.0.0.0 \
     --port 8001
   ```

5. **Put it behind HTTPS**:

   * Either:

     * Use a reverse proxy (nginx/traefik) on same VM, or
     * Put it behind a cloud load balancer.
   * Decide on a domain, e.g. `llm.yourcompany.com`.

6. **Add authentication**:

   * Minimum: API key in `Authorization` header.
   * Can be something simple at first, but do not leave it wide open.

7. **Test from your laptop**:

   ```bash
   curl -X POST https://llm.yourcompany.com/v1/chat/completions \
     -H "Authorization: Bearer <API_KEY>" \
     -H "Content-Type: application/json" \
     -d '{
       "model": "qwen-30b",
       "messages": [{"role": "user", "content": "Hello"}]
     }'
   ```

If this works, your **LLM server is ready**.
Now you just point your backend to this URL via env vars.

---

## 4. Prepare RAG / FAISS for Docker

Your RAG currently uses FAISS indexes from a folder, e.g.:

* `data/faiss/` or `docs/faiss/`
* plus maybe `docs/registry.json`, etc.

For now, the simplest approach is:

> **Include those index files inside the backend Docker image.**

### 4.1. Keep FAISS files in the repo

Ensure the FAISS index files and registry are part of the project structure, like:

```text
project_root/
  api.py
  ...
  data/
    faiss/
      fewshots/
        index.faiss
        metadatas.jsonl
        ids.jsonl
      glossary/
        ...
  prompts/
    ...
```

### 4.2. Use relative paths in code

In your retrieval code (e.g. `retrieve_context.py`), make sure you use paths that will be valid inside the container, e.g.:

```python
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

FAISS_ROOT = DATA_DIR / "faiss"
REGISTRY_PATH = DATA_DIR / "registry.json"
```

Or similar. The key is: **no absolute paths pointing to your local machine**.

When you copy the entire project into the Docker image, these files will be included.

---

## 5. Oracle Integration from Inside Docker

You already call Oracle via a helper (e.g. `oracle_exec.py`).

You need to make sure this still works from inside the container.

### 5.1. Use environment variables for Oracle

Code example:

```python
import os
import oracledb

ORACLE_DSN = os.getenv("ORACLE_DSN")
ORACLE_USER = os.getenv("ORACLE_USER")
ORACLE_PASSWORD = os.getenv("ORACLE_PASSWORD")

def get_connection():
    return oracledb.connect(
        user=ORACLE_USER,
        password=ORACLE_PASSWORD,
        dsn=ORACLE_DSN
    )
```

### 5.2. Networking

Your backend container must be able to reach the Oracle host:

* If Oracle is on-prem:

  * You might need VPN / private connection between your cloud and data center.
* If Oracle is in cloud:

  * Ensure the backend is in the same VPC / network, or has routing configured.

This part is mostly infra / network setup, not code.

### 5.3. Running the container with secrets

When running the backend container, pass Oracle secrets via env vars:

```bash
docker run \
  -e ORACLE_DSN="host:port/service" \
  -e ORACLE_USER="ats_user" \
  -e ORACLE_PASSWORD="super-secret" \
  -e LLM_BASE_URL="https://llm.yourcompany.com" \
  -e LLM_API_KEY="some-secret" \
  -e LLM_MODEL_NAME="qwen-30b" \
  -p 8000:8000 \
  your-backend-image:latest
```

In real production, you’d likely use a **secret manager** instead of putting secrets in plain text.

---

## 6. Dockerizing the Backend (FastAPI)

Here’s a **basic Dockerfile** example for the backend:

```dockerfile
# Dockerfile (backend)

FROM python:3.11-slim

# 1. Set working directory inside the container
WORKDIR /app

# 2. Install system deps (optional, e.g. for Oracle client/PDF tools)
# RUN apt-get update && apt-get install -y <needed-packages> && rm -rf /var/lib/apt/lists/*

# 3. Copy and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 4. Copy the entire project (code + prompts + FAISS indexes)
COPY . .

# 5. Expose FastAPI port
EXPOSE 8000

# 6. Command to start the app
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]
```

### 6.1. Build and run locally (for testing)

```bash
# Build the image
docker build -t nl-sql-backend:latest .

# Run the container (local test)
docker run \
  -e ORACLE_DSN="..." \
  -e ORACLE_USER="..." \
  -e ORACLE_PASSWORD="..." \
  -e LLM_BASE_URL="http://host.docker.internal:11434" \
  -e LLM_MODEL_NAME="qwen-30b" \
  -p 8000:8000 \
  nl-sql-backend:latest
```

> Note: `host.docker.internal` lets the container call your host machine, useful for local dev if the LLM is still running on your laptop.

If you hit `http://localhost:8000/docs` and see your FastAPI docs, the backend image works.

---

## 7. Frontend (Optional, for Now)

You can treat your UI (`frontend.py` or `app.py`) in two ways:

1. **Internal tool / dev mode**:

   * Run Streamlit locally on your laptop.
   * Point it to the deployed backend URL.

2. **Also containerize frontend** (later):

   * Create another Dockerfile for Streamlit.
   * Deploy it similarly and put it behind HTTPS.

For now, the key part is the **backend + LLM + Oracle**; frontend can stay simple while you test.

---

## 8. Putting It All Together (Step-by-Step Summary)

**Step 1 – Refactor LLM calls to use env-based URL**

* Introduce a `call_llm()` helper that reads `LLM_BASE_URL`, `LLM_MODEL_NAME`, `LLM_API_KEY`.
* Update all agents to use this helper.

**Step 2 – Make sure RAG file paths are relative and inside the project**

* Confirm FAISS/txt/prompt files are in the repo.
* Use relative paths or a `BASE_DIR` pattern.

**Step 3 – Prepare Oracle config with env vars**

* Confirm Oracle code reads `ORACLE_DSN`, `ORACLE_USER`, `ORACLE_PASSWORD` from env.
* Test locally without Docker.

**Step 4 – Spin up LLM server on a GPU VM**

* Provision GPU machine.
* Install serving stack + model.
* Expose `/v1/chat/completions` endpoint over HTTPS with API key.
* Test via `curl`.

**Step 5 – Create Dockerfile for backend and build image**

* Copy project into image.
* Install dependencies.
* Start FastAPI via `uvicorn`.

**Step 6 – Run backend container in the target environment**

* Pass Oracle + LLM env vars when starting the container.
* Ensure networking to Oracle and LLM works.
* Test `/health` or `/docs` endpoint from a browser.

**Step 7 – Point frontend to the backend**

* In `frontend.py` / `app.py`, change API base URL from `localhost` to your backend URL.
* Test full NL→SQL flow end-to-end.

---

## 9. Future Improvements (for later discussion)

Once the basics work, you can talk with your senior about:

* **Authentication & Authorization**

  * JWT / SSO so only approved users can access the app.
* **Rate limiting & quotas**

  * To protect the LLM and DB from abuse.
* **Monitoring & logging**

  * Logs for each request, latency, tokens, errors.
* **CI/CD**

  * Automated build and deploy of the backend image on each commit.
* **RAG index management**

  * Volume mounts and dynamic refresh instead of baking FAISS into the image.

---

You can share this doc as a starting blueprint.
When you’re ready, we can zoom into any one section (for example, “let’s design the env variables + config layout properly” or “let’s refine the Dockerfile for your exact folder structure”).
