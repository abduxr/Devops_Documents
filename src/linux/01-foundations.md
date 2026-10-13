# Part 1 — Foundations

This course takes you from "I can open a terminal" to the level expected of a senior DevOps or SRE engineer: someone who can build, automate, secure, tune and debug Linux systems under pressure. Every Part explains *why* things work the way they do, because the commands change between distros and versions, while the underlying model stays the same.

How to use this course:

- Read each Part in order; later Parts assume the earlier ones.
- Do every Lab on a disposable machine: a local VM (Multipass, Vagrant, UTM, VirtualBox), a cloud instance, or a container where noted. Never practise destructive commands on a machine you care about.
- Type the commands rather than pasting them. Muscle memory is the point.

## 1.1 What Linux Is

Strictly speaking, **Linux is a kernel**: the program that runs with full hardware privileges, schedules processes, manages memory, talks to devices through drivers, and exposes all of this to programs through **system calls** (`open`, `read`, `write`, `fork`, `execve`, `mmap`, and a few hundred more). Linus Torvalds released it in 1991, and it is licensed under the GPLv2.

What most people call "Linux" is a **distribution**: the kernel plus a userland, which typically includes:

- An init system and service manager (almost always **systemd** today)
- Core utilities (`ls`, `cp`, `cat`, usually GNU coreutils, or BusyBox on Alpine)
- A C library (glibc on most distros, musl on Alpine)
- A shell (bash, dash, zsh)
- A package manager and repositories
- Default configuration, security policy and a release/support lifecycle

```text
+---------------------------------------------------------+
|  Applications: nginx, postgres, python, your app        |
+---------------------------------------------------------+
|  Userland: shell, coreutils, systemd, libc, package mgr |
+---------------------------------------------------------+
|  System call interface  (open, read, fork, execve ...)  |
+=========================================================+
|  KERNEL: scheduler, memory mgmt, VFS, network stack,    |
|          drivers, namespaces, cgroups, security (LSM)   |
+---------------------------------------------------------+
|  Hardware / hypervisor: CPU, RAM, disk, NIC             |
+---------------------------------------------------------+
```

### 1.1.1 Kernel space vs user space

The CPU has privilege levels. The kernel runs in **kernel space** (ring 0 on x86) and can touch any memory or device. Your programs run in **user space** and must ask the kernel for anything privileged. That boundary is why:

- A buggy program crashes itself, not the machine.
- `strace` can show you everything a program asks the kernel for (Part 8).
- Containers are possible: the kernel can present different "views" of the system to different processes (Part 9).

### 1.1.2 Why DevOps runs on Linux

- The overwhelming majority of cloud workloads, all Kubernetes nodes in practice, and nearly every container image are Linux.
- Everything is scriptable and text-configured, so it can be version-controlled and automated.
- Containers are just Linux kernel features (namespaces + cgroups) with good tooling.
- It is open: when something is weird, you can read the source, the docs, and the kernel's own counters in `/proc` and `/sys`.

### 1.1.3 The "everything is a file" idea

Unix design exposes most resources as files or file-like objects: regular files, directories, devices (`/dev/sda`), kernel state (`/proc/meminfo`), sockets and pipes. One set of tools (`cat`, `echo`, redirection) works across all of them:

```bash
cat /proc/loadavg              # kernel load statistics
cat /sys/class/net/eth0/mtu     # NIC MTU
echo 1 | sudo tee /proc/sys/net/ipv4/ip_forward   # change a kernel setting
```

## 1.2 Distributions You Will Meet

Distributions differ mainly in **package format, release cadence, default security tooling, and support lifecycle**. The kernel and most tools are the same.

| Family | Examples | Package tools | Format | Typical use |
|---|---|---|---|---|
| Debian | Debian, Ubuntu (LTS) | `apt`, `dpkg` | `.deb` | Cloud VMs, dev machines, many container bases |
| Red Hat | RHEL, Rocky Linux, AlmaLinux, CentOS Stream, Fedora | `dnf` (`yum` alias), `rpm` | `.rpm` | Enterprise servers, regulated environments |
| Amazon | Amazon Linux 2023 | `dnf` | `.rpm` | AWS EC2, Fedora-derived |
| Alpine | Alpine Linux | `apk` | `.apk` | Tiny container images (musl + BusyBox) |
| SUSE | SLES, openSUSE | `zypper` | `.rpm` | Enterprise, SAP |

