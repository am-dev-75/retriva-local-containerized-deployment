# Creditsafe activation and rollback runbook (Spec 037)

Status: **planning only — Creditsafe is DISABLED and NOT activated.**
This runbook describes how to promote the signed-off Spec 037 work and,
under separate future authorizations, how to validate against the live
sandbox and activate in production. Nothing in this document has been
executed against a live provider, production, or real credentials.

Cross-references:

- Accepted pack: `retriva-crm-assistant/specs/037-creditsafe-financial-score-gate/`
  (spec, tasks, acceptance §B/§C/§D).
- Operator/feature documentation:
  `retriva-crm-assistant/docs/creditsafe-financial-score.md`.
- Phase 5 evidence: acceptance record §B (commits `9f2a476`, `42885b5`,
  `8d2272c`, `f7320d5`, `7356a66`, `fb204b7`) and owner sign-off
  `da736da`.
- Deployment commit: `48c478f` on branch `crm-assistant` (placeholders +
  acceptance harness `scripts/spec037_acceptance_harness.py`).

## 1. Promotion via pull request

The deployment repository's `main` (release line, currently `ce219cb`
"Version 1.10.1.") and the `crm-assistant` feature branch (40 commits,
`48c478f` + this documentation commit) have **diverged**; a
fast-forward merge is not possible. Direct merges to `main` are not
performed here.

- PR: `integrate/spec037-creditsafe-deployment` → `main` in
  `retriva-local-containerized-deployment` (prepared integration branch:
  normal merge of `crm-assistant` onto the current `main` line with
  conflicts resolved to retain both sides; both histories and all
  signed-off hashes remain intact).  **Merged 2026-10-10 as PR #1**
  (normal merge commit `f852c49`, parents `ce219cb` + `8f86eaa`);
  deployment support is now on `main`.  The merge does not activate
  Creditsafe — this runbook's activation stages remain separately
  gated.
- Suggested title:
  `Spec 037: Creditsafe deployment placeholders, acceptance harness, and activation runbook`
- Suggested description: see §12 of this document.
- Conflict risk: `main`-unique commits touch `docker-compose.yml`,
  `.env.example`, `README.md` (retrieval rerank / determinism env
  additions). The Spec 037 additions use different anchor points, so an
  automatic merge is likely, but reviewers must confirm; any conflict is
  resolved inside the PR only, with the sign-off owner reviewing that
  `CRM_CREDITSAFE_*` defaults remain exactly as documented in §2.
- The branch contains the full CRM Assistant line plus monitoring fixes;
  the PR is the canonical promotion path for the branch.

Until the PR is merged, the deployment defaults shipped on `main` do
not include the `CRM_CREDITSAFE_*` passthrough (the application itself
still defaults to disabled/zero even without the compose entries).

## 2. Safe defaults (must hold before and after any merge)

Rendered from the committed Compose file (verified via
`scripts/check_creditsafe_defaults.sh`):

| Setting | Default |
|---|---|
| `CRM_CREDITSAFE_ENABLED` | `false` |
| `CRM_CREDITSAFE_ENVIRONMENT` | `sandbox` |
| `CRM_CREDITSAFE_USERNAME` / `CRM_CREDITSAFE_PASSWORD` | empty |
| `CRM_CREDITSAFE_BUDGET_COMPANY_SEARCH` | `0` |
| `CRM_CREDITSAFE_BUDGET_CREDIT_REPORT` | `0` |
| `CRM_CREDITSAFE_FRESHNESS_DAYS` | `90` |
| `CRM_CREDITSAFE_ALLOW_NAME_ONLY_SEARCH` | `false` |
| `CRM_CREDITSAFE_TIMEOUT_SECONDS` / `CRM_CREDITSAFE_MAX_RETRIES` | `15` / `2` |

All three pro extension hosts (`retriva-ingestion`, `retriva-core`,
`retriva-worker`) receive these variables. Merge must not change any of
these defaults; a PR that does is rejected.

Operational notes:

- The acceptance harness `scripts/spec037_acceptance_harness.py` is a
  standalone operator-run script. It is **not** referenced by Compose,
  entrypoints, cron, or startup; nothing can run it automatically.
