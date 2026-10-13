# Part 9 — Troubleshooting Toolkit, Methodology, and Network Security

Knowing protocols is half the job. The other half is finding out, under pressure, which hop is dropping, delaying, or mangling traffic. This Part covers the essential tools with the output you should expect, a systematic methodology, the MTU problems that defeat most engineers, and the security architecture (zero trust, DDoS protection, WAFs) that production networks need.

## 9.1 The Toolkit

| Tool | Layer | Answers |
|------|-------|---------|
| `ip`, `ethtool` | L1–L3 | Is the interface up? What is my IP, route, neighbor? Errors on the NIC? |
| `ping` | L3 | Is the host reachable? What is the RTT and loss? |
| `traceroute`, `tracepath`, `mtr` | L3 | Which hop is losing or delaying packets? What is the path MTU? |
| `dig`, `getent` | L7 (DNS) | What does the name resolve to, and from whose perspective? |
| `nc`, `curl`, `openssl s_client` | L4–L7 | Is the port open? Does TLS work? What does the app return? |
| `ss`, `nstat`, `conntrack` | L4 | What sockets exist? Retransmissions? NAT state? |
| `tcpdump`, Wireshark, `tshark` | All | What is actually on the wire? |
| `nmap` | L3–L4 | Which ports are open/filtered across a host or range? |
| `iperf3` | L4 | What throughput can this path sustain? |
| `tc` (netem) | L2–L3 | Simulate latency, loss, and jitter for testing |

> **Tip:** Keep a debug image handy. `nicolaka/netshoot` contains nearly every tool in this Part and can be attached to any container's or pod's network namespace: `docker run -it --net container:<name> nicolaka/netshoot` or `kubectl debug -it <pod> --image=nicolaka/netshoot --target=<container>`.

### 9.1.1 ping

```bash
ping -c 5 10.0.64.10
```

```text
PING 10.0.64.10 (10.0.64.10) 56(84) bytes of data.
64 bytes from 10.0.64.10: icmp_seq=1 ttl=64 time=0.512 ms
64 bytes from 10.0.64.10: icmp_seq=2 ttl=64 time=0.431 ms
...
--- 10.0.64.10 ping statistics ---
5 packets transmitted, 5 received, 0% packet loss, time 4005ms
rtt min/avg/max/mdev = 0.431/0.470/0.512/0.029 ms
```

Read: loss percentage, average RTT, and `mdev` (jitter). TTL hints at the OS and hop count: Linux starts at 64, Windows at 128, many network devices at 255.

> **Warning:** A failed ping does **not** prove a host is down. Security groups, Azure NSGs, and many firewalls block ICMP by default. A successful ping also does not prove your service works. Always follow with a TCP test on the real port.

### 9.1.2 traceroute and mtr

`traceroute` sends probes with increasing TTL; each router that decrements TTL to zero returns an ICMP Time Exceeded, revealing itself.

```bash
traceroute -n -T -p 443 api.example.com    # TCP SYN probes to 443 (gets through firewalls that block UDP/ICMP)
tracepath -n api.example.com                # also discovers path MTU, no root needed
```

`mtr` combines ping and traceroute continuously and is the best tool for intermittent loss:

```bash
mtr -n -r -c 100 -T -P 443 api.example.com
```

```text
HOST: bastion                    Loss%   Snt   Last   Avg  Best  Wrst StDev
  1.|-- 10.0.48.1                 0.0%   100    0.3   0.3   0.2   0.9   0.1
  2.|-- 100.65.12.33              0.0%   100    0.8   1.1   0.6   4.2   0.5
  3.|-- 52.93.28.144             38.0%   100    1.2   1.4   1.0   3.1   0.4
  4.|-- 99.83.89.10               0.0%   100    2.1   2.3   1.9   5.0   0.4
  5.|-- 203.0.113.10              0.0%   100   11.8  12.0  11.5  14.9   0.6
```

> **Note:** Loss at an intermediate hop (hop 3 above) that does **not** continue to later hops is not real loss. Routers rate-limit the ICMP replies they generate for probes because it is a low-priority control-plane task. Only loss that persists through to the final hop matters. Likewise, latency that jumps at one hop and stays high afterward is real (often a long-distance link); a spike at one hop that disappears at the next is not.

