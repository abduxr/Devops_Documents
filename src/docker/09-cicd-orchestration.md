# Part 9 — Docker in CI/CD, Swarm, the Path to Kubernetes, and Production Practices

Up to now you have built and run containers by hand. In real organizations, images are built by pipelines, deployed by orchestrators, and operated under production standards. This Part covers Docker in GitHub Actions, GitLab CI and Jenkins (including caching), Docker Swarm basics, how every Docker concept maps to Kubernetes, and a production best-practice checklist.

## 9.1 Principles of Container CI/CD

1. **Build once, promote everywhere.** Build an image once per commit, tag it with the Git SHA, and promote that exact digest through test, staging and production. Never rebuild for production.
2. **The pipeline is the only path to the registry.** Developers do not push production images from laptops.
3. **Gate on quality and security**: tests, lint (`docker build --check`, hadolint), vulnerability scan, then sign.
4. **Cache aggressively** so builds stay fast on ephemeral runners.
5. **Configuration lives outside the image**, injected per environment.

```text
 git push
   |
   v
 [lint] -> [build test stage + unit tests] -> [build & push image :sha-abc123 (cache)]
   -> [scan: Trivy/Scout gate] -> [SBOM + provenance + cosign sign]
   -> [integration tests with docker compose up --wait]
   -> [deploy to staging by digest] -> [smoke tests]
   -> [manual approval / tag v1.4.2] -> [retag digest as 1.4.2, deploy prod]
```

## 9.2 GitHub Actions

```yaml
# .github/workflows/ci.yaml
name: ci
on:
  push:
    branches: [main]
    tags: ["v*"]
  pull_request:

permissions:
  contents: read
  packages: write
  id-token: write        # for keyless cosign signing
  security-events: write # upload SARIF

env:
  IMAGE: ghcr.io/${{ github.repository }}

jobs:
  build:
    runs-on: ubuntu-latest
    outputs:
      digest: ${{ steps.build.outputs.digest }}
    steps:
      - uses: actions/checkout@v4

      - uses: docker/setup-qemu-action@v3
      - uses: docker/setup-buildx-action@v3

      - uses: docker/login-action@v3
        if: github.event_name != 'pull_request'
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - id: meta
        uses: docker/metadata-action@v5
        with:
          images: ${{ env.IMAGE }}
          tags: |
            type=sha,prefix=sha-
            type=ref,event=branch
            type=ref,event=pr
            type=semver,pattern={{version}}
            type=semver,pattern={{major}}.{{minor}}

      - name: Unit tests (test stage)
        uses: docker/build-push-action@v6
        with:
          context: .
          target: test
          cache-from: type=gha
          cache-to: type=gha,mode=max

      - id: build
        name: Build and push
        uses: docker/build-push-action@v6
        with:
          context: .
          target: runtime
          platforms: linux/amd64,linux/arm64
          push: ${{ github.event_name != 'pull_request' }}
          tags: ${{ steps.meta.outputs.tags }}
          labels: ${{ steps.meta.outputs.labels }}
          cache-from: type=gha
          cache-to: type=gha,mode=max
          sbom: true
          provenance: mode=max

      - name: Scan
        if: github.event_name != 'pull_request'
        uses: aquasecurity/trivy-action@0.28.0
        with:
          image-ref: ${{ env.IMAGE }}@${{ steps.build.outputs.digest }}
          severity: HIGH,CRITICAL
          ignore-unfixed: true
          exit-code: "1"
          format: sarif
          output: trivy.sarif

      - uses: github/codeql-action/upload-sarif@v3
        if: always() && github.event_name != 'pull_request'
        with:
          sarif_file: trivy.sarif

      - uses: sigstore/cosign-installer@v3
        if: github.event_name != 'pull_request'
      - name: Sign
        if: github.event_name != 'pull_request'
        run: cosign sign --yes "${IMAGE}@${{ steps.build.outputs.digest }}"
```

