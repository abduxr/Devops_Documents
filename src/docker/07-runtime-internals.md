# Part 7 — Runtime Internals: cgroups, Namespaces, Logging, Monitoring and Filesystems

Senior engineers are expected to explain *how* containers work, not just how to run them. That knowledge pays off in troubleshooting: an OOMKilled container, a CPU-throttled service, a host whose disk filled with logs, a "file not found" that exists. This Part covers resource limits and cgroups, namespaces, logging drivers, monitoring, overlayfs, and finally builds a container by hand.

## 7.1 Resource Limits and cgroups

### 7.1.1 What cgroups do

Control groups organize processes into a hierarchy and apply resource controllers to each group: `cpu`, `memory`, `io`, `pids`, `cpuset`, `hugetlb`. Modern distributions use **cgroup v2**, a unified hierarchy at `/sys/fs/cgroup`. With the systemd cgroup driver, each container gets a scope like:

```text
/sys/fs/cgroup/system.slice/docker-<container-id>.scope/
    cpu.max            memory.max        pids.max
    cpu.stat           memory.current    memory.events
    io.max             memory.high       cgroup.procs
```

Without limits, a container can consume all host CPU and memory. Always set limits in production.

### 7.1.2 Memory

```bash
docker run -d --name api --memory 512m --memory-swap 512m myorg/api
docker run -d --memory 1g --memory-reservation 768m myorg/api
```

| Flag | Effect |
|---|---|
| `--memory` (`-m`) | Hard limit (`memory.max`). Exceeding it triggers the kernel OOM killer inside the cgroup. |
| `--memory-swap` | Memory + swap total. Equal to `--memory` means no swap. `-1` means unlimited swap. |
| `--memory-reservation` | Soft limit (`memory.low`/`high` behavior) applied under host memory pressure. |
| `--oom-kill-disable` | Do not OOM-kill; dangerous without a hard limit. Avoid. |

When a container exceeds its limit, the kernel kills a process in the cgroup (usually the biggest, typically your app):

```bash
docker inspect api --format '{{.State.OOMKilled}} {{.State.ExitCode}}'
```

```text
true 137
```

```bash
cat /sys/fs/cgroup/system.slice/docker-$(docker inspect -f '{{.Id}}' api).scope/memory.events
```

```text
low 0
high 0
max 2314
oom 3
oom_kill 3
```

> **Warning:** Language runtimes must know their limit. Modern JVMs (17+) respect cgroup limits automatically (tune with `-XX:MaxRAMPercentage=75`). Node.js sizes its heap from available memory in recent versions but `--max-old-space-size` is still common. Go 1.19+ honors `GOMEMLIMIT`. Python does not manage this at all; watch worker counts.

Note: page cache counts toward the cgroup's memory usage but is reclaimable. `docker stats` subtracts inactive file cache; tools reading raw `memory.current` may show higher numbers.

### 7.1.3 CPU

```bash
docker run -d --cpus 1.5 myorg/api            # quota: 150ms per 100ms period
docker run -d --cpu-shares 512 myorg/batch    # relative weight under contention (default 1024)
docker run -d --cpuset-cpus "0,1" myorg/latency-sensitive
```

| Flag | cgroup v2 file | Semantics |
|---|---|---|
| `--cpus` | `cpu.max` (`150000 100000`) | Hard cap; the process is throttled once it uses its quota in the period. |
| `--cpu-shares` | `cpu.weight` | Proportional share only when CPUs are contended. |
| `--cpuset-cpus` | `cpuset.cpus` | Pin to specific cores. |

Throttling is a silent latency killer. A multi-threaded app with `--cpus 1` can burn its 100 ms quota in 25 ms on 4 cores and then sit idle for 75 ms. Check:

```bash
cat /sys/fs/cgroup/system.slice/docker-<id>.scope/cpu.stat
```

