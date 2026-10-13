# Part 11 — Capstone Project, Interview Preparation and Cheat Sheet

This final Part pulls everything together. You will build a production-style multi-service application end to end: hardened images, a Compose environment with healthchecks and profiles, a CI pipeline with caching, scanning, SBOM and signing, and a deployment by digest. Then you will test yourself with more than forty interview questions, and finish with a dense command cheat sheet to keep at hand.

## 11.1 Capstone: "Linkly", a URL Shortener Platform

### 11.1.1 Architecture

```text
                       +-------------------+
   browser ---HTTPS--> |  proxy (Caddy)    |  frontend network
                       +---------+---------+
                                 |
                       +---------v---------+         +-----------------+
                       |  api (Go, x2)     |-------->|  redis (cache)  |
                       |  distroless,      |         +-----------------+
                       |  non-root         |                backend (internal)
                       +---------+---------+
                                 |
                       +---------v---------+   +----------------------------+
                       | postgres 16       |   | migrate (one-shot job)     |
                       | volume: pgdata    |   +----------------------------+
                       +-------------------+
   optional profiles: adminer (debug), prometheus + cadvisor + grafana (monitoring)
```

Requirements:

- `api` written in Go, built multi-stage and multi-arch, final image on distroless static, UID 65532, under 20 MB.
- Read-only root filesystem, all capabilities dropped, `no-new-privileges`, resource limits.
- Postgres password from a secret file; no secrets in env for production.
- Healthchecks everywhere; `api` waits for healthy `db` and successful `migrate`.
- Backend network is internal; only the proxy publishes ports.
- CI: lint, test stage, build with registry/GHA cache, Trivy gate, SBOM + provenance, cosign keyless signing, push by SHA, promote on tag.

### 11.1.2 Repository layout

```text
linkly/
  api/
    cmd/linkly/main.go
    internal/...
    go.mod
    go.sum
    Dockerfile
    .dockerignore
  proxy/
    Caddyfile
  db/
    migrations/001_init.sql
  secrets/
    db_password.txt        (gitignored)
  compose.yaml
  compose.override.yaml
  compose.prod.yaml
  monitoring/
    compose.yaml
    prometheus.yml
  .env.example
  .github/workflows/ci.yaml
```

### 11.1.3 Step 1: the application

A minimal but realistic Go service (shortened for space):

```go
package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	_ "github.com/jackc/pgx/v5/stdlib"
)

func readSecret(name string) string {
	if p := os.Getenv(name + "_FILE"); p != "" {
		b, err := os.ReadFile(p)
		if err == nil {
			return strings.TrimSpace(string(b))
		}
	}
	return os.Getenv(name)
}

func main() {
	log := slog.New(slog.NewJSONHandler(os.Stdout, nil))
	dsn := "postgres://linkly:" + readSecret("DB_PASSWORD") + "@db:5432/linkly?sslmode=disable"
	db, err := sql.Open("pgx", dsn)
	if err != nil {
		log.Error("db open", "err", err)
		os.Exit(1)
	}

	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	})
	mux.HandleFunc("GET /readyz", func(w http.ResponseWriter, r *http.Request) {
		ctx, cancel := context.WithTimeout(r.Context(), time.Second)
		defer cancel()
		if err := db.PingContext(ctx); err != nil {
			http.Error(w, "db not ready", http.StatusServiceUnavailable)
			return
		}
		json.NewEncoder(w).Encode(map[string]string{"status": "ready"})
	})
	// ... POST /links, GET /{code} handlers ...

	srv := &http.Server{Addr: ":8080", Handler: mux, ReadHeaderTimeout: 5 * time.Second}
	go func() {
		log.Info("listening", "addr", srv.Addr)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Error("serve", "err", err)
			os.Exit(1)
		}
	}()

	// Graceful shutdown on SIGTERM (docker stop) or SIGINT
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()
	<-ctx.Done()
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	srv.Shutdown(shutdownCtx)
	log.Info("shut down cleanly")
}
```

