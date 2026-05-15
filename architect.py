"""
The Architect — a Claude Code instance running in an isolated sandbox.

Claude Code is used as the architect (not a plain API call) because it is an
expert agentic code-writer that can inspect schemas, write inference scripts,
run them, read errors, and self-correct — all without any hand-coded templates
or prompt engineering for the planning step.

The only inputs are:
  - user_query  : the natural language question
  - db_path     : absolute path to the SQLite database

Claude Code figures out everything else.
"""

import asyncio
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from typing import Callable, Awaitable, Optional


GHOST_SCHEMA_MARKER = "GHOST_SCHEMA_RESULT:"

# --------------------------------------------------------------------------- #
# Prompt — minimal, trusts Claude Code's native intelligence                  #
# --------------------------------------------------------------------------- #

PROMPT = """\
You are the Architect of Ghost Schema — an agentic database system.

Your inputs:
  Database  : {db_path}
  User query: "{query}"

Ghost Schema concept:
  Data is stored in SQLite. When a query requires reasoning about row content
  (e.g. "sells to healthcare", "is B2B", "mentions climate change") that cannot
  be answered with plain SQL, you create a "Just-In-Time column": you classify
  every row using Gemini Flash in parallel and cache results in `jit_columns`.
  Subsequent queries with the same column skip inference entirely (cache hit).

  jit_columns table (already exists in the DB):
    id INTEGER PRIMARY KEY, table_name TEXT, entity_id INTEGER,
    column_name TEXT, value TEXT, created_at TIMESTAMP
  Convention: value is 'yes' or 'no' for binary classification.
  Skip rows whose entity_id is already present for the target column_name.

  GEMINI_API_KEY is available as an environment variable.
  Use the google-genai package (already installed):
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    response = await client.aio.models.generate_content(
        model="gemini-2.5-flash", contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0.0,
            max_output_tokens=50,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        ))

  IMPORTANT — Gemini 2.5 Flash uses thinking tokens by default. Thinking tokens
  silently consume max_output_tokens before producing any output, causing empty
  or truncated responses. Always set thinking_budget=0 UNLESS the task genuinely
  requires multi-step reasoning (e.g. complex math, chained logic). Row-level
  classification (yes/no, category) never needs thinking — always disable it.

  IMPORTANT — Always wrap every Gemini call in retry logic with exponential
  backoff. Rate limits (429) and transient errors (503/502) are common when
  making many parallel requests. Use this pattern in every inference script:

    import asyncio, random
    from google.api_core.exceptions import ResourceExhausted, ServiceUnavailable

    async def call_gemini_with_retry(client, prompt, config, retries=5):
        for attempt in range(retries):
            try:
                return await client.aio.models.generate_content(
                    model="gemini-2.5-flash", contents=prompt, config=config)
            except (ResourceExhausted, ServiceUnavailable) as e:
                if attempt == retries - 1:
                    raise
                wait = (2 ** attempt) + random.uniform(0, 1)
                await asyncio.sleep(wait)
            except Exception as e:
                if attempt == retries - 1:
                    raise
                await asyncio.sleep(2 ** attempt)

  Also use an asyncio.Semaphore (limit 20) to cap concurrency and avoid
  flooding the API when classifying large tables.

Do your work, then end your response with EXACTLY this line:
{marker} {{"jit_column_name": "col_or_null", "final_sql": "SELECT ...", "answer_prefix": "There are", "explanation": "one sentence"}}
"""


# --------------------------------------------------------------------------- #
# Result                                                                      #
# --------------------------------------------------------------------------- #

@dataclass
class ArchitectResult:
    jit_column_name: Optional[str]
    final_sql: str
    answer_prefix: str
    explanation: str


# --------------------------------------------------------------------------- #
# Run                                                                         #
# --------------------------------------------------------------------------- #

