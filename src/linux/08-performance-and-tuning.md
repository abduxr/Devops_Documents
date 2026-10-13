# Part 8 — Performance, Troubleshooting Tools and Kernel Tuning

"The server is slow" is a symptom, not a diagnosis. Senior engineers stand out by having a *method*: they quickly narrow down which resource is the bottleneck, prove it with numbers, and only then change something. This Part gives you that method, the tools behind it, and the kernel knobs you'll actually tune.

## 8.1 A Method: USE

Brendan Gregg's **USE method**: for every resource, check

- **Utilization**: how busy is it (% time busy, or % capacity used)?
- **Saturation**: is work queuing because it's overloaded (run queue, swap, I/O wait queue)?
- **Errors**: are there error events (disk errors, dropped packets, OOM kills)?

| Resource | Utilization | Saturation | Errors |
|---|---|---|---|
| CPU | `mpstat -P ALL`, `top` (%us+%sy) | load avg vs cores, `vmstat` `r` column | rare (`dmesg` MCE) |
| Memory | `free -h` (used vs available) | swapping (`vmstat` `si/so`), PSI, OOM kills | `dmesg` OOM, failed allocations |
| Disk | `iostat -x` `%util` | `aqu-sz`, `await` | `dmesg` I/O errors, `smartctl` |
| Network | `sar -n DEV` throughput vs link speed | drops, retransmits (`nstat`, `ss -ti`) | `ip -s link` errors |
| File descriptors | `lsof`, `/proc/sys/fs/file-nr` | "Too many open files" | EMFILE in app logs |

### 8.1.1 The first 60 seconds

When you land on a sick box, run these in order (each takes seconds):

```bash
uptime                      # load averages: rising or falling?
dmesg -T | tail -20         # OOM kills, disk errors, NIC resets
vmstat 1 5                  # run queue, swap, I/O wait, context switches
mpstat -P ALL 1 3           # is one CPU pegged (single-threaded app)?
pidstat 1 3                 # which processes use CPU
iostat -xz 1 3              # disk latency and utilization
free -h                     # memory and cache
sar -n DEV 1 3              # network throughput
sar -n TCP,ETCP 1 3         # connection rates, retransmits
top                         # overall picture, sorted by CPU (press M for memory)
```

`mpstat`, `pidstat`, `iostat` and `sar` come from the **sysstat** package. Install it everywhere, and enable its collector (`systemctl enable --now sysstat`) so `sar` has history you can query after an incident.

## 8.2 CPU and Load

### 8.2.1 Load average

```bash
uptime
```

```text
 10:42:11 up 12 days,  3:01,  2 users,  load average: 7.82, 4.10, 2.03
```

The three numbers are exponentially damped averages over 1, 5 and 15 minutes of the number of tasks that are **runnable (R) or in uninterruptible sleep (D)**. Interpretations:

- Compare with CPU count (`nproc`). Load 7.8 on 8 cores is busy but OK; on 2 cores it's heavily saturated.
- 1-minute much higher than 15-minute: problem is starting now. The reverse: it's recovering.
- On Linux, load includes `D` state, so high load with **idle CPUs** usually means tasks blocked on disk or NFS, not CPU pressure. Check `vmstat`'s `b` column and `iostat`.

### 8.2.2 top and htop

```text
top - 10:42:15 up 12 days, load average: 7.82, 4.10, 2.03
Tasks: 212 total,   9 running, 203 sleeping,   0 stopped,   0 zombie
%Cpu(s): 71.2 us,  6.1 sy,  0.0 ni, 20.3 id,  1.9 wa,  0.0 hi,  0.4 si,  0.1 st
MiB Mem :  15876.4 total,    412.3 free,  11020.1 used,   4444.0 buff/cache
MiB Swap:      0.0 total,      0.0 free,      0.0 used.   4501.7 avail Mem

    PID USER      PR  NI    VIRT    RES    SHR S  %CPU  %MEM     TIME+ COMMAND
   2210 app       20   0   12.1g   8.2g  31440 S 385.0  52.9  91:12.33 java
```