Lines of `* * *` mean that hop does not reply to probes; it is common in cloud networks and not a failure by itself.

### 9.1.3 nc (netcat): the fastest port test

```bash
nc -zv -w 3 10.0.64.10 5432
```

```text
Connection to 10.0.64.10 5432 port [tcp/postgresql] succeeded!
nc: connect to 10.0.64.10 port 5433 (tcp) failed: Connection refused
nc: connect to 10.0.64.11 port 5432 (tcp) timed out: Operation now in progress
```

Succeeded, refused, and timed out are three different diagnoses (Part 3). If `nc` is unavailable, Bash can test a port itself: `timeout 3 bash -c '</dev/tcp/10.0.64.10/5432' && echo open`.

### 9.1.4 ss

```bash
ss -tlnp                                   # listeners and owning processes
ss -tn state established '( dport = :443 )' | wc -l
ss -tin dst 10.0.2.40                      # per-connection RTT, cwnd, retransmits
ss -s                                      # summary
```

```text
Total: 1630
TCP:   21450 (estab 1205, closed 19980, orphaned 3, timewait 19960)
```

### 9.1.5 tcpdump

`tcpdump` is the ground truth. When the application, the cloud console, and your teammate disagree, the packet capture settles it.

```bash
sudo tcpdump -ni any host 10.0.64.10 and port 5432          # filter by host and port
sudo tcpdump -ni eth0 'tcp[tcpflags] & (tcp-syn|tcp-rst) != 0' # only SYNs and RSTs
sudo tcpdump -ni eth0 -w /tmp/cap.pcap -s 0 port 443 -c 5000 # write to file for Wireshark
sudo tcpdump -ni eth0 -A 'tcp port 80 and (tcp[((tcp[12:1] & 0xf0) >> 2):4] = 0x47455420)'  # HTTP GETs
sudo tcpdump -ni eth0 icmp                                   # unreachable, fragmentation needed, TTL exceeded
```

Reading a failed connection where the server never answers:

```text
10:41:02.100 IP 10.0.1.25.51544 > 10.0.64.10.5432: Flags [S], seq 1048..., win 62727, length 0
10:41:03.121 IP 10.0.1.25.51544 > 10.0.64.10.5432: Flags [S], seq 1048..., win 62727, length 0
10:41:05.169 IP 10.0.1.25.51544 > 10.0.64.10.5432: Flags [S], seq 1048..., win 62727, length 0
```

Retransmitted SYNs at 1 s, then 2 s intervals with no SYN-ACK: something is dropping. Now capture on the **server** side. If SYNs arrive there and SYN-ACKs leave, the return path is the problem (NACL, asymmetric route). If SYNs never arrive, look at the path between (security group, NACL, routing).

> **Tip:** Capture at both ends simultaneously. The question "did the packet leave A?" and "did it arrive at B?" splits the problem in half every time. In Kubernetes, capture inside the pod's namespace (`kubectl debug` or `nsenter -t <pid> -n tcpdump ...`) and on the node's veth.

### 9.1.6 Wireshark

Open the `.pcap` from tcpdump in Wireshark for deep analysis. Useful display filters:

| Filter | Shows |
|--------|-------|
| `tcp.analysis.retransmission` | Retransmitted segments |
| `tcp.analysis.zero_window` | Receiver buffer full (slow consumer) |
| `tcp.flags.reset == 1` | Resets |
| `tcp.time_delta > 1` | Gaps over 1 s within a stream |
| `http.response.code >= 500` | Server errors |
| `tls.handshake.type == 1` | ClientHellos (see SNI in `tls.handshake.extensions_server_name`) |
| `dns.flags.rcode != 0` | DNS errors |
| `icmp.type == 3 && icmp.code == 4` | Fragmentation needed (MTU problems) |

Statistics > Conversations and Statistics > I/O Graph are the fastest way to see who talks to whom and when throughput dropped. To decrypt TLS in a test environment, have the client write session keys (`SSLKEYLOGFILE=/tmp/keys.log curl ...`) and point Wireshark at that file.

