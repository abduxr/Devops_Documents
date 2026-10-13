# Part 10 — Troubleshooting Playbook

Everything breaks eventually. The difference between a junior and a senior engineer is a systematic approach: gather facts, form a hypothesis, test it, fix, and prevent recurrence. This Part is a field guide organized by symptom.

## 10.1 The General Method

```text
 1. What is the state?     docker ps -a, docker compose ps, docker events
 2. What did it say?       docker logs --tail 200, docker inspect .State
 3. What changed?          image digest, config, host, dependency, traffic
 4. Reproduce in isolation docker run -it --entrypoint sh <image>
 5. Look from outside      nsenter, netshoot, cgroup files, host logs (journalctl -u docker)
 6. Fix, then prevent      healthcheck, limits, alert, test, runbook
```

The first five commands to run on any broken container:

```bash
docker ps -a --filter name=api
docker logs --tail 100 api
docker inspect api --format 'Status={{.State.Status}} Exit={{.State.ExitCode}} OOM={{.State.OOMKilled}} Err={{.State.Error}} Restarts={{.RestartCount}}'
docker inspect api --format '{{json .State.Health}}' | jq '.Log[-3:]'
docker events --since 1h --filter container=api
```

## 10.2 Container Exits Immediately

### Symptom

```text
$ docker ps -a
NAMES   STATUS
api     Exited (1) 3 seconds ago
```

### Causes and checks

| Exit code | Look for |
|---|---|
| 0 | The main process finished. A container lives only as long as PID 1. Common with images whose CMD is a shell, or a daemon that forks into the background (`nginx` without `daemon off`, `service x start`). |
| 1 | App error: read `docker logs`. Missing env var, bad config, cannot reach dependency. |
| 125 | `docker run` itself failed: invalid flag, name conflict, port already allocated. |
| 126 | Not executable: missing `+x`, script with Windows CRLF line endings, wrong architecture. |
| 127 | Not found: typo, binary not in PATH, or the interpreter in the shebang does not exist (`#!/bin/bash` on Alpine). |
| 137 | SIGKILL: OOM, `docker kill`, or stop timeout. Check `OOMKilled`. |
| 139 | Segfault: often native library or musl/glibc mismatch. |
| 143 | SIGTERM: stopped normally. |

Debug by overriding the entrypoint and exploring:

```bash
docker run --rm -it --entrypoint sh myorg/api:1.4.2
# inside: ls -l /app, cat /app/entrypoint.sh, which python, env
docker run --rm myorg/api:1.4.2 --version     # test args
docker image inspect myorg/api:1.4.2 --format 'EP={{json .Config.Entrypoint}} CMD={{json .Config.Cmd}} USER={{.Config.User}} WD={{.Config.WorkingDir}}'
```

The tricky "no such file or directory" when the file clearly exists:

- Script has CRLF endings: the kernel looks for `/bin/sh\r`. Fix with `dos2unix` or `.gitattributes` (`*.sh text eol=lf`).
- Dynamically linked binary on `scratch`/Alpine: the ELF loader (`/lib64/ld-linux-x86-64.so.2`) is missing. Build static (`CGO_ENABLED=0`) or use a glibc base.
- Architecture mismatch: `exec format error` when running an amd64 image on arm64 without emulation. Check `docker image inspect --format '{{.Architecture}}'`.

## 10.3 Restart Loops

```text
api   Restarting (1) 4 seconds ago
```

```bash
docker inspect api --format '{{.RestartCount}}'
docker logs --tail 50 api
docker update --restart no api     # stop the loop so you can investigate
```

Typical causes: dependency not ready at startup (add retries or `depends_on: condition: service_healthy`), migration failing, missing secret file, healthcheck killing the container under an orchestrator. Remember that Docker's backoff grows to one minute, so a looping container may look "stuck".

## 10.4 OOMKilled

```bash
docker inspect api --format 'OOM={{.State.OOMKilled}} Exit={{.State.ExitCode}} Limit={{.HostConfig.Memory}}'
journalctl -k | grep -i -E 'oom|killed process' | tail
docker stats --no-stream api
```

