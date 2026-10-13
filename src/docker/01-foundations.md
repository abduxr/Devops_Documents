# Part 1 — Foundations

This course takes you from "I have run `docker run hello-world` once" to the level expected of a senior DevOps or platform engineer: someone who can build small, secure images, debug a broken container at 3 a.m., wire Docker into CI/CD, and explain exactly what happens in the kernel when a container starts.

Part 1 lays the groundwork. You will learn why containers exist, how they differ from virtual machines, what the moving parts of the Docker platform are (dockerd, containerd, runc, the OCI specifications), and how to install Docker properly on Linux and macOS, including rootless mode.

## 1.1 Why Containers?

### 1.1.1 The problem containers solve

Before containers, shipping software meant shipping *instructions*: "install Python 3.11, then these 40 packages, set these environment variables, make sure libssl is version X". Every environment (laptop, CI runner, staging VM, production fleet) was a slightly different snowflake. The classic symptom was "works on my machine".

Containers change what you ship. Instead of instructions, you ship the **result**: a filesystem snapshot containing the application and all of its user-space dependencies, plus metadata describing how to run it. That artifact is an **image**. A running instance of it is a **container**.

The key benefits for a DevOps engineer:

| Benefit | What it means in practice |
|---|---|
| Reproducibility | The same image digest runs identically in CI, staging and production. |
| Isolation | Processes cannot see each other's files, processes or network by default. |
| Density | Containers share the host kernel, so you can run dozens or hundreds per host. |
| Speed | A container starts in milliseconds; there is no OS boot. |
| Immutable deployments | You replace containers rather than patching them, which makes rollbacks trivial. |
| Standard interface | Every app is started, stopped, logged and monitored the same way. |

### 1.1.2 What a container actually is

A container is **not** a lightweight VM. It is an ordinary Linux process (or group of processes) that the kernel has been told to isolate and constrain using three main features:

- **Namespaces** control what a process can *see*: its own process tree (PID), network stack (NET), mount table (MNT), hostname (UTS), IPC objects, user IDs (USER) and cgroup view (CGROUP).
- **Control groups (cgroups)** control what a process can *use*: CPU, memory, I/O, number of PIDs.
- **A layered root filesystem** (usually overlayfs) gives the process its own `/` assembled from read-only image layers plus a thin writable layer.

Add security filters (capabilities, seccomp, AppArmor/SELinux) and you have a container. We will build one by hand in Part 7.

> **Note:** Because containers share the host kernel, a Linux container needs a Linux kernel. On macOS and Windows, Docker Desktop runs a small Linux VM and your containers live inside it.

## 1.2 Virtual Machines vs Containers

```text
        Virtual Machines                         Containers
+-----------+-----------+-----------+   +-----------+-----------+-----------+
|   App A   |   App B   |   App C   |   |   App A   |   App B   |   App C   |
+-----------+-----------+-----------+   +-----------+-----------+-----------+
| Bins/Libs | Bins/Libs | Bins/Libs |   | Bins/Libs | Bins/Libs | Bins/Libs |
+-----------+-----------+-----------+   +-----------+-----------+-----------+
| Guest OS  | Guest OS  | Guest OS  |   |      Container runtime (Docker)   |
+-----------+-----------+-----------+   +-----------------------------------+
|            Hypervisor             |   |          Host OS kernel           |
+-----------------------------------+   +-----------------------------------+
|          Host hardware            |   |          Host hardware            |
+-----------------------------------+   +-----------------------------------+
```

| Aspect | Virtual machine | Container |
|---|---|---|
| Isolation boundary | Hardware virtualization (hypervisor) | Kernel features (namespaces, cgroups) |
| Kernel | Each VM has its own | Shared with host |
| Boot time | Tens of seconds to minutes | Milliseconds |
| Size | GBs (full OS) | MBs (just app + libs) |
| Density | Tens per host | Hundreds per host |
| Security isolation | Strong | Good, but weaker; a kernel exploit affects all containers |
| Run a different OS kernel | Yes (Windows on Linux host) | No (Linux containers need a Linux kernel) |

