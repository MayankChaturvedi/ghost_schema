# Ghost Schema — Design Document

## North Star

Transform a database from a passive storage layer into an active, reasoning-first engine.

Users upload raw, messy data and ask complex analytical questions that require high-level reasoning — without ever defining a schema or writing a script. The system figures out what attributes are needed, computes them on demand, and caches the results for future queries.

The core idea is **Just-In-Time Schema Manifestation**: columns don't exist until a question requires them. Once reasoned into existence, they are cached as first-class citizens of the database.

---

## How a Query Works (End-to-End)

```
User: "find companies selling to the healthcare sector"

  │
  ▼
FastAPI (main.py)
  Receives the query + project name.
  Looks up data/yc_companies.db and passes its path to the Architect.

  │
  ▼
Claude Code Architect (architect.py)
  Spawned as a real `claude -p` subprocess in an isolated temp directory.
  Receives only two things: the query and the DB path.

  Claude Code then autonomously:
    1. Runs `sqlite3` to inspect the schema
    2. Samples rows to understand the data
    3. Checks jit_columns for existing cached attributes
    4. Decides: plain SQL is enough, or LLM inference is needed
    5. If inference needed:
         - Writes inference.py (its own script, no templates)
         - Calls Gemini Flash in parallel for each row
         - Handles rate limits, retries, and errors itself
         - Writes yes/no results to jit_columns
    6. Writes and verifies the final SQL
    7. Outputs: GHOST_SCHEMA_RESULT: { jit_column_name, final_sql, ... }

  │
  ▼
FastAPI (main.py)
  Runs the final SQL against the project DB.
  Streams the answer + JIT distribution back to the UI via SSE.

  │
  ▼
Browser (static/index.html)
  Displays the answer, the live execution log (Claude Code's tool calls),
  and a bar chart of the JIT column distribution (yes/no breakdown).
```

---

## Why Claude Code as the Architect (not a plain API call)

Claude Code is an agentic code-writing loop. It can read files, write scripts,
run them, see errors, and self-correct — all in one unbroken reasoning chain.

A plain LLM API call would require us to:
- Hand-engineer prompts for schema inspection
- Template the inference script
- Build our own retry/self-correction logic
- Handle every edge case explicitly

With Claude Code, we pass it the query and the DB path and get out of the way.
It handles schema discovery, script authoring, error recovery, and SQL generation
entirely on its own. The system has no templates, no planning prompts, no retry code.

---

## Data Model

Each **project** is one SQLite file. A project can have multiple tables
(one per CSV upload). All JIT columns for that project live in the same file.

```
data/
  yc_companies.db
    companies          ← ingested from yc_companies.csv
    medical_papers     ← ingested from medical_papers.csv (second upload)
    jit_columns        ← cache for all LLM-inferred attributes

  another_project.db
    products
    jit_columns
```

### jit_columns table schema

```sql
CREATE TABLE jit_columns (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name  TEXT    NOT NULL,
    entity_id   INTEGER NOT NULL,   -- FK to the main table's id
    column_name TEXT    NOT NULL,   -- e.g. "sells_to_healthcare", "is_b2b"
    value       TEXT,               -- always "yes" or "no" for binary tasks
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(table_name, entity_id, column_name)
)
```

**FIFO eviction** (planned): when the number of distinct JIT columns exceeds
a configured limit, the oldest column is dropped to make room.

---

## File & Folder Reference

```
ghost_schema/
│
├── architect.py          ← The entire agentic brain. Spawns a Claude Code
│                           subprocess with (query, db_path). Streams its
│                           tool calls back as SSE events. Parses the final
│                           GHOST_SCHEMA_RESULT: JSON line from Claude's output.
│                           No templates. No retry logic. Claude Code does it.
│
├── main.py               ← FastAPI server. Owns three concerns:
│                           1. Project management (create/delete .db files)
│                           2. CSV ingestion (CSV → SQLite table)
│                           3. Query routing (pass db_path + query to architect,
│                              run final SQL, stream SSE to browser)
│                           Intentionally thin — no business logic lives here.
│
├── static/
│   └── index.html        ← Single-page UI. No build step, no framework.
│                           Left sidebar: project list + schema + JIT cache.
│                           Main area: CSV upload, query input, live execution
│                           log (streams Claude Code's tool calls in real time),
│                           result card with JIT distribution bar chart.
│
├── data/                 ← One SQLite file per project. This directory is
│   ├── yc_companies.db     mounted as an EBS volume in AWS so data persists
│   └── ...                 across container restarts. Never committed to git.
│
├── sample_data/
│   └── yc_companies.csv  ← 68 YC companies (name, description). Used for
│                           demo and manual testing. Not used by the app itself.
│
├── Dockerfile            ← Builds the production image.
│                           Base: node:20-slim (Node needed for Claude Code CLI).
│                           Adds: Python 3, sqlite3 CLI, pip dependencies.
│                           Installs: @anthropic-ai/claude-code via npm.
│                           Runs: uvicorn main:app on port 8000.
│
├── docker-compose.yml    ← Local development and production compose file.
│                           Mounts ./data as a volume (swap for EBS in AWS).
│                           Injects ANTHROPIC_API_KEY and GEMINI_API_KEY.
│
├── deploy.sh             ← One-click AWS deploy script.
│                           Provisions: EC2 (t3.medium) + EBS (20 GB gp3)
│                           + security group (ports 22, 8000).
│                           Bootstraps: Docker, docker-compose, the app.
│                           EBS volume is set to persist on instance termination.
│
├── .env                  ← Local secrets. Never committed.
│                           ANTHROPIC_API_KEY, GEMINI_API_KEY, GHOST_DATA_DIR
│
├── .env.example          ← Template for .env. Committed to git.
│
└── requirements.txt      ← Python dependencies:
                            fastapi, uvicorn, python-multipart,
                            python-dotenv, google-genai
```

