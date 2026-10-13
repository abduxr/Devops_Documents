# Part 3 — Routing, Transport (TCP and UDP), Ports, Sockets, and DHCP

Part 2 gave every host an address. This Part answers two questions: how does a packet find its way to a destination network (routing), and how does data get reliably from one *process* to another (transport)? Most production incidents you will debug, from connection timeouts to 502s to "too many open files", live in the transport layer, so we go deep on TCP.

## 3.1 Routing Fundamentals

### 3.1.1 The route table

Every host, not just routers, has a **route table**: a list of destination prefixes and where to send packets for each. When sending a packet, the kernel finds all routes whose prefix contains the destination and picks the **longest prefix match** (the most specific route).

```bash
ip route show
```

```text
default via 10.0.1.1 dev ens5 proto dhcp src 10.0.1.25 metric 100
10.0.1.0/24 dev ens5 proto kernel scope link src 10.0.1.25
10.100.0.0/16 via 10.0.1.250 dev ens5
172.17.0.0/16 dev docker0 proto kernel scope link src 172.17.0.1 linkdown
```

Read each line:

| Route | Meaning |
|-------|---------|
| `default via 10.0.1.1` | 0.0.0.0/0: anything not matched elsewhere goes to the gateway 10.0.1.1 |
| `10.0.1.0/24 dev ens5 scope link` | Directly connected: deliver on-link, ARP for the destination itself |
| `10.100.0.0/16 via 10.0.1.250` | Static route: send to a specific next-hop router (e.g., a VPN appliance) |
| `172.17.0.0/16 dev docker0 linkdown` | Connected to docker0, currently no containers |

Ask the kernel which route it would actually use; this is far more reliable than reading the table yourself:

```bash
ip route get 10.100.4.7
```

```text
10.100.4.7 via 10.0.1.250 dev ens5 src 10.0.1.25 uid 1000
    cache
```

### 3.1.2 Longest prefix match example

```text
Routes:   0.0.0.0/0      -> IGW
          10.0.0.0/8     -> Transit Gateway
          10.20.0.0/16   -> VPC peering
          10.20.5.0/24   -> local

Destination 10.20.5.9  -> matches all four; /24 is longest -> local
Destination 10.20.9.1  -> matches /0, /8, /16; /16 wins     -> peering
Destination 10.99.0.1  -> matches /0, /8;  /8 wins          -> Transit Gateway
Destination 8.8.8.8    -> only /0                           -> IGW
```

Longest prefix match is the rule in Linux, in AWS route tables, in Kubernetes nodes, and on Internet routers. When two routes have the *same* prefix length, metrics (administrative distance or route priority) decide.

### 3.1.3 The default gateway

The default gateway is just the next hop for `0.0.0.0/0`. It must be on a directly connected subnet, because the host must be able to ARP for it. A host with a correct IP but no default route can talk to its own subnet but nothing else, a classic "I can ping my neighbor but not the Internet" symptom.

### 3.1.4 What a router does with a packet

```text
1. Receive frame; verify FCS; strip L2 header.
2. Check IP header checksum; decrement TTL. If TTL hits 0 -> drop,
   send ICMP Time Exceeded back to the source (this is how traceroute works).
3. Longest-prefix lookup on destination IP -> next hop + egress interface.
4. ARP (or neighbor cache) for next hop MAC.
5. Build new L2 header (src = router's egress MAC, dst = next hop MAC).
6. If packet > egress MTU and DF bit set -> drop, send ICMP "Fragmentation Needed".
7. Transmit.
```

To make a Linux machine route between interfaces you must enable forwarding: `sysctl -w net.ipv4.ip_forward=1`. Kubernetes nodes and Docker hosts have this enabled.

### 3.1.5 Asymmetric routing and reverse path filtering

