# Part 6 — Networking and SSH

Most production incidents eventually involve the network: a service that can't reach its database, DNS that resolves to the wrong place, a firewall rule nobody remembers, or an SSH session that hangs. This Part gives you the Linux side of networking with modern tools (`ip`, `ss`, `nmcli`, `resolvectl`, `nft`) and a deep dive into SSH, the tool you'll use more than any other.

## 6.1 Network Fundamentals in Five Minutes

```text
 Layer         What                  Linux tools
 -----------   -------------------   -------------------------------
 7 App         HTTP, DNS, SSH        curl, dig, ssh, openssl s_client
 4 Transport   TCP / UDP ports       ss, nc, iptables/nft
 3 Network     IP, routing           ip addr, ip route, ping, traceroute, mtr
 2 Link        Ethernet, MAC, ARP    ip link, ip neigh, ethtool
```

Quick vocabulary: an **IP address with prefix** (`10.0.1.15/24`) identifies a host and its subnet; the **default gateway** is where packets go when no specific route matches; **DNS** maps names to IPs; a **port** identifies a service on a host; a **socket** is one endpoint (IP:port) of a connection; **TCP** is reliable and connection-oriented (three-way handshake SYN, SYN-ACK, ACK); **UDP** is connectionless (DNS, much monitoring, QUIC).

## 6.2 Interfaces, Addresses and Routes with `ip`

`ifconfig`, `route` and `arp` (net-tools) are deprecated and often not installed. Use `ip` (iproute2).

```bash
ip -br addr                 # brief: interface, state, addresses
ip addr show dev eth0
ip -br link                 # link state, MAC
ip route                    # routing table
ip route get 8.8.8.8        # which route/interface/source IP would be used
ip neigh                    # ARP/neighbor table
ip -s link show eth0        # packet/error counters
```

```text
lo               UNKNOWN        127.0.0.1/8 ::1/128
ens5             UP             10.0.1.15/24 fe80::8ff:fe12:3456/64

default via 10.0.1.1 dev ens5 proto dhcp src 10.0.1.15 metric 100
10.0.1.0/24 dev ens5 proto kernel scope link src 10.0.1.15
```

Temporary changes (lost on reboot):

```bash
sudo ip addr add 10.0.1.50/24 dev ens5
sudo ip link set ens5 up
sudo ip route add 192.168.50.0/24 via 10.0.1.254
sudo ip route del 192.168.50.0/24
sudo ip link set ens5 mtu 9001
```

Interface names like `ens5`, `enp0s3`, `eth0` come from "predictable network interface naming" (based on firmware/PCI location), so they don't change when hardware is added.

### 6.2.1 Persistent configuration

Different distros use different config front ends. Know which one your system uses before editing anything.

| Distro | Tool | Config |
|---|---|---|
| Ubuntu (server/cloud) | **netplan** → systemd-networkd | `/etc/netplan/*.yaml` |
| RHEL/Rocky/Alma/Fedora | **NetworkManager** (`nmcli`) | `/etc/NetworkManager/system-connections/*.nmconnection` |
| Amazon Linux 2023 | systemd-networkd | `/etc/systemd/network/` |
| Debian (classic) | ifupdown | `/etc/network/interfaces` |

netplan example:

```yaml
# /etc/netplan/60-static.yaml
network:
  version: 2
  ethernets:
    ens5:
      dhcp4: false
      addresses: [10.0.1.50/24]
      routes:
        - to: default
          via: 10.0.1.1
      nameservers:
        addresses: [10.0.0.2, 1.1.1.1]
        search: [corp.example.com]
```

```bash
sudo netplan try        # applies with automatic rollback if you lose connectivity
sudo netplan apply
```

NetworkManager with `nmcli`:

```bash
nmcli device status
nmcli connection show
sudo nmcli con mod "System eth0" ipv4.method manual \
     ipv4.addresses 10.0.1.50/24 ipv4.gateway 10.0.1.1 ipv4.dns "10.0.0.2 1.1.1.1"
sudo nmcli con up "System eth0"
nmcli -f IP4 device show eth0
```

> **Warning:** Changing the network config of a remote machine can cut off your own SSH session. Use `netplan try`, schedule a rollback (`sudo systemd-run --on-active=5min /path/restore.sh`), or have console access ready.

## 6.3 Sockets and Ports with `ss`

`ss` replaces `netstat`.

