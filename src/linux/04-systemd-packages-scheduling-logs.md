# Part 4 — systemd, Packages, Scheduling and Logs

This Part covers the operational core of a Linux server: how services are started and supervised (systemd), how software is installed and patched (package managers), how work is scheduled (cron and timers), and where evidence goes when things break (logs).

## 4.1 systemd in Depth

systemd is PID 1 and the service manager on every mainstream distro except Alpine (which uses OpenRC) and minimal containers. It replaced shell-script SysV init because it offers dependency-based parallel startup, process supervision with automatic restarts, cgroup-based resource tracking (a service's child processes can't "escape"), socket activation, and a unified log (the journal).

### 4.1.1 Units

Everything systemd manages is a **unit**, defined by a file. The suffix is the type:

| Type | Purpose |
|---|---|
| `.service` | A daemon or one-shot task |
| `.socket` | A listening socket that starts a service on demand |
| `.timer` | Scheduled activation of another unit (cron replacement) |
| `.target` | A named group / synchronization point (like runlevels) |
| `.mount`, `.automount` | Filesystem mounts (generated from fstab too) |
| `.path` | Start a unit when a file/directory changes |
| `.slice` | A cgroup node for resource control |
| `.device`, `.swap`, `.scope` | Kernel devices, swap, externally created process groups |

Unit file locations, in order of precedence:

| Path | Owner |
|---|---|
| `/etc/systemd/system/` | Administrator (you). Highest precedence |
| `/run/systemd/system/` | Runtime, generated |
| `/usr/lib/systemd/system/` (`/lib/...` on Debian) | Packages. Don't edit these; upgrades overwrite them |

### 4.1.2 systemctl essentials

```bash
systemctl status nginx              # state, PID, cgroup, recent logs
sudo systemctl start|stop|restart nginx
sudo systemctl reload nginx         # re-read config without restart (if supported)
sudo systemctl enable --now nginx   # start at boot AND now
sudo systemctl disable nginx
sudo systemctl mask nginx           # make it impossible to start (symlink to /dev/null)
systemctl is-active nginx; systemctl is-enabled nginx; systemctl is-failed nginx
systemctl list-units --type=service --state=running
systemctl --failed                  # what's broken right now
systemctl list-unit-files --state=enabled
systemctl cat nginx                 # show the unit file(s) in effect
systemctl show nginx -p MainPID,Restart,MemoryCurrent
systemctl list-dependencies multi-user.target
sudo systemctl daemon-reload        # after editing unit files
```

```text
* nginx.service - A high performance web server and a reverse proxy server
     Loaded: loaded (/usr/lib/systemd/system/nginx.service; enabled; preset: enabled)
     Active: active (running) since Mon 2026-10-12 09:14:02 UTC; 2h 3min ago
       Docs: man:nginx(8)
   Main PID: 812 (nginx)
      Tasks: 3 (limit: 4558)
     Memory: 5.1M (peak: 6.0M)
        CPU: 147ms
     CGroup: /system.slice/nginx.service
             |-812 "nginx: master process /usr/sbin/nginx -g daemon on; master_process on;"
             `-813 "nginx: worker process"
```

`enable` means "start at boot" (it creates a symlink in a target's `.wants/` directory). `start` means "start now". They are independent, which is why `enable --now` exists.

### 4.1.3 Anatomy of a service unit

```ini
# /etc/systemd/system/myapp.service
[Unit]
Description=My API service
Documentation=https://git.example.com/myapp
After=network-online.target postgresql.service
Wants=network-online.target
Requires=postgresql.service

