# Part 8 — Container Security

Containers are not a security boundary by default, but they can be made into a strong one with layered controls. This Part follows the container lifecycle: secure the **host and daemon**, secure the **image** (minimal, scanned, signed, with an SBOM), secure the **runtime** (non-root, capabilities, seccomp, AppArmor/SELinux, read-only filesystem, limits), and handle **secrets** correctly.

```text
 Supply chain           Build                  Registry               Runtime
 +-------------+   +----------------+   +--------------------+   +---------------------+
 | Pinned base |-->| Multi-stage,   |-->| Scan on push,      |-->| non-root, cap-drop, |
 | images,     |   | no secrets in  |   | sign (cosign),     |   | seccomp, AppArmor,  |
 | deps lockfile|  | layers, SBOM   |   | immutable tags     |   | read-only, limits   |
 +-------------+   +----------------+   +--------------------+   +---------------------+
                                       verify signature before deploy
```

## 8.1 Threat Model in One Table

| Threat | Example | Primary controls |
|---|---|---|
| Vulnerable packages | Old OpenSSL in base image | Minimal bases, scanning, regular rebuilds |
| Malicious image | Typosquatted image on Docker Hub | Trusted sources, signing and verification |
| Secret leakage | API key in a layer or `ENV` | Secret mounts, runtime secrets, scanning |
| Container breakout | Kernel exploit, privileged container | Non-root, userns/rootless, seccomp, capabilities, patching |
| Lateral movement | Compromised web container reaching the DB | Network segmentation, internal networks |
| Resource exhaustion | Fork bomb, memory leak | cgroup limits, pids limit |
| Daemon takeover | Exposed Docker socket or TCP API | Never expose socket, TLS for remote API, rootless |

## 8.2 Securing the Host and Daemon

- Keep the kernel, Docker Engine, containerd and runc patched. Several historical breakouts were runc or kernel bugs (for example CVE-2019-5736, CVE-2024-21626 "Leaky Vessels").
- **Never** expose the Docker API on TCP without mutual TLS (`tcp://0.0.0.0:2375` is a classic way to get cryptominers). Prefer SSH contexts.
- Do not mount `/var/run/docker.sock` into containers unless you fully trust them; it is root on the host. If a tool needs it, consider a socket proxy that allows only read endpoints.
- Restrict the `docker` group. Consider **rootless mode** or **userns-remap**.
- Run the **CIS Docker Benchmark** checks:

```bash
docker run --rm --net host --pid host --userns host --cap-add audit_control \
  -v /etc:/etc:ro -v /var/lib:/var/lib:ro -v /var/run/docker.sock:/var/run/docker.sock:ro \
  -v /usr/lib/systemd:/usr/lib/systemd:ro \
  docker/docker-bench-security
```

```text
[WARN] 2.2  - Ensure network traffic is restricted between containers on the default bridge
[PASS] 2.14 - Ensure Userland Proxy is Disabled
[WARN] 4.1  - Ensure that a user for the container has been created
[WARN] 5.10 - Ensure that the memory usage for containers is limited
...
```

Hardened daemon settings:

```json
{
  "icc": false,
  "no-new-privileges": true,
  "live-restore": true,
  "userland-proxy": false,
  "userns-remap": "default",
  "log-driver": "local",
  "log-opts": { "max-size": "10m", "max-file": "3" }
}
```

`"icc": false` disables inter-container communication on the default bridge (user-defined networks are controlled by network membership).

## 8.3 Securing Images

### 8.3.1 Minimal, pinned, maintained bases

- Use official, verified, or hardened images (Docker Official Images, Docker Hardened Images, Chainguard, distroless).
- Pin versions and ideally digests: `FROM python:3.12.7-slim-bookworm@sha256:...`. Use Renovate or Dependabot to bump them automatically so pinning does not mean stagnating.
- Rebuild regularly (weekly, or on base image update) to pick up patches even when your code has not changed.

### 8.3.2 Never bake secrets

```dockerfile
# BAD: visible in docker history and in the layer forever
ARG GITHUB_TOKEN
ENV API_KEY=abc123
COPY id_rsa /root/.ssh/id_rsa

# GOOD: available only during this RUN
RUN --mount=type=secret,id=github_token \
    GITHUB_TOKEN=$(cat /run/secrets/github_token) ./fetch-private-deps.sh
```

Prove a leak to yourself:

```bash
docker history --no-trunc badimage | grep -i token
docker save badimage | tar -xO --wildcards '*/layer.tar' 2>/dev/null | strings | grep -i 'BEGIN OPENSSH'
```

Scan for secrets in images and repositories with Trivy (`--scanners secret`), gitleaks or trufflehog.

### 8.3.3 Vulnerability scanning with Trivy

Trivy (Aqua Security, open source) scans images, filesystems, repositories, IaC and Kubernetes for CVEs, misconfigurations, secrets and licenses.