CPU fields: `us` user code, `sy` kernel, `ni` niced, `id` idle, **`wa`** waiting for I/O (CPU idle while I/O outstanding), `hi/si` interrupts, **`st` steal** (hypervisor gave your vCPU's time to someone else; high steal on cloud burstable instances means you've exhausted CPU credits or have a noisy neighbour).

`%CPU` is per core, so 385% means almost four cores busy. Useful keys in top: `1` per-CPU view, `P`/`M` sort by CPU/memory, `c` full command, `H` threads, `k` kill. **htop** is friendlier: tree view (`F5`), filtering, and colour-coded meters.

### 8.2.3 vmstat

```bash
vmstat 1 5
```

```text
procs -----------memory---------- ---swap-- -----io---- -system-- ------cpu-----
 r  b   swpd   free   buff  cache   si   so    bi    bo   in   cs us sy id wa st
 9  0      0 421900  81200 4470000    0    0     4   120 3100 5400 71  6 21  2  0
12  3      0 398000  81200 4471000    0    0  8200  9100 4200 7900 65  8  9 18  0
```

- `r`: runnable tasks (consistently > CPU count = CPU saturation)
- `b`: blocked in uninterruptible sleep (I/O)
- `si/so`: swap in/out per second (any sustained non-zero = memory pressure)
- `bi/bo`: blocks read/written
- `cs`: context switches (very high values can indicate lock contention or too many threads)

### 8.2.4 Finding what the CPU is doing

```bash
pidstat -u -p 2210 1          # per-process CPU over time
pidstat -t -p 2210 1          # per-thread
top -H -p 2210                # threads of one process
sudo perf top                 # live: hottest functions system-wide
sudo perf top -p 2210
sudo perf record -F 99 -g -p 2210 -- sleep 30   # sample stacks for 30s
sudo perf report --stdio | head -50
```

`perf` (package `linux-tools-$(uname -r)` on Ubuntu, `perf` on RHEL) samples where CPUs spend time, including inside the kernel. Rendering `perf script` output as a **flame graph** is the standard way to visualise hot code paths. For JIT runtimes (Java, Node) you need symbol maps (e.g. async-profiler for Java).

## 8.3 Memory

### 8.3.1 Reading free correctly

```bash
free -h
```

```text
               total        used        free      shared  buff/cache   available
Mem:            15Gi        10Gi       412Mi       210Mi       4.3Gi       4.4Gi
Swap:          2.0Gi          0B       2.0Gi
```

Linux uses otherwise idle RAM as **page cache** for file data, so "free" is always low on a healthy server. **`available`** estimates how much memory can be given to applications without swapping (free + reclaimable cache). Watch `available`, not `free`.

```bash
grep -E 'MemAvailable|Dirty|Slab|SReclaimable|Committed_AS' /proc/meminfo
ps -eo pid,user,rss,vsz,cmd --sort=-rss | head     # RSS = resident memory in KiB
smem -tk                                            # proportional set size (if installed)
cat /proc/2210/status | grep -E 'VmRSS|VmSwap'
```

**VSZ/VIRT** is address space reserved (often huge and meaningless); **RSS/RES** is physical memory currently used (shared pages are counted in every process that maps them).

### 8.3.2 The OOM killer

Linux overcommits memory: `malloc` usually succeeds even if RAM isn't available, because many programs never touch all they allocate. When memory really runs out and nothing more can be reclaimed, the kernel's **OOM killer** picks a process (highest `oom_score`, mostly driven by memory size) and kills it with SIGKILL.

```bash
dmesg -T | grep -iE 'out of memory|oom-kill|killed process'
journalctl -k --since "1 hour ago" | grep -i oom
```

```text
[Mon Oct 12 10:31:07 2026] Out of memory: Killed process 2210 (java) total-vm:12688128kB, anon-rss:8601220kB, file-rss:0kB, shmem-rss:0kB, UID:1001 pgtables:17220kB oom_score_adj:0
```

Within a cgroup (a container or a systemd unit with `MemoryMax=`), the limit triggers a *cgroup* OOM: only processes in that group are candidates. In Kubernetes this appears as `OOMKilled` with exit code 137 (128 + 9 = SIGKILL).

