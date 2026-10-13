# Part 10 — Real-World Troubleshooting, Interview Prep and Cheat Sheet

This final Part puts everything together. First, six incident scenarios with a diagnostic walkthrough for each: the order in which you'd look, what the output tells you, and how to fix and prevent the problem. Then a 45-question interview bank, and a dense command cheat sheet to keep at hand.

The general approach for every incident:

```text
 1. Scope      What exactly is broken? Since when? One host or many? What changed?
 2. Observe    Gather evidence with read-only commands before changing anything.
 3. Hypothesize  Form the simplest explanation consistent with ALL the evidence.
 4. Test       Change one thing, verify the effect.
 5. Fix + prevent  Restore service, then fix the root cause, add monitoring/automation.
 6. Write it up  Timeline, cause, fix, follow-ups (blameless postmortem).
```

## 10.1 Scenario: Disk Full

**Symptom:** The application returns 500s; logs show `No space left on device`. Deploys fail.

**Step 1: Which filesystem, and is it bytes or inodes?**

```bash
df -hT
df -i
```

```text
Filesystem     Type  Size  Used Avail Use% Mounted on
/dev/nvme0n1p1 ext4   30G   30G     0 100% /
/dev/nvme1n1   xfs   100G   41G   59G  41% /data
```

If `df -i` shows 100% `IUse%` with free bytes, it's inode exhaustion: count files per directory (`find / -xdev -type f | cut -d/ -f2-4 | sort | uniq -c | sort -rn | head`) and remove the swarm of small files (sessions, cache, mail queue).

**Step 2: Where is the space?**

```bash
sudo du -xh / --max-depth=1 2>/dev/null | sort -h | tail
sudo du -xh /var --max-depth=2 2>/dev/null | sort -h | tail
sudo find / -xdev -type f -size +500M -exec ls -lh {} + 2>/dev/null
```

Usual suspects: `/var/log` (an app logging in debug mode, or no logrotate), `/var/lib/docker` (images, stopped containers, container logs in `/var/lib/docker/containers/*/*-json.log`), the journal (`/var/log/journal`), core dumps (`/var/lib/systemd/coredump`), old kernels in `/boot`, package caches, forgotten backups or heap dumps in `/tmp` or home directories.

**Step 3: If `du` totals far less than `df` reports**, look for deleted-but-open files:

```bash
sudo lsof +L1 | sort -k7 -n | tail
```

**Step 4: Free space safely.**

```bash
sudo truncate -s 0 /var/log/app/debug.log      # not rm, if a process has it open
sudo journalctl --vacuum-size=500M
docker system df && docker system prune -af --filter "until=168h"   # check what it removes first
sudo apt clean     # or: sudo dnf clean all
sudo apt autoremove --purge    # old kernels on Ubuntu
# For deleted-open files: restart the process, or truncate via /proc/<pid>/fd/<n>
```

**Prevent:** logrotate with `maxsize`, journald `SystemMaxUse=`, Docker log driver limits (`"log-opts": {"max-size": "50m", "max-file": "3"}` in `/etc/docker/daemon.json`), separate volumes for `/var/lib/docker` and data, and alerting at 80%/90% with time-to-full prediction.

> **Tip:** Root reserved blocks: ext4 reserves 5% for root by default, so root-owned daemons may keep working when users see 100%. On a big data volume you can reduce it with `tune2fs -m 1 /dev/...`.

## 10.2 Scenario: High CPU / High Load

**Symptom:** Latency alerts; load average 24 on an 8-vCPU host.

**Step 1: Is it CPU, or something that looks like it?**

```bash
uptime; nproc
vmstat 1 5
top -b -n 1 | head -20
```

Decide from `vmstat`:

- High `r`, low `id`, high `us`: genuine CPU saturation in user space (application work).
- High `sy`: kernel time (syscall storms, lock contention, lots of small I/O or network packets).
- High `b` and `wa`, CPU mostly idle: it's I/O, not CPU. Go to `iostat -xz 1`.
- High `st`: the hypervisor is taking CPU (burstable instance out of credits, noisy neighbour). Check CloudWatch `CPUCreditBalance`; move to a non-burstable type.

**Step 2: Which process and which threads?**

```bash
pidstat -u 1 5
top -H -p <PID>                 # hottest threads
ps -eLo pid,tid,pcpu,comm --sort=-pcpu | head
```

**Step 3: What is it doing?**

```bash
sudo perf top -p <PID>          # hot functions
sudo strace -c -f -p <PID>      # syscall summary (briefly!)
# Java: jstack <PID> (convert the hot TID to hex and find it in the dump), or async-profiler
# Python: py-spy top --pid <PID>
```

**Step 4: Check the context.** Was there a deploy (`journalctl --since "2 hours ago" | grep -i -E 'started|deploy'`, CI history)? Traffic spike (access logs per minute)? A cron job (`systemctl list-timers`, `grep CRON /var/log/syslog`)? Runaway process left by a previous run? Crypto miner (unknown process name, high CPU, outbound connections to odd ports: `ss -tnp`)?

**Fix:** roll back the deploy, scale out, rate-limit, `renice` a batch job, or kill a runaway process (SIGTERM first). **Prevent:** CPU limits/cgroups for batch work, autoscaling on CPU and latency, profiling in staging, alerts on load per core.