Notes:

- `type=gha` stores BuildKit cache in the GitHub Actions cache service; `mode=max` includes intermediate stages so the `test` and `runtime` stages share dependency layers.
- Pull requests from forks do not get secrets; the workflow builds but does not push.
- Third-party actions should be pinned to a commit SHA in security-sensitive repos (tags are shown here for readability).
- Downstream deploy jobs consume `needs.build.outputs.digest` so they deploy exactly what was scanned and signed.

## 9.3 GitLab CI

GitLab runners can build images in three ways:

| Approach | How | Trade-offs |
|---|---|---|
| Docker-in-Docker (dind) | `docker:27` job image + `docker:27-dind` service, privileged runner | Full Docker; requires privileged mode; cache is empty each job unless exported. |
| Socket binding | Mount host `/var/run/docker.sock` into the job | Fast, shared cache; jobs get root on the runner host and can see each other's containers. |
| Daemonless (BuildKit rootless, Kaniko successors, Buildah) | Build without a Docker daemon | No privileged mode; good for Kubernetes runners. |

```yaml
# .gitlab-ci.yml
stages: [build, scan, release]

variables:
  IMAGE: $CI_REGISTRY_IMAGE
  DOCKER_TLS_CERTDIR: "/certs"

build:
  stage: build
  image: docker:27
  services: [docker:27-dind]
  before_script:
    - echo "$CI_REGISTRY_PASSWORD" | docker login -u "$CI_REGISTRY_USER" --password-stdin "$CI_REGISTRY"
    - docker buildx create --use --driver docker-container
  script:
    - >
      docker buildx build
      --cache-from type=registry,ref=$IMAGE:buildcache
      --cache-to type=registry,ref=$IMAGE:buildcache,mode=max
      --provenance=mode=max --sbom=true
      -t $IMAGE:$CI_COMMIT_SHORT_SHA
      --push .

scan:
  stage: scan
  image:
    name: aquasec/trivy:0.56.2
    entrypoint: [""]
  variables:
    TRIVY_USERNAME: $CI_REGISTRY_USER
    TRIVY_PASSWORD: $CI_REGISTRY_PASSWORD
  script:
    - trivy image --exit-code 1 --severity HIGH,CRITICAL --ignore-unfixed $IMAGE:$CI_COMMIT_SHORT_SHA

release:
  stage: release
  image: docker:27
  rules:
    - if: $CI_COMMIT_TAG
  script:
    - echo "$CI_REGISTRY_PASSWORD" | docker login -u "$CI_REGISTRY_USER" --password-stdin "$CI_REGISTRY"
    - docker buildx imagetools create -t $IMAGE:$CI_COMMIT_TAG $IMAGE:$CI_COMMIT_SHORT_SHA
```

`imagetools create` retags in the registry without pulling, preserving multi-arch manifests: this is "build once, promote".

> **Note:** Kaniko, once the standard daemonless builder for GitLab on Kubernetes, was archived by Google in 2025. Prefer rootless BuildKit (`moby/buildkit:rootless` with `buildctl-daemonless.sh`), Buildah, or a remote BuildKit service.

## 9.4 Jenkins

```groovy
// Jenkinsfile (declarative)
pipeline {
  agent { label 'docker' }
  environment {
    REGISTRY = 'harbor.corp.example.com'
    IMAGE    = "${REGISTRY}/payments/api"
    TAG      = "${env.GIT_COMMIT.take(7)}"
  }
  options { timestamps() }
  stages {
    stage('Build') {
      steps {
        withCredentials([usernamePassword(credentialsId: 'harbor', usernameVariable: 'U', passwordVariable: 'P')]) {
          sh '''
            echo "$P" | docker login "$REGISTRY" -u "$U" --password-stdin
            docker buildx build \
              --cache-from type=registry,ref=$IMAGE:buildcache \
              --cache-to type=registry,ref=$IMAGE:buildcache,mode=max \
              -t $IMAGE:$TAG --push .
          '''
        }
      }
    }
    stage('Scan') {
      steps {
        sh 'trivy image --exit-code 1 --severity CRITICAL $IMAGE:$TAG'
      }
    }
    stage('Integration') {
      steps {
        sh '''
          docker compose -p ci-$BUILD_NUMBER -f compose.yaml -f compose.ci.yaml up -d --wait
          docker compose -p ci-$BUILD_NUMBER run --rm api pytest -q
        '''
      }
      post {
        always { sh 'docker compose -p ci-$BUILD_NUMBER down -v' }
      }
    }
  }
  post { always { sh 'docker logout $REGISTRY || true' } }
}
```

