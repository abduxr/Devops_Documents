# Part 1 — Foundations

Networking is the one discipline that every DevOps task eventually touches. A failed deployment, a slow API, a pod that cannot reach its database, a certificate warning, a mysterious 502: all of them are networking problems wearing a different costume. This Part builds the mental model you will use for the rest of the course. Do not skip it even if you "know the OSI model"; the goal here is not to memorize layers but to be able to *reason* about where a packet is and what is touching it.

## 1.1 What a Network Actually Is

A network is a set of devices (hosts) that can exchange data using agreed-upon rules (protocols). That definition hides three separate problems that every network must solve:

1. **Physical delivery** — getting bits from one interface to another over copper, fiber, or radio.
2. **Addressing** — identifying who the data is for, both on the local segment and across the world.
3. **Reliability and meaning** — making sure data arrives intact, in order (when needed), and is delivered to the right application.

Each of those problems is solved by a different layer of protocols. Engineers split the problem into layers so that each layer can evolve independently. Your web application does not care whether the packet travelled over Wi-Fi or a 100 Gbit fiber, and your Ethernet switch does not care whether the payload is HTTP or SSH.

### 1.1.1 Key vocabulary

| Term | Meaning |
|------|---------|
| Host | Any device with a network interface: server, laptop, container, VM |
| Interface / NIC | The point where a host attaches to a network (`eth0`, `ens5`, `wlan0`) |
| Link | A direct connection between two or more interfaces at Layer 2 |
| Segment / broadcast domain | The set of hosts that receive each other's broadcasts |
| Packet | A unit of data at Layer 3 (IP) |
| Frame | A unit of data at Layer 2 (Ethernet) |
| Segment (TCP) / Datagram (UDP) | A unit of data at Layer 4 |
| Protocol | A formal set of rules for formatting and exchanging messages |
| Bandwidth | Maximum rate of a link (bits per second) |
| Throughput | Actual achieved rate |
| Latency | Time for a bit to travel from A to B (often measured as round-trip time, RTT) |
| Jitter | Variation in latency |
| Packet loss | Percentage of packets that never arrive |

> **Note:** "Bandwidth" and "latency" are independent. A satellite link can have high bandwidth and terrible latency. A cross-region API call is usually latency-bound, not bandwidth-bound. Most "the network is slow" tickets are actually latency multiplied by the number of round trips an application makes.

### 1.1.2 LAN, WAN, and the Internet

- A **LAN** (Local Area Network) is a network under one administrative control in one location, typically Ethernet and Wi-Fi.
- A **WAN** (Wide Area Network) connects LANs across distance: MPLS circuits, SD-WAN, site-to-site VPNs, or the Internet itself.
- **The Internet** is a network of about 75,000+ independently operated networks (Autonomous Systems) that agree to exchange routes using BGP.
- A **cloud VPC** is a software-defined LAN that lives inside a provider's data center. It behaves like a LAN to you but is implemented completely differently underneath (no real broadcast, no real ARP on the wire).

## 1.2 Layered Models: OSI vs TCP/IP

### 1.2.1 The OSI model

The OSI (Open Systems Interconnection) model has seven layers. Nobody implements OSI as a protocol suite, but its numbering is the industry's shared language: "that's an L4 load balancer", "L7 firewall", "L2 adjacency".

| # | OSI Layer | Job | Examples | Unit |
|---|-----------|-----|----------|------|
| 7 | Application | Application semantics | HTTP, DNS, SMTP, gRPC | Message |
| 6 | Presentation | Encoding, encryption, compression | TLS (loosely), JSON, gzip | Message |
| 5 | Session | Session setup and teardown | RPC sessions, TLS sessions (loosely) | Message |
| 4 | Transport | End-to-end delivery between processes | TCP, UDP, QUIC | Segment / Datagram |
| 3 | Network | Logical addressing and routing across networks | IPv4, IPv6, ICMP | Packet |
| 2 | Data Link | Delivery on the local link, MAC addressing | Ethernet, 802.11, ARP (L2/L3 boundary) | Frame |
| 1 | Physical | Bits on a medium | Copper, fiber, radio, optics | Bit |