```bash
trivy image nginx:1.27-alpine
trivy image --severity HIGH,CRITICAL --ignore-unfixed --exit-code 1 ghcr.io/acme/api:1.4.2
trivy image --scanners vuln,secret,misconfig ghcr.io/acme/api:1.4.2
trivy config Dockerfile                 # Dockerfile misconfigurations
trivy fs --scanners vuln .              # scan lockfiles in the repo
trivy image --format sarif -o trivy.sarif ghcr.io/acme/api:1.4.2
```

```text
ghcr.io/acme/api:1.4.2 (debian 12.7)
Total: 3 (HIGH: 2, CRITICAL: 1)

+-----------+----------------+----------+-------------------+---------------+
|  Library  | Vulnerability  | Severity | Installed Version | Fixed Version |
+-----------+----------------+----------+-------------------+---------------+
| libssl3   | CVE-2024-XXXX  | CRITICAL | 3.0.14-1~deb12u1  | 3.0.14-1~deb12u2 |
...
```

Key flags: `--exit-code 1` fails CI, `--ignore-unfixed` reduces noise from CVEs with no fix available, `.trivyignore` documents accepted risks (with expiry and justification).

### 8.3.4 Docker Scout

Docker Scout is built into the Docker CLI and Docker Hub.

```bash
docker scout quickview ghcr.io/acme/api:1.4.2
docker scout cves --only-severity critical,high ghcr.io/acme/api:1.4.2
docker scout recommendations ghcr.io/acme/api:1.4.2   # suggests better base image tags
docker scout compare ghcr.io/acme/api:1.4.2 --to ghcr.io/acme/api:1.4.1
```

`recommendations` is particularly useful: it tells you which base image update removes the most CVEs.

> **Tip:** Scanning is a point-in-time result. New CVEs are published daily against images you already shipped. Re-scan images in your registry continuously (ECR enhanced scanning, Harbor scheduled scans, Scout policies), not only at build time.

### 8.3.5 SBOMs

A **Software Bill of Materials** lists every package in an image. When the next Log4Shell happens, you query SBOMs instead of scanning thousands of images in a panic.

```bash
# Generate with BuildKit at build time (attached as an attestation)
docker buildx build --sbom=true --provenance=mode=max -t ghcr.io/acme/api:1.4.2 --push .

# Generate with Syft (SPDX or CycloneDX)
syft ghcr.io/acme/api:1.4.2 -o spdx-json > sbom.spdx.json
syft ghcr.io/acme/api:1.4.2 -o cyclonedx-json > sbom.cdx.json

# Scan an SBOM instead of the image
grype sbom:sbom.cdx.json
trivy sbom sbom.cdx.json

# Inspect attestations on a pushed image
docker buildx imagetools inspect ghcr.io/acme/api:1.4.2 --format '{{json .SBOM}}' | jq '.SPDX.packages | length'
```

**Provenance** attestations (SLSA) record how, where and from what source an image was built, which lets consumers verify it came from your CI.

### 8.3.6 Signing and verification with cosign

Sigstore's **cosign** signs images and stores signatures in the registry next to the image.

Keyless signing (recommended in CI with OIDC, e.g. GitHub Actions):

```bash
# In CI with id-token permission; identity comes from the OIDC token
cosign sign --yes ghcr.io/acme/api@sha256:5e1f...
```

Verification pins the expected identity and issuer:

