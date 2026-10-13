# Part 6 — Docker Compose in Depth

Running a real application with `docker run` means remembering dozens of flags across several containers. **Docker Compose** describes a multi-container application declaratively in YAML and manages it as one unit. It is the standard tool for local development environments, integration tests in CI, and small single-host deployments.

This course uses **Compose v2**: the `docker compose` CLI plugin (written in Go), following the **Compose Specification**. The old Python `docker-compose` v1 is end-of-life, and the top-level `version:` key is obsolete; Compose ignores it and prints a warning.

## 6.1 Core Concepts

- **Project**: the application, named after the directory by default (override with `-p` or the top-level `name:`). Every resource is prefixed or labeled with the project name.
- **Service**: a container definition (image, config, scale). One service can run several replicas.
- **Networks, volumes, configs, secrets**: top-level resources the services use.

Compose looks for `compose.yaml` (preferred), `compose.yml`, `docker-compose.yaml` or `docker-compose.yml` in the current directory.

By default Compose creates a network called `<project>_default` and attaches every service to it, so services reach each other by **service name** via Docker's embedded DNS.

## 6.2 A Complete Example

```yaml
# compose.yaml
name: shop

services:
  web:
    build:
      context: ./web
      target: runtime
    image: ghcr.io/acme/shop-web:${TAG:-dev}
    ports:
      - "127.0.0.1:8080:8080"
    environment:
      API_URL: http://api:8000
    depends_on:
      api:
        condition: service_healthy
    networks: [frontend]

  api:
    build:
      context: ./api
      args:
        PYTHON_VERSION: "3.12"
    image: ghcr.io/acme/shop-api:${TAG:-dev}
    env_file:
      - ./api/.env
    environment:
      DATABASE_URL: postgresql://shop:${DB_PASSWORD:?DB_PASSWORD must be set}@db:5432/shop
      REDIS_URL: redis://cache:6379/0
    depends_on:
      db:
        condition: service_healthy
      cache:
        condition: service_started
      migrate:
        condition: service_completed_successfully
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"]
      interval: 10s
      timeout: 3s
      retries: 5
      start_period: 15s
    deploy:
      resources:
        limits:
          cpus: "1.0"
          memory: 512M
    restart: unless-stopped
    networks: [frontend, backend]

  migrate:
    image: ghcr.io/acme/shop-api:${TAG:-dev}
    command: ["python", "-m", "shop.migrate"]
    environment:
      DATABASE_URL: postgresql://shop:${DB_PASSWORD}@db:5432/shop
    depends_on:
      db:
        condition: service_healthy
    restart: "no"
    networks: [backend]

  db:
    image: postgres:16.4-alpine
    environment:
      POSTGRES_USER: shop
      POSTGRES_DB: shop
      POSTGRES_PASSWORD_FILE: /run/secrets/db_password
    secrets:
      - db_password
    volumes:
      - dbdata:/var/lib/postgresql/data
      - ./db/init:/docker-entrypoint-initdb.d:ro
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U shop -d shop"]
      interval: 5s
      timeout: 3s
      retries: 10
    networks: [backend]

  cache:
    image: redis:7.4-alpine
    command: ["redis-server", "--appendonly", "yes"]
    volumes:
      - cachedata:/data
    networks: [backend]

  adminer:
    image: adminer:4
    ports: ["127.0.0.1:8081:8080"]
    profiles: [debug]
    networks: [backend]

networks:
  frontend:
  backend:
    internal: true

volumes:
  dbdata:
  cachedata:

secrets:
  db_password:
    file: ./secrets/db_password.txt
```

Bring it up:

```bash
echo "s3cr3t" > secrets/db_password.txt
export DB_PASSWORD=s3cr3t
docker compose up -d --build
docker compose ps
```

```text
NAME             IMAGE                           SERVICE   STATUS                    PORTS
shop-api-1       ghcr.io/acme/shop-api:dev       api       Up 20 seconds (healthy)
shop-cache-1     redis:7.4-alpine                cache     Up 32 seconds             6379/tcp
shop-db-1        postgres:16.4-alpine            db        Up 32 seconds (healthy)   5432/tcp
shop-migrate-1   ghcr.io/acme/shop-api:dev       migrate   Exited (0) 22 seconds ago
shop-web-1       ghcr.io/acme/shop-web:dev       web       Up 8 seconds              127.0.0.1:8080->8080/tcp
```

Note the `backend` network is `internal: true`, and the `api` is on both networks. Since `api` must reach the internet (for example, a payment API), it gets egress via `frontend`.

## 6.3 Service Configuration Reference

### 6.3.1 build

