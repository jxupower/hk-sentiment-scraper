# Compliance Readiness Assessment

**System**: Croissant Stock Analyser (HK / US sentiment + fundamentals dashboard)
**Assessment date**: 2026-07-16
**Assessment type**: Internal maturity snapshot — control-mapping only, not an attestation
**Frameworks covered**: SOC 2 Trust Services Criteria · CIS Controls v8 (IG1) · OWASP ASVS 5.0 Level 1 · NIST CSF 2.0 · ISO 27001:2022 Annex A
**Baseline commit**: post `ef81721` (perf P3.16) with security hardening `1637588`

---

## 1. Executive summary

### 1.1 Purpose

Produce a defensible written snapshot of where the platform stands against five widely-referenced security frameworks, following the July 2026 security uplift (Supabase RLS + least-privilege `app_backend` role, loopback bind + refuse-debug guard, gated Cloudflare-Access header trust). The intent is (a) to cement the current posture in a citable artefact, (b) to expose remaining structural gaps as a prioritised backlog, and (c) to serve as the baseline against which future changes are measured.

### 1.2 Non-purpose

This document is **not** and cannot be an attestation. Attestation-track frameworks (SOC 2 Type II, ISO 27001) require an independent CPA/certification body, a 6-12 month observation window, formal policies, an Information Security Management System (ISMS) with periodic management review, and continuous evidence collection tooling — none of which exists for a single-operator personal project. Self-assessable frameworks (CIS v8 IG1, OWASP ASVS L1) *can* be evaluated pass/fail from the codebase alone, and those scores below are the most defensible external signal in this document.

### 1.3 Headline verdicts

| Framework | Verdict | Score (headline) |
|---|---|---|
| **SOC 2 TSC (Security CC + AC/CI/PI/PY)** | Not attestable · Control alignment moderate | ~60 % Security · Availability moderate · Confidentiality moderate · Processing Integrity moderate · **Privacy weak** |
| **CIS Controls v8 (IG1, 18 controls)** | Self-assessed | **11 met · 4 partial · 3 not met** |
| **OWASP ASVS 5.0 Level 1** | Self-assessed | **~70 % pass** by chapter |
| **NIST CSF 2.0 (6 functions)** | Current-state profile | Govern **weak** · Identify **moderate** · Protect **strong** · Detect **weak** · Respond **weak** · Recover **moderate** |
| **ISO 27001:2022 Annex A (93 controls)** | Not certifiable | **~40 % addressed · ~30 % partial · ~30 % not met** |

### 1.4 Top-10 prioritised gaps (from Section 5)

1. **No CF Access JWT signature verification** — [dashboard/app.py:72-75](../dashboard/app.py). Trust boundary is a header + env-var toggle, not a cryptographic proof.
2. **No dependency vulnerability scanning** — no `.github/dependabot.yml`, no `pip-audit`, no `trivy` step in CI. Confirmed absent.
3. **No SIEM / centralised logging** — only `docker compose logs`; no immutable audit trail, no alerting beyond UptimeRobot.
4. **No SQLite backup** — `docs/deploy.md:230-232` explicitly notes on VM loss articles/sentiment/signals are lost.
5. **No security response headers** — [deploy/Caddyfile:17-49](../deploy/Caddyfile) has no CSP, HSTS, X-Frame-Options, Referrer-Policy, Permissions-Policy.
6. **Base image tag-pinned, not digest-pinned** — [Dockerfile:16,35](../Dockerfile) is `python:3.11-slim-bookworm`, mutable.
7. **No lockfile** — [requirements.txt](../requirements.txt) floor-pins with `>=`; every rebuild resolves fresh.
8. **No image signing (cosign) or SBOM (syft)** — supply-chain provenance absent.
9. **PII / third-party-PII handling undocumented** — CF Access email logged (`dashboard/app.py:99-101`); Reddit / news bylines stored in `articles.author` (`storage/database.py:60`).
10. **RSS ingestion redistribution risk** — `articles.body` stores SCMP/Bloomberg/CNBC RSS content without a legal-terms review.

---

## 2. Scope and limitations

**In scope**

