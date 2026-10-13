# Part 3 — Writing Dockerfiles

A Dockerfile is a recipe that BuildKit executes to produce an image. Writing good Dockerfiles is the single highest-leverage Docker skill: it determines image size, build speed, security posture and runtime behavior. This Part covers every instruction, how layers and the build cache work, and multi-stage builds.

## 3.1 The Build Context and .dockerignore

When you run `docker build .`, the `.` is the **build context**: the set of files the builder may access via `COPY` and `ADD`. With BuildKit, only files actually referenced are transferred, but the context still defines what is reachable, and a large or sensitive context slows builds and risks leaking secrets.

Always add a `.dockerignore`:

```text
# .dockerignore
.git
.github
node_modules
dist
build
*.log
.env
.env.*
**/*.pem
**/__pycache__
.venv
Dockerfile*
docker-compose*.yaml
compose*.yaml
```

> **Warning:** Without `.dockerignore`, `COPY . .` copies your `.env`, `.git` history (which may contain old secrets) and local `node_modules` (built for the wrong OS) into the image.

Basic build invocations:

```bash
docker build -t myapp:dev .
docker build -t myapp:dev -f docker/Dockerfile.prod .
docker build --build-arg APP_VERSION=1.4.2 -t myapp:1.4.2 .
docker build --target test .            # build a specific stage
docker build --no-cache .
docker build --progress=plain .         # full log output, useful in CI
```

## 3.2 Every Dockerfile Instruction

### 3.2.1 Parser directives and syntax

```dockerfile
# syntax=docker/dockerfile:1
# check=error=true
```

The `syntax` directive must be the very first line. It tells BuildKit to fetch the latest stable Dockerfile frontend (version 1.x), giving you new features (heredocs, `COPY --exclude`, `--chmod`, build checks) without upgrading Docker. `check=error=true` turns build-check warnings (e.g. `JSONArgsRecommended`, `SecretsUsedInArgOrEnv`) into errors.

### 3.2.2 FROM

```dockerfile
FROM python:3.12-slim-bookworm AS base
FROM --platform=$BUILDPLATFORM golang:1.23 AS build
FROM scratch
```

Starts a new build stage. `AS name` names the stage for later reference. Choose base images deliberately: official images, slim or alpine variants, distroless for runtime. `--platform=$BUILDPLATFORM` is for cross-compilation (Part 4).

### 3.2.3 ARG

```dockerfile
ARG BASE_TAG=3.12-slim
FROM python:${BASE_TAG}
ARG APP_VERSION=dev
RUN echo "building ${APP_VERSION}"
```

Build-time variables, set with `--build-arg`. Scoping rules trip people up:

- An `ARG` before the first `FROM` is only usable in `FROM` lines. To use it inside a stage, redeclare `ARG BASE_TAG` (without a value) in that stage.
- An `ARG` declared in a stage is only visible within that stage.
- Values are recorded in image history. **Never pass secrets as build args**; use secret mounts (Part 4).

Predefined args: `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY` (not persisted in history), plus BuildKit's `TARGETPLATFORM`, `TARGETOS`, `TARGETARCH`, `BUILDPLATFORM`, `BUILDOS`, `BUILDARCH`.

### 3.2.4 ENV

```dockerfile
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:${PATH}"
```

Sets environment variables that persist in the image and in running containers. Use `ARG` for build-only values and `ENV` for values the runtime needs. A common pattern to make an arg available at runtime: `ARG APP_VERSION` then `ENV APP_VERSION=${APP_VERSION}`.

### 3.2.5 WORKDIR

```dockerfile
WORKDIR /app
```

Sets the working directory for subsequent `RUN`, `CMD`, `ENTRYPOINT`, `COPY`, `ADD`. Creates it if missing. Use absolute paths and prefer `WORKDIR` over `RUN cd /app && ...`.

### 3.2.6 COPY

```dockerfile
COPY requirements.txt .
COPY --chown=app:app --chmod=0755 entrypoint.sh /usr/local/bin/
COPY --from=build /out/server /server
COPY --from=nginx:1.27-alpine /etc/nginx/nginx.conf /tmp/ref.conf
COPY --link package.json package-lock.json ./
COPY --exclude=*.md . /src
```

Copies files from the build context (or another stage/image with `--from`). Key flags:

- `--chown`, `--chmod`: set ownership and mode without an extra `RUN chown` layer (which would duplicate file data).
- `--from`: copy from a stage name, stage index, or any image reference.
- `--link`: copies into an independent layer that does not depend on the previous layers, so changes to earlier layers do not invalidate it and it can be rebased efficiently.
- `--exclude`: skip matching paths (Dockerfile frontend 1.7+ / stable in recent versions).
- `--parents`: preserve directory structure for glob matches.

### 3.2.7 ADD