[Service]
Type=simple
User=myapp
Group=myapp
WorkingDirectory=/opt/myapp
EnvironmentFile=-/etc/myapp/env
ExecStartPre=/opt/myapp/bin/check-config
ExecStart=/opt/myapp/bin/server --port 8080
ExecReload=/bin/kill -HUP $MAINPID
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
```

Key directives explained:

- **`After=` vs `Requires=`/`Wants=`**: ordering and dependency are separate. `Wants=` pulls a unit in (soft); `Requires=` pulls it in and stops us if it stops (hard); `After=` only orders. You usually need both `Wants=` and `After=`.
- **`network-online.target`**: "network is configured". `network.target` only means the network stack is up, which isn't enough for apps that connect elsewhere at startup.
- **`Type=`**:
  - `simple` (default): the `ExecStart` process *is* the service. Don't let it daemonize.
  - `exec`: like simple, but startup counts as successful only once the binary has been executed.
  - `forking`: traditional daemons that fork and exit; pair with `PIDFile=`.
  - `oneshot`: runs to completion (setup scripts); often with `RemainAfterExit=yes`.
  - `notify`: the app tells systemd it's ready via `sd_notify()`.
- **`Restart=`**: `no`, `on-failure` (non-zero exit, signal, timeout), `always`. Combined with `RestartSec=`, and rate-limited by `StartLimitIntervalSec=`/`StartLimitBurst=` in `[Unit]`.
- **`EnvironmentFile=-/path`**: the `-` means "ignore if missing".
- **`[Install] WantedBy=`**: what `enable` hooks into.

### 4.1.4 Overriding vendor units safely

Never edit files in `/usr/lib/systemd/system`. Use a drop-in:

```bash
sudo systemctl edit nginx
```

This opens `/etc/systemd/system/nginx.service.d/override.conf`:

```ini
[Service]
LimitNOFILE=200000
Restart=always
```

To *replace* a list-type setting like `ExecStart`, clear it first:

```ini
[Service]
ExecStart=
ExecStart=/usr/sbin/nginx -g 'daemon off;' -c /etc/nginx/custom.conf
```

`systemctl edit` runs `daemon-reload` for you. `systemd-delta` lists all overrides on the system, which is handy when inheriting a mystery server.

### 4.1.5 Hardening and resource controls

systemd can sandbox a service with a few lines, often more effectively than you'd achieve manually:

```ini
[Service]
NoNewPrivileges=yes
ProtectSystem=strict          # /usr, /etc read-only
ProtectHome=yes
PrivateTmp=yes
ReadWritePaths=/var/lib/myapp /var/log/myapp
StateDirectory=myapp          # creates /var/lib/myapp owned by User=
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
AmbientCapabilities=CAP_NET_BIND_SERVICE
MemoryMax=512M
CPUQuota=50%
TasksMax=200
```

```bash
systemd-analyze security myapp.service   # scores exposure, lists suggestions
systemd-cgtop                            # live resource use per unit
```

**DynamicUser=yes** goes further, allocating a transient UID for the service's lifetime.

### 4.1.6 Debugging a unit that won't start

```bash
systemctl status myapp -l --no-pager
journalctl -u myapp -b --no-pager -n 100
systemd-analyze verify /etc/systemd/system/myapp.service
sudo -u myapp /opt/myapp/bin/server --port 8080   # run by hand as the service user
```

Typical causes: wrong path in `ExecStart` (must be absolute), missing execute bit, the user can't read config, port in use, SELinux denial, `Type=forking` mismatch, or the start-limit being hit (`systemctl reset-failed myapp` clears it). Part 10 walks through this scenario end to end.

## 4.2 journald and journalctl

systemd-journald collects stdout/stderr of all services, kernel messages, and syslog messages into a structured, indexed binary journal.

```bash
journalctl -u nginx                  # one unit
journalctl -u nginx -f               # follow
journalctl -u nginx --since "1 hour ago"
journalctl --since "2026-10-12 09:00" --until "2026-10-12 10:00"
journalctl -b                        # this boot; -b -1 previous boot
journalctl -k                        # kernel ring buffer (like dmesg)
journalctl -p err -b                 # priority err and worse
journalctl _PID=1234
journalctl _SYSTEMD_UNIT=sshd.service + _COMM=sudo   # OR of matches
journalctl -o json-pretty -n 1       # see all structured fields
journalctl -o cat -u myapp           # message only
journalctl --list-boots
journalctl --disk-usage
sudo journalctl --vacuum-size=500M   # shrink
sudo journalctl --vacuum-time=14d
```

Persistence: if `/var/log/journal/` exists (or `Storage=persistent` is set), the journal survives reboots; otherwise it lives in `/run/log/journal` and is lost. Ubuntu and RHEL 9+ default to persistent. Configure limits in `/etc/systemd/journald.conf`:

```ini
[Journal]
Storage=persistent
SystemMaxUse=1G
MaxRetentionSec=1month
```

> **Tip:** `journalctl -b -1 -p warning` after an unexpected reboot shows warnings from the boot that crashed. If nothing is there, persistence is probably off.

## 4.3 rsyslog and logrotate

### 4.3.1 rsyslog

Many systems still run **rsyslog** alongside journald, writing classic text files: `/var/log/syslog` and `/var/log/auth.log` (Debian/Ubuntu) or `/var/log/messages` and `/var/log/secure` (RHEL). It's also the usual way to forward logs to a central server.

Syslog messages have a **facility** (auth, cron, daemon, kern, local0–7...) and a **severity** (emerg, alert, crit, err, warning, notice, info, debug).

```conf
# /etc/rsyslog.d/50-myapp.conf
# Write local0 messages to their own file
local0.*    /var/log/myapp.log