### 1.2.2 The TCP/IP model

The TCP/IP model is what the Internet actually runs. It collapses OSI 5–7 into one "Application" layer and OSI 1–2 into "Link".

```text
   OSI                         TCP/IP
+------------------+       +------------------+
| 7 Application    |       |                  |
| 6 Presentation   |  -->  |   Application    |   HTTP, DNS, TLS, SSH
| 5 Session        |       |                  |
+------------------+       +------------------+
| 4 Transport      |  -->  |   Transport      |   TCP, UDP, QUIC
+------------------+       +------------------+
| 3 Network        |  -->  |   Internet       |   IP, ICMP
+------------------+       +------------------+
| 2 Data Link      |  -->  |   Link           |   Ethernet, Wi-Fi, ARP
| 1 Physical       |       |                  |
+------------------+       +------------------+
```

> **Interview:** "Which layer does TLS live in?" The honest answer: it sits between the transport layer and the application, so people call it L6 or "L4.5". In the TCP/IP model it is part of the application layer. Show you understand that models are approximations, not laws.

### 1.2.3 Encapsulation: why layers matter in practice

When your application sends data, each layer wraps the payload with its own header. On the receiving side, each layer strips its header and hands the rest upward.

```text
Application data:                                [ HTTP GET /index.html ]
Transport (TCP) adds ports, seq numbers:     [TCP| HTTP GET /index.html ]
Network (IP) adds source/dest IP:         [IP|TCP| HTTP GET /index.html ]
Link (Ethernet) adds MACs + FCS:      [ETH|IP|TCP| HTTP GET /index.html |FCS]

Typical header sizes:
  Ethernet header 14 bytes (+4 FCS)   IPv4 header 20 bytes (no options)
  TCP header 20 bytes (no options; usually 32 with timestamps)
  IPv6 header 40 bytes                UDP header 8 bytes
```

This is the single most useful model for troubleshooting. Every device in the path operates on a specific layer and only looks at headers up to that layer:

| Device | Highest layer it reads | What it changes |
|--------|------------------------|-----------------|
| Hub / repeater (obsolete) | L1 | Nothing, just repeats bits |
| Switch | L2 | Nothing in the frame (it forwards by MAC) |
| Router | L3 | Rewrites L2 header, decrements TTL |
| NAT device | L3/L4 | Rewrites IPs and ports |
| Stateful firewall / Security Group | L3/L4 | Drops or allows; tracks connections |
| L4 load balancer | L4 | Rewrites destination IP/port (or proxies TCP) |
| L7 load balancer / reverse proxy | L7 | Terminates TCP and TLS, opens a new connection |

> **Tip:** When debugging, ask "which layer is broken?" and test bottom-up. Is the interface up (L1/L2)? Do I have an IP and a route (L3)? Is the port open (L4)? Does the application respond correctly (L7)? Part 9 turns this into a formal methodology.

### 1.2.4 MTU and why headers cost you

The **MTU** (Maximum Transmission Unit) is the largest L3 packet a link can carry without fragmentation. Standard Ethernet MTU is 1500 bytes. Subtract 20 bytes IPv4 and 20 bytes TCP and you get a **MSS** (Maximum Segment Size) of 1460 bytes of application data per packet. Tunnels (VXLAN, IPsec, WireGuard, GRE) add their own headers and reduce effective MTU. That is the root cause of a whole class of "small requests work, large ones hang" bugs, which we cover in Part 9.

| Network | Typical MTU |
|---------|-------------|
| Standard Ethernet | 1500 |
| AWS within a VPC | 9001 (jumbo frames) |
| AWS over IGW / VPN / inter-region peering | 1500 (VPN lower, approx. 1436 or less) |
| VXLAN overlay on 1500 underlay | 1450 |
| WireGuard default | 1420 |
| PPPoE home broadband | 1492 |

## 1.3 Ethernet and MAC Addresses

### 1.3.1 The Ethernet frame

Ethernet (IEEE 802.3) is the dominant Layer 2 technology, and Wi-Fi (802.11) is designed to look like Ethernet to upper layers.