Note the production behaviors: JSON logs to stdout, `*_FILE` secrets, separate liveness (`/healthz`) and readiness (`/readyz`), graceful shutdown on SIGTERM. Add a `healthcheck` subcommand (or a tiny separate binary) that GETs `/healthz`, because distroless has no `curl`.

### 11.1.4 Step 2: the hardened Dockerfile

```dockerfile
# syntax=docker/dockerfile:1
# check=error=true

ARG GO_VERSION=1.23

FROM --platform=$BUILDPLATFORM golang:${GO_VERSION} AS deps
WORKDIR /src
COPY go.mod go.sum ./
RUN --mount=type=cache,target=/go/pkg/mod go mod download

FROM deps AS test
COPY . .
RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    go vet ./... && go test ./...

FROM deps AS build
ARG TARGETOS TARGETARCH
ARG VERSION=dev
COPY . .
RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    CGO_ENABLED=0 GOOS=$TARGETOS GOARCH=$TARGETARCH \
    go build -trimpath -ldflags="-s -w -X main.version=${VERSION}" \
      -o /out/linkly ./cmd/linkly

FROM gcr.io/distroless/static-debian12:nonroot AS runtime
ARG VERSION=dev
ARG GIT_SHA=unknown
LABEL org.opencontainers.image.title="linkly-api" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.source="https://github.com/acme/linkly"
COPY --from=build /out/linkly /linkly
USER 65532:65532
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
  CMD ["/linkly", "healthcheck"]
ENTRYPOINT ["/linkly"]
```

`.dockerignore`:

```text
.git
**/*_test.go.orig
bin/
dist/
*.md
.env*
```

Build and check:

```bash
docker build --check api/
docker build --target test api/
docker buildx build --platform linux/amd64,linux/arm64 -t linkly-api:dev --target runtime api/
docker image ls linkly-api
```

```text
REPOSITORY   TAG   IMAGE ID       CREATED          SIZE
linkly-api   dev   5d3b...        12 seconds ago   14.2MB
```

### 11.1.5 Step 3: Compose

```yaml
# compose.yaml
name: linkly

x-hardening: &hardening
  read_only: true
  cap_drop: [ALL]
  security_opt: ["no-new-privileges:true"]
  init: false

services:
  proxy:
    image: caddy:2.8-alpine
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./proxy/Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy_data:/data
    cap_drop: [ALL]
    cap_add: [NET_BIND_SERVICE]
    depends_on:
      api:
        condition: service_healthy
    networks: [frontend]
    restart: unless-stopped

  api:
    <<: *hardening
    build:
      context: ./api
      target: runtime
    image: ghcr.io/acme/linkly-api:${TAG:-dev}
    environment:
      DB_PASSWORD_FILE: /run/secrets/db_password
      REDIS_ADDR: cache:6379
    secrets: [db_password]
    depends_on:
      db: { condition: service_healthy }
      cache: { condition: service_healthy }
      migrate: { condition: service_completed_successfully }
    deploy:
      replicas: 2
      resources:
        limits: { cpus: "0.50", memory: 128M, pids: 100 }
    networks: [frontend, backend]
    restart: unless-stopped

  migrate:
    image: migrate/migrate:v4.17.1
    command: ["-path=/migrations", "-database", "postgres://linkly:${DB_PASSWORD:?set DB_PASSWORD}@db:5432/linkly?sslmode=disable", "up"]
    volumes:
      - ./db/migrations:/migrations:ro
    depends_on:
      db: { condition: service_healthy }
    networks: [backend]
    restart: "no"

  db:
    image: postgres:16.4-alpine
    environment:
      POSTGRES_USER: linkly
      POSTGRES_DB: linkly
      POSTGRES_PASSWORD_FILE: /run/secrets/db_password
    secrets: [db_password]
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U linkly -d linkly"]
      interval: 5s
      timeout: 3s
      retries: 10
    deploy:
      resources:
        limits: { memory: 512M }
    networks: [backend]
    restart: unless-stopped

  cache:
    <<: *hardening
    image: redis:7.4-alpine
    command: ["redis-server", "--maxmemory", "64mb", "--maxmemory-policy", "allkeys-lru", "--save", ""]
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      retries: 5
    deploy:
      resources:
        limits: { memory: 96M }
    networks: [backend]
    restart: unless-stopped

  adminer:
    image: adminer:4
    profiles: [debug]
    ports: ["127.0.0.1:8081:8080"]
    networks: [backend, frontend]

networks:
  frontend:
  backend:
    internal: true

volumes:
  pgdata:
  caddy_data:

secrets:
  db_password:
    file: ./secrets/db_password.txt
```

Points worth noticing:

- `x-hardening` is an **extension field** with a YAML anchor reused by several services.
- `migrate` gets the password via interpolation only because the migrate tool needs a URL; in a stricter setup, wrap it in a script that reads the secret file.
- The `api` replicas share the service DNS name `api`; Caddy load-balances with `reverse_proxy api:8080`. No host ports on `api`, so scaling does not cause port conflicts.
- Redis gets an internal `maxmemory` below its cgroup limit, so it evicts keys instead of being OOM-killed.

`proxy/Caddyfile`:

```text
:80 {
    reverse_proxy api:8080 {
        health_uri /readyz
        lb_policy round_robin
    }
}
```

`compose.override.yaml` (dev):

```yaml
services:
  api:
    build:
      target: build
    read_only: false
    develop:
      watch:
        - action: rebuild
          path: ./api
```

`compose.prod.yaml`:

```yaml
services:
  api:
    image: ghcr.io/acme/linkly-api@${API_DIGEST:?set API_DIGEST}
    build: !reset null
    logging:
      driver: local
      options: { max-size: "10m", max-file: "5" }
  db:
    logging:
      driver: local
      options: { max-size: "10m", max-file: "5" }
```

Run it:

```bash
cp .env.example .env && openssl rand -hex 16 > secrets/db_password.txt
echo "DB_PASSWORD=$(cat secrets/db_password.txt)" >> .env
docker compose config --quiet
docker compose up -d --build --wait
docker compose ps
curl -s -X POST localhost/links -d '{"url":"https://docs.docker.com"}'
```

### 11.1.6 Step 4: monitoring profile

Add `monitoring/compose.yaml` with cAdvisor, Prometheus and Grafana from Part 7 (each with `profiles: [monitoring]`) and include it:

```yaml
# at the top of compose.yaml
include:
  - monitoring/compose.yaml
```

```bash
docker compose --profile monitoring up -d
```

Build a Grafana panel for CPU throttling and memory working set versus limit for the `api` containers.

### 11.1.7 Step 5: CI pipeline

Use the workflow from Part 9 with these changes:

- `context: ./api`.
- A `lint` job: `docker build --check ./api` and `hadolint api/Dockerfile`.
- An `integration` job that `needs: build`, pulls the image by digest, and runs:

```bash
export API_DIGEST=${{ needs.build.outputs.digest }}
openssl rand -hex 16 > secrets/db_password.txt
export DB_PASSWORD=$(cat secrets/db_password.txt)
docker compose -f compose.yaml -f compose.prod.yaml up -d --wait
curl -fsS localhost/readyz
docker compose logs --no-color > compose.log
docker compose down -v
```

- A `release` job on tags that promotes the digest: `docker buildx imagetools create -t ghcr.io/acme/linkly-api:${TAG} ghcr.io/acme/linkly-api@${DIGEST}`.

### 11.1.8 Step 6: deploy and verify

On the production host (via a Docker context over SSH):

```bash
DIGEST=sha256:...   # from the CI release job
cosign verify ghcr.io/acme/linkly-api@$DIGEST \
  --certificate-identity-regexp 'https://github.com/acme/linkly/.github/workflows/ci.yaml@refs/tags/v.*' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com > /dev/null \
  && echo "signature OK"
export API_DIGEST=$DIGEST
docker --context prod compose -f compose.yaml -f compose.prod.yaml pull
docker --context prod compose -f compose.yaml -f compose.prod.yaml up -d --wait --remove-orphans
```

Rollback = redeploy with the previous digest.

### 11.1.9 Acceptance checklist

| Check | Command | Expected |
|---|---|---|
| Image size | `docker image ls` | under 20 MB |
| Non-root | `docker image inspect -f '{{.Config.User}}' linkly-api:dev` | `65532:65532` |
| Read-only | `docker inspect -f '{{.HostConfig.ReadonlyRootfs}}'` | `true` |
| Capabilities | `docker inspect -f '{{.HostConfig.CapDrop}}'` | `[ALL]` |
| No published DB port | `docker compose port db 5432` | error / empty |
| Vulnerabilities | `trivy image --severity HIGH,CRITICAL` | 0 |
| Signed | `cosign verify ...` | success |
| Graceful stop | `time docker compose stop api` | under 2 s, exit 0 |
| Data persists | `down` then `up` without `-v` | links still resolve |
| Backend isolation | `docker compose exec cache ping -c1 1.1.1.1` | fails |

## 11.2 Interview Questions and Answers

### 11.2.1 Beginner

**1. What is the difference between an image and a container?**
An image is an immutable, layered filesystem plus metadata (a template). A container is a running (or stopped) instance of an image with its own writable layer, namespaces and cgroup.

**2. What is the difference between a container and a VM?**
A VM virtualizes hardware and runs its own kernel under a hypervisor. A container is a process isolated by kernel namespaces and constrained by cgroups, sharing the host kernel. Containers are lighter and faster; VMs offer stronger isolation.

**3. What does `docker run` actually do?**
Pulls the image if missing, creates a container (writable layer, network endpoint, mounts, config), starts it via containerd/runc, and attaches or detaches.

**4. `CMD` vs `ENTRYPOINT`?**
`ENTRYPOINT` sets the executable; `CMD` sets default arguments (or the default command if there is no entrypoint). Arguments to `docker run` replace `CMD`; `--entrypoint` replaces `ENTRYPOINT`.

**5. `COPY` vs `ADD`?**
Both copy files into the image. `ADD` also extracts local tar archives and can fetch URLs and Git repos. Prefer `COPY` for clarity unless you need those features.

**6. What is a Docker volume and why use it?**
Docker-managed persistent storage outside the container's writable layer. Data survives container removal, performs better than the overlay layer, and can be shared and backed up.

**7. Volume vs bind mount?**
A volume is managed by Docker under its storage area; a bind mount maps any host path. Bind mounts are good for dev and config; volumes for persistent app data.

**8. What does `EXPOSE` do?**
Documents which ports the app listens on and enables `-P`. It does not publish ports.

**9. How do you see logs of a container?**
`docker logs [-f] [--tail N] <container>`, provided the logging driver supports reading.

**10. How do you get a shell in a running container?**
`docker exec -it <container> sh` (or bash). For images without a shell, use a debug container sharing namespaces or `docker debug`.

**11. What is a Dockerfile layer?**
The filesystem change produced by an instruction like `RUN`, `COPY` or `ADD`. Layers are cached, content-addressed and shared between images.

**12. What is `.dockerignore` for?**
Excludes files from the build context, making builds faster and preventing secrets, `.git` and local artifacts from entering images.

**13. What is Docker Compose?**
A tool to define and run multi-container apps from a YAML file (`compose.yaml`), managing services, networks, volumes and secrets as one project with `docker compose up/down`.

**14. What does `latest` mean?**
Only the default tag name when none is specified. It does not mean newest and should not be used for deployments.

### 11.2.2 Intermediate

**15. How does the build cache work, and how do you optimize for it?**
Each instruction is cached by its text and inputs (file checksums for `COPY`). A miss invalidates all subsequent steps. Copy dependency manifests and install dependencies before copying source code; use cache mounts for package managers.

**16. What are multi-stage builds and why use them?**
Multiple `FROM` stages in one Dockerfile; the final stage copies only artifacts from earlier stages. This removes compilers and dev dependencies from the runtime image, reducing size and attack surface, and enables test/dev stages.

**17. Why should a container's main process use the exec form?**
Shell form runs under `/bin/sh -c`, which becomes PID 1 and does not forward signals, so SIGTERM never reaches the app and `docker stop` ends with SIGKILL after the timeout.

**18. What is the PID 1 problem?**
PID 1 gets no default signal handlers and must reap zombies. Apps not written for that ignore SIGTERM and leak zombies. Solutions: handle signals, use `--init` (tini) or `exec` in entrypoint scripts.

**19. What does exit code 137 mean?**
128 + 9: the process received SIGKILL, commonly from the OOM killer (check `OOMKilled`) or a stop timeout.

**20. How do containers on the same host talk to each other?**
Via a user-defined bridge network, using container or service names resolved by Docker's embedded DNS at 127.0.0.11. The default bridge has no name resolution.

**21. How does port publishing work under the hood?**
Docker adds iptables DNAT rules in the `nat` table's `DOCKER` chain to forward host port traffic to the container IP and port, plus filter rules and MASQUERADE for outbound traffic. Published ports bypass host INPUT firewall rules; use `DOCKER-USER`.

**22. Explain `depends_on` conditions in Compose.**
`service_started` (default) only orders startup; `service_healthy` waits for the dependency's healthcheck; `service_completed_successfully` waits for a one-shot job to exit 0. Apps still need retry logic at runtime.

**23. Difference between `ARG` and `ENV`?**
`ARG` exists only at build time (and is visible in history); `ENV` persists into the image and running containers.

**24. How do you pass secrets during a build safely?**
BuildKit secret mounts: `RUN --mount=type=secret,id=x` with `docker build --secret id=x,src=...`. The secret is never written into a layer. Never use `ARG`/`ENV` for secrets.

**25. How do you limit a container's resources?**
`--memory`, `--memory-swap`, `--cpus`, `--cpu-shares`, `--pids-limit`, `--cpuset-cpus`, or `deploy.resources` in Compose. These configure cgroup controllers.

**26. What are Docker's logging drivers and what is the risk with the default?**
Drivers send stdout/stderr to `json-file`, `local`, `journald`, `syslog`, `fluentd`, `awslogs`, and others. The default `json-file` does not rotate unless configured, so logs can fill the disk.

**27. How would you reduce an image from 1 GB to under 100 MB?**
Multi-stage build, slim/alpine/distroless base, production-only dependencies, clean package caches in the same layer, tight `.dockerignore`, static binaries for compiled languages, and inspect with `dive`.

**28. What is the difference between `docker stop` and `docker kill`?**
`stop` sends the stop signal (SIGTERM by default), waits for the grace period (10 s), then SIGKILL. `kill` sends SIGKILL immediately (or a chosen signal with `-s`).

**29. How do you make Compose work for dev, CI and prod?**
A canonical `compose.yaml`, an auto-loaded `compose.override.yaml` for dev, explicit `-f compose.prod.yaml` or `compose.ci.yaml` overlays, profiles for optional services, and `.env` interpolation; verify with `docker compose config`.

**30. What is the difference between a tag and a digest?**
A tag is a mutable, human-readable pointer; a digest (`sha256:...`) is the immutable content hash of a manifest or index. Deploy by digest for reproducibility and safety.

### 11.2.3 Advanced

**31. Walk through the Docker architecture from CLI to process.**
The CLI calls the dockerd REST API; dockerd asks containerd (gRPC) to create and start the container; containerd spawns a shim per container; the shim invokes runc with an OCI bundle; runc creates namespaces, cgroups, mounts, applies seccomp/capabilities/LSM, execs the entrypoint, and exits. The shim stays as the parent, holding stdio and exit status.

**32. What is OCI and why does it matter?**
The Open Container Initiative defines the image, runtime and distribution specs. They make images portable across Docker, Podman, containerd and Kubernetes, and allow swapping runtimes like runc, crun, gVisor or Kata.

**33. Kubernetes removed dockershim. Do Docker-built images still work?**
Yes. Kubernetes uses CRI runtimes (containerd, CRI-O) that run any OCI image. Only using dockerd as the node runtime was removed.

**34. Explain how overlayfs implements image layers.**
Read-only lower directories (image layers) and a writable upper directory are merged into one view. Reads hit the topmost copy; writes to lower files trigger copy-up; deletions create whiteout files. That is why deleting a file in a later layer does not shrink an image.

**35. Which namespaces does a container use, and how would you enter them without docker exec?**
PID, NET, MNT, UTS, IPC, USER (optional), CGROUP. Find the PID with `docker inspect -f '{{.State.Pid}}'` and use `nsenter -t <pid> -n -m -p ...`, which lets you use host tools inside a minimal container.

**36. How do cgroups v2 enforce `--cpus` and `--memory`, and what is CPU throttling?**
`--cpus` writes a quota/period to `cpu.max`; once the group uses its quota within a period, it is throttled until the next period. `--memory` sets `memory.max`; exceeding it invokes the OOM killer within the cgroup. Throttling shows in `cpu.stat` (`nr_throttled`) and causes latency spikes for multi-threaded apps.

**37. How do you harden a container at runtime?**
Run as non-root (numeric UID), `--cap-drop ALL` plus minimal adds, `no-new-privileges`, keep default seccomp and AppArmor/SELinux or tighten them, `--read-only` with tmpfs for scratch, resource and PID limits, internal networks, no Docker socket, no `--privileged`, and user namespaces or rootless mode.

**38. Why is access to the Docker socket dangerous?**
The daemon runs as root and will start any container you ask for, including one that bind-mounts the host root filesystem with full privileges. Socket access equals root on the host.

**39. Rootless Docker vs userns-remap?**
Rootless runs the daemon and containers as an unprivileged user with user namespaces, so even the daemon is not root. userns-remap keeps a root daemon but maps container root to an unprivileged UID range. Both reduce breakout impact; rootless has more networking and feature limitations.

**40. How do you build multi-architecture images efficiently?**
`docker buildx build --platform linux/amd64,linux/arm64 --push`, producing an image index. Use `FROM --platform=$BUILDPLATFORM` and `TARGETOS`/`TARGETARCH` to cross-compile natively instead of emulating with QEMU, or use native builder nodes per architecture.

**41. How do you speed up Docker builds on ephemeral CI runners?**
Export and import BuildKit cache (`type=gha`, `type=registry,mode=max`, `s3`), use a persistent or remote builder (Build Cloud, BuildKit on Kubernetes), order layers well, use cache mounts, and keep the context small.

**42. What is an SBOM and how is it produced and used?**
A Software Bill of Materials listing every package and version in an image (SPDX or CycloneDX). Produce it with `docker buildx build --sbom=true` or Syft, attach it as an attestation, and scan it with Grype or Trivy to answer "where are we affected?" quickly when a CVE appears.

**43. How does keyless signing with cosign work?**
In CI, cosign obtains an OIDC token (e.g. from GitHub Actions), gets a short-lived certificate from Fulcio binding the workload identity, signs the image digest, and records the signature in the Rekor transparency log. Verification checks the certificate identity and issuer and the log entry; no long-lived keys to manage.

**44. A container is OOMKilled but its memory graph never reached the limit. Why?**
Metrics are sampled and may miss short spikes; the graph may show working set excluding page cache while the kernel counts some of it; tmpfs mounts count toward memory; or a child process spiked. Check `memory.events`, kernel logs, and `memory.stat`.

**45. Published port works locally but not from another host. Diagnose.**
Check the binding (`127.0.0.1:` vs `0.0.0.0`), the app's listen address inside the container, cloud security groups/NACLs, `DOCKER-USER` rules, and whether host firewalls (firewalld zones) block FORWARD. Use `tcpdump` on the host interface and `docker0`.

**46. How do you achieve zero-downtime deployments with containers?**
Run multiple replicas behind a load balancer with readiness checks, update gradually (start new before stopping old), drain connections with graceful SIGTERM handling, and keep backward-compatible database migrations. Swarm (`order: start-first`) and Kubernetes rolling updates automate it.

**47. How does Docker's embedded DNS work, and what is `ndots`?**
Containers on user-defined networks use 127.0.0.11, which the daemon intercepts to resolve container names, service names and aliases, forwarding other queries to upstream resolvers. `ndots` controls when search domains are appended; in Kubernetes the default `ndots:5` can cause many extra lookups for external names.

**48. Map Docker Compose concepts to Kubernetes.**
Service -> Deployment + Service; ports -> Service/Ingress; named volume -> PVC; config bind mount -> ConfigMap; secrets -> Secret; healthcheck -> liveness/readiness probes; resource limits -> requests/limits; `depends_on` -> init containers and retries; container network sharing -> Pod.

## 11.3 Command Cheat Sheet

### 11.3.1 Containers

```bash
docker run -d --name app -p 8080:80 -e K=V -v vol:/data --network net IMAGE
docker run --rm -it --entrypoint sh IMAGE
docker run --init --restart unless-stopped --memory 512m --cpus 1 --pids-limit 200 IMAGE
docker run --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges:true -u 10001 IMAGE
docker ps -a --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
docker logs -f --tail 100 --since 10m app
docker exec -it app sh          ;  docker exec -u 0 app id
docker cp app:/path ./local     ;  docker cp ./local app:/path
docker stop -t 30 app ; docker kill -s HUP app ; docker restart app
docker rm -f app ; docker container prune -f
docker inspect -f '{{.State.Status}} {{.State.ExitCode}} {{.State.OOMKilled}}' app
docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' app
docker stats --no-stream ; docker top app ; docker diff app ; docker port app
docker update --memory 1g --cpus 2 --restart on-failure:5 app
docker events --since 1h --filter type=container
docker wait app
```

### 11.3.2 Images and builds

```bash
docker build -t img:tag .                       # BuildKit
docker build -f Dockerfile.prod --target runtime --build-arg V=1 -t img:1 .
docker build --check .                          # lint
docker build --progress=plain --no-cache .
docker build --secret id=npmrc,src=$HOME/.npmrc --ssh default .
docker build --target export --output type=local,dest=./out .
docker buildx create --name b --driver docker-container --use --bootstrap
docker buildx build --platform linux/amd64,linux/arm64 -t reg/img:1 --push .
docker buildx build --cache-from type=registry,ref=reg/img:cache --cache-to type=registry,ref=reg/img:cache,mode=max .
docker buildx build --sbom=true --provenance=mode=max --push -t reg/img:1 .
docker buildx bake --push
docker buildx imagetools inspect reg/img:1
docker buildx imagetools create -t reg/img:prod reg/img@sha256:...
docker buildx du ; docker builder prune --keep-storage 10gb
docker image ls ; docker image ls --digests ; docker image history img
docker image inspect -f '{{json .Config}}' img | jq
docker tag img reg/img:1 ; docker push reg/img:1 ; docker pull --platform linux/arm64 img
docker image save -o img.tar img ; docker image load -i img.tar
docker image prune -a -f --filter until=168h
```

### 11.3.3 Volumes and networks

```bash
docker volume create v ; docker volume ls ; docker volume inspect v ; docker volume rm v
docker volume prune                      # anonymous unused
docker run --rm -v v:/data -v "$PWD":/b alpine tar czf /b/v.tgz -C /data .
docker network create --subnet 10.20.0.0/24 net
docker network create --internal backend
docker network connect net app ; docker network disconnect net app
docker network inspect net ; docker network ls ; docker network prune
docker run --rm --network container:app nicolaka/netshoot
docker run --rm --network host IMAGE ; docker run --rm --network none IMAGE
```

### 11.3.4 Compose

