#!/bin/bash
# hp-ai-agent deployment: build image, push to EC2, run container.
# Usage: ./deployment/deploy.sh [production|staging]
# Run from repository root.

set -e

ENVIRONMENT=${1:-production}
APP_NAME="hp-ai-agent"
CONTAINER_NAME="hp-ai-agent-app"
EC2_PATH="/app"
TAR_FILE="${APP_NAME}-${ENVIRONMENT}.tar"

# --- Edit these for your environment ---
EC2_HOST="3.235.62.105"
EC2_USER="ubuntu"
SSH_KEY="$HOME/.ssh/hp-ai-agent.pem"
NANOBOT_HOST_PATH="/app/nanobot"
# ---------------------------------------

# SSH options reused for all ssh/scp calls.
# ServerAliveInterval keeps the connection alive during large layer transfers.
SSH_OPTS="-o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30 -o ServerAliveCountMax=20 -o TCPKeepAlive=yes"

if [ -z "$EC2_HOST" ] || [ "$EC2_HOST" = "YOUR_EC2_IP_OR_HOSTNAME" ]; then
    echo "Error: Edit EC2_HOST in deployment/deploy.sh and set it to your EC2 instance IP or hostname."
    exit 1
fi

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

print_status() { echo -e "${GREEN}[INFO]${NC} $1"; }
print_warning() { echo -e "${YELLOW}[WARNING]${NC} $1"; }
print_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# Ensure we're run from repo root (deployment/Dockerfile and deployment/start.sh exist)
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

if [ ! -f "deployment/Dockerfile" ]; then
    print_error "deployment/Dockerfile not found. Run from repository root."
    exit 1
fi

if [ ! -f "deployment/start.sh" ]; then
    print_error "deployment/start.sh not found."
    exit 1
fi

if [ ! -f "$SSH_KEY" ]; then
    print_error "SSH key not found: $SSH_KEY. Set SSH_KEY or create the key."
    exit 1
fi

print_status "Starting deployment to $ENVIRONMENT..."

# ── Load .env from repo root (same as MCP server deploy.sh) ──────────────────
if [ -f "$REPO_ROOT/.env" ]; then
    print_status "Loading environment from .env..."
    set -a
    # shellcheck source=/dev/null
    . "$REPO_ROOT/.env"
    set +a
fi

# Construct DATABASE_URL from RDS vars if hostname is provided
if [ -n "${RDS_HOSTNAME:-}" ]; then
    print_status "Constructing RDS connection string from RDS_* variables..."
    RDS_URL="postgresql://${RDS_USERNAME}:${RDS_PASSWORD}@${RDS_HOSTNAME}:${RDS_PORT:-5432}"
fi


# ── Docker build ──────────────────────────────────────────────────────────────
# Build image using deployment Dockerfile (includes start.sh, aws-cli, jq)
print_status "Building Docker image..."
docker build -f deployment/Dockerfile -t "${APP_NAME}:${ENVIRONMENT}" .

# Create app directory on EC2 and prune stopped containers to free space
print_status "Preparing EC2 (directories + cleanup)..."
ssh -i "$SSH_KEY" $SSH_OPTS "$EC2_USER@$EC2_HOST" "
    sudo mkdir -p $EC2_PATH ${NANOBOT_HOST_PATH:+$NANOBOT_HOST_PATH/workspace/skills $NANOBOT_HOST_PATH/workspace/sessions}
    sudo chown -R $EC2_USER:$EC2_USER $EC2_PATH ${NANOBOT_HOST_PATH:-}
    echo 'Pruning stopped containers and dangling images to free space...'
    docker container prune -f
    docker image prune -f
    df -h / | tail -1
"

# Save image to tar and copy to EC2.
# scp shows a native progress bar (filename  XX%  speed  ETA) — no extra tools needed.
print_status "Saving Docker image to tar..."
docker save "${APP_NAME}:${ENVIRONMENT}" -o "$TAR_FILE"
TAR_SIZE=$(du -sh "$TAR_FILE" | cut -f1)
print_status "Copying image to EC2 (${TAR_SIZE}) — scp progress below:"
scp -i "$SSH_KEY" $SSH_OPTS "$TAR_FILE" "$EC2_USER@$EC2_HOST:/tmp/"
print_status "Image uploaded successfully."