The two are complementary, not rivals. In the cloud, your containers almost always run *inside* VMs (EC2 instances, GKE nodes). Sandboxed runtimes such as **gVisor** and **Kata Containers** blur the line further by giving each container a user-space kernel or a micro-VM for stronger isolation.

> **Interview:** "Is a container a VM?" No. A container is a process isolated with namespaces and limited with cgroups, sharing the host kernel. A VM virtualizes hardware and runs its own kernel. Mention the security implication: kernel vulnerabilities cross container boundaries.

## 1.3 Docker Architecture

When you type `docker run nginx`, several components cooperate. Understanding them is essential for troubleshooting and for understanding Kubernetes, which reuses most of the stack.

```text
  docker CLI  ---REST over /var/run/docker.sock--->  dockerd (Docker Engine)
                                                         |
                                                         | gRPC
                                                         v
                                                     containerd
                                                         |
                                            containerd-shim-runc-v2 (one per container)
                                                         |
                                                         v
                                                       runc  --> creates namespaces,
                                                                 cgroups, mounts,
                                                                 then exec()s your process
                                                                 and exits
```

### 1.3.1 The components

- **Docker CLI (`docker`)**: a client. It translates commands into REST API calls. It can talk to a local daemon over a Unix socket or a remote one via `DOCKER_HOST` or contexts.
- **dockerd (Docker Engine)**: the daemon. It owns the high-level concepts: images, builds (via BuildKit), volumes, networks, the API, logging drivers, Swarm.
- **containerd**: a CNCF-graduated container supervisor. It manages the container lifecycle, pulls and stores images, manages snapshots (filesystem layers). Kubernetes can talk to containerd directly through its CRI plugin, without dockerd.
- **containerd-shim**: a tiny process that sits between containerd and each container. It keeps the container's stdio open and reports its exit status, which is why you can restart dockerd or containerd without killing running containers (with `live-restore` enabled).
- **runc**: the reference low-level OCI runtime. It reads an OCI bundle (a root filesystem plus `config.json`), sets up namespaces, cgroups, capabilities and seccomp, then `exec`s the container's entrypoint and exits.
- **BuildKit**: the build engine, default since Docker Engine 23. It parallelizes builds, supports cache mounts, secrets, and multi-platform output.

### 1.3.2 The OCI specifications

The **Open Container Initiative** (Linux Foundation) standardizes containers so that tools are interchangeable:

| Spec | Defines | Why you care |
|---|---|---|
| Image spec | Image manifest, config, layers, index (multi-arch) | Images built by Docker run in Podman, containerd, Kubernetes. |
| Runtime spec | `config.json` + rootfs bundle, lifecycle | runc, crun, gVisor (runsc), Kata are swappable. |
| Distribution spec | HTTP API for pushing and pulling | Any OCI registry works: Docker Hub, ECR, GHCR, Harbor. |

So "Docker image" really means "OCI image". Kubernetes removed its built-in Docker shim (dockershim) in v1.24, but Docker-built images still run fine there, because they are OCI images.

### 1.3.3 The Docker socket is root

The daemon traditionally runs as root, and anyone who can write to `/var/run/docker.sock` can start a privileged container that mounts the host's `/`. **Access to the Docker socket equals root on the host.** Keep that in mind whenever someone suggests adding a user to the `docker` group or mounting the socket into a CI container.

```bash
# Why the docker group is root-equivalent:
docker run --rm -it -v /:/host alpine chroot /host sh
# You now have a root shell on the host filesystem.
```

### 1.3.4 Docker Desktop, Engine, and alternatives

- **Docker Engine**: the open-source daemon on Linux. Free.
- **Docker Desktop**: a GUI app for macOS, Windows and Linux that bundles a Linux VM, Engine, Compose, BuildKit, Scout, Kubernetes and extensions. Commercial use in larger organizations (more than 250 employees or more than USD 10M revenue) requires a paid subscription.
- **Alternatives**: Podman (daemonless, rootless-first, mostly CLI-compatible), Colima and OrbStack (lightweight VMs on macOS that expose a Docker-compatible socket), Rancher Desktop, nerdctl (Docker-like CLI for containerd).