```text
+-------------+-------------+-----------+------------------+-----+
| Dest MAC    | Source MAC  | EtherType | Payload          | FCS |
| 6 bytes     | 6 bytes     | 2 bytes   | 46-1500 bytes    | 4 B |
+-------------+-------------+-----------+------------------+-----+
EtherType examples: 0x0800 = IPv4, 0x86DD = IPv6, 0x0806 = ARP, 0x8100 = 802.1Q VLAN tag
```

The **FCS** (Frame Check Sequence) is a CRC. A receiving NIC silently drops frames with a bad FCS, which shows up as `rx_crc_errors` in interface counters. Rising CRC errors almost always mean a physical problem: bad cable, dirty optic, duplex mismatch.

### 1.3.2 MAC addresses

A MAC address is a 48-bit identifier written as six hex octets: `0a:1b:2c:3d:4e:5f`.

- The first three octets are the **OUI** (Organizationally Unique Identifier) assigned to the manufacturer.
- The least significant bit of the first octet is the **I/G bit**: 0 = unicast, 1 = multicast.
- The second-least significant bit is the **U/L bit**: 1 = locally administered. Virtual NICs, containers, and phones using MAC randomization set this bit. The `02:42:...` prefix you see on Docker containers is a locally administered address.
- `ff:ff:ff:ff:ff:ff` is the broadcast address: every host on the segment receives it.

MAC addresses are only meaningful **on the local link**. Every time a packet crosses a router, the source and destination MACs are rewritten. The IP addresses stay the same end-to-end (unless NAT is involved).

```bash
# Show interfaces and their MAC addresses
ip link show
```

```text
1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536 qdisc noqueue state UNKNOWN mode DEFAULT group default qlen 1000
    link/loopback 00:00:00:00:00:00 brd 00:00:00:00:00:00
2: ens5: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 9001 qdisc mq state UP mode DEFAULT group default qlen 1000
    link/ether 0a:3f:91:c2:7d:11 brd ff:ff:ff:ff:ff:ff
3: docker0: <NO-CARRIER,BROADCAST,MULTICAST,UP> mtu 1500 qdisc noqueue state DOWN mode DEFAULT group default
    link/ether 02:42:5e:8a:01:9c brd ff:ff:ff:ff:ff:ff
```

Read that output carefully: `UP` means administratively up, `LOWER_UP` means the physical link (carrier) is detected. `NO-CARRIER` on `docker0` simply means no containers are attached yet.

## 1.4 ARP: Gluing Layer 3 to Layer 2

### 1.4.1 The problem ARP solves

Your application knows the destination **IP** address. The NIC can only send frames to a **MAC** address. ARP (Address Resolution Protocol, RFC 826) asks the local segment: "Who has 10.0.1.20? Tell 10.0.1.10."

The critical decision a host makes before sending any packet:

```text
Is the destination IP in one of my directly connected subnets?
   |
   +-- YES --> ARP for the destination IP itself, send frame directly to it
   |
   +-- NO  --> Look up the route table, find the next hop (usually the default gateway),
               ARP for the GATEWAY's IP, send the frame to the gateway's MAC
               (the IP header still carries the final destination IP)
```

This is why a wrong subnet mask breaks things in strange ways: the host thinks a remote IP is local (or vice versa) and ARPs for something that will never answer.

### 1.4.2 ARP exchange

```text
Host A 10.0.1.10 (aa:aa..)                         Host B 10.0.1.20 (bb:bb..)
      |                                                    |
      |-- ARP Request (broadcast ff:ff:ff:ff:ff:ff) ------->|  "Who has 10.0.1.20?
      |   (every host on the segment sees this)            |   Tell 10.0.1.10"
      |                                                    |
      |<------------- ARP Reply (unicast to aa:aa..) ------|  "10.0.1.20 is at bb:bb.."
      |                                                    |
      |== IP packet in frame dst=bb:bb.. ================>|
```

Both hosts cache the result. On Linux, the cache is the **neighbor table**:

```bash
ip neigh show
```