Key facts worth knowing:

- **Ubuntu LTS** ships every two years (22.04, 24.04, 26.04) with 5 years of standard support, more with Ubuntu Pro. Uses AppArmor and `ufw` by default.
- **RHEL** has ~10-year lifecycles. **Rocky** and **Alma** are free, binary-compatible rebuilds. **CentOS Stream** is upstream of RHEL, not a rebuild. RHEL-family uses SELinux (enforcing) and `firewalld` by default.
- **Amazon Linux 2023 (AL2023)** is based on Fedora, uses `dnf`, SELinux in permissive mode by default, and is the default choice on AWS. Amazon Linux 2 (yum-based) reached end of support in 2026.
- **Alpine** images are about 5–8 MB. The catch: **musl libc** behaves differently from glibc (DNS resolution quirks, Python wheels that need recompiling, different `ls`/`ps` flags in BusyBox).

To identify any system:

```bash
cat /etc/os-release
uname -r           # kernel release
hostnamectl        # systemd summary (not on Alpine)
```

```text
PRETTY_NAME="Ubuntu 24.04.1 LTS"
NAME="Ubuntu"
VERSION_ID="24.04"
ID=ubuntu
ID_LIKE=debian
```

> **Tip:** In scripts, source `/etc/os-release` and branch on `$ID` or `$ID_LIKE` rather than parsing `uname` or guessing from file existence.

```bash
. /etc/os-release
case "$ID $ID_LIKE" in
  *debian*) PKG="apt-get" ;;
  *rhel*|*fedora*) PKG="dnf" ;;
  *alpine*) PKG="apk" ;;
esac
```

## 1.3 The Boot Process

Knowing the boot chain lets you fix machines that don't come up, and explains things like "why did my disk mount fail at boot" or "why is the instance stuck in emergency mode".

```text
 Power on
    |
    v
 Firmware (BIOS or UEFI) -- POST, finds boot device
    |
    v
 Bootloader (GRUB 2 / systemd-boot) -- loads kernel + initramfs
    |
    v
 Kernel -- decompresses, inits CPU/memory/drivers, mounts initramfs
    |
    v
 initramfs (/init) -- loads storage drivers, unlocks LUKS, assembles
                      LVM/RAID, finds and mounts the real root fs
    |
    v
 switch_root -> /sbin/init = systemd (PID 1)
    |
    v
 systemd -- reaches default.target (multi-user or graphical)
    |
    v
 Login prompt / sshd / your services
```

### 1.3.1 Firmware: BIOS vs UEFI

- **BIOS (legacy)**: reads the first 512 bytes of the boot disk (the MBR), which contains a tiny first-stage bootloader. MBR partitioning supports max 4 primary partitions and 2 TiB disks.
- **UEFI**: modern firmware that understands filesystems. It reads an **EFI System Partition (ESP)**, a small FAT32 partition usually mounted at `/boot/efi`, and runs a `.efi` binary (e.g. `shimx64.efi` → `grubx64.efi`). Pairs with **GPT** partitioning. Supports **Secure Boot**, where each stage verifies the signature of the next.

```bash
[ -d /sys/firmware/efi ] && echo UEFI || echo BIOS
efibootmgr -v    # list UEFI boot entries (UEFI only)
```

### 1.3.2 GRUB 2

GRUB shows a menu, lets you choose a kernel, and passes the **kernel command line**. Configuration is *generated*: you edit `/etc/default/grub` and regenerate.

```bash
# /etc/default/grub (excerpt)
GRUB_TIMEOUT=5
GRUB_CMDLINE_LINUX="console=ttyS0 crashkernel=auto"

# Regenerate
sudo update-grub                                  # Debian/Ubuntu
sudo grub2-mkconfig -o /boot/grub2/grub.cfg        # RHEL family (BIOS & UEFI on RHEL 9+)
sudo grubby --update-kernel=ALL --args="audit=1"  # RHEL-family convenience tool
```

