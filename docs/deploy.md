# Deploy runbook — Oracle Cloud Always Free + Cloudflare

Single-VM, free-forever deployment of the dashboard + APScheduler scrapers
behind Cloudflare Tunnel and Cloudflare Access. Targets:

- **Hosting**: Oracle Cloud Always Free (4-core Ampere ARM VM · 24 GB RAM · always-on · no expiration)
- **Auth**: Cloudflare Access (free up to 50 users · email OTP / Google login)
- **CI/CD**: GitHub Actions → ghcr.io → SSH `docker compose pull && up -d`
- **Monitoring**: UptimeRobot free tier (5-min ping interval)
- **Cost**: $0/month

> [!IMPORTANT]
> This file describes the **production** deployment. Local development
> doesn't need any of this — just `docker compose up --build` works.

---

## Prerequisites (one-time, all done by you)

1. **Oracle Cloud account** — sign up at https://signup.cloud.oracle.com (asks for a card but won't charge for Always Free resources)
2. **Cloudflare account** (free) with a domain you control (or use a free `*.trycloudflare.com` URL for testing)
3. **GitHub access** to this repo with admin rights (for Secrets + Actions)

---

## Step 1 — Provision the Oracle VM

In the Oracle Cloud Console:

1. **Compute → Instances → Create Instance**
2. Name: `hk-dashboard`
3. **Image**: Canonical Ubuntu 22.04 (LTS)
4. **Shape**: `VM.Standard.A1.Flex` (Ampere ARM) — **4 OCPUs · 24 GB RAM** (within Always Free)
5. **Region**: pick `ap-tokyo-1` or `ap-singapore-1` — both have good akshare reachability for HK data
6. **Networking**: assign a public IPv4; in the VCN's Security List add ingress rules:
   - TCP/22 from your home IP /32 (initial SSH bootstrap; can tighten to CF IPs after tunnel is up)
   - TCP/80 + TCP/443 from `0.0.0.0/0` (only used by Cloudflare's tunnel pull, OS firewall blocks externally)
7. **SSH keys**: paste your public key
8. Click Create. Wait ~60s for state = Running.

Note the public IP — you'll need it for `VM_HOST` in step 4.

---

## Step 2 — Bootstrap the VM

SSH in:

```bash
ssh ubuntu@<VM_PUBLIC_IP>
```

Clone the bootstrap script (the repo only contains *its source*, you don't need the whole repo on the VM):

```bash
curl -fsSL https://raw.githubusercontent.com/jxupower/hk-sentiment-scraper/main/scripts/deploy_vm_setup.sh -o setup.sh
chmod +x setup.sh
sudo ./setup.sh
```

The script:

- Installs Docker + docker-compose plugin (apt + Docker's official repo)
- Creates `/srv/dashboard/` with the production `docker-compose.yml` (pointing at ghcr.io)
- Creates a `deploy` system user with the SSH key from `~/ubuntu/.ssh/authorized_keys` (re-used so the CD step uses the same key)
- Installs `cloudflared` from Cloudflare's apt repo and enables the systemd unit
- Configures `ufw`: deny all inbound, allow 22 (from your IP), allow Cloudflare's IP ranges for the tunnel
- Adds the GitHub Container Registry credentials to root's docker login so private pulls work

After the script finishes, populate the runtime secrets:

```bash
sudo nano /srv/dashboard/.env
```

Paste:

```
USE_CLOUD_DB=true
SUPABASE_DB_URL=postgresql://postgres.<project>:<password>@<pooler>.supabase.com:6543/postgres
CLAUDE_API_KEY=sk-ant-...
REDDIT_CLIENT_ID=...
REDDIT_CLIENT_SECRET=...
REDDIT_USER_AGENT=SentimentScraper/1.0 (production)
```

`chmod 600` the file. Never commit this to git.

---

## Step 3 — Cloudflare Tunnel + Access

### Tunnel (gives the VM a public HTTPS URL without opening ports)

1. Cloudflare dashboard → **Zero Trust** → **Networks → Tunnels** → **Create a tunnel** (Cloudflared)
2. Name: `hk-dashboard`
3. Copy the install token shown on screen
4. On the VM:
   ```bash
   sudo cloudflared service install <TOKEN>
   sudo systemctl restart cloudflared
   ```
5. Back in the Cloudflare UI, under **Public Hostname**:
   - Subdomain: `dashboard` (or whatever you prefer)
   - Domain: pick your domain
   - Service: `http://localhost:8050`

DNS gets created automatically. Within a minute, `https://dashboard.<your-domain>` is live (TLS via Cloudflare's edge).

### Access (gates the URL behind email login)

1. Cloudflare dashboard → **Zero Trust** → **Access → Applications** → **Add an application** → **Self-hosted**
2. App name: `Croissant Stock Analyser`
3. Application domain: `dashboard.<your-domain>`
4. Identity providers: enable **One-time PIN** (no setup, sends emails) and/or **Google** (OAuth)
5. Create a policy:
   - Action: Allow
   - Rule: `Emails → Include → <your friends' emails>` (newline-separated, up to 50 on free tier)
6. Save

Now `https://dashboard.<your-domain>` redirects to a Cloudflare login page; authorised emails get through, others get blocked at the edge.

---

## Step 4 — Wire up GitHub Actions CD

1. Generate a deploy SSH key locally:
   ```bash
   ssh-keygen -t ed25519 -f deploy_key -N "" -C "github-actions-deploy"
   ```
2. Append `deploy_key.pub` to `/home/deploy/.ssh/authorized_keys` on the VM
3. In the GitHub repo: **Settings → Secrets and variables → Actions → New repository secret**:
   - `VM_HOST` — Oracle VM public IP
   - `SSH_PRIVATE_KEY` — contents of `deploy_key` (the private one)

The `.github/workflows/deploy.yml` workflow handles the rest: on every push to `main` (after CI goes green), it builds the linux/arm64 image, pushes to `ghcr.io/jxupower/hk-sentiment-scraper:latest`, SSHes in as `deploy`, and runs `docker compose pull && up -d`.

First deploy: push any commit, watch Actions → Deploy → in ~5 min the dashboard is live at your CF URL.

---

## Step 5 — Monitoring (free)

1. UptimeRobot account (free) → **Add New Monitor**:
   - Monitor Type: HTTPS
   - URL: `https://dashboard.<your-domain>/_dash-layout`
   - Monitoring Interval: 5 minutes
2. Cloudflare Access will block UptimeRobot's pings by default. Two options:
   - **Service Token bypass** (recommended): in CF Access → Service Auth → Create service token; add a "Service Auth" rule to your Access app that allows the token; configure UptimeRobot to send the `CF-Access-Client-Id` + `CF-Access-Client-Secret` headers
   - **Bypass policy** (simpler, less secure): add a CF Access policy with action "Bypass" and rule "IP ranges → UptimeRobot's IPs"
3. Configure email/SMS alerts in UptimeRobot for >10min downtime

---

## Operations cheatsheet

```bash
# SSH to VM
ssh ubuntu@<VM_PUBLIC_IP>

# Live logs
sudo docker compose -f /srv/dashboard/docker-compose.yml logs --tail=100 --follow

# Force a redeploy (e.g. after editing /srv/dashboard/.env)
sudo docker compose -f /srv/dashboard/docker-compose.yml pull
sudo docker compose -f /srv/dashboard/docker-compose.yml up -d

# Rotate a secret
sudo nano /srv/dashboard/.env       # edit
sudo docker compose -f /srv/dashboard/docker-compose.yml restart

# Check container health
sudo docker compose -f /srv/dashboard/docker-compose.yml ps

# Disk usage (free tier limits)
df -h /
sudo docker system df

# Reclaim space (safe — bind mount survives)
sudo docker image prune -f

# Cloudflare tunnel status
sudo systemctl status cloudflared
sudo journalctl -u cloudflared -n 50

# Rollback to previous image tag (semantic version recommended for prod)
sudo docker compose -f /srv/dashboard/docker-compose.yml pull
# (edit image: tag in the compose file first if you tagged a SHA)
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `https://dashboard.<domain>` 502 Bad Gateway | App container down or not listening on 8050 | `docker compose ps` → if unhealthy, check `docker compose logs` |
| Cloudflare login loops infinitely | Cookie domain mismatch | In CF Access app settings, confirm "Custom Pages → Block Page" domain matches what you typed |
| Deploy workflow times out on SSH | VM firewall blocking the GHA runner IPs | GHA runners use AWS IPs; either temporarily open SSH to 0.0.0.0/0 during deploy, or use a self-hosted runner on the VM |
| Scraper logs say "akshare timeout" | Outbound network from Oracle region blocked | Try a different Oracle region (`ap-tokyo-1` is most reliable for HK / China endpoints) |
| Oracle reclaims the VM after 7 days | Idle CPU <20% triggered the reclamation | The 30-min APScheduler cycle should keep CPU above this; if not, add a 5-min CPU-bumping cron |
| `docker pull` fails with 403 from ghcr.io | Package set to private + no auth | Either make package public in repo settings, or `docker login ghcr.io -u <user> -p <PAT>` on VM |

---

## Disaster recovery

- **DB loss (local SQLite)**: the `/srv/dashboard/data/` bind mount holds `sentiment.db`. On full VM loss, articles/sentiment/signals are lost (those are only local). The HEAVY data — `historical_prices` + `fundamentals_snapshots` + `securities_reference` — lives in Supabase and survives any VM loss. First scrape cycle on a fresh VM will rebuild article/signal state from current feeds.
- **DB loss (Supabase)**: rare. Restore from Supabase's daily snapshots in the Supabase dashboard. Then redeploy.
- **Full VM loss**: provision a new Always-Free VM, re-run `setup.sh`, restore `/srv/dashboard/.env`, update GitHub `VM_HOST` secret, push to main.
- **Cloudflare account loss**: tunnel + Access policies need rebuilding. The VM itself stays reachable via SSH on the public IP.

---

## When NOT to use this deployment

- If you need >50 authenticated users → upgrade to CF Access Standard ($3/user/mo) or move to a different auth layer
- If you need geographic redundancy → upgrade to multi-region (Always Free is single-region by definition)
- If you need real-time push (websockets at scale) → Cloudflare Tunnel works but the free tier has connection limits
