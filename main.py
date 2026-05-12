"""
Ghost Schema — Agentic Database
Multi-project: each project gets its own SQLite file in DATA_DIR.
architect.py receives (query, db_path) and handles everything else.
"""

import asyncio
import csv
import io
import json
import os
import sqlite3
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import architect

# --------------------------------------------------------------------------- #
# Config                                                                      #
# --------------------------------------------------------------------------- #

DATA_DIR = Path(os.environ.get("GHOST_DATA_DIR", "data")).absolute()

app = FastAPI(title="Ghost Schema", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory="static"), name="static")


# --------------------------------------------------------------------------- #
# Project helpers                                                              #
# --------------------------------------------------------------------------- #

def _project_path(project: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in project).strip("_")
    if not safe:
        raise ValueError("Invalid project name")
    return DATA_DIR / f"{safe}.db"


def _conn(project: str) -> sqlite3.Connection:
    db = _project_path(project)
    if not db.exists():
        raise HTTPException(404, f"Project '{project}' not found")
    c = sqlite3.connect(str(db))
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def _init_project(project: str):
    """Create the project DB and the jit_columns table."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db = _project_path(project)
    c = sqlite3.connect(str(db))
    c.execute("""
        CREATE TABLE IF NOT EXISTS jit_columns (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            table_name  TEXT    NOT NULL,
            entity_id   INTEGER NOT NULL,
            column_name TEXT    NOT NULL,
            value       TEXT,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(table_name, entity_id, column_name)
        )
    """)
    c.commit(); c.close()


def _list_projects() -> list[str]:
    if not DATA_DIR.exists():
        return []
    return sorted(p.stem for p in DATA_DIR.glob("*.db"))


def _schema(project: str) -> dict:
    c = _conn(project)
    tables = c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT IN ('jit_columns','sqlite_sequence')"
    ).fetchall()

    schema: dict = {}
    for t in tables:
        name = t["name"]
        cols = c.execute(f'PRAGMA table_info("{name}")').fetchall()
        sample = c.execute(f'SELECT * FROM "{name}" LIMIT 3').fetchall()
        count = c.execute(f'SELECT COUNT(*) as n FROM "{name}"').fetchone()["n"]
        schema[name] = {
            "columns": [{"name": r["name"], "type": r["type"]} for r in cols],
            "sample": [dict(r) for r in sample],
            "row_count": count,
        }

    jit = c.execute(
        "SELECT DISTINCT table_name, column_name, COUNT(*) as n "
        "FROM jit_columns GROUP BY table_name, column_name"
    ).fetchall()
    c.close()

    return {
        "tables": schema,
        "jit_columns": [
            {"table": r["table_name"], "column": r["column_name"], "cached_rows": r["n"]}
            for r in jit
        ],
    }


def _ingest_csv(project: str, content: str, table_name: str) -> dict:
    reader = csv.DictReader(io.StringIO(content.strip()))
    rows = list(reader)
    if not rows:
        raise ValueError("CSV is empty")

    raw_keys = [k for k in rows[0].keys() if k and str(k).strip()]
    columns = [k.strip().lower().replace(" ", "_") for k in raw_keys]
    safe_table = "".join(c if c.isalnum() or c == "_" else "_" for c in table_name)

    c = _conn(project)
    c.execute(f'DROP TABLE IF EXISTS "{safe_table}"')
    col_defs = ", ".join(f'"{col}" TEXT' for col in columns)
    c.execute(f'CREATE TABLE "{safe_table}" (id INTEGER PRIMARY KEY AUTOINCREMENT, {col_defs})')

    col_names = ", ".join(f'"{col}"' for col in columns)
    placeholders = ", ".join("?" for _ in columns)
    for row in rows:
        vals = [str(row.get(k) or "").strip() for k in raw_keys]
        c.execute(f'INSERT INTO "{safe_table}" ({col_names}) VALUES ({placeholders})', vals)

    c.execute("DELETE FROM jit_columns WHERE table_name = ?", [safe_table])
    c.commit(); c.close()
    return {"project": project, "table": safe_table, "rows": len(rows), "columns": columns}


def _run_sql(project: str, query: str) -> list:
    if not query.strip().upper().startswith("SELECT"):
        raise ValueError("Only SELECT queries allowed")
    c = _conn(project)
    rows = c.execute(query).fetchall()
    c.close()
    return [dict(r) for r in rows]


def _jit_distribution(project: str, column: str) -> dict:
    c = _conn(project)
    rows = c.execute(
        "SELECT value, COUNT(*) as n FROM jit_columns "
        "WHERE column_name=? AND value NOT LIKE 'error:%' GROUP BY value",
        [column],
    ).fetchall()
    c.close()
    return {r["value"]: r["n"] for r in rows}


# --------------------------------------------------------------------------- #
# Routes                                                                      #
# --------------------------------------------------------------------------- #

@app.on_event("startup")
async def _startup():
    DATA_DIR.mkdir(parents=True, exist_ok=True)


@app.get("/")
async def root():
    return FileResponse("static/index.html")


@app.get("/api/health")
async def health():
    return {"status": "ok"}


# ── Projects ──────────────────────────────────────────────────────────────────

@app.get("/api/projects")
async def list_projects():
    return {"projects": _list_projects()}


@app.post("/api/projects")
async def create_project(body: dict):
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "Project name required")
    if _project_path(name).exists():
        raise HTTPException(409, f"Project '{name}' already exists")
    _init_project(name)
    return {"project": name, "created": True}


@app.delete("/api/projects/{project}")
async def delete_project(project: str):
    db = _project_path(project)
    if not db.exists():
        raise HTTPException(404, f"Project '{project}' not found")
    db.unlink()
    return {"project": project, "deleted": True}


# ── Ingest ────────────────────────────────────────────────────────────────────

@app.post("/api/projects/{project}/ingest")
async def ingest(
    project: str,
    file: UploadFile = File(...),
    table_name: str = Form(""),
):
    # Default table name = CSV filename without extension
    name = table_name.strip() or Path(file.filename or "data").stem
    content = await file.read()
    try:
        result = _ingest_csv(project, content.decode("utf-8", errors="replace"), name)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, str(e))
    return result


# ── Schema ────────────────────────────────────────────────────────────────────

@app.get("/api/projects/{project}/schema")
async def schema(project: str):
    return _schema(project)


@app.delete("/api/projects/{project}/jit/{table}/{column}")
async def delete_jit(project: str, table: str, column: str):
    c = _conn(project)
    c.execute("DELETE FROM jit_columns WHERE table_name=? AND column_name=?", [table, column])
    c.commit(); c.close()
    return {"deleted": True}


# ── Query ─────────────────────────────────────────────────────────────────────

@app.get("/api/projects/{project}/query/stream")
async def query_stream(project: str, q: str):
    """
    SSE. Claude Code receives (query, db_path) and figures out everything:
    schema inspection, JIT inference via Gemini Flash, self-correction, final SQL.
    """
    db_path = str(_project_path(project))
    if not Path(db_path).exists():
        raise HTTPException(404, f"Project '{project}' not found")

    async def event_gen():
        queue: asyncio.Queue = asyncio.Queue()

        async def push(step: str, message: str):
            await queue.put({"step": step, "message": message})

        async def run():
            try:
                s = _schema(project)
                if not s["tables"]:
                    await queue.put({"step": "error", "message": "No tables in this project — ingest data first."})
                    return

                result = await architect.run(q, db_path, progress_cb=push)

                await push("aggregating", "Running final SQL…")
                rows = _run_sql(project, result.final_sql)

                dist = None
                if result.jit_column_name:
                    dist = _jit_distribution(project, result.jit_column_name)

                await queue.put({
                    "step": "done",
                    "result": rows,
                    "answer_prefix": result.answer_prefix,
                    "jit_column": result.jit_column_name,
                    "jit_distribution": dist,
                    "final_sql": result.final_sql,
                    "explanation": result.explanation,
                })
            except Exception as e:
                await queue.put({"step": "error", "message": str(e)})

        asyncio.create_task(run())

        while True:
            event = await queue.get()
            yield f"data: {json.dumps(event)}\n\n"
            if event.get("step") in ("done", "error"):
                break

    return StreamingResponse(event_gen(), media_type="text/event-stream")