```yaml
build:
  context: ./api
  dockerfile: Dockerfile.prod      # relative to context
  target: runtime
  args:
    APP_VERSION: ${APP_VERSION:-dev}
  secrets: [npmrc]
  ssh: [default]
  cache_from: ["type=registry,ref=ghcr.io/acme/api:buildcache"]
  cache_to: ["type=registry,ref=ghcr.io/acme/api:buildcache,mode=max"]
  platforms: ["linux/amd64", "linux/arm64"]
  labels:
    org.opencontainers.image.source: https://github.com/acme/shop
```

If both `build` and `image` are set, Compose builds and tags the result with `image`, so `docker compose push` can publish it.

### 6.3.2 depends_on conditions

Plain `depends_on: [db]` only controls **start order**; it does not wait for the database to be ready. Use the long form:

| Condition | Waits until |
|---|---|
| `service_started` | The dependency container has started (default). |
| `service_healthy` | The dependency's healthcheck reports `healthy`. |
| `service_completed_successfully` | The dependency ran to completion with exit code 0 (migrations, seeders). |

Additional keys: `restart: true` (restart this service when the dependency is restarted by a Compose operation) and `required: false` (do not fail if the dependency is not enabled, e.g. disabled by a profile).

> **Note:** `depends_on` only applies when Compose starts things. If the DB dies later, your app must still handle reconnects. Readiness gating at startup is not a substitute for retry logic.

### 6.3.3 healthcheck

```yaml
healthcheck:
  test: ["CMD", "curl", "-fsS", "http://localhost:8080/healthz"]
  interval: 30s
  timeout: 5s
  retries: 3
  start_period: 30s
  start_interval: 2s
```

`CMD` runs directly (exec), `CMD-SHELL` runs via `/bin/sh -c`. `disable: true` turns off an image-defined healthcheck. The tool you call (`curl`, `wget`) must exist in the image; for distroless images, ship a tiny health binary or build the check into the app.

### 6.3.4 Environment variables and interpolation

Two separate mechanisms that beginners confuse:

1. **Interpolation**: Compose substitutes `${VAR}` in the YAML itself, using the shell environment and the project `.env` file. This happens on the client, before anything is sent to Docker.
2. **Container environment**: `environment:` and `env_file:` set variables inside the container.

```yaml
services:
  api:
    image: "acme/api:${TAG:-latest}"     # interpolation
    env_file:
      - path: ./api/.env                 # container env from file
        required: true
      - path: ./api/.env.local
        required: false
    environment:
      LOG_LEVEL: ${LOG_LEVEL:-info}      # interpolation feeding container env
      FEATURE_X: "true"
```

Interpolation syntax:

| Syntax | Meaning |
|---|---|
| `${VAR}` | Value of VAR (empty if unset, with a warning). |
| `${VAR:-default}` | Default if unset or empty. |
| `${VAR-default}` | Default only if unset. |
| `${VAR:?error msg}` | Fail with message if unset or empty. |
| `${VAR:+replacement}` | Replacement if set and non-empty. |
| `$$` | A literal `$` (e.g. for shell commands in `command:`). |

Precedence for a container variable (highest first): `docker compose run -e`, `environment:` (with interpolation from shell/`.env`), `env_file:`, then the image's `ENV`.

```bash
docker compose config            # show the fully resolved model; your best debugging tool
docker compose --env-file .env.staging config
```

> **Tip:** Commit a `.env.example` and add `.env` to `.gitignore`.

### 6.3.5 Ports, expose, networks

```yaml
ports:
  - "8080:80"
  - "127.0.0.1:5432:5432"
  - target: 443
    published: "8443"
    protocol: tcp
    host_ip: 0.0.0.0
expose:
  - "9090"           # documentation only; services on the same network can reach all ports anyway
networks:
  backend:
    aliases: [database]
    ipv4_address: 10.30.0.10
```

### 6.3.6 Volumes

```yaml
services:
  app:
    volumes:
      - dbdata:/var/lib/data                 # named volume
      - ./config:/etc/app:ro                 # bind mount
      - type: tmpfs
        target: /tmp
        tmpfs:
          size: 67108864
      - type: bind
        source: ./src
        target: /app/src
        bind:
          create_host_path: false
volumes:
  dbdata:
    driver: local
  legacy:
    external: true            # created outside Compose; never deleted by `down -v`
    name: prod_legacy_data
```

### 6.3.7 Resource limits, restart, security