```bash
ss -tulpn             # TCP+UDP, listening, process, numeric
ss -tnp state established
ss -tn dst 10.0.2.20:5432       # connections to the DB
ss -s                 # summary counts
ss -tan | awk 'NR>1 {print $1}' | sort | uniq -c   # connections by state
```

```text
Netid State  Recv-Q Send-Q Local Address:Port  Peer Address:Port Process
tcp   LISTEN 0      511          0.0.0.0:80         0.0.0.0:*     users:(("nginx",pid=812,fd=6))
tcp   LISTEN 0      4096       127.0.0.1:5432       0.0.0.0:*     users:(("postgres",pid=990,fd=7))
tcp   LISTEN 0      128             [::]:22            [::]:*     users:(("sshd",pid=701,fd=4))
```

Read the **Local Address** carefully: `127.0.0.1:5432` means Postgres only accepts local connections, which is why remote clients get "connection refused" even with the firewall open. `0.0.0.0` (or `[::]`/`*`) means all interfaces.

TCP states worth recognising: `LISTEN`, `ESTAB`, `TIME-WAIT` (normal after the side that closed first; lots of them on a busy client is expected), `CLOSE-WAIT` (the remote closed but *our app* hasn't closed its socket: usually an application bug or leak), `SYN-SENT` (we're trying to connect and getting no answer: firewall or host down).

## 6.4 DNS on Linux

Name resolution order is controlled by `/etc/nsswitch.conf` (`hosts: files dns` means check `/etc/hosts` first, then DNS). The resolver configuration is `/etc/resolv.conf`:

```text
nameserver 127.0.0.53
options edns0 trust-ad
search ec2.internal corp.example.com
```

On Ubuntu and other systemd-resolved systems, `/etc/resolv.conf` points to a local stub (`127.0.0.53`) and the real upstream servers are managed by systemd-resolved:

```bash
resolvectl status         # per-link DNS servers and search domains
resolvectl query api.example.com
resolvectl flush-caches
ls -l /etc/resolv.conf    # usually a symlink to /run/systemd/resolve/stub-resolv.conf
```

> **Note:** Don't hand-edit `/etc/resolv.conf` if it's managed (by systemd-resolved, NetworkManager, or DHCP); your change will be overwritten. Configure DNS in netplan, nmcli, or `/etc/systemd/resolved.conf`.

Query tools:

```bash
dig api.example.com              # full answer with TTL
dig +short api.example.com
dig @8.8.8.8 api.example.com     # ask a specific server (bypass local config)
dig -x 10.0.1.15                 # reverse lookup
dig api.example.com AAAA; dig example.com MX; dig example.com TXT
dig +trace example.com           # follow delegation from the root
getent hosts api.example.com     # resolve the way applications do (nsswitch, /etc/hosts)
```

The difference between `dig` and `getent` matters: `dig` talks DNS directly and ignores `/etc/hosts` and nsswitch; `getent` uses the system resolver exactly like your app. If `dig` works but the app fails, check `/etc/hosts`, nsswitch and search domains.

## 6.5 Testing Connectivity

```bash
ping -c 4 10.0.2.20            # ICMP; often blocked in clouds, so failure isn't proof
traceroute -n 10.0.2.20        # or tracepath (no root)
mtr -rwc 50 api.example.com    # continuous traceroute + loss stats
nc -zv 10.0.2.20 5432          # is TCP port open?
timeout 3 bash -c '</dev/tcp/10.0.2.20/5432' && echo open   # no nc installed
curl -v https://api.example.com/health
curl -sS -o /dev/null -w '%{http_code} dns=%{time_namelookup} connect=%{time_connect} tls=%{time_appconnect} total=%{time_total}\n' https://api.example.com
curl --resolve api.example.com:443:10.0.1.99 https://api.example.com/   # force IP, keep SNI/Host
curl -H "Host: app.example.com" http://10.0.1.99/
openssl s_client -connect api.example.com:443 -servername api.example.com </dev/null | openssl x509 -noout -dates -subject
sudo tcpdump -ni ens5 port 5432 -c 20      # watch packets on the wire
sudo tcpdump -ni any host 10.0.2.20 -w cap.pcap   # capture for Wireshark
```

Interpreting connection failures:

| Symptom | Likely meaning |
|---|---|
| `Connection refused` (immediate) | Host reachable, nothing listening on that port/address (or REJECT rule) |
| Timeout / hang | Packets dropped: firewall, security group, NACL, routing, host down |
| `No route to host` | No route, or ICMP-unreachable from a firewall |
| `Name or service not known` | DNS failure |
| TLS errors | Wrong cert, expired cert, SNI mismatch, missing CA |