---

## Component Roles

| Component | Technology | Role |
|---|---|---|
| Architect | Claude Code CLI (`claude -p`) | Query planning, script authoring, self-correction |
| Workers | Gemini 2.5 Flash | Row-level inference and classification (called from scripts the Architect writes) |
| Persistent store | SQLite + EBS | Raw data tables + materialized JIT columns |
| API server | FastAPI + uvicorn | Project management, CSV ingestion, SSE streaming |
| Sandbox | Temp directory + `--dangerously-skip-permissions` | Isolated execution environment for Architect. The Docker container is the outer security boundary. |
| UI | Vanilla HTML/JS | No build step. Streams SSE events for live progress. |

---

## API Reference

```
GET  /                                          Serve index.html

GET  /api/health                                Liveness check

GET  /api/projects                              List all projects
POST /api/projects          {name}              Create a new project (new .db file)
DELETE /api/projects/{project}                  Delete project and its DB

POST /api/projects/{project}/ingest             Upload CSV → create/replace table
     form: file (CSV), table_name (optional)

GET  /api/projects/{project}/schema             Tables, columns, row counts, JIT columns

DELETE /api/projects/{project}/jit/{table}/{col} Evict a JIT column from cache

GET  /api/projects/{project}/query/stream?q=…  SSE stream:
                                                  architect_start → architect_thinking
                                                  → architect_tool → tool_output
                                                  → aggregating → done | error
```

---

## API Keys

The system needs exactly two credentials:

| Credential | Used by | Where to get it |
|---|---|---|
| Anthropic auth | Claude Code CLI (`claude -p`) — the Architect subprocess | console.anthropic.com |
| `GEMINI_API_KEY` | Gemini Flash — the inference scripts Claude Code writes | aistudio.google.com |

**There is no separate "Claude Code key."** Claude Code is a CLI tool built on
the Anthropic API. Authentication works in one of two ways depending on context:

### Local development — OAuth session (no key needed)

When you log in interactively with `claude` (via browser OAuth), Claude Code
stores session tokens in `~/.claude/session-env/`. Any subprocess spawned by
the same user automatically shares this directory and is therefore authenticated.

This means: **if you are already running Claude Code, the architect subprocess
just works.** You only need to supply `GEMINI_API_KEY` in your `.env`.

```
# .env for local dev
GEMINI_API_KEY=AIza...
```

### Docker / AWS — API key required

Containers have no `~/.claude/` directory, so OAuth sessions are unavailable.
In this case Claude Code falls back to `ANTHROPIC_API_KEY` as an environment
variable. Both keys must be injected at runtime:

```
# injected at runtime — never baked into the image
ANTHROPIC_API_KEY=sk-ant-...
GEMINI_API_KEY=AIza...
```

The Dockerfile uses `--bare` mode for the Claude Code warm-up step, which
forces API-key-only auth and skips all keychain/OAuth reads.

---

## Deployment (AWS)

```
EC2 (t3.medium)
  └── Docker container
        ├── Claude Code CLI  (ANTHROPIC_API_KEY from env)
        ├── FastAPI / uvicorn (port 8000)
        └── /app/data → EBS volume (20 GB gp3, persists on termination)

Security group: port 8000 open, port 22 for SSH
```

**One-click deploy:**
```bash
export ANTHROPIC_API_KEY=sk-ant-...
export GEMINI_API_KEY=AIza...
export KEY_NAME=my-ec2-keypair   # optional, for SSH access
./deploy.sh
```

**Local dev:**
```bash
cp .env.example .env   # fill in keys
python3 -m uvicorn main:app --port 8000 --reload
# or
docker-compose up --build
```

---

## Key Design Decisions

**Claude Code as Architect, not a plain API call.**
Claude Code is a full agentic loop: it reads files, runs code, sees errors, and
fixes them. A plain API call would require us to build all of that infrastructure
ourselves. By using `claude -p` as a subprocess, we get self-correction and
adaptive code generation for free.

**One SQLite file per project.**
Projects are isolated at the filesystem level. There is no shared mutable state
between projects. Backup, deletion, and migration are a single file operation.

**Workers are Gemini Flash, not Claude.**
Row-level inference (classify this row: yes/no?) is a high-throughput,
low-complexity task. Gemini Flash is faster and cheaper for this. Claude Code
writes the scripts that call Gemini; it does not perform the classification itself.

**The Docker container is the sandbox.**
For local dev, Claude Code runs in a temp directory. In production, the container
is the security boundary — Claude Code runs with `--dangerously-skip-permissions`
because the container itself restricts what it can reach.

**No ORM, no migrations.**
Schema is defined by the data, not by the developer. Tables are created from CSV
headers at ingest time. JIT columns are added dynamically by Claude Code.
This is the whole point.
