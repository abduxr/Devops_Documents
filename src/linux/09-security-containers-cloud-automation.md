# Part 9 — Security Hardening, Containers Under the Hood, Cloud and Automation

This Part covers the advanced topics that distinguish a DevOps engineer from a sysadmin: hardening hosts to a standard, understanding the kernel features that containers are made of, bootstrapping cloud instances, and handing configuration over to automation.

## 9.1 Security Hardening

Hardening is about reducing attack surface and adding layers so a single failure doesn't become a breach. The principles: **least privilege, minimal installed software, patch fast, log everything important, and verify continuously**.

### 9.1.1 A baseline checklist

| Area | Action |
|---|---|
| Patching | Unattended security updates; reboot or live-patch for kernel CVEs |
| Accounts | No shared accounts; root login disabled; sudo via groups; remove stale users |
| SSH | Keys only, no root, `AllowGroups`, restricted source IPs or SSM/VPN only |
| Services | Remove/disable anything not needed (`systemctl list-unit-files --state=enabled`, `ss -tulpn`) |
| Firewall | Default deny inbound; allow only required ports and sources |
| Filesystems | `/tmp` with `nodev,nosuid,noexec` where feasible; separate `/var/log` on long-lived hosts |
| MAC | SELinux enforcing / AppArmor enforcing |
| Logging | Persistent journal, auditd, logs shipped off-host |
| Integrity | AIDE file integrity; SUID audit; package verification (`rpm -Va`, `debsums`) |
| Time | NTP via chrony or systemd-timesyncd (TLS, Kerberos and log correlation depend on it) |
| Secrets | Not in scripts, env files world-readable, or shell history; use a secrets manager |

### 9.1.2 Mandatory Access Control: SELinux

Normal Unix permissions are **discretionary**: the owner decides. **Mandatory Access Control** adds a system-wide policy that even root processes are subject to. If nginx is compromised, SELinux can still stop it from reading `/home` or connecting to the database port, because the policy says the `httpd_t` domain may not.

SELinux (default on RHEL, Rocky, Alma, Fedora; present on Amazon Linux 2023 in permissive mode) labels every process and file with a **context** `user:role:type:level`. Policy rules are mostly about **types**.

```bash
getenforce                         # Enforcing / Permissive / Disabled
sestatus
ls -Z /var/www/html                # file contexts
ps -eZ | grep nginx                # process domain: httpd_t
sudo setenforce 0                  # permissive until reboot (for diagnosis only!)
```

```text
-rw-r--r--. root root unconfined_u:object_r:httpd_sys_content_t:s0 index.html
```

Three things fix 95% of SELinux problems:

1. **Wrong file label.** Files moved (not copied) keep their old label; custom paths have no rule.

   ```bash
   sudo semanage fcontext -a -t httpd_sys_content_t "/srv/site(/.*)?"
   sudo restorecon -Rv /srv/site
   ```

2. **Booleans** toggle common policy options.

   ```bash
   getsebool -a | grep httpd
   sudo setsebool -P httpd_can_network_connect on   # let nginx proxy to upstream apps
   ```

3. **Non-standard ports.**

   ```bash
   sudo semanage port -l | grep http_port_t
   sudo semanage port -a -t http_port_t -p tcp 8081
   ```

Diagnose denials:

```bash
sudo ausearch -m AVC -ts recent
sudo journalctl -t setroubleshoot      # human-readable suggestions, if setroubleshoot installed
sudo audit2why < /var/log/audit/audit.log | tail
```

```text
type=AVC msg=audit(1760262000.123:812): avc:  denied  { name_connect } for  pid=812 comm="nginx" dest=8080 scontext=system_u:system_r:httpd_t:s0 tcontext=system_u:object_r:http_cache_port_t:s0 tclass=tcp_socket permissive=0
```