Influence victim selection:

```bash
cat /proc/2210/oom_score
echo -1000 | sudo tee /proc/$(pidof sshd | cut -d' ' -f1)/oom_score_adj   # never kill
# Persistently in a unit:  OOMScoreAdjust=-900
```

Many modern distros also run **systemd-oomd**, which kills cgroups early based on memory pressure (PSI) before the kernel OOM killer has to act.

### 8.3.3 Pressure Stall Information (PSI)

PSI shows the percentage of time tasks were stalled waiting for a resource. It's the most direct "is this resource a bottleneck" signal the kernel offers.

```bash
cat /proc/pressure/cpu /proc/pressure/memory /proc/pressure/io
```

```text
some avg10=12.40 avg60=8.11 avg300=3.02 total=88231001
full avg10=4.10 avg60=2.20 avg300=0.80 total=21033011
```

`some` = at least one task stalled; `full` = all non-idle tasks stalled simultaneously. Per-cgroup PSI lives in `/sys/fs/cgroup/<group>/memory.pressure`.

## 8.4 Disk I/O

```bash
iostat -xz 1
```

```text
Device   r/s    w/s   rkB/s   wkB/s  r_await w_await aqu-sz  %util
nvme1n1  820.0 1250.0 52480.0 160000.0  4.10   38.20  48.90  99.80
```

- **`r_await`/`w_await`**: average ms per I/O including queue time. For SSD/NVMe, single-digit ms is normal; tens to hundreds means saturation.
- **`aqu-sz`**: average queue length. Sustained high values = saturated.
- **`%util`**: time the device was busy. For parallel devices (SSD, NVMe, EBS), 100% doesn't necessarily mean maxed out; trust latency and queue size more.

Find the process doing I/O:

```bash
sudo iotop -oPa                 # accumulated I/O per process
pidstat -d 1                    # per-process read/write kB/s
cat /proc/2210/io
```

In the cloud, EBS volumes have IOPS and throughput limits (gp3 baseline 3,000 IOPS and 125 MB/s unless provisioned higher) and instances have their own EBS bandwidth caps. A perfectly healthy disk can look "slow" because you're hitting the provisioned limit; CloudWatch `VolumeQueueLength` and throughput metrics confirm it.

## 8.5 Processes, Files and System Calls

### 8.5.1 lsof

```bash
sudo lsof -p 2210 | head           # all open files of a process
sudo lsof -i :8080                 # who's listening/connected on port 8080
sudo lsof -i TCP -s TCP:ESTABLISHED
sudo lsof /var/log/app.log         # who has this file open
sudo lsof +D /data                 # everything under a directory (slow)
sudo lsof +L1                      # deleted-but-open files
ls /proc/2210/fd | wc -l           # count FDs quickly
```

### 8.5.2 strace

`strace` shows every system call a process makes: the boundary between the program and the kernel. When logs say nothing, strace often shows exactly which file is missing, which permission is denied, or what it's blocked on.

```bash
strace -f -o /tmp/trace.txt ./myapp          # trace new program, follow children
sudo strace -p 2210 -f -tt -T                # attach; timestamps and time per call
sudo strace -p 2210 -e trace=network         # only network syscalls
strace -e trace=openat,stat -f ./myapp 2>&1 | grep -E 'ENOENT|EACCES'
sudo strace -c -p 2210                       # summary table (Ctrl-C to stop)
```

```text
openat(AT_FDCWD, "/etc/myapp/config.yaml", O_RDONLY) = -1 EACCES (Permission denied)
connect(7, {sa_family=AF_INET, sin_port=htons(5432), sin_addr=inet_addr("10.0.2.20")}, 16) = -1 ETIMEDOUT (Connection timed out)
```

> **Warning:** strace uses ptrace and can slow the target dramatically (10x or more on syscall-heavy workloads). Use it briefly on production, or prefer low-overhead eBPF tools (`bpftrace`, the `bcc` tools such as `execsnoop`, `opensnoop`, `biolatency`, `tcpretrans`), which are the modern standard for production tracing.

### 8.5.3 Other quick tools