```text
usage_usec 98234123
user_usec 80211022
system_usec 18023101
nr_periods 51234
nr_throttled 8721
throttled_usec 412938112
```

A high `nr_throttled / nr_periods` ratio means raise the limit or reduce the app's thread pool.

### 7.1.4 PIDs, I/O and ulimits

```bash
docker run -d --pids-limit 200 myorg/api                  # fork-bomb protection
docker run -d --device-write-bps /dev/sda:20mb myorg/etl  # I/O throttle
docker run -d --ulimit nofile=65536:65536 myorg/proxy
```

### 7.1.5 Changing limits at runtime

```bash
docker update --memory 1g --memory-swap 1g --cpus 2 api
docker stats --no-stream api
```

```text
CONTAINER ID   NAME   CPU %    MEM USAGE / LIMIT   MEM %    NET I/O         BLOCK I/O     PIDS
4f2a1b9c8d7e   api    12.31%   312MiB / 1GiB       30.47%   1.2MB / 890kB   0B / 4.1MB    23
```

## 7.2 Namespaces

| Namespace | Isolates | Docker flag to share with host |
|---|---|---|
| PID | Process IDs | `--pid host` |
| NET | Interfaces, routes, ports, iptables | `--network host` |
| MNT | Mount points | (bind mounts selectively) |
| UTS | Hostname, domain name | `--uts host` |
| IPC | SysV IPC, POSIX message queues, shared memory | `--ipc host` |
| USER | UID/GID mapping | `--userns host` (when userns-remap is on) |
| CGROUP | View of cgroup hierarchy | `--cgroupns host` |
| TIME | System clocks (offsets) | Not exposed by Docker by default |

Inspect a container's namespaces from the host:

```bash
PID=$(docker inspect -f '{{.State.Pid}}' api)
sudo ls -l /proc/$PID/ns
sudo nsenter -t "$PID" -n ip addr      # enter only the network namespace
sudo nsenter -t "$PID" -m -u -i -n -p sh   # enter almost everything (like docker exec)
lsns -p "$PID"
```

`nsenter` is how you debug a container that has no shell: you use the host's tools inside the container's namespaces.

**User namespaces** remap UIDs: root (UID 0) inside the container maps to an unprivileged UID (e.g. 100000) outside. Enable host-wide with `"userns-remap": "default"` in `daemon.json`, or use rootless Docker. This is one of the strongest hardening measures, because even a container breakout yields an unprivileged user.

## 7.3 Logging

### 7.3.1 How container logging works

The container's stdout and stderr are captured by the shim and passed to the configured **logging driver**. Applications should write logs to stdout/stderr (twelve-factor), ideally as structured JSON.

| Driver | Where logs go | `docker logs` works |
|---|---|---|
| `json-file` | JSON lines under `/var/lib/docker/containers/<id>/` | Yes |
| `local` | Compressed, rotated files; more efficient | Yes |
| `journald` | systemd journal | Yes |
| `syslog` | Syslog server | Via dual logging |
| `fluentd` | Fluentd / Fluent Bit forwarder | Via dual logging |
| `awslogs` | Amazon CloudWatch Logs | Via dual logging |
| `gcplogs` | Google Cloud Logging | Via dual logging |
| `splunk`, `gelf` | Splunk HEC, Graylog | Via dual logging |
| `none` | Discarded | No |

"Dual logging" (Engine 20.10+) keeps a local cache so `docker logs` works even with remote drivers.

### 7.3.2 Configuration

Daemon-wide in `/etc/docker/daemon.json`:

```json
{
  "log-driver": "local",
  "log-opts": { "max-size": "20m", "max-file": "5" }
}
```

Per container:

```bash
docker run -d --log-driver json-file --log-opt max-size=10m --log-opt max-file=3 nginx
docker run -d --log-driver awslogs \
  --log-opt awslogs-region=eu-west-1 \
  --log-opt awslogs-group=/prod/api \
  --log-opt awslogs-create-group=true myorg/api
docker run -d --log-driver fluentd --log-opt fluentd-address=127.0.0.1:24224 \
  --log-opt tag="docker.{{.Name}}" myorg/api
```

> **Warning:** Daemon-wide `log-opts` changes apply only to containers created **after** the change. Existing containers keep their old config until recreated.

### 7.3.3 Delivery mode

`--log-opt mode=non-blocking --log-opt max-buffer-size=4m` makes logging asynchronous. In the default `blocking` mode, if the log destination is slow (e.g. a struggling Fluentd), your app blocks on writes to stdout. Non-blocking trades potential log loss for app availability.

### 7.3.4 Architecture for production

A common pattern: containers log JSON to stdout -> `local` or `json-file` driver -> a node agent (Fluent Bit, Vector, Grafana Alloy, Datadog Agent) tails the files -> central store (Loki, Elasticsearch/OpenSearch, CloudWatch, Splunk). The agent enriches logs with container labels.

## 7.4 Monitoring

### 7.4.1 Built-in tools

```bash
docker stats
docker system df
docker events --filter event=oom --filter event=die
docker inspect --format '{{json .State.Health}}' api | jq
```

### 7.4.2 cAdvisor, Prometheus and Grafana

**cAdvisor** exports per-container metrics (CPU, memory, network, filesystem, throttling) in Prometheus format.

```yaml
# compose.monitoring.yaml
services:
  cadvisor:
    image: gcr.io/cadvisor/cadvisor:v0.49.1
    privileged: true
    devices: ["/dev/kmsg"]
    volumes:
      - /:/rootfs:ro
      - /var/run:/var/run:ro
      - /sys:/sys:ro
      - /var/lib/docker/:/var/lib/docker:ro
    ports: ["127.0.0.1:8088:8080"]

  node-exporter:
    image: prom/node-exporter:v1.8.2
    pid: host
    volumes: ["/:/host:ro,rslave"]
    command: ["--path.rootfs=/host"]

  prometheus:
    image: prom/prometheus:v2.54.1
    volumes: ["./prometheus.yml:/etc/prometheus/prometheus.yml:ro"]
    ports: ["127.0.0.1:9090:9090"]

  grafana:
    image: grafana/grafana:11.2.0
    ports: ["127.0.0.1:3000:3000"]
```

```yaml
# prometheus.yml
scrape_configs:
  - job_name: cadvisor
    static_configs: [{ targets: ["cadvisor:8080"] }]
  - job_name: node
    static_configs: [{ targets: ["node-exporter:9100"] }]
```

Useful PromQL:

```text
# CPU cores used per container
sum by (name) (rate(container_cpu_usage_seconds_total{name!=""}[5m]))

# Memory working set vs limit
container_memory_working_set_bytes{name!=""} / container_spec_memory_limit_bytes{name!=""}

# CPU throttling ratio
rate(container_cpu_cfs_throttled_periods_total[5m]) / rate(container_cpu_cfs_periods_total[5m])
```

The Docker daemon itself can expose metrics with `"metrics-addr": "127.0.0.1:9323"` in `daemon.json`.

### 7.4.3 What to alert on

- Container restarts and OOM kills (events, `container_oom_events_total`).
- Memory working set above 90 percent of limit.
- CPU throttling ratio above about 25 percent sustained.
- Unhealthy containers.
- Host disk usage of `/var/lib/docker` above 80 percent.

## 7.5 Storage Drivers and overlayfs

### 7.5.1 How overlayfs assembles a container root

```text
          Container view (merged)  /
                 ^
                 |  overlay mount
   +-------------+--------------------------------+
   | upperdir    | container writable layer (rw)  |
   +-------------+--------------------------------+
   | lowerdir 3  | image layer: app files   (ro)  |
   | lowerdir 2  | image layer: packages    (ro)  |
   | lowerdir 1  | image layer: base OS     (ro)  |
   +-------------+--------------------------------+
     workdir: scratch space used by overlayfs
```

