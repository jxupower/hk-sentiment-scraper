# Cloudflare edge caching for `/_dash-layout`

Perf P2.13 — cut container hit rate by ~80% for reload / return-visit
patterns by caching the initial Dash layout JSON at Cloudflare's edge for
60 seconds. First user pays origin cost; next 60 s of visitors hit the
edge cache. Container CPU on the Oracle VM drops noticeably on any
"team of 3 people all reload at 9 am" pattern.

**Scope**: `/_dash-layout` **only**. All the interactive endpoints
(`/_dash-update-component`, `/_dash-dependencies`, `/assets/*`) are
either state-dependent or already cached correctly by Dash itself.

## What NOT to cache (never add these to the rule)

| Endpoint | Why never |
|---|---|
| `/_dash-update-component` | Every callback POST — caching would freeze the UI on stale server state |
| `/_dash-dependencies` | Small JSON but changes on every deploy; the 5-min CF asset default already handles it fine |
| `/api/*`, `/health*` | Reserved for future endpoints; safer to be explicit |

## The rule (Cloudflare dashboard steps)

1. **Cloudflare Dashboard** → pick your zone (the domain that fronts
   `dashboard.<your-domain>`) → left sidebar → **Caching** → **Cache
   Rules** → **Create rule**
2. Name: `hk-dashboard: cache /_dash-layout at edge`
3. **When incoming requests match** — click "Custom filter expression"
   (not the simple form) and paste:
   ```
   (http.host eq "dashboard.<your-domain>" and http.request.uri.path eq "/_dash-layout")
   ```
   Replace `<your-domain>` with your actual apex domain. (If the
   dashboard lives at a subdomain like `dash.example.com`, use that
   full hostname in the `eq` comparison.)
4. **Then** section:
   - **Cache eligibility**: `Eligible for cache`
   - **Edge TTL**: choose `Override origin` → **1 minute** (60 s)
   - **Browser TTL**: `Respect origin` (leave the browser to obey any
     `Cache-Control` Dash sends — we only want to reduce origin
     hits, not to make the user's own browser stale)
   - **Cache key** → **Query string**: `Ignore query string`
     (Dash appends nothing meaningful to `/_dash-layout`)
   - Everything else: defaults
5. **Deploy**

## Verify

After ~1 minute of propagation, from any browser:

```bash
# First request — CF pierces to origin. Look for cf-cache-status: MISS
curl -sI https://dashboard.<your-domain>/_dash-layout | grep -iE 'cf-cache-status|age'

# Wait 5 seconds, request again — should show HIT with age=5
curl -sI https://dashboard.<your-domain>/_dash-layout | grep -iE 'cf-cache-status|age'

# Wait 65 seconds after the first — should show EXPIRED, then next
# request re-populates. cf-cache-status transitions: MISS -> HIT -> EXPIRED -> MISS.
```

If you see `DYNAMIC` instead of `MISS`, the rule didn't apply (double-check
the hostname in the expression).

## What this changes about the app

- **Nothing on the server side.** Dash still produces the layout JSON on
  every origin request. The container's advantage is that origin requests
  become rarer.
- **60 s of staleness is acceptable**: `/_dash-layout` describes the
  static shape of the tab tree — component IDs and default props. It
  does NOT contain any market/sentiment/portfolio data (that all flows
  through `/_dash-update-component` callbacks, which are NOT cached).
  Even during a deploy, the worst case is that a visitor in the first
  60 s sees the old shape for that brief window before their next
  interactive callback hits the new backend.
- **Cache invalidation on deploy**: not automatic. If you ever change
  the layout structure (new tab, renamed component ID), the CF cache
  will serve the old shape for up to 60 s to new visitors. If that ever
  bites, Cloudflare → Caching → Configuration → **Purge Cache** →
  **Custom purge** → URL: `https://dashboard.<your-domain>/_dash-layout`.

## Rollback

Cloudflare → Caching → Cache Rules → find the rule → toggle **Enabled**
off. Instant. No code deploy needed.