```bash
docker compose up -d --build --wait
docker compose -f compose.yaml -f compose.prod.yaml up -d --remove-orphans
docker compose --profile debug up -d       # or COMPOSE_PROFILES=debug
docker compose config                      # resolved model
docker compose ps ; docker compose logs -f api ; docker compose top
docker compose exec api sh ; docker compose run --rm api pytest
docker compose up -d --scale worker=3
docker compose watch
docker compose pull ; docker compose build --pull ; docker compose push
docker compose down ; docker compose down -v --rmi local
docker compose ls
```

### 11.3.5 System, daemon and contexts

```bash
docker version ; docker info
docker system df -v
docker system prune ; docker system prune -a --volumes   # careful
docker context create prod --docker host=ssh://user@host ; docker context use prod
docker login reg ; docker logout reg
journalctl -u docker -f
sudo kill -HUP "$(pidof dockerd)"                         # reload daemon.json (supported keys)
```

### 11.3.6 Security and supply chain

```bash
trivy image --severity HIGH,CRITICAL --ignore-unfixed --exit-code 1 img
trivy config Dockerfile ; trivy fs .
docker scout quickview img ; docker scout cves img ; docker scout recommendations img
syft img -o cyclonedx-json > sbom.json ; grype sbom:sbom.json
cosign sign --yes reg/img@sha256:...
cosign verify reg/img@sha256:... --certificate-identity-regexp '...' --certificate-oidc-issuer '...'
docker run --rm hadolint/hadolint < Dockerfile
```

### 11.3.7 Swarm

```bash
docker swarm init --advertise-addr IP ; docker swarm join-token worker
docker node ls ; docker node update --availability drain NODE
docker service create --name web --replicas 3 -p 80:80 nginx:1.27-alpine
docker service ls ; docker service ps web ; docker service logs -f web
docker service scale web=5 ; docker service update --image nginx:1.27.2-alpine web
docker service rollback web
printf 'x' | docker secret create s - ; docker stack deploy -c stack.yaml app ; docker stack rm app
```

### 11.3.8 Internals and debugging

```bash
PID=$(docker inspect -f '{{.State.Pid}}' app)
sudo nsenter -t "$PID" -n ss -tlnp
sudo nsenter -t "$PID" -m -u -i -n -p sh
lsns -p "$PID"
cat /sys/fs/cgroup/system.slice/docker-$(docker inspect -f '{{.Id}}' app).scope/memory.events
cat /sys/fs/cgroup/system.slice/docker-$(docker inspect -f '{{.Id}}' app).scope/cpu.stat
docker inspect -f '{{json .GraphDriver.Data}}' app | jq
sudo iptables -t nat -L DOCKER -n ; sudo iptables -L DOCKER-USER -n
```

### 11.3.9 Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Application error |
| 125 | Docker run error |
| 126 | Not executable |
| 127 | Not found |
| 137 | SIGKILL (OOM / kill / timeout) |
| 139 | SIGSEGV |
| 143 | SIGTERM |

## 11.4 Where to Go Next

- Kubernetes: Pods, Deployments, Services, Ingress/Gateway API, Helm, operators.
- Supply chain: SLSA levels, in-toto attestations, admission policies (Kyverno, policy-controller).
- Runtime security: Falco, Tetragon, gVisor and Kata for sandboxing.
- Platform engineering: remote builders, internal base image programs, golden paths.

## Key takeaways

- The capstone combines everything: multi-stage multi-arch distroless images, hardened runtime flags, Compose with healthchecks, profiles, secrets and internal networks, and a CI pipeline that tests, caches, scans, generates SBOMs, signs and promotes by digest.
- Production readiness is verifiable: write an acceptance checklist and test it with commands.
- Interviews reward depth: be ready to go from `docker run` down to containerd, runc, namespaces, cgroups and overlayfs.
- Keep the cheat sheet nearby; fluency with `inspect`, `logs`, `events`, `system df`, `nsenter` and `buildx` is what makes you fast in real incidents.