```text
OOM=true Exit=137 Limit=536870912
kernel: Memory cgroup out of memory: Killed process 48211 (java) total-vm:3194824kB, anon-rss:523112kB
```

Decision tree:

1. Is the limit realistic? Compare peak working set from metrics with the limit.
2. Does the runtime know the limit? JVM `-XX:MaxRAMPercentage`, Node `--max-old-space-size`, Go `GOMEMLIMIT`, worker counts for Python/Ruby.
3. Is there a leak? Memory climbs steadily under constant load.
4. Is it page cache or tmpfs? A tmpfs mount counts against the container's memory. Writing large files to `--tmpfs /tmp` can OOM-kill an app.
5. Exit 137 with `OOMKilled=false`: something else sent SIGKILL (stop timeout, `docker kill`, host-level OOM killer outside the cgroup, orchestrator).

## 10.5 Networking Problems

### 10.5.1 Cannot reach the app from the host

```bash
docker port api                                      # is it published?
docker exec api ss -tlnp 2>/dev/null || docker run --rm --network container:api nicolaka/netshoot ss -tlnp
curl -v http://127.0.0.1:8080/
```

```text
LISTEN 0  128  127.0.0.1:8080  0.0.0.0:*  users:(("node",pid=1,fd=18))
```

The app listens on `127.0.0.1` **inside** the container, which is the container's own loopback. Port forwarding arrives on the container's `eth0`. Fix: bind to `0.0.0.0` (e.g. `--host 0.0.0.0`, `server.address=0.0.0.0`).

Other causes: wrong container port in `-p host:container`, publishing to `127.0.0.1` while testing from another machine, cloud security groups, `DOCKER-USER` rules.

### 10.5.2 Containers cannot reach each other

```bash
docker inspect api db --format '{{.Name}} {{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}'
docker exec api getent hosts db
docker exec api nc -zv db 5432
```

- Different networks: connect them with `docker network connect`.
- Default `bridge` network: no DNS by name. Use a user-defined network.
- Using `localhost:5432` from the app container: `localhost` is the app container itself. Use the service name `db`.
- Calling the **published** host port from another container instead of the container port.

### 10.5.3 Container has no internet / DNS fails

```bash
docker run --rm alpine:3.20 ping -c1 1.1.1.1        # IP connectivity
docker run --rm alpine:3.20 nslookup example.com    # DNS
sysctl net.ipv4.ip_forward                          # must be 1
sudo iptables -t nat -S POSTROUTING | grep MASQ
cat /etc/resolv.conf                                # host resolvers
```

- `ip_forward=0` (some hardening scripts disable it): re-enable.
- Host uses `127.0.0.53` (systemd-resolved); Docker detects this and uses upstream servers, but on custom setups set `"dns": ["10.0.0.2"]` in `daemon.json`.
- Corporate proxy: set `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY` for the daemon (systemd drop-in) for pulls, and in `~/.docker/config.json` `proxies` for containers and builds.
- Firewalld or nftables flushing Docker's rules: restart Docker after firewall reloads, or integrate properly.
- VPN or subnet overlap: route conflicts with `172.17.0.0/16`-style ranges. Set `default-address-pools` and `bip`.

### 10.5.4 "Port is already allocated"

```bash
sudo ss -tlnp | grep ':8080'
docker ps --filter publish=8080
```

## 10.6 Disk Space

The most common production Docker incident: `/var/lib/docker` fills the disk and everything on the host fails.

```bash
df -h /var/lib/docker
docker system df
docker system df -v | head -40
sudo du -sh /var/lib/docker/* 2>/dev/null | sort -h
```

Usual suspects:

| Culprit | Detect | Fix |
|---|---|---|
| Container logs without rotation | `sudo du -sh /var/lib/docker/containers/*/*-json.log` | `log-opts` max-size/max-file, `local` driver; recreate containers. Emergency: `truncate -s 0` the log file. |
| Old images | `docker image ls`, `docker system df` | `docker image prune -a --filter until=168h` |
| Build cache | `docker buildx du` | `docker builder prune --keep-storage 20gb` |
| Dangling/anonymous volumes | `docker volume ls -f dangling=true` | `docker volume prune` (check first!) |
| Writable layers | `docker ps -s` | App writing to container FS; move to a volume or fix the app. |
| Stopped containers | `docker ps -a -f status=exited` | `docker container prune` |

Safe cleanup sequence on a server:

```bash
docker container prune -f --filter "until=24h"
docker image prune -a -f --filter "until=168h"
docker builder prune -f --keep-storage 10gb
docker network prune -f
# volumes: only after reviewing
docker volume ls -f dangling=true
```

> **Warning:** Do not delete files under `/var/lib/docker` by hand (except truncating log files). You will corrupt the daemon's metadata. If the disk is completely full and the daemon cannot start, free space elsewhere first.

Also watch **inodes** (`df -i`): many small layers can exhaust inodes before bytes.

## 10.7 Build Failures

| Error | Cause | Fix |
|---|---|---|
| `failed to compute cache key: "/app/x" not found` | File excluded by `.dockerignore` or wrong context path. | Check `.dockerignore`, the `-f` and context arguments. |
| `COPY failed: forbidden path outside the build context` | `COPY ../shared` | Move the context up a level and use `-f`, or use named contexts: `--build-context shared=../shared` and `COPY --from=shared`. |
| Stale dependencies | Cached `apt-get update` layer | Combine update+install; `--pull`; `--no-cache-filter`. |
| `exec format error` during RUN | Building for another platform without QEMU | Install binfmt, or cross-compile. |
| `no space left on device` | Builder cache or disk | `docker builder prune`. |
| Network timeouts in RUN | Proxy/DNS inside build | `--build-arg HTTPS_PROXY=...`, `--network host` for build, daemon DNS. |
| Secrets not found | Missing `--secret id=...` | Match the `id` in Dockerfile and CLI. |

Get the full log and debug interactively:

```bash
docker build --progress=plain --no-cache . 2>&1 | tee build.log
docker buildx build --target build -t debug-stage . && docker run --rm -it debug-stage sh
BUILDX_EXPERIMENTAL=1 docker buildx debug --invoke /bin/sh build .   # drop into a failed step
```

## 10.8 Permission Problems

```text
PermissionError: [Errno 13] Permission denied: '/data/app.db'
```

```bash
docker exec api id
docker exec api ls -ln /data
ls -ln ./data          # host side for bind mounts
```

- UID mismatch between container user and host directory owner (see Part 5).
- Read-only root filesystem: the app writes somewhere you did not provide as tmpfs/volume.
- SELinux denials: `ausearch -m avc -ts recent`; add `:Z` to bind mounts.
- Capability dropped: e.g. `cap_drop: [ALL]` breaks an entrypoint that runs `chown` (needs `CHOWN`) or `su-exec` (needs `SETUID/SETGID`). Fix the image so it does not need them.

## 10.9 Performance Problems

```bash
docker stats
cat /sys/fs/cgroup/system.slice/docker-<id>.scope/cpu.stat    # throttling
cat /sys/fs/cgroup/system.slice/docker-<id>.scope/memory.stat  # anon vs file
cat /sys/fs/cgroup/system.slice/docker-<id>.scope/io.stat
```

- CPU throttling from tight `--cpus` limits (Part 7).
- Heavy writes to the overlay writable layer: move to a volume.
- Bind mount performance on Docker Desktop: use named volumes for dependency directories, enable VirtioFS or synchronized file shares.
- Logging in blocking mode to a slow remote driver.
- DNS latency: `ndots` and search domains causing multiple lookups; use FQDNs.
- Emulated architecture (`--platform linux/amd64` on Apple Silicon) running 5-10 times slower.

## 10.10 Daemon-Level Problems