> **Warning:** "Disable SELinux" is the most common bad advice on the internet. Use `setenforce 0` briefly to *confirm* SELinux is the cause, then fix the label, boolean or port and set it back to enforcing. As a last resort `audit2allow -M mypolicy` builds a custom module, but review what it allows.

### 9.1.3 AppArmor

Ubuntu and Debian (and SUSE) use **AppArmor**, which confines programs by **path-based profiles** rather than labels. Simpler to read, slightly less granular.

```bash
sudo aa-status                          # loaded profiles and their mode
sudo aa-complain /usr/sbin/nginx        # log but don't block
sudo aa-enforce /usr/sbin/nginx
ls /etc/apparmor.d/
sudo journalctl -k | grep -i apparmor   # DENIED messages
```

Container runtimes apply a default AppArmor profile (`docker-default`) to containers on Ubuntu hosts.

### 9.1.4 fail2ban

fail2ban watches logs for repeated failures (SSH brute force, web login abuse) and bans offending IPs via the firewall.

```ini
# /etc/fail2ban/jail.local
[DEFAULT]
bantime  = 1h
findtime = 10m
maxretry = 5
backend  = systemd
ignoreip = 127.0.0.1/8 10.0.0.0/8

[sshd]
enabled = true
```

```bash
sudo systemctl enable --now fail2ban
sudo fail2ban-client status sshd
sudo fail2ban-client set sshd unbanip 203.0.113.42
```

With key-only SSH, brute force can't succeed, but fail2ban still reduces log noise and load. Restricting port 22 at the security group is better still.

### 9.1.5 auditd

The Linux audit system records security-relevant events in the kernel: file access, syscalls, logins, sudo, changes to identity files. It's required by most compliance frameworks (PCI DSS, SOC 2, HIPAA, FedRAMP).

```bash
sudo systemctl enable --now auditd
sudo auditctl -l                             # active rules
sudo auditctl -w /etc/passwd -p wa -k identity
sudo auditctl -w /etc/sudoers -p wa -k sudoers
```

Persist rules in `/etc/audit/rules.d/`:

```text
# /etc/audit/rules.d/50-devops.rules
-w /etc/passwd -p wa -k identity
-w /etc/shadow -p wa -k identity
-w /etc/group -p wa -k identity
-w /etc/sudoers -p wa -k sudoers
-w /etc/sudoers.d/ -p wa -k sudoers
-w /etc/ssh/sshd_config -p wa -k sshd
-a always,exit -F arch=b64 -S execve -F euid=0 -F auid>=1000 -F auid!=unset -k root-commands
```

```bash
sudo augenrules --load
sudo ausearch -k identity -i --start today
sudo aureport --login --summary -i
```

`auid` (audit UID, the "login UID") survives `sudo` and `su`, so audit logs show which human actually ran a root command.

### 9.1.6 CIS Benchmarks and compliance scanning

The **CIS Benchmarks** are consensus hardening guides per OS (Ubuntu 24.04, RHEL 9, Amazon Linux 2023...), with Level 1 (practical baseline) and Level 2 (stricter) profiles. Don't apply them by hand:

- **OpenSCAP**: `oscap xccdf eval --profile cis ...` with the SCAP Security Guide content (RHEL family), generating HTML reports and remediation scripts.
- **Ubuntu Security Guide**: `usg audit cis_level1_server` / `usg fix` (Ubuntu Pro).
- **Lynis**: `sudo lynis audit system` for a quick, distro-agnostic score and suggestions.
- **Ansible roles** (e.g. ansible-lockdown) or hardened cloud images (CIS Hardened AMIs).

Treat findings as a backlog: some controls break workloads (e.g. `noexec` on `/tmp` breaks some installers).

### 9.1.7 Patching and vulnerability response