Routing decisions are made independently in each direction. If the request goes A -> R1 -> B but the reply goes B -> R2 -> A, stateful firewalls on either path will see only half the conversation and drop it. Linux's `rp_filter` (strict mode) drops packets that arrive on an interface the kernel would not use to reach the source. Multi-homed hosts (two NICs in different subnets) commonly hit this: you need **policy routing** (`ip rule`, multiple route tables) so that replies leave via the interface they arrived on.

```bash
# Replies from 10.0.2.50 use table 100, which has its own default route
ip route add default via 10.0.2.1 dev ens6 table 100
ip rule add from 10.0.2.50 table 100
ip rule show
```

## 3.2 Dynamic Routing Protocols

Static routes do not scale and do not react to failures. Dynamic routing protocols let routers exchange reachability information automatically.

### 3.2.1 Interior vs exterior

| Category | Protocol | Algorithm | Where you see it |
|----------|----------|-----------|------------------|
| IGP | OSPF | Link-state (Dijkstra shortest path) | Enterprise and data center networks |
| IGP | IS-IS | Link-state | Large ISPs, some DC fabrics |
| IGP | RIP | Distance vector (hop count) | Legacy only |
| EGP | BGP | Path vector | The Internet, cloud hybrid connectivity, Kubernetes (Calico, MetalLB, Cilium) |

### 3.2.2 OSPF in one page

- Every router floods **Link State Advertisements** describing its links and costs.
- Every router builds an identical map of the topology (the LSDB) and runs Dijkstra to compute the shortest path to every prefix.
- Cost is usually derived from bandwidth. Equal-cost paths are load-balanced (**ECMP**).
- Networks are split into **areas** (area 0 is the backbone) to limit flooding.
- Converges in seconds after a link failure, faster with BFD.

### 3.2.3 BGP in one page

BGP (Border Gateway Protocol) is how the Internet's ~75,000+ **Autonomous Systems** (each identified by an ASN) exchange routes. Unlike OSPF, BGP does not compute shortest paths; it applies **policy**.

- BGP peers form a TCP session on **port 179** and exchange prefixes with attributes.
- **AS_PATH** lists the ASes a route has traversed. It prevents loops and serves as a basic distance metric.
- Path selection order (simplified): highest LOCAL_PREF, shortest AS_PATH, lowest origin type, lowest MED, eBGP over iBGP, lowest IGP cost to next hop, then tie-breakers.
- **eBGP** runs between different ASes; **iBGP** runs within one AS.

```text
      AS 64500 (your DC)            AS 16509 (AWS)
   [Router] <==== eBGP TCP/179 ====> [Direct Connect / VPN endpoint]
   advertises 10.50.0.0/16             advertises 10.0.0.0/16 (VPC)
```

Where a DevOps engineer meets BGP:

- **AWS Direct Connect and Site-to-Site VPN** use BGP to exchange routes between on-prem and the VPC/Transit Gateway.
- **Kubernetes**: Calico peers with top-of-rack switches to advertise pod CIDRs; MetalLB in BGP mode and Cilium BGP Control Plane advertise LoadBalancer IPs.
- **Anycast**: CDNs and DNS providers announce the same prefix from many locations; BGP sends you to the nearest one.
- **Outages**: route leaks and hijacks (where an AS announces prefixes it does not own) have taken down major services. RPKI signs route origins to mitigate this.

> **Interview:** "What is the difference between OSPF and BGP?" OSPF is a link-state IGP that finds the shortest path inside one organization. BGP is a path-vector EGP that exchanges routes between organizations based on policy, over TCP 179. OSPF optimizes for cost; BGP optimizes for business policy and scale.

## 3.3 The Transport Layer: Ports and Sockets

IP delivers packets to a *host*. The transport layer delivers data to a *process*, identified by a **port** (a 16-bit number, 0–65535).

| Range | Name | Notes |
|-------|------|-------|
| 0–1023 | Well-known / system ports | Binding requires root or `CAP_NET_BIND_SERVICE` on Linux |
| 1024–49151 | Registered ports | e.g., 3306 MySQL, 5432 PostgreSQL, 6443 Kubernetes API |
| 49152–65535 | Dynamic / ephemeral (IANA) | Linux default ephemeral range is 32768–60999 |