- `httpx` logging must remain at `WARNING` (deployment default) before
  activation: `INFO`/`DEBUG` would log request URLs containing company
  identifiers. Verify with
  `docker exec <core> python3 -c "import logging;print(logging.getLogger('httpx').getEffectiveLevel())"`
  (expect `30`).

## 3. Credential and subscription readiness checklist (owner/operator)

Owner/provider confirmation required for every line; do not infer
entitlement from documentation.

- [ ] Creditsafe account exists with a **Connect API** subscription
      entitlement covering the intended product (company search +
      credit report).
- [ ] Company search and credit reports are entitled for every intended
      country of operation.
- [ ] Sandbox username/password available (separate from production).
- [ ] Production username/password available (later, separate step).
- [ ] Approved secret-storage location chosen (deployment git-ignored
      env file or stronger secret store) and injection method recorded.
- [ ] Rotation ownership + process + schedule defined.
- [ ] Confirmation that secrets never enter Git, issues, chat, logs,
      shell history, or screenshots.
- [ ] Environment-specific secret separation (sandbox vs production
      credentials never cross-used; the application enforces this).
- [ ] Provider-side restrictions (allowed source IPs, TLS, rate limits)
      confirmed if applicable.
- [ ] Contractual/licensing approval for score use, retention, and
      display confirmed.
- [ ] Data-protection/retention review completed for storing score
      observations and snapshots.
- [ ] Named technical owner and operational owner recorded.

## 4. Bounded live-sandbox validation plan (NOT executed; separate authorization)

Preconditions (all mandatory before execution):

- separate, written owner authorization referencing this plan;
- owner-supplied **sandbox** credentials through the approved secret
  channel (agent must not read/handle them);
- confirmed sandbox subscription/entitlements;
- approved synthetic or provider-authorized test-company identifiers;
- no real customer data unless separately approved;
- dedicated isolated environment (own compose project/volumes, no
  production network), production credentials absent;
- `CRM_CREDITSAFE_ENVIRONMENT=sandbox` explicitly selected.

Call bounds (hard caps; the operator confirms quota/cost impact with the
provider before execution — no cost estimates are made here):

- authentication attempts: ≤ 3 (single-flight + one 401 refresh path);
- company-search logical calls: ≤ 4;
- credit-report logical calls: ≤ 2;
- retries: ≤ 2 per logical call (application bound);
- total test duration: ≤ 60 minutes wall clock.

These bounds validate, in the smallest call count: authentication,
legal-entity resolution, one credit report, documented score
path/schema, score normalization (exact decimals), error sanitization,
readiness, and bounded metrics.

Stop immediately on: unexpected environment/host; credential rejection
beyond the bounded auth retry; permission/subscription failure;
unexpected country/schema variant; absent documented provider scale
fields; identity ambiguity; out-of-range/contradictory score fields;
potential secret leakage; request count above cap; any unexpected
production-endpoint traffic.

Retain as evidence: timestamps; endpoint categories (not sensitive
URLs); HTTP status classes; sanitized correlation ids; selected schema
mapping identifier; normalized score only if approved; request counts;
readiness payload; bounded metric deltas; pass/fail per item. Never
retain JWTs, credentials, full response bodies, raw reports, or
sensitive company details.

## 5. Production rollout plan (schema-first, disabled-first; PLAN ONLY)

### Stage 0 — Change approval
Entry: signed live-sandbox acceptance report; security/privacy/
licensing approvals; confirmed credentials and entitlement; named
rollback owner; a maintenance/low-traffic window booked for the V010
index replacement.
Exit: written go decision for Stage 1. Responsible: release owner.

### Stage 1 — Code and schema deployed, feature disabled
Entry: Stage 0 exit.
Actions: deploy images containing the signed-off code; apply
migrations through the canonical migration service **before** new code
paths are used (§6); keep `CRM_CREDITSAFE_ENABLED=false` and budgets
`0`.
Checks: health (`/api/v2/crm/health`), readiness
(`/api/v2/crm/intelligence/creditsafe/readiness` shows
`enabled=false`), migration `verify`, no outbound Creditsafe traffic in
logs/network.
Exit: healthy, disabled, zero calls. Rollback trigger: any health
regression → Level 1/2 (§7). Responsible: release owner/operator.
Evidence: rendered env (redacted), ledger output, verify output, log
scan.