- Subscribe to your distro's security notices (USN for Ubuntu, RHSA for Red Hat, ALAS for Amazon Linux).
- Check exposure: `apt list --upgradable`, `dnf updateinfo list --security`, `dnf updateinfo info CVE-2024-6387`.
- Kernel updates need a reboot unless you use **live patching** (Canonical Livepatch, kpatch on RHEL). Know your current kernel (`uname -r`) vs installed (`ls /boot`).
- In immutable setups, rebuild golden images (Packer) or container base images on every patch release and redeploy.

## 9.2 Namespaces and cgroups: the Bridge to Containers

A container is **not** a VM. It's an ordinary Linux process (or group of processes) that the kernel isolates with **namespaces** (what it can *see*) and constrains with **cgroups** (what it can *use*), started from a filesystem image, with capabilities dropped and a seccomp filter applied.

```text
                 Host kernel (shared by all containers)
  +----------------------------------------------------------------+
  |  Container A                     Container B                   |
  |  PID ns: sees itself as PID 1    PID ns: own PID 1             |
  |  NET ns: own eth0, IP, routes    NET ns: own eth0              |
  |  MNT ns: overlayfs rootfs        MNT ns: different rootfs      |
  |  UTS ns: own hostname            cgroup: memory.max=512M       |
  |  cgroup: cpu.max=50%                                           |
  +----------------------------------------------------------------+
```

### 9.2.1 Namespaces

| Namespace | Isolates | Effect in a container |
|---|---|---|
| `pid` | Process IDs | App sees itself as PID 1; can't see host processes |
| `net` | Interfaces, IPs, routes, ports, firewall | Own `eth0`, can bind port 80 independently |
| `mnt` | Mount table | Own root filesystem |
| `uts` | Hostname, domain name | Own hostname |
| `ipc` | SysV IPC, POSIX message queues | No shared memory with host |
| `user` | UID/GID mappings | Root inside maps to unprivileged UID outside (rootless containers) |
| `cgroup` | View of the cgroup tree | Sees its own cgroup as root |
| `time` | Some clocks | Rarely used |

```bash
ls -l /proc/$$/ns                  # namespaces of your shell
lsns                               # all namespaces on the system
sudo lsns -t net
```

Build a "container" by hand with `unshare`:

```bash
sudo unshare --pid --fork --mount-proc --uts --net bash
hostname container-demo; hostname    # changes only inside
ps aux                               # you are PID 1, only a few processes visible
ip -br link                          # only 'lo', and it's down
exit
```

Enter an existing container's namespaces from the host (great for debugging minimal images that have no shell tools):

```bash
pid=$(docker inspect -f '{{.State.Pid}}' web)
sudo nsenter -t "$pid" -n ss -tlnp        # host's ss binary, container's network namespace
sudo nsenter -t "$pid" -n tcpdump -ni eth0
```

Network namespaces with a virtual Ethernet pair, the basis of Docker's bridge networking and Kubernetes CNIs:

```bash
sudo ip netns add demo
sudo ip link add veth-host type veth peer name veth-demo
sudo ip link set veth-demo netns demo
sudo ip addr add 10.200.0.1/24 dev veth-host && sudo ip link set veth-host up
sudo ip netns exec demo ip addr add 10.200.0.2/24 dev veth-demo
sudo ip netns exec demo ip link set veth-demo up
sudo ip netns exec demo ip link set lo up
ping -c 2 10.200.0.2
sudo ip netns del demo
```

### 9.2.2 cgroups v2

**Control groups** organise processes into a hierarchy and apply resource limits and accounting per group. All current distros use the **unified cgroup v2** hierarchy mounted at `/sys/fs/cgroup`. systemd manages it: every service gets its own cgroup under `system.slice`, user sessions under `user.slice`.

```bash
stat -fc %T /sys/fs/cgroup        # cgroup2fs = v2
systemd-cgls                      # tree of cgroups and processes
systemd-cgtop                     # live usage
cat /proc/self/cgroup             # 0::/user.slice/user-1000.slice/session-3.scope
ls /sys/fs/cgroup/system.slice/nginx.service/
cat /sys/fs/cgroup/system.slice/nginx.service/memory.current
```

Key controller files:

| File | Meaning |
|---|---|
| `cpu.max` | `quota period`, e.g. `50000 100000` = 0.5 CPU |
| `cpu.weight` | Relative share under contention (default 100) |
| `memory.max` | Hard limit; exceeding it triggers cgroup OOM |
| `memory.high` | Soft limit; throttles and reclaims above it |
| `memory.current`, `memory.stat` | Usage and breakdown |
| `memory.events` | Counts of `high`, `max`, `oom`, `oom_kill` events |
| `io.max` | Per-device IOPS/bandwidth limits |
| `pids.max` | Max processes (fork bomb protection) |
| `cpu.stat` | Includes `nr_throttled`, `throttled_usec` |

Set limits through systemd rather than writing files directly:

```bash
sudo systemctl set-property nginx.service MemoryMax=512M CPUQuota=150%
sudo systemd-run --scope -p MemoryMax=100M -p CPUQuota=20% ./batch.sh   # ad-hoc
```

How this maps to containers: `docker run --memory=512m --cpus=1.5` writes `memory.max` and `cpu.max` in the container's cgroup. Kubernetes `resources.limits.memory` becomes `memory.max`; `limits.cpu` becomes `cpu.max` (CFS quota); `requests.cpu` becomes `cpu.weight`.

> **Interview:** "Why is my container's latency spiky even though average CPU is low?" CPU limits are enforced per scheduling period (100 ms by default). A multi-threaded app can burn its whole quota in the first 20 ms and is then **throttled** for the remaining 80 ms. Check `nr_throttled` and `throttled_usec` in `cpu.stat`. Fixes: raise or remove the CPU limit, or reduce thread counts.

### 9.2.3 The rest of a container

- **Root filesystem**: image layers stacked with **overlayfs** (read-only lower layers + writable upper layer). `mount | grep overlay` on a Docker host shows them.
- **Capabilities**: runtimes drop most capabilities by default; `--privileged` gives them all back (avoid).
- **seccomp**: a filter blocking dangerous syscalls (default Docker profile blocks several dozen, such as `kexec_load`, `mount` without CAP_SYS_ADMIN).
- **LSM**: SELinux/AppArmor labels confine the container further.
- **Runtimes**: `runc` / `crun` implement all of this per the OCI spec; containerd or CRI-O manage images and lifecycle; Docker and Kubernetes sit on top.

Since all containers share the host kernel, a kernel vulnerability or a container running as root with excessive privileges can mean host compromise. That's why you run as non-root, drop capabilities, use read-only root filesystems, and keep nodes patched. For stronger isolation, gVisor or microVMs (Firecracker, Kata Containers) add a separate kernel boundary.

## 9.3 Linux in the Cloud

### 9.3.1 Cloud images and cloud-init

Cloud VMs boot from images (AMIs on AWS) that are generic. **cloud-init** runs on first boot to personalise them: it reads **metadata** (instance ID, hostname, network, SSH public key) and **user data** you supplied, then sets hostname, creates users, installs SSH keys, grows the root partition, writes files, installs packages and runs scripts.

```text
 Boot -> cloud-init stages:
   init-local  (find datasource, configure network)
   init        (disk setup, growpart, SSH host keys, users/keys)
   config      (packages, write_files, ntp ...)
   final       (runcmd, user scripts, "cloud-init finished")
```

User data can be a shell script (starts with `#!`) or a **cloud-config** YAML (starts with `#cloud-config`):

```yaml
#cloud-config
hostname: web-01
timezone: UTC
package_update: true
package_upgrade: true
packages:
  - nginx
  - jq
  - chrony
users:
  - default
  - name: deploy
    groups: [sudo]
    shell: /bin/bash
    sudo: "ALL=(ALL) NOPASSWD:ALL"
    ssh_authorized_keys:
      - ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA... deploy@ci
write_files:
  - path: /etc/sysctl.d/90-web.conf
    content: |
      net.core.somaxconn = 4096
  - path: /var/www/html/index.html
    content: "<h1>web-01</h1>"
runcmd:
  - sysctl --system
  - systemctl enable --now nginx
final_message: "cloud-init done after $UPTIME seconds"
```

