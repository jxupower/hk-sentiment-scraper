# Blue/green deploys (perf P2.11) — optional runbook

The default production compose (`docker-compose.yml`) recreates the
`app` container in-place on every deploy, giving a **~30-60 s downtime
window** while the container restarts + waits for its `HEALTHCHECK`
`start_period`. This runbook migrates the VM to a **zero-downtime**
blue/green setup fronted by Caddy.

Cost: **~200 MB extra RAM** for the idle app container + Caddy. On the
24 GB Oracle Ampere VM this is a rounding error. On smaller VMs
(Hetzner CX21 / 4 GB) you'd need to serialise deploys instead.

## Files involved

| File | What it is |
|---|---|
| `deploy/Caddyfile` | Reverse proxy config: `:8050` → `${UPSTREAM}` (defaults to `app-blue:8050`) |
| `docker-compose.bluegreen.yml` | Opt-in compose file with `caddy` + `app-blue` + `app-green` + `scheduler` |

## Migration from single-instance

On the VM (`ssh <vm>` as `deploy`):

```bash
# Stop and remove the current single-instance stack. Data volume
# (./data) is untouched.
sudo docker compose -f /srv/dashboard/docker-compose.yml down

# Copy the blue/green compose + Caddyfile into place.
sudo mkdir -p /srv/dashboard/deploy
sudo cp <path-to-repo>/docker-compose.bluegreen.yml /srv/dashboard/docker-compose.yml
sudo cp <path-to-repo>/deploy/Caddyfile             /srv/dashboard/deploy/Caddyfile

# Cold-start with blue live. Caddyfile's default $UPSTREAM points at
# app-blue, so no env is needed for the initial boot.
sudo docker compose -f /srv/dashboard/docker-compose.yml up -d

# Confirm: 4 containers running, caddy healthy, app-blue healthy.
sudo docker compose -f /srv/dashboard/docker-compose.yml ps
curl -sI http://127.0.0.1:8050/_dash-layout | head -1   # HTTP/1.1 200 OK
```

## Deploy flow (roll-forward)

Run this on the VM after `docker compose pull` has fetched the new
image. The script is intentionally shell — a five-liner is easier to
audit than a Python helper.

```bash
# Determine which slot is currently live by inspecting Caddy's env.
LIVE=$(sudo docker compose -f /srv/dashboard/docker-compose.yml exec caddy \
    printenv UPSTREAM | cut -d: -f1)
if [ "$LIVE" = "app-green" ]; then IDLE=app-blue; else IDLE=app-green; fi
echo "Live: $LIVE, deploying to: $IDLE"

# Recreate the idle slot on the new image.
sudo docker compose -f /srv/dashboard/docker-compose.yml up -d --force-recreate "$IDLE"

# Wait for the idle slot to become healthy (max 90 s).
for i in $(seq 1 18); do
    STATUS=$(sudo docker inspect --format='{{.State.Health.Status}}' \
        "hk-sentiment-${IDLE}" 2>/dev/null)
    [ "$STATUS" = "healthy" ] && { echo "$IDLE healthy after $((i*5))s"; break; }
    echo "[$i/18] $IDLE: $STATUS"; sleep 5
done
[ "$STATUS" = "healthy" ] || { echo "Deploy failed — $IDLE did not become healthy"; exit 1; }

# Flip the proxy. Caddy reload is graceful — in-flight requests drain
# on $LIVE, new requests hit $IDLE.
UPSTREAM="${IDLE}:8050" sudo docker compose -f /srv/dashboard/docker-compose.yml \
    up -d caddy    # env-var change requires recreate; caddy has no state
sudo docker compose -f /srv/dashboard/docker-compose.yml exec caddy \
    caddy reload -c /etc/caddy/Caddyfile

# Drain the old slot (5 s grace for late requests), then stop it.
sleep 5
sudo docker compose -f /srv/dashboard/docker-compose.yml stop "$LIVE"

echo "Deploy complete. Live: $IDLE"
```

## Rolling back

```bash
# If the freshly-deployed slot misbehaves, flip UPSTREAM back and
# restart the OLD slot (which was stopped by the deploy script above).
sudo docker compose -f /srv/dashboard/docker-compose.yml start "$LIVE"
UPSTREAM="${LIVE}:8050" sudo docker compose -f /srv/dashboard/docker-compose.yml \
    up -d caddy
sudo docker compose -f /srv/dashboard/docker-compose.yml exec caddy \
    caddy reload -c /etc/caddy/Caddyfile
```

Old-image container is still there (docker doesn't delete stopped
containers), so rollback is a `start` + `reload` — sub-second.

## What blue/green does NOT solve

- **Schema migrations.** If a deploy changes SQLite schema, both slots
  serve the same file — the OLD slot will crash on the new schema. For
  those (rare) deploys, use the single-instance compose file temporarily
  or accept the brief unavailability during the migration.
- **Scheduler downtime.** The `scheduler` service is still a singleton;
  a scheduler-image update recreates it in-place with ~10 s of gap.
  That's fine because the scheduler has no user-facing latency.
- **Multi-region.** Everything still lives on one Oracle VM; a data-center
  failure takes the app down. This is deliberate for the free-tier
  deployment; see P3 in the perf redesign plan for scale-out options.

## Automated deploy from GitHub Actions

The current `.github/workflows/deploy.yml` uses the single-instance
`docker compose up -d` shape. To wire blue/green into CI:

1. Copy the shell block above into a new step named `Blue/green flip`
   in `.github/workflows/deploy.yml`.
2. Delete the existing `Wait for healthy` step — the flip script has its
   own wait loop.
3. Commit + push. Next push to `main` deploys via blue/green.

Deferred to a future commit because CI-workflow edits deserve a solo PR.