# Forward everything to a central collector over TCP with a disk-assisted queue
# (legacy one-line form: *.* @@logs.example.com:514  where @@ = TCP, @ = UDP)
*.*  action(type="omfwd" target="logs.example.com" port="514" protocol="tcp"
            queue.type="LinkedList" queue.filename="fwd" action.resumeRetryCount="-1")
```

```bash
sudo systemctl restart rsyslog
logger -p local0.info "test from $(hostname)"   # send a test message
```

In modern stacks you'll often ship logs with an agent (Fluent Bit, Vector, the OpenTelemetry Collector, CloudWatch agent) reading the journal or files directly.

### 4.3.2 logrotate

Text logs grow forever unless rotated. `logrotate` runs daily (systemd timer `logrotate.timer` or cron) and reads `/etc/logrotate.conf` plus `/etc/logrotate.d/*`.

```conf
# /etc/logrotate.d/myapp
/var/log/myapp/*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    maxsize 500M
    create 0640 myapp adm
    sharedscripts
    postrotate
        systemctl kill -s USR1 myapp.service >/dev/null 2>&1 || true
    endscript
}
```

Why `postrotate`? After renaming `app.log` to `app.log.1`, the app still holds the old file descriptor and keeps writing to the renamed file. Signalling it to reopen logs fixes that. If the app can't reopen, use `copytruncate` (copies then truncates in place; may lose a few lines).

```bash
sudo logrotate -d /etc/logrotate.d/myapp    # dry run / debug
sudo logrotate -f /etc/logrotate.d/myapp    # force rotation now
```

## 4.4 Package Management

Package managers install software with dependency resolution, verify signatures, track which files belong to which package, and make patching a single command. **Prefer distro packages** over hand-installed binaries: they get security updates.

### 4.4.1 Debian/Ubuntu: apt and dpkg

```bash
sudo apt update                      # refresh package index (do this first)
sudo apt upgrade                     # upgrade installed packages
sudo apt full-upgrade                # also allow removals for dependency changes
sudo apt install -y nginx=1.24.*     # install (optionally a version)
sudo apt remove nginx; sudo apt purge nginx   # purge also removes config
sudo apt autoremove
apt search redis; apt show redis-server
apt list --installed | grep nginx
apt-cache policy nginx               # candidate version and which repo
sudo apt-mark hold kubelet           # pin version
dpkg -l | grep ssl                   # installed packages (low level)
dpkg -L nginx                        # files from a package
dpkg -S /usr/sbin/nginx              # which package owns a file
sudo dpkg -i ./pkg.deb && sudo apt -f install   # local .deb, fix deps
```

In scripts and Dockerfiles use `apt-get` (stable output) with `DEBIAN_FRONTEND=noninteractive`:

```bash
apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
  && rm -rf /var/lib/apt/lists/*
```

Adding a third-party repository the modern way (signed-by keyring, not the deprecated `apt-key`):

```bash
sudo install -d -m 0755 /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release; echo $VERSION_CODENAME) stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list
sudo apt update
```

Ubuntu 24.04 also supports the deb822 `.sources` format in `/etc/apt/sources.list.d/`.

### 4.4.2 RHEL/Rocky/Alma/Fedora/Amazon Linux: dnf and rpm

`yum` on RHEL 8+ is an alias for `dnf`.

```bash
sudo dnf check-update
sudo dnf upgrade -y
sudo dnf upgrade --security          # security errata only (RHEL)
sudo dnf install -y nginx
sudo dnf remove nginx
dnf search redis; dnf info redis
dnf provides /usr/bin/dig            # which package provides a file -> bind-utils
dnf list installed | grep kernel
dnf repolist
sudo dnf install -y epel-release     # Extra Packages for Enterprise Linux (Rocky/Alma)
sudo dnf history; sudo dnf history undo 15
sudo dnf install 'dnf-command(versionlock)' && sudo dnf versionlock add kubelet
rpm -qa | grep openssl
rpm -ql nginx                        # files in package
rpm -qf /etc/nginx/nginx.conf        # owner of file
rpm -V nginx                         # verify files against package metadata
```

Repositories are defined in `/etc/yum.repos.d/*.repo`:

```ini
[myrepo]
name=My internal repo
baseurl=https://repo.example.com/el9/$basearch
enabled=1
gpgcheck=1
gpgkey=https://repo.example.com/RPM-GPG-KEY
```

### 4.4.3 Alpine: apk

```bash
apk update
apk add --no-cache curl bash        # --no-cache: don't keep index (containers)
apk del curl
apk search nginx; apk info -L nginx
apk add --virtual .build-deps gcc musl-dev && ... && apk del .build-deps
```

### 4.4.4 Building from source

Sometimes you need a version or build flag the distro doesn't offer.

```bash
sudo apt install -y build-essential   # or: sudo dnf groupinstall "Development Tools"
curl -LO https://example.org/tool-2.4.1.tar.gz
sha256sum tool-2.4.1.tar.gz            # compare with published checksum!
tar xzf tool-2.4.1.tar.gz && cd tool-2.4.1
./configure --prefix=/usr/local
make -j"$(nproc)"
sudo make install
```

Downsides: no automatic security updates, no clean uninstall, no inventory. Better options, in order: the distro package, the vendor's official repo, a static binary from the vendor's releases (verify checksum), building your own `.deb`/`.rpm` (e.g. with `fpm` or proper packaging), or running it in a container.

> **Note:** Snap and Flatpak exist (Ubuntu ships some tools as snaps), but servers in DevOps environments rarely rely on them.

### 4.4.5 Patching strategy

- Enable unattended security updates: `unattended-upgrades` on Ubuntu, `dnf-automatic` on RHEL family.
- After kernel or glibc/openssl updates, services must restart or the host must reboot. Check: `needrestart` (Ubuntu), `dnf needs-restarting -r` (RHEL), `/var/run/reboot-required` (Ubuntu).
- Patch in rings: dev → staging → a canary slice of prod → everything. In immutable infrastructure you rebuild the image instead of patching in place.

## 4.5 Scheduling: cron and systemd Timers

### 4.5.1 cron

```text
# m   h   dom mon dow   command
  */5 *   *   *   *     /usr/local/bin/healthcheck.sh
  0   2   *   *   *     /usr/local/bin/backup.sh >> /var/log/backup.log 2>&1
  30  6   *   *   1-5   /usr/local/bin/report.sh       # weekdays 06:30
  0   0   1   *   *     /usr/local/bin/monthly.sh
  @reboot               /usr/local/bin/on-boot.sh