## 10.3 Scenario: Service Won't Start

**Symptom:** After a config change and restart, `myapp` is down. `systemctl restart myapp` returns an error.

**Step 1: Read what systemd and the service say.**

```bash
systemctl status myapp -l --no-pager
journalctl -u myapp -b -n 50 --no-pager
```

```text
x myapp.service - My API service
     Loaded: loaded (/etc/systemd/system/myapp.service; enabled; preset: enabled)
     Active: failed (Result: exit-code) since Mon 2026-10-12 11:02:41 UTC; 4s ago
    Process: 4410 ExecStart=/opt/myapp/bin/server --port 8080 (code=exited, status=1/FAILURE)
   Main PID: 4410 (code=exited, status=1/FAILURE)
Oct 12 11:02:41 web1 server[4410]: FATAL: listen tcp :8080: bind: address already in use
```

**Step 2: Interpret the result code.**

| What you see | Meaning | Next check |
|---|---|---|
| `status=203/EXEC` | Binary not found or not executable | `ls -l` the ExecStart path; absolute path? `+x`? shebang valid? |
| `status=200/CHDIR` | `WorkingDirectory=` missing/inaccessible | Directory exists, user can enter it |
| `status=217/USER` | `User=` doesn't exist | `getent passwd myapp` |
| `status=226/NAMESPACE` | Sandboxing path problem | `ReadWritePaths=` points to non-existent dir |
| `status=1/FAILURE` | The app itself exited with error | App's own log message (config parse error, bind failure) |
| `Result: timeout` | Didn't become ready in time | `Type=` mismatch (`forking`/`notify` vs app behaviour), slow dependency |
| `start-limit-hit` | Restarted too often | Fix cause, then `systemctl reset-failed myapp` |
| `Result: oom-kill` | Hit `MemoryMax=` | Raise limit or fix memory use |
| Active but immediately "inactive (dead)" | `Type=simple` with a daemon that forks and exits | Use foreground flag or `Type=forking` |

**Step 3: Reproduce by hand, as the service user.**

```bash
sudo systemctl cat myapp                          # including drop-ins
sudo -u myapp env $(grep -v '^#' /etc/myapp/env | xargs) /opt/myapp/bin/server --port 8080
```

If it works by hand but not under systemd, the difference is environment (PATH, variables), sandboxing (`ProtectSystem`, `PrivateTmp`), limits, or SELinux.

**Step 4: Check the common environmental causes.**

```bash
sudo ss -tlnp | grep ':8080'                     # port taken? by whom?
sudo -u myapp test -r /etc/myapp/config.yaml && echo readable
namei -l /etc/myapp/config.yaml                  # traversal permissions
sudo ausearch -m AVC -ts recent                  # SELinux denials (RHEL family)
sudo journalctl -k | grep -i apparmor            # AppArmor (Ubuntu)
systemd-analyze verify /etc/systemd/system/myapp.service
df -h / /var; df -i                              # can't write PID/log/temp files?
```

In the example above, a manually started copy (from a debugging session last week, under `nohup`) is holding port 8080. `ss -tlnp` shows its PID; stop it and start the unit.

**Prevent:** validate configs before restart (`nginx -t`, `sshd -t`, app `--check-config` as `ExecStartPre=`), deploy via automation with health checks, never run ad-hoc copies of services outside systemd.

## 10.4 Scenario: Can't SSH to a Server

**Symptom:** `ssh web1` hangs or is rejected.

**Step 1: Classify the failure from the client.**

```bash
ssh -vvv web1
```

| Client output | Layer | Likely cause |
|---|---|---|
| `Could not resolve hostname` | DNS | Wrong name, DNS down, `~/.ssh/config` HostName typo |
| Hangs at `Connecting to ... port 22`, then timeout | Network | Security group/NACL/firewall, wrong IP (instance restarted with new public IP), host down, route missing |
| `Connection refused` | Host | sshd not running or listening on a different port/address |
| `Connection closed by ... [preauth]` | sshd | `AllowUsers/AllowGroups`, `MaxStartups`, fail2ban ban, TCP wrappers |
| `Permission denied (publickey)` | Auth | Wrong user, wrong key, key not in `authorized_keys`, bad permissions, SELinux label |
| `REMOTE HOST IDENTIFICATION HAS CHANGED` | Host key | Host rebuilt (verify, then `ssh-keygen -R`) or MITM |
| Login succeeds then immediately closes | Shell/session | User shell is `nologin`, home missing, disk full, `ForceCommand` |

**Step 2: Network path.**

```bash
dig +short web1.example.com
nc -zv -w 5 web1.example.com 22
# From a host in the same VPC:
nc -zv 10.0.1.11 22
```

In AWS also check: the instance's security group allows 22 from *your current IP* (home IPs change), NACLs allow inbound 22 and **outbound ephemeral ports** (1024–65535) for the return traffic, the subnet's route table, and whether you're using the public vs private IP correctly.

**Step 3: If you can get any other access** (SSM Session Manager, EC2 serial console, cloud console, another admin), inspect the server side:

```bash
sudo systemctl status ssh          # or sshd
sudo ss -tlnp | grep sshd
sudo sshd -t                       # config error after a recent edit?
sudo journalctl -u ssh -n 50       # or /var/log/auth.log, /var/log/secure
ls -ld ~user ~user/.ssh; ls -l ~user/.ssh/authorized_keys   # 755/700/600, owned by user
sudo fail2ban-client status sshd
df -h /                            # full disk can break logins
sudo restorecon -Rv /home/user/.ssh   # SELinux contexts
```

Server log examples and their meaning:

```text
Authentication refused: bad ownership or modes for directory /home/deploy   -> fix permissions
User deploy from 203.0.113.5 not allowed because not listed in AllowGroups  -> group membership
Connection closed by authenticating user ubuntu 203.0.113.5 port 50122 [preauth]  -> key rejected
```

**Step 4: If there's no other access** (unbootable or locked out): stop the instance, detach the root volume, attach it to a rescue instance, mount it, fix `sshd_config`/`authorized_keys`/fstab, reattach and boot. On AWS, the EC2 serial console or SSM can avoid that.

**Prevent:** SSM or a bastion as a second path, config changes via Ansible with `validate`, always keep a session open when editing sshd, monitor sshd availability.

## 10.5 Scenario: DNS Failing

**Symptom:** The app logs `could not resolve host: api.payments.internal` intermittently or constantly. `curl https://api.payments.internal` fails; IPs work.

**Step 1: Reproduce with the system resolver and with DNS directly.**

```bash
getent hosts api.payments.internal         # the way the app resolves
dig api.payments.internal                  # via configured nameserver
dig @10.0.0.2 api.payments.internal        # VPC resolver directly (AWS: VPC CIDR base + 2)
dig @1.1.1.1 example.com                   # can we resolve anything externally?
```

Interpret:

- `getent` fails, `dig` works: problem in `/etc/hosts`, `/etc/nsswitch.conf`, or search domains (the app uses a short name and the search list is wrong).
- `dig` returns `NXDOMAIN`: the record doesn't exist in the zone the resolver sees (private hosted zone not associated with this VPC, typo, wrong environment).
- `dig` returns `SERVFAIL`: the resolver couldn't get an answer (upstream down, DNSSEC failure, forwarding rule broken).
- `dig` times out (`connection timed out; no servers could be reached`): can't reach the resolver at all (firewall blocking UDP/TCP 53, resolver down, wrong IP in config).

**Step 2: Inspect resolver configuration.**

```bash
cat /etc/resolv.conf; ls -l /etc/resolv.conf
resolvectl status
grep hosts /etc/nsswitch.conf
grep -v '^#' /etc/hosts
```

**Step 3: Check the systemd-resolved layer** (Ubuntu): the stub at `127.0.0.53` must be running, and the per-link upstream servers must be correct.

```bash
systemctl status systemd-resolved
resolvectl query api.payments.internal
sudo resolvectl flush-caches
journalctl -u systemd-resolved --since "30 min ago"
```

**Step 4: Intermittent failures** point to: one of several nameservers being broken (resolvers are tried in order with timeouts), UDP packet loss, conntrack table full on busy nodes (`dmesg | grep conntrack`), AWS per-ENI DNS packet limits (1024 packets/s to the VPC resolver) on very chatty hosts, or Kubernetes `ndots:5` causing many extra lookups per name. Watch it on the wire:

```bash
sudo tcpdump -ni any port 53 -c 50
```

**Fix:** correct the netplan/nmcli DNS settings or private zone association, restart the resolver, and use FQDNs with a trailing dot in latency-sensitive configs. **Prevent:** local caching (systemd-resolved, NodeLocal DNSCache in Kubernetes), monitoring DNS resolution latency and errors as a first-class SLI.

> **Note:** "It's always DNS" is a joke because it's frequently true. Rule it out early with `getent hosts` and `dig`.

## 10.6 Scenario: Server Doesn't Boot After Changes (Bonus)

**Symptom:** After a reboot, the instance fails health checks; console shows `You are in emergency mode` or `Give root password for maintenance`.

**Walkthrough:** view the serial console / system log. `emergency mode` almost always means a failed mount: a typo or a missing device in `/etc/fstab` without `nofail`. From the emergency shell (or a rescue instance with the root volume attached):

```bash
journalctl -xb | grep -iE 'fail|mount|timed out'
systemctl --failed
mount -o remount,rw /
vi /etc/fstab               # fix UUID or add nofail
findmnt --verify
systemctl daemon-reload && mount -a
reboot
```

Other boot failures: kernel panic after an update (choose the previous kernel in GRUB, then fix the default with `grubby --set-default` or `grub-set-default`), missing initramfs drivers after moving to a new instance type (rebuild with `dracut -f` / `update-initramfs -u`), SELinux relabel needed (`touch /.autorelabel`), full `/boot` preventing kernel installation.

## 10.7 Lab: Break-Fix Game

Goal: practise the scenarios under realistic conditions. Work in pairs: one person breaks a VM (secretly), the other fixes it using only the methods above, narrating each step. Swap roles. Snapshot the VM first.

Suggested breakages (the breaker picks two at a time):

