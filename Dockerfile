# Ghost Schema — Agentic Database
# Installs: Claude Code CLI (Node), Python 3, and app dependencies.
#
# Auth model in production:
#   Claude Code reads ANTHROPIC_API_KEY from the environment.
#   OAuth/keychain are never used (--bare flag enforces this).
#   ANTHROPIC_API_KEY and GEMINI_API_KEY must be injected at runtime.
#   Neither key is baked into this image.

FROM node:20-slim

# Python + sqlite3 CLI (Claude Code uses sqlite3 to inspect schemas)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        python3 python3-pip sqlite3 curl && \
    rm -rf /var/lib/apt/lists/*

# Claude Code CLI — the Architect
RUN npm install -g @anthropic-ai/claude-code@latest

WORKDIR /app

# Python dependencies
COPY requirements.txt .
RUN python3 -m pip install --no-cache-dir --break-system-packages -r requirements.txt

# Warm Claude Code: write any first-run config files at build time.
# --bare: forces API-key-only auth, skips OAuth/keychain entirely.
# Uses a placeholder key — the warmup only initialises config files, not auth.
RUN ANTHROPIC_API_KEY=build-time-placeholder \
    claude -p "ok" --bare --dangerously-skip-permissions 2>/dev/null || true

COPY . .

# Data directory — mount an EBS volume here in production
RUN mkdir -p /app/data
VOLUME ["/app/data"]

EXPOSE 8000

# Claude Code uses ANTHROPIC_API_KEY (never OAuth in container).
# Inference scripts use GEMINI_API_KEY.
# Both must be set in docker-compose.yml or EC2 user-data.
ENV GHOST_DATA_DIR=/app/data

CMD ["python3", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