A **socket** is an endpoint: IP + port + protocol. A TCP **connection** is uniquely identified by the **5-tuple**:

```text
(protocol, source IP, source port, destination IP, destination port)
(TCP,      10.0.1.25, 51544,       10.0.2.40,      5432)
```

This is why one server port (443) can serve millions of clients: each connection has a different client IP/port. It is also why a single client can open only about 28,000 connections (the Linux ephemeral range) to the *same* destination IP:port from one source IP, which matters for proxies and NAT.

```bash
cat /proc/sys/net/ipv4/ip_local_port_range
ss -tlnp            # listening TCP sockets with process
ss -tunap           # all TCP and UDP sockets
ss -tan state established '( dport = :5432 )'
```

```text
State   Recv-Q  Send-Q   Local Address:Port    Peer Address:Port  Process
LISTEN  0       511            0.0.0.0:80           0.0.0.0:*      users:(("nginx",pid=1201,fd=6))
LISTEN  0       4096         127.0.0.1:5432         0.0.0.0:*      users:(("postgres",pid=900,fd=5))
LISTEN  0       4096                 *:9100               *:*      users:(("node_exporter",pid=777,fd=3))
```

> **Warning:** `127.0.0.1:5432` means PostgreSQL only accepts connections from the same host. If a remote client gets "connection refused", the bind address is the first thing to check. `0.0.0.0` (or `*`, or `[::]`) means all interfaces. Inside containers, an app bound to 127.0.0.1 is unreachable through `docker run -p`, which is a very common bug.

For LISTEN sockets, `Recv-Q` is the current accept queue length and `Send-Q` is the backlog limit. If `Recv-Q` approaches `Send-Q`, the application is not calling `accept()` fast enough and new connections will be dropped.

## 3.4 TCP in Depth

TCP (Transmission Control Protocol) provides a reliable, ordered, byte-stream connection with flow control and congestion control, on top of unreliable IP.

### 3.4.1 The TCP header (fields that matter)

```text
 0                   15 16                   31
+----------------------+----------------------+
|     Source Port      |   Destination Port   |
+----------------------+----------------------+
|               Sequence Number               |
+---------------------------------------------+
|            Acknowledgment Number            |
+------+------+--------+----------------------+
|Offset| Rsvd | Flags  |     Window Size      |
+------+------+--------+----------------------+
|      Checksum        |   Urgent Pointer     |
+----------------------+----------------------+
|          Options (MSS, SACK, WScale, TS)    |
+---------------------------------------------+
Flags: SYN, ACK, FIN, RST, PSH, URG, ECE, CWR
```

### 3.4.2 The three-way handshake

```text
Client                                             Server (LISTEN)
  |---- SYN  seq=1000, MSS=1460, WScale=7, SACK --->|
  |                                                 |  (half-open, in SYN queue)
  |<--- SYN-ACK seq=5000 ack=1001, MSS=8961 --------|
  |                                                 |
  |---- ACK  seq=1001 ack=5001 -------------------->|  (moves to accept queue)
  |                                                 |
  ESTABLISHED                                   ESTABLISHED
```

Why three steps: both sides must choose an initial sequence number (ISN) and confirm the other side received it. Options negotiated here (MSS, window scaling, SACK, timestamps) cannot be changed later.

**Cost:** a new TCP connection costs one round trip before any data flows. Add TLS 1.3 and it is two; TLS 1.2 is three. Across regions with 80 ms RTT, a fresh HTTPS connection costs 160–240 ms before the first byte of the request. This is the core reason connection pooling and keep-alive matter so much.

**What failure looks like:**

| Symptom | Meaning |
|---------|---------|
| SYN sent, RST received immediately | Host reachable, nothing listening on that port ("Connection refused") |
| SYN sent, no reply, retransmits | Packet dropped by firewall/SG/NACL, or host down, or routing broken ("Connection timed out") |
| SYN sent, ICMP unreachable received | A router or firewall actively rejecting ("No route to host") |