```bash
cosign verify ghcr.io/acme/api@sha256:5e1f... \
  --certificate-identity-regexp 'https://github.com/acme/api/.github/workflows/release.yml@refs/tags/v.*' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

Key-based signing (air-gapped or KMS-backed):

```bash
cosign generate-key-pair                       # or --kms awskms:///alias/cosign
cosign sign --key cosign.key ghcr.io/acme/api@sha256:5e1f...
cosign verify --key cosign.pub ghcr.io/acme/api@sha256:5e1f...
cosign attest --key cosign.key --type cyclonedx --predicate sbom.cdx.json ghcr.io/acme/api@sha256:5e1f...
```

Always sign **digests**, not tags. Enforce verification at deploy time: Kubernetes admission controllers (Sigstore policy-controller, Kyverno, Connaisseur), Harbor's content trust policies, or a deploy script that runs `cosign verify` before rollout.

> **Note:** Docker Content Trust (Notary v1, `DOCKER_CONTENT_TRUST=1`) is legacy. The ecosystem has moved to Sigstore/cosign and Notary Project (notation) signatures stored as OCI artifacts.

## 8.4 Securing the Runtime

### 8.4.1 Run as non-root

Root in a container is real root on the host kernel (without user namespaces), restricted only by capabilities and filters. A breakout from a root container is far worse.

```dockerfile
RUN groupadd -r -g 10001 app && useradd -r -u 10001 -g app -s /sbin/nologin app
USER 10001:10001
```

```bash
docker run --user 10001:10001 myorg/api
docker run --rm myorg/api id
```

```text
uid=10001(app) gid=10001(app) groups=10001(app)
```

Non-root processes cannot bind ports below 1024 without `CAP_NET_BIND_SERVICE`; listen on 8080 and map `-p 80:8080` instead.

### 8.4.2 Linux capabilities

Root's power is split into about 40 capabilities. Docker grants a default subset (e.g. `CHOWN`, `DAC_OVERRIDE`, `FOWNER`, `SETUID`, `SETGID`, `NET_BIND_SERVICE`, `NET_RAW`, `KILL`, `SYS_CHROOT`, `MKNOD`, `AUDIT_WRITE`, `SETFCAP`, `SETPCAP`, `FSETID`). Dangerous ones like `SYS_ADMIN`, `NET_ADMIN`, `SYS_PTRACE`, `SYS_MODULE` are not granted.

Best practice: drop all, add back only what is needed.

```bash
docker run --cap-drop ALL --cap-add NET_BIND_SERVICE nginx-custom
docker run --rm --cap-drop ALL alpine:3.20 sh -c 'grep Cap /proc/self/status'
```

```text
CapInh: 0000000000000000
CapPrm: 0000000000000000
CapEff: 0000000000000000
CapBnd: 0000000000000000
CapAmb: 0000000000000000
```

Decode with `capsh --decode=00000000a80425fb`.

### 8.4.3 --privileged: avoid it

`--privileged` grants all capabilities, all devices, disables seccomp and AppArmor, and makes `/sys` writable. It is effectively no isolation. If a tool claims to need it, find the specific capability or device: `--cap-add SYS_ADMIN` (still dangerous), `--device /dev/fuse`, etc.

### 8.4.4 no-new-privileges

```bash
docker run --security-opt no-new-privileges:true myorg/api
```

Prevents processes from gaining privileges through setuid/setgid binaries or file capabilities (for example, a `sudo` binary in the image becomes useless to an attacker).

### 8.4.5 seccomp

seccomp filters which **system calls** a process may make. Docker's default profile blocks around 40-60 dangerous syscalls (`kexec_load`, `mount` without `CAP_SYS_ADMIN`, `keyctl`, `bpf`, `ptrace` on older kernels, and so on) while allowing what nearly all apps need.

```bash
docker info --format '{{.SecurityOptions}}'       # confirm seccomp is active
docker run --security-opt seccomp=./custom.json myorg/api
docker run --security-opt seccomp=unconfined myorg/api   # disables it: debugging only
```

A custom profile can be tighter. Tools such as the Security Profiles Operator (Kubernetes) or `strace`-based recording can generate an allowlist of the syscalls your app actually uses.

```json
{
  "defaultAction": "SCMP_ACT_ERRNO",
  "architectures": ["SCMP_ARCH_X86_64", "SCMP_ARCH_AARCH64"],
  "syscalls": [
    { "names": ["read", "write", "openat", "close", "fstat", "mmap", "futex",
                "epoll_wait", "accept4", "socket", "bind", "listen", "exit_group"],
      "action": "SCMP_ACT_ALLOW" }
  ]
}
```

### 8.4.6 AppArmor and SELinux

Mandatory access control limits what files, capabilities and network operations a process can use, independent of UID.

- **AppArmor** (Ubuntu, Debian, SUSE): Docker applies the `docker-default` profile automatically. Load a custom one with `apparmor_parser -r -W myprofile` and run with `--security-opt apparmor=myprofile`.
- **SELinux** (RHEL, Fedora, CentOS): enable with `"selinux-enabled": true`. Containers run as `container_t` with unique MCS labels so one container cannot read another's files. Use `:z`/`:Z` on bind mounts.

```bash
docker inspect api --format '{{.AppArmorProfile}}'
```

```text
docker-default
```

### 8.4.7 Read-only root filesystem

An attacker who cannot write to the filesystem cannot drop binaries or modify app code.

```bash
docker run -d --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  --tmpfs /var/run:rw,size=1m \
  -v appdata:/data \
  myorg/api
```

Find what needs to be writable by running normally and checking `docker diff`.

### 8.4.8 Putting it together

```bash
docker run -d --name api \
  --user 10001:10001 \
  --read-only --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --memory 512m --memory-swap 512m --cpus 1 --pids-limit 200 \
  --network backend \
  -p 127.0.0.1:8080:8080 \
  --restart unless-stopped \
  --health-cmd 'wget -qO- http://127.0.0.1:8080/healthz || exit 1' \
  ghcr.io/acme/api@sha256:5e1f...
