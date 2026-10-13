# Part 3 — Users, Permissions and Processes

Linux is a multi-user system, and almost every security control and every "permission denied" you'll ever debug comes down to three questions: *who* is this process running as, *what* does the file allow, and *which* extra mechanism (ACL, capability, SELinux) applies. This Part also covers processes, the running instances of programs, and how to control them.

## 3.1 Users and Groups

### 3.1.1 Identities are numbers

The kernel doesn't know names, only numeric **UIDs** and **GIDs**. Names are mapped in files:

| File | Contents |
|---|---|
| `/etc/passwd` | `name:x:UID:GID:comment:home:shell` (world-readable, no passwords) |
| `/etc/shadow` | Password hashes and aging (root-only) |
| `/etc/group` | `group:x:GID:member1,member2` |
| `/etc/gshadow` | Group passwords/admins (rarely used) |

```bash
grep alice /etc/passwd
getent passwd alice     # also queries LDAP/SSSD, not just files
id alice
```

```text
alice:x:1001:1001:Alice Ng:/home/alice:/bin/bash
uid=1001(alice) gid=1001(alice) groups=1001(alice),27(sudo),998(docker)
```

UID ranges by convention: **0** is root, **1–999** are system accounts (services), **1000+** are humans. Service accounts typically have shell `/usr/sbin/nologin`.

> **Note:** Because the kernel compares numbers, a file created by UID 1001 in a container appears as owned by whoever is UID 1001 on the host. UID mismatches are a common cause of permission errors on bind mounts and NFS.

### 3.1.2 Managing users and groups

```bash
sudo useradd -m -s /bin/bash -c "Alice Ng" alice   # -m creates home
sudo passwd alice
sudo useradd -r -s /usr/sbin/nologin -d /var/lib/myapp myapp   # system/service user
sudo usermod -aG docker alice        # -a APPEND to supplementary groups (never omit -a!)
sudo usermod -L alice                # lock password
sudo chage -l alice                  # password aging
sudo chage -E 2026-12-31 contractor  # account expiry
sudo userdel -r olduser              # remove with home
sudo groupadd devops
sudo gpasswd -d alice docker         # remove from a group
```

On Debian/Ubuntu, `adduser` is a friendlier interactive wrapper. Group membership changes apply at the next login (or `newgrp docker` in the current shell), because group lists are attached to processes at login time.

> **Warning:** `usermod -G docker alice` without `-a` *replaces* all supplementary groups, potentially removing `sudo` access. Always use `-aG`.

### 3.1.3 root, su and sudo

**root** (UID 0) bypasses normal permission checks. Logging in as root directly is discouraged because there's no audit trail of *who* did what and one mistake is unlimited.