## 6.6 Firewalls

Linux packet filtering is done in the kernel by **netfilter**. User-space front ends program it.

```text
 ufw (Ubuntu)      firewalld (RHEL)       \
        \               |                  >  front ends
         iptables-nft / nft  --------------/
                  |
         netfilter (kernel) hooks: PREROUTING, INPUT, FORWARD, OUTPUT, POSTROUTING
```

Modern distros use **nftables** under the hood; the `iptables` command on them is usually `iptables-nft`, a compatibility layer. Pick **one** front end per host and don't mix them.

### 6.6.1 ufw (Ubuntu)

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow OpenSSH            # allow SSH BEFORE enabling!
sudo ufw allow from 10.0.0.0/16 to any port 5432 proto tcp
sudo ufw limit 22/tcp             # rate-limit brute force
sudo ufw enable
sudo ufw status numbered
sudo ufw delete 3
```

### 6.6.2 firewalld (RHEL family)

firewalld groups rules into **zones** applied to interfaces or source ranges.

```bash
sudo firewall-cmd --state
sudo firewall-cmd --get-active-zones
sudo firewall-cmd --list-all
sudo firewall-cmd --permanent --add-service=https
sudo firewall-cmd --permanent --add-port=8080/tcp
sudo firewall-cmd --permanent --zone=internal --add-source=10.0.0.0/16
sudo firewall-cmd --reload        # --permanent changes apply after reload
```

### 6.6.3 nftables directly

```bash
sudo nft list ruleset
```

```conf
# /etc/nftables.conf
table inet filter {
  chain input {
    type filter hook input priority 0; policy drop;
    ct state established,related accept
    iif lo accept
    ip protocol icmp accept
    ip6 nexthdr icmpv6 accept
    tcp dport 22 ip saddr 10.0.0.0/16 accept
    tcp dport { 80, 443 } accept
  }
  chain forward { type filter hook forward priority 0; policy drop; }
  chain output  { type filter hook output priority 0; policy accept; }
}
```

```bash
sudo nft -c -f /etc/nftables.conf     # check syntax
sudo systemctl enable --now nftables
```

### 6.6.4 iptables (legacy, still everywhere in docs)

```bash
sudo iptables -L -n -v --line-numbers
sudo iptables -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
sudo iptables -A INPUT -p tcp --dport 22 -s 10.0.0.0/16 -j ACCEPT
sudo iptables -A INPUT -p tcp --dport 443 -j ACCEPT
sudo iptables -P INPUT DROP
sudo iptables -t nat -L -n -v          # NAT table (Docker and kube-proxy live here)
sudo iptables-save > /etc/iptables/rules.v4
```

Rule order matters: the first matching rule wins. The `ESTABLISHED,RELATED` rule near the top allows replies to connections you initiated.

> **Note:** Docker and Kubernetes (kube-proxy in iptables mode, many CNIs) write their own netfilter rules. A host firewall that flushes all rules will break container networking. In clouds, security groups usually do the heavy lifting; host firewalls are defence in depth.

## 6.7 SSH Deep Dive

SSH gives you an encrypted, authenticated channel for shells, file copy, port forwarding and automation (Ansible, Git, CI deploys all ride on it).

### 6.7.1 How it works

1. The client connects to TCP 22; both sides negotiate algorithms and perform a key exchange (e.g. curve25519) to derive session keys.
2. The server proves its identity with its **host key**. The client checks it against `~/.ssh/known_hosts`. A mismatch triggers the scary "REMOTE HOST IDENTIFICATION HAS CHANGED" warning: either the host was rebuilt or someone is intercepting.
3. The user authenticates, ideally with a **public key**: the client proves possession of the private key by signing data; the server checks the signature against `~/.ssh/authorized_keys`. The private key never leaves the client.

### 6.7.2 Keys

```bash
ssh-keygen -t ed25519 -C "abdul@laptop" -f ~/.ssh/id_ed25519   # use a passphrase
ssh-copy-id -i ~/.ssh/id_ed25519.pub user@server
# or manually:
cat ~/.ssh/id_ed25519.pub | ssh user@server 'mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys'
ssh-keygen -lf ~/.ssh/id_ed25519.pub    # fingerprint
ssh-keygen -R server                    # remove stale known_hosts entry
ssh-keyscan -t ed25519 server >> ~/.ssh/known_hosts   # pre-seed (verify fingerprint out of band!)
```

Prefer **ed25519** (small, fast, secure). Use RSA 3072/4096 only for legacy systems. Permissions matter: sshd refuses keys if `~/.ssh` or `authorized_keys` is writable by others (`StrictModes`).

`authorized_keys` options restrict what a key can do:

```text
from="10.0.0.0/16",command="/usr/local/bin/backup-receive",no-port-forwarding,no-pty ssh-ed25519 AAAAC3... backup@ci
```

### 6.7.3 The agent

`ssh-agent` holds decrypted keys in memory so you type the passphrase once.

```bash
eval "$(ssh-agent -s)"
ssh-add ~/.ssh/id_ed25519
ssh-add -l
ssh -A bastion        # agent forwarding: use with care
```

> **Warning:** With agent forwarding, anyone with root on the remote host can use your agent socket to authenticate as you while you're connected. Prefer `ProxyJump` (below), which never exposes your agent to intermediate hosts.

### 6.7.4 Client configuration

`~/.ssh/config` turns long commands into short names and is read by `ssh`, `scp`, `sftp`, `rsync`, `git` and Ansible.

```conf
Host *
    ServerAliveInterval 30
    ServerAliveCountMax 3
    AddKeysToAgent yes
    IdentitiesOnly yes

