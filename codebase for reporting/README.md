# 🧠 **NL→SQL Multi-Agent Analytics Platform**

### *Natural Language → Oracle SQL → Execution → Reports (HTML/CSV/PDF/DOCX)*


## 📄 **Executive Summary**

This repository contains a **full multi-agent analytics platform** that transforms natural-language business questions into:

1. A **validated Oracle SQL query**,
2. Executed results (optional),
3. A downloadable report (HTML, CSV, DOCX, or PDF),
4. Using both **multi-agent LLM reasoning** and **retrieval-augmented generation**.

It includes:

* A **production-ready FastAPI backend**
* A **Streamlit frontend** that communicates **ONLY** via API
* A **direct-mode Streamlit app** for dev/debug (optional)
* A 4-agent pipeline (Intent → Planner → Composer → Validator)
* A RAG engine (FAISS + docs + fewshots + glossary)
* An Oracle SQL execution engine
* Full reporting utilities

This README provides a complete enterprise-grade documentation of architecture, configuration, deployment, APIs, and usage.

---

# 🏗️ **System Architecture Overview**

The system consists of the following major layers:

```
                ┌──────────────────────────────────┐
                │            FRONTEND              │
                │   (Streamlit API-based UI)       │
                └──────────────────────────────────┘
                               │ HTTP
                               ▼
         ┌──────────────────────────────────────────────────────────┐
         │                         BACKEND                          │
         │                         (FastAPI)                        │
         ├──────────────────────────────────────────────────────────┤
         │   /v1/intent → Agent-0 (Intent Classifier)               │
         │   /v1/pipeline/run → Full NL→SQL Pipeline                │
         │   /v1/sql/execute → Oracle SQL Execution                 │
         │   /v1/report → Report Generator (HTML/CSV/PDF/DOCX)      │
         └──────────────────────────────────────────────────────────┘
                               │
               ┌──────────────┼─────────────────┬──────────────┐
               ▼              ▼                 ▼              ▼
     ┌─────────────────┐ ┌───────────────┐ ┌───────────────┐ ┌───────────────────┐
     │  RAG Engine     │ │ Agent-1       │ │ Agent-2       │ │ Agent-3           │
     │ (FAISS + docs)  │ │ (Planner)     │ │ (Composer)    │ │ (Verifier/Refiner)│
     └─────────────────┘ └───────────────┘ └───────────────┘ └───────────────────┘
                               │
                               ▼
                   ┌──────────────────────────────┐
                   │ Oracle Execution Layer        │
                   └──────────────────────────────┘
                               │
                               ▼
                   ┌──────────────────────────────┐
                   │ Report Engine (HTML/CSV/PDF) |
                   |         DB Results           |
                   |             ↓                |
                   |         Column Customisation |
                   |             ↓                |
                   |         Layout Engine        |
                   |             ↓                | 
                   |    HTML/PDF/DOCX Renderer    |
                   |                              |
                   └──────────────────────────────┘
```

---

# 🤖 **Multi-Agent Pipeline (0 → 3)**

## **Agent-0 — Intent Classifier (Mandatory Gatekeeper)**

The pipeline begins with an intent filtering agent:

* Rejects small talk / greetings
* Rejects unsafe or irrelevant requests
* Allows NL→SQL queries
* Optionally rewrites unclear questions
* Returns structured intent JSON

Example output:

```json
{
  "intent": "sql_query",
  "should_run_pipeline": true,
  "reason": "...",
  "assistant_reply": null
}
```

---

## **RAG Layer (Retrieval-Augmented Context)**

Powered by FAISS vector search + docstore:

* `schema_card.md` (table definitions)
* Few-shot example JSONL
* Glossary entries
* Narrative domain docs

RAG produces:

```json
{
  "chapters_selected": [...],
  "fewshots": [...],
  "glossary": [...],
  "context_length": ...
}
```

---

## **Agent-1 — Planner (NL → JSON Envelope)**

Transforms the user query + RAG context into a structured SQL plan:

* tables
* joins
* filters
* metrics
* group-by
* time-window
* date-key
* row-limit

Example:

```json
{
  "tables": ["ATS.TRADES", "ATS.SUBSCRIBED_BROKERS"],
  "joins": ["..."],
  "metrics": [{"name": "notional", "expr": "SUM(PRICE * VOLUME)"}],
  "filters": ["ENTRY_DATETIME >= SYSDATE - 30"],
  "group_by": ["BROKER_NAME"]
}
```

---

## **Agent-2 — Composer (JSON → Oracle SQL)**

Using:

* Schema card
* Envelope from Agent-1
* Oracle join rules
* Metrics
* Time windows

Produces:

```sql
SELECT ...
FROM ...
JOIN ...
WHERE ...
GROUP BY ...
ORDER BY ...
FETCH FIRST ...
```

---

## **Agent-3 — Verifier & Refiner (Final SQL)**

