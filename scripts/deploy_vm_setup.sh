#!/usr/bin/env bash
# Bootstrap an Oracle Cloud Always-Free Ubuntu 22.04 VM to host the
# Croissant Stock Analyser dashboard.
#
# Idempotent: re-running is safe — every section checks for existing
# state and skips work already done. Useful when you tweak the script
# during initial setup or want to re-apply after a security upgrade.
#
# Usage (run on the VM, NOT locally):
#   curl -fsSL https://raw.githubusercontent.com/jxupower/hk-sentiment-scraper/main/scripts/deploy_vm_setup.sh -o setup.sh
#   chmod +x setup.sh
#   sudo ./setup.sh
#
# What this does, in order:
#   1. apt-get update + install base packages (curl, ca-certificates, ufw)
#   2. Install Docker engine + compose plugin via Docker's official apt repo
#      (Oracle's Ubuntu image's bundled Docker is older and lacks compose v2)
#   3. Create the `deploy` system user that GitHub Actions SSHes in as
#   4. Create /srv/dashboard with the production docker-compose.yml
#   5. Install cloudflared via Cloudflare's apt repo (no token configured
#      here — user creates the tunnel in the CF UI and runs the install
#      token command manually; see docs/deploy.md)
#   6. Configure ufw: deny all inbound except 22 (your IP) + tunnelled traffic
#   7. Print the next-step checklist for the user
#
# Pre-requisites the user does first (not automated here):
#   - SSH'd in as `ubuntu` with sudo privileges
#   - Has the repo's deploy public SSH key ready to paste

set -euo pipefail

# ---- Pretty output helpers ----
RED='\033[0;31m'; GRN='\033[0;32m'; YEL='\033[1;33m'; CLR='\033[0m'
log()  { echo -e "${GRN}[setup]${CLR} $*"; }
warn() { echo -e "${YEL}[setup]${CLR} $*"; }
fail() { echo -e "${RED}[setup]${CLR} $*"; exit 1; }

[[ $EUID -eq 0 ]] || fail "Run with sudo: sudo $0"

# ============================================================
# 1. Base packages + system update
# ============================================================
log "Updating apt + installing base packages..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
    curl ca-certificates gnupg lsb-release ufw \
    fail2ban htop tini

# ============================================================
# 2. Docker engine + compose v2
# ============================================================
if command -v docker >/dev/null && docker compose version >/dev/null 2>&1; then
    log "Docker + compose v2 already installed, skipping."
else
    log "Installing Docker engine + compose plugin from Docker's apt repo..."
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | \
        gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg

    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
        > /etc/apt/sources.list.d/docker.list

    apt-get update -qq
    apt-get install -y -qq --no-install-recommends \
        docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

    systemctl enable --now docker
fi

# ============================================================
# 3. Deploy user (passwordless sudo limited to docker compose ops)
# ============================================================
if id deploy >/dev/null 2>&1; then
    log "deploy user already exists, skipping create."
else
    log "Creating 'deploy' system user..."
    useradd --create-home --shell /bin/bash --groups docker deploy
fi

# Re-use the ubuntu user's authorized_keys so the deploy user accepts
# the same SSH key the operator already used to SSH in.
if [[ -f /home/ubuntu/.ssh/authorized_keys ]]; then
    log "Copying SSH authorized_keys from ubuntu → deploy..."
    install -d -o deploy -g deploy -m 700 /home/deploy/.ssh
    install -o deploy -g deploy -m 600 \
        /home/ubuntu/.ssh/authorized_keys /home/deploy/.ssh/authorized_keys
else
    warn "No /home/ubuntu/.ssh/authorized_keys found — you'll need to "
    warn "manually add the GitHub Actions deploy key to /home/deploy/.ssh/authorized_keys"
fi

# Sudoers entry: deploy can ONLY run the docker compose commands needed
# for CD, no general root access. Limits blast radius of a compromised
# CD key.
cat > /etc/sudoers.d/deploy <<'EOF'
# Allow the CD pipeline (which SSHes in as deploy) to run docker
# compose pull/up/down/ps + image prune without a password prompt.
deploy ALL=(root) NOPASSWD: /usr/bin/docker compose -f /srv/dashboard/docker-compose.yml *
deploy ALL=(root) NOPASSWD: /usr/bin/docker image prune -f
deploy ALL=(root) NOPASSWD: /usr/bin/docker logout ghcr.io
deploy ALL=(root) NOPASSWD: /usr/bin/docker login ghcr.io *
EOF
chmod 440 /etc/sudoers.d/deploy

# ============================================================
# 4. /srv/dashboard layout + production docker-compose
# ============================================================
log "Creating /srv/dashboard layout..."
install -d -o deploy -g deploy -m 755 /srv/dashboard
install -d -o deploy -g deploy -m 755 /srv/dashboard/data