- **Reads** search from the top layer down; the first match wins.
- **Writes** to an existing lower file trigger **copy-up**: the whole file is copied to `upperdir`, then modified.
- **Deletes** create a **whiteout** file (a character device 0/0) in `upperdir` that hides the lower file.

See it for a running container (classic overlay2 driver):

```bash
docker inspect api --format '{{json .GraphDriver.Data}}' | jq
mount | grep overlay | head -1
```

```json
{
  "LowerDir": "/var/lib/docker/overlay2/7c1e.../diff:/var/lib/docker/overlay2/a9f3.../diff",
  "MergedDir": "/var/lib/docker/overlay2/3be2.../merged",
  "UpperDir": "/var/lib/docker/overlay2/3be2.../diff",
  "WorkDir": "/var/lib/docker/overlay2/3be2.../work"
}
```

With the containerd image store, the snapshots live under `/var/lib/containerd/io.containerd.snapshotter.v1.overlayfs/` instead, but the mechanism is the same.

## 7.6 Building a Container by Hand

The best way to demystify containers is to build one without Docker. Run this on a Linux VM as root (not on your production host).

### 7.6.1 Step 1: get a root filesystem

```bash
mkdir -p /tmp/mycontainer/rootfs && cd /tmp/mycontainer
# Export Alpine's filesystem using Docker (or download alpine-minirootfs tarball)
docker export "$(docker create alpine:3.20)" | tar -C rootfs -xf -
ls rootfs
```

```text
bin  dev  etc  home  lib  media  mnt  opt  proc  root  run  sbin  srv  sys  tmp  usr  var
```

### 7.6.2 Step 2: chroot only (filesystem isolation)

```bash
chroot rootfs /bin/sh
ps aux          # empty or errors: /proc is not mounted
mount -t proc proc /proc
ps aux          # you now see ALL host processes: no PID isolation
exit
umount rootfs/proc
```

`chroot` changes the apparent root, but the process still shares every namespace with the host.

### 7.6.3 Step 3: add namespaces with unshare

```bash
unshare --pid --fork --mount --uts --ipc --net --mount-proc=rootfs/proc \
  chroot rootfs /bin/sh
```

Inside:

```bash
hostname mycontainer && hostname   # UTS namespace: does not affect host
ps aux                             # PID 1 is /bin/sh: PID namespace works
ip addr                            # only 'lo', down: empty NET namespace
```

```text
PID   USER     TIME  COMMAND
    1 root      0:00 /bin/sh
    3 root      0:00 ps aux
```

(Use `pivot_root` instead of `chroot` for real isolation; runc does that, because chroot can be escaped by a privileged process.)

### 7.6.4 Step 4: add a cgroup limit

In another host terminal:

```bash
mkdir /sys/fs/cgroup/mycontainer
echo "50M" > /sys/fs/cgroup/mycontainer/memory.max
echo "50000 100000" > /sys/fs/cgroup/mycontainer/cpu.max   # half a CPU
# find the PID of the containerized sh (the forked child of unshare) and move it in
CPID=$(pgrep -P "$(pgrep -xf 'unshare --pid.*')")
echo "$CPID" > /sys/fs/cgroup/mycontainer/cgroup.procs
```

Inside the container, try to allocate more than 50 MB (for example `head -c 100m /dev/zero | tail`) and it gets OOM-killed.

### 7.6.5 Step 5: give it a network

```bash
# Host side
ip link add veth-host type veth peer name veth-ctr
ip link set veth-ctr netns "$CPID"
ip addr add 10.99.0.1/24 dev veth-host && ip link set veth-host up
# Container side (via nsenter)
nsenter -t "$CPID" -n ip addr add 10.99.0.2/24 dev veth-ctr
nsenter -t "$CPID" -n ip link set veth-ctr up
nsenter -t "$CPID" -n ip link set lo up
ping -c1 10.99.0.2
```