> **Tip:** "Refused" versus "timed out" is the most valuable single clue in network debugging. Refused proves the network path works and the host is up; the problem is the service. Timeout points to a firewall, security group, routing, or the host being down.

Linux retries SYN `net.ipv4.tcp_syn_retries` times (default 6) with exponential backoff (1, 2, 4, 8, 16, 32 s), so a connection to a black-holed port takes about 127 seconds to fail. Applications should set their own connect timeouts.

### 3.4.3 Connection teardown

```text
Active closer (A)                                 Passive closer (B)
ESTABLISHED                                       ESTABLISHED
  |---- FIN ------------------------------------>|
FIN_WAIT_1                                      CLOSE_WAIT  (app must call close())
  |<--- ACK -------------------------------------|
FIN_WAIT_2                                        |
  |<--- FIN -------------------------------------| LAST_ACK
TIME_WAIT                                         |
  |---- ACK ------------------------------------>| CLOSED
  (wait 2*MSL; 60 s on Linux)
CLOSED
```

A connection can also be killed instantly with **RST** (reset), for example when an application crashes, a load balancer idle-times out a connection, or a firewall rejects it. "Connection reset by peer" means you received an RST.

### 3.4.4 TCP states you will see in `ss`

| State | Meaning | When it is a problem |
|-------|---------|---------------------|
| LISTEN | Waiting for connections | — |
| SYN-SENT | Client sent SYN, waiting | Many = destination unreachable or filtered |
| SYN-RECV | Server got SYN, sent SYN-ACK | Many = SYN flood or clients cannot complete handshake |
| ESTABLISHED | Data flowing | Huge counts may indicate connection leaks |
| FIN-WAIT-2 | We closed, waiting for peer's FIN | Many = peer not closing |
| CLOSE-WAIT | Peer closed, **our app has not called close()** | Almost always an application bug (leaked sockets) |
| TIME-WAIT | We closed first; waiting to absorb stray packets | Tens of thousands on a client may exhaust ports |

```bash
ss -tan | awk 'NR>1 {print $1}' | sort | uniq -c | sort -rn
```

```text
  18234 TIME-WAIT
   1210 ESTAB
    412 CLOSE-WAIT
      9 LISTEN
```

That output tells a story: lots of short-lived outbound connections (TIME-WAIT), and an application leaking sockets (412 CLOSE-WAIT that will never clear by themselves).

### 3.4.5 TIME_WAIT: why it exists and how to handle it

TIME_WAIT exists for two reasons: (1) if the final ACK is lost, the peer will resend its FIN, and we must still be able to respond; (2) delayed packets from the old connection must not be accepted by a new connection reusing the same 5-tuple. It lasts 2 x MSL; Linux hardcodes 60 seconds.

TIME_WAIT sits on the side that **closes first**. Problems appear when a client (often a proxy or a service calling an API) opens and closes thousands of connections per second to the same destination: each consumes an ephemeral port for 60 seconds. At 28,000 ports / 60 s you cap out around 470 new connections per second to one destination.

Fixes, in order of preference:

1. **Reuse connections** (HTTP keep-alive, connection pools, upstream keepalive in Nginx). This removes the problem instead of tuning around it.
2. `net.ipv4.tcp_tw_reuse=1` allows reusing TIME_WAIT sockets for new *outbound* connections when safe (uses timestamps). Safe and widely used.
3. Widen `net.ipv4.ip_local_port_range`, or add source IPs.
4. Do **not** use `tcp_tw_recycle`; it broke clients behind NAT and was removed in Linux 4.12.

### 3.4.6 Flow control: the receive window

Each side advertises a **receive window** (rwnd): how many bytes it can buffer. The sender must not have more unacknowledged data in flight than the window allows. The 16-bit window field maxes at 65,535 bytes, so the **window scale** option multiplies it (up to 1 GiB).

