# Troubleshooting

## Port already in use

Edit `.env` and change the host port:

```text
WEBUI_PORT=5174
GATEWAY_PORT=8012
CORE_PORT=8011
QDRANT_HTTP_PORT=6335
TIKA_PORT=9999
```

Then restart:

```bash
./scripts/manage.sh down
./scripts/manage.sh up
```

## Service cannot reach another service

Inside Docker Compose, services must use service names, not localhost:

```text
http://qdrant:6333
http://tika:9998
http://retriva-core:8001
http://retriva-gateway:8002
```

The browser, however, uses host ports:

```text
http://localhost:5173
http://localhost:8002
```

## OpenRouter errors

Check:

```bash
grep OPENAI_PROVIDER_API_KEY .env
./scripts/manage.sh logs retriva-core
```

## Tika image is large

`apache/tika:latest-full` includes OCR-related dependencies. For faster downloads, try:

```text
TIKA_IMAGE=apache/tika:latest
```

## Clean reset

```bash
./scripts/manage.sh purge
./scripts/manage.sh build
./scripts/manage.sh up
```