Jenkins tips: use dedicated build agents (or Kubernetes pod agents) rather than building on the controller, use the credentials store, clean up with `docker system prune -f --filter until=24h` in a scheduled job, and give each build a unique Compose project name.

## 9.5 CI Caching Strategies Compared

| Strategy | Speed | Works on ephemeral runners | Notes |
|---|---|---|---|
| Local builder cache on persistent runner | Fastest | No | Self-hosted runners; must prune. |
| `type=gha` | Fast | Yes | GitHub Actions only; 10 GB per repo limit (by default), eviction. |
| `type=registry,mode=max` | Good | Yes | Any CI; separate cache tag; needs registry storage. |
| `type=inline` | OK | Yes | Only final stage layers; easiest. |
| `type=s3` / `azblob` | Good | Yes | Cloud object storage. |
| Remote BuildKit / Docker Build Cloud | Fast | Yes | Shared cache across all pipelines and developers. |

Plus: order Dockerfile layers well, use cache mounts for package managers, and keep the build context small. Those matter more than any backend.

## 9.6 Docker Swarm Basics

Swarm mode is Docker's built-in orchestrator. It is simpler than Kubernetes, and still used for small clusters and edge deployments, but Kubernetes dominates the industry. Know the concepts.

| Concept | Meaning |
|---|---|
| Node | A Docker host in the swarm: manager (Raft consensus, scheduling) or worker. |
| Service | Desired state: image, replicas, ports, networks, update policy. |
| Task | One container scheduled for a service on a node. |
| Stack | A set of services deployed from a Compose file. |
| Routing mesh | Any node accepts traffic on a published port and load-balances to tasks via IPVS. |

```bash
# Manager
docker swarm init --advertise-addr 10.0.0.10
# Prints: docker swarm join --token SWMTKN-1-... 10.0.0.10:2377

# Workers
docker swarm join --token SWMTKN-1-... 10.0.0.10:2377

docker node ls
docker service create --name web --replicas 3 -p 80:80 \
  --update-parallelism 1 --update-delay 10s --update-failure-action rollback \
  nginx:1.27-alpine
docker service ls
docker service ps web
docker service scale web=5
docker service update --image nginx:1.27.2-alpine web    # rolling update
docker service rollback web
docker service logs -f web
```

Stacks reuse Compose files, with a `deploy:` section that Swarm honors fully:

```yaml
# stack.yaml
services:
  api:
    image: ghcr.io/acme/api:1.4.2
    networks: [appnet]
    secrets: [db_password]
    deploy:
      replicas: 3
      update_config: { parallelism: 1, delay: 10s, order: start-first, failure_action: rollback }
      restart_policy: { condition: on-failure }
      placement:
        constraints: ["node.role == worker"]
      resources:
        limits: { cpus: "0.5", memory: 256M }
networks:
  appnet:
    driver: overlay
secrets:
  db_password:
    external: true
```

```bash
printf 's3cr3t' | docker secret create db_password -
docker stack deploy -c stack.yaml shop
docker stack services shop
docker stack rm shop
```

Run 3 or 5 managers for fault tolerance (Raft needs a majority: 3 managers tolerate 1 failure, 5 tolerate 2).

## 9.7 The Path to Kubernetes