Debugging cloud-init:

```bash
cloud-init status --long
cloud-init status --wait          # block until finished (useful in provisioning scripts)
sudo cat /var/log/cloud-init-output.log   # stdout/stderr of your scripts
sudo less /var/log/cloud-init.log         # detailed internal log
sudo cloud-init query userdata            # what user data the instance received
sudo cloud-init schema --system           # validate cloud-config
sudo cloud-init clean --logs && sudo reboot   # re-run as if first boot (testing images)
```

### 9.3.2 EC2 specifics

**User data** on EC2 runs once, as root, at first boot, via cloud-init. A typical shell-style user data:

```bash
#!/bin/bash
set -euxo pipefail
exec > >(tee /var/log/user-data.log) 2>&1     # capture everything

dnf -y install nginx amazon-cloudwatch-agent
TOKEN=$(curl -sX PUT "http://169.254.169.254/latest/api/token" \
        -H "X-aws-ec2-metadata-token-ttl-seconds: 300")
IID=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" \
        http://169.254.169.254/latest/meta-data/instance-id)
echo "<h1>$IID</h1>" > /usr/share/nginx/html/index.html
systemctl enable --now nginx
```

Key points:

- The **Instance Metadata Service (IMDS)** at `169.254.169.254` provides instance identity, network info and **temporary IAM role credentials**. Require **IMDSv2** (token-based, shown above): it blocks the classic SSRF attack where a vulnerable web app is tricked into fetching credentials from IMDSv1.
- Applications should get AWS credentials from the **instance profile** (IAM role), never from access keys in files.
- Default users vary by AMI: `ec2-user` (Amazon Linux, RHEL), `ubuntu` (Ubuntu), `rocky` (Rocky), `admin` (Debian).
- **SSM Agent** (preinstalled on Amazon Linux and Ubuntu AMIs) enables Session Manager shells, Run Command and Patch Manager without opening port 22.
- Instance store volumes are ephemeral (lost on stop); EBS persists. EBS volumes are NVMe devices on Nitro instances; use `/dev/disk/by-id` or UUIDs.
- The **EC2 serial console** and "detach root volume, attach to rescue instance" are your recovery paths for an unbootable instance.

### 9.3.3 Immutable infrastructure

Mature teams avoid configuring long-lived servers by hand. They bake **golden images** (Packer + Ansible), launch them via Auto Scaling groups or Kubernetes node groups, keep user data minimal (fetch config, join cluster), and *replace* instances rather than patching them. Everything in this course still applies: you use it to build the image, debug the instance when it misbehaves, and write the automation.

## 9.4 Automation Hand-off: Ansible

Once you know how to configure a server by hand, the next step is never doing it by hand again. **Ansible** is the most common Linux configuration management tool: agentless (it uses SSH and Python on the target), declarative YAML, and **idempotent** (running it twice changes nothing the second time).

### 9.4.1 Concepts

| Term | Meaning |
|---|---|
| Inventory | List of hosts and groups (static INI/YAML or dynamic from AWS etc.) |
| Module | A unit of work (`apt`, `dnf`, `copy`, `template`, `systemd`, `user`, `lineinfile`) |
| Task | One module call with arguments |
| Play / Playbook | Tasks mapped to host groups / a YAML file of plays |
| Handler | Task run only when notified by a change (e.g. restart nginx after config change) |
| Role | Reusable bundle of tasks, templates, handlers, defaults |
| Facts | Variables gathered from hosts (`ansible_facts['os_family']`) |

### 9.4.2 Inventory and ad-hoc commands

