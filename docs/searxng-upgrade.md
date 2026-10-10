# SearXNG upgrade & rollback procedure

Applies to the `retriva-searxng` service defined in `docker-compose.yml`
(image pinned via `SEARXNG_IMAGE`, default
`searxng/searxng:2026.9.11-61d660276`). Never track `latest`.

## Upgrade steps

1. **Review release/migration notes** for the target version
   (https://docs.searxng.org — check `settings.yml` schema changes, engine
   definition changes, limiter/Valkey changes).
2. **Update the pin** — either edit the default in `docker-compose.yml`
   (`SEARXNG_IMAGE`) or set `SEARXNG_IMAGE=searxng/searxng:<new-tag>` in the
   env file. Prefer a date-hash tag; record the digest for production:
   `docker buildx imagetools inspect searxng/searxng:<tag>`.
3. **Pull the candidate image** without touching the running service:
   ```bash
   docker pull searxng/searxng:<new-tag>
   ```
4. **Start the candidate** (recreates the container with the new image):
   ```bash
   ENV_FILE=.env.cust_0007 ./scripts/manage.sh up-pro
   ```
5. **Inspect the effective `/config`** from a trusted internal container:
   ```bash
   docker exec retriva-worker python -c \
     "import urllib.request,json; print(json.dumps(json.loads(urllib.request.urlopen('http://retriva-searxng:8080/config').read()), indent=1))" \
     | python3 -c "import json,sys; cfg=json.load(sys.stdin); \
       print([e['name'] for e in cfg['engines'] if e.get('enabled') and 'general' in (e.get('categories') or [])])"
   ```
   The enabled general engines must be exactly: `brave`, `duckduckgo`,
   `google cse`, `wikidata`, `wikipedia`.
6. **Verify JSON search**: `GET /search?q=test&format=json` returns 200 with
   `application/json` and a `results` list.
7. **Run provider tests**: `cd retriva-web-research && python -m pytest tests/ -q`
   (65 tests; deterministic, no live engines).
8. **Run the fixed smoke-query set** (see `docs/searxng.md`): generic,
   exact-org, org+domain, org+product, org+techdoc, Italian, English.
9. **Compare upstream failures and evidence yield** against the recorded
   baseline in this document (below). A new version that materially
   increases CAPTCHA/timeout rates or drops evidence yield is a rollback
   candidate.
10. **Run one CRM qualification** end-to-end through the chat tool path.
11. **Confirm no secret leakage**: `docker logs retriva-searxng | grep -c
    <secret>` must be 0; readiness output must not contain it.
12. **Promote or roll back.**

## Rollback

```bash
# Re-pin to the previous tag (example: rollback to 2026.9.11-61d660276)
SEARXNG_IMAGE=searxng/searxng:2026.9.11-61d660276 \
  ENV_FILE=.env.cust_0007 ./scripts/manage.sh up-pro
```

Then repeat steps 5–6 (effective config + JSON search). The settings mount
(`config/searxng/settings.yml`) is version-controlled; if the upgrade
required settings changes, revert them with `git checkout` before rollback.

## Recorded baselines

| Date | Image | Notes |
|---|---|---|
| 2026-09-13 | `2026.9.11-61d660276` | Initial operational baseline. Engines: brave/duckduckgo/google cse/wikidata/wikipedia (general). wikipedia+wikidata timeout raised 3→6 s (measured). duckduckgo CAPTCHA + brave/google-cse 429 observed under burst from datacenter IP; recovery within ~3–5 min cooldown. |