# Part 5 — Storage and Networking

Containers are ephemeral by design: their writable layer disappears when they are removed. Real systems need persistent data and need to talk to each other and to the outside world. This Part covers Docker's three storage mechanisms (volumes, bind mounts, tmpfs) and its networking model in depth, down to the iptables rules that make port publishing work.

## 5.1 Storage

### 5.1.1 The container writable layer

Every container gets a thin writable layer on top of the read-only image layers. Writes there:

- Are lost when the container is removed.
- Use copy-on-write: modifying a 1 GB file from the image first copies the whole file up into the writable layer.
- Are slower than writing to a volume, because they go through the overlay filesystem.

So: write application data to volumes, write logs to stdout, and treat the container filesystem as read-only when you can.

### 5.1.2 The three mount types

```text
                Host
 +-----------------------------------------------------------+
 |  /var/lib/docker/volumes/pgdata/_data   <-- volume (Docker managed)
 |  /home/dev/project/src                  <-- bind mount (any host path)
 |  RAM                                    <-- tmpfs (memory only)
 +-----------------------------------------------------------+
            |                 |                  |
            v                 v                  v
 Container: /var/lib/postgresql/data    /app/src         /tmp
```

| Type | Managed by | Location | Typical use |
|---|---|---|---|
| Volume | Docker | `/var/lib/docker/volumes/<name>/_data` (or a driver backend) | Databases, persistent app state, sharing between containers. |
| Bind mount | You | Any host path | Source code in development, config files, host sockets. |
| tmpfs | Kernel memory | RAM | Scratch space, secrets that must never touch disk, `/tmp` on read-only containers. |

### 5.1.3 `-v` vs `--mount`

Both work; `--mount` is explicit and errors instead of silently creating things.

```bash
# Named volume
docker run -d -v pgdata:/var/lib/postgresql/data postgres:16
docker run -d --mount type=volume,src=pgdata,dst=/var/lib/postgresql/data postgres:16

# Bind mount (read-only)
docker run -d -v "$PWD/nginx.conf:/etc/nginx/nginx.conf:ro" nginx:1.27-alpine
docker run -d --mount type=bind,src="$PWD/nginx.conf",dst=/etc/nginx/nginx.conf,readonly nginx:1.27-alpine

# tmpfs
docker run -d --tmpfs /tmp:rw,size=64m,mode=1777 myapp
docker run -d --mount type=tmpfs,dst=/tmp,tmpfs-size=67108864 myapp
```

> **Warning:** With `-v`, if the host path of a bind mount does not exist, Docker creates it as an empty **directory** owned by root. Mounting a "file" that does not exist yet gives you a directory and a confusing app error. `--mount` fails loudly instead.

### 5.1.4 Volume operations

```bash
docker volume create pgdata
docker volume ls
docker volume inspect pgdata
docker volume rm pgdata
docker volume prune            # removes unused anonymous volumes
docker volume prune -a         # also unused named volumes
```

```json
[
  {
    "CreatedAt": "2026-10-12T09:14:02Z",
    "Driver": "local",
    "Labels": null,
    "Mountpoint": "/var/lib/docker/volumes/pgdata/_data",
    "Name": "pgdata",
    "Options": null,
    "Scope": "local"
  }
]
```

A useful volume behavior: when you mount an **empty** named volume over a directory that contains files in the image, Docker copies the image's files into the volume first. Bind mounts never do this; they always hide the image content.

### 5.1.5 Volume drivers and NFS

The `local` driver can mount NFS or other filesystems through mount options:

```bash
docker volume create --driver local \
  --opt type=nfs --opt o=addr=10.0.0.20,rw,nfsvers=4 \
  --opt device=:/exports/shared \
  shared-nfs
```

Plugins exist for cloud block storage and distributed filesystems, though in clustered environments this job has largely moved to Kubernetes CSI drivers.

### 5.1.6 Permissions and UID mapping

The most common storage problem is "permission denied". Files in a bind mount keep their **host** numeric UID/GID. If the container runs as UID 10001 and the host directory is owned by UID 1000, writes fail.

Fixes:

- Run the container with a matching user: `-u "$(id -u):$(id -g)"` (great for dev tooling containers).
- `chown` the host directory to the container's UID.
- For named volumes, create the directory with correct ownership in the Dockerfile before `VOLUME`/mount; the volume copy-up preserves ownership.
- On SELinux hosts (RHEL, Fedora), add `:z` (shared) or `:Z` (private) to relabel bind mounts: `-v "$PWD/data:/data:Z"`.

### 5.1.7 Backup and restore

Volumes are just directories; back them up by mounting them into a throwaway container:

```bash
# Backup
docker run --rm -v pgdata:/data:ro -v "$PWD:/backup" alpine:3.20 \
  tar czf /backup/pgdata-$(date +%F).tar.gz -C /data .

# Restore into a new volume
docker volume create pgdata-restored
docker run --rm -v pgdata-restored:/data -v "$PWD:/backup" alpine:3.20 \
  tar xzf /backup/pgdata-2026-10-12.tar.gz -C /data
```

> **Warning:** A file-level copy of a running database can be inconsistent. Use the database's own tool (`pg_dump`, `mysqldump`, `mongodump`) or stop the container first.

### 5.1.8 Performance notes for Docker Desktop

On macOS and Windows, bind mounts cross the VM boundary and can be slow for workloads with many small files (like `node_modules`). Docker Desktop uses VirtioFS (fast), and the synchronized file shares feature helps further. A common pattern is to bind mount source code but keep dependencies in a named volume:

```yaml
services:
  web:
    volumes:
      - ./:/app
      - node_modules:/app/node_modules
volumes:
  node_modules:
```

## 5.2 Networking Fundamentals

### 5.2.1 Network drivers

| Driver | Scope | Description |
|---|---|---|
| `bridge` | Single host | Default. Private virtual network with NAT to the outside. |
| `host` | Single host | Container shares the host's network namespace. No isolation, no port mapping, best performance. |
| `none` | Single host | Only a loopback interface. |
| `overlay` | Multi-host (Swarm) | VXLAN-based network spanning cluster nodes. |
| `macvlan` | Single host, L2 | Container gets its own MAC and IP on the physical LAN. |
| `ipvlan` | Single host, L2/L3 | Like macvlan but shares the parent's MAC; works where switches limit MACs. |

```bash
docker network ls
```

```text
NETWORK ID     NAME      DRIVER    SCOPE
3c1e5b0d9f2a   bridge    bridge    local
8a7d6c5b4e3f   host      host      local
1f2e3d4c5b6a   none      null      local
```

### 5.2.2 How the default bridge works

```text
                         Host network namespace
 +-----------------------------------------------------------------+
 |  eth0 (192.168.1.50) <---- NAT (iptables MASQUERADE) ----+      |
 |                                                          |      |
 |                docker0 bridge  172.17.0.1/16 -----------+      |
 |                 /                         \                     |
 |           vethA1                        vethB1                  |
 +-----------|------------------------------|----------------------+
             |  veth pair                   |  veth pair
 +-----------|---------+         +----------|----------+
 |  eth0 172.17.0.2    |         |  eth0 172.17.0.3    |
 |  container A netns  |         |  container B netns  |
 +---------------------+         +---------------------+
```

Each container gets a network namespace with its own `eth0`, which is one end of a **veth pair**; the other end is plugged into the Linux bridge `docker0`. The bridge forwards between containers; outbound traffic is NATed via iptables `MASQUERADE` so it appears to come from the host's IP.

### 5.2.3 User-defined bridges: always use them

The default `bridge` network is legacy. It has **no automatic DNS** between containers (only the deprecated `--link`). User-defined bridges provide:

- Embedded DNS: containers resolve each other by name and network alias.
- Better isolation: only containers on the same network can talk.
- Containers can be connected and disconnected at runtime.

```bash
docker network create --driver bridge --subnet 10.20.0.0/24 backend
docker run -d --name db --network backend -e POSTGRES_PASSWORD=dev postgres:16
docker run --rm --network backend postgres:16 pg_isready -h db
```

```text
db:5432 - accepting connections
```

Multi-network isolation:

```bash
docker network create frontend
docker network create backend --internal      # no outbound internet
docker run -d --name api --network backend myorg/api
docker network connect frontend api           # api is on both networks
docker run -d --name proxy --network frontend -p 443:443 nginx:1.27-alpine
# proxy can reach api; proxy cannot reach db (backend only)
```

### 5.2.4 Embedded DNS

On user-defined networks, containers use Docker's embedded DNS server at `127.0.0.11`. It resolves container names, service names (in Compose), and network aliases, and forwards everything else to the host's resolvers.

```bash
docker run --rm --network backend alpine:3.20 cat /etc/resolv.conf
```