### Stage 2 — Sandbox activation in a non-production environment
Entry: Stage 1 exit; separate authorization.
Actions: set sandbox credentials; `CRM_CREDITSAFE_ENVIRONMENT=sandbox`;
minimal positive budgets (e.g. search 2, report 1 — operator confirms
quota impact); restricted test tenant/company; trigger bounded
enrichment; observe auth/search/report/persistence/qualification/
reporting/approval behavior.
Exit: acceptance items pass; return to disabled + zero budgets unless
continuous sandbox operation is separately approved.
Rollback trigger: any stop condition from §4. Responsible: release
owner. Evidence: §4 evidence set.

### Stage 3 — Production dark launch (PLAN ONLY)
Entry: Stage 2 acceptance; production credentials available from
approved storage; environment explicitly `production`.
Note: the architecture does **not** scope activation per tenant (the
enabled flag and budgets are process-wide settings per service);
activation is environment-wide, so a low-traffic window and
environment-wide change control are required.
Actions: inject production credentials; enable with minimal budgets;
name-only search remains off; verify readiness before triggering
enrichment; trigger one owner-approved synthetic/internal test entity if
legally and operationally permitted.
Exit: one successful, sanitized end-to-end run with bounded metrics.
Rollback trigger: stop conditions as §4 plus any approval gate failure.
Responsible: release owner + operational owner.

### Stage 4 — Controlled production activation (PLAN ONLY)
Entry: Stage 3 exit.
Actions: raise budgets gradually only after observed success; define
daily limits and alert thresholds (§8); keep the operational disable
switch tested; define monitoring duration and change owner.
Exit: steady-state operation accepted by the owner.
Rollback trigger: alert thresholds (§8) → Level 1 disable first.
Responsible: operational owner.

## 6. Migration execution plan (documented; not applied to production)

Canonical, transactional path (never raw ad hoc `psql` DDL):

1. Core/platform first: `retriva-pg-bootstrap` (roles, per cluster) and
   `retriva-pg-migrate` (`python -m retriva.infrastructure.postgres.migrate upgrade`).
2. CRM roles: `retriva-pg-crm-bootstrap`.
3. CRM stream: `retriva-pg-crm-migrate`
   (`python -m retriva_crm_assistant.postgres.migrate upgrade`), which
   applies pending versions in one transaction per migration and
   records the ledger row.
4. Verify: `…migrate verify` (RLS/role/constraint invariants) and
   `…migrate status`.

Expected state: `platform.schema_migrations` stream `pro.crm` at
version **10**; V010 checksum
`3758ec50dbae8a3be3b50a5246923c556de5b650f997d545b3bbcae98d7aa8e3`
(algorithm `sha256(up + NUL + down)`; verified against the signed-off
record). No migration above V010 exists.

Lock behavior: V010 drops/re-adds the identifier CHECK and replaces
`uq_org_ident_registry_active` with a plain (non-concurrent) unique
index inside the migration transaction, taking a brief
`ACCESS EXCLUSIVE` lock on `business.organization_identifiers`.
Schedule the low-traffic window in Stage 0; expect sub-second work for
typical table sizes, monitor lock waits.

Post-migration checks: three Spec 037 tables exist with RLS
ENABLED+FORCED and `tenant_isolation` policies; identifier CHECK
includes `CREDITSAFE_CONNECT_ID`; registry index predicate includes it;
threshold rows seeded `0.6` v1 for existing tenants.

Backward compatibility: old (pre-Spec-037) application images do not
use the new objects; V010 is additive, so Stage 1 may deploy schema and
code in either order after migrations complete.

## 7. Rollback runbook (accepted order of preference)

### Level 1 — Operational disable (fastest; no code/schema change)
- Set `CRM_CREDITSAFE_ENABLED=false`, both budgets `0`, keep
  credentials in place or remove them.
- Recreate only the affected services (`docker compose up -d
  retriva-ingestion retriva-core retriva-worker`).
- Verify: readiness `enabled=false`; enrichment stage records
  `SKIPPED_DISABLED`; no outbound Creditsafe traffic in logs.
- Data: V010 data and reports are preserved untouched.
- Trigger: any Stage 1–4 alert or stop condition. Owner: operator.

### Level 2 — Code rollback
- Redeploy the last known good pre-Spec-037 images.
- Leave V010 in place (additive; old code ignores it).
- Verify old services healthy; preserve data for recovery.
- Owner: release owner. Trigger: defects in new code not manageable by
  Level 1.

