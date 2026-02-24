#!/bin/sh
# Container entrypoint: fetch config from AWS Secrets Manager, write config.json, then start nanobot gateway.

set -e

CONFIG_PATH="${NANOBOT_CONFIG_PATH:-/root/.nanobot/config.json}"
SECRET_ID="${NANOBOT_SECRET_ID:-hp-ai-agent-secrets}"
REGION="${AWS_DEFAULT_REGION:-us-east-1}"

echo "Fetching config from AWS Secrets Manager (${SECRET_ID})..."
SECRET_JSON=$(aws secretsmanager get-secret-value \
    --secret-id "$SECRET_ID" \
    --region "$REGION" \
    --query SecretString \
    --output text 2>/dev/null) || true

if [ -n "$SECRET_JSON" ] && [ "$SECRET_JSON" != "None" ]; then
    echo "Writing config to ${CONFIG_PATH}"
    mkdir -p "$(dirname "$CONFIG_PATH")"
    # Support both: raw JSON string, or key/value secret with e.g. "config" key holding the JSON
    if echo "$SECRET_JSON" | jq -e .config >/dev/null 2>&1; then
        jq -r '.config' <<EOF > "$CONFIG_PATH"
$SECRET_JSON
EOF
    else
        echo "$SECRET_JSON" > "$CONFIG_PATH"
    fi
else
    echo "Warning: Could not fetch secret. Using existing config if present."
fi

# Optional: start WhatsApp bridge in background if config enables it and bridge exists
if [ -f /app/bridge/dist/index.js ] && command -v node jq >/dev/null 2>&1; then
    if [ -f "$CONFIG_PATH" ]; then
        WA_ENABLED=$(jq -r '.channels.whatsapp.enabled // false' "$CONFIG_PATH" 2>/dev/null)
        if [ "$WA_ENABLED" = "true" ]; then
            echo "Starting WhatsApp bridge in background..."
            (cd /app/bridge && node dist/index.js &) || true
        fi
    fi
fi

echo "Starting nanobot gateway..."
exec nanobot gateway