```dockerfile
ADD https://github.com/org/tool/releases/download/v1.2/tool.tar.gz /tmp/
ADD --checksum=sha256:4b1c... https://example.com/file.tar.gz /tmp/
ADD rootfs.tar.gz /
ADD git@github.com:org/repo.git#v1.2.0 /src
```

Like `COPY` plus extras: local tar archives are auto-extracted, remote URLs are fetched, and Git repositories can be cloned. Because of the implicit magic, **prefer `COPY` unless you need these features**. When fetching URLs, always use `--checksum` for integrity.

### 3.2.8 RUN

```dockerfile
# Shell form: runs via /bin/sh -c
RUN apt-get update && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

# Exec form: no shell
RUN ["/bin/bash", "-c", "set -euo pipefail; ./build.sh"]

# Heredoc (Dockerfile 1.4+)
RUN <<EOF
set -eux
apt-get update
apt-get install -y --no-install-recommends ca-certificates
rm -rf /var/lib/apt/lists/*
EOF

# BuildKit mounts
RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements.txt
RUN --mount=type=secret,id=npmrc,target=/root/.npmrc npm ci
RUN --network=none make test
```

Each `RUN` creates a layer. Rules of thumb:

- Combine `update` and `install` in the same `RUN`, otherwise a cached stale `apt-get update` layer can be reused with a new install line.
- Clean caches in the **same** layer they were created in; deleting in a later layer does not shrink the image.
- `/bin/sh -c` does not have `pipefail`; use `SHELL` or `set -o pipefail` inside bash.

### 3.2.9 SHELL

```dockerfile
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
```

Changes the shell used by shell-form instructions. Useful for `pipefail` and essential on Windows images (PowerShell).

### 3.2.10 USER

```dockerfile
RUN groupadd --system --gid 10001 app && useradd --system --uid 10001 --gid app app
USER 10001:10001
```

Sets the user for subsequent `RUN`, `CMD`, `ENTRYPOINT`, and for the running container. Use a numeric UID so Kubernetes `runAsNonRoot` can verify it without looking up `/etc/passwd`. Running as root is the default and is a security smell (Part 8).

### 3.2.11 EXPOSE

```dockerfile
EXPOSE 8080/tcp
EXPOSE 9090/udp
```

Pure documentation plus a hint for `docker run -P`. It does **not** publish ports or open firewalls.

### 3.2.12 VOLUME

```dockerfile
VOLUME /var/lib/postgresql/data
```

Declares a mount point; at runtime Docker creates an anonymous volume there if you do not mount one. Pitfalls: any build step that changes that path *after* `VOLUME` is discarded, and anonymous volumes accumulate silently. Many teams avoid `VOLUME` in app images and declare volumes in Compose or Kubernetes instead.

### 3.2.13 CMD and ENTRYPOINT

These define what runs. **Always prefer exec form (JSON array).**

```dockerfile
ENTRYPOINT ["python", "-m", "myapp"]
CMD ["--port", "8080"]
```

- `ENTRYPOINT` is the executable; `CMD` provides default arguments.
- `docker run image --port 9000` replaces `CMD` only.
- `docker run --entrypoint sh image` replaces `ENTRYPOINT`.

| | No ENTRYPOINT | `ENTRYPOINT ["e1"]` | `ENTRYPOINT e1` (shell) |
|---|---|---|---|
| No CMD | error | `e1` | `/bin/sh -c e1` |
| `CMD ["c1"]` | `c1` | `e1 c1` | `/bin/sh -c e1` (CMD ignored) |
| `CMD c1` (shell) | `/bin/sh -c c1` | `e1 /bin/sh -c c1` | `/bin/sh -c e1` |

Shell form makes `/bin/sh` PID 1, which does not forward SIGTERM to your app, so `docker stop` waits and then kills it.

The wrapper-script pattern: an entrypoint script that does setup and then `exec`s the real process so it becomes PID 1:

```bash
#!/bin/sh
# docker-entrypoint.sh
set -e
if [ -n "$WAIT_FOR_DB" ]; then
  until nc -z "$DB_HOST" 5432; do echo "waiting for db"; sleep 1; done
fi
exec "$@"
```

```dockerfile
COPY --chmod=0755 docker-entrypoint.sh /usr/local/bin/
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["gunicorn", "-b", "0.0.0.0:8080", "app:app"]
```

### 3.2.14 HEALTHCHECK

```dockerfile
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --start-interval=2s --retries=3 \
  CMD ["wget", "-qO-", "http://127.0.0.1:8080/healthz"]
```

Docker runs the command inside the container and sets health status to `starting`, `healthy` or `unhealthy`. Exit 0 is healthy, 1 is unhealthy. Compose uses it for `depends_on: condition: service_healthy`; Swarm replaces unhealthy tasks. Plain `docker run` only reports it (it does not restart). Kubernetes ignores `HEALTHCHECK` and uses its own probes. `HEALTHCHECK NONE` disables an inherited check.