```text
nameserver 127.0.0.11
options ndots:0
```

Aliases give multiple names, and multiple containers with the same alias get round-robin DNS:

```bash
docker run -d --network backend --network-alias search elasticsearch:8.15.0
```

Custom DNS settings: `--dns 10.0.0.2`, `--dns-search corp.local`, `--add-host api.local:10.0.0.9`, or `--add-host host.docker.internal:host-gateway` to reach the host from a container on Linux (Docker Desktop provides `host.docker.internal` automatically).

### 5.2.5 Port publishing

```bash
docker run -d -p 8080:80 nginx              # all host interfaces, TCP
docker run -d -p 127.0.0.1:8080:80 nginx    # localhost only
docker run -d -p 8080:80/udp myudpapp
docker run -d -p 8000-8010:8000-8010 app
docker run -d -p 80 nginx                   # random host port
docker port <container>
```

> **Warning:** `-p 8080:80` binds to `0.0.0.0` (and `::`), exposing the service on every interface. On a cloud VM with a public IP, that is the internet. Bind databases and admin ports to `127.0.0.1`.

### 5.2.6 iptables: what Docker does behind the scenes

Docker programs netfilter rules (iptables, or nftables in newer releases where enabled) to implement NAT and isolation. For `-p 8080:80` on a container at `172.17.0.2`:

```bash
sudo iptables -t nat -L DOCKER -n
```

```text
Chain DOCKER (2 references)
target     prot opt source       destination
RETURN     all  --  0.0.0.0/0    0.0.0.0/0
DNAT       tcp  --  0.0.0.0/0    0.0.0.0/0    tcp dpt:8080 to:172.17.0.2:80
```

```bash
sudo iptables -t nat -L POSTROUTING -n | grep MASQ
```

```text
MASQUERADE  all  --  172.17.0.0/16   0.0.0.0/0
```

Chains to know:

| Chain | Table | Purpose |
|---|---|---|
| `DOCKER` | nat | DNAT for published ports. |
| `POSTROUTING` (MASQUERADE) | nat | Outbound NAT for container subnets. |
| `DOCKER` | filter | Accepts traffic to published container ports. |
| `DOCKER-ISOLATION-STAGE-1/2` | filter | Blocks traffic between different bridge networks. |
| `DOCKER-USER` | filter | **Your** rules; evaluated first, Docker never touches it. |

> **Warning:** Published ports bypass host firewalls like `ufw` and `firewalld` rules in the INPUT chain, because the traffic is DNATed and goes through FORWARD instead. To restrict access to published ports, add rules to `DOCKER-USER`:

```bash
# Allow only the office network to reach published container ports via eth0
sudo iptables -I DOCKER-USER -i eth0 ! -s 203.0.113.0/24 -j DROP
```

The `userland-proxy` (`docker-proxy` processes) handles edge cases like hairpin traffic from localhost; with `"userland-proxy": false` the kernel handles everything via iptables and you avoid one process per published port.

### 5.2.7 host and none

```bash
docker run -d --network host nginx:1.27-alpine   # listens directly on host :80
docker run --rm --network none alpine:3.20 ip addr
```

`host` is useful for performance-sensitive or port-range-heavy workloads (some monitoring agents, VoIP), at the cost of isolation and port conflicts. On Docker Desktop, host networking refers to the VM (support for host networking on Desktop exists but behaves differently from Linux). `none` is for batch jobs that must not touch the network.

### 5.2.8 Container network sharing

```bash
docker run -d --name app myapp
docker run --rm -it --network container:app nicolaka/netshoot
```

The second container joins `app`'s network namespace: same IP, same `localhost`. This is exactly how a Kubernetes Pod works (the "pause" container holds the namespace) and is the standard way to debug networking in a minimal container.

### 5.2.9 macvlan and ipvlan

When a container must appear as a first-class device on the physical LAN (legacy apps, network appliances, DHCP-dependent software):

```bash
docker network create -d macvlan \
  --subnet 192.168.1.0/24 --gateway 192.168.1.1 \
  --ip-range 192.168.1.192/27 \
  -o parent=eth0 lan
docker run -d --network lan --ip 192.168.1.200 --name legacy nginx:1.27-alpine
```

Caveats: the host cannot talk to its own macvlan containers directly without an extra macvlan interface on the host; many cloud networks and Wi-Fi adapters drop frames from unknown MACs (use `ipvlan` there).

### 5.2.10 Overlay networks

