#!/bin/bash
# One-time EC2 setup for hp-ai-agent (nanobot on AWS).
# Run on a fresh Ubuntu 22.04 instance.
#
# From your machine:
#   scp deployment/setup-ec2.sh ubuntu@<EC2_IP>:/tmp/
#   ssh ubuntu@<EC2_IP> 'sudo bash /tmp/setup-ec2.sh'

set -e

echo "Setting up EC2 for hp-ai-agent..."

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

print_status() { echo -e "${GREEN}[INFO]${NC} $1"; }
print_warning() { echo -e "${YELLOW}[WARNING]${NC} $1"; }

# Update system
print_status "Updating system packages..."
apt-get update && apt-get upgrade -y

# Install Docker
print_status "Installing Docker..."
curl -fsSL https://get.docker.com -o get-docker.sh
sh get-docker.sh
rm get-docker.sh

# Add ubuntu user to docker group (adjust if your SSH user is different)
if getent passwd ubuntu >/dev/null 2>&1; then
    usermod -aG docker ubuntu
    print_status "Added user ubuntu to docker group."
fi

# Create application directory
print_status "Creating application directory..."
mkdir -p /app
if getent passwd ubuntu >/dev/null 2>&1; then
    chown ubuntu:ubuntu /app
fi

# Firewall: SSH, gateway (18790), WebUI (8080), HTTP/HTTPS for nginx
print_status "Configuring firewall..."
ufw --force enable
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow 18790/tcp
ufw allow 8080/tcp
ufw status

# Nginx + Certbot (for https://agent.highperformr.ai)
print_status "Installing nginx and certbot..."
apt-get install -y nginx certbot python3-certbot-nginx

print_status "Configuring nginx for agent.highperformr.ai..."
cat > /etc/nginx/sites-available/agent.highperformr.ai << 'NGINX_EOF'
server {
    listen 80;
    server_name agent.highperformr.ai;
    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
NGINX_EOF
ln -sf /etc/nginx/sites-available/agent.highperformr.ai /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl enable nginx && systemctl reload nginx
print_status "Nginx configured; HTTPS cert: run 'sudo certbot --nginx -d agent.highperformr.ai' after DNS points here."

# Optional: install aws-cli on host for debugging (container uses instance role)
if ! command -v aws >/dev/null 2>&1; then
    print_status "Installing AWS CLI for host debugging..."
    apt-get install -y awscli 2>/dev/null || true
fi

print_status "EC2 setup completed. Attach an IAM role with Secrets Manager access, then run ./deployment/deploy.sh from your machine."
print_status "After DNS (agent.highperformr.ai) points to this server, run: sudo certbot --nginx -d agent.highperformr.ai"