```yaml
services:
  api:
    restart: unless-stopped
    deploy:
      replicas: 2
      resources:
        limits: { cpus: "1.5", memory: 768M, pids: 200 }
        reservations: { memory: 256M }
    read_only: true
    tmpfs: [/tmp]
    user: "10001:10001"
    cap_drop: [ALL]
    cap_add: [NET_BIND_SERVICE]
    security_opt:
      - no-new-privileges:true
    init: true
    stop_grace_period: 30s
    ulimits:
      nofile: { soft: 65536, hard: 65536 }
    logging:
      driver: local
      options: { max-size: "10m", max-file: "3" }
```

Compose v2 applies `deploy.resources` limits for plain `docker compose up` (not only Swarm).

### 6.3.8 Configs and secrets

```yaml
services:
  nginx:
    image: nginx:1.27-alpine
    configs:
      - source: nginx_conf
        target: /etc/nginx/conf.d/default.conf
    secrets:
      - source: tls_key
        target: /run/secrets/tls.key
        mode: 0400
configs:
  nginx_conf:
    file: ./nginx/default.conf
  inline_cfg:
    content: |
      upstream api { server api:8000; }
secrets:
  tls_key:
    file: ./secrets/tls.key
  api_token:
    environment: API_TOKEN       # secret sourced from an env var on the host
```

Secrets are mounted under `/run/secrets/<name>`. Many official images (postgres, mysql, mariadb) support `*_FILE` variables to read secrets from files.

## 6.4 Profiles

Profiles let one Compose file describe optional services: debug tools, monitoring, seed jobs.

```yaml
services:
  adminer:
    image: adminer:4
    profiles: [debug]
  prometheus:
    image: prom/prometheus:v2.54.1
    profiles: [monitoring]
  loadtest:
    image: grafana/k6:0.54.0
    profiles: [test]
```

```bash
docker compose up -d                              # only services without a profile
docker compose --profile debug up -d              # plus adminer
COMPOSE_PROFILES=debug,monitoring docker compose up -d
docker compose run --rm loadtest run /scripts/smoke.js   # explicitly running a service enables its profile
```

## 6.5 Multiple Files: Overrides, extends, include

### 6.5.1 Override files

Compose automatically merges `compose.override.yaml` on top of `compose.yaml`. The convention:

- `compose.yaml`: the canonical, production-like definition.
- `compose.override.yaml`: local dev tweaks (bind mounts, debug ports, dev target). Applied automatically.
- `compose.prod.yaml`, `compose.ci.yaml`: applied explicitly with `-f`.

```yaml
# compose.override.yaml (dev)
services:
  api:
    build:
      target: dev
    volumes:
      - ./api/src:/app/src
    environment:
      LOG_LEVEL: debug
    ports:
      - "127.0.0.1:5678:5678"   # debugger
```

```bash
docker compose up -d                                       # base + override
docker compose -f compose.yaml -f compose.prod.yaml up -d  # base + prod (override NOT applied)
docker compose -f compose.yaml -f compose.prod.yaml config > resolved.yaml
```

Merge rules: scalar values are replaced; `ports`, `expose`, `dns` and similar sequences are concatenated; `environment`, `labels`, `volumes` (by target path) are merged by key. Use `!reset` to clear an inherited value and `!override` to replace a sequence entirely:

```yaml
services:
  api:
    ports: !override
      - "9000:8000"
    healthcheck: !reset null
```

### 6.5.2 extends and include

```yaml
# compose.yaml
include:
  - path: ./observability/compose.yaml     # pull in another project's services
services:
  worker:
    extends:
      file: common.yaml
      service: python-base
    command: ["celery", "-A", "shop", "worker"]
```

`include` imports a whole Compose file (with its own relative paths resolved correctly), which is great for splitting a platform into team-owned pieces.

## 6.6 Day-to-Day Commands

```bash
docker compose up -d                  # create/start, recreating changed services
docker compose up -d --build api      # rebuild and restart one service
docker compose up -d --wait           # block until services are running/healthy
docker compose up -d --scale worker=4
docker compose ps
docker compose logs -f --tail 50 api
docker compose exec api sh
docker compose run --rm api pytest    # one-off container with service config
docker compose stop / start / restart
docker compose pull                   # pull newer images
docker compose build --pull
docker compose push
docker compose top
docker compose events
docker compose cp api:/app/report.html .
docker compose down                   # stop and remove containers + networks
docker compose down -v                # also remove named volumes (data loss!)
docker compose down --rmi local       # also remove locally built images
docker compose ls                     # list running projects
```

`up` is idempotent: Compose computes a config hash per service and recreates only containers whose configuration or image changed.

### 6.6.1 Compose Watch for development

`develop.watch` syncs files or rebuilds automatically, replacing hand-rolled bind-mount hacks:

```yaml
services:
  web:
    build: .
    develop:
      watch:
        - action: sync
          path: ./src
          target: /app/src
          ignore: [node_modules/]
        - action: rebuild
          path: package.json
        - action: sync+restart
          path: ./config
          target: /app/config
```

```bash
docker compose watch
# or: docker compose up --watch
```

## 6.7 Compose in CI and Production

**CI integration tests:**

```bash
docker compose -f compose.yaml -f compose.ci.yaml up -d --wait
docker compose run --rm api pytest -q
status=$?
docker compose logs --no-color > compose-logs.txt
docker compose down -v
exit $status
```

Use a unique project name per CI job (`-p ci-$CI_JOB_ID`) so parallel jobs on one runner do not collide.

**Single-host production**: Compose is acceptable for small, single-host deployments if you add: pinned image tags or digests, `restart: unless-stopped`, healthchecks, resource limits, log rotation, secrets via files, a reverse proxy with TLS, and backups. It does not give you multi-host scheduling, rolling updates with zero downtime, or self-healing across nodes. For those, use Swarm or (much more commonly) Kubernetes.

Deploying to a remote host:

```bash
docker context create prod --docker "host=ssh://deploy@prod-1"
docker --context prod compose -f compose.yaml -f compose.prod.yaml pull
docker --context prod compose -f compose.yaml -f compose.prod.yaml up -d --remove-orphans
```

## 6.8 Lab: Build a Full Dev Environment With Compose

**Goal:** a web API, Postgres and Redis with healthchecks, a migration job, a debug profile and a dev override.

1. Project layout:

```text
compose-lab/
  compose.yaml
  compose.override.yaml
  .env
  api/
    Dockerfile
    requirements.txt
    app.py
  secrets/db_password.txt
```

2. `api/app.py` (Flask app that counts hits in Redis and checks Postgres):

```python
import os
import psycopg
import redis
from flask import Flask

app = Flask(__name__)
r = redis.Redis.from_url(os.environ["REDIS_URL"])

@app.get("/healthz")
def healthz():
    return {"ok": True}

@app.get("/")
def index():
    hits = r.incr("hits")
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        version = conn.execute("select version()").fetchone()[0]
    return {"hits": hits, "db": version.split(",")[0]}
```

`api/requirements.txt`:

```text
flask==3.0.3
gunicorn==23.0.0
redis==5.0.8
psycopg[binary]==3.2.3
```

Use the multi-stage Python Dockerfile from Part 3, with `CMD ["gunicorn", "-b", "0.0.0.0:8000", "app:app"]` and a `dev` stage that runs `flask --app app run --debug --host 0.0.0.0 --port 8000`.

3. Write `compose.yaml` with services `api`, `db`, `cache`, `adminer` (profile `debug`), using the patterns in 6.2: healthchecks on `db` and `api`, `depends_on` with `service_healthy`, a secret for the DB password, a named volume for Postgres. Publish the API as `"127.0.0.1:8080:8000"`, and set `DATABASE_URL: postgresql://postgres:${DB_PASSWORD}@db:5432/postgres` and `REDIS_URL: redis://cache:6379/0` in its environment. Keep this lab on the single default network to keep it simple.

4. `.env`:

```ini
TAG=dev
DB_PASSWORD=labpass
```

5. Validate and start:

```bash
docker compose config --quiet && echo "config OK"
docker compose up -d --build --wait
curl -s localhost:8080/ ; curl -s localhost:8080/
```

```text
{"db":"PostgreSQL 16.4","hits":1}
{"db":"PostgreSQL 16.4","hits":2}
```

6. Add `compose.override.yaml` that targets the `dev` stage and adds a `develop.watch` sync of `./api` to `/app`. Run `docker compose watch`, edit `app.py`, and see the reload.

7. Start the debug profile: `docker compose --profile debug up -d` and log in to Adminer at `localhost:8081` (server `db`).

8. Kill the database and watch the API's health and logs: `docker compose kill db`, `docker compose ps`, `docker compose logs api`. Then `docker compose up -d` to restore.

9. Tear down: `docker compose --profile debug down -v`.

## Key takeaways

- Use `docker compose` (v2) and `compose.yaml` without a `version:` key.
- Services discover each other by service name on the project network; split networks for isolation.
- `depends_on` with `service_healthy` and `service_completed_successfully` fixes startup ordering, but apps must still retry.
- Distinguish YAML interpolation (`.env`, shell) from container environment (`environment`, `env_file`); `docker compose config` shows the truth.
- Profiles add optional services; override files and `-f` layering adapt one definition to dev, CI and prod.
- `docker compose watch` gives fast inner-loop development; `up --wait` makes CI deterministic.
