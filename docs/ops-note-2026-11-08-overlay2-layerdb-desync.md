# Ops note: Docker overlay2 layerdb desync (2026-11-08)

## Symptom

`ENV_FILE=.env.cust_0007 ./scripts/manage.sh build` failed while pulling the
`python:3.12-slim` base for the `retriva-gateway` target:

```text
failed to register layer: rename
/mnt/devel/docker/image/overlay2/layerdb/tmp/write-set-1781475015
/mnt/devel/docker/image/overlay2/layerdb/sha256/dd57f903...
file exists
```

This is the same failure class as the 2026-10-31 incident (then
`node:20-alpine`), caused by the Docker Engine image cache losing sync with
the on-disk overlay2 layer store after a stale/failed containerd session.

## Diagnosis evidence

- Docker Engine `29.6.1`, containerd `2.2.5`, runc `1.3.6`,
  storage driver `overlay2`, root `/mnt/devel/docker`.
- The Engine reported `Images: 0` and `Containers: 0` while the on-disk
  store still held metadata:
  - `layerdb/sha256`: 253 entries (including the offending `dd57f903...`);
  - `imagedb/content/sha256`: 68 entries;
  - `containers`: 50 entries; overlay2 data: 129G.
- `repositories.json` retained only the Qdrant tag; a subsequent
  `GET /images/qdrant/qdrant:latest/json` returned `layer does not exist`.
- `dockerd` journal immediately before the build:
  - `healthcheck failed ... "only one connection allowed"`
  - `healthcheck failed fatally: session healthcheck failed fatally:
    Unavailable: ... only one connection allowed`
  - then four `failed to register layer ... file exists` errors for
    `dd57f90311a2567b55ff22eeacff1602fd04ad7566e315957731359eab173b90`.

## Recovery applied

Neither `/mnt/devel/docker` content nor `docker system prune` was touched.
The daemons were cycled in this order:

```bash
sudo systemctl stop docker
sudo systemctl stop containerd
sleep 3
sudo systemctl start containerd
sleep 3
sudo systemctl start docker
```

After the restart the Engine reloaded the on-disk store: 68 images and all
previously running containers reappeared; `docker pull python:3.12-slim`
completed and the full flow passed:

```bash
ENV_FILE=.env.cust_0007 ./scripts/manage.sh down
ENV_FILE=.env.cust_0007 ./scripts/manage.sh build
ENV_FILE=.env.cust_0007 ./scripts/manage.sh db-up
ENV_FILE=.env.cust_0007 ./scripts/manage.sh up-pro
ENV_FILE=.env.cust_0007 ./scripts/manage.sh logs --no-follow
```

Image tags lost from `repositories.json` were recreated by the rebuild
(the local `retriva-*` tags and the third-party base tags were pulled or
rebuilt once; no data volumes were affected).

## Durable fix applied

The Docker packages were upgraded from the Docker apt repository:

| Component | Before | After |
| --- | --- | --- |
| docker-ce / docker-ce-cli | 29.6.1 | 29.9.3 |
| containerd.io | 2.2.5 | 2.4.2 |
| runc | 1.3.6 | 1.5.2 |
| docker-buildx-plugin | 0.35.0 | 0.38.1 |
| docker-compose-plugin | 5.3.1 | 5.7.0 |

A cached build after the upgrade succeeded with zero layer-registration
errors, and Engine/on-disk counts matched again (84 images = 84 imagedb
entries, 50 containers = 50 container dirs, empty `layerdb/tmp`).

## Residual risk

The containerd session healthcheck signature
`only one connection allowed` / `session healthcheck failed fatally` was
still observed occasionally after the upgrade to Docker `29.9.3` and
containerd `2.4.2` (for example during a long build). It did not cause a
desync or layer-registration failure in this run, but the underlying
session flapping is not proven fixed by the upgrade.

Operational rules until confirmed otherwise:

1. Keep the recovery order above for this signature; do not delete, move,
   or prune files under `/mnt/devel/docker`.
2. Re-check `docker images -aq | wc -l` against
   `layerdb`/`imagedb` counts after any future layer-registration error.
3. Prefer restarting both daemons over rebuilding on top of a desynced
   store, to avoid compounding stale metadata.