Host bastion
    HostName bastion.example.com
    User ec2-user
    IdentityFile ~/.ssh/id_ed25519

Host prod-*
    User ubuntu
    ProxyJump bastion
    IdentityFile ~/.ssh/prod_ed25519

Host prod-db1
    HostName 10.0.3.21

Host *.internal
    ControlMaster auto
    ControlPath ~/.ssh/cm-%r@%h:%p
    ControlPersist 10m
```

Now `ssh prod-db1` goes through the bastion automatically. `ControlMaster` multiplexes connections over one TCP session, making repeated SSH (Ansible!) much faster.

### 6.7.5 Bastions and ProxyJump

```text
 laptop --SSH--> bastion (public subnet) --SSH--> app server (private subnet)
          ProxyJump: the second SSH is tunnelled end-to-end through the first;
          the bastion only forwards TCP and never sees your keys or session.
```

```bash
ssh -J ec2-user@bastion.example.com ubuntu@10.0.3.21
scp -J bastion file.tgz ubuntu@10.0.3.21:/tmp/
```

In AWS, **SSM Session Manager** (and EC2 Instance Connect Endpoint) can replace bastions entirely: no inbound port 22, IAM-based access, and session logging.

### 6.7.6 Tunnels (port forwarding)

```bash
# Local forward: reach a private DB through a jump host as localhost:15432
ssh -N -L 15432:prod-db.internal:5432 bastion
psql -h 127.0.0.1 -p 15432 -U app

# Remote forward: expose your local port 3000 on the server's port 8080
ssh -N -R 8080:localhost:3000 server