```ini
# inventory.ini
[web]
web1 ansible_host=10.0.1.11
web2 ansible_host=10.0.1.12

[db]
db1 ansible_host=10.0.3.21

[all:vars]
ansible_user=ubuntu
ansible_ssh_common_args='-o ProxyJump=bastion'
```

```bash
pipx install ansible-core     # or: python3 -m pip install --user ansible
ansible -i inventory.ini all -m ping
ansible -i inventory.ini web -a "uptime"
ansible -i inventory.ini web -b -m apt -a "name=nginx state=present update_cache=true"
ansible -i inventory.ini all -m setup -a "filter=ansible_distribution*"
```

### 9.4.3 A playbook that encodes this course

```yaml
# site.yml
- name: Baseline and web servers
  hosts: web
  become: true
  vars:
    app_user: deploy
    sysctl_settings:
      net.core.somaxconn: 4096
      vm.swappiness: 10

  tasks:
    - name: Install packages
      ansible.builtin.package:
        name: [nginx, chrony, fail2ban, sysstat]
        state: present

    - name: Create deploy user
      ansible.builtin.user:
        name: "{{ app_user }}"
        groups: sudo
        append: true
        shell: /bin/bash

    - name: Authorize deploy key
      ansible.posix.authorized_key:
        user: "{{ app_user }}"
        key: "{{ lookup('file', 'files/deploy.pub') }}"

    - name: Harden sshd
      ansible.builtin.copy:
        dest: /etc/ssh/sshd_config.d/10-hardening.conf
        content: |
          PermitRootLogin no
          PasswordAuthentication no
        mode: "0644"
        validate: /usr/sbin/sshd -t -f %s
      notify: Reload ssh

    - name: Kernel parameters
      ansible.posix.sysctl:
        name: "{{ item.key }}"
        value: "{{ item.value }}"
        sysctl_file: /etc/sysctl.d/90-ansible.conf
        reload: true
      loop: "{{ sysctl_settings | dict2items }}"

    - name: Nginx site from template
      ansible.builtin.template:
        src: templates/site.conf.j2
        dest: /etc/nginx/conf.d/site.conf
        mode: "0644"
        validate: nginx -t -c /etc/nginx/nginx.conf
      notify: Reload nginx

    - name: Services enabled and running
      ansible.builtin.systemd:
        name: "{{ item }}"
        enabled: true
        state: started
      loop: [nginx, chrony, fail2ban]

  handlers:
    - name: Reload ssh
      ansible.builtin.systemd:
        name: ssh
        state: reloaded
    - name: Reload nginx
      ansible.builtin.systemd:
        name: nginx
        state: reloaded
```

```bash
ansible-playbook -i inventory.ini site.yml --check --diff   # dry run with diffs
ansible-playbook -i inventory.ini site.yml --limit web1
ansible-playbook -i inventory.ini site.yml
```

Notes:

- Prefer specific modules over `shell`/`command`; modules are idempotent and report "changed" accurately.
- `validate:` runs a check against the candidate file *before* replacing the real one: a bad sshd config never lands.
- Handlers run once at the end even if notified many times.
- For rolling changes, add `serial: 1` (or `"25%"`) to the play, with a health-check task.
- Secrets go in **Ansible Vault** or are fetched from a secrets manager, never plaintext in Git.
- `sshd -t -f %s` checks the drop-in fragment on its own, which catches syntax errors; after the reload, confirm effective settings with `sshd -T`. The service is named `sshd` on the RHEL family and `ssh` on Ubuntu.

Beyond Ansible: Terraform/OpenTofu provisions the infrastructure (VPCs, instances), Packer builds images, Ansible configures them, and Kubernetes or ECS runs the containers. Linux knowledge sits under all of them.

## 9.5 Lab: Harden, Containerize by Hand, and Automate

Goal: apply hardening controls, build namespace/cgroup isolation by hand, and capture the setup as an Ansible playbook.

