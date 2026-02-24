# Stage 1: Build nanobot + bridge (same as root Dockerfile, context = repo root)
# Target linux/amd64 so the image runs on typical EC2 (x86_64); avoids exec format error on Mac-built images
FROM --platform=linux/amd64 ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS nanobot-build

RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates gnupg git && \
    mkdir -p /etc/apt/keyrings && \
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg && \
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" > /etc/apt/sources.list.d/nodesource.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends nodejs && \
    apt-get purge -y gnupg && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
RUN mkdir -p nanobot bridge && touch nanobot/__init__.py && \
    uv pip install --system --no-cache . && \
    rm -rf nanobot bridge

COPY nanobot/ nanobot/
COPY bridge/ bridge/
RUN uv pip install --system --no-cache .

WORKDIR /app/bridge
RUN npm install && npm run build
WORKDIR /app

RUN mkdir -p /root/.nanobot

# Stage 2: Add AWS CLI v2, jq, and start script for deployment
FROM --platform=linux/amd64 nanobot-build AS production

RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates jq unzip && \
    curl -sS "https://awscli.amazonaws.com/awscli-exe-linux-$(uname -m).zip" -o /tmp/awscliv2.zip && \
    unzip -q /tmp/awscliv2.zip -d /tmp && \
    /tmp/aws/install -i /usr/local/aws-cli -b /usr/local/bin && \
    rm -rf /tmp/awscliv2.zip /tmp/aws /var/lib/apt/lists/*

COPY deployment/start.sh /app/start.sh
RUN chmod +x /app/start.sh

EXPOSE 18790 8080

ENTRYPOINT ["/app/start.sh"]