See what the running kernel was booted with:

```bash
cat /proc/cmdline
```

```text
BOOT_IMAGE=/vmlinuz-6.8.0-45-generic root=UUID=3f1c... ro quiet splash
```

> **Tip:** At the GRUB menu press `e` to edit an entry for one boot only. Adding `systemd.unit=rescue.target` or `rd.break` (RHEL, breaks inside initramfs) is how you recover a lost root password or a broken fstab.

### 1.3.3 Kernel and initramfs

The kernel image lives at `/boot/vmlinuz-<version>`. The kernel cannot mount the root filesystem if the driver for it (NVMe, virtio, LVM, RAID, encryption) is a module stored *on* that root filesystem. The **initramfs** (`/boot/initrd.img-*` or `/boot/initramfs-*.img`) solves this chicken-and-egg problem: it's a small compressed cpio archive the bootloader loads into RAM, containing just enough tools and modules to find and mount the real root.

Rebuild it after changing storage drivers, crypttab, or certain kernel modules:

```bash
sudo update-initramfs -u           # Debian/Ubuntu
sudo dracut -f                     # RHEL family, Fedora, AL2023
lsinitramfs /boot/initrd.img-$(uname -r) | head   # inspect (Debian)
lsinitrd | head                                   # inspect (dracut)
```

### 1.3.4 systemd as PID 1

Once root is mounted, the kernel executes `/sbin/init`, which is a symlink to systemd. PID 1 is special: it adopts orphaned processes, and if it dies the kernel panics. systemd reads unit files, builds a dependency graph, and starts everything in parallel towards a **target**.

| Target | Old runlevel | Meaning |
|---|---|---|
| `poweroff.target` | 0 | Shut down |
| `rescue.target` | 1 | Single-user, root fs mounted, minimal services |
| `multi-user.target` | 3 | Full system, no GUI (servers) |
| `graphical.target` | 5 | multi-user + display manager |
| `reboot.target` | 6 | Reboot |
| `emergency.target` | - | Bare minimum shell, root mounted read-only |

```bash
systemctl get-default
sudo systemctl set-default multi-user.target
systemd-analyze                    # total boot time
systemd-analyze blame | head       # slowest units
systemd-analyze critical-chain     # what the boot actually waited for
```

```text
Startup finished in 2.112s (kernel) + 6.904s (userspace) = 9.016s
multi-user.target reached after 6.851s in userspace.
```

> **Interview:** "Walk me through what happens when a Linux server boots." Answer with the chain: firmware (UEFI/BIOS) → bootloader (GRUB) loads kernel and initramfs and passes the command line → kernel initializes hardware and runs initramfs → initramfs mounts the real root → `switch_root` and exec systemd as PID 1 → systemd activates units up to `default.target`. Bonus points for mentioning where you'd intervene when each stage fails.

## 1.4 The Filesystem Hierarchy

Linux has a single tree rooted at `/`. Other disks are **mounted** onto directories in that tree; there are no drive letters. The layout follows the **Filesystem Hierarchy Standard (FHS)**.

| Path | Purpose | DevOps notes |
|---|---|---|
| `/` | Root of the tree | |
| `/bin`, `/sbin`, `/lib` | Essential binaries/libs | On modern distros these are symlinks to `/usr/...` ("usrmerge") |
| `/usr` | Installed software, read-only-ish | `/usr/bin`, `/usr/lib`, `/usr/share` |
| `/usr/local` | Locally built software | Where `make install` goes by default |
| `/opt` | Self-contained third-party apps | Vendor software, `/opt/app/` |
| `/etc` | System configuration | Text files; version this with Ansible/Git |
| `/var` | Variable data | `/var/log`, `/var/lib` (databases, docker), `/var/cache` |
| `/var/log` | Logs | First place to look when something breaks |
| `/var/lib` | Persistent state of services | `/var/lib/docker`, `/var/lib/postgresql` |
| `/tmp` | Temporary files | Often tmpfs (RAM); cleared at reboot |
| `/home` | User homes | `/root` is root's home |
| `/boot` | Kernels, initramfs, GRUB | Running out of space here breaks kernel updates |
| `/dev` | Device nodes | Managed by udev (`/dev/sda`, `/dev/nvme0n1`, `/dev/null`) |
| `/proc` | Virtual: processes and kernel state | `/proc/<pid>/`, `/proc/meminfo` |
| `/sys` | Virtual: devices, drivers, kernel objects | cgroups live under `/sys/fs/cgroup` |
| `/run` | Runtime state since boot (tmpfs) | PID files, sockets |
| `/mnt`, `/media` | Manual / removable mounts | |
| `/srv` | Data served by this system | Sometimes used for web roots |