The maximum throughput of a single TCP connection is bounded by:

```text
Throughput <= Window / RTT

Example: 64 KB window, 100 ms RTT  -> 65,535 B / 0.1 s = 655 KB/s  (~5.2 Mbit/s)
Example: 4 MB window,  100 ms RTT  -> 4,194,304 / 0.1  = 42 MB/s   (~335 Mbit/s)

Bandwidth-delay product (BDP) = bandwidth x RTT
1 Gbit/s x 100 ms = 125,000,000 B/s x 0.1 s = 12.5 MB of buffer needed to fill the pipe
```

This explains why cross-region transfers are slow even on fat links: buffers too small for the BDP. Linux autotunes buffers within `net.ipv4.tcp_rmem` / `tcp_wmem`. A zero window ("TCP ZeroWindow" in Wireshark) means the receiving application is not reading data fast enough.

### 3.4.7 Congestion control

The receive window protects the receiver; the **congestion window** (cwnd) protects the network. The sender can have at most min(rwnd, cwnd) bytes in flight.

- **Slow start**: cwnd starts small (Linux initial cwnd = 10 segments, about 14 KB) and roughly doubles every RTT until loss or a threshold. This is why short HTTP transfers never reach full bandwidth, and why reusing a warm connection is faster.
- **Congestion avoidance**: grow linearly after the threshold.
- **On loss**: reduce cwnd. Classic algorithms (Reno, **CUBIC**, the Linux default) treat loss as congestion.
- **BBR**: models bottleneck bandwidth and RTT instead of reacting to loss; often much better on lossy long-haul links. Enable with `sysctl -w net.ipv4.tcp_congestion_control=bbr`.

### 3.4.8 Retransmission

TCP detects loss two ways:

- **Retransmission timeout (RTO)**: no ACK within the RTO (computed from smoothed RTT; Linux minimum 200 ms, initial 1 s). Expensive: the connection stalls.
- **Fast retransmit**: three duplicate ACKs indicate a specific segment was lost while later ones arrived; retransmit immediately. **SACK** (selective acknowledgment) tells the sender exactly which ranges arrived.

Even 1% packet loss can cut throughput dramatically and adds tail latency spikes of 200 ms+ on affected requests. Check retransmissions with:

```bash
nstat -az | grep -E 'TcpRetransSegs|TcpExtTCPTimeouts|TcpExtListenOverflows|TcpExtListenDrops'
ss -ti dst 10.0.2.40   # per-connection rtt, cwnd, retrans
```

```text
ESTAB 0 0 10.0.1.25:51544 10.0.2.40:5432
   cubic wscale:7,7 rto:204 rtt:1.27/0.4 mss:8949 cwnd:10 bytes_acked:48211 retrans:0/2 rcv_space:62727
```

### 3.4.9 Keepalives and idle timeouts

TCP itself sends nothing on an idle connection. Middleboxes (NAT gateways, load balancers, firewalls) drop idle state after a timeout: AWS NAT Gateway 350 s, AWS ALB default 60 s, AWS NLB 350 s for TCP. The endpoints do not know and the next send gets an RST or hangs. Solutions: enable TCP keepalive with an interval below the shortest middlebox timeout (Linux default `tcp_keepalive_time` is 7200 s, far too long), or application-level pings.

## 3.5 UDP

UDP (User Datagram Protocol) is an 8-byte header with ports, length, and checksum. No handshake, no ordering, no retransmission, no congestion control.

