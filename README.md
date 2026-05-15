# Ghost Schema · On-Prem Agentic Database · Proudly the slowest and the most expensive query engine · Most accurate

**Deep research on every query.**

## The idea

```
User query
    │
    ▼
Claude Code (Architect) — one sandboxed instance per query
    Inspects schema · writes inference script · self-corrects
    │
    ▼
Gemini 2.5 Flash (Workers) — one call per row, fully parallel
    Classifies each row · writes results to jit_columns cache
    │
    ▼
Final SQL · answer streamed back to browser
```

No prompt engineering. No templates. No retry code written by hand.
Claude Code handles schema discovery, script authoring, and error recovery on its own.

---

## Stack

| Layer | Technology |
|---|---|
| Architect | Claude Code CLI (`claude -p`) in a temp-dir sandbox |
| Workers | Gemini 2.5 Flash — parallel row classification |
| Store | SQLite (one file per project) + EBS in production |
| API | FastAPI + SSE streaming |
| UI | Vanilla HTML/JS, no build step |

---

## Quickstart (local)

```bash
git clone https://github.com/MayankChaturvedi/ghost_schema
cd ghost_schema
cp .env.example .env   # add your GEMINI_API_KEY
pip install -r requirements.txt
uvicorn main:app --port 8000 --reload
```

Open **http://localhost:8000** — the UI will prompt for your API keys on first launch.

You only need a Gemini key locally. Claude Code uses your existing OAuth session automatically (run `claude` once to log in).

---

## One-click AWS deploy

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export GEMINI_API_KEY=AIza...
export KEY_NAME=my-ec2-keypair   # optional, for SSH
./deploy.sh
```

Provisions a **t3.medium** EC2 instance + **20 GB EBS** volume (persists across restarts). App is live on port 8000 in ~2 minutes.

**AWS prerequisites:**
- `aws` CLI installed and configured (`aws configure`)
- IAM permissions: EC2 (RunInstances, CreateSecurityGroup, AuthorizeSecurityGroupIngress, DescribeImages, DescribeInstances)

---

## How JIT columns work

```sql
-- Cached in jit_columns after first query
table_name  entity_id  column_name          value
companies   1          sells_to_healthcare  yes
companies   2          sells_to_healthcare  no
companies   3          sells_to_healthcare  yes
...
```

The second time you ask "how many companies sell to healthcare?" — no Gemini calls, instant SQL.

---

## API keys

| Key | Used by | Where to get |
|---|---|---|
| Anthropic | Claude Code Architect subprocess | console.anthropic.com |
| Gemini | Inference workers | aistudio.google.com/apikey |

Locally: Anthropic key is optional — Claude Code reuses your OAuth session.
Docker/AWS: both keys required, injected via environment variables.