# Dynamic: SOCKS proxy for browsing internal sites
ssh -N -D 1080 bastion
curl --socks5-hostname localhost:1080 http://internal-dashboard/
```

`-N` means don't run a command (just forward); add `-f` to background it.

### 6.7.7 File transfer

```bash
scp -r ./dist user@server:/opt/app/          # simple copy
rsync -avz --delete ./dist/ user@server:/opt/app/   # incremental, trailing slash matters
rsync -avz -e "ssh -J bastion" ./data/ prod-db1:/backup/
sftp user@server
```

`rsync` only transfers differences and can resume, making it the right tool for deploys and backups. `src/` copies the *contents* of src; `src` copies the directory itself.

### 6.7.8 Server hardening

```conf
# /etc/ssh/sshd_config.d/10-hardening.conf
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
AuthenticationMethods publickey
MaxAuthTries 3
LoginGraceTime 20
AllowGroups ssh-users
X11Forwarding no
AllowTcpForwarding no        # unless needed (bastions need it)
ClientAliveInterval 300
ClientAliveCountMax 2
```

```bash
sudo sshd -t                     # test config BEFORE restarting
sudo systemctl reload ssh        # Debian/Ubuntu unit name 'ssh'; RHEL uses 'sshd'
sudo sshd -T | grep -i passwordauth   # effective settings
```

> **Warning:** Keep your current session open while changing sshd config, and test a *new* login from a second terminal before disconnecting. A typo or an `AllowGroups` you're not in will lock you out.

On Ubuntu 22.10+, sshd may be **socket-activated** (`ssh.socket`); changing `Port` then requires editing the socket unit or disabling socket activation. Extra hardening: fail2ban (Part 9), restricting port 22 to VPN/bastion ranges, short-lived **SSH certificates** signed by a CA (Vault, Teleport, step-ca) instead of distributing static keys.

### 6.7.9 Debugging SSH

```bash
ssh -vvv user@server            # client-side verbose: which keys tried, where it stops
sudo journalctl -u ssh -f       # server side (or -u sshd); /var/log/auth.log, /var/log/secure
```

Common failures: wrong user for the AMI (`ubuntu`, `ec2-user`, `rocky`, `admin`), bad permissions on `~/.ssh`, key not offered (`IdentitiesOnly` / wrong `IdentityFile`), security group blocking 22, host key changed, `AllowGroups` excluding the user, SELinux contexts on a non-standard `authorized_keys` path (`restorecon -Rv ~/.ssh`). Part 10 has a full walkthrough.

## 6.8 Lab: Network Inspection, Firewall and SSH Tunnels

Goal: inspect networking with modern tools, lock down a host firewall, and use SSH config, ProxyJump and tunnels. You need two VMs (`vm1`, `vm2`) on the same network; one can act as the bastion.

1. On `vm1`, inventory the network:

   ```bash
   ip -br addr; ip route; ip route get 1.1.1.1
   resolvectl status 2>/dev/null || cat /etc/resolv.conf
   ss -tulpn
   ```

2. Compare resolution paths: add `10.9.9.9 fake.lab` to `/etc/hosts`, then:

   ```bash
   getent hosts fake.lab     # found
   dig +short fake.lab       # empty: dig ignores /etc/hosts
   ```

3. Start a service listening only on localhost, and observe it from `vm2`:

   ```bash
   # vm1
   python3 -m http.server 8000 --bind 127.0.0.1 &
   ss -tlnp | grep 8000
   # vm2
   curl -m 3 http://VM1_IP:8000/    # refused
   ```

4. Rebind to all interfaces (`--bind 0.0.0.0`) and confirm `vm2` can connect. Then firewall it (Ubuntu):

   ```bash
   sudo ufw allow OpenSSH
   sudo ufw default deny incoming
   sudo ufw enable
   # vm2: curl now times out instead of being refused. Why the difference?
   sudo ufw allow from VM2_IP to any port 8000 proto tcp
   ```

   On Rocky use `firewall-cmd --permanent --add-rich-rule='rule family=ipv4 source address=VM2_IP port port=8000 protocol=tcp accept' && firewall-cmd --reload`.

5. Measure HTTP timing from `vm2`:

   ```bash
   curl -s -o /dev/null -w 'connect=%{time_connect} total=%{time_total}\n' http://VM1_IP:8000/
   ```

6. Set up key-based SSH from your laptop to both VMs, and write `~/.ssh/config` with `vm1` as the bastion and `ProxyJump vm1` for `vm2`. Verify `ssh vm2 hostname` works through the jump.

7. Create a local tunnel to the localhost-only service (restore `--bind 127.0.0.1` first):

   ```bash
   ssh -N -L 18000:127.0.0.1:8000 vm1 &
   curl http://localhost:18000/
   ```

8. Harden sshd on `vm2` with the drop-in from 6.7.8 (keep a session open), run `sudo sshd -t`, reload, and confirm password login now fails: `ssh -o PubkeyAuthentication=no vm2`.

9. Capture the handshake: on `vm1` run `sudo tcpdump -ni any port 8000 -c 10` while `vm2` curls it. Identify SYN, SYN-ACK and ACK in the output.

## Key takeaways

- Use `ip` for addresses/routes/links and `ss` for sockets; `ip route get` and `ss -tulpn` answer "where does traffic go" and "what is listening where".
- A service bound to `127.0.0.1` is unreachable remotely regardless of firewall rules.
- Persistent network config depends on the distro (netplan, NetworkManager, networkd); use `netplan try` or a rollback plan on remote hosts.
- `dig` queries DNS directly; `getent hosts` resolves like applications do. On systemd-resolved hosts, use `resolvectl`.
- "Refused" means reachable but nothing listening; "timeout" means packets are being dropped.
- nftables is the modern netfilter interface; ufw and firewalld are front ends. Allow SSH before enabling a default-deny policy.
- Use ed25519 keys, `~/.ssh/config`, `ProxyJump` rather than agent forwarding, and `rsync` for transfers.
- Harden sshd with drop-ins, `sshd -t`, and a second session open; disable passwords and root login.