```

```bash
crontab -e            # edit your crontab
crontab -l            # list
sudo crontab -u deploy -l
ls /etc/cron.d /etc/cron.daily   # system cron (files in cron.d have a USER field)
```

cron pitfalls that bite everyone:

- **Minimal environment**: `PATH` is usually just `/usr/bin:/bin`. Use absolute paths or set `PATH=` at the top of the crontab.
- **`%` is special** in crontab lines (newline). Escape it: `date +\%F`.
- **Output** is mailed (often nowhere). Redirect to a log file or `logger`.
- **Overlap**: if a job runs longer than its interval, copies pile up. Use `flock -n /run/lock/job.lock cmd`.
- **Timezone**: system timezone unless `CRON_TZ=` is set.

### 4.5.2 systemd timers

Timers fix most cron pitfalls: logs go to the journal, runs don't overlap (a service that's still active isn't started again), missed runs can be caught up (`Persistent=true`), and you get resource limits and dependencies for free.

```ini
# /etc/systemd/system/backup.service
[Unit]
Description=Nightly backup

[Service]
Type=oneshot
User=backup
ExecStart=/usr/local/bin/backup.sh
```

```ini
# /etc/systemd/system/backup.timer
[Unit]
Description=Run backup nightly

[Timer]
OnCalendar=*-*-* 02:00:00
RandomizedDelaySec=15m
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now backup.timer
systemctl list-timers
systemd-analyze calendar "Mon..Fri 06:30"     # validate an expression
sudo systemctl start backup.service           # run now, manually
journalctl -u backup.service
```

Other timer forms: `OnBootSec=5min`, `OnUnitActiveSec=1h` (monotonic, relative to last run), `OnCalendar=hourly|daily|weekly`.

> **Interview:** "Cron vs systemd timers?" Cron is universal and simple. Timers integrate with the journal, avoid overlapping runs, support catch-up after downtime and randomized delay, can depend on other units, and can be sandboxed and resource-limited. On systemd hosts, prefer timers for anything important.

## 4.6 Lab: Deploy a Supervised Service with a Timer

Goal: run a small HTTP service under systemd with hardening, automatic restart, log rotation, and a scheduled health check.

1. Create a service user and app:

   ```bash
   sudo useradd -r -s /usr/sbin/nologin -d /opt/hello hello
   sudo mkdir -p /opt/hello
   sudo tee /opt/hello/server.py > /dev/null <<'EOF'
   import http.server, socketserver, os, sys
   PORT = int(os.environ.get("PORT", "8080"))
   class H(http.server.SimpleHTTPRequestHandler):
       def do_GET(self):
           self.send_response(200); self.end_headers()
           self.wfile.write(b"hello from systemd\n")
           print(f"GET {self.path}", flush=True)
   socketserver.TCPServer.allow_reuse_address = True
   with socketserver.TCPServer(("", PORT), H) as s:
       print(f"listening on {PORT}", flush=True); s.serve_forever()
   EOF
   echo "PORT=8080" | sudo tee /etc/hello.env
   ```

2. Write the unit:

   ```bash
   sudo tee /etc/systemd/system/hello.service > /dev/null <<'EOF'
   [Unit]
   Description=Hello demo service
   After=network-online.target
   Wants=network-online.target

   [Service]
   User=hello
   EnvironmentFile=/etc/hello.env
   ExecStart=/usr/bin/python3 /opt/hello/server.py
   Restart=on-failure
   RestartSec=2
   NoNewPrivileges=yes
   ProtectSystem=strict
   ProtectHome=yes
   PrivateTmp=yes
   MemoryMax=100M

   [Install]
   WantedBy=multi-user.target
   EOF
   sudo systemctl daemon-reload
   sudo systemctl enable --now hello
   systemctl status hello --no-pager
   curl -s localhost:8080
   ```

3. Prove supervision works. Kill it with a signal that counts as failure and watch it come back:

   ```bash
   sudo systemctl kill -s KILL hello
   sleep 3; systemctl status hello --no-pager | head -5
   journalctl -u hello -n 20 --no-pager
   ```

4. Add a drop-in override changing the port:

   ```bash
   sudo systemctl edit hello
   ```

   Add these lines in the editor, then save:

   ```ini
   [Service]
   EnvironmentFile=
   Environment=PORT=9090
   ```

   ```bash
   sudo systemctl restart hello && curl -s localhost:9090
   systemctl cat hello
   ```

   Why the empty `EnvironmentFile=` line? Variables read from `EnvironmentFile=` override those set with `Environment=`, so without clearing the list the file's `PORT=8080` would still win. An empty assignment resets a list-type setting, the same trick used for `ExecStart=`.

5. Check its security score: `systemd-analyze security hello`.

6. Create a health-check timer every minute:

   ```bash
   sudo tee /etc/systemd/system/hello-check.service > /dev/null <<'EOF'
   [Unit]
   Description=Check hello service
   [Service]
   Type=oneshot
   ExecStart=/bin/sh -c 'curl -fsS --max-time 3 localhost:9090 >/dev/null || systemctl restart hello'
   EOF
   sudo tee /etc/systemd/system/hello-check.timer > /dev/null <<'EOF'
   [Timer]
   OnBootSec=1min
   OnUnitActiveSec=1min
   [Install]
   WantedBy=timers.target
   EOF
   sudo systemctl daemon-reload && sudo systemctl enable --now hello-check.timer
   systemctl list-timers hello-check.timer
   ```

7. Install a package and query it both ways: on Ubuntu `sudo apt install -y jq && dpkg -L jq | head && dpkg -S "$(command -v jq)"`; on Rocky `sudo dnf install -y jq && rpm -ql jq | head && rpm -qf "$(command -v jq)"`.

8. Create a logrotate config for a test file and force a rotation:

   ```bash
   sudo mkdir -p /var/log/hello && echo test | sudo tee /var/log/hello/app.log
   printf '/var/log/hello/*.log {\n daily\n rotate 3\n compress\n missingok\n}\n' | sudo tee /etc/logrotate.d/hello
   sudo logrotate -f /etc/logrotate.d/hello && ls /var/log/hello
   ```

9. Clean up: disable and remove the units, `daemon-reload`, delete the user.

## Key takeaways

- systemd units describe services declaratively; `enable` (boot) and `start` (now) are different things.
- Separate ordering (`After=`) from dependency (`Wants=`/`Requires=`); use `network-online.target` for apps that need the network.
- Never edit vendor units; use `systemctl edit` drop-ins and `daemon-reload`.
- Sandbox services with `ProtectSystem`, `NoNewPrivileges`, `PrivateTmp`, and set resource limits with `MemoryMax`/`CPUQuota`.
- `journalctl -u UNIT -b -p err --since` answers most "what happened" questions; make the journal persistent.
- logrotate needs the app to reopen its log (postrotate signal) or `copytruncate`.
- apt/dnf/apk all do the same jobs: refresh, install, upgrade, query ownership. Prefer distro or vendor packages over source builds.
- Prefer systemd timers to cron for important jobs; if you use cron, mind PATH, `%`, output and overlaps.