```text
10.0.1.1 dev ens5 lladdr 0a:12:7f:00:00:01 REACHABLE
10.0.1.20 dev ens5 lladdr 0a:9c:44:31:be:02 STALE
10.0.1.33 dev ens5  FAILED
```

- `REACHABLE`: recently confirmed.
- `STALE`: valid but will be re-verified on next use.
- `FAILED`: ARP got no answer. If you see `FAILED` for your default gateway, nothing off-subnet will work. This is a strong L2/L3 clue.

### 1.4.3 Gratuitous ARP and why HA setups depend on it

A **gratuitous ARP** is an unsolicited ARP reply a host sends to announce "IP X is now at MAC Y". Keepalived/VRRP uses it when a virtual IP fails over from one server to another so that switches and neighbors update their caches immediately. In public clouds you usually cannot do this (the provider's virtual network ignores it), which is why cloud HA uses API calls to move Elastic IPs or secondary IPs instead.

> **Warning:** ARP has no authentication. Any host on a segment can claim any IP (ARP spoofing), which enables man-in-the-middle attacks. Cloud providers block this at the hypervisor (source/destination check). On-prem, switches use Dynamic ARP Inspection. This is one of many reasons "being on the same LAN" should not imply trust.

### 1.4.4 IPv6 equivalent

IPv6 does not use ARP. It uses **NDP** (Neighbor Discovery Protocol), which runs over ICMPv6 and uses multicast instead of broadcast. `ip -6 neigh` shows the IPv6 neighbor cache. Part 2 covers this.

## 1.5 Switches

### 1.5.1 How a switch learns

A switch is a multi-port Layer 2 device. It builds a **MAC address table** (also called a CAM table) by watching source MACs of incoming frames:

```text
Step 1: A (port 1) sends a frame to B. Switch learns "A is on port 1".
        Switch does not know B yet -> FLOODS frame out all ports except port 1.
Step 2: B (port 3) replies to A. Switch learns "B is on port 3".
        Switch knows A is on port 1 -> forwards ONLY to port 1.
Step 3: All further A<->B traffic is switched directly between ports 1 and 3.

MAC table:
  MAC                 Port   VLAN   Age
  aa:aa:aa:aa:aa:aa   1      10     12s
  bb:bb:bb:bb:bb:bb   3      10     12s
```

Entries age out (commonly 300 seconds). Broadcasts (ARP, DHCP Discover) are always flooded within the VLAN.

### 1.5.2 Loops and Spanning Tree

Ethernet frames have no TTL. If you connect two switches with two cables, a broadcast will loop forever and multiply, saturating the network in seconds (a **broadcast storm**). **STP** (Spanning Tree Protocol, 802.1D, and its faster successor RSTP 802.1w) detects redundant paths and blocks some ports to make the topology loop-free. Modern data centers avoid STP's limitations by using routed leaf-spine fabrics (every link is L3, using ECMP) with overlays like VXLAN/EVPN on top.

### 1.5.3 Linux bridges are switches

On Linux, a **bridge** is a software switch. Docker's `docker0`, libvirt's `virbr0`, and many Kubernetes CNIs use bridges. You will build one in this Part's lab.

```bash
ip link add br0 type bridge
ip link set br0 up
bridge fdb show br br0    # the bridge's MAC table
```

## 1.6 VLANs

### 1.6.1 Why VLANs exist

A **VLAN** (Virtual LAN, IEEE 802.1Q) splits one physical switch into multiple isolated broadcast domains. Hosts in VLAN 10 cannot talk to hosts in VLAN 20 at Layer 2; traffic between them must go through a router (or L3 switch), where it can be filtered. Reasons to use VLANs:

- **Security segmentation**: separate management, production, storage, guest Wi-Fi.
- **Smaller broadcast domains**: ARP and other broadcasts do not reach everyone.
- **Flexibility**: group hosts logically, not by which switch they are plugged into.

### 1.6.2 Access ports, trunk ports, and tagging

```text
                  Trunk (802.1Q tagged: VLAN 10, 20, 30)
   +----------+ ========================================== +----------+
   | Switch A |                                            | Switch B |
   +----------+                                            +----------+
    |access    |access                                      |access
    |VLAN 10   |VLAN 20                                     |VLAN 10
   [Web-1]   [DB-1]                                       [Web-2]

802.1Q tag (4 bytes) inserted after the source MAC:
+---------+---------+---------------------------+-----------+---------+-----+
| Dst MAC | Src MAC | 802.1Q tag                | EtherType | Payload | FCS |
|         |         | TPID 0x8100 | PRI|DEI|VID |           |         |     |
+---------+---------+---------------------------+-----------+---------+-----+
VID = 12 bits -> 4094 usable VLAN IDs (0 and 4095 reserved)
```

- **Access port**: belongs to one VLAN, frames are untagged. Hosts are unaware of VLANs.
- **Trunk port**: carries multiple VLANs; frames are tagged with the VLAN ID. Used between switches, and to hypervisors.
- **Native VLAN**: the one VLAN on a trunk that is sent untagged. Mismatched native VLANs are a classic misconfiguration (and a VLAN-hopping attack vector).

On Linux you can create a VLAN sub-interface directly:

```bash
ip link add link eth0 name eth0.20 type vlan id 20
ip addr add 192.168.20.5/24 dev eth0.20
ip link set eth0.20 up
```

### 1.6.3 VLANs vs VXLAN

The 4094 VLAN limit is far too small for multi-tenant clouds. **VXLAN** encapsulates Layer 2 frames in UDP (port 4789) with a 24-bit identifier (VNI), giving about 16 million segments, and lets L2 segments stretch over an L3 network. Kubernetes CNIs such as Flannel and Calico (in VXLAN mode) use it. The cost is 50 bytes of overhead, which is why VXLAN interfaces default to MTU 1450.

```text
[Outer Eth 14][Outer IP 20][Outer UDP 8][VXLAN 8][ Inner Eth frame ... ]
               \___________ 50 bytes of overhead ___________/
```

## 1.7 Putting It Together: A Packet's Journey on a LAN

Laptop `192.168.1.50/24` opens `http://192.168.1.10` (a local NAS), then `https://example.com` (remote).

```text
Case 1: Local destination 192.168.1.10
  1. 192.168.1.10 is in 192.168.1.0/24 -> on-link.
  2. ARP "who has 192.168.1.10?" -> reply with NAS MAC.
  3. Frame: src=laptop MAC, dst=NAS MAC; IP: src=.50 dst=.10.
  4. Switch forwards by MAC. No router involved.

Case 2: Remote destination 93.184.215.14
  1. Not in 192.168.1.0/24 -> use default route via 192.168.1.1.
  2. ARP for 192.168.1.1 (the gateway), NOT for the remote IP.
  3. Frame: src=laptop MAC, dst=ROUTER MAC; IP: src=.50 dst=93.184.215.14.
  4. Router strips L2 header, decrements TTL, applies NAT (Part 2),
     builds a new L2 header for its upstream link, forwards.
```

> **Interview:** "What happens when you type a URL into a browser?" is the most common networking interview question. A strong answer walks the layers: DNS resolution (cache, resolver, recursion), route lookup, ARP for the gateway, TCP handshake, TLS handshake, HTTP request, response, and rendering. You will be able to answer every step in depth by the end of this course.

## 1.8 Lab: Build a Virtual LAN with Network Namespaces

You need a Linux machine (a VM or cloud instance is fine) with root access. Network namespaces give each "host" its own interfaces, routes, ARP table, and firewall, exactly like a container (containers *are* namespaces). This lab builds two hosts connected through a bridge.

```text
   ns: red                              ns: blue
  [veth-red 10.10.0.1/24]            [veth-blue 10.10.0.2/24]
          |                                  |
     veth-red-br                        veth-blue-br
          \__________ br-lab (bridge) _______/
```

**Step 1 — Create the namespaces.**

```bash
sudo ip netns add red
sudo ip netns add blue
ip netns list
```

**Step 2 — Create the bridge (our virtual switch).**

```bash
sudo ip link add br-lab type bridge
sudo ip link set br-lab up
```

**Step 3 — Create veth pairs.** A veth pair is a virtual cable: whatever goes in one end comes out the other.

```bash
sudo ip link add veth-red type veth peer name veth-red-br
sudo ip link add veth-blue type veth peer name veth-blue-br
```

**Step 4 — Plug one end into each namespace, the other into the bridge.**

```bash
sudo ip link set veth-red netns red
sudo ip link set veth-blue netns blue
sudo ip link set veth-red-br master br-lab
sudo ip link set veth-blue-br master br-lab
sudo ip link set veth-red-br up
sudo ip link set veth-blue-br up
```

**Step 5 — Assign IP addresses and bring up interfaces inside each namespace.**

```bash
sudo ip -n red addr add 10.10.0.1/24 dev veth-red
sudo ip -n red link set veth-red up
sudo ip -n red link set lo up
sudo ip -n blue addr add 10.10.0.2/24 dev veth-blue
sudo ip -n blue link set veth-blue up
sudo ip -n blue link set lo up
```

**Step 6 — Test and observe ARP.**

```bash
sudo ip netns exec red ping -c 3 10.10.0.2
sudo ip netns exec red ip neigh
bridge fdb show br br-lab | grep -v permanent
```

```text
64 bytes from 10.10.0.2: icmp_seq=1 ttl=64 time=0.071 ms
...
10.10.0.2 dev veth-red lladdr 6e:21:9a:4c:10:f3 REACHABLE
```

> **Note:** If ping fails, the most common cause is the host's firewall filtering bridged traffic (`br_netfilter` loaded with Docker installed sets `net.bridge.bridge-nf-call-iptables=1` and Docker's FORWARD policy is DROP). Run `sudo iptables -I FORWARD -i br-lab -j ACCEPT` for the lab.