```bash
systemctl status docker containerd
journalctl -u docker --since "30 min ago" --no-pager | tail -50
sudo dockerd --debug          # foreground with debug (stop the service first)
docker info 2>&1 | grep -i -E 'warning|error'
```

Enable debug without restart: set `"debug": true` in `daemon.json` and send `SIGHUP` to dockerd (`sudo kill -HUP $(pidof dockerd)`). Send `SIGUSR1` to dump goroutine stacks to the log when the daemon hangs.

Common issues: invalid JSON in `daemon.json` (daemon will not start; validate with `jq . /etc/docker/daemon.json`), conflicting flags between `daemon.json` and the systemd unit (`-H` defined in both), storage driver errors after kernel changes, containerd not running.

## 10.11 Quick Reference: Symptom to First Command

| Symptom | First command |
|---|---|
| Container not running | `docker ps -a` then `docker logs` |
| Exit 137 | `docker inspect -f '{{.State.OOMKilled}}'` |
| Unhealthy | `docker inspect -f '{{json .State.Health}}' api` |
| Cannot connect from host | `docker port`, then `ss -tlnp` inside |
| Cannot connect between containers | `docker network inspect`, `getent hosts` |
| No internet | `ping 1.1.1.1` then `nslookup` from a container |
| Disk full | `docker system df`, `du` on `/var/lib/docker/containers` |
| Slow | `docker stats`, `cpu.stat` throttling |
| Build cannot find file | `.dockerignore`, context path |
| Permission denied | `id` inside, `ls -ln` on the mount |
| Daemon errors | `journalctl -u docker` |

## 10.12 Lab: Break It and Fix It

Each scenario: run the broken command, diagnose with the playbook, write down the root cause, then fix.

1. **The disappearing container**

```bash
docker run -d --name s1 ubuntu:24.04
```

Hint: what is the default CMD, and is there a TTY?

2. **The invisible port**

```bash
docker run -d --name s2 -p 8080:8000 python:3.12-alpine python -m http.server 8000 --bind 127.0.0.1
curl -m 3 localhost:8080
```

Fix: bind to `0.0.0.0`.

3. **The memory hog**

```bash
docker run -d --name s3 --memory 50m redis:7.4-alpine
docker exec s3 redis-cli DEBUG POPULATE 2000000 key 64
docker inspect s3 --format '{{.State.OOMKilled}}'
```

Fix: raise the limit or set `maxmemory` and an eviction policy in Redis config so the app stays under the cgroup limit.

4. **The lonely database**

```bash
docker run -d --name s4db -e POSTGRES_PASSWORD=x postgres:16
docker run --rm postgres:16 pg_isready -h s4db
```

Fix: put both on a user-defined network.

5. **The CRLF entrypoint**

```bash
mkdir -p ~/labs/crlf && cd ~/labs/crlf
printf '#!/bin/sh\r\necho hi\r\n' > entry.sh
printf 'FROM alpine:3.20\nCOPY --chmod=755 entry.sh /entry.sh\nENTRYPOINT ["/entry.sh"]\n' > Dockerfile
docker build -t crlf . && docker run --rm crlf
```

Fix: convert line endings (`sed -i 's/\r$//' entry.sh`) and add a `.gitattributes` rule.

6. **The full disk**: on a scratch VM, run the `chatty` container from Part 7 *without* log rotation for a minute, measure its log size, then configure rotation and recreate it.

7. Clean up: `docker rm -f s1 s2 s3 s4db`.

## Key takeaways

- Start every investigation with `ps -a`, `logs`, `inspect .State`, and `events`.
- Exit codes point the way: 125/126/127 startup problems, 137 kill/OOM, 139 segfault.
- "Connection refused" through a published port usually means the app is bound to `127.0.0.1` inside the container.
- Container-to-container problems are almost always networks, default bridge DNS, or using `localhost`.
- Disk incidents come from logs, images, build cache and volumes; prevent them with rotation and scheduled pruning.
- Debug minimal images from outside with `nsenter`, `--network container:`, `--pid container:` and netshoot.