Overlay networks connect containers across hosts in a Swarm using VXLAN encapsulation (UDP 4789), with control traffic on TCP 2377 and gossip on TCP/UDP 7946.

```bash
docker network create -d overlay --attachable --opt encrypted appnet
```

`--attachable` allows standalone containers (not only Swarm services) to join; `encrypted` enables IPsec between nodes. Covered further in Part 9.

### 5.2.11 IPv6

Enable per network:

```bash
docker network create --ipv6 --subnet 2001:db8:1::/64 v6net
```

Recent Engine versions publish ports on IPv6 by default and support IPv6-only networks; check `ip6tables` rules if v6 traffic misbehaves.

## 5.3 Networking Troubleshooting Toolkit

```bash
docker network inspect backend --format '{{range .Containers}}{{.Name}} {{.IPv4Address}}{{"\n"}}{{end}}'
docker exec api getent hosts db                 # DNS resolution from inside
docker run --rm --network container:api nicolaka/netshoot \
  sh -c 'dig db; curl -sv http://db:5432; ss -tlnp'
sudo nsenter -t "$(docker inspect -f '{{.State.Pid}}' api)" -n ss -tlnp   # host-side
```

| Symptom | Likely cause |
|---|---|
| `could not resolve host: db` | Containers on the default bridge, or on different networks. |
| Connection refused from host on published port | App listens on `127.0.0.1` inside the container; it must bind `0.0.0.0`. |
| Works locally, not from other machines | Bound to `127.0.0.1:port`, or cloud security group/firewall. |
| No internet from containers | `ip_forward` disabled, broken iptables rules, or DNS unreachable (corporate DNS). |
| Subnet clash, VPN breaks | Docker network overlaps corporate range; set `default-address-pools`. |
| Port already allocated | Another container or host process is using that host port. |

## 5.4 Lab: Volumes, Isolated Networks and Firewalling

**Goal:** build a three-tier topology, verify DNS and isolation, persist data, and restrict a published port.

1. Networks:

```bash
docker network create frontend
docker network create --internal backend
```

2. Database with a named volume on the internal network:

```bash
docker run -d --name db --network backend \
  -e POSTGRES_PASSWORD=labpass \
  -v labdb:/var/lib/postgresql/data postgres:16
```

3. An "API" (here `netshoot` as a stand-in) on both networks, and a proxy on frontend only:

```bash
docker run -d --name api --network backend nicolaka/netshoot sleep infinity
docker network connect frontend api
docker run -d --name proxy --network frontend -p 127.0.0.1:8080:80 nginx:1.27-alpine
```

4. Verify DNS and isolation:

```bash
docker exec api nc -zv db 5432            # succeeds
docker exec proxy getent hosts api        # resolves
docker exec proxy getent hosts db         # fails: different network
docker exec api ping -c1 -W2 8.8.8.8      # succeeds: api also has egress via frontend
docker run --rm --network backend alpine:3.20 ping -c1 -W2 8.8.8.8   # fails: backend is internal-only
```

5. Persistence: create a table, destroy the container, recreate it with the same volume, confirm the table is still there.

```bash
docker exec db psql -U postgres -c 'CREATE TABLE notes(id int);'
docker rm -f db
docker run -d --name db --network backend -e POSTGRES_PASSWORD=labpass \
  -v labdb:/var/lib/postgresql/data postgres:16
sleep 5; docker exec db psql -U postgres -c '\dt'
```

6. Back up the volume with the tar method in 5.1.7.

7. On a Linux host, inspect the NAT rule for port 8080 and add a `DOCKER-USER` rule that drops traffic from a test range. Remove it afterwards with `iptables -D`.

8. Clean up:

```bash
docker rm -f db api proxy
docker network rm frontend backend
docker volume rm labdb
```

## Key takeaways

- Use named volumes for persistent data, bind mounts for development and config, tmpfs for scratch and sensitive in-memory data.
- Prefer `--mount` in scripts: it is explicit and fails instead of silently creating directories.
- Bind mounts keep host UIDs; mismatched UIDs cause most "permission denied" errors.
- Always use user-defined networks for name-based DNS via `127.0.0.11`; use `--internal` networks for backends.
- Port publishing is iptables DNAT; published ports bypass `ufw`, so filter in `DOCKER-USER` and bind to `127.0.0.1` when possible.
- `--network container:<name>` shares a network namespace, the same model as a Kubernetes Pod, and is the go-to debugging technique.