**Step 7 — Watch ARP on the wire.** In one terminal run a capture, in another flush the cache and ping again.

```bash
sudo ip netns exec blue tcpdump -n -e -i veth-blue arp or icmp
sudo ip netns exec red ip neigh flush all
sudo ip netns exec red ping -c 1 10.10.0.2
```

```text
12:01:44.101 6e:5b:..:a1 > ff:ff:ff:ff:ff:ff, ethertype ARP (0x0806), length 42: Request who-has 10.10.0.2 tell 10.10.0.1, length 28
12:01:44.101 6e:21:..:f3 > 6e:5b:..:a1, ethertype ARP (0x0806), length 42: Reply 10.10.0.2 is-at 6e:21:9a:4c:10:f3, length 28
12:01:44.101 6e:5b:..:a1 > 6e:21:..:f3, ethertype IPv4 (0x0800), length 98: 10.10.0.1 > 10.10.0.2: ICMP echo request
```

**Step 8 — Break it on purpose.** Change blue's address to `10.10.1.2/24` and ping again from red. Observe the error (`Network is unreachable` from red, because red has no route to 10.10.1.0/24). Then set red's mask to `/16` so it thinks blue is local: now red ARPs, but blue (still /24) has no route back. This asymmetry is exactly what wrong masks do in production.

**Step 9 — Clean up.**

```bash
sudo ip netns del red
sudo ip netns del blue
sudo ip link del br-lab
```

## Key takeaways

- Layers let you isolate problems: test bottom-up (link, IP, port, application).
- Each device reads headers only up to its layer; routers rewrite L2 headers at every hop, while IPs stay constant end-to-end unless NAT intervenes.
- MAC addresses are local to a link; ARP maps IPs to MACs, and for off-subnet destinations a host ARPs for its gateway, not the destination.
- Switches learn MACs from source addresses and flood unknown destinations; Linux bridges are software switches.
- VLANs split broadcast domains (12-bit ID, 4094 usable); VXLAN extends L2 over L3 with a 24-bit VNI and 50 bytes of overhead.
- MTU (1500 by default) minus headers determines MSS (1460); tunnels shrink it and cause hard-to-diagnose bugs.
- Network namespaces plus veth pairs and bridges are the building blocks of container networking.