### 3.2.15 LABEL

```dockerfile
LABEL org.opencontainers.image.source="https://github.com/acme/api" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.licenses="Apache-2.0"
```

Key/value metadata. Use the standard `org.opencontainers.image.*` annotation keys; registries and scanners understand them. (`MAINTAINER` is deprecated; use a label.)

### 3.2.16 STOPSIGNAL, ONBUILD

```dockerfile
STOPSIGNAL SIGQUIT          # nginx graceful shutdown
ONBUILD COPY . /app         # runs when another image uses this as a base (rarely recommended)
```

`ONBUILD` hides behavior from downstream users; avoid it except in tightly controlled internal base images.

## 3.3 Layers and the Build Cache

### 3.3.1 How layers form

Each `RUN`, `COPY` and `ADD` produces a filesystem layer. Other instructions (`ENV`, `CMD`, `LABEL`, `EXPOSE`, `USER`, `WORKDIR`) change only the image config and produce empty layers. Layers are stacked using overlayfs at runtime (Part 7).

Consequences:

- Deleting a file in a later layer hides it but does not remove its bytes from the image. A secret copied in layer 3 and deleted in layer 4 is still extractable.
- Changing permissions with `RUN chmod -R` on large trees duplicates the data in a new layer. Use `COPY --chmod`.

### 3.3.2 Cache rules

BuildKit reuses a cached result for a step when:

- For `RUN`: the instruction string is identical and all parent layers are cached. It does **not** look at what the command would fetch from the network.
- For `COPY`/`ADD`: the checksums of the copied files (content and metadata, not mtime) are unchanged.
- Once one step misses the cache, every later step in that stage rebuilds (unless using `--link` layers or independent stages).

Therefore: **order instructions from least frequently changing to most frequently changing.**

Bad:

```dockerfile
FROM node:22-alpine
WORKDIR /app
COPY . .
RUN npm ci
CMD ["node", "server.js"]
```

Any source change invalidates `COPY . .`, so `npm ci` reruns every build.

Good:

```dockerfile
FROM node:22-alpine
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev
COPY . .
USER node
CMD ["node", "server.js"]
```

Now dependency installation is cached until the lockfile changes.

```text
$ docker build -t api .
 => [1/5] FROM docker.io/library/node:22-alpine@sha256:...       0.0s
 => CACHED [2/5] WORKDIR /app                                     0.0s
 => CACHED [3/5] COPY package.json package-lock.json ./           0.0s
 => CACHED [4/5] RUN npm ci --omit=dev                            0.0s
 => [5/5] COPY . .                                                0.1s
 => exporting to image                                            0.2s
```

### 3.3.3 Cache busting deliberately

```dockerfile
ARG CACHE_BUST
RUN apt-get update && apt-get upgrade -y   # rerun when CACHE_BUST changes
```

```bash
docker build --build-arg CACHE_BUST=$(date +%F) .
```

Or `docker build --no-cache-filter stage-name .` to rebuild a single stage. Rebuilding base images regularly (with `--pull`) is how you pick up OS security patches.

## 3.4 Multi-Stage Builds

Multi-stage builds separate the **build environment** (compilers, dev dependencies, test tools) from the **runtime image**. Only what you `COPY --from` reaches the final image.

### 3.4.1 Go: from 900 MB to under 15 MB

```dockerfile
# syntax=docker/dockerfile:1
FROM golang:1.23 AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/server ./cmd/server

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/server /server
USER nonroot:nonroot
EXPOSE 8080
ENTRYPOINT ["/server"]
```

### 3.4.2 Python with a virtual environment

```dockerfile
# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm AS build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements.txt

FROM python:3.12-slim-bookworm AS runtime
ENV PYTHONUNBUFFERED=1 PATH="/opt/venv/bin:$PATH"
RUN useradd --system --uid 10001 app
COPY --from=build /opt/venv /opt/venv
WORKDIR /app
COPY --chown=10001:10001 src/ ./
USER 10001
EXPOSE 8000
CMD ["gunicorn", "-b", "0.0.0.0:8000", "app:app"]
```

### 3.4.3 Node.js frontend served by nginx

```dockerfile
# syntax=docker/dockerfile:1
FROM node:22-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci

FROM deps AS build
COPY . .
RUN npm run build

FROM deps AS test
COPY . .
RUN npm test

FROM nginxinc/nginx-unprivileged:1.27-alpine AS runtime
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 8080
```

```bash
docker build --target test .        # CI: run tests in the build
docker build --target runtime -t web:1.0 .
```

BuildKit builds only the stages needed for the requested target, and builds independent stages in parallel. A `test` stage that nothing depends on is skipped when you build `runtime`.