# Upload workspace (AGENTS.md, skills/) from repo to EC2
if [ -n "$NANOBOT_HOST_PATH" ]; then
    if [ -f "AGENTS.md" ]; then
        print_status "Uploading AGENTS.md to EC2 workspace..."
        scp -i "$SSH_KEY" $SSH_OPTS \
            "AGENTS.md" "$EC2_USER@$EC2_HOST:${NANOBOT_HOST_PATH}/workspace/"
    fi
    if [ -d "skills" ] && [ -n "$(ls -A skills 2>/dev/null)" ]; then
        print_status "Uploading skills/ to EC2 workspace..."
        scp -i "$SSH_KEY" $SSH_OPTS \
            -r skills "$EC2_USER@$EC2_HOST:${NANOBOT_HOST_PATH}/workspace/"
    fi
fi

# Stop old container, run new one
print_status "Deploying on EC2..."
ssh -i "$SSH_KEY" $SSH_OPTS "$EC2_USER@$EC2_HOST" << EOF
    set -e
    echo "Loading Docker image from tar..."
    docker load -i /tmp/$TAR_FILE
    rm -f /tmp/$TAR_FILE

    echo "Setting up RDS database from EC2 (VPC-internal access)..."
    docker run --rm \
        -e ADMIN_URL="postgresql://${RDS_USERNAME}:${RDS_PASSWORD}@${RDS_HOSTNAME}:${RDS_PORT:-5432}/postgres?sslmode=require" \
        -e DB_NAME="${RDS_DB_NAME:-nanobot}" \
        ${APP_NAME}:${ENVIRONMENT} \
        python3 -c "
import asyncio, asyncpg, os, ssl

async def main():
    ssl_ctx = ssl.create_default_context()
    admin_url = os.environ['ADMIN_URL']
    db_name = os.environ['DB_NAME']

    conn = await asyncpg.connect(admin_url, ssl=ssl_ctx)
    exists = await conn.fetchval('SELECT 1 FROM pg_database WHERE datname = \$1', db_name)
    if not exists:
        await conn.execute('CREATE DATABASE ' + db_name)
        print('Created database: ' + db_name)
    else:
        print('Database already exists: ' + db_name)
    await conn.close()

    target_url = admin_url.rsplit('/', 1)[0] + '/' + db_name
    conn = await asyncpg.connect(target_url, ssl=ssl_ctx)
    try:
        await conn.execute('CREATE EXTENSION IF NOT EXISTS vector')
        print('pgvector: enabled')
    except Exception as e:
        print('pgvector: ' + str(e) + ' (non-fatal)')
    await conn.close()

asyncio.run(main())
" && echo "RDS setup complete." || echo "Warning: RDS setup step failed — DB may already exist, continuing."

    echo "Stopping existing container (if any)..."
    docker stop $CONTAINER_NAME 2>/dev/null || true
    docker rm $CONTAINER_NAME 2>/dev/null || true
    echo "Starting new container..."
    if [ -n "${NANOBOT_HOST_PATH}" ]; then
        NANOBOT_VOL="-v ${NANOBOT_HOST_PATH}:/root/.nanobot"
    else
        NANOBOT_VOL="-v /root/.nanobot:/root/.nanobot"
    fi
    docker run -d \
        --name $CONTAINER_NAME \
        --restart unless-stopped \
        --add-host host.docker.internal:host-gateway \
        -p 18790:18790 \
        -p 8080:8080 \
        -e AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-us-east-1}" \
        -e HP_SECRET_ID="${HP_SECRET_ID:-hp-ai-agent-secrets}" \
        -e NANOBOT_DATABASE__URL="${RDS_URL}/${RDS_DB_NAME:-nanobot}?sslmode=require" \
        \$NANOBOT_VOL \
        ${APP_NAME}:${ENVIRONMENT}
    echo "Deployment completed on EC2."
EOF

rm -f "$TAR_FILE"
print_status "Deployment completed successfully."
print_status "Gateway: $EC2_HOST:18790  WebUI: $EC2_HOST:8080"
print_status "Logs: ssh -i $SSH_KEY $EC2_USER@$EC2_HOST 'docker logs -f $CONTAINER_NAME'"
