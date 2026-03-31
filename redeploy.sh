#!/usr/bin/env bash
# redeploy.sh — rebuild and restart the full nanobot stack (nanobot + postgres)
#               using docker compose. Suitable for local Mac development.
#
# Usage:
#   ./redeploy.sh           # fast rebuild (uses Docker layer cache — seconds)
#   ./redeploy.sh --full    # full clean rebuild (re-downloads everything — minutes)
#                             Use --full only when Dockerfile, pyproject.toml,
#                             or package.json dependencies change.

set -euo pipefail

COMPOSE_FILE="$(cd "$(dirname "$0")" && pwd)/docker-compose.yml"
PROJECT="nanobot"
FULL_REBUILD=false

# Parse flags
for arg in "$@"; do
    case "$arg" in
        --full) FULL_REBUILD=true ;;
        *) echo "Unknown flag: $arg (supported: --full)" >&2; exit 1 ;;
    esac
done

# ── helpers ──────────────────────────────────────────────────────────────────
sep() { echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"; }
step() { sep; echo "  $1"; sep; }

# ── sanity checks ─────────────────────────────────────────────────────────────
if ! command -v docker &>/dev/null; then
    echo "ERROR: docker not found. Install Docker Desktop for Mac." >&2
    exit 1
fi

# Prefer 'docker compose' (v2 plugin) over 'docker-compose' (v1 standalone)
if docker compose version &>/dev/null 2>&1; then
    DC="docker compose"
elif command -v docker-compose &>/dev/null; then
    DC="docker-compose"
else
    echo "ERROR: docker compose not found. Install Docker Desktop for Mac." >&2
    exit 1
fi

# ── [0] ensure local 'nanobot' database exists in hp_mcp_postgres ─────────────
step "[0/4] Ensure local 'nanobot' database exists"
if docker ps --format '{{.Names}}' | grep -q '^hp_mcp_postgres$'; then
    docker exec hp_mcp_postgres psql -U hp_mcp -tc \
        "SELECT 1 FROM pg_database WHERE datname='nanobot'" \
        | grep -q 1 \
    || docker exec hp_mcp_postgres psql -U hp_mcp \
        -c "CREATE DATABASE nanobot OWNER hp_mcp"
    echo "  ✓ Database 'nanobot' ready on local postgres."
else
    echo "  ⚠  hp_mcp_postgres not running — start it first:" >&2
    echo "       cd ../hp-mcp-server && docker compose up -d postgres" >&2
    exit 1
fi

# ── [1] stop existing stack ───────────────────────────────────────────────────
step "[1/4] Stop existing stack"
$DC -f "$COMPOSE_FILE" -p "$PROJECT" down --remove-orphans || true
echo "  ✓ Stack stopped."

# ── [2] (optional) prune — only on --full ────────────────────────────────────
if $FULL_REBUILD; then
    step "[2/4] Full rebuild: pruning images and build cache"
    docker image prune -f
    docker builder prune -f --filter type=exec.cachemount || docker builder prune -f || true
    echo "  ✓ Prune complete."
    BUILD_FLAGS="--no-cache"
    echo "  Mode: FULL clean rebuild (this will take several minutes)"
else
    step "[2/4] Fast rebuild: using Docker layer cache"
    BUILD_FLAGS=""
    echo "  Mode: CACHED rebuild (only changed layers rebuilt — usually < 30s)"
    echo "  Tip:  run with --full if you changed Dockerfile or dependencies."
fi

# ── [3] build nanobot image ───────────────────────────────────────────────────
step "[3/4] Build nanobot image"
# shellcheck disable=SC2086
$DC -f "$COMPOSE_FILE" -p "$PROJECT" build $BUILD_FLAGS nanobot-gateway
echo "  ✓ Build complete."

# ── [4] start the full stack ──────────────────────────────────────────────────
step "[4/4] Start postgres + nanobot-gateway"
$DC -f "$COMPOSE_FILE" -p "$PROJECT" up -d nanobot-gateway
echo "  ✓ Stack started."

echo ""
echo "  Services:"
echo "    WebUI    →  http://localhost:8080"
echo "    Gateway  →  http://localhost:18790"
echo "    Postgres →  localhost:5433  (shared hp_mcp_postgres — db: nanobot)"
echo ""
echo "  Useful commands:"
echo "    Logs (all):      $DC -f $COMPOSE_FILE -p $PROJECT logs -f"
echo "    Logs (nanobot):  $DC -f $COMPOSE_FILE -p $PROJECT logs -f nanobot-gateway"
echo "    Stop:            $DC -f $COMPOSE_FILE -p $PROJECT down"
echo ""

# ── tail nanobot-gateway logs ─────────────────────────────────────────────────
sep
echo "  Tailing nanobot-gateway logs (Ctrl+C to stop)"
sep
$DC -f "$COMPOSE_FILE" -p "$PROJECT" logs -f nanobot-gateway