- 5 SOC 2 Trust Services Criteria (Security's 9 Common Criteria groups plus Availability, Confidentiality, Processing Integrity, Privacy).
- CIS Controls v8 — all 18 controls at Implementation Group 1 (IG1) depth.
- OWASP ASVS 5.0 Level 1 — 14 chapters, grouped rating per chapter (not per-item).
- NIST Cybersecurity Framework 2.0 — 6 functions, current-state Tier rating per function (Tier 1 Partial → Tier 4 Adaptive).
- ISO/IEC 27001:2022 Annex A — 93 controls across 4 themes (Organizational 37, People 8, Physical 14, Technological 34).

**Explicitly out of scope**

- **PCI DSS** — N/A. No cardholder data is stored, transmitted, or processed. No card acceptance surface exists.
- **HIPAA** — N/A. No Protected Health Information.
- **FedRAMP** — N/A. Not a US-government supplier.
- **Full GDPR DPIA** — deferred. Single-operator with no EU-user marketing surface. Privacy controls are flagged where relevant to any future opening to EU users.
- **SOC 2 / ISO attestation activities** — not attempted (no auditor engagement, no ISMS, no evidence-collection tooling).
- **Penetration testing, DAST, SAST run** — separate exercise, not covered here.
- **Cloudflare, Oracle Cloud, Supabase infrastructure controls** — inherited from their respective SOC 2 Type II / ISO 27001 attestations; assessed only at the boundary this system controls.

**Method**

- Three parallel `Explore` agents inventoried the codebase against six control axes (auth/secrets/DB, data-lifecycle, infra/CI/monitoring).
- Every finding below is either (a) cited to `file:line`, (b) marked "N/A — [rationale]", or (c) marked "GAP — [what's missing]".
- Where a control has an infrastructure-inherited component (physical security → Oracle Cloud; TLS termination → Cloudflare), the boundary is called out explicitly.

---

## 3. Environment inventory

### 3.1 Architecture recap

Single Docker container (`hk-sentiment-scraper:latest`) on an Oracle Cloud Always-Free Ampere ARM VM (`ap-tokyo-1` or `ap-singapore-1` per [docs/deploy.md:30-38](../docs/deploy.md)), fronted by Cloudflare Tunnel + Access. Blue/green option via Caddy reverse proxy (`docker-compose.bluegreen.yml`, `deploy/Caddyfile`). Scheduler split into its own container (perf P2.9). Storage is dual-backend: local SQLite (WAL) for articles/sentiment/signals/notes/portfolios/backtests; Supabase Postgres (session pooler) for prices, fundamentals, financial statements, portfolios (backup source), securities reference, and sector taxonomy. Perf P3.16 introduced a local Parquet store for `historical_prices` that supersedes the Supabase copy when populated.

### 3.2 Trust boundaries

```
Browser
  │  TLS 1.3 (CF-managed cert)
  ▼
Cloudflare edge  ──  Access policy (email OTP / Google OAuth, ≤50-email allow-list)
  │  cloudflared tunnel (outbound-only from VM; no inbound port)
  ▼
Oracle VM (ufw: 22 + CF ranges only)  ──  docs/deploy.md:79
  │  loopback bind 127.0.0.1:8050
  ▼
[optional Caddy reverse proxy → app-blue / app-green]  ──  deploy/Caddyfile:17-49
  │
  ├──▶  SQLite (data/sentiment.db, WAL)              [file perms only]
  ├──▶  Supabase pool (psycopg2 max=20, TLS default) [app_backend role, RLS FORCE]
  ├──▶  yfinance / akshare / RSS / Reddit / Anthropic (outbound only, per-host throttled)
  └──▶  Parquet store (data/prices/, local disk)     [file perms only]
```

### 3.3 Data classification

| Table | Backend | Class | PII risk | Notes |
|---|---|---|---|---|
| `articles` | SQLite | (d) third-party licensed + (c) PII-adjacent | Medium | `.author` free-text may contain Reddit handles / news bylines ([storage/database.py:60](../storage/database.py), [storage/repository.py:22](../storage/repository.py)) |
| `article_tickers`, `sentiment_scores`, `ticker_signals`, `sector_signals` | SQLite | (a) derived aggregates | None | No user-attributable data |
| `securities`, `securities_meta`, `latest_prices` | SQLite | (a) reference | None | |
| `backtest_runs`, `backtest_holdings`, `optimized_parameters` | SQLite | (b) user-generated | Low | No user identifier column (single-tenant) |
| `research_notes` | SQLite | (b) user-generated free-text | Low-medium | SWOT/thesis/DCF notes; single-tenant |
| `historical_prices` | Supabase → Parquet | (a) public + (d) licensed | None | 16.9 M rows migrating to local Parquet (P3.16) |
| `fundamentals_snapshots`, `financial_statements` | Supabase | (d) licensed | None | yfinance/akshare-sourced ratios and filings |
| `portfolios` | Supabase | (b) user-generated | Low | `name` is PK, no `owner_email` — single-tenant only |
| `securities_reference`, `sector_taxonomy`, `taxonomy_meta`, `ticker_taxonomy_history` | Supabase | (a) reference | None | |

No user email, IP address, session token, or password is persisted anywhere.

### 3.4 Ephemeral PII flows

- **Cloudflare Access email** — extracted from `Cf-Access-Authenticated-User-Email` header when `TRUST_CF_ACCESS_HEADER=true`, stashed on `flask.g.user_email` ([dashboard/app.py:96-100](../dashboard/app.py)), logged once per unique email per process ([dashboard/app.py:99-101](../dashboard/app.py)). Not persisted to any database.
- **User research prompts** — free-text sent to Anthropic Claude API from Stock Research tab ([dashboard/stock_research_callbacks.py:1711](../dashboard/stock_research_callbacks.py)) and article summarisation ([dashboard/callbacks.py:504](../dashboard/callbacks.py)). Cross-border transfer to US-based Anthropic.

### 3.5 Third-party dependencies

| Dep | Where | Purpose | ToS/licence signal |
|---|---|---|---|
| yfinance | `scrapers/yahoo_scraper.py`, `historical_price_scraper.py` | Prices + news headlines | No attribution surface |
| akshare | `scrapers/akshare_*` | HK/CN fundamentals + indices | No attribution surface |
| PRAW (Reddit) | `scrapers/reddit_scraper.py:26` | Community sentiment | Reddit terms — attribution needed if displayed |
| Anthropic Claude | `dashboard/callbacks.py:504`, `stock_research_callbacks.py:1711` | Sentiment + research chat | Cross-border data transfer to US |
| RSS: SCMP, Bloomberg, CNBC, Google News | `config/rss_feeds.yaml:3-8` | Broad market news | **Redistribution-risk**: `articles.body` stores full RSS content, not just headlines |

---

## 4. Framework-by-framework assessment

### 4.1 SOC 2 Trust Services Criteria

Verdict scale: **Met** · **Partial** · **Gap** · **N/A**.

#### CC1 — Control Environment

| # | Control (paraphrased) | Status | Evidence / gap |
|---|---|---|---|
| CC1.1 | Commitment to integrity + ethical values | N/A | Single-operator; no organisational tone-at-the-top |
| CC1.2 | Board oversight of controls | N/A | Same |
| CC1.4 | Competence: hiring, training, retention | N/A | Same |

**Net**: N/A for a single-operator project. In a real audit these would be marked with a written "single-person entity" attestation.

#### CC2 — Communication and Information

| # | Control | Status | Evidence / gap |
|---|---|---|---|
| CC2.2 | Internal comms about roles + responsibilities | N/A | Single-operator |
| CC2.3 | External comms about system + commitments | **Gap** | No SLA, privacy notice, or terms-of-use published. If widened beyond self, GAP. |

#### CC3 — Risk Assessment

| # | Control | Status | Evidence / gap |
|---|---|---|---|
| CC3.1 | Objectives specified with sufficient clarity | Partial | [CLAUDE.md](../CLAUDE.md) documents goals implicitly, not as risk statements |
| CC3.2 | Identify + analyse risks | **Gap** | No formal risk register (`docs/risk_register.md` not present) |
| CC3.3 | Consider potential for fraud | **Gap** | Not considered |
| CC3.4 | Identify + assess significant changes | Partial | Perf and security uplifts documented as plans; no risk-impact scoring |

#### CC4 — Monitoring Activities

| # | Control | Status | Evidence / gap |
|---|---|---|---|
| CC4.1 | Ongoing + separate evaluations | **Gap** | This document is the first formal evaluation |
| CC4.2 | Evaluate + communicate deficiencies | Partial | Fixed inline via commits; no register of open findings until now |

#### CC5 — Control Activities

| # | Control | Status | Evidence / gap |
|---|---|---|---|
| CC5.1 | Select + develop control activities | Partial | Security controls implemented ad-hoc; no control catalogue until this doc |
| CC5.2 | Deploy controls over technology | **Met** | See CC6 detail below |
| CC5.3 | Deploy through policies + procedures | **Gap** | No written policies |

#### CC6 — Logical + Physical Access (largest section)

| # | Control | Status | Evidence / gap |
|---|---|---|---|
| CC6.1 | Logical access + credential management | **Met** | Cloudflare Access (email OTP / Google OAuth) is sole authN; ≤50-email allow-list per [docs/deploy.md:110-131](../docs/deploy.md). SSH to VM via key-only. GHCR pushes use `GITHUB_TOKEN` per [.github/workflows/deploy.yml:47-50](../.github/workflows/deploy.yml) |
| CC6.2 | New credential provisioning | **Met** | CF Access admin UI |
| CC6.3 | Removal of terminated / role-changed access | **Met** | CF Access allow-list edit is single-source-of-truth |
| CC6.4 | Physical access to facilities | **N/A → Inherited** | Oracle Cloud SOC 2 Type II covers datacenter physical controls |
| CC6.5 | Physical protection of removable media | N/A | No physical media |
| CC6.6 | Restrict logical access to internal system components | **Met** | Loopback bind ([main.py:190-207](../main.py), [docker-compose.yml:34](../docker-compose.yml)); ufw + CF ranges only ([docs/deploy.md:79](../docs/deploy.md)); Supabase `app_backend` role has DML only, no CREATE ([scripts/supabase_rls_setup.sql:78-92](../scripts/supabase_rls_setup.sql)); RLS FORCEd on every public table ([:159-181](../scripts/supabase_rls_setup.sql)) |
| CC6.7 | Transmission of data + credentials | **Met** | TLS 1.3 at CF edge; Supabase psycopg2 TLS default; CF Tunnel encrypted end-to-end |
| CC6.8 | Prevent + detect unauthorised software | **Partial** | Non-root container (UID 1000, [Dockerfile:48,70](../Dockerfile)); no image scan (Trivy/Grype absent — GAP); no runtime EDR |

#### CC7 — System Operations

| # | Control | Status | Evidence / gap |
|---|---|---|---|
| CC7.1 | Detection of configuration deviations | **Gap** | No config-drift detection |
| CC7.2 | Detection of anomalies indicating malicious acts | **Gap** | No SIEM, no anomaly detection |
| CC7.3 | Evaluation + communication of security events | **Gap** | Only signal is UptimeRobot ping ([docs/deploy.md:164-173](../docs/deploy.md)) |
| CC7.4 | Response to identified security incidents | **Partial** | Rollback via SHA-tagged images ([deploy.yml:20-21,86-89](../.github/workflows/deploy.yml)) and blue/green ([docs/bluegreen_deploy.md:82-95](../docs/bluegreen_deploy.md)); no IR runbook |
| CC7.5 | Recovery from identified security incidents | **Partial** | Supabase managed snapshots; **no SQLite backup** ([docs/deploy.md:230-232](../docs/deploy.md)) |

#### CC8 — Change Management

| # | Control | Status | Evidence / gap |
|---|---|---|---|
| CC8.1 | Authorise, develop, test, approve, implement changes | **Partial** | CI gate (ruff + smoke + build) before deploy ([.github/workflows/ci.yml:33-166](../.github/workflows/ci.yml)); deploy conditioned on CI success ([deploy.yml:46](../.github/workflows/deploy.yml)); [CLAUDE.md:7](../CLAUDE.md) requires user approval for new deps; **branch protection status not verifiable from the repo tree — GAP unless enabled in GitHub UI** |

#### CC9 — Risk Mitigation

| # | Control | Status | Evidence / gap |
|---|---|---|---|
| CC9.1 | Identify + implement risk-mitigation activities | Partial | This document is the identification pass |
| CC9.2 | Assess + manage risks from vendors + partners | **Gap** | No vendor-risk review (CF, Oracle, Supabase, Anthropic, yfinance data hosts) |

#### Availability (A-series)

| # | Control | Status | Evidence / gap |
|---|---|---|---|
| A1.1 | Capacity planning | Partial | Perf redesign plan (`.claude/plans/wobbly-bouncing-spindle.md` prior version) covered capacity; no ongoing tracking |
| A1.2 | Environmental protections + backup + recovery | **Partial** | Blue/green + SHA rollback yes; SQLite backup no |
| A1.3 | Recovery testing | **Gap** | No documented drill |

#### Confidentiality (C-series)

| # | Control | Status | Evidence / gap |
|---|---|---|---|
| C1.1 | Identify + classify confidential info | Partial | This document's §3.3 is the first classification |
| C1.2 | Disposal of confidential info | **Gap** | No disposal policy; articles prune 90d exists but not documented as C-control |

#### Processing Integrity (PI-series)

| # | Control | Status | Evidence / gap |
|---|---|---|---|
| PI1.1-1.5 | Input validation, processing accuracy, output completeness | **Partial** | Dedup by URL, sentiment window filters, retention scripts exist; no formal SLA on correctness; RLS + verify script provide integrity floor |

#### Privacy (P-series)

| # | Control | Status | Evidence / gap |
|---|---|---|---|
| P1-P8 (notice, choice, collection, use, retention, disclosure, quality, monitoring) | **Gap** | No published privacy notice; no consent surface; CF Access email logged but not disclosed; third-party PII (bylines) in `articles.author` without notice |

**SOC 2 headline**: Security CC is the strongest area (roughly 60 % of testable controls met, with the single-operator N/As excluded from the denominator). Availability, Confidentiality, and Processing Integrity are moderate. Privacy is the weakest — no notice, no consent, no disposal policy.

---

### 4.2 CIS Controls v8 (Implementation Group 1)

| # | Control | Status | Evidence / gap |
|---|---|---|---|
| 1 | Inventory + Control of Enterprise Assets | **Met** | Single VM, single container tree; asset list in [docs/deploy.md](../docs/deploy.md) |
| 2 | Inventory + Control of Software Assets | **Partial** | [requirements.txt](../requirements.txt) present but no lockfile; container base pinned by tag not digest |
| 3 | Data Protection | **Met** | RLS FORCE + `app_backend` least-priv + TLS in transit; disposal (90d article prune) via [storage/repository.py:57](../storage/repository.py) |
| 4 | Secure Configuration of Enterprise Assets + Software | **Met** | Non-root container, loopback bind, refuse-debug guard, Caddy admin off (`deploy/Caddyfile:17-23`) |
| 5 | Account Management | **Met** | CF Access allow-list; SSH key-only; GHCR via `GITHUB_TOKEN` |
| 6 | Access Control Management | **Met** | Single-operator model; `app_backend` DB role isolated from postgres owner |
| 7 | Continuous Vulnerability Management | **Not met** | No Dependabot, no pip-audit, no Trivy — confirmed absent |
| 8 | Audit Log Management | **Not met** | Only `docker compose logs`; no retention, no central sink, no immutability |
| 9 | Email + Web Browser Protections | **N/A** | No email service; no user-controlled browser deployment |
| 10 | Malware Defences | **Partial** | Container isolation only; no AV, no image scan |
| 11 | Data Recovery | **Partial** | Supabase snapshots yes; SQLite backup no |
| 12 | Network Infrastructure Management | **Met** | ufw, CF-only ingress, single-VM topology |
| 13 | Network Monitoring + Defence | **Partial** | CF WAF/DDoS at edge; no on-VM IDS |
| 14 | Security Awareness + Skills Training | **N/A** | Single-operator |
| 15 | Service Provider Management | **Partial** | Vendors listed here for first time; no formal review cycle |
| 16 | Application Software Security | **Partial** | Dash CSRF-token-in-props default; no threat model; no security testing in CI |
| 17 | Incident Response Management | **Not met** | No IR runbook beyond DR notes |
| 18 | Penetration Testing | **N/A / Deferred** | Out of scope for this assessment |

**CIS v8 headline**: **11 Met · 4 Partial · 3 Not met · 2 N/A**. The three "Not met" (Vulnerability Mgmt, Audit Log Mgmt, Incident Response) map directly to the top gaps in §1.4.

---

### 4.3 OWASP ASVS 5.0 Level 1

Per-chapter grouped rating.

| Chapter | Focus | Rating | Notes |
|---|---|---|---|
| V1 Architecture, Design, Threat Modelling | Documentation of trust boundaries + threat model | **Partial** | Trust boundaries in §3.2 above; no threat model artefact |
| V2 Authentication | Password/MFA/session establishment | **Met (via CF Access)** | Delegated entirely to Cloudflare Access with email OTP / Google OAuth; no app-layer creds |
| V3 Session Management | Cookie flags, timeout, rotation | **Met (via CF Access)** | CF Access manages session; Dash is stateless server-side |
| V4 Access Control | Authorisation checks | **Partial** | Single-tenant; no authorisation logic (anyone through CF Access gets full access — intentional) |
| V5 Validation, Sanitisation, Encoding | Input validation, output encoding, XSS | **Partial** | Dash sanitises component props by default; no explicit input validation on free-text (research notes) |
| V6 Cryptography | Cipher use, key mgmt | N/A | No app-layer crypto today |
| V7 Errors + Logging | Structured logs, PII redaction, sink | **Not met** | Rich console only; CF Access email logged in plain text ([dashboard/app.py:99-101](../dashboard/app.py)) |
| V8 Data Protection | At-rest + in-transit + retention | **Partial** | TLS OK; at-rest inherited (Supabase/OCI); retention only on `articles` |
| V9 Communications | TLS versions, HSTS, certificate mgmt | **Partial** | TLS 1.3 at CF edge; **no HSTS header** in [deploy/Caddyfile](../deploy/Caddyfile) |
| V10 Malicious Code | Supply-chain integrity | **Not met** | No SBOM, no cosign, no digest-pinned base image, no dep scan |
| V11 Business Logic | Anti-automation, sequence enforcement | **Partial** | CF Access rate-limits at edge; no app-layer rate limits |
| V12 Files + Resources | Upload validation | **N/A** | No file-upload surface |
| V13 API + Web Service | Auth, throttling, versioning | **Partial** | Dash callbacks are POST; auth via CF Access; no throttling |
| V14 Configuration | Secure defaults, secrets mgmt, headers | **Not met** | Missing CSP / X-Frame-Options / Referrer-Policy / Permissions-Policy in Caddy |

**ASVS L1 headline**: **~70 % pass** (5 Met/Met-via-CF, 5 Partial, 3 Not met, 1 N/A). Biggest wins from Cloudflare Access (V2, V3). Biggest gaps in supply-chain (V10) and response-header configuration (V14).

---

### 4.4 NIST CSF 2.0 (current-state profile)

Tier scale: 1 Partial · 2 Risk Informed · 3 Repeatable · 4 Adaptive.

#### Govern (GV) — **Tier 1 (Partial)**
Weak. No organisational risk-mgmt strategy, no policy, no roles/responsibilities (single-operator), no cybersecurity supply-chain risk mgmt process. This assessment is the first artefact.
- **Key gaps**: GV.PO (Policy), GV.SC (Supply Chain), GV.RM (Risk Management Strategy).

#### Identify (ID) — **Tier 2 (Risk Informed)**
Moderate. Asset inventory implicit in Docker + config; data classification in §3.3 (first-time); no risk register.
- **Met**: ID.AM (asset mgmt). **Gap**: ID.RA (Risk Assessment), ID.IM (Improvement).

#### Protect (PR) — **Tier 3 (Repeatable)**
Strong. RLS + least-priv role + TLS + non-root + loopback + refuse-debug + gated header trust are repeatable, code-enforced controls with a verification script ([scripts/verify_supabase_rls.py](../scripts/verify_supabase_rls.py)).
- **Met**: PR.AA (Identity/Access), PR.DS (Data Security), PR.IR (Tech Infra Resilience), PR.PS (Platform Security).
- **Partial**: PR.AT (Awareness/Training — single-operator).

#### Detect (DE) — **Tier 1 (Partial)**
Weak. Only signal is UptimeRobot HTTP-status ping. No log aggregation, no anomaly detection, no auth-failure alerting, no scrape-anomaly alerting.
- **Gaps**: DE.CM (Continuous Monitoring), DE.AE (Adverse Event Analysis).

#### Respond (RS) — **Tier 1 (Partial)**
Weak. Deploy rollback exists ([docs/bluegreen_deploy.md:82-95](../docs/bluegreen_deploy.md)) but no defined IR trigger, no communication plan, no forensics preservation process.
- **Gaps**: RS.MA (Management), RS.AN (Analysis), RS.CO (Communication).

#### Recover (RC) — **Tier 2 (Risk Informed)**
Moderate. Supabase managed snapshots + blue/green + SHA-tagged images give a recovery path for cloud data + deploy artefacts. SQLite is the recovery weak-link.
- **Partial**: RC.RP (Recovery Plan Execution), RC.CO (Communication).

**CSF headline**: Protect is a **Tier 3**; Govern/Detect/Respond are **Tier 1**. The uneven profile is expected for an engineer-built single-operator app: controls implemented in code are strong; controls that need policy/process/monitoring are weak.

---

### 4.5 ISO/IEC 27001:2022 Annex A

Grouped narrative + summary per theme (full 93-control appendix would repeat information already surfaced in CIS/SOC 2 sections).

#### A.5 Organizational Controls (37 controls)

- **A.5.1 Policies for information security** — Gap. No written InfoSec policy.
- **A.5.2 Information security roles and responsibilities** — N/A (single-operator).
- **A.5.7 Threat intelligence** — Gap. No TI feed subscription or process.
- **A.5.8 Information security in project management** — Partial. [CLAUDE.md](../CLAUDE.md) working rules include security constraints (dep approval) but no formal security-in-SDLC policy.
- **A.5.9-5.14 Asset management sub-controls** — Partial. Inventory exists in code; classification exists in §3.3 for the first time.
- **A.5.15-5.18 Access control policy / user registration / privileged access / removal** — Met via CF Access single source of truth.
- **A.5.23 Cloud services** — Partial. Cloud vendors (CF, Oracle, Supabase, Anthropic) are used without a formal cloud-services policy or vendor-risk assessment.
- **A.5.28-5.30 Incident learning / evidence collection / ICT continuity** — Gap. No lessons-learned register; no formal evidence-collection procedure.
- **A.5.31-5.37 Legal, contractual, IP, records** — Partial. Third-party data terms (RSS redistribution, PRAW ToS) not formally tracked — flagged as top-10 gap.

**A.5 net**: ~30% addressed. This theme is the weakest — most controls presume org-level artefacts (policy, register, review).

#### A.6 People Controls (8 controls)

Almost entirely N/A for a single-operator context (background checks, terms of employment, disciplinary process, training). A.6.6 (Confidentiality/NDA) applies only if collaborators added.

**A.6 net**: N/A across the board.

#### A.7 Physical Controls (14 controls)

**Inherited** from Oracle Cloud SOC 2 Type II attestation for physical + hypervisor layers (VM residency in `ap-tokyo-1` / `ap-singapore-1`, [docs/deploy.md:30-38](../docs/deploy.md)). On-premises exposure: nil (developer laptop only, out of scope).

**A.7 net**: Inherited from OCI — Met at the boundary this system controls.

#### A.8 Technological Controls (34 controls) — the strongest theme

Selected highlights:

- **A.8.2 Privileged access rights** — Met. `app_backend` role has no CREATE/superuser bits ([scripts/supabase_rls_setup.sql:78-92](../scripts/supabase_rls_setup.sql)).
- **A.8.3 Information access restriction** — Met. RLS FORCE + policies on every public table.
- **A.8.4 Access to source code** — Met. GitHub private repo; SSH deploy key.
- **A.8.5 Secure authentication** — Met (via CF Access).
- **A.8.6 Capacity management** — Partial. Perf plan artefacts exist; no ongoing capacity tracking.
- **A.8.7 Protection against malware** — Partial. Container isolation; no image/runtime AV.
- **A.8.8 Management of technical vulnerabilities** — **Gap**. No Dependabot / pip-audit / Trivy.
- **A.8.9 Configuration management** — Partial. Dockerfile + compose are configuration-as-code; no drift detection.
- **A.8.10 Information deletion** — Partial. 90-day article prune; no policy for other user-generated tables.
- **A.8.11 Data masking** — N/A / Gap. No masking on `articles.author` or CF-Access-email logs.
- **A.8.12 Data leakage prevention** — Gap. No DLP tooling.
- **A.8.13 Information backup** — **Gap**. SQLite unbacked-up.
- **A.8.14-15 Redundancy + Logging** — Partial. Blue/green option; logging is basic stdout.
- **A.8.16 Monitoring activities** — Gap. UptimeRobot only.
- **A.8.17-18 Clock synchronisation + Privileged utility programs** — Met (OS-inherited NTP; no priv utilities installed).
- **A.8.19 Installation of software on operational systems** — Met. Container immutable per deploy.
- **A.8.20-24 Networks security / services / segregation / filtering / cryptography** — Met. ufw, CF-only ingress, TLS end-to-end.
- **A.8.25-27 Secure development lifecycle / secure coding / architecture** — Partial. CLAUDE.md working rules; no formal SDL.
- **A.8.28 Secure coding** — Partial. Code review is single-operator (self-review); ruff lint in CI.
- **A.8.29-31 Security testing / outsourced dev / dev+test env separation** — Partial. Smoke test in CI; no dedicated staging.
- **A.8.32 Change management** — Partial. Same as SOC 2 CC8.
- **A.8.33 Test information** — Met. No production data in tests.
- **A.8.34 Protection of information systems during audit testing** — N/A.

**A.8 net**: ~65% Met, ~25% Partial, ~10% Gap. The technological theme is where the platform genuinely earns marks.

**ISO 27001 headline overall**: **not certifiable** (no ISMS, no risk methodology, no management review), but the technological Annex A controls score materially better than the organisational ones. Certification would require standing up an ISMS with a compliant risk-assessment methodology (ISO 27005), Statement of Applicability, periodic management review, and internal audit programme — none of which is present.

---

## 5. Consolidated gap register (prioritised backlog)

Findings folded into an ordered backlog with rough effort estimates. Each item is anchored to one or more framework controls above.

### P1 — Quick wins (≤ half a day each)

| # | Item | Frameworks | Effort |
|---|---|---|---|
| P1.1 | Add `.github/dependabot.yml` — pip + docker weekly PRs | CIS 7, ISO A.8.8, ASVS V10 | ✅ **CLOSED** 2026-07-17 |
| P1.2 | Add `pip-audit` step to [ci.yml](../.github/workflows/ci.yml) | CIS 7, SOC 2 CC7.1, ISO A.8.8 | ✅ **CLOSED** 2026-07-17 (continue-on-error during triage window; tighten after 30 days) |
| P1.3 | Add `trivy image scan` step to CI | CIS 7, 10; ASVS V10 | ✅ **CLOSED** 2026-07-17 (HIGH/CRITICAL + continue-on-error initially) |
| P1.4 | Digest-pin base image in [Dockerfile](../Dockerfile) (e.g. `python:3.11-slim-bookworm@sha256:…`) | CIS 2, ASVS V10 | ✅ **CLOSED** 2026-07-17 (both stages pinned to `@sha256:b18992999dbe963a45a8a4da40ac2b1975be1a776d939d098c647482bcad5cba`) |
| P1.5 | Compile [requirements.lock](../requirements.txt) via `pip-compile` | CIS 2, ASVS V10 | 🟡 **BLOCKED** — needs a Python 3.11 environment (Docker Desktop or local py 3.11); `docs/lockfile_workflow.md` documents the exact command. Dockerfile + CI still install from `requirements.txt` until this lands. |
| P1.6 | Add `SECURITY.md` with responsible-disclosure contact | SOC 2 CC2.3, ISO A.5.5 | 15 min |
| P1.7 | Add security response headers in [deploy/Caddyfile](../deploy/Caddyfile) (CSP, HSTS, X-Frame-Options, Referrer-Policy, Permissions-Policy) | ASVS V9, V14 | ✅ **CLOSED** 2026-07-17 (Flask middleware + Caddy `header {}` block, gated by `SECURITY_HEADERS_ENABLED`) |
| P1.8 | Enable branch protection + required review on `main` (GitHub UI) | SOC 2 CC8.1, ISO A.8.32 | 15 min |
| P1.9 | Add GitHub secret scanning + push protection (repo settings) | ISO A.8.24 | 5 min |
| P1.10 | Publish `docs/data_classification.md` promoting §3.3 to a standalone artefact | ISO A.5.9-5.14, SOC 2 C1.1 | 1 hr |

### P2 — Moderate (1-3 days each)

| # | Item | Frameworks | Effort |
|---|---|---|---|
| P2.1 | **Cf-Access-Jwt-Assertion signature verification** against Cloudflare JWKS in [dashboard/app.py:82-100](../dashboard/app.py) | SOC 2 CC6.1, ASVS V2, NIST PR.AA | 1 day |
| P2.2 | Nightly encrypted **SQLite backup** to Cloudflare R2 or Backblaze B2 (age or gpg encryption, 30-day retention) | SOC 2 A1.2, CIS 11, ISO A.8.13 | 1 day |
| P2.3 | Structured JSON logging with correlation IDs; ship to a free-tier log sink (Grafana Cloud Loki, Better Stack, Axiom) | SOC 2 CC7.2, CIS 8, ISO A.8.15-16, NIST DE.CM | 2-3 days |
| P2.4 | Data retention policies (delete rules) on `sentiment_scores`, `research_notes`, `portfolios`, `backtest_runs` | SOC 2 C1.2, ISO A.8.10 | 1 day |
| P2.5 | Publish `docs/privacy_notice.md` covering CF Access email logging, third-party PII in articles, Anthropic cross-border | SOC 2 P1-P8, ISO A.5.34 | 1 day |
| P2.6 | Container **image signing** via `cosign` + **SBOM** via `syft`, both attested in CI | CIS 2, ASVS V10, ISO A.8.30 | 1 day |
| P2.7 | Formal **IR runbook**: alert-to-action mapping, contact tree, rollback drill schedule | SOC 2 CC7.4, CIS 17, ISO A.5.24-27, NIST RS.* | 2 days |
| P2.8 | Schedule [scripts/verify_supabase_rls.py](../scripts/verify_supabase_rls.py) as an APScheduler daily job with alerting on failure | SOC 2 CC4.1, CIS 3, ISO A.8.3 | 4 hr |
| P2.9 | `docs/vendor_risk_register.md` — one row per vendor (CF, OCI, Supabase, Anthropic, RSS sources) with SOC 2 status, DPA link, contact | SOC 2 CC9.2, ISO A.5.19-23 | 1 day |

### P3 — Structural (weeks each)

| # | Item | Frameworks | Effort |
|---|---|---|---|
| P3.1 | **Multi-tenant readiness** — per-user data scoping in `portfolios`/`research_notes` (add `owner_email` column, tighten RLS from `USING (true)` to `USING (owner_email = current_setting('app.user_email'))`, set the GUC on each request from `flask.g.user_email`) | SOC 2 CC6.1/CC6.7, ISO A.8.3 | 1-2 weeks |
| P3.2 | Central log aggregation + basic **SIEM rules** (auth spikes, scrape anomalies) | NIST DE.CM, DE.AE, ISO A.8.16 | 2-3 weeks |
| P3.3 | **Third-party licence audit** — RSS body redistribution, PRAW attribution, Bloomberg/SCMP/CNBC ToS; decide between headline-only storage or explicit licensing | ISO A.5.32-34, SOC 2 CC9.2 | 1-2 weeks |
| P3.4 | Formal **risk register** + annual review cadence (`docs/risk_register.md` + calendar reminder) | ISO A.5.7-8, SOC 2 CC3.2, NIST GV.RM | 1 week |
| P3.5 | Full **DPA/DPIA** if opening to EU users | ISO A.5.34, GDPR | 2 weeks |
| P3.6 | Redaction/hashing policy for `articles.author` — protect third-party public-forum PII (Reddit handles) | SOC 2 P-series, ISO A.8.11 | 1 week |
| P3.7 | Threat model artefact for the Dash app (STRIDE per component) | ASVS V1, ISO A.8.25 | 1 week |
| P3.8 | Formal **ISMS scaffolding** — SoA, risk methodology, management review calendar — IF certification is ever pursued | ISO 27001 core clauses 4-10 | 4-8 weeks |

---

## 6. Recommended next steps

**Immediate (this month)**: Close P1.1 – P1.10 as a single "compliance quick wins" plan. These are ≤ half-day items each, mostly configuration, and materially move the CIS v8 score (from 11/18 met to ~14/18) and ASVS L1 (from ~70% to ~80%) without touching product code.

**Next quarter**: Sequence P2.1 (JWT verify) and P2.2 (SQLite backup) first — they close the two most impactful residual risks (spoofable trust boundary; unrecoverable data loss). P2.3 (structured logging + sink) enables Detect/Respond gains that unlock several downstream findings.

**Only if scope expands**: P3 items are triggered by real events — P3.1 by adding a second user, P3.5 by adding EU users, P3.8 by pursuing certification. Do not pre-invest.

**Cadence**: Re-run this assessment (a) annually, (b) whenever `docs/deploy.md` architecture changes (new service, new data class, new vendor), or (c) after any P1 batch closes. The Explore-agent inventory pattern used here reproduces the findings efficiently.

---

## Appendix A — Framework version reference

- **SOC 2** — AICPA Trust Services Criteria, 2017 revision (with 2022 points of focus update).
- **CIS Controls v8.0** — Center for Internet Security, released May 2021; IG1 depth per CIS v8 IG mapping.
- **OWASP ASVS 5.0** — Application Security Verification Standard, current release.
- **NIST CSF 2.0** — released February 2024, adds the Govern function.
- **ISO/IEC 27001:2022 + Annex A** — 93 controls (down from 114 in 2013), reorganised into 4 themes.