Checks:

* Column validity
* Table existence
* Owner-qualified names
* Joins correctness
* Syntax structure
* Logical consistency

Produces final SQL + rationale:

```json
{
  "sql": "...",
  "rationale": "validated; no issues found"
}
```

---

# 🗂️ **Folder Structure (Updated)**

```
/
├── api.py                           # FastAPI entrypoint (backend)
├── app.py                           # Legacy standalone Streamlit pipeline (direct mode)
├── frontend.py                      # API-based Streamlit UI
│
├── endpoints/
│   ├── intent.py                    # /v1/intent
│   ├── sql.py                       # /v1/sql/execute
│   ├── pipeline.py                  # /v1/pipeline/run (Agents 0–3)
│   └── report.py                    # /v1/report
│
├── agents/
│   ├── agent0_intent_llm.py
│   ├── agent1_planner_llm.py
│   ├── agent2_composer_llm.py
│   └── agent3_verifier_refiner_llm.py
│
├── retrieve_context.py              # RAG engine
├── build_faiss_indexes.py           # FAISS builder
│
├── db/
│   └── oracle_exec.py               # Oracle SQL execution
│
├── reports/
    templates/
        report_grouped.html      # NEW grouped report template
    report_utils.py              # Updated with grouping engine
    pdf_utils.py
│
├── data/
│   ├── docs/
│   │   ├── fewshots.docs.jsonl
│   │   └── glossary.docs.jsonl
│   ├── faiss/
│   └── registry.json
│
└── out/                             # Artifacts (optional)
```

---

# ⚙️ **Configuration & Environment**

## **Required environment variables:**

| Variable           | Description              |
| ------------------ | ------------------------ |
| `ORACLE_DSN`       | Oracle connection string |
| `ORACLE_USER`      | Oracle username          |
| `ORACLE_PASS`      | Oracle password          |
| `WKHTMLTOPDF_PATH` | path to wkhtmltopdf exe  |

---

# 🏁 **Running the Project (Two Modes)**

---

# **Mode 1 — Direct Pipeline (app.py)**

Runs everything internally without the backend.

```bash
streamlit run app.py
```

Use for:

* Debugging agents
* Testing RAG
* Local experimentation

---

# **Mode 2 — API Backend + Streamlit Frontend**

This is the recommended production architecture.

## **Step 1 — Start backend (FastAPI)**

```bash
uvicorn api:app --reload --port 8000
```

Available endpoints:

* `/v1/intent`
* `/v1/sql/execute`
* `/v1/pipeline/run`
* `/v1/report`
* Swagger: [http://localhost:8000/docs](http://localhost:8000/docs)

---

## **Step 2 — Start frontend (Streamlit UI via API)**

```bash
streamlit run frontend.py --server.port 6768
```

Features:

* Query input
* Pipeline run
* SQL preview
* DB execution
* Report download (HTML/CSV/PDF/DOCX)

---

# 📡 **API Documentation**

The backend exposes the following endpoints:

---

## ## **1. `GET /health`**

Simple health check.

---

## ## **2. `POST /v1/intent`**

Runs Agent-0.

### Request:

```json
{
  "query": "Hello, how are you?"
}
```

### Response:

```json
{
  "intent": "non_db_smalltalk",
  "should_run_pipeline": false
}
```

---

## ## **3. `POST /v1/sql/execute`**

Executes SQL on Oracle DB.

### Body:

```json
{
  "sql": "SELECT 1 FROM DUAL",
  "row_limit": 5
}
```

---

## ## **4. `POST /v1/pipeline/run`**

Full NL→SQL pipeline (Agents 0→3, optional DB).

### Body:

```json
{
  "query": "Top 5 brokers by notional...",
  "options": {
    "run_db": true,
    "row_limit": 200
  }
}
```

---

## ## **5. `POST /v1/report`**

Generates HTML / CSV / DOCX / PDF.

### Body:

```json
The /v1/report endpoint now supports dynamic layout selection:

"layout": {
  "layout_type": "flat" | "grouped",
  "group_by": ["COLUMN_NAME"],
  "detail_columns": ["COL_A", "COL_B", "COL_C"]
}
```

- flat → single table (default)

- grouped → sectioned report (broker-wise, client-wise, symbol-wise, etc.)

- group_by controls grouping keys

- detail_columns controls which columns appear inside each group section

- Supported in HTML, PDF, DOCX (CSV remains flat)


---

# 🧪 **Testing the System**

## **Swagger UI**

[http://localhost:8000/docs](http://localhost:8000/docs)

## **Postman Collection**

Included: `postman_collection.json`

---

### **Dockerization (Completed)**

Backend runs fully inside Docker using `Dockerfile.api`.
Supports:

* Multi-agent pipeline
* SQL execution
* HTML/PDF/DOCX report generation
* Grouped reports
* RAG (if enabled)

---
