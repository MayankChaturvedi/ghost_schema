# Ghost Schema — Agentic Database
# Installs: Claude Code CLI (Node), Python 3, and app dependencies.
#
# Auth model in production:
#   Claude Code reads ANTHROPIC_API_KEY from the environment.
#   OAuth/keychain are never used (--bare flag enforces this).
#   ANTHROPIC_API_KEY and GEMINI_API_KEY must be injected at runtime.
#   Neither key is baked into this image.
#
# Non-root user 'ghost' (uid 1001):
#   Claude Code refuses --dangerously-skip-permissions when running as root.
#   All runtime processes run as ghost. The EBS mount at /app/data must be
#   chowned to uid 1001 on the host (deploy.sh handles this).

FROM node:20-slim

# Python + sqlite3 CLI (Claude Code uses sqlite3 to inspect schemas)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        python3 python3-pip sqlite3 curl && \
    rm -rf /var/lib/apt/lists/*

# Claude Code CLI — the Architect
RUN npm install -g @anthropic-ai/claude-code@2.1.142

WORKDIR /app

# Python dependencies
COPY requirements.txt .
RUN python3 -m pip install --no-cache-dir --break-system-packages -r requirements.txt

# Create non-root user — claude-code refuses --dangerously-skip-permissions as root
RUN useradd -m -u 1001 -s /bin/bash ghost && chown -R ghost:ghost /app

USER ghost

# Warm Claude Code config files as the runtime user (non-root, so the flag works)
RUN ANTHROPIC_API_KEY=build-time-placeholder \
    claude -p "ok" --bare --dangerously-skip-permissions 2>/dev/null || true

COPY --chown=ghost:ghost . .

# Data directory — mount an EBS volume here in production (chown 1001 on host)
RUN mkdir -p /app/data
VOLUME ["/app/data"]

EXPOSE 8000

ENV GHOST_DATA_DIR=/app/data

CMD ["python3", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