1. **Baseline scan.** On a fresh Ubuntu or Rocky VM:

   ```bash
   sudo apt install -y lynis || sudo dnf install -y lynis
   sudo lynis audit system --quick | tee ~/lynis-before.txt | tail -30
   ```

   Note the hardening index.

2. **auditd.** Install (`auditd` on Ubuntu, `audit` on RHEL), add the identity and sudoers rules from 9.1.5, load them, then trigger and find an event:

   ```bash
   sudo augenrules --load
   sudo useradd audittest
   sudo ausearch -k identity -i | tail -20
   sudo userdel audittest
   ```

3. **fail2ban.** Install, create the jail from 9.1.4, then from another machine attempt 6 failed logins (`ssh nosuchuser@VM`) and confirm the ban with `sudo fail2ban-client status sshd`. Unban yourself.

4. **MAC.** On Rocky: install nginx, move a file into `/usr/share/nginx/html` with `mv` from your home directory, observe the 403, find the AVC with `ausearch -m AVC -ts recent`, and fix it with `restorecon`. On Ubuntu: run `sudo aa-status` and identify which processes are confined.

5. **Namespaces by hand.**

   ```bash
   sudo unshare --pid --fork --mount-proc --uts bash -c 'hostname box; hostname; ps aux'
   lsns -t pid
   ```

6. **cgroups by hand.** Run a CPU hog limited to 20% of one CPU and watch throttling:

   ```bash
   sudo systemd-run --unit=hog -p CPUQuota=20% sh -c 'while :; do :; done'
   top -b -n 1 | head -15                                   # ~20% CPU
   cat /sys/fs/cgroup/system.slice/hog.service/cpu.max      # 20000 100000
   grep throttled /sys/fs/cgroup/system.slice/hog.service/cpu.stat
   sudo systemctl stop hog
   ```

7. **Network namespace.** Run the veth example from 9.2.1 and ping between host and namespace. Then run `python3 -m http.server 8000` inside the namespace (`sudo ip netns exec demo python3 -m http.server 8000 &`) and curl it from the host at `10.200.0.2:8000`.

8. **cloud-init.** If you're on a cloud VM, run `cloud-init status --long`, read `/var/log/cloud-init-output.log`, and `sudo cloud-init query userdata`. Launch a second instance with the cloud-config from 9.3.1 and verify each item was applied.

9. **Ansible.** From your workstation, write `inventory.ini` with your VM and run:

   ```bash
   ansible -i inventory.ini all -m ping
   ansible-playbook -i inventory.ini site.yml --check --diff
   ansible-playbook -i inventory.ini site.yml
   ansible-playbook -i inventory.ini site.yml     # second run: changed=0 (idempotent)
   ```

   (Create `files/deploy.pub` and a minimal `templates/site.conf.j2` first; adjust the `sudo` group and `ssh` service name for RHEL.)

10. Re-run Lynis and compare the hardening index with step 1.

## Key takeaways

- Harden with layers: patching, minimal services, key-only SSH, firewall, MAC, auditing, and off-host logs. Use CIS/OpenSCAP/Lynis to measure rather than guess.
- SELinux problems are usually labels (`restorecon`, `semanage fcontext`), booleans (`setsebool -P`) or ports (`semanage port`). Don't disable it; fix the cause.
- auditd with `auid` tells you which human did what, even through sudo.
- Containers are processes with namespaces (visibility) plus cgroups (resource limits), an overlayfs root, dropped capabilities, and seccomp. `unshare`, `nsenter` and `/sys/fs/cgroup` let you see and debug this directly.
- CPU limits cause throttling within each 100 ms period; check `cpu.stat`.
- cloud-init personalises cloud images on first boot; its logs are `/var/log/cloud-init-output.log` and `/var/log/cloud-init.log`. Use IMDSv2 and instance roles on EC2.
- Ansible turns manual steps into idempotent, reviewable code; use modules, handlers, `validate:`, `--check --diff`, and Vault for secrets.