| | TCP | UDP |
|---|-----|-----|
| Connection | Connection-oriented (handshake) | Connectionless |
| Reliability | Acknowledged, retransmitted | Best effort |
| Ordering | Guaranteed | None |
| Flow/congestion control | Yes | No (application's job) |
| Header | 20–60 bytes | 8 bytes |
| Boundaries | Byte stream (no message boundaries) | Preserves datagram boundaries |
| Uses | HTTP/1.1, HTTP/2, SSH, databases | DNS, NTP, DHCP, syslog, VoIP, video, QUIC (HTTP/3), WireGuard, VXLAN |

Why use UDP? Lower latency (no handshake), no head-of-line blocking, and the ability to build your own reliability exactly how you want it (which is what QUIC does).

Debugging UDP is harder: there is no handshake to prove reachability. A closed UDP port usually triggers an ICMP "port unreachable", but if a firewall drops packets you just get silence. Firewalls and NATs track UDP "connections" with short timeouts (often 30–180 s).

```bash
# Test UDP reachability with netcat
nc -u -v -z 10.0.0.2 53
# Listen on UDP 9999 on one host, send from another
nc -u -l 9999
echo hello | nc -u -w1 10.0.0.5 9999
```

## 3.6 DHCP

**DHCP** (Dynamic Host Configuration Protocol) automatically gives hosts an IP address, mask, default gateway, DNS servers, and other options. It uses UDP: server port 67, client port 68.

### 3.6.1 DORA

```text
Client (0.0.0.0:68)                          DHCP Server (:67)
  |--- DISCOVER (broadcast 255.255.255.255) --->|   "I need an address"
  |<-- OFFER  (10.0.1.25, lease 3600 s) --------|   "You can have 10.0.1.25"
  |--- REQUEST (broadcast, "I take .25") ------>|   (broadcast so other servers know)
  |<-- ACK (lease confirmed + options) ---------|
```

- **Lease**: the address is rented. The client renews at 50% of the lease (T1) directly with the server, and rebinds via broadcast at 87.5% (T2).
- **Options**: 1 subnet mask, 3 router, 6 DNS servers, 15 domain name, 42 NTP, 119 search domains, 121 classless static routes.
- **DHCP relay** (ip helper): broadcasts do not cross routers, so routers forward DHCP to a central server and tag the originating subnet (giaddr).

In AWS, DHCP is provided by the VPC and configured by a **DHCP options set** (domain name, DNS servers, NTP). Changing it is how you point an entire VPC at custom DNS resolvers. Kubernetes pods do not use DHCP; IPs come from the CNI's IPAM.

```bash
# Inspect lease details on a systemd-networkd host
networkctl status ens5
# Or NetworkManager
nmcli device show ens5 | grep -E 'IP4|DHCP'
```

> **Note:** Rogue DHCP servers (someone plugs in a home router) hand out wrong gateways and break whole offices. Switches defend against this with DHCP snooping.

## 3.7 Lab: Routing Between Networks and Watching TCP

**Part A — Two subnets and a router.**

```text
 ns: a 10.1.0.2/24 ---- ns: r (10.1.0.1 | 10.2.0.1) ---- ns: b 10.2.0.2/24
```

**Step 1.** Build the topology:

```bash
for ns in a r b; do sudo ip netns add $ns; done
sudo ip link add va type veth peer name vra
sudo ip link add vb type veth peer name vrb
sudo ip link set va netns a; sudo ip link set vra netns r
sudo ip link set vb netns b; sudo ip link set vrb netns r
sudo ip -n a addr add 10.1.0.2/24 dev va
sudo ip -n r addr add 10.1.0.1/24 dev vra
sudo ip -n r addr add 10.2.0.1/24 dev vrb
sudo ip -n b addr add 10.2.0.2/24 dev vb
for p in "a va" "r vra" "r vrb" "b vb"; do set -- $p; sudo ip -n $1 link set $2 up; sudo ip -n $1 link set lo up; done
sudo ip netns exec r sysctl -qw net.ipv4.ip_forward=1
```

**Step 2.** Ping `b` from `a`. It fails with "Network is unreachable", because `a` has only a connected route:

```bash
sudo ip netns exec a ping -c1 10.2.0.2
sudo ip -n a route
```

**Step 3.** Add a route on `a` only. Ping now times out instead: requests reach `b`, but `b` has no route back. Confirm with `tcpdump` in `b`:

```bash
sudo ip -n a route add 10.2.0.0/24 via 10.1.0.1
sudo ip netns exec b tcpdump -ni vb icmp -c 2 &
sudo ip netns exec a ping -c2 -W1 10.2.0.2
```

**Step 4.** Add the return route on `b` (use a default route this time) and the ping succeeds. Note the TTL of 63: one router hop decremented it.

```bash
sudo ip -n b route add default via 10.2.0.1
sudo ip netns exec a ping -c2 10.2.0.2
sudo ip netns exec a ip route get 10.2.0.2
```

**Part B — Watch a TCP handshake and states.**

**Step 5.** Start a capture on the router and a TCP listener in `b`:

```bash
sudo ip netns exec r tcpdump -ni vrb 'tcp port 8000' &
sudo ip netns exec b python3 -m http.server 8000 --bind 10.2.0.2 &
```

**Step 6.** Make a request from `a`:

```bash
sudo ip netns exec a curl -s -o /dev/null -w '%{http_code}\n' http://10.2.0.2:8000/
```

```text
IP 10.1.0.2.46302 > 10.2.0.2.8000: Flags [S], seq 3711029473, win 64240, options [mss 1460,sackOK,TS val 1 ecr 0,nop,wscale 7], length 0
IP 10.2.0.2.8000 > 10.1.0.2.46302: Flags [S.], seq 1402219917, ack 3711029474, win 65160, options [mss 1460,...], length 0
IP 10.1.0.2.46302 > 10.2.0.2.8000: Flags [.], ack 1, win 502, length 0
IP 10.1.0.2.46302 > 10.2.0.2.8000: Flags [P.], seq 1:78, ack 1, win 502, length 77: HTTP: GET / HTTP/1.1
...
IP 10.2.0.2.8000 > 10.1.0.2.46302: Flags [F.], seq 924, ack 78, length 0
```

In tcpdump, `[S]` = SYN, `[S.]` = SYN-ACK, `[.]` = ACK, `[P.]` = PUSH+ACK (data), `[F.]` = FIN, `[R]` = RST.

**Step 7.** Observe TIME-WAIT on the side that closed first (here the server, because `http.server` closes after responding):

```bash
sudo ip netns exec b ss -tan state time-wait
```

**Step 8.** Compare refused vs timeout. Connect to a closed port (`curl http://10.2.0.2:9999`): you get an immediate "Connection refused" and tcpdump shows `[R.]`. Now drop traffic on the router and retry:

```bash
sudo ip netns exec r iptables -A FORWARD -p tcp --dport 8000 -j DROP
sudo ip netns exec a curl -m 5 http://10.2.0.2:8000/
```

```text
curl: (28) Connection timed out after 5001 milliseconds
```

**Step 9.** Clean up: `for ns in a r b; do sudo ip netns del $ns; done` and `kill %1 %2` for the background jobs.

## Key takeaways

- Every host has a route table; the kernel uses longest prefix match. Use `ip route get <ip>` to see the real decision.
- Routing is per direction; missing return routes and asymmetric paths are common causes of "one-way" connectivity.
- OSPF is a link-state IGP; BGP (TCP 179) is the policy-driven path-vector protocol of the Internet, hybrid cloud, and Kubernetes load balancer advertisement.
- A connection is identified by its 5-tuple; ephemeral port ranges limit connections from one source to one destination.
- TCP handshake costs one RTT; teardown leaves TIME_WAIT on the active closer for 60 s on Linux. CLOSE_WAIT buildup is an application bug.
- "Connection refused" (RST) means the path works but nothing listens; "timed out" means packets are being dropped.
- Throughput is bounded by window / RTT; loss triggers retransmission and slashes throughput. Keep idle connections alive below middlebox timeouts.
- UDP trades reliability for latency and simplicity; DHCP (UDP 67/68) uses DORA to hand out leases and options.
