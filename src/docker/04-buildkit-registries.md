# Part 4 — BuildKit, Image Optimization and Registries

Part 3 taught you to write correct Dockerfiles. This Part makes builds fast, images small, and distribution professional: BuildKit features (cache mounts, secrets, SSH, buildx, remote cache, multi-platform), image minimization strategies (slim, alpine, distroless, scratch), and tagging and registry workflows across Docker Hub, ECR, GCR/Artifact Registry, ACR, GHCR and Harbor.

## 4.1 BuildKit

BuildKit has been the default builder since Docker Engine 23.0. `docker build` is now an alias for `docker buildx build`. Its advantages over the legacy builder:

- Builds a dependency graph (DAG) and executes independent stages in parallel.
- Skips stages the target does not need.
- Content-addressable cache that can be exported to and imported from registries or CI caches.
- `RUN --mount` for caches, secrets, SSH agents and bind mounts.
- Multi-platform builds and multiple output types.

### 4.1.1 Cache mounts

A cache mount persists a directory across builds **without** baking it into a layer. Package managers keep their download caches warm even when the layer itself is invalidated.

```dockerfile
# syntax=docker/dockerfile:1
FROM python:3.12-slim
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

FROM golang:1.23 AS build
RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    go build -o /out/app ./...

FROM debian:bookworm-slim
RUN rm -f /etc/apt/apt.conf.d/docker-clean
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends curl
```

| Ecosystem | Cache target |
|---|---|
| pip | `/root/.cache/pip` |
| uv | `/root/.cache/uv` |
| npm | `/root/.npm` |
| yarn | `/usr/local/share/.cache/yarn` |
| pnpm | `/root/.local/share/pnpm/store` |
| Go | `/go/pkg/mod`, `/root/.cache/go-build` |
| Maven | `/root/.m2` |
| Gradle | `/root/.gradle` |
| Cargo | `/usr/local/cargo/registry`, `/app/target` |
| apt | `/var/cache/apt`, `/var/lib/apt` (with `sharing=locked`) |

> **Note:** Cache mounts live in the builder's local cache. On ephemeral CI runners they start empty unless you persist the builder state; layer cache export (section 4.1.5) is usually the bigger CI win.

### 4.1.2 Secret mounts

Secrets are mounted as files (or env vars) only during a single `RUN`, never written into a layer or history.

```dockerfile
# syntax=docker/dockerfile:1
FROM node:22-alpine
WORKDIR /app
COPY package.json package-lock.json ./
RUN --mount=type=secret,id=npmrc,target=/root/.npmrc npm ci
RUN --mount=type=secret,id=api_token,env=API_TOKEN ./fetch-assets.sh
```

```bash
docker build --secret id=npmrc,src=$HOME/.npmrc \
             --secret id=api_token,env=API_TOKEN \
             -t app .
```

Compare with the anti-pattern `ARG NPM_TOKEN` which ends up readable via `docker history`.

### 4.1.3 SSH mounts

To clone private Git repositories during a build without copying keys:

```dockerfile
RUN --mount=type=ssh git clone git@github.com:acme/private-lib.git /src/lib
```

```bash
eval "$(ssh-agent)" && ssh-add ~/.ssh/id_ed25519
docker build --ssh default .
```

Remember to add the host key (`ssh-keyscan github.com >> /etc/ssh/ssh_known_hosts` in an earlier step) so the clone does not prompt.

### 4.1.4 Bind mounts in RUN

```dockerfile
RUN --mount=type=bind,source=requirements.txt,target=/tmp/requirements.txt \
    pip install -r /tmp/requirements.txt
```

The file is available during that step but not copied into a layer. Useful for files only needed at build time.

### 4.1.5 buildx, builders and drivers

`docker buildx` manages builder instances:

```bash
docker buildx ls
docker buildx create --name ci --driver docker-container --use --bootstrap
docker buildx inspect ci
docker buildx du               # builder cache usage
docker buildx prune --keep-storage 20gb
```

| Driver | Description |
|---|---|
| `docker` | Built into the daemon. Simple, but limited cache export and multi-platform (unless containerd image store is enabled). |
| `docker-container` | BuildKit in a container. Full features: all cache backends, multi-platform. |
| `kubernetes` | BuildKit pods in a cluster; scales builds horizontally. |
| `remote` | Connect to an existing BuildKit daemon. |
| Docker Build Cloud | Managed remote builders with shared cache (subscription). |

### 4.1.6 Remote cache backends

On ephemeral CI runners the local cache is empty every run. Export the cache somewhere persistent:

```bash
# Registry cache (works anywhere)
docker buildx build \
  --cache-from type=registry,ref=ghcr.io/acme/api:buildcache \
  --cache-to   type=registry,ref=ghcr.io/acme/api:buildcache,mode=max \
  -t ghcr.io/acme/api:1.4.2 --push .

# GitHub Actions cache
docker buildx build --cache-from type=gha --cache-to type=gha,mode=max .

# Inline cache (metadata embedded in the pushed image, min mode only)
docker buildx build --cache-to type=inline -t ghcr.io/acme/api:1.4.2 --push .
```

`mode=min` caches only layers of the final image; `mode=max` caches all intermediate stages too, which matters for multi-stage builds. Other backends: `type=local`, `type=s3`, `type=azblob`.

### 4.1.7 Multi-platform builds

ARM servers (AWS Graviton, Ampere) and Apple Silicon laptops make multi-architecture images a normal requirement.

Two strategies:

1. **Emulation (QEMU)**: simple, slow for compile-heavy builds.
2. **Cross-compilation**: build on the native platform and target another. Fast; requires language support (Go, Rust, many others).

```bash
# One-time: register QEMU emulators (Docker Desktop has them built in)
docker run --privileged --rm tonistiigi/binfmt --install all

docker buildx build --platform linux/amd64,linux/arm64 \
  -t ghcr.io/acme/api:1.4.2 --push .
```

Cross-compiling Go:

```dockerfile
# syntax=docker/dockerfile:1
FROM --platform=$BUILDPLATFORM golang:1.23 AS build
ARG TARGETOS TARGETARCH
WORKDIR /src
COPY . .
RUN --mount=type=cache,target=/go/pkg/mod \
    CGO_ENABLED=0 GOOS=$TARGETOS GOARCH=$TARGETARCH go build -o /out/api ./cmd/api

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/api /api
ENTRYPOINT ["/api"]
```

The build stage always runs natively (`$BUILDPLATFORM`); only the tiny final stage is per-platform. Result: an image index with one manifest per architecture, and each host pulls the right one automatically.

> **Tip:** With the classic image store, a multi-platform result cannot be loaded into the local `docker images` list; you push it. With the containerd image store enabled, you can `--load` multi-platform images locally.

### 4.1.8 Outputs, attestations and Bake

```bash
docker buildx build -o type=local,dest=./out .            # files to disk
docker buildx build -o type=tar,dest=out.tar .
docker buildx build -o type=oci,dest=app-oci.tar .
docker buildx build --load -t app:dev .                   # into local image store
docker buildx build --push -t ghcr.io/acme/app:1.0 .
docker buildx build --sbom=true --provenance=mode=max --push -t ghcr.io/acme/app:1.0 .
```

`--sbom` and `--provenance` attach SBOM and SLSA provenance attestations to the image (Part 8).

**Bake** defines builds declaratively, like Compose for builds:

```text
# docker-bake.hcl
variable "TAG" { default = "dev" }

group "default" { targets = ["api", "web"] }

target "api" {
  context    = "./api"
  tags       = ["ghcr.io/acme/api:${TAG}"]
  platforms  = ["linux/amd64", "linux/arm64"]
  cache-from = ["type=registry,ref=ghcr.io/acme/api:buildcache"]
  cache-to   = ["type=registry,ref=ghcr.io/acme/api:buildcache,mode=max"]
}

target "web" {
  context = "./web"
  tags    = ["ghcr.io/acme/web:${TAG}"]
}
```

```bash
TAG=1.4.2 docker buildx bake --push
docker buildx bake --print     # show resolved config
```

## 4.2 Image Optimization

### 4.2.1 Why size matters

- Faster pulls mean faster scaling and faster deploys, especially on cold nodes.
- Fewer packages mean fewer CVEs and a smaller attack surface.
- Less storage and registry egress cost.

### 4.2.2 Base image choices

| Base | Approx size | Has shell / package manager | libc | Notes |
|---|---|---|---|---|
| `ubuntu:24.04` | ~78 MB | yes / apt | glibc | Familiar, large. |
| `debian:bookworm-slim` | ~75 MB | yes / apt | glibc | Good general default. |
| `python:3.12-slim` | ~125 MB | yes / apt | glibc | Language-specific slim. |
| `alpine:3.20` | ~8 MB | yes / apk | musl | Tiny; musl can break or slow some native wheels and DNS edge cases. |
| `gcr.io/distroless/base-debian12` | ~20 MB | no / no | glibc | Runtime-only, has CA certs, tzdata. |
| `gcr.io/distroless/static-debian12` | ~2 MB | no / no | none | For static binaries. |
| Chainguard / Wolfi images | small | variants with and without shell | glibc | Minimal, frequently rebuilt, low CVE counts. |
| Docker Hardened Images | small | minimal | varies | Docker's hardened, low-CVE catalog. |
| `scratch` | 0 MB | no / no | none | Empty; you supply everything. |

### 4.2.3 Distroless and scratch

**Distroless** images contain only your app's runtime dependencies: no shell, no package manager, no coreutils. Attackers who get code execution have no tools, and scanners have little to flag. `:nonroot` tags default to UID 65532.

**scratch** is an empty image. For a statically linked binary you need to bring anything else yourself:

```dockerfile
FROM golang:1.23-alpine AS build
RUN apk add --no-cache ca-certificates tzdata
WORKDIR /src
COPY . .
RUN CGO_ENABLED=0 go build -ldflags="-s -w" -o /app .

FROM scratch
COPY --from=build /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/
COPY --from=build /usr/share/zoneinfo /usr/share/zoneinfo
COPY --from=build /app /app
USER 65532:65532
ENTRYPOINT ["/app"]
```

Common scratch gotchas: missing CA certificates (TLS fails with "x509: certificate signed by unknown authority"), missing timezone data, no `/etc/passwd` (so use numeric UID), no `/tmp` (mount a tmpfs), and dynamically linked binaries failing with a confusing "no such file or directory" because the loader is absent.

### 4.2.4 Optimization techniques summary

1. Multi-stage builds; copy only artifacts.
2. Choose the smallest base that works; prefer distroless for runtime.
3. `--no-install-recommends` (apt), `apk add --no-cache`, `pip --no-cache-dir` or cache mounts.
4. Remove build deps and caches in the same `RUN`.
5. Install only production dependencies (`npm ci --omit=dev`, `poetry install --only main`).
6. Strip binaries (`-ldflags="-s -w"`).
7. Keep `.dockerignore` tight.
8. Analyze layers with `dive`:

```bash
docker run --rm -it -v /var/run/docker.sock:/var/run/docker.sock wagoodman/dive:latest myapp:1.0
```

`dive` shows each layer's files and an "efficiency score", highlighting wasted space from files overwritten or deleted in later layers.

## 4.3 Tagging Strategy

Tags should communicate meaning and support rollbacks.

| Tag | Example | Mutable? | Use |
|---|---|---|---|
| Semantic version | `1.4.2` | Should be immutable | Releases. |
| Minor/major float | `1.4`, `1` | Mutable | Consumers that want patches automatically. |
| Git SHA | `sha-3f9c2ab` | Immutable | Traceability from CI; deploy exactly what was tested. |
| Branch | `main`, `pr-123` | Mutable | Preview environments. |
| `latest` | `latest` | Mutable | Convenience only; never deploy it. |

A typical CI produces several tags for the same digest:

```bash
docker buildx build \
  -t ghcr.io/acme/api:1.4.2 \
  -t ghcr.io/acme/api:1.4 \
  -t ghcr.io/acme/api:sha-3f9c2ab \
  --push .
```

Deploy by digest for maximum safety:

```bash
docker buildx imagetools inspect ghcr.io/acme/api:1.4.2 --format '{{.Manifest.Digest}}'
# deploy: ghcr.io/acme/api@sha256:5e1f...
```

## 4.4 Registries

### 4.4.1 Docker Hub

```bash
docker login                     # interactive, or use a personal access token
echo "$DOCKERHUB_TOKEN" | docker login -u myuser --password-stdin
docker tag api:1.4.2 myuser/api:1.4.2
docker push myuser/api:1.4.2
```

> **Warning:** Docker Hub enforces pull rate limits for unauthenticated and free users. CI systems pulling public images anonymously from shared IPs hit limits quickly. Authenticate, use a pull-through cache, or mirror base images into your own registry.

Credentials are stored in `~/.docker/config.json`, ideally via a credential helper (`osxkeychain`, `desktop`, `pass`, `secretservice`, `ecr-login`) rather than base64 plaintext.

### 4.4.2 AWS ECR

```bash
aws ecr create-repository --repository-name api \
  --image-scanning-configuration scanOnPush=true \
  --image-tag-mutability IMMUTABLE

aws ecr get-login-password --region eu-west-1 \
  | docker login --username AWS --password-stdin 123456789012.dkr.ecr.eu-west-1.amazonaws.com

docker tag api:1.4.2 123456789012.dkr.ecr.eu-west-1.amazonaws.com/api:1.4.2
docker push 123456789012.dkr.ecr.eu-west-1.amazonaws.com/api:1.4.2
```

ECR tips: tokens last 12 hours (or use `amazon-ecr-credential-helper`), configure **lifecycle policies** to expire untagged and old images, use **pull-through cache rules** for Docker Hub/GHCR/Quay, and enable tag immutability.

```json
{
  "rules": [
    {
      "rulePriority": 1,
      "description": "Expire untagged after 7 days",
      "selection": { "tagStatus": "untagged", "countType": "sinceImagePushed", "countUnit": "days", "countNumber": 7 },
      "action": { "type": "expire" }
    }
  ]
}
```

### 4.4.3 Google Artifact Registry (successor to GCR)

Google Container Registry has been replaced by **Artifact Registry**.

```bash
gcloud artifacts repositories create apps --repository-format=docker --location=europe-west1
gcloud auth configure-docker europe-west1-docker.pkg.dev
docker tag api:1.4.2 europe-west1-docker.pkg.dev/my-project/apps/api:1.4.2
docker push europe-west1-docker.pkg.dev/my-project/apps/api:1.4.2
```

### 4.4.4 Azure Container Registry

```bash
az acr create -n acmeregistry -g rg-platform --sku Standard
az acr login -n acmeregistry
docker tag api:1.4.2 acmeregistry.azurecr.io/api:1.4.2
docker push acmeregistry.azurecr.io/api:1.4.2
az acr build -r acmeregistry -t api:1.4.2 .   # build in Azure, no local Docker needed
```

### 4.4.5 GitHub Container Registry (GHCR)

```bash
echo "$GITHUB_TOKEN" | docker login ghcr.io -u "$GITHUB_ACTOR" --password-stdin
docker push ghcr.io/acme/api:1.4.2
```

In GitHub Actions, the built-in `GITHUB_TOKEN` with `packages: write` permission is enough.

### 4.4.6 Harbor

Harbor is a CNCF-graduated, self-hosted registry adding projects with RBAC, vulnerability scanning (Trivy), replication between registries, signature verification policies, quotas, retention rules and proxy caching. It is a common choice for on-prem and air-gapped environments.

```bash
docker login harbor.corp.example.com
docker tag api:1.4.2 harbor.corp.example.com/payments/api:1.4.2
docker push harbor.corp.example.com/payments/api:1.4.2
```

### 4.4.7 Running a private registry

The open-source `registry` image (CNCF Distribution) is enough for labs and simple setups:

```bash
docker run -d --name registry -p 5000:5000 \
  -v registry-data:/var/lib/registry \
  --restart unless-stopped registry:2

docker tag alpine:3.20 localhost:5000/alpine:3.20
docker push localhost:5000/alpine:3.20
curl -s localhost:5000/v2/_catalog
curl -s localhost:5000/v2/alpine/tags/list
```

```text
{"repositories":["alpine"]}
{"name":"alpine","tags":["3.20"]}
```

For anything beyond localhost, you need TLS and authentication. Docker refuses plain-HTTP registries other than `localhost` unless you add them to `insecure-registries` in `daemon.json` (do not do this in production). A production-ish setup uses htpasswd auth and certificates:

```bash
mkdir -p auth certs
docker run --rm --entrypoint htpasswd httpd:2 -Bbn admin 'S3cure!' > auth/htpasswd
docker run -d --name registry -p 443:5000 \
  -v "$PWD/auth:/auth" -v "$PWD/certs:/certs" -v registry-data:/var/lib/registry \
  -e REGISTRY_AUTH=htpasswd -e REGISTRY_AUTH_HTPASSWD_REALM="Registry" \
  -e REGISTRY_AUTH_HTPASSWD_PATH=/auth/htpasswd \
  -e REGISTRY_HTTP_TLS_CERTIFICATE=/certs/domain.crt \
  -e REGISTRY_HTTP_TLS_KEY=/certs/domain.key \
  registry:2
```

Configure it as a Docker Hub pull-through mirror by setting `REGISTRY_PROXY_REMOTEURL=https://registry-1.docker.io` and adding `"registry-mirrors": ["https://mirror.corp.example.com"]` to clients' `daemon.json`.

Deleting images from the registry requires `REGISTRY_STORAGE_DELETE_ENABLED=true`, a DELETE by digest via the API, then running garbage collection (`registry garbage-collect /etc/docker/registry/config.yml`).

### 4.4.8 Copying images between registries

Do not `pull`/`tag`/`push` through your laptop for large or multi-arch images. Use registry-to-registry tools:

```bash
docker buildx imagetools create -t ghcr.io/acme/api:1.4.2 docker.io/acme/api:1.4.2
crane copy docker.io/library/nginx:1.27-alpine harbor.corp.example.com/mirror/nginx:1.27-alpine
skopeo copy --all docker://nginx:1.27-alpine docker://registry.local/nginx:1.27-alpine
```

These preserve all platforms in the index.

## 4.5 Lab: Multi-Platform Build to a Local Registry With Remote Cache

**Goal:** build a Go service for amd64 and arm64, push to a local registry, and reuse cache across builder instances.

1. Start a local registry on a network the builder can reach:

```bash
docker network create buildnet
docker run -d --name registry --network buildnet -p 5000:5000 registry:2
```

2. Create the app:

```bash
mkdir -p ~/labs/gohello && cd ~/labs/gohello
cat > main.go <<'EOF'
package main

import (
	"fmt"
	"net/http"
	"runtime"
)

func main() {
	http.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprintf(w, "hello from %s/%s\n", runtime.GOOS, runtime.GOARCH)
	})
	http.ListenAndServe(":8080", nil)
}
EOF
cat > go.mod <<'EOF'
module gohello

go 1.23
EOF
```

3. Write the cross-compiling Dockerfile from section 4.1.7 (adjust `./cmd/api` to `.`).

4. Create a `docker-container` builder attached to the network, allowing HTTP to the lab registry:

```bash
cat > buildkitd.toml <<'EOF'
[registry."registry:5000"]
  http = true
EOF
docker buildx create --name labbuilder --driver docker-container \
  --driver-opt network=buildnet --config buildkitd.toml --use --bootstrap
```

5. Build and push both platforms with registry cache:

```bash
docker buildx build --platform linux/amd64,linux/arm64 \
  -t registry:5000/gohello:1.0 \
  --cache-to type=registry,ref=registry:5000/gohello:cache,mode=max \
  --cache-from type=registry,ref=registry:5000/gohello:cache \
  --push .
curl -s -H 'Accept: application/vnd.oci.image.index.v1+json' \
  localhost:5000/v2/gohello/manifests/1.0 | jq '.manifests[].platform'
```

You should see entries for `amd64` and `arm64` (plus `unknown/unknown` entries, which are attestation manifests).

6. Remove the builder (and its local cache), recreate it, and rebuild. Observe `CACHED` steps imported from the registry:

```bash
docker buildx rm labbuilder
docker buildx create --name labbuilder --driver docker-container \
  --driver-opt network=buildnet --config buildkitd.toml --use --bootstrap
# rerun the build command from step 5
```

7. Run each platform (emulated if necessary):

```bash
docker run --rm -d -p 8081:8080 --platform linux/arm64 --name a localhost:5000/gohello:1.0
docker run --rm -d -p 8082:8080 --platform linux/amd64 --name b localhost:5000/gohello:1.0
curl localhost:8081; curl localhost:8082
```

```text
hello from linux/arm64
hello from linux/amd64
```

8. Clean up: `docker rm -f a b registry`, `docker buildx rm labbuilder`, `docker network rm buildnet`.

## Key takeaways

- BuildKit is the default builder: use `RUN --mount=type=cache` for package caches, `type=secret` and `type=ssh` for credentials.
- Use a `docker-container` builder plus registry or GHA cache export to make CI builds fast.
- Prefer cross-compilation with `$BUILDPLATFORM`/`$TARGETARCH` over QEMU emulation for multi-arch images.
- Distroless and scratch shrink both size and attack surface; remember CA certs, tzdata and numeric UIDs.
- Tag with semver and Git SHA, never deploy `latest`, and deploy by digest when you can.
- Every cloud has an OCI registry; enable scanning, immutability and lifecycle policies, and mirror Docker Hub to avoid rate limits.