```bash
ltrace ./myapp              # library calls
pmap -x 2210 | tail -1      # memory map summary
cat /proc/2210/limits       # effective ulimits of a running process
cat /proc/2210/stack        # kernel stack (where a D-state process is stuck), root only
nstat -az | grep -i retrans # network counters
ss -ti dst 10.0.2.20        # per-connection TCP info: rtt, cwnd, retrans
```

## 8.6 Kernel Tuning with sysctl

`sysctl` reads and writes kernel parameters under `/proc/sys`. Change them only for a measured reason, and record why.

```bash
sysctl net.core.somaxconn
sudo sysctl -w net.core.somaxconn=4096        # runtime only
sysctl -a | grep tcp_keepalive
```

Persist in a drop-in file and load it:

```conf
# /etc/sysctl.d/90-app.conf
# Larger accept backlog for busy web servers (app must also request it in listen())
net.core.somaxconn = 4096
net.ipv4.tcp_max_syn_backlog = 8192
# More ephemeral ports for clients making many outbound connections
net.ipv4.ip_local_port_range = 10240 65535
# Detect dead peers faster than the default 2 hours
net.ipv4.tcp_keepalive_time = 300
# Required for routers, NAT, Kubernetes nodes and Docker
net.ipv4.ip_forward = 1
# Prefer dropping page cache over swapping application memory
vm.swappiness = 10
# Elasticsearch/OpenSearch requirement
vm.max_map_count = 262144
# Many inotify watchers (IDEs, file sync, kubelet, log shippers)
fs.inotify.max_user_watches = 524288
# System-wide max open files
fs.file-max = 2097152
```

```bash
sudo sysctl --system      # load all sysctl.d files
```

| Parameter | Why you'd touch it |
|---|---|
| `vm.swappiness` | Lower to keep app memory in RAM (databases) |
| `vm.dirty_ratio`, `vm.dirty_background_ratio` | Control how much dirty page cache builds before writeback (smooth I/O spikes) |
| `vm.overcommit_memory` | `2` for strict accounting; Redis recommends `1` |
| `net.ipv4.tcp_tw_reuse` | Reuse TIME-WAIT sockets for outbound connections |
| `net.core.rmem_max`/`wmem_max`, `tcp_rmem`/`tcp_wmem` | Socket buffers for high-bandwidth/high-latency links |
| `net.netfilter.nf_conntrack_max` | "nf_conntrack: table full, dropping packet" on busy NAT/K8s nodes |
| `kernel.pid_max` | Hosts with huge numbers of processes/threads |

> **Note:** In containers, many `net.*` sysctls are namespaced and set per pod/container (Kubernetes `securityContext.sysctls`), while `vm.*` and `fs.*` are host-wide and must be set on the node.

## 8.7 Resource Limits: ulimits

Per-process limits (`man 2 setrlimit`) cap resources like open files and processes. The classic production error is **"Too many open files"** (EMFILE), because the default soft limit for open files is often 1024.

```bash
ulimit -a           # current shell's limits
ulimit -n           # open files (soft)
ulimit -Hn          # hard limit
ulimit -n 65536     # raise soft up to the hard limit (current shell only)
cat /proc/2210/limits   # what a running process actually has
```

Where to set them persistently, and the trap:

- **Login sessions** (SSH, su): `/etc/security/limits.conf` or `/etc/security/limits.d/*.conf`, applied by PAM.

  ```conf
  # /etc/security/limits.d/90-app.conf
  app   soft  nofile  65536
  app   hard  nofile  65536
  app   soft  nproc   4096
  ```

- **systemd services ignore limits.conf** entirely (no PAM login). Set limits in the unit:

  ```ini
  [Service]
  LimitNOFILE=65536
  LimitNPROC=4096
  ```

  or defaults for all services in `/etc/systemd/system.conf` (`DefaultLimitNOFILE=`).

- **Containers**: `docker run --ulimit nofile=65536:65536`, or the container runtime's defaults.

> **Interview:** "You raised the limit in limits.conf, but the service still hits 'Too many open files'. Why?" Because the service is started by systemd, which doesn't go through PAM. Set `LimitNOFILE=` in the unit, restart, and verify with `cat /proc/<pid>/limits`.

