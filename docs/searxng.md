# Internal SearXNG deployment (ADR-014)

SearXNG is the default production **search/URL-discovery** provider for
Retriva Web Research. It replaces the Tavily MCP server as the mandatory
search dependency; no external API key is required.

## What SearXNG does and does not do

SearXNG is a **metasearch engine**: it queries configured upstream engines
(DuckDuckGo, Brave, Wikipedia, Wikidata, OpenStreetMap) and aggregates their
results. It is **not** an independent search index. It remains subject to
upstream engines' rate limits, CAPTCHAs, failures, and result quality.

Its role is strictly limited to **URL discovery**:

```
SearXNG result (title/url/snippet)
    -> URL policy + deduplication (SearxngSearchProvider)
    -> ControlledHttpProvider (full source retrieval, SSRF-safe)
    -> Retriva parsers
    -> normalized PublicEvidence
```

Search snippets are never cited as authoritative evidence; snippet-only
fallback evidence is explicitly marked secondary/weak.

## Configuration

- Compose service: `retriva-searxng` (pro profile, `docker-compose.yml`).
- Image: `searxng/searxng:2025.6.19-93f66bfb4` (pinned; override with
  `SEARXNG_IMAGE`).
- Settings: `config/searxng/settings.yml` (mounted read-only at
  `/etc/searxng/settings.yml`). JSON output (`search.formats: [html, json]`)
  is **required** — readiness treats a JSON-disabled instance as not ready.
- Secret: `SEARXNG_SECRET` from the env file. Generate at install time:
  `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
- Network: internal `retriva-net` only — **no published host port**.
- Healthcheck: `GET /healthz` inside the container.
- Upstream engines: a conservative, administrator-controlled set is enabled
  in `settings.yml` (DuckDuckGo, Brave, Wikipedia, Wikidata, OpenStreetMap;
  Google and Bing are disabled due to bot-detection/CAPTCHA frequency for
  datacenter IPs). Adjust there — never per-request from the application.

## Web Research configuration (CRM env vars)

```
CRM_PUBLIC_RESEARCH_PROVIDER=searxng
CRM_SEARXNG_URL=http://retriva-searxng:8080
```

Rate limiting is applied Retriva-side (`CRM_SEARXNG_MAX_CONCURRENT`,
`CRM_SEARXNG_MIN_INTERVAL_MS`); the SearXNG limiter is disabled because the
instance is internal-only and its limiter would add a Valkey dependency
(see ADR-014 for the reasoning and revisit triggers).

## Search budgets, circuit breaker, and degradation control (2026-09-13)

Upstream engines flag the egress IP for **hours** (not the 180 s SearXNG
suspension) after a query burst. The following controls bound the damage:

- **Rolling search budgets** (`ResearchLimits` in retriva-web-research):
  3 queries/subject, 150/job, 10/rolling-minute, 120/rolling-hour.
  Exhaustion returns explicit structured warnings and marks the result
  partial — searches are never silently skipped.
- **Per-engine circuit breaker** (`SearxngSearchProvider`): 3 consecutive
  captcha/rate-limit failures → OPEN (900 s cooldown) → one bounded
  HALF_OPEN probe → CLOSED on success / OPEN again on failure. Timeouts
  never open the breaker. State via `get_diagnostics()`.
- **Capability levels** in readiness: `NONE` < `IDENTITY_ONLY`
  (wikipedia/wikidata only) < `GENERAL_WEB` < `FULL_PUBLIC_RESEARCH_AVAILABLE`.
  CRM qualification requires `GENERAL_WEB` or better; identity-level
  evidence alone cannot qualify prospects.
- **Known-domain direct path**: when a candidate/reference organization has
  a credible domain, the official site is fetched directly (0 search
  queries, cached, single fetch — not a crawler) before any broad search.
- **Bounded resource cache**: 512 entries, 300 s TTL (60 s negative),
  tenant-scoped keys that never contain candidate or session IDs.

Known upstream behavior from measurement: wikipedia only answers
single-token queries (opensearch title matching), so IDENTITY_ONLY degraded
mode yields identity-level evidence only.

## Start / verify

```bash
ENV_FILE=.env.cust_0007 ./scripts/manage.sh build retriva-searxng
ENV_FILE=.env.cust_0007 ./scripts/manage.sh up-pro
# JSON API check (from inside the network):
docker exec retriva-worker python -c \
  "import urllib.request; print(urllib.request.urlopen('http://retriva-searxng:8080/search?q=test&format=json', timeout=20).status)"
# Readiness:
curl -s http://localhost:${GATEWAY_PORT:-8002}/api/v2/crm/readiness | python3 -m json.tool
```

Readiness states reported for SearXNG: `unreachable`, `json_disabled`,
`search_failed`, `all_engines_failed`, `partial`, `ok` — only `ok`/`partial`
count as production-capable, and qualification requiring public research is
refused otherwise (never silently falling back to mock providers).