### 3.4.4 Stage patterns

| Pattern | Use |
|---|---|
| `deps` -> `build` -> `runtime` | Cache dependencies separately from source. |
| `test` stage | Run unit tests during CI builds with `--target test`. |
| `dev` stage | Includes hot-reload tools; Compose uses `target: dev` locally. |
| `FROM runtime AS debug` | Adds shell and tools on top of a minimal runtime for troubleshooting. |
| Export stage | `FROM scratch AS export` + `COPY --from=build /out /` with `--output type=local,dest=./bin` to extract build artifacts without an image. |

```dockerfile
FROM scratch AS export
COPY --from=build /out/server /
```

```bash
docker build --target export --output type=local,dest=./bin .
ls ./bin   # server
```

## 3.5 Dockerfile Best-Practice Checklist

| Practice | Why |
|---|---|
| Start with `# syntax=docker/dockerfile:1` | Latest stable frontend features. |
| Pin base image versions (and digests for prod) | Reproducibility, supply chain safety. |
| Use `.dockerignore` | Smaller context, no leaked secrets. |
| Order layers by change frequency | Maximize cache hits. |
| Multi-stage builds | Small runtime images without build tools. |
| One concern per container | Easier scaling, logging, restarts. |
| Exec-form `CMD`/`ENTRYPOINT` | Correct signal handling. |
| Non-root `USER` with numeric UID | Least privilege, Kubernetes compatibility. |
| `--no-install-recommends` and clean caches in the same layer | Smaller images. |
| `COPY` over `ADD` | Explicit, no surprise extraction. |
| No secrets in `ARG`, `ENV`, or copied files | They persist in layers and history. |
| OCI labels | Traceability back to source and commit. |
| Run `docker build --check .` | Built-in linting of Dockerfile issues. |

Linting:

```bash
docker build --check .
docker run --rm -i hadolint/hadolint < Dockerfile
```

```text
$ docker build --check .
WARNING: JSONArgsRecommended - JSON arguments recommended for CMD to prevent unintended behavior related to OS signals
Dockerfile:12
```

## 3.6 Lab: Build, Measure and Optimize a Python API

**Goal:** turn a naive Dockerfile into an optimized multi-stage one and measure the difference.

1. Create the app:

```bash
mkdir -p ~/labs/pyapi/src && cd ~/labs/pyapi
cat > requirements.txt <<'EOF'
flask==3.0.3
gunicorn==23.0.0
EOF
cat > src/app.py <<'EOF'
from flask import Flask
app = Flask(__name__)

@app.get("/healthz")
def healthz():
    return {"status": "ok"}

@app.get("/")
def index():
    return {"hello": "docker"}
EOF
```

2. Naive Dockerfile:

```dockerfile
# Dockerfile.naive
FROM python:3.12
COPY . /app
WORKDIR /app
RUN pip install -r requirements.txt
CMD gunicorn -b 0.0.0.0:8000 --chdir src app:app
```

```bash
docker build -f Dockerfile.naive -t pyapi:naive .
docker build --check -f Dockerfile.naive .
```

3. Edit `src/app.py` (change the greeting) and rebuild. Observe that `pip install` reruns.

4. Write the optimized multi-stage Dockerfile from section 3.4.2 (adapt the CMD to `--chdir /app app:app` since `src/` is copied to `/app`), add a `.dockerignore`, and a `HEALTHCHECK` using Python:

```dockerfile
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"]
```

5. Build and compare:

```bash
docker build -t pyapi:opt .
docker image ls pyapi
```

```text
REPOSITORY   TAG     IMAGE ID       CREATED          SIZE
pyapi        opt     7a1c...        10 seconds ago   148MB
pyapi        naive   3e9b...        3 minutes ago    1.03GB
```

6. Edit the source again and rebuild `pyapi:opt`. Confirm the dependency layer is `CACHED`.

7. Run it and check health and stop behavior:

```bash
docker run -d --name pyapi -p 8000:8000 pyapi:opt
sleep 15; docker ps --filter name=pyapi --format '{{.Status}}'
time docker stop pyapi     # should be fast: gunicorn is PID 1 and handles SIGTERM
docker rm pyapi
```

## Key takeaways

- The build context plus `.dockerignore` controls what can enter an image; always ignore `.git`, `.env` and local dependency folders.
- Every instruction has a precise purpose; know `ARG` vs `ENV`, `COPY` vs `ADD`, `CMD` vs `ENTRYPOINT`, and exec vs shell form.
- Layers are additive: deleting later never shrinks an image or removes a leaked secret.
- Order instructions by change frequency so dependency installs stay cached.
- Multi-stage builds keep compilers and dev tools out of runtime images and enable test, dev and export stages.
- Use `docker build --check` and hadolint to catch mistakes early.