```

The Compose equivalent:

```yaml
services:
  api:
    image: ghcr.io/acme/api@sha256:5e1f...
    user: "10001:10001"
    read_only: true
    tmpfs:
      - /tmp:rw,noexec,nosuid,size=64m
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]
    deploy:
      resources:
        limits: { cpus: "1", memory: 512M, pids: 200 }
    networks: [backend]
```

## 8.5 Secrets at Runtime

| Method | Security | Notes |
|---|---|---|
| Baked into image | Very poor | Anyone with pull access has it, forever. |
| `-e SECRET=...` | Poor to fair | Visible in `docker inspect`, `/proc/*/environ`, child processes, crash reports. |
| Bind-mounted file | Fair | File on host disk; restrict permissions. |
| Compose/Swarm secrets | Good | Mounted at `/run/secrets/`; Swarm encrypts at rest in the Raft log and only sends to nodes that need it. |
| External secrets manager | Best | Vault, AWS Secrets Manager, GCP Secret Manager, Azure Key Vault; short-lived credentials, rotation, audit. |

App pattern: read `*_FILE` env vars pointing to secret files, falling back to the plain var only for local development.

```python
import os

def secret(name: str) -> str:
    path = os.environ.get(f"{name}_FILE")
    if path:
        with open(path) as f:
            return f.read().strip()
    return os.environ[name]
```

## 8.6 Security Checklist

| Area | Control |
|---|---|
| Host | Patched kernel/Engine, rootless or userns-remap, no TCP socket, CIS benchmark |
| Image | Minimal pinned base, multi-stage, no secrets, non-root USER, OCI labels |
| Pipeline | Trivy/Scout gate on HIGH/CRITICAL, SBOM, provenance, cosign keyless signing |
| Registry | Private, scan on push, immutable tags, retention, pull-through cache |
| Runtime | cap-drop ALL, no-new-privileges, seccomp default or tighter, AppArmor/SELinux, read-only FS, limits |
| Network | User-defined/internal networks, bind to 127.0.0.1 where possible, DOCKER-USER rules |
| Secrets | Files or secrets manager; never in env for production |
| Ops | Continuous re-scanning, rebuild cadence, audit logs, signature verification at deploy |

## 8.7 Lab: Harden and Verify an Image

**Goal:** take a root, full-OS image, harden it, scan it, sign it, and verify the signature.

1. Baseline scan of a naive image:

```bash
docker build -f Dockerfile.naive -t pyapi:naive ~/labs/pyapi   # from Part 3
trivy image --severity HIGH,CRITICAL pyapi:naive | tail -n 5
docker run --rm pyapi:naive id
```

2. Scan the optimized image (`pyapi:opt`) and compare counts. Then try distroless Python (`gcr.io/distroless/python3-debian12:nonroot`) as the runtime stage, copying the venv's site-packages, and scan again.

3. Run the hardened container:

```bash
docker run -d --name hard -p 127.0.0.1:8000:8000 \
  --read-only --tmpfs /tmp --cap-drop ALL \
  --security-opt no-new-privileges:true --user 10001:10001 \
  --memory 256m --pids-limit 100 pyapi:opt
curl -s localhost:8000/healthz
docker exec hard sh -c 'touch /app/hack' ; echo "exit=$?"     # read-only FS: fails
docker exec hard sh -c 'grep CapEff /proc/self/status'
```

4. Generate an SBOM and scan it:

```bash
syft pyapi:opt -o cyclonedx-json > sbom.json
grype sbom:sbom.json --fail-on high
```

5. Sign and verify with a local key, pushing to the lab registry from Part 4:

```bash
docker run -d --name registry -p 5000:5000 registry:2
docker tag pyapi:opt localhost:5000/pyapi:opt && docker push localhost:5000/pyapi:opt
REF=$(docker inspect --format '{{index .RepoDigests 0}}' localhost:5000/pyapi:opt)   # localhost:5000/pyapi@sha256:...
cosign generate-key-pair
cosign sign --key cosign.key --allow-insecure-registry --yes "$REF"
cosign verify --key cosign.pub --allow-insecure-registry "$REF" | jq '.[0].critical'
```

6. Tamper test: push a different image to the same tag and verify the tag; verification fails because the new digest has no signature.

7. Run `docker-bench-security` and fix two warnings.

8. Clean up: `docker rm -f hard registry`.

## Key takeaways

- Secure the whole lifecycle: host, image, pipeline, registry, runtime, secrets.
- Never bake secrets into images; use BuildKit secret mounts for builds and files or a secrets manager at runtime.
- Scan with Trivy or Docker Scout in CI with a severity gate, and re-scan continuously in the registry.
- Generate SBOMs and provenance; sign digests with cosign (keyless in CI) and verify before deploy.
- Run as non-root, drop all capabilities, keep seccomp/AppArmor/SELinux on, set no-new-privileges and a read-only root.
- `--privileged` and the Docker socket are root on the host; treat both as exceptional.