- `su -` switches to root (needs root's password); `su - alice` becomes Alice with a login environment.
- `sudo cmd` runs one command as root (needs *your* password), logged to the journal/`/var/log/auth.log` or `/var/log/secure`.
- `sudo -i` gives a root login shell; `sudo -u postgres psql` runs as another user.

sudo policy lives in `/etc/sudoers` and drop-ins in `/etc/sudoers.d/`. **Always edit with `visudo`**, which syntax-checks before saving; a broken sudoers file can lock everyone out.

```bash
sudo visudo -f /etc/sudoers.d/devops
```

```conf
# Members of devops get full sudo
%devops ALL=(ALL:ALL) ALL

# Deploy user may only restart the app, without a password
deploy ALL=(root) NOPASSWD: /usr/bin/systemctl restart myapp.service, /usr/bin/systemctl status myapp.service

# Keep a variable through sudo
Defaults env_keep += "HTTP_PROXY HTTPS_PROXY NO_PROXY"
```

Syntax: `who host=(runas_user:runas_group) [NOPASSWD:] commands`. Ubuntu grants sudo to the `sudo` group, RHEL-family to `wheel`.

```bash
sudo -l              # what am I allowed to run?
sudo visudo -c       # check all sudoers files
```

> **Interview:** "Why is `NOPASSWD: /usr/bin/vim` dangerous?" Because vim can run shell commands (`:!bash`), giving a full root shell. Same for `less`, `find -exec`, `tar`, `python`, and many others (see GTFOBins). Grant narrowly scoped commands only.

## 3.2 File Permissions

### 3.2.1 Reading `ls -l`

```text
-rwxr-x---  1 alice devops  4096 Oct 12 10:00 deploy.sh
| |  |  |      |     |
| |  |  |      owner group
| |  |  other (o)
| |  group (g)
| user/owner (u)
file type: - file, d dir, l symlink, c char dev, b block dev, s socket, p pipe
```

| Bit | On a file | On a directory |
|---|---|---|
| `r` (4) | Read contents | List names (`ls`) |
| `w` (2) | Modify contents | Create, delete, rename entries inside |
| `x` (1) | Execute | Enter/traverse (`cd`, access files by path) |

Two consequences people get wrong:

- Deleting a file requires `w` on the **directory**, not on the file. A read-only file in a writable directory can be deleted.
- To read `/a/b/c.txt` you need `x` on `/a` and `/a/b`, plus `r` on the file. One missing `x` anywhere in the path gives "Permission denied". Use `namei -l /a/b/c.txt` to see permissions along the whole path.

The kernel checks owner first, then group, then other, and uses the **first category that matches**, not the most permissive. If you own a file with mode `0077`, you can't read it even though "other" can.

### 3.2.2 chmod, chown, chgrp

```bash
chmod 640 app.conf             # rw-r-----
chmod 750 deploy.sh            # rwxr-x---
chmod u+x,g-w,o= script.sh     # symbolic
chmod -R g+rX /srv/shared      # capital X: execute only on dirs (and already-executable files)
sudo chown alice:devops file
sudo chown -R www-data: /var/www/site    # trailing ':' = user's login group
sudo chgrp devops file
```

Common modes: `644` files, `755` dirs and scripts, `600` private keys and secrets, `700` `~/.ssh`, `640` configs readable by a service group.

### 3.2.3 umask

New files get permissions `requested & ~umask`. Programs typically request `666` for files and `777` for directories. With the default umask `022`, files become `644` and directories `755`; with `027`, `640` and `750`.

```bash
umask           # 0022
umask 027       # current shell only
```

Set it per service with `UMask=0027` in a systemd unit, or system-wide in `/etc/login.defs` / shell profiles.

### 3.2.4 Special bits: SUID, SGID, sticky

| Bit | Octal | On a file | On a directory |
|---|---|---|---|
| SUID | 4000 | Runs with the **file owner's** UID | (no effect) |
| SGID | 2000 | Runs with the file's group | New files inherit the directory's **group** |
| Sticky | 1000 | (no effect) | Only the file owner (or root) can delete entries |

```bash
ls -l /usr/bin/passwd   # -rwsr-xr-x root root  (s = SUID; lets users update /etc/shadow)
ls -ld /tmp             # drwxrwxrwt (t = sticky; you can't delete others' files)
sudo chmod 2775 /srv/shared && sudo chgrp devops /srv/shared   # team directory
find / -xdev -perm -4000 -type f 2>/dev/null     # audit SUID binaries
```

SGID directories are the standard way to build shared team folders: combined with umask `002`, everyone's files land in the team group and stay group-writable.

> **Warning:** Every SUID-root binary is a privilege-escalation candidate. Audit the list regularly; an unexpected SUID file is a classic sign of compromise. Shell scripts ignore SUID on Linux for safety.

### 3.2.5 ACLs

Standard permissions allow exactly one owner and one group. **POSIX ACLs** add per-user and per-group entries.

```bash
setfacl -m u:bob:rw report.csv          # give bob rw
setfacl -m g:auditors:r -R /var/log/app # group read, recursive
setfacl -d -m g:devops:rwX /srv/shared  # DEFAULT ACL: inherited by new files
getfacl report.csv
setfacl -x u:bob report.csv             # remove entry
setfacl -b report.csv                   # remove all ACLs
```

```text
# file: report.csv
# owner: alice
# group: alice
user::rw-
user:bob:rw-
group::r--
mask::rw-
other::---
```

A `+` at the end of the mode in `ls -l` (`-rw-rw----+`) indicates an ACL is present. The **mask** caps the effective permissions of all named users and groups; `chmod g=...` on an ACL'd file changes the mask, which surprises people.

### 3.2.6 Capabilities and file attributes

Root's power is split into **capabilities** (`man 7 capabilities`). Instead of running a web server as root just to bind port 80, grant only that capability:

```bash
sudo setcap 'cap_net_bind_service=+ep' /usr/local/bin/myserver
getcap /usr/local/bin/myserver
```

In systemd: `AmbientCapabilities=CAP_NET_BIND_SERVICE`. Containers use capabilities heavily (`docker run --cap-drop ALL --cap-add NET_BIND_SERVICE`).

Extended attributes like **immutable** prevent even root from modifying a file until removed:

```bash
sudo chattr +i /etc/resolv.conf    # cannot be changed or deleted
lsattr /etc/resolv.conf
sudo chattr -i /etc/resolv.conf
```

## 3.3 Processes

A **process** is a running program: an address space, open file descriptors, credentials (UID/GID), a current directory, environment, and one or more threads. Each has a **PID** and a parent (**PPID**).

### 3.3.1 How processes are created

```text
 bash (PID 1200)
   |  fork()  -> child is a copy of bash (PID 1201)
   |  execve("/usr/bin/ls") in child -> PID 1201 now runs ls
   |  wait() -> parent collects child's exit status
```

- When a child exits, it becomes a **zombie** (`Z` state) until its parent calls `wait()`. Zombies hold no memory, just a process table entry. Many zombies mean a buggy parent; killing the zombie does nothing, you must fix or kill the parent.
- If the parent dies first, the child is an **orphan** and is re-parented to PID 1 (or a subreaper), which reaps it. This is why containers need a proper init (`tini`, `docker run --init`) as PID 1.

### 3.3.2 Viewing processes

```bash
ps aux                      # BSD style: all processes, user-oriented
ps -ef                      # System V style, shows PPID
ps -eo pid,ppid,user,%cpu,%mem,stat,etime,cmd --sort=-%cpu | head
ps -fp 1234                 # one process
pstree -p                   # tree with PIDs
pgrep -a nginx              # find by name
top / htop                  # live view (Part 8)
ls -l /proc/1234/           # everything about PID 1234
cat /proc/1234/status       # state, memory, threads, capabilities
ls -l /proc/1234/fd         # open files
tr '\0' '\n' < /proc/1234/environ   # its environment (root/owner only)
```

Process states (the `STAT` column):

| State | Meaning |
|---|---|
| `R` | Running or runnable (on CPU or in run queue) |
| `S` | Interruptible sleep (waiting for event, e.g. network) |
| `D` | Uninterruptible sleep (usually waiting on disk/NFS I/O; can't be killed) |
| `T` | Stopped (Ctrl-Z or SIGSTOP) |
| `Z` | Zombie |
| `I` | Idle kernel thread |

Many processes in `D` state usually means a storage or NFS problem, and they count towards load average (Part 8).

### 3.3.3 Signals

Signals are asynchronous notifications sent to processes (`man 7 signal`).

| Signal | Number | Default | Typical use |
|---|---|---|---|
| `SIGHUP` | 1 | Terminate | Terminal closed; many daemons reload config on it |
| `SIGINT` | 2 | Terminate | Ctrl-C |
| `SIGQUIT` | 3 | Core dump | Ctrl-\ |
| `SIGKILL` | 9 | Terminate | Cannot be caught or ignored |
| `SIGTERM` | 15 | Terminate | Polite "please exit"; default for `kill` |
| `SIGSTOP` | 19 | Stop | Cannot be caught |
| `SIGCONT` | 18 | Continue | Resume stopped process |
| `SIGUSR1/2` | 10/12 | Terminate | App-defined (e.g. reopen logs) |
| `SIGCHLD` | 17 | Ignore | Child changed state |

```bash
kill 1234                 # SIGTERM
kill -HUP $(pidof nginx)  # reload (nginx master)
kill -9 1234              # last resort
pkill -f "python worker.py"
killall -USR1 myapp
```

> **Tip:** Always try `SIGTERM` first and wait. `SIGKILL` gives the process no chance to flush buffers, release locks, or remove temp files. Kubernetes and systemd both send SIGTERM, wait a grace period, then SIGKILL; your apps must handle SIGTERM to shut down gracefully.

### 3.3.4 Job control

```bash
long_task            # running in foreground
Ctrl-Z               # stop it -> [1]+ Stopped
bg                   # continue it in the background
jobs -l              # list jobs in this shell
fg %1                # bring job 1 back
sleep 600 &          # start in background
disown -h %1         # don't send SIGHUP to it when the shell exits
nohup ./backup.sh > backup.log 2>&1 &   # immune to hangup
```

For anything long-running over SSH, use **tmux** (or screen): the session lives on the server and survives disconnects.

```bash
tmux new -s deploy     # start
# Ctrl-b d to detach; later:
tmux attach -t deploy
tmux ls
```

For real services, don't use `nohup` at all: write a systemd unit (Part 4).

### 3.3.5 Priority: nice and ionice

```bash
nice -n 10 ./batch-job.sh      # lower priority (19 lowest, -20 highest)
renice -n 5 -p 1234
ionice -c3 -p 1234             # idle I/O class: only uses disk when idle
ionice -c2 -n7 tar czf ...     # best-effort, lowest
```

Only root can increase priority (negative nice values).

## 3.4 Lab: Shared Team Directory and Process Control

Goal: build a correctly permissioned shared directory, grant narrow sudo, and practise signals.

1. Create users and a group:

   ```bash
   sudo groupadd devops
   for u in alice bob carol; do sudo useradd -m -s /bin/bash "$u"; done
   sudo usermod -aG devops alice
   sudo usermod -aG devops bob
   id alice; id carol
   ```

2. Create a SGID team directory:

   ```bash
   sudo mkdir -p /srv/devops
   sudo chgrp devops /srv/devops
   sudo chmod 2770 /srv/devops
   ls -ld /srv/devops     # drwxrws---
   ```

3. Test it. Alice creates a file; check the group; Carol is denied:

   ```bash
   sudo -u alice bash -c 'umask 007; echo hi > /srv/devops/plan.txt'
   ls -l /srv/devops/plan.txt          # group should be devops
   sudo -u bob cat /srv/devops/plan.txt
   sudo -u carol cat /srv/devops/plan.txt   # Permission denied
   ```

4. Give Carol read access with an ACL, and make it default for new files:

   ```bash
   sudo setfacl -m u:carol:rx /srv/devops
   sudo setfacl -m u:carol:r /srv/devops/plan.txt
   sudo setfacl -d -m u:carol:r /srv/devops
   sudo -u carol cat /srv/devops/plan.txt
   getfacl /srv/devops
   ```

5. Add a sticky bit so members can't delete each other's files, then verify:

   ```bash
   sudo chmod +t /srv/devops
   sudo -u bob rm /srv/devops/plan.txt   # Operation not permitted
   ```

6. Grant Bob permission to run only `systemctl status`:

   ```bash
   echo 'bob ALL=(root) NOPASSWD: /usr/bin/systemctl status *' | sudo tee /etc/sudoers.d/bob
   sudo chmod 440 /etc/sudoers.d/bob
   sudo visudo -c
   sudo -u bob sudo -l
   ```

7. Audit SUID binaries and record the list as a baseline:

   ```bash
   sudo find / -xdev -perm -4000 -type f 2>/dev/null | sort | tee ~/suid-baseline.txt
   ```

8. Signals and job control:

   ```bash
   sleep 1000 &
   jobs -l
   kill -STOP %1; ps -o pid,stat,cmd -p $!   # state T
   kill -CONT %1; ps -o pid,stat,cmd -p $!   # state S
   kill %1
   ```

9. Create a zombie to see one (Python is fine):

   ```bash
   python3 -c 'import os,time
   if os.fork()==0: os._exit(0)
   time.sleep(60)' &
   sleep 1; ps -eo pid,ppid,stat,cmd | grep -w Z
   ```

10. Clean up: `sudo userdel -r carol` (etc.), `sudo rm -rf /srv/devops /etc/sudoers.d/bob`.

## Key takeaways

- The kernel sees only UIDs/GIDs; `getent` and `id` show how names map to them.
- Use `usermod -aG`, `visudo`, and narrowly scoped sudo rules. Some commands (editors, pagers, interpreters) grant a full root shell.
- Directory `w` controls deletion; directory `x` controls traversal; `namei -l` diagnoses path permission problems.
- umask removes bits from new files; SGID directories plus a group give you team folders; sticky protects shared directories.
- ACLs and capabilities offer finer control than owner/group/other and all-or-nothing root.
- Processes are created by fork/exec; zombies need their parent to reap them; orphans get adopted by PID 1.
- Send SIGTERM first, SIGKILL last. Use tmux for interactive long jobs and systemd for services.