## 8.8 Lab: Create and Diagnose Bottlenecks

Goal: generate CPU, memory, disk and FD problems on purpose and identify each one with the right tool. Use a disposable VM with 2+ vCPUs.

1. Install tools: `sudo apt install -y sysstat stress-ng htop iotop-c strace` (Rocky: `sudo dnf install -y sysstat stress-ng htop iotop strace` with EPEL enabled).

2. **CPU saturation.** Start more CPU workers than cores:

   ```bash
   stress-ng --cpu $(( $(nproc) * 2 )) --timeout 120s &
   uptime; vmstat 1 5; mpstat -P ALL 1 2; pidstat 1 2 | head -20
   ```

   Confirm: `r` column greater than `nproc`, `id` near 0, load rising above core count.

3. **I/O saturation.** High load, idle CPU:

   ```bash
   stress-ng --hdd 2 --hdd-bytes 1G --timeout 120s &
   vmstat 1 5          # look at b and wa
   iostat -xz 1 3      # await, aqu-sz, %util
   sudo iotop -oPa -n 3 -b | head -20
   cat /proc/pressure/io
   ```

4. **Memory pressure and OOM.** Put a memory hog in a limited scope so it OOMs in its own cgroup and doesn't hurt the system:

   ```bash
   sudo systemd-run --unit=memhog -p MemoryMax=200M -p MemorySwapMax=0 \
        stress-ng --vm 1 --vm-bytes 400M --timeout 60s
   sleep 10
   systemctl status memhog --no-pager
   journalctl -k --since "2 min ago" | grep -i -E 'oom|killed'
   ```

5. **File descriptor exhaustion.**

   ```bash
   bash -c 'ulimit -n 64; for i in $(seq 1 100); do exec {fd}</etc/hostname || { echo "failed at $i"; break; }; done'
   ```

   You'll see "Too many open files". Now check the limit of a real service, raise it with a drop-in, and verify:

   ```bash
   grep 'open files' /proc/$(systemctl show -p MainPID --value cron)/limits
   sudo systemctl edit cron      # [Service] LimitNOFILE=65536
   sudo systemctl restart cron
   grep 'open files' /proc/$(systemctl show -p MainPID --value cron)/limits
   ```

   (Use `crond` on RHEL family.)

6. **strace a failure.** Make a config unreadable and find it with strace instead of logs:

   ```bash
   echo "secret" > /tmp/app.conf && chmod 000 /tmp/app.conf
   strace -e trace=openat cat /tmp/app.conf 2>&1 | tail -3
   ```

7. **sysctl.** Read, change at runtime, persist, and reload:

   ```bash
   sysctl vm.swappiness
   echo 'vm.swappiness = 10' | sudo tee /etc/sysctl.d/90-lab.conf
   sudo sysctl --system | grep swappiness
   ```

8. **perf.** While a CPU stress runs, `sudo perf top` and identify the hot function names. Then `sudo perf record -F 99 -a -g -- sleep 10 && sudo perf report --stdio | head -40`.

9. Look at the history: `sar -u` and `sar -r` (after sysstat has collected a few samples) to see the spikes you created.

## Key takeaways

- Follow a method (USE: utilization, saturation, errors) instead of guessing; the "first 60 seconds" checklist triages most hosts.
- Load average counts runnable *and* uninterruptible tasks; compare it with `nproc`, and suspect I/O when load is high but CPU is idle.
- Watch `available` memory, swap activity and PSI, not "free". OOM kills are recorded in the kernel log; exit code 137 means SIGKILL.
- For disks, latency (`await`) and queue size matter more than `%util`; cloud volumes have provisioned limits.
- `lsof` shows open files and sockets, `strace` shows syscalls (high overhead), `perf` and eBPF tools show where CPU time goes.
- Tune the kernel with files in `/etc/sysctl.d/` and `sysctl --system`, only for measured reasons.
- systemd services don't read `limits.conf`; use `LimitNOFILE=` and verify via `/proc/<pid>/limits`.
