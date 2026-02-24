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

# Build image using deployment Dockerfile (includes start.sh, aws-cli, jq)
print_status "Building Docker image..."
docker build -f deployment/Dockerfile -t "${APP_NAME}:${ENVIRONMENT}" .

# Save image to tar
print_status "Saving Docker image to ${TAR_FILE}..."
docker save "${APP_NAME}:${ENVIRONMENT}" -o "$TAR_FILE"

# Create app directory (and optional nanobot data dir) on EC2
print_status "Creating app directory on EC2..."
if [ -n "$NANOBOT_HOST_PATH" ]; then
    ssh -i "$SSH_KEY" -o StrictHostKeyChecking=accept-new "$EC2_USER@$EC2_HOST" "sudo mkdir -p $EC2_PATH $NANOBOT_HOST_PATH/workspace/skills && sudo chown -R $EC2_USER:$EC2_USER $EC2_PATH $NANOBOT_HOST_PATH"
else
    ssh -i "$SSH_KEY" -o StrictHostKeyChecking=accept-new "$EC2_USER@$EC2_HOST" "sudo mkdir -p $EC2_PATH && sudo chown $EC2_USER:$EC2_USER $EC2_PATH"
fi

# Copy image to EC2
print_status "Copying image to EC2..."
scp -i "$SSH_KEY" "$TAR_FILE" "$EC2_USER@$EC2_HOST:/tmp/"

# Upload workspace (AGENTS.md, skills/) from repo to EC2 when using NANOBOT_HOST_PATH
if [ -n "$NANOBOT_HOST_PATH" ]; then
    if [ -f "AGENTS.md" ]; then
        print_status "Uploading AGENTS.md to EC2 workspace..."
        scp -i "$SSH_KEY" "AGENTS.md" "$EC2_USER@$EC2_HOST:${NANOBOT_HOST_PATH}/workspace/"
    fi
    if [ -d "skills" ] && [ -n "$(ls -A skills 2>/dev/null)" ]; then
        print_status "Uploading skills/ to EC2 workspace..."
        scp -i "$SSH_KEY" -r skills "$EC2_USER@$EC2_HOST:${NANOBOT_HOST_PATH}/workspace/"
    fi
fi

# Load, stop old container, run new container
print_status "Deploying on EC2..."
ssh -i "$SSH_KEY" "$EC2_USER@$EC2_HOST" << EOF
    set -e
    echo "Loading Docker image..."
    docker load -i /tmp/$TAR_FILE
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
        -p 18790:18790 \
        -p 8080:8080 \
        -e AWS_DEFAULT_REGION=\${AWS_DEFAULT_REGION:-us-east-1} \
        $NANOBOT_VOL \
        ${APP_NAME}:${ENVIRONMENT}
    echo "Removing image tar from EC2..."
    rm -f /tmp/$TAR_FILE
    echo "Deployment completed on EC2."
EOF

# Clean up local tar
rm -f "$TAR_FILE"
print_status "Removed local $TAR_FILE"

print_status "Deployment completed successfully."
print_status "Gateway: $EC2_HOST:18790  WebUI: $EC2_HOST:8080"
print_status "Logs: ssh -i $SSH_KEY $EC2_USER@$EC2_HOST 'docker logs -f $CONTAINER_NAME'"