1. Fill the root filesystem with a hidden, open, deleted file: `sudo bash -c 'exec 3>/var/tmp/x; dd if=/dev/zero bs=1M count=<size> >&3; rm /var/tmp/x; sleep infinity' &`
2. Exhaust inodes in `/var/tmp` with a loop of `touch`.
3. Start a CPU hog with an innocent-looking name: `bash -c 'exec -a logsync bash -c "while :; do :; done"' &`.
4. Change `ExecStart` in a unit to a wrong path, or set `User=` to a non-existent user, and restart it.
5. Bind something to a service's port so it can't start: `python3 -m http.server 80`.
6. Add `DenyUsers <you>` in a drop-in in `/etc/ssh/sshd_config.d/` and reload (keep a console!).
7. `chmod 777 ~/.ssh` for the login user.
8. Point `/etc/resolv.conf` (or netplan nameservers) at an unreachable IP.
9. Add a bogus line to `/etc/fstab` *without* `nofail` and reboot (needs console access to recover).
10. On Rocky: move a web page into the docroot with `mv` so it has the wrong SELinux label.

For each fix, the fixer writes a three-line postmortem: symptom, root cause and evidence, fix and prevention.

## 10.8 Interview Questions and Answers

### Beginner

**1. What is the difference between Linux the kernel and a Linux distribution?**
The kernel manages hardware, processes, memory and system calls. A distribution bundles the kernel with userland tools, a C library, init system, package manager, default configuration and a support lifecycle (Ubuntu, RHEL, Alpine...).

**2. What does `/etc` contain, and `/var`?**
`/etc` holds system-wide configuration files. `/var` holds variable data that changes at runtime: logs (`/var/log`), service state (`/var/lib`), caches and spools.

**3. What's the difference between an absolute and a relative path?**
Absolute starts from `/` and is the same from anywhere; relative is resolved from the current working directory.

**4. Explain `chmod 754 file`.**
Owner rwx (7), group r-x (5), others r-- (4).