Kubernetes runs OCI images through a CRI runtime (containerd or CRI-O), not through dockerd. Your Docker skills transfer almost one to one.

| Docker / Compose | Kubernetes | Notes |
|---|---|---|
| Image | Image | Same OCI image, same registries. |
| Container | Container | Inside a Pod. |
| `--network container:x` / shared netns | Pod | Containers in a Pod share network (and optionally PID) namespaces. |
| Compose service | Deployment + Service | Deployment manages replicas; Service gives a stable virtual IP and DNS name. |
| `deploy.replicas`, `--scale` | `spec.replicas`, HPA | HorizontalPodAutoscaler adds metric-driven scaling. |
| `ports: "8080:80"` | Service (ClusterIP/NodePort/LoadBalancer), Ingress/Gateway | Ingress or Gateway API for HTTP routing and TLS. |
| User-defined network + DNS | Cluster network + CoreDNS | `api.default.svc.cluster.local`; NetworkPolicy for isolation. |
| Named volume | PersistentVolumeClaim | Backed by CSI drivers (EBS, PD, Azure Disk, Ceph). |
| Bind mount of config | ConfigMap | Mounted as files or env. |
| Compose/Swarm secret | Secret | Base64, not encrypted by default; enable encryption at rest or use External Secrets. |
| `HEALTHCHECK` | livenessProbe, readinessProbe, startupProbe | Kubernetes ignores Dockerfile HEALTHCHECK. |
| `depends_on` | initContainers, readiness probes, retries | No direct equivalent; design for retries. |
| `--memory`, `--cpus` | `resources.limits` and `resources.requests` | Requests drive scheduling; limits enforce cgroups. |
| `--restart` | `restartPolicy` + controller | Deployments always restart; Jobs for run-to-completion. |
| `ENTRYPOINT` / `CMD` | `command` / `args` | `command` overrides ENTRYPOINT, `args` overrides CMD. |
| `--user`, `--cap-drop`, `--read-only` | `securityContext` | `runAsNonRoot`, `capabilities.drop: [ALL]`, `readOnlyRootFilesystem`. |
| `docker logs`, `exec` | `kubectl logs`, `kubectl exec` | `kubectl debug` for ephemeral debug containers. |
| Compose migration migrate job | Job / Helm hook | |
| Compose file | Manifests, Helm chart, Kustomize | Kompose can convert Compose to manifests as a starting point. |

The same API container, translated:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
spec:
  replicas: 3
  selector:
    matchLabels: { app: api }
  template:
    metadata:
      labels: { app: api }
    spec:
      securityContext:
        runAsNonRoot: true
        runAsUser: 10001
        seccompProfile: { type: RuntimeDefault }
      containers:
        - name: api
          image: ghcr.io/acme/api@sha256:5e1f...
          ports: [{ containerPort: 8080 }]
          envFrom: [{ configMapRef: { name: api-config } }]
          resources:
            requests: { cpu: 250m, memory: 256Mi }
            limits: { memory: 512Mi }
          readinessProbe:
            httpGet: { path: /healthz, port: 8080 }
          livenessProbe:
            httpGet: { path: /healthz, port: 8080 }
            initialDelaySeconds: 10
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities: { drop: [ALL] }
          volumeMounts: [{ name: tmp, mountPath: /tmp }]
      volumes:
        - name: tmp
          emptyDir: { medium: Memory, sizeLimit: 64Mi }
---
apiVersion: v1
kind: Service
metadata:
  name: api
spec:
  selector: { app: api }
  ports: [{ port: 80, targetPort: 8080 }]