> **Note:** `/proc` and `/sys` consume no disk space. They are windows into kernel data structures, generated on read. That's why `cat /proc/meminfo` is always current.

Understanding this layout tells you where to look: config in `/etc`, logs in `/var/log` or the journal, data in `/var/lib`, binaries in `/usr/bin`, runtime sockets in `/run`.

```bash
man hier          # the hierarchy documented on your system
findmnt           # what is mounted where, as a tree
```

## 1.5 Getting Help

Professional Linux users read docs constantly. Learn to do it fast.

```bash
man ls              # manual page; '/' to search, 'n' next, 'q' quit
man 5 crontab       # section 5 = file formats (vs crontab(1) the command)
man -k "disk usage" # search man page descriptions (apropos)
ls --help           # short usage
help cd             # bash builtins
type cd ls ll       # is it a builtin, alias, function or file?
tldr tar            # community examples (install 'tldr')
```

Man page sections you'll use: **1** user commands, **5** file formats, **7** overviews (`man 7 signal`, `man 7 capabilities`), **8** admin commands.

## 1.6 Lab: Explore a Fresh Linux System

Goal: get oriented on a new machine the way you would on day one of an incident.

1. Start a VM. With Multipass: `multipass launch 24.04 --name lab1 && multipass shell lab1`. Or launch an Ubuntu/Rocky cloud instance and SSH in.
2. Identify the system:

   ```bash
   cat /etc/os-release
   uname -a
   hostnamectl
   ```

3. Determine the firmware type and boot details:

   ```bash
   [ -d /sys/firmware/efi ] && echo UEFI || echo BIOS
   cat /proc/cmdline
   ls -lh /boot
   ```

4. Measure the boot:

   ```bash
   systemd-analyze
   systemd-analyze blame | head -5
   systemctl get-default
   ```

5. Walk the hierarchy. For each, write down one thing you find:

   ```bash
   ls /etc | head -30
   ls /var/log
   ls -l /bin              # is it a symlink to usr/bin?
   findmnt -t ext4,xfs,vfat
   ```

6. Read kernel state directly:

   ```bash
   cat /proc/cpuinfo | grep "model name" | head -1
   grep -E 'MemTotal|MemAvailable' /proc/meminfo
   cat /proc/uptime /proc/loadavg
   ```

7. Look up two man pages: `man 5 os-release` and `man 7 hier`. Find the section that explains `ID_LIKE`.

8. Stretch: reboot the VM, interrupt GRUB (hold Shift on BIOS, Esc on UEFI, or use the cloud serial console), press `e`, and look at the kernel line without changing it. Press Esc to boot normally.

## Key takeaways

- Linux is the kernel; a distribution is the kernel plus userland, packaging and policy.
- Debian/Ubuntu (apt), RHEL/Rocky/Alma/Amazon Linux (dnf), and Alpine (apk, musl) are the families you'll meet. Use `/etc/os-release` to detect them.
- Boot chain: firmware → GRUB → kernel + initramfs → systemd (PID 1) → `default.target`. Each stage has its own failure mode and recovery hook.
- The initramfs exists so the kernel can mount a root filesystem whose drivers it doesn't have built in.
- `/etc` is config, `/var` is changing data, `/usr` is software, `/proc` and `/sys` are live kernel views.
- `man`, `--help`, `type`, and `man -k` are the fastest way to answer your own questions.