async def run(
    query: str,
    db_path: str,
    progress_cb: Optional[Callable[[str, str], Awaitable[None]]] = None,
) -> ArchitectResult:
    prompt = PROMPT.format(
        db_path=db_path,
        query=query,
        marker=GHOST_SCHEMA_MARKER,
    )

    sandbox = tempfile.mkdtemp(prefix="ghost_schema_sandbox_")
    db_dir = str(os.path.dirname(os.path.abspath(db_path)))

    if progress_cb:
        await progress_cb("architect_start", "Spawning Claude Code in sandbox…")

    # Auth detection:
    #   Local dev  → ANTHROPIC_API_KEY not set, OAuth session in ~/.claude/
    #                subprocess inherits ~/.claude/ automatically, no flag needed.
    #   Docker/AWS → ANTHROPIC_API_KEY set as env var, no ~/.claude/ directory.
    #                --bare forces API-key-only auth, skipping OAuth/keychain.
    # Model is pinned explicitly so quality is identical across OAuth (local)
    # and API key (Docker/AWS). Change ARCHITECT_MODEL to upgrade globally.
    model = os.environ.get("ARCHITECT_MODEL", "claude-sonnet-4-6")

    # Auth detection:
    #   Local dev  → ANTHROPIC_API_KEY not set, OAuth session in ~/.claude/
    #                subprocess inherits ~/.claude/ automatically, no flag needed.
    #   Docker/AWS → ANTHROPIC_API_KEY set as env var, no ~/.claude/ directory.
    #                --bare forces API-key-only auth, skipping OAuth/keychain.
    in_container = "ANTHROPIC_API_KEY" in os.environ
    cmd = [
        "claude", "-p", prompt,
        "--output-format", "stream-json",
        "--verbose",
        "--model", model,
        "--allowedTools", "Bash", "Write", "Read",
        "--add-dir", db_dir,
        "--dangerously-skip-permissions",
        *(["--bare"] if in_container else []),
    ]

    env = {**os.environ, "GHOST_DB_PATH": db_path}

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=sandbox,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        result_text: Optional[str] = None

        async def drain_stderr():
            while True:
                chunk = await proc.stderr.read(4096)
                if not chunk:
                    break

        async def read_stdout():
            nonlocal result_text
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                raw = line.decode("utf-8", errors="replace").strip()
                if not raw:
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    if GHOST_SCHEMA_MARKER in raw and progress_cb:
                        result_text = raw
                    continue

                etype = event.get("type")

                if etype == "assistant":
                    for block in event.get("message", {}).get("content", []):
                        btype = block.get("type")
                        if btype == "text":
                            text = (block.get("text") or "").strip()
                            if text and progress_cb:
                                first = text.splitlines()[0][:200]
                                if first:
                                    await progress_cb("architect_thinking", first)
                            if GHOST_SCHEMA_MARKER in (text or ""):
                                result_text = text
                        elif btype == "tool_use":
                            msg = _tool_msg(block.get("name", ""), block.get("input", {}))
                            if msg and progress_cb:
                                await progress_cb("architect_tool", msg)

                elif etype == "user":
                    for block in event.get("message", {}).get("content", []):
                        if block.get("type") == "tool_result":
                            snippet = _tool_result_snippet(block.get("content", ""))
                            if snippet and progress_cb:
                                await progress_cb("tool_output", snippet)

                elif etype == "result":
                    text = event.get("result") or event.get("content") or ""
                    if GHOST_SCHEMA_MARKER in text:
                        result_text = text

        await asyncio.gather(read_stdout(), drain_stderr())
        await asyncio.wait_for(proc.wait(), timeout=600)

        if not result_text:
            raise RuntimeError("Claude Code did not output GHOST_SCHEMA_RESULT.")

        return _parse(result_text)

    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #

def _tool_msg(name: str, inp: dict) -> str:
    if name == "Bash":
        label = (inp.get("description") or inp.get("command") or "").strip()
        return f"$ {label[:160]}" if label else ""
    elif name == "Write":
        return f"Writing {inp.get('file_path', '')}"
    elif name == "Read":
        return f"Reading {inp.get('file_path', '')}"
    return ""


def _tool_result_snippet(content) -> str:
    if isinstance(content, str):
        return content.strip()[:160]
    if isinstance(content, list):
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                return (c.get("text") or "").strip()[:160]
    return ""


def _parse(text: str) -> ArchitectResult:
    match = re.search(
        rf"{re.escape(GHOST_SCHEMA_MARKER)}\s*(\{{[\s\S]*?\}})",
        text,
    )
    if not match:
        raise ValueError(
            f"Could not parse GHOST_SCHEMA_RESULT from architect output.\n"
            f"Tail:\n{text[-800:]}"
        )
    raw = re.sub(r",\s*([}\]])", r"\1", match.group(1))  # tolerate trailing commas
    d = json.loads(raw)
    return ArchitectResult(
        jit_column_name=d.get("jit_column_name") or None,
        final_sql=d["final_sql"],
        answer_prefix=d.get("answer_prefix", "Result:"),
        explanation=d.get("explanation", ""),
    )