### 9.1.7 nmap

```bash
nmap -Pn -p 22,80,443,5432 10.0.64.0/24     # specific ports on a subnet, skip ping discovery
nmap -sV -p 443 api.example.com              # detect service versions
nmap --script ssl-enum-ciphers -p 443 api.example.com   # list TLS versions and ciphers
```

```text
PORT     STATE    SERVICE
22/tcp   filtered ssh
80/tcp   closed   http
443/tcp  open     https
5432/tcp filtered postgresql
```

`open` = SYN-ACK received; `closed` = RST received (reachable, nothing listening); `filtered` = no response or ICMP prohibited (firewall). Only scan systems you are authorized to scan.

### 9.1.8 iperf3

```bash
iperf3 -s                                   # on server (TCP 5201)
iperf3 -c 10.0.64.10 -t 10                  # single stream client test
iperf3 -c 10.0.64.10 -P 8 -t 10             # 8 parallel streams
iperf3 -c 10.0.64.10 -u -b 500M             # UDP at 500 Mbit/s: shows loss and jitter
iperf3 -c 10.0.64.10 -R                     # reverse direction
```

```text
[ ID] Interval           Transfer     Bitrate         Retr
[  5]   0.00-10.00  sec  5.80 GBytes  4.98 Gbits/sec   12             sender
[  5]   0.00-10.00  sec  5.80 GBytes  4.98 Gbits/sec                  receiver
```

If one stream is slow but eight parallel streams fill the link, the bottleneck is per-connection (window/RTT, Part 3), not the link. Note that AWS limits a single flow to about 5 Gbit/s outside a cluster placement group, which matches the output above.

### 9.1.9 Simulating bad networks with tc netem

Test how your application behaves under latency and loss before production does it for you:

```bash
sudo tc qdisc add dev eth0 root netem delay 100ms 20ms loss 1%
sudo tc qdisc show dev eth0
sudo tc qdisc del dev eth0 root
```

## 9.2 A Systematic Troubleshooting Methodology

Random command execution wastes time during incidents. Use a repeatable process.

### 9.2.1 The steps

```text
1. DEFINE      Exact symptom, error message, scope. Who/what is affected? Since when?
               What changed? (deploys, config, certs, DNS, infra, provider status)
2. LOCATE      Draw the path: client -> DNS -> LB -> proxy -> service -> DB.
               List every hop, NAT, firewall, and policy on it.
3. ISOLATE     Test layer by layer and hop by hop. Halve the path each time.
4. HYPOTHESIZE Form a specific, testable explanation from the evidence.
5. TEST        Change one thing, or gather one decisive piece of evidence.
6. FIX         Apply the smallest safe fix; verify the original symptom is gone.
7. PREVENT     Monitoring, alerts, runbooks, postmortem actions.
```

### 9.2.2 The layer-by-layer checklist

```text
Layer / question                       Command
-------------------------------------  -------------------------------------------------
Interface up, address correct?         ip -br addr; ip -br link
NIC errors / drops?                    ip -s link show eth0; ethtool -S eth0 | grep -i err
Route to destination?                  ip route get <dst>
Gateway / neighbor resolved?           ip neigh
Name resolves (as the app sees it)?    getent ahosts <name>; dig <name>
IP reachable?                          ping -c3 <dst>  (if ICMP allowed); mtr -T -P <port>
Port reachable?                        nc -zv <dst> <port>   (refused vs timeout!)
Firewall / SG / NACL / NetworkPolicy?  flow logs, iptables -L -v -n counters, policy review
TLS valid?                             openssl s_client -connect <h>:443 -servername <h>
Application responding correctly?      curl -v; curl -w timings; app and proxy logs
Return path OK?                        tcpdump at both ends
Performance?                           ss -ti; nstat; iperf3; mtr over time
```

### 9.2.3 Principles

