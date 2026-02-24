# AWS EC2 Deployment Guide for hp-ai-agent (nanobot)

Deploy nanobot to AWS EC2 as **hp-ai-agent**: one-time EC2 setup, then a single deploy command. Config is stored in AWS Secrets Manager and written into the container at startup (same shape as your local `config.json`).

## Prerequisites

- AWS account
- EC2 instance: **Ubuntu 22.04 LTS**
- SSH key pair (e.g. `~/.ssh/hp-ai-agent.pem`)
- Docker installed locally (to build the image)

## Step 1: Create EC2 instance

1. In AWS EC2 Console, launch an instance.
2. Choose **Ubuntu Server 22.04 LTS**.
3. Pick an instance type (e.g. **t3.small**).
4. Create or select a key pair and download the `.pem` file (e.g. `hp-ai-agent.pem`).
5. **Security group:** allow:
   - **SSH (22)** – your IP (recommended) or 0.0.0.0/0
   - **TCP 18790** – gateway (0.0.0.0/0 or your clients)
   - **TCP 8080** – WebUI (0.0.0.0/0 or your frontend origin)
6. **Attach IAM role:** EC2 → instance → Actions → Security → Modify IAM role. Create/attach a role with a policy that allows:
   - `secretsmanager:GetSecretValue` for the secret `hp-ai-agent-secrets` (or use `SecretsManagerReadWrite` for simplicity).

Note the instance **public IP** (or hostname); you will use it as `EC2_HOST`.

## Step 2: One-time EC2 setup

From your **local machine** (repository root):

```bash
# Copy setup script to EC2 (replace <EC2_IP> with your instance IP)
scp -i ~/.ssh/hp-ai-agent.pem deployment/setup-ec2.sh ubuntu@<EC2_IP>:/tmp/

# Run setup on EC2
ssh -i ~/.ssh/hp-ai-agent.pem ubuntu@<EC2_IP> 'sudo bash /tmp/setup-ec2.sh'
```

This installs Docker, creates `/app`, and configures the firewall for ports 22, 18790, and 8080.

## Step 3: Create AWS Secrets Manager secret