**5. What's the difference between a hard link and a symbolic link?**
A hard link is another directory entry for the same inode (same filesystem, can't link directories, survives deletion of the original name). A symlink is a separate file containing a path; it can cross filesystems and breaks if the target is removed.

**6. How do you find which process is listening on port 8080?**
`sudo ss -tlnp | grep :8080` or `sudo lsof -i :8080`.

**7. What do `>`, `>>`, `2>&1` and `|` do?**
Redirect stdout to a file (truncate), append, send stderr to wherever stdout currently goes, and pipe stdout into the next command's stdin.

**8. How do you view the last 100 lines of a log and keep following it?**
`tail -n 100 -f file` (`-F` to survive rotation), or `journalctl -u svc -n 100 -f` for systemd services.

**9. What's the difference between `apt update` and `apt upgrade`?**
`update` refreshes the package index from repositories; `upgrade` installs newer versions of installed packages.

**10. How do you check disk space and directory sizes?**
`df -h` for filesystem usage, `df -i` for inodes, `du -sh dir` or `du -h --max-depth=1 | sort -h` for directories.

**11. What is the root user, and why use sudo instead?**
UID 0, which bypasses permission checks. sudo grants scoped privileges per command, requires the user's own credentials, and logs who did what.

**12. What does `kill -9` do and why avoid it?**
Sends SIGKILL, which can't be caught: the process dies immediately without cleanup (flushing data, removing locks, closing connections gracefully). Try SIGTERM first.

**13. How do you make a script executable and run it?**
`chmod +x script.sh` then `./script.sh`; the shebang line picks the interpreter.

**14. What is an environment variable, and how do you set one for child processes?**
A key-value string in a process's environment, inherited by children. `export NAME=value`.

**15. How do you search for a string recursively in a directory?**
`grep -rn "string" dir/` (`-i` for case-insensitive, `-l` for filenames only).

### Intermediate

**16. Describe the Linux boot process.**
Firmware (UEFI/BIOS) → bootloader (GRUB) loads the kernel and initramfs and passes the command line → kernel initialises hardware and unpacks initramfs → initramfs loads drivers and mounts the real root filesystem → `switch_root` to systemd as PID 1 → systemd starts units to reach `default.target`.

**17. What is the initramfs for?**
It's a temporary root filesystem in RAM that contains the drivers and tools (NVMe, LVM, RAID, LUKS) needed to find and mount the real root filesystem, which the kernel can't do alone if those drivers are modules.

**18. Explain SUID, SGID and the sticky bit.**
SUID on an executable runs it with the owner's UID (e.g. `passwd`). SGID on an executable runs with the file's group; on a directory, new files inherit the directory's group. Sticky on a directory (e.g. `/tmp`) lets only a file's owner delete or rename it.

**19. What is umask?**
A mask of permission bits removed from newly created files and directories. With `022`, files (requested 666) become 644 and directories (777) become 755.

**20. `systemctl enable` vs `systemctl start`?**
`start` starts the unit now; `enable` creates symlinks so it starts at boot. `enable --now` does both.

**21. How do you change a setting in a vendor-provided systemd unit?**
`systemctl edit unit` to create a drop-in in `/etc/systemd/system/unit.d/override.conf`; clear list settings like `ExecStart=` before redefining them. Never edit `/usr/lib/systemd/system`. systemctl edit reloads automatically; after manual edits run `systemctl daemon-reload`.

**22. What is load average, and what does a load of 8 mean?**
Exponentially damped 1/5/15-minute averages of tasks runnable or in uninterruptible (D) sleep. On 8 cores, 8 means fully utilised; on 2 cores it means heavy saturation. With idle CPUs, high load indicates I/O-blocked tasks.

**23. `free` shows almost no free memory. Is the server in trouble?**
Not necessarily. Linux uses spare RAM as page cache. Look at the `available` column, swap activity (`vmstat si/so`) and memory PSI.

**24. What is a zombie process and how do you get rid of it?**
A process that has exited but whose parent hasn't called `wait()` to collect its status. It uses no resources besides a PID entry. You can't kill it; fix or restart the parent so it reaps (or so the zombie is re-parented to PID 1 and reaped).

**25. `df` shows the disk 100% full but `du` shows much less. Why?**
Most often a deleted file still held open by a process (`lsof +L1`); the space is freed when the process closes it or restarts. Alternatively, files hidden under a mount point, or the reserved root blocks.

**26. How do you extend an LVM logical volume and its filesystem?**
Add space to the VG if needed (`pvcreate`, `vgextend`), then `lvextend -r -L +10G /dev/vg/lv`, which also grows the filesystem (`resize2fs` for ext4, `xfs_growfs` for XFS).

**27. Why use UUIDs and `nofail` in `/etc/fstab`?**
Device names like `/dev/sdb` aren't stable across boots. `nofail` prevents a missing secondary volume from dropping the system into emergency mode.

**28. What's the difference between "connection refused" and a timeout?**
Refused: the host answered with a TCP RST because nothing listens on that address/port (or a REJECT rule). Timeout: packets are dropped somewhere (firewall, security group, routing, host down).

**29. How does `dig` differ from `getent hosts`?**
`dig` sends DNS queries directly to a nameserver. `getent hosts` uses the system's NSS resolution (including `/etc/hosts`, search domains and nsswitch order), which is what applications use.

**30. How do you securely reach a database in a private subnet from your laptop?**
SSH local port forwarding through a bastion (`ssh -N -L 15432:db.internal:5432 bastion`), or SSM Session Manager port forwarding; then connect to `localhost:15432`.

**31. Why is `ProxyJump` preferred over agent forwarding?**
With agent forwarding, root on the intermediate host can use your agent socket to authenticate as you. ProxyJump tunnels the connection end to end; the bastion only relays encrypted TCP.

**32. What does `set -euo pipefail` do?**
Exit on command failure (`-e`), treat unset variables as errors (`-u`), and make a pipeline fail if any command in it fails (`pipefail`), rather than only the last.

**33. How do you prevent two instances of a cron job running at the same time?**
`flock -n /run/lock/job.lock command`, or use a systemd timer, which won't start the service again while it's still active.

**34. A service hits "Too many open files" even after editing limits.conf. Why?**
systemd services don't go through PAM, so `limits.conf` doesn't apply. Set `LimitNOFILE=` in the unit (drop-in), restart, and check `/proc/<pid>/limits`.

**35. cron vs systemd timers?**
Timers log to the journal, avoid overlapping runs, support `Persistent=` catch-up and randomized delays, can depend on other units and be sandboxed. cron is simpler and universal but has a minimal environment, special `%` handling and output that's easy to lose.

### Advanced

**36. What are namespaces and cgroups, and how do they make containers?**
Namespaces isolate what a process can see: PIDs, network stack, mounts, hostname, IPC, users, cgroups. cgroups (v2) limit and account resource use: CPU, memory, I/O, PIDs. A container is a process in its own set of namespaces, constrained by a cgroup, with an overlayfs root filesystem, reduced capabilities and a seccomp filter, all sharing the host kernel.

**37. A container's latency spikes, but average CPU usage is well below its limit. What's going on?**
CFS quota throttling: the limit is enforced per 100 ms period, so bursty multi-threaded workloads exhaust the quota early and are paused for the rest of the period. Check `nr_throttled`/`throttled_usec` in the cgroup's `cpu.stat`; raise or remove the CPU limit or reduce parallelism.

**38. How does the OOM killer choose a victim, and how do you influence it?**
When memory can't be reclaimed, the kernel scores processes mainly by memory footprint (`/proc/<pid>/oom_score`), adjusted by `oom_score_adj` (-1000 to 1000). Within a cgroup with `memory.max`, only that group's processes are candidates. Influence it with `OOMScoreAdjust=` in units, memory limits per service, and systemd-oomd.

**39. Explain how you would troubleshoot high I/O wait.**
Confirm with `vmstat` (`b`, `wa`) and `iostat -xz` (`await`, `aqu-sz`, `%util`) which device is saturated. Identify the process with `iotop`/`pidstat -d`. Check for swapping, log storms, backups or compaction jobs, and cloud volume limits (IOPS/throughput caps, burst balance). Fix by moving workloads, tuning the app, provisioning more IOPS, or separating volumes.

**40. What is SELinux, and how do you fix "permission denied" that only happens with SELinux enforcing?**
A mandatory access control system that labels processes and files and enforces a policy beyond Unix permissions. Find the AVC (`ausearch -m AVC -ts recent`), then fix the cause: relabel files (`semanage fcontext` + `restorecon`), enable a boolean (`setsebool -P`), or label a port (`semanage port -a`). Keep it enforcing.

**41. What happens when you type `curl https://example.com` and press Enter?**
The shell parses and expands the line, finds `curl` in PATH, forks and execs it. curl resolves the name via NSS (`/etc/hosts`, then DNS through the configured resolver), opens a TCP socket (three-way handshake via the kernel's routing table and default gateway, through netfilter rules), performs a TLS handshake (SNI, certificate validation against the CA bundle), sends an HTTP request, receives and prints the response, and exits; the shell collects its exit status.

**42. How would you harden a new Linux server?**
Patch and enable automatic security updates; create named users with sudo via groups; key-only SSH, no root login, restricted sources; default-deny firewall; remove unneeded packages and services; SELinux/AppArmor enforcing; auditd rules and persistent, shipped logs; time sync; file integrity monitoring; scan against a CIS benchmark; capture all of it in Ansible or a golden image.

**43. How do you debug a process that is hanging and not logging anything?**
Check its state (`ps -o stat`, `/proc/<pid>/status`). If `D`, look at `/proc/<pid>/stack` and storage/NFS health. Otherwise `strace -p` to see the blocking syscall (e.g. `futex` for lock wait, `read` on a socket, `connect` timing out), `lsof -p` for which file or socket that FD is, and language-specific stack dumps (jstack, py-spy dump, gdb).

**44. What is the difference between `TIME-WAIT` and `CLOSE-WAIT`, and which one indicates an application bug?**
TIME-WAIT is normal on the side that closed first, ensuring late packets don't corrupt new connections; many of them on a busy client is expected. CLOSE-WAIT means the peer closed but the local application never closed its socket; a growing number indicates a socket leak in the application.

**45. How do you make a configuration change to 300 servers safely?**
Encode it in Ansible (or your config tool), validate files before replacing (`validate:`), test with `--check --diff` and in staging, roll out with `serial` batches and health checks so a failure stops the rollout, keep it idempotent and in version control with code review, and have a rollback path. In immutable setups, bake a new image and replace instances gradually.

**46. Explain the difference between `Wants=`, `Requires=` and `After=`.**
`Wants=` and `Requires=` declare dependencies (soft and hard: with `Requires=`, if the dependency fails or stops, so does this unit). `After=` only controls ordering. They are independent; a typical pairing is `Wants=` plus `After=`.

**47. How does `sudo` work given that it needs root privileges?**
The `sudo` binary is owned by root with the SUID bit, so it runs with effective UID 0, checks the sudoers policy and authenticates the user, then executes the requested command as the target user and logs the event.

## 10.9 Command Cheat Sheet

### System information

| Task | Command |
|---|---|
| OS and version | `cat /etc/os-release`, `hostnamectl` |
| Kernel | `uname -r`, `cat /proc/cmdline` |
| CPU / memory | `nproc`, `lscpu`, `free -h`, `cat /proc/meminfo` |
| Uptime and load | `uptime`, `cat /proc/loadavg` |
| Boot timing | `systemd-analyze blame`, `systemd-analyze critical-chain` |
| Hardware | `lsblk`, `lspci`, `lsusb`, `dmidecode -t system` |
| Kernel messages | `dmesg -T`, `journalctl -k` |

### Files and text

| Task | Command |
|---|---|
| Find big files | `find / -xdev -type f -size +500M -exec ls -lh {} +` |
| Find recent changes | `find /etc -mmin -60 -type f` |
| Biggest directories | `du -xh --max-depth=1 / \| sort -h` |
| Search content | `grep -rn --include='*.conf' pattern /etc` |
| Count by field | `awk '{print $1}' f \| sort \| uniq -c \| sort -rn \| head` |
| Replace in place | `sed -i 's/old/new/g' file` |
| JSON field | `jq -r '.items[].metadata.name'` |
| Compare files | `diff -u a b`, `diff <(cmd1) <(cmd2)` |
| Archive | `tar -czf out.tgz dir`, `tar -xzf out.tgz -C dest` |
| Sync | `rsync -avz --delete src/ host:/dest/` |

### Users and permissions

| Task | Command |
|---|---|
| Who am I / groups | `id`, `whoami`, `groups` |
| Add user to group | `usermod -aG group user` |
| Service account | `useradd -r -s /usr/sbin/nologin svc` |
| Edit sudoers | `visudo -f /etc/sudoers.d/name` |
| Permissions along path | `namei -l /path/to/file` |
| ACL | `setfacl -m u:bob:rw f`, `getfacl f` |
| SUID audit | `find / -xdev -perm -4000 -type f` |
| Capabilities | `getcap file`, `setcap cap_net_bind_service=+ep file` |

### Processes

| Task | Command |
|---|---|
| Top consumers | `ps -eo pid,user,%cpu,%mem,cmd --sort=-%cpu \| head` |
| Process tree | `pstree -p`, `ps -ef --forest` |
| Find by name | `pgrep -a name` |
| Signals | `kill -TERM pid`, `kill -HUP pid`, `pkill -f pattern` |
| Open files / ports | `lsof -p pid`, `lsof -i :443`, `lsof +L1` |
| Syscalls | `strace -f -p pid -e trace=network` |
| Limits | `cat /proc/pid/limits`, `ulimit -n` |
| Priority | `nice -n 10 cmd`, `renice 5 -p pid`, `ionice -c3 -p pid` |
| Survive logout | `tmux new -s name`, `nohup cmd &` |

### systemd and logs

| Task | Command |
|---|---|
| Status | `systemctl status svc` |
| Start at boot and now | `systemctl enable --now svc` |
| Failed units | `systemctl --failed` |
| Show unit with drop-ins | `systemctl cat svc` |
| Override | `systemctl edit svc` |
| Reload unit files | `systemctl daemon-reload` |
| Clear failure state | `systemctl reset-failed svc` |
| Timers | `systemctl list-timers` |
| Service logs | `journalctl -u svc -b -n 100 -f` |
| Errors this boot | `journalctl -p err -b` |
| Previous boot | `journalctl -b -1` |
| Time window | `journalctl --since "1 hour ago"` |
| Journal size | `journalctl --disk-usage`, `journalctl --vacuum-size=500M` |
| Test logrotate | `logrotate -d /etc/logrotate.d/app` |

### Packages

| Task | Debian/Ubuntu | RHEL/Rocky/AL2023 | Alpine |
|---|---|---|---|
| Refresh | `apt update` | `dnf makecache` | `apk update` |
| Install | `apt install pkg` | `dnf install pkg` | `apk add pkg` |
| Upgrade all | `apt upgrade` | `dnf upgrade` | `apk upgrade` |
| Remove | `apt remove pkg` | `dnf remove pkg` | `apk del pkg` |
| Search | `apt search x` | `dnf search x` | `apk search x` |
| Files in package | `dpkg -L pkg` | `rpm -ql pkg` | `apk info -L pkg` |
| Owner of file | `dpkg -S /path` | `rpm -qf /path` | `apk info --who-owns /path` |
| Pin version | `apt-mark hold pkg` | `dnf versionlock add pkg` | `apk add pkg=1.2.3-r0` |
| Security updates | `unattended-upgrades` | `dnf upgrade --security` | `apk upgrade` |

### Storage

| Task | Command |
|---|---|
| Devices and fs | `lsblk -f`, `blkid` |
| Usage / inodes | `df -hT`, `df -i` |
| Partition | `parted -s /dev/sdb mklabel gpt mkpart primary 1MiB 100%` |
| Format | `mkfs.ext4 -L data /dev/sdb1`, `mkfs.xfs /dev/sdb1` |
| Mount / check fstab | `mount /dev/sdb1 /data`, `findmnt --verify`, `mount -a` |
| Who uses mount | `lsof +f -- /data`, `fuser -vm /data` |
| LVM overview | `pvs`, `vgs`, `lvs` |
| Grow LV + fs | `lvextend -r -l +100%FREE /dev/vg/lv` |
| Grow cloud disk | `growpart /dev/nvme0n1 1 && resize2fs /dev/nvme0n1p1` (or `xfs_growfs /`) |
| RAID status | `cat /proc/mdstat`, `mdadm --detail /dev/md0` |
| Disk I/O | `iostat -xz 1`, `iotop -oPa` |

### Networking

| Task | Command |
|---|---|
| Addresses / links | `ip -br addr`, `ip -br link` |
| Routes | `ip route`, `ip route get 8.8.8.8` |
| Neighbours | `ip neigh` |
| Listening sockets | `ss -tulpn` |
| Connections by state | `ss -tan state established` |
| DNS | `dig +short name`, `dig @server name`, `getent hosts name`, `resolvectl status` |
| Port test | `nc -zv host 443`, `timeout 3 bash -c '</dev/tcp/host/443'` |
| HTTP timing | `curl -w '%{time_connect} %{time_total}\n' -o /dev/null -s URL` |
| TLS cert | `openssl s_client -connect host:443 -servername host </dev/null \| openssl x509 -noout -dates` |
| Path | `mtr -rwc 50 host`, `tracepath host` |
| Packets | `tcpdump -ni any port 53 -c 20` |
| NetworkManager | `nmcli device status`, `nmcli con show` |
| netplan | `netplan try`, `netplan apply` |

### Firewalls

| Task | Command |
|---|---|
| ufw | `ufw allow OpenSSH`, `ufw default deny incoming`, `ufw enable`, `ufw status numbered` |
| firewalld | `firewall-cmd --list-all`, `firewall-cmd --permanent --add-service=https`, `firewall-cmd --reload` |
| nftables | `nft list ruleset`, `nft -c -f /etc/nftables.conf` |
| iptables | `iptables -L -n -v --line-numbers`, `iptables -t nat -L -n` |

### SSH

| Task | Command |
|---|---|
| Generate key | `ssh-keygen -t ed25519 -C "me@host"` |
| Install key | `ssh-copy-id -i ~/.ssh/id_ed25519.pub user@host` |
| Agent | `eval "$(ssh-agent -s)"`, `ssh-add`, `ssh-add -l` |
| Jump host | `ssh -J bastion user@private` |
| Local forward | `ssh -N -L 15432:db:5432 bastion` |
| Remote forward | `ssh -N -R 8080:localhost:3000 host` |
| SOCKS proxy | `ssh -N -D 1080 bastion` |
| Debug | `ssh -vvv host` |
| Test server config | `sshd -t`, `sshd -T` |
| Remove old host key | `ssh-keygen -R host` |

### Performance

| Task | Command |
|---|---|
| Overview | `top`, `htop`, `uptime` |
| CPU / run queue / swap | `vmstat 1`, `mpstat -P ALL 1` |
| Per process | `pidstat -u -r -d 1` |
| Memory | `free -h`, `ps --sort=-rss -eo pid,rss,cmd \| head` |
| Pressure | `cat /proc/pressure/{cpu,memory,io}` |
| OOM kills | `dmesg -T \| grep -i oom`, `journalctl -k \| grep -i killed` |
| History | `sar -u`, `sar -r`, `sar -n DEV` |
| Profiling | `perf top`, `perf record -g -p pid`, `perf report` |
| Kernel params | `sysctl -a`, `sysctl -w k=v`, `sysctl --system` |

### Security

| Task | Command |
|---|---|
| SELinux | `getenforce`, `ls -Z`, `ausearch -m AVC -ts recent`, `restorecon -Rv path`, `setsebool -P b on` |
| AppArmor | `aa-status`, `aa-complain prof`, `aa-enforce prof` |
| fail2ban | `fail2ban-client status sshd`, `fail2ban-client set sshd unbanip IP` |
| auditd | `auditctl -l`, `ausearch -k key -i`, `aureport --login` |
| Recent logins | `last -n 20`, `lastb`, `who`, `w` |
| Failed logins | `journalctl -u ssh \| grep -i failed` |
| Pending updates | `apt list --upgradable`, `dnf updateinfo list --security` |
| Reboot needed | `ls /var/run/reboot-required`, `dnf needs-restarting -r` |
| Audit scan | `lynis audit system`, `oscap xccdf eval ...` |

### Containers and cgroups

| Task | Command |
|---|---|
| Namespaces | `lsns`, `ls -l /proc/pid/ns` |
| New namespaces | `unshare --pid --fork --mount-proc --uts --net bash` |
| Enter a container's netns | `nsenter -t $(docker inspect -f '{{.State.Pid}}' c) -n ss -tlnp` |
| Network namespaces | `ip netns add x`, `ip netns exec x cmd` |
| cgroup tree / usage | `systemd-cgls`, `systemd-cgtop` |
| Limit a unit | `systemctl set-property svc MemoryMax=512M CPUQuota=50%` |
| Ad-hoc limited run | `systemd-run --scope -p MemoryMax=100M cmd` |
| Throttling | `cat /sys/fs/cgroup/<path>/cpu.stat` |

### Cloud and automation

| Task | Command |
|---|---|
| cloud-init status | `cloud-init status --long`, `cloud-init status --wait` |
| cloud-init logs | `/var/log/cloud-init-output.log`, `/var/log/cloud-init.log` |
| User data received | `cloud-init query userdata` |
| IMDSv2 token | `curl -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 300'` |
| Ansible ping | `ansible -i inv all -m ping` |
| Ansible ad-hoc | `ansible -i inv web -b -m package -a 'name=nginx state=present'` |
| Ansible dry run | `ansible-playbook -i inv site.yml --check --diff` |

### Bash scripting skeleton

```bash
#!/usr/bin/env bash
set -Eeuo pipefail
readonly SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
log()  { printf '%s [%s] %s\n' "$(date -u +%FT%TZ)" "$1" "${*:2}" >&2; }
die()  { log ERROR "$*"; exit 1; }
tmp=$(mktemp -d); trap 'rm -rf -- "$tmp"' EXIT
trap 'log ERROR "line $LINENO: $BASH_COMMAND"' ERR
exec 9>"/run/lock/${0##*/}.lock"; flock -n 9 || die "already running"
for c in curl jq; do command -v "$c" >/dev/null || die "missing $c"; done
usage() { echo "Usage: ${0##*/} [-v] <target>"; }
while getopts ":vh" o; do case $o in v) set -x ;; h) usage; exit 0 ;; *) usage >&2; exit 2 ;; esac; done
shift $((OPTIND - 1)); target=${1:?$(usage)}
log INFO "working on $target"
```

## Key takeaways

- Troubleshoot with a method: scope, observe with read-only commands, hypothesize, test one change, fix, prevent, write it up.
- Disk full: check bytes *and* inodes, find the space with `du -x`, look for deleted-open files, truncate rather than delete, and add rotation and alerting.
- High load: separate CPU saturation from I/O wait and steal before blaming the application; then find the process, the thread, and the code path.
- Service won't start: `systemctl status` and `journalctl -u` first, decode the result code, then reproduce by hand as the service user.
- Can't SSH: classify by symptom (resolve, timeout, refused, publickey), check the network path, and keep a second access path (SSM, console).
- DNS: compare `getent hosts` with `dig`, interpret NXDOMAIN vs SERVFAIL vs timeout, and check the resolver layer (`resolvectl`).
- Most interview questions are about *why*: be able to explain the mechanism behind every command in the cheat sheet.