```

For local Kubernetes, use kind (Kubernetes in Docker), k3d, minikube, or Docker Desktop's built-in cluster. `kind load docker-image myapp:dev` copies a locally built image into the cluster nodes without a registry.

## 9.8 Production Best Practices

### 9.8.1 Image

- Small, pinned, regularly rebuilt base; multi-stage; non-root numeric UID; no secrets.
- OCI labels with source repo and commit SHA.
- One process per container (sidecars for helpers), exec-form entrypoint, handles SIGTERM, `--init` or tini if it spawns children.

### 9.8.2 Application behavior

- Log JSON to stdout/stderr.
- Read configuration from env and files; no environment-specific images.
- Health endpoints: liveness ("process is not wedged") separate from readiness ("can serve traffic now").
- Graceful shutdown: on SIGTERM stop accepting new work, finish in-flight requests within the grace period, exit 0.
- Stateless where possible; state in databases, object storage or volumes.
- Retry with backoff for dependencies.

### 9.8.3 Runtime and operations

- Resource limits on every container; size from observed metrics.
- Restart policy (`unless-stopped`) or orchestrator controller.
- Log rotation (`local` driver) and central shipping.
- Metrics (cAdvisor/Prometheus), alerting on restarts, OOMs, throttling, disk.
- Deploy by digest; keep previous digests for instant rollback.
- Separate `/var/lib/docker` onto its own disk or volume to protect the root filesystem.
- Scheduled cleanup (`docker system prune -f --filter until=72h`, `docker builder prune`).
- Use `live-restore` so Engine upgrades do not restart containers.
- Back up volumes and test restores.

### 9.8.4 Zero-downtime deploy on a single host

Compose recreates containers, causing a brief gap. Options: run two replicas behind a reverse proxy (Traefik, Caddy, nginx) and update one at a time; use Swarm's `order: start-first`; or graduate to Kubernetes rolling updates.

## 9.9 Lab: A GitHub Actions Pipeline With Cache, Scan and Sign

**Goal:** wire the Python API from Part 3 into a real pipeline.

1. Push the `pyapi` project to a new GitHub repository. Add `test` and `runtime` stages to the Dockerfile (the test stage installs `pytest` and runs a one-line test that imports `app`).

2. Add the workflow from section 9.2 at `.github/workflows/ci.yaml`.

3. Open a pull request. Confirm the build and test stages run, but nothing is pushed.

4. Merge to `main`. Confirm in the Actions log:
   - `importing cache manifest from gha` on the second run, and `CACHED` steps.
   - Trivy SARIF results appear under the repository's Security tab.
   - `cosign sign` succeeds with a Fulcio certificate for the workflow identity.

5. Locally verify the signature:

```bash
cosign verify ghcr.io/<you>/pyapi@sha256:<digest> \
  --certificate-identity-regexp "https://github.com/<you>/pyapi/.*" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com | jq '.[0].optional.Subject'
```

6. Tag `v0.1.0` and confirm the semver tags `0.1.0` and `0.1` point to the same digest:

```bash
docker buildx imagetools inspect ghcr.io/<you>/pyapi:0.1.0 --format '{{.Manifest.Digest}}'
docker buildx imagetools inspect ghcr.io/<you>/pyapi:sha-<short> --format '{{.Manifest.Digest}}'
```

7. Introduce a vulnerable dependency (an old version of a package with a known HIGH CVE) and confirm the scan gate fails the pipeline. Revert.

8. Optional: deploy the digest to a kind cluster with the manifest from section 9.7.

## Key takeaways

- Build once per commit, tag with the SHA, promote the same digest; retag with `imagetools create` rather than rebuilding.
- In GitHub Actions use `setup-buildx`, `metadata-action`, `build-push-action` with `type=gha` cache, then scan and sign by digest.
- GitLab and Jenkins builds use dind, socket binding, or daemonless BuildKit; know the security trade-offs of each.
- Swarm offers services, stacks, overlay networks, secrets and rolling updates with a Compose-like file.
- Kubernetes maps Docker concepts directly: Pod = shared namespaces, Deployment + Service = Compose service, probes = healthchecks, securityContext = run flags.
- Production readiness is a checklist: limits, health, graceful shutdown, logs, metrics, digests, cleanup, backups.