### Level 3 — Destructive database downgrade (DOCUMENTED ONLY; separate authorization)
- Requires separate explicit authorization. Stop writers first.
- Export/retain data per policy.
- Detect active `CREDITSAFE_CONNECT_ID` rows. The documented downgrade
  (`downgrade --to 9 --confirm-destructive`) **refuses** while such rows
  exist (phase 5 rehearsal confirmed even soft-deleted rows refuse,
  because the restored pre-V010 CHECK is unconditional).
- Approved decision required: remove or transform those identifier
  rows (they are business identifiers; removal is irreversible).
- After downgrade to V009: prior CHECK and active-registry index are
  restored exactly; `qualification.crm_settings` and all score
  observation/state history are **irreversibly lost** (documented loss).
- Re-upgrade returns to V010 with fresh default thresholds; owner must
  re-approve re-activation.

## 8. Observability, metrics, logging, alerts

Readiness/health:

- `GET /api/v2/crm/health` — extension health.
- `GET /api/v2/crm/intelligence/creditsafe/readiness` (VIEW_INTELLIGENCE)
  — `enabled`, `environment`, `config_complete`,
  `allow_name_only_search`, last outcome categories (nullable),
  `budget_exhausted`, `budget_remaining` per category.

Bounded metrics (exact committed names/labels from
`retriva_crm_assistant/intelligence_metrics.py`):

| Metric | Labels |
|---|---|
| `creditsafe_call_total` | authenticate, company_search, credit_report |
| `creditsafe_auth_total` | attempt, success, failure, cache_hit, cache_miss |
| `creditsafe_resolution_total` | reused_verified, resolved, not_found, ambiguous, identity_insufficient, identity_mismatch |
| `creditsafe_credit_report_total` | available, disabled, config_missing, unresolved, auth_failed, permission_denied, identity_insufficient, company_not_found, ambiguous_match, report_unavailable, score_section_failed, unsupported_schema, missing_score, invalid_score, rate_limited, budget_exhausted, provider_error |
| `creditsafe_score_extraction_total` | available, report_unavailable, score_section_failed, unsupported_schema, missing_score, invalid_score |
| `creditsafe_retry_total` | attempt, exhausted |
| `creditsafe_rate_limited_total` | total |
| `creditsafe_budget_total` | company_search_reserved/-_exhausted, credit_report_reserved/-_exhausted |
| `creditsafe_request_ms_total` | authenticate, company_search, credit_report |
| `creditsafe_enrichment_total` | SKIPPED_DISABLED, SKIPPED_CONFIG_MISSING, SKIPPED_IDENTITY_INSUFFICIENT, SKIPPED_BUDGET_EXHAUSTED, SKIPPED_CANCELLED, DEFERRED_LOCK_CONTENTION, REUSED, REFRESHED, UNAVAILABLE, IDENTITY_CONFLICT |
| `creditsafe_enrichment_duration_ms_total` | total |
| `creditsafe_freshness_total` | hit, stale, forced |
| `creditsafe_lock_total` | acquired, contended, timeout, cancelled |
| `creditsafe_persistence_total` | success, failure |
| `financial_gate_total` | pass, fail, unknown |
| `financial_gate_unknown_reason` | no_observation, stale, unavailable, invalid |
| `financial_approval_total` | allowed, blocked |
| `financial_settings_total` | read, update, version_conflict, unauthorized, invalid |
| `legacy_financial_gate_total` | reevaluation_required, reevaluated |

Unknown labels collapse to `other`/`total` (bounded cardinality; no
company names, identifiers, Connect IDs, tokens, URLs, scores, or
thresholds).

Proposed alert conditions (documentation only; no alert rules are added
in this step):

- repeated auth failures (`creditsafe_auth_total/failure` rate);
- permission/subscription failures (`creditsafe_credit_report_total/
  permission_denied` > 0);
- rate limiting (`creditsafe_rate_limited_total` growth);
- provider error rate (`…/provider_error` share);
- budget exhaustion (`creditsafe_budget_total/*_exhausted` > 0);
- unsupported schema (`…/unsupported_schema` > 0);
- identity ambiguity/conflict (`…/ambiguous_match`,
  `creditsafe_enrichment_total/IDENTITY_CONFLICT` > 0);