- **Reproduce from where the problem is.** Testing from your laptop proves only that your laptop works. Test from the failing pod, node, or subnet.
- **Read the exact error.** "Connection refused", "Connection timed out", "No route to host", "Name or service not known", "certificate verify failed", and "502 Bad Gateway" each point to different layers.
- **Compare working and non-working.** One host works, another does not: diff their resolv.conf, routes, security groups, versions.
- **Suspect recent change first.** Most incidents follow a change.
- **Trust packets over dashboards.** Metrics and consoles summarize; captures show truth.

## 9.3 MTU Problems

### 9.3.1 Path MTU Discovery and the black hole

TCP sets the **DF** (Don't Fragment) bit. If a packet is larger than a link's MTU, the router drops it and sends **ICMP Type 3 Code 4 "Fragmentation Needed"** (IPv6: "Packet Too Big") with the next-hop MTU. The sender reduces its packet size. That is **PMTUD**.

If a firewall blocks those ICMP messages, the sender never learns, keeps resending large packets, and they are silently dropped: a **PMTUD black hole**.

```text
Client (MTU 1500) ---> [Router] ---> [IPsec tunnel MTU 1400] ---> Server
  1500-byte packet, DF=1 -> dropped at tunnel ingress
  ICMP "frag needed, MTU 1400" -> BLOCKED by a firewall
  Client retransmits 1500-byte packet ... forever

Symptoms:
  - TCP handshake works (small packets)
  - Small requests work: ping, health checks, short API responses
  - Large responses, TLS handshakes with big certificate chains, file transfers,
    git clone, docker pull HANG
```

### 9.3.2 Finding the path MTU

```bash
# Linux ping: -M do sets DF; -s is payload size (add 28 for IP+ICMP headers)
ping -M do -s 1472 -c 2 10.50.0.10     # 1472 + 28 = 1500
ping -M do -s 1372 -c 2 10.50.0.10     # 1400
tracepath -n 10.50.0.10
```

```text
PING 10.50.0.10 (10.50.0.10) 1472(1500) bytes of data.
ping: local error: message too long, mtu=1500
--- or, if an upstream link is smaller and ICMP is blocked: ---
2 packets transmitted, 0 received, 100% packet loss

PING 10.50.0.10 (10.50.0.10) 1372(1400) bytes of data.
1380 bytes from 10.50.0.10: icmp_seq=1 ttl=62 time=8.21 ms

 1?: [LOCALHOST]                      pmtu 1500
 1:  10.0.48.1                         0.4ms
 2:  169.254.12.1                      1.1ms pmtu 1400
```

Binary-search the size between the largest success and smallest failure.

### 9.3.3 Fixes

1. **Allow ICMP Type 3 Code 4** (and ICMPv6 Packet Too Big) through firewalls and security groups. This is the correct fix.
2. **MSS clamping** on the tunnel or router, so TCP negotiates smaller segments in the handshake:

   ```bash
   iptables -t mangle -A FORWARD -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu
   # or a fixed value for IPsec
   iptables -t mangle -A FORWARD -o vti0 -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --set-mss 1360
   ```

3. **Lower the interface MTU** on hosts, overlays, Docker (`"mtu": 1400` in daemon.json), or the CNI configuration, so packets fit the smallest link.
4. Enable `net.ipv4.tcp_mtu_probing=1`, which lets Linux detect black holes and probe down.

> **Interview:** "SSH works but `scp` of a large file hangs and `git clone` stalls. Ping works." A strong answer identifies a PMTUD black hole, describes testing with `ping -M do -s`, and lists MSS clamping, allowing ICMP fragmentation-needed, and MTU adjustment as fixes.

## 9.4 Network Security

### 9.4.1 Defense in depth

```text
Internet
   |  DDoS protection (provider edge, anycast scrubbing)
   |  CDN + WAF (L7 filtering, bot management, rate limiting)
   v
Load balancer (TLS termination, only 443 exposed)
   |  Security groups: only LB -> app port
   v
Application tier (private subnets, no public IPs)
   |  NetworkPolicy / mesh AuthorizationPolicy (identity-based, mTLS)
   v
Data tier (isolated subnets, no Internet route, SG from app only, encryption in transit)
   |
Egress control (NAT + egress firewall / proxy allowlist, DNS filtering)
Monitoring everywhere (flow logs, DNS logs, WAF logs, IDS)
```

Each layer assumes the one before it might fail.

### 9.4.2 Zero trust

Traditional "castle and moat" security trusts anything inside the network perimeter. One compromised laptop or pod then moves laterally everywhere. **Zero trust** removes implicit trust based on network location:

- **Verify explicitly**: every request is authenticated and authorized based on identity (user, device, workload), not source IP.
- **Least privilege**: grant minimal access, per service, just in time where possible.
- **Assume breach**: segment aggressively, encrypt all traffic including internal, log and inspect.

In practice for DevOps:

| Principle | Implementation |
|-----------|----------------|
| Workload identity | SPIFFE/SPIRE, service mesh mTLS certificates, cloud IAM roles for pods (IRSA, EKS Pod Identity) |
| User access without VPN | Identity-aware proxies (Google IAP, Cloudflare Access, AWS Verified Access, Teleport), SSO + device posture |
| Micro-segmentation | Security groups per service, Kubernetes NetworkPolicies, mesh authorization policies |
| No standing SSH | AWS SSM Session Manager, short-lived certificates, audited sessions |
| Encrypt everywhere | TLS/mTLS internally, not only at the edge |

### 9.4.3 DDoS basics

| Attack class | Example | Mitigation |
|-------------|---------|------------|
| Volumetric (L3/L4) | UDP floods, DNS/NTP/memcached **amplification** (small spoofed request, huge reply to victim) | Provider-scale absorption: anycast networks, AWS Shield, Cloudflare; BCP 38 ingress filtering stops spoofing at the source |
| Protocol / state exhaustion | **SYN flood** fills the SYN queue; connection floods exhaust conntrack or LB state | SYN cookies (`net.ipv4.tcp_syncookies=1`), stateless edge, larger backlogs, provider protection |
| Application (L7) | HTTP floods hitting expensive endpoints, slowloris holding connections open | WAF rate limiting, bot detection, caching, request timeouts (`client_header_timeout`), autoscaling, challenge pages |

Design guidance: put everything behind a CDN or a provider edge that can absorb volume, keep origins unreachable directly, cache aggressively, rate limit per client, and make sure autoscaling has upper bounds (an L7 attack can otherwise become a billing attack).

### 9.4.4 Web Application Firewalls

A WAF inspects HTTP requests at the edge or load balancer:

- **Managed rule sets**: OWASP Core Rule Set, provider rules for SQL injection, XSS, known exploits (e.g., Log4Shell signatures), bad bots.
- **Rate-based rules**: block clients exceeding N requests per window, per IP or per header/key.
- **Geo and IP reputation** rules.
- **Custom rules**: block paths like `/.git/` or `/wp-admin` you never serve.

Deploy new rules in **count/detection mode** first, review logs for false positives, then switch to block. A WAF reduces risk but does not replace fixing vulnerable code.

### 9.4.5 Egress control

Attackers who get code execution want to reach the Internet (download tools, exfiltrate data, contact command-and-control). Restricting egress is underused and highly effective: route egress through a proxy or firewall with a domain allowlist (AWS Network Firewall, Squid, Cilium FQDN policies), use VPC endpoints for cloud APIs, log DNS queries, and alert on unusual destinations.

## 9.5 Lab: Troubleshooting Drills

These drills use the namespace topology from Part 3 (namespaces `a`, `r`, `b`, with routes and forwarding configured). Rebuild it first. For each drill, a teammate (or you, with your eyes closed) applies the fault; you diagnose it using only the methodology.

**Drill 1 — Latency and loss.**

```bash
sudo ip netns exec r tc qdisc add dev vrb root netem delay 150ms 30ms loss 5%
sudo ip netns exec a mtr -n -r -c 50 10.2.0.2
sudo ip netns exec a curl -o /dev/null -s -w 'connect=%{time_connect} total=%{time_total}\n' http://10.2.0.2:8000/
sudo ip netns exec r tc qdisc del dev vrb root
```

Expected finding: latency and loss appear at the final hop; connect time is about the RTT; occasional requests take over 1 s because a lost SYN waits for the 1 s initial RTO.

**Drill 2 — The silent firewall.**

```bash
sudo ip netns exec r iptables -I FORWARD -p tcp --dport 8000 -j DROP
```

Diagnose with `nc -zv` (timeout, not refused), then `tcpdump` on both router interfaces: SYNs arrive on `vra` but never leave `vrb`. Check counters with `sudo ip netns exec r iptables -L FORWARD -v -n` and remove the rule (`-D` instead of `-I`).

**Drill 3 — Missing return route.**

```bash
sudo ip -n b route del default
```

Ping times out. tcpdump in `b` shows echo requests arriving and no replies leaving; `ip -n b route get 10.1.0.2` reports "Network is unreachable". Restore with `sudo ip -n b route add default via 10.2.0.1`.

**Drill 4 — MTU black hole.**

The large data in an HTTP download flows from `b` to `a`, so we shrink the router's interface toward `a` and block the ICMP messages the router would generate:

```bash
sudo ip -n r link set vra mtu 1400
sudo ip netns exec r iptables -I OUTPUT -p icmp --icmp-type fragmentation-needed -j DROP
sudo ip netns exec b sh -c 'head -c 2000000 /dev/urandom > /tmp/big.bin; cd /tmp && python3 -m http.server 8000 --bind 10.2.0.2' &
sudo ip netns exec a curl -m 10 -o /dev/null -w '%{http_code}\n' http://10.2.0.2:8000/        # small: works
sudo ip netns exec a curl -m 10 -o /dev/null http://10.2.0.2:8000/big.bin                       # large: stalls
```

The handshake and small directory listing succeed; the large download times out because `b` keeps sending 1500-byte DF packets that `r` drops, and the "fragmentation needed" message never reaches `b`. Prove it with `sudo ip netns exec b ping -M do -s 1472 -c 2 10.1.0.2` (100% loss) versus `-s 1372` (success). Fix by removing the ICMP drop rule or clamping MSS:

```bash
# Fix A: let PMTUD work again
sudo ip netns exec r iptables -D OUTPUT -p icmp --icmp-type fragmentation-needed -j DROP
# Fix B (alternative): rewrite the MSS in SYNs passing the router to fit the 1400 link (1400 - 40 = 1360)
sudo ip netns exec r iptables -t mangle -A FORWARD -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --set-mss 1360
```

> **Note:** `--clamp-mss-to-pmtu` would not help in this topology. It clamps based on the MTU of the route each SYN takes; the client's SYN leaves the router via `vrb` (MTU 1500), so the MSS that `b` learns stays 1460. The small link is on the *return* path, which is why a fixed `--set-mss` is used here. This asymmetry trips up real VPN deployments too.

**Drill 5 — Wrong DNS.** In namespace `a`, create `/etc/netns/a/resolv.conf` pointing to a non-existent server (`nameserver 10.9.9.9`). Run `sudo ip netns exec a getent ahosts example.com` (hangs, then fails) and `sudo ip netns exec a dig @1.1.1.1 example.com` to prove the network path is not the problem (if `a` has Internet access via NAT). Remove the file to restore.

Write a two-line incident summary for each drill: symptom, root cause, and the single piece of evidence that proved it.

## Key takeaways

- Learn the toolkit by layer: `ip`, `ping`, `mtr`, `dig`/`getent`, `nc`, `curl -v`, `openssl s_client`, `ss`, `tcpdump`, `nmap`, `iperf3`.
- Intermediate-hop loss in mtr is usually ICMP rate limiting; only loss that persists to the destination is real.
- Refused, timed out, and unreachable are different diagnoses. Capture at both ends to split the path.
- Follow a method: define, locate, isolate layer by layer, hypothesize, test one change, fix, prevent.
- MTU black holes break large transfers while small requests work; test with `ping -M do -s`, and fix by allowing ICMP fragmentation-needed, clamping MSS, or lowering MTU.
- Zero trust replaces network location with identity: mTLS, identity-aware proxies, micro-segmentation, no standing access.
- Layer DDoS defense (edge absorption, SYN cookies, WAF rate limits) and control egress, not just ingress.