## 1.4 Installing Docker

### 1.4.1 Linux (Ubuntu/Debian) from the official repository

Distribution packages (`docker.io`) are often outdated. Use Docker's own apt repository.

```bash
# Remove conflicting packages
for pkg in docker.io docker-doc docker-compose podman-docker containerd runc; do
  sudo apt-get remove -y "$pkg"
done

# Add Docker's GPG key and repository
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io \
  docker-buildx-plugin docker-compose-plugin
```

The packages:

- `docker-ce`: the Engine (dockerd)
- `docker-ce-cli`: the `docker` client
- `containerd.io`: containerd and runc
- `docker-buildx-plugin`: `docker buildx`
- `docker-compose-plugin`: `docker compose` (v2, written in Go, a CLI plugin)

For RHEL, Fedora, CentOS Stream or Amazon Linux, the procedure is the same idea using `dnf config-manager --add-repo https://download.docker.com/linux/<distro>/docker-ce.repo`. On Amazon Linux 2023, `sudo dnf install docker` gives you a supported Engine from Amazon's repository.

Enable and verify:

```bash
sudo systemctl enable --now docker
sudo docker run --rm hello-world
```

```text
Hello from Docker!
This message shows that your installation appears to be working correctly.
...
```

Optionally allow your user to run `docker` without sudo (remember: root-equivalent):

```bash
sudo usermod -aG docker "$USER"
newgrp docker   # or log out and back in
```

### 1.4.2 macOS

Options:

1. **Docker Desktop**: download from docker.com, choose Apple Silicon or Intel. Easiest, includes everything.
2. **Colima** (free, CLI-only):

```bash
brew install colima docker docker-compose docker-buildx
colima start --cpu 4 --memory 8 --disk 60 --vm-type vz --vz-rosetta
docker context ls
```

3. **OrbStack**: a fast commercial alternative popular with Mac users.

> **Tip:** On Apple Silicon, images default to `linux/arm64`. If you need to test an `amd64`-only image, use `--platform linux/amd64`; Docker Desktop and Colima (with Rosetta) will emulate it.

### 1.4.3 Rootless mode

Rootless mode runs the daemon *and* containers as an unprivileged user using user namespaces. A container breakout lands the attacker as your normal user, not root.

```bash
# Prerequisites (Ubuntu)
sudo apt-get install -y uidmap dbus-user-session slirp4netns
# Ensure subordinate ID ranges exist
grep "^$USER:" /etc/subuid /etc/subgid

# Install as the normal user (not root)
dockerd-rootless-setuptool.sh install

systemctl --user enable --now docker
export DOCKER_HOST=unix:///run/user/$(id -u)/docker.sock
docker info --format '{{.SecurityOptions}}'
```

```text
[name=seccomp,profile=builtin name=rootless name=cgroupns]
```

Limitations to know:

- Binding ports below 1024 needs `sysctl net.ipv4.ip_unprivileged_port_start=0` or `setcap cap_net_bind_service`.
- Networking goes through a user-space stack (slirp4netns, or the faster pasta / RootlessKit port driver), which is slower than the rootful bridge.
- Resource limits require cgroup v2 with systemd delegation.
- Some storage drivers and features (AppArmor, certain overlay setups) behave differently.

Rootless mode is different from **userns-remap**, where a rootful daemon maps container root to an unprivileged host UID range. Both reduce the blast radius of a breakout.

### 1.4.4 Post-install configuration: daemon.json

The daemon reads `/etc/docker/daemon.json` (rootless: `~/.config/docker/daemon.json`). A sensible production baseline:

```json
{
  "log-driver": "local",
  "log-opts": { "max-size": "10m", "max-file": "3" },
  "live-restore": true,
  "default-address-pools": [
    { "base": "10.200.0.0/16", "size": 24 }
  ],
  "features": { "containerd-snapshotter": true },
  "userland-proxy": false,
  "no-new-privileges": true
}
```

Why each matters:

- `log-driver`/`log-opts`: the default `json-file` driver has **no rotation** and will fill your disk. `local` is compact and rotates.
- `live-restore`: containers survive a daemon restart or upgrade.
- `default-address-pools`: avoids clashes between Docker's default `172.17.0.0/16`-style subnets and your corporate network or VPN.
- `containerd-snapshotter`: uses containerd's image store, which enables multi-platform images locally and is the default for fresh installs in recent Docker Engine releases.
- `no-new-privileges`: blocks setuid escalation in all containers by default.

```bash
sudo systemctl restart docker
docker info | grep -iE 'logging driver|live restore|storage driver|cgroup'
```

```text
 Storage Driver: overlayfs
 Logging Driver: local
 Cgroup Driver: systemd
 Cgroup Version: 2
 Live Restore Enabled: true
```

### 1.4.5 Docker contexts

A context bundles an endpoint (local socket, SSH host, remote TLS) so you can switch between daemons:

```bash
docker context create prod-host --docker "host=ssh://deploy@10.0.0.15"
docker context use prod-host
docker ps              # runs against the remote daemon over SSH
docker context use default
```

## 1.5 Your First Containers

```bash
docker run --rm hello-world
docker run -it --rm alpine:3.20 sh
docker run -d --name web -p 8080:80 nginx:1.27-alpine
curl -s localhost:8080 | head -n 4
docker logs web
docker stop web && docker rm web
```

What happened during `docker run -d --name web -p 8080:80 nginx:1.27-alpine`:

1. The CLI POSTed to `/containers/create` on the daemon.
2. The daemon checked the local image store; on a miss it pulled `docker.io/library/nginx:1.27-alpine` (resolving the tag to a digest, choosing the manifest for your CPU architecture, downloading layers).
3. It created a writable layer on top of the image layers, a network endpoint on the default bridge, and a port-forwarding rule for 8080 to 80.
4. containerd started a shim, which invoked runc to create namespaces and cgroups and exec `nginx -g 'daemon off;'`.
5. The CLI printed the container ID and returned (because of `-d`).

## 1.6 Lab: Install, Inspect, and Explore the Engine

**Goal:** install Docker, confirm every component, and prove that a container is just a process.

1. Install Docker using section 1.4 for your OS. Then:

```bash
docker version
docker info
docker buildx version
docker compose version
```

Note the separate Client and Server sections in `docker version`. If the Server section errors, the daemon is not running or you lack socket permissions.

2. Start a long-ish running container:

```bash
docker run -d --name sleeper alpine:3.20 sleep 600
```

3. On a Linux host, find it as a normal process:

```bash
ps -ef | grep "sleep 600" | grep -v grep
pstree -sp "$(pgrep -f 'sleep 600')"
```

```text
root  48211  48190  0 10:02 ?  00:00:00 sleep 600
systemd(1)---containerd-shim(48190)---sleep(48211)
```

The parent is the containerd shim, not dockerd. (On macOS, run these inside the VM, e.g. `colima ssh`.)

4. Compare views from inside and outside:

```bash
docker exec sleeper ps
docker exec sleeper hostname
readlink /proc/$(pgrep -f 'sleep 600')/ns/pid
readlink /proc/self/ns/pid
```

Inside, `sleep` is PID 1; outside it has a large PID. The namespace inode numbers differ.

5. Configure `daemon.json` with log rotation and `live-restore`, restart the daemon, and confirm with `docker info`. Verify `sleeper` is still running after the restart (thanks to live-restore).

6. Clean up:

```bash
docker rm -f sleeper
```

## Key takeaways

- A container is an isolated, constrained Linux process: namespaces for visibility, cgroups for resources, a layered filesystem for its root.
- Containers share the host kernel; VMs do not. That gives speed and density at the cost of a weaker isolation boundary.
- The stack is CLI -> dockerd -> containerd -> shim -> runc. Kubernetes reuses containerd and runc without dockerd.
- OCI specs make images and runtimes interoperable across Docker, Podman, containerd and Kubernetes.
- Access to the Docker socket is equivalent to root on the host. Prefer rootless mode where possible.
- Configure log rotation and address pools in `daemon.json` on day one.