# Production compose file pulls the pre-built image from ghcr.io. The
# image tag is `latest` for simplicity; for production-grade pinning,
# switch to a git-SHA tag in the deploy.yml workflow and update this
# image: line at the same time.
COMPOSE_PATH=/srv/dashboard/docker-compose.yml
if [[ ! -f $COMPOSE_PATH ]]; then
    log "Writing production docker-compose.yml..."
    cat > $COMPOSE_PATH <<'EOF'
# Production compose — pulled by GitHub Actions CD pipeline.
# Differs from the repo-root docker-compose.yml in two ways:
#   1. Uses the pre-built ghcr.io image (no `build:` context — the VM
#      doesn't have build tools installed by design).
#   2. Binds port 8050 to 127.0.0.1 ONLY. Cloudflare Tunnel reaches it
#      via localhost; nothing else on the VM (or the public internet)
#      can talk to it directly.
services:
  app:
    image: ghcr.io/jxupower/hk-sentiment-scraper:latest
    container_name: hk-sentiment-dashboard
    ports:
      - "127.0.0.1:8050:8050"
    env_file:
      - /srv/dashboard/.env
    volumes:
      - /srv/dashboard/data:/app/data
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "--fail", "--silent", "http://localhost:8050/_dash-layout"]
      interval: 30s
      timeout: 10s
      start_period: 60s
      retries: 3
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "5"
EOF
    chown deploy:deploy $COMPOSE_PATH
else
    log "docker-compose.yml already exists, leaving alone."
fi

# .env placeholder — operator fills in the real secrets after this script.
ENV_PATH=/srv/dashboard/.env
if [[ ! -f $ENV_PATH ]]; then
    log "Writing .env placeholder (FILL IN SECRETS BEFORE FIRST DEPLOY)..."
    cat > $ENV_PATH <<'EOF'
# Production secrets — DO NOT COMMIT. chmod 600.
USE_CLOUD_DB=true
SUPABASE_DB_URL=postgresql://postgres:PASSWORD@HOST:5432/postgres
CLAUDE_API_KEY=
REDDIT_CLIENT_ID=
REDDIT_CLIENT_SECRET=
REDDIT_USER_AGENT=SentimentScraper/1.0 (production)
EOF
    chmod 600 $ENV_PATH
    chown deploy:deploy $ENV_PATH
fi

# ============================================================
# 5. cloudflared (tunnel binary; tunnel token added by the operator)
# ============================================================
if command -v cloudflared >/dev/null; then
    log "cloudflared already installed, skipping."
else
    log "Installing cloudflared via Cloudflare's apt repo..."
    mkdir -p --mode=0755 /usr/share/keyrings
    curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | \
        tee /usr/share/keyrings/cloudflare-main.gpg > /dev/null
    echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] \
https://pkg.cloudflare.com/cloudflared $(lsb_release -cs) main" \
        > /etc/apt/sources.list.d/cloudflared.list
    apt-get update -qq
    apt-get install -y -qq cloudflared
fi

# ============================================================
# 6. ufw firewall: deny all inbound except SSH (your IP) + nothing else
# ============================================================
# The dashboard is NEVER exposed publicly — cloudflared makes an outbound
# tunnel to Cloudflare's edge; no inbound ports need to be open. Only SSH
# stays open (and only from operator IPs, ideally tightened post-bootstrap).
log "Configuring ufw..."
ufw --force default deny incoming
ufw --force default allow outgoing
ufw --force allow 22/tcp comment "SSH (tighten to operator IP post-bootstrap)"
ufw --force enable

# ============================================================
# 7. Next-step checklist
# ============================================================
cat <<EOF

${GRN}=============================================================
                    BOOTSTRAP COMPLETE
=============================================================${CLR}

Next steps (manual — see docs/deploy.md for screenshots):

  1. Populate ${YEL}/srv/dashboard/.env${CLR} with your real secrets:
        sudo nano /srv/dashboard/.env

  2. ${YEL}Authenticate Docker to GitHub Container Registry${CLR} so the
     deploy pipeline can pull the private image:
        sudo docker login ghcr.io -u <gh-username> -p <PAT-with-read:packages>

     Generate the PAT at https://github.com/settings/tokens (classic).
     Scopes needed: read:packages

  3. ${YEL}Create the Cloudflare Tunnel in the CF UI${CLR}:
        Zero Trust → Networks → Tunnels → Create a tunnel (cloudflared)
        Copy the install token; run on this VM:
          sudo cloudflared service install <TOKEN>
          sudo systemctl restart cloudflared

     Then add Public Hostname:
        dashboard.<your-domain>  →  http://localhost:8050

  4. ${YEL}Set up Cloudflare Access${CLR} to gate the URL:
        Zero Trust → Access → Applications → Add self-hosted
        Application: dashboard.<your-domain>
        Policy: Allow emails matching your list

  5. ${YEL}Add GitHub repo secrets${CLR} at
     https://github.com/jxupower/hk-sentiment-scraper/settings/secrets/actions:
        VM_HOST           = $(hostname -I | awk '{print $1}')
        SSH_PRIVATE_KEY   = (contents of the deploy_key private key)

  6. ${YEL}Push to main${CLR} to trigger the first deploy.

EOF