- persistence failures (`creditsafe_persistence_total/failure` > 0);
- lock contention (`creditsafe_lock_total/contended|timeout` growth);
- high unknown-gate rate (`financial_gate_total/unknown` share);
- secret/token leakage indicators: any credential-shaped string in
  logs is a page-worthy incident;
- unexpected outbound traffic while disabled (`enabled=false` and any
  `creditsafe_call_total` increase).

Logging rules: `httpx` logger stays at `WARNING`; safe correlation
fields only (correlation id, category, status class); forbidden fields:
credentials, tokens, Authorization headers, endpoint URLs with
identifiers, raw payloads, company names/identifiers, Connect IDs,
scores, thresholds.

## 9. Security and compliance readiness checklist

- [ ] Secret management: credentials only from approved secret storage;
      never in Git/issues/chat/logs/history/screenshots.
- [ ] Least privilege: dedicated provider account; operator access
      limited; `MANAGE_CRM_SETTINGS` restricted to CRM administrators.
- [ ] RLS: `tenant_isolation` policies ENABLED+FORCED verified after
      migration (blocking item for activation).
- [ ] Tenant isolation: cross-tenant read/update attempts denied
      (Phase 5 proofs); re-verify at activation.
- [ ] Audit logs: threshold updates and approvals audited (actor,
      tenant, old/new, timestamp, version).
- [ ] Data minimization: only normalized score values + bounded
      provenance are stored; no raw reports.
- [ ] Report immutability: stored snapshots never re-read settings or
      scores (Phase 5 proofs).
- [ ] Provider-data licensing: use/retention/display approved by owner/
      legal.
- [ ] Retention/deletion policy defined for score observations and
      snapshots.
- [ ] Incident response for credential leakage documented.
- [ ] Operator access matrix defined.
- [ ] Synthetic vs real test data separated; sandbox validation uses
      approved synthetic/provider-authorized entities only.
- [ ] Prohibited information check for logs/metrics/UI automated (Phase
      5 scans re-run at activation).

Items requiring legal/procurement/security/data-protection approval:
subscription/licensing, data-protection and retention review, provider
contract terms, incident-response sign-off.

## 10. Cost/budget semantics (operator facts)

- Budgets are **process-local counters per service process**. With N
  replicas, the effective paid-call allowance is up to N × the
  configured limit per restart cycle; restarting a service resets its
  counters. Keep limits minimal; the readiness endpoint reports the
  per-process remaining values.
- Consuming actions: one logical company search per resolution attempt
  (fallback strategies may consume more than one search; see the
  feature doc), one logical credit report per fetch. Retries and the
  single 401 re-authentication retry within one logical call do not
  consume additional units.
- Non-consuming actions: authentication, readiness, reuse of fresh
  observations, qualification/report/approval paths, legacy
  re-evaluation (never triggers enrichment).
- Immediate disable without code/schema rollback: set
  `CRM_CREDITSAFE_ENABLED=false` and budgets `0`, recreate the three
  pro services; the enrichment stage then reports `SKIPPED_DISABLED`.

## 11. Static validation helper

`scripts/check_creditsafe_defaults.sh` renders the committed Compose
configuration with a minimal synthetic env file (never committed, no
containers started) and fails if any of the §2 defaults deviate. Run it
before/after any merge or activation change.

## 12. Suggested PR description (crm-assistant → main)

> **Spec 037: Creditsafe deployment placeholders, acceptance harness,
> and activation runbook**
>
> Adds explicit `CRM_CREDITSAFE_*` env passthroughs (safe defaults:
> disabled, sandbox, zero budgets, 90-day freshness, name-only search
> off, no credentials) to the three pro extension hosts, documented
> placeholders in `.env.example`, the isolated acceptance harness used
> by the signed-off Phase 5 record, and the activation/rollback runbook.
>
> Evidence: Spec 037 Phase 5 isolated deployed acceptance (acceptance
> record §B/§C/§D, owner sign-off `da736da` in retriva-crm-assistant);
> deployment commit `48c478f`; static defaults check
> `scripts/check_creditsafe_defaults.sh`; focused Spec 037 suites green.
> No activation, no credentials, no live calls. Conflict risk:
> `docker-compose.yml` / `.env.example` / `README.md` also changed on
> main (rerank/determinism) — verify defaults unchanged during merge.
