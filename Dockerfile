FROM node:20-bookworm-slim AS node_source
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# ── Node.js 20 — copy from official image ──
# This avoids slow nodesource CDN and apt-get hanging issues
COPY --from=node_source /usr/local/ /usr/local/

# Install only what apt still needs (git for pip VCS installs)
RUN apt-get update && \
    apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Python deps (cached layer — only re-runs when pyproject.toml changes) ──
COPY pyproject.toml README.md LICENSE ./
RUN mkdir -p nanobot bridge && touch nanobot/__init__.py && \
    uv pip install --system --no-cache . && \
    rm -rf nanobot bridge

# ── Application source ──
COPY nanobot/ nanobot/
COPY bridge/ bridge/
RUN uv pip install --system --no-cache .

# ── WhatsApp bridge (npm install + build) ──
WORKDIR /app/bridge
RUN npm install && npm run build
WORKDIR /app

# Create config directory
RUN mkdir -p /root/.nanobot

# Gateway default port
EXPOSE 18790

ENTRYPOINT ["nanobot"]
CMD ["status"]