1. Open [AWS Secrets Manager](https://console.aws.amazon.com/secretsmanager/).
2. Create a secret:
   - Type: **Other type of secret**
   - Key/value: add one key, e.g. `config`, and set the value to the **full nanobot config JSON** (see shape below).
   - Or store the JSON as **Plaintext** (single key with the whole JSON).
3. Name the secret: **hp-ai-agent-secrets** (or set `NANOBOT_SECRET_ID` in the container to match your name).
4. Same region as the EC2 instance (e.g. `us-east-1`).

### Config JSON shape (same as local `~/.nanobot/config.json`)

The secret value should be the full nanobot config. Example:

```json
{
  "providers": {
    "openai": {
      "apiKey": "sk-your-key"
    }
  },
  "agents": {
    "defaults": {
      "model": "gpt-4o-mini"
    }
  },
  "tools": {
    "mcpServers": {
      "hp-discovery": {
        "url": "https://mcp.highperformr.ai/mcp",
        "toolTimeout": 600
      }
    },
    "restrictToWorkspace": true
  },
  "channels": {
    "webui": {
      "enabled": true,
      "host": "0.0.0.0",
      "port": 8080,
      "corsOrigins": ["https://your-production-frontend.com"],
      "apiKey": "optional-api-key-for-production"
    }
  }
}
```

- **corsOrigins:** set to your real frontend origin(s) (e.g. `https://app.highperformr.ai`). For local testing you can temporarily add `http://localhost:5173`.
- **apiKey:** optional; set a value to protect the WebUI in production.
- **restrictToWorkspace:** set to `true` for production (recommended in nanobot README).

**Secret format:** Either (1) **Plaintext** – paste the full JSON as the secret value – or (2) **Key/value** – one key, e.g. `config`, with the JSON above as the value. The container’s `start.sh` supports both: raw JSON or a key named `config` containing the JSON.

## Step 4: Deploy

Edit **`deployment/deploy.sh`** and set the variables at the top for your environment:

- **EC2_HOST** – your EC2 instance IP or hostname (e.g. `54.123.45.67`)
- **SSH_KEY** – path to your `.pem` key (default: `$HOME/.ssh/hp-ai-agent.pem`)
- **NANOBOT_HOST_PATH** – path on EC2 for nanobot data (default: `/app/nanobot`)
- **EC2_USER** – SSH user (default: `ubuntu`)

Then from your **local machine** (repository root):

```bash
./deployment/deploy.sh production
```

This will:

1. Build the Docker image (nanobot + bridge + start script).
2. Save it as a tar and copy it to EC2.
3. If `NANOBOT_HOST_PATH` is set: upload repo `AGENTS.md` and `skills/` to that path’s `workspace/` on EC2.
4. On EC2: load the image, stop/remove the old container (if any), run a new container with name `hp-ai-agent-app`, mapping ports 18790 and 8080.

### Single mount for all nanobot data (like local `-v ~/.nanobot:/root/.nanobot`)

All nanobot data lives under **one path** on the EC2 host, mounted as `/root/.nanobot` in the container (same as when you run locally: `docker run -v ~/.nanobot:/root/.nanobot ...`).

- **Config:** **start.sh** fetches the secret from AWS Secrets Manager and writes `config.json` into this mount at startup. You do not need to put a config file on the host.
- **Workspace (AGENTS.md, skills):** When you set `NANOBOT_HOST_PATH`, the deploy script **uploads** `AGENTS.md` and `skills/` from your **repository root** to the EC2 workspace. So keep `AGENTS.md` and a `skills/` folder (with one subfolder per skill, each containing `SKILL.md`) in the repo and they are deployed automatically.

Layout on EC2 after deploy (when using `NANOBOT_HOST_PATH=/app/nanobot`):

```
/app/nanobot/                    # NANOBOT_HOST_PATH → /root/.nanobot in container
├── config.json                  # written by start.sh from Secrets Manager at startup
├── workspace/
│   ├── AGENTS.md                # uploaded from repo root AGENTS.md
│   ├── skills/                  # uploaded from repo root skills/
│   │   ├── highperformr/SKILL.md
│   │   └── ...
│   └── memory/                  # optional; nanobot can create
```

With **EC2_HOST** and **NANOBOT_HOST_PATH** set in `deploy.sh`, run:

```bash
./deployment/deploy.sh production
```

The script will create the nanobot path and `workspace` on EC2, upload repo `AGENTS.md` and `skills/` there, then run the container with that single mount. You do not need to manually copy AGENTS.md or skills to EC2.

## Step 5: Verify and access

- **Gateway:** `http://<EC2_IP>:18790`
- **WebUI:** `http://<EC2_IP>:8080` (if `channels.webui.enabled` is true in config)

Check logs:

```bash
ssh -i ~/.ssh/hp-ai-agent.pem ubuntu@<EC2_IP> 'docker logs -f hp-ai-agent-app'
```

## Updating the application

Redeploy with the same command:

```bash
./deployment/deploy.sh production
```

## Changing config (secrets only)

1. Update the secret **hp-ai-agent-secrets** in AWS Secrets Manager (edit the JSON).
2. Restart the container so it fetches the new config:

```bash
ssh -i ~/.ssh/hp-ai-agent.pem ubuntu@<EC2_IP> 'docker restart hp-ai-agent-app'
```

## Troubleshooting

| Issue | What to check |
|-------|----------------|
| Container exits immediately | `docker logs hp-ai-agent-app` – often AWS credentials (IAM role) or invalid JSON in secret. |
| Cannot fetch secret | EC2 instance role has `secretsmanager:GetSecretValue` for `hp-ai-agent-secrets`; secret name and region match. |
| WebUI / gateway not reachable | Security group allows 8080 and 18790; firewall on EC2 was configured by `setup-ec2.sh` (ufw). |
| CORS errors in browser | `channels.webui.corsOrigins` in the secret includes your frontend origin. |

### Useful commands on EC2

```bash
docker ps
docker logs hp-ai-agent-app
docker exec -it hp-ai-agent-app cat /root/.nanobot/config.json   # verify written config
```

---

After following these steps, the app is available at **http://&lt;EC2_IP&gt;:18790** (gateway) and **http://&lt;EC2_IP&gt;:8080** (WebUI).