That is, in essence, what Docker's bridge driver does, plus a bridge, NAT and iptables rules.

### 7.6.6 Step 6: an overlay root

```bash
mkdir -p /tmp/ov/{lower,upper,work,merged}
cp -a /tmp/mycontainer/rootfs/. /tmp/ov/lower/
mount -t overlay overlay -o lowerdir=/tmp/ov/lower,upperdir=/tmp/ov/upper,workdir=/tmp/ov/work /tmp/ov/merged
echo hi > /tmp/ov/merged/etc/motd
rm /tmp/ov/merged/etc/hostname
ls -la /tmp/ov/upper/etc
```

```text
c---------  1 root root 0, 0 Oct 12 10:30 hostname
-rw-r--r--  1 root root    3 Oct 12 10:30 motd
```

You see the copied-up `motd` and the whiteout for `hostname`; `lower` is untouched.

What runc adds beyond this: pivot_root, a minimal `/dev`, dropping capabilities, seccomp filters, AppArmor/SELinux labels, user namespace mapping, masked `/proc` paths, and lifecycle hooks, all driven by the OCI `config.json`. You can generate one with `runc spec`.

## 7.7 Lab: Limits, OOM, Throttling and Logs

**Goal:** observe cgroup enforcement and logging behavior directly.

1. Memory limit and OOM:

```bash
docker run --name oomtest --memory 64m --memory-swap 64m python:3.12-alpine \
  python -c "x = bytearray(200*1024*1024)"
docker inspect oomtest --format 'OOMKilled={{.State.OOMKilled}} Exit={{.State.ExitCode}}'
docker events --since 5m --until "$(date +%s)" --filter event=oom
```

```text
OOMKilled=true Exit=137
```

2. CPU throttling:

```bash
docker run -d --name burner --cpus 0.5 alpine:3.20 sh -c 'while :; do :; done'
docker stats --no-stream burner     # about 50% CPU
CG=/sys/fs/cgroup/system.slice/docker-$(docker inspect -f '{{.Id}}' burner).scope
cat $CG/cpu.max; grep throttled $CG/cpu.stat
docker update --cpus 1 burner && sleep 3 && docker stats --no-stream burner
```

3. PID limit:

```bash
docker run --rm --pids-limit 20 alpine:3.20 sh -c 'for i in $(seq 50); do sleep 30 & done; wait'
```

Expect `can't fork: Resource temporarily unavailable`.

4. Logging rotation: run a chatty container with `--log-opt max-size=1m --log-opt max-file=2` using the `json-file` driver, then inspect `/var/lib/docker/containers/<id>/` and confirm at most two files of about 1 MB.

```bash
docker run -d --name chatty --log-driver json-file --log-opt max-size=1m --log-opt max-file=2 \
  alpine:3.20 sh -c 'while :; do echo "$(date) some log line padding padding padding"; done'
sleep 10; sudo ls -lh /var/lib/docker/containers/$(docker inspect -f '{{.Id}}' chatty)/
```

5. Work through section 7.6 (by-hand container) on a scratch VM.

6. Clean up: `docker rm -f oomtest burner chatty`.

## Key takeaways

- cgroups enforce limits: `--memory` triggers the OOM killer (exit 137, `OOMKilled=true`), `--cpus` throttles via `cpu.max`.
- CPU throttling hurts latency invisibly; watch `nr_throttled` in `cpu.stat` or cAdvisor's throttling metrics.
- Namespaces isolate PID, NET, MNT, UTS, IPC, USER and CGROUP; `nsenter` lets host tools work inside a container's namespaces.
- Log to stdout, use the `local` driver or configure rotation, and ship logs with a node agent.
- overlayfs stacks read-only image layers under a writable upper layer with copy-up and whiteouts.
- A container is chroot/pivot_root plus namespaces plus cgroups plus security filters; you can build one with `unshare`.
