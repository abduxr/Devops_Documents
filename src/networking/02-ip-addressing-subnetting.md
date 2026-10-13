# Part 2 — IP Addressing, Subnetting, NAT, and IPv6

If there is one skill in this course you must be able to do quickly and correctly, it is subnetting. You will use it when designing a VPC, sizing a Kubernetes pod CIDR, writing a security group rule, debugging an overlapping-CIDR peering failure, or reading a route table. This Part goes from binary basics to VLSM and route summarization, with worked examples and practice problems you should actually do on paper.

## 2.1 IPv4 Addresses

An IPv4 address is a 32-bit number, written as four 8-bit **octets** in dotted decimal: `192.168.10.77`. Each octet ranges from 0 to 255.

```text
192      .168      .10       .77
11000000  10101000  00001010  01001101
```

### 2.1.1 Binary in two minutes

Each bit position in an octet has a value:

```text
Bit position:  128  64  32  16   8   4   2   1
Example 77:      0   1   0   0   1   1   0   1   = 64 + 8 + 4 + 1 = 77
Example 200:     1   1   0   0   1   0   0   0   = 128 + 64 + 8   = 200
Example 255:     1   1   1   1   1   1   1   1   = 255
```

Memorize these "mask octet" values; they are the only values a subnet mask octet can take:

| Bits set | Binary | Decimal | Block size (256 - value) |
|----------|--------|---------|--------------------------|
| 0 | 00000000 | 0 | 256 |
| 1 | 10000000 | 128 | 128 |
| 2 | 11000000 | 192 | 64 |
| 3 | 11100000 | 224 | 32 |
| 4 | 11110000 | 240 | 16 |
| 5 | 11111000 | 248 | 8 |
| 6 | 11111100 | 252 | 4 |
| 7 | 11111110 | 254 | 2 |
| 8 | 11111111 | 255 | 1 |

### 2.1.2 Network part and host part

Every IP address is split into a **network portion** (identifies the subnet) and a **host portion** (identifies the device within the subnet). The **subnet mask** or **prefix length** says where the split is.

- `/24` means the first 24 bits are network bits. Mask `255.255.255.0`.
- `/26` means 26 network bits. Mask `255.255.255.192`.

In every subnet, two addresses are special:

- **Network address**: all host bits = 0. Identifies the subnet itself; not assignable.
- **Broadcast address**: all host bits = 1. Sent to every host in the subnet; not assignable.

So usable hosts = 2^(host bits) - 2. (Exceptions: `/31` for point-to-point links per RFC 3021, where both addresses are usable, and `/32` for a single host route.)

### 2.1.3 Classful addressing (history only)

Before 1993, addresses were divided into Class A (`/8`, first octet 1–126), B (`/16`, 128–191), C (`/24`, 192–223), D (multicast, 224–239), and E (reserved, 240–255). Classful addressing wasted enormous numbers of addresses and was replaced by **CIDR** (Classless Inter-Domain Routing), where the prefix can be any length. You will still hear "a class C" used to mean "a /24". Know the history, but always think in CIDR.

## 2.2 CIDR Reference Table

| Prefix | Subnet mask | Total addresses | Usable hosts | Wildcard |
|--------|-------------|-----------------|--------------|----------|
| /8 | 255.0.0.0 | 16,777,216 | 16,777,214 | 0.255.255.255 |
| /12 | 255.240.0.0 | 1,048,576 | 1,048,574 | 0.15.255.255 |
| /16 | 255.255.0.0 | 65,536 | 65,534 | 0.0.255.255 |
| /17 | 255.255.128.0 | 32,768 | 32,766 | 0.0.127.255 |
| /18 | 255.255.192.0 | 16,384 | 16,382 | 0.0.63.255 |
| /19 | 255.255.224.0 | 8,192 | 8,190 | 0.0.31.255 |
| /20 | 255.255.240.0 | 4,096 | 4,094 | 0.0.15.255 |
| /21 | 255.255.248.0 | 2,048 | 2,046 | 0.0.7.255 |
| /22 | 255.255.252.0 | 1,024 | 1,022 | 0.0.3.255 |
| /23 | 255.255.254.0 | 512 | 510 | 0.0.1.255 |
| /24 | 255.255.255.0 | 256 | 254 | 0.0.0.255 |
| /25 | 255.255.255.128 | 128 | 126 | 0.0.0.127 |
| /26 | 255.255.255.192 | 64 | 62 | 0.0.0.63 |
| /27 | 255.255.255.224 | 32 | 30 | 0.0.0.31 |
| /28 | 255.255.255.240 | 16 | 14 | 0.0.0.15 |
| /29 | 255.255.255.248 | 8 | 6 | 0.0.0.7 |
| /30 | 255.255.255.252 | 4 | 2 | 0.0.0.3 |
| /31 | 255.255.255.254 | 2 | 2 (point-to-point) | 0.0.0.1 |
| /32 | 255.255.255.255 | 1 | 1 (host route) | 0.0.0.0 |

> **Tip:** Each step of one bit in the prefix halves (or doubles) the size. A /24 is two /25s, four /26s, eight /27s. A /16 is 256 /24s. Anchor on "/24 = 256" and count bits from there.

> **Note:** AWS reserves **5** addresses in every subnet (network, +1 VPC router, +2 DNS, +3 reserved for future use, and the last address). A /24 subnet in AWS gives 251 usable IPs, a /28 (the smallest allowed) gives 11. Azure also reserves 5. GCP reserves 4.

## 2.3 The Fast Method: Block Sizes

You do not need to convert everything to binary. Use the **block size** (also called the "magic number") method:

1. Find the **interesting octet**: the octet where the mask is neither 255 nor 0.
2. Block size = 256 - (mask value in that octet).
3. Subnets start at multiples of the block size in that octet.
4. The network address is the largest multiple of the block size that is less than or equal to the IP's value in that octet.
5. Broadcast = next network address - 1.

### 2.3.1 Worked example 1: 192.168.10.77/26

```text
Mask /26 = 255.255.255.192         interesting octet = 4th, mask value = 192
Block size = 256 - 192 = 64        subnets: .0, .64, .128, .192
77 falls in the block starting at 64

Network:     192.168.10.64
First host:  192.168.10.65
Last host:   192.168.10.126
Broadcast:   192.168.10.127
Usable:      2^6 - 2 = 62
```

Verify in binary:

```text
IP      77  = 01001101
Mask   192  = 11000000
AND         = 01000000 = 64   -> network .64
Host bits all 1: 01111111 = 127 -> broadcast .127
```

### 2.3.2 Worked example 2: 10.20.30.77/27

```text
Mask /27 = 255.255.255.224         block = 256 - 224 = 32
Blocks: 0, 32, 64, 96, ...         77 is in [64, 95]

Network:    10.20.30.64
Hosts:      10.20.30.65 - 10.20.30.94
Broadcast:  10.20.30.95
Usable:     30
```

### 2.3.3 Worked example 3: 172.16.45.200/20

Here the interesting octet is the **third**.

```text
Mask /20 = 255.255.240.0           interesting octet = 3rd, value 240
Block = 256 - 240 = 16             third-octet blocks: 0,16,32,48,...
45 falls in [32, 47]

Network:    172.16.32.0
First host: 172.16.32.1
Last host:  172.16.47.254
Broadcast:  172.16.47.255
Usable:     2^12 - 2 = 4,094
```

Note that `172.16.40.0` and `172.16.45.255` are perfectly normal host addresses inside this /20. Only the very first and very last addresses of the whole block are special.

### 2.3.4 Worked example 4: 10.200.37.5/13

```text
Mask /13 = 255.248.0.0             interesting octet = 2nd, value 248
Block = 256 - 248 = 8              second-octet blocks: 0,8,...,192,200,208
200 is itself a multiple of 8 -> block [200, 207]

Network:    10.200.0.0
Broadcast:  10.207.255.255
Usable:     2^19 - 2 = 524,286
```

### 2.3.5 Worked example 5: Is 10.1.5.130 inside 10.1.4.0/23?

```text
/23 = 255.255.254.0, block 2 in the 3rd octet: 0,2,4,6,...
10.1.4.0/23 spans 10.1.4.0 - 10.1.5.255
10.1.5.130 is within that range -> YES
```

This is exactly the question you answer when reading a security group rule like "allow 10.1.4.0/23".

## 2.4 Dividing Networks: Subnetting and VLSM

### 2.4.1 Equal-size subnets

To split a network into 2^n equal subnets, borrow n bits.

**Example: split 10.0.0.0/16 into /20s.** Borrowing 4 bits gives 2^4 = 16 subnets of 4,096 addresses each. The third octet increments by 16:

```text
10.0.0.0/20     10.0.64.0/20    10.0.128.0/20   10.0.192.0/20
10.0.16.0/20    10.0.80.0/20    10.0.144.0/20   10.0.208.0/20
10.0.32.0/20    10.0.96.0/20    10.0.160.0/20   10.0.224.0/20
10.0.48.0/20    10.0.112.0/20   10.0.176.0/20   10.0.240.0/20
```

**Example: split 192.168.0.0/24 into at least 6 subnets.** 2^2 = 4 is too few, 2^3 = 8 is enough, so borrow 3 bits: /27, eight subnets of 30 usable hosts: `.0, .32, .64, .96, .128, .160, .192, .224`.

### 2.4.2 VLSM: different sizes from one block

**VLSM** (Variable Length Subnet Masking) carves different-sized subnets from one block. The rule: **allocate the largest subnet first**, each aligned on its own block boundary. This prevents fragmentation and overlap.

**Requirement:** from 192.168.1.0/24 allocate a 100-host LAN, a 50-host LAN, a 20-host LAN, and two point-to-point router links.

| Need | Smallest fit | Subnet | Range | Usable |
|------|--------------|--------|-------|--------|
| 100 hosts | /25 (126) | 192.168.1.0/25 | .0 – .127 | 126 |
| 50 hosts | /26 (62) | 192.168.1.128/26 | .128 – .191 | 62 |
| 20 hosts | /27 (30) | 192.168.1.192/27 | .192 – .223 | 30 |
| Link 1 | /30 (2) | 192.168.1.224/30 | .224 – .227 | 2 |
| Link 2 | /30 (2) | 192.168.1.228/30 | .228 – .231 | 2 |
| Free | | 192.168.1.232/29, .240/28 | .232 – .255 | spare |

> **Warning:** A subnet must start on a multiple of its own size. `192.168.1.100/26` is not a valid network address (100 is not a multiple of 64); the network that contains .100 is `192.168.1.64/26`. Tools like Terraform will error on misaligned CIDRs, and some will silently "correct" them, which is worse.

### 2.4.3 A realistic VPC plan

You are given `10.0.0.0/16` for a production VPC spanning three availability zones. Plan for growth: Kubernetes nodes in private subnets need many IPs (with AWS VPC CNI, every pod gets a VPC IP).

```text
VPC 10.0.0.0/16 (65,536 addresses)

Private (app/EKS)  10.0.0.0/20   AZ-a   4,096 addresses
                   10.0.16.0/20  AZ-b
                   10.0.32.0/20  AZ-c
Public (ALB/NAT)   10.0.48.0/24  AZ-a     256 addresses
                   10.0.49.0/24  AZ-b
                   10.0.50.0/24  AZ-c
Database           10.0.64.0/24  AZ-a
                   10.0.65.0/24  AZ-b
                   10.0.66.0/24  AZ-c
Reserved for future 10.0.128.0/17 (half the VPC kept free)
```

Why this shape: public subnets only hold load balancers and NAT gateways (small), private subnets hold workloads (large), and leaving half the space free means you can add a fourth AZ or a new tier without re-IPing.

## 2.5 Route Summarization (Supernetting)

Summarization combines contiguous networks into one shorter prefix, which keeps route tables small and lets one firewall rule cover many subnets.

**Example: summarize 192.168.16.0/24 through 192.168.19.0/24.**

```text
16 = 000100 00
17 = 000100 01
18 = 000100 10
19 = 000100 11
     ^^^^^^ first 6 bits of the 3rd octet are common
16 + 6 = 22 common bits -> 192.168.16.0/22
Check: /22 block is 4 in the 3rd octet; 16 is a multiple of 4. Range 16.0 - 19.255.
```

**Example: summarize 10.4.0.0/16 through 10.7.0.0/16.** Four /16s, block 4 in the second octet, 4 is a multiple of 4: `10.4.0.0/14`.

**Counter-example:** 192.168.17.0/24 through 192.168.20.0/24 cannot be summarized into a single /22, because 17 is not a multiple of 4. The smallest single prefix covering them is 192.168.16.0/21, which also includes 16 and 21–23. That over-coverage might be fine for a route but dangerous for a firewall "allow" rule.

## 2.6 Subnetting Practice Problems

Work these on paper before checking the answers.

1. Find network, broadcast, host range, and usable count for `192.168.5.130/26`.
2. Same for `10.10.10.10/29`.
3. Same for `172.31.100.5/18`.
4. How many /26 subnets fit in a /22?
5. What is the smallest prefix that holds 500 hosts (non-cloud)?
6. What is the dotted-decimal mask for /21?
7. Is `10.0.15.255` a usable host address in `10.0.8.0/21`?
8. Summarize `10.4.0.0/16`, `10.5.0.0/16`, `10.6.0.0/16`, `10.7.0.0/16`.
9. Do `10.0.0.0/16` and `10.0.128.0/17` overlap?
10. Find network, broadcast and host range for `192.168.100.33/28`.
11. What is the wildcard mask for /20?
12. How many /64 subnets are in an IPv6 /48?
13. From `10.50.0.0/16` you need subnets with at least 1,000 hosts each. What prefix, and how many subnets?
14. What is the 5th /26 subnet in `172.20.0.0/22` (counting the first as the 1st)?
15. How many usable IPs does an AWS subnet of /27 provide?
16. A host is `10.8.3.7` with mask `255.255.252.0`. What is its network in CIDR notation?

### 2.6.1 Answers

| # | Answer | Working |
|---|--------|---------|
| 1 | Net 192.168.5.128, bcast .191, hosts .129–.190, 62 | Block 64; 130 in [128,191] |
| 2 | Net 10.10.10.8, bcast .15, hosts .9–.14, 6 | Block 8; 10 in [8,15] |
| 3 | Net 172.31.64.0, bcast 172.31.127.255, hosts 172.31.64.1–172.31.127.254, 16,382 | /18 = 255.255.192.0, block 64 in 3rd octet; 100 in [64,127] |
| 4 | 16 | 2^(26-22) = 16 |
| 5 | /23 | /23 = 510 usable; /24 = 254 is too small |
| 6 | 255.255.248.0 | 5 bits in 3rd octet = 248 |
| 7 | No, it is the broadcast | /21 block 8: 10.0.8.0–10.0.15.255 |
| 8 | 10.4.0.0/14 | 4 contiguous /16s, aligned on 4 |
| 9 | Yes | 10.0.128.0/17 is the upper half of 10.0.0.0/16 |
| 10 | Net .32, bcast .47, hosts .33–.46 (14) | Block 16; 33 in [32,47] |
| 11 | 0.0.15.255 | 255.255.255.255 - 255.255.240.0 |
| 12 | 65,536 | 2^(64-48) = 2^16 |
| 13 | /22, 64 subnets | /22 = 1,022 usable; 2^(22-16) = 64 |
| 14 | 172.20.1.0/26 | .0.0, .0.64, .0.128, .0.192, then .1.0 |
| 15 | 27 | 32 - 5 reserved |
| 16 | 10.8.0.0/22 | /22 block 4 in 3rd octet; 3 in [0,3] |

### 2.6.2 Let a tool check you, but know the math

```bash
python3 -c "import ipaddress as i; n=i.ip_interface('172.31.100.5/18').network; print(n, n.broadcast_address, n.num_addresses)"
```

```text
172.31.64.0/18 172.31.127.255 16384
```

```bash
# Overlap check, useful before VPC peering
python3 -c "import ipaddress as i; print(i.ip_network('10.0.0.0/16').overlaps(i.ip_network('10.0.128.0/17')))"
# Split into subnets
python3 -c "import ipaddress as i; [print(s) for s in i.ip_network('10.0.0.0/22').subnets(new_prefix=24)]"
```

```text
True
10.0.0.0/24
10.0.1.0/24
10.0.2.0/24
10.0.3.0/24
```

Terraform has the same capability built in, which you will use in Part 7:

```hcl
# cidrsubnet(prefix, newbits, netnum)
cidrsubnet("10.0.0.0/16", 4, 2)   # => "10.0.32.0/20"
cidrsubnet("10.0.0.0/16", 8, 48)  # => "10.0.48.0/24"
```

## 2.7 Special and Private Address Ranges

| Range | Purpose | Notes |
|-------|---------|-------|
| 10.0.0.0/8 | Private (RFC 1918) | 16.7M addresses, typical for VPCs |
| 172.16.0.0/12 | Private (RFC 1918) | 172.16.0.0 – 172.31.255.255 |
| 192.168.0.0/16 | Private (RFC 1918) | Home and small office |
| 100.64.0.0/10 | Carrier-grade NAT (RFC 6598) | Also used by Tailscale and as EKS secondary CIDR |
| 127.0.0.0/8 | Loopback | Never leaves the host |
| 169.254.0.0/16 | Link-local | Cloud metadata at 169.254.169.254 |
| 0.0.0.0/8 | "This network" | 0.0.0.0 as bind address means "all interfaces" |
| 224.0.0.0/4 | Multicast | |
| 240.0.0.0/4 | Reserved | 255.255.255.255 is limited broadcast |
| 192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24 | Documentation (TEST-NET) | Safe to use in examples |
| 198.18.0.0/15 | Benchmarking | |

> **Warning:** Docker's default bridge uses `172.17.0.0/16` and new networks take further ranges from 172.17–172.31 and 192.168.x. If your corporate VPN or a peered VPC uses 172.16.0.0/12, containers may become unable to reach those networks because the host now has a more specific local route. Set `default-address-pools` in `/etc/docker/daemon.json` to a range you know is unused.

> **Tip:** Plan non-overlapping CIDRs across *all* environments and accounts from day one (an IPAM spreadsheet or AWS VPC IPAM). Overlapping VPC CIDRs cannot be peered or attached to the same Transit Gateway route table without NAT tricks. Re-IPing a production VPC is one of the most expensive mistakes in cloud networking.

## 2.8 NAT and PAT

### 2.8.1 Why NAT exists

There are only about 4.3 billion IPv4 addresses, and they ran out at the regional registries between 2011 and 2019. **NAT** (Network Address Translation) lets many private hosts share one or a few public addresses. It is also used to connect networks with overlapping address spaces.

### 2.8.2 Types of NAT

| Type | What it rewrites | Example |
|------|------------------|---------|
| SNAT (Source NAT) | Source IP on outbound | Private instance reaches the Internet via NAT gateway |
| PAT / NAPT / Masquerade | Source IP **and** source port, many-to-one | Home router, AWS NAT gateway, Docker MASQUERADE |
| DNAT (Destination NAT) | Destination IP/port on inbound | Port forwarding, `docker run -p 8080:80`, Kubernetes Services |
| 1:1 static NAT | One public IP maps to one private IP | AWS Elastic IP on an instance (done by the IGW) |

### 2.8.3 How PAT works

```text
 Private side                     NAT device                       Internet
 10.0.1.10:51000 --+         public IP 203.0.113.5
                   |---> [translation table] ----> 203.0.113.5:40001 -> 93.184.215.14:443
 10.0.1.11:51000 --+                         ----> 203.0.113.5:40002 -> 93.184.215.14:443

 Translation table (conntrack):
 inside src           outside src          destination
 10.0.1.10:51000  ->  203.0.113.5:40001    93.184.215.14:443
 10.0.1.11:51000  ->  203.0.113.5:40002    93.184.215.14:443
```

The reply comes back to `203.0.113.5:40001`; the NAT device looks up the table and rewrites the destination back to `10.0.1.10:51000`. Key consequences:

- **Inbound connections cannot be initiated** to hosts behind PAT unless you configure DNAT. That is an accidental security property, not a firewall.
- **State is required.** If the NAT device restarts or the entry times out, long-idle connections silently die. This is why idle database connections through a NAT gateway break after 350 seconds on AWS (the NAT gateway idle timeout). Use TCP keepalives shorter than that.
- **Port exhaustion.** Each public IP has about 64,000 source ports per destination IP and port. An AWS NAT gateway supports about 55,000 simultaneous connections to each unique destination per public IP; services that hammer one external API can hit `ErrorPortAllocation`. Fix by adding IPs to the NAT gateway, using connection pooling, or using VPC endpoints for AWS services.

On Linux, NAT is implemented by netfilter and tracked by conntrack:

```bash
# Masquerade traffic leaving eth0 from the 10.10.0.0/24 network
sudo iptables -t nat -A POSTROUTING -s 10.10.0.0/24 -o eth0 -j MASQUERADE
# Port-forward host port 8080 to an internal host
sudo iptables -t nat -A PREROUTING -p tcp --dport 8080 -j DNAT --to-destination 10.10.0.2:80
# Inspect tracked connections
sudo conntrack -L -p tcp --dport 443 | head
```

## 2.9 IPv6 Essentials

### 2.9.1 Why you need to know it

IPv6 is no longer optional: mobile networks are majority IPv6, AWS charges for every public IPv4 address (since February 2024), and IPv6-only subnets and EKS IPv6 clusters remove IP exhaustion problems for pods. Every DevOps engineer should be able to read and configure it.

### 2.9.2 Address format

An IPv6 address is 128 bits written as eight groups of four hex digits:

```text
Full:        2001:0db8:0000:0000:0000:ff00:0042:8329
Rule 1 - drop leading zeros in each group:   2001:db8:0:0:0:ff00:42:8329
Rule 2 - replace ONE run of all-zero groups with "::"  2001:db8::ff00:42:8329

Loopback:    ::1
Unspecified: ::
```

`::` can appear only once in an address; otherwise it would be ambiguous. In URLs, wrap IPv6 in brackets: `http://[2001:db8::1]:8080/`.

### 2.9.3 Address types

| Prefix | Type | Analogy in IPv4 |
|--------|------|-----------------|
| 2000::/3 | Global unicast (GUA) | Public addresses |
| fc00::/7 (fd00::/8 in practice) | Unique local (ULA) | RFC 1918 private |
| fe80::/10 | Link-local, every interface has one | 169.254.0.0/16 |
| ff00::/8 | Multicast (no broadcast in IPv6) | 224.0.0.0/4 |
| ::1/128 | Loopback | 127.0.0.1 |
| 64:ff9b::/96 | NAT64 well-known prefix | — |

### 2.9.4 Subnetting IPv6

IPv6 subnetting is simpler because you never run out of host bits:

- A **/64** is the standard subnet size for any LAN (required by SLAAC). It holds 2^64 addresses.
- Sites typically receive a **/48** (65,536 /64s) or **/56** (256 /64s).
- AWS assigns a VPC a /56 and each subnet a /64.

Subnetting happens on nibble (4-bit, one hex digit) boundaries for readability: from `2001:db8:abcd::/48`, subnets are `2001:db8:abcd:0000::/64`, `2001:db8:abcd:0001::/64`, ... `2001:db8:abcd:ffff::/64`.

### 2.9.5 NDP, SLAAC, and DHCPv6

IPv6 replaces ARP and broadcast with **Neighbor Discovery Protocol** (ICMPv6):

| Message | Purpose |
|---------|---------|
| Router Solicitation (RS) | Host asks "any routers here?" |
| Router Advertisement (RA) | Router announces prefix, default gateway, flags |
| Neighbor Solicitation (NS) | Equivalent of ARP request (sent to a solicited-node multicast address) |
| Neighbor Advertisement (NA) | Equivalent of ARP reply |
| Redirect | Better next hop available |

- **SLAAC** (Stateless Address Autoconfiguration): the host takes the /64 prefix from the RA and generates its own interface ID (randomized for privacy on clients).
- **DHCPv6**: stateful assignment, used when you need central control. RA flags tell hosts which method to use.

> **Warning:** Never blanket-block ICMPv6. NDP, router discovery, and Path MTU Discovery all depend on it. In IPv6, routers do not fragment packets, so a blocked "Packet Too Big" message causes connections to hang.

### 2.9.6 IPv6 and NAT

IPv6 is designed for end-to-end addressing, so NAT is generally not used. Security comes from stateful firewalls instead. AWS provides an **egress-only Internet gateway** that allows outbound IPv6 connections but blocks inbound, giving the same effect as a NAT gateway without translation. **NAT64 + DNS64** let IPv6-only workloads reach IPv4-only services.

```bash
ip -6 addr show dev ens5
ip -6 route show
ping -6 -c 2 ipv6.google.com
```

```text
2: ens5: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 9001 state UP qlen 1000
    inet6 2600:1f18:4b2:9e00:7c1a:3a5f:91b2:4e10/128 scope global dynamic noprefixroute
       valid_lft 447sec preferred_lft 137sec
    inet6 fe80::8ff:a1ff:fe2c:7d11/64 scope link
       valid_lft forever preferred_lft forever
```

## 2.10 Lab: Subnet Calculator Drills and NAT in Namespaces

**Part A — Calculator drills.**

**Step 1.** Write this small helper to `subnet.py`:

```bash
cat > subnet.py <<'EOF'
import ipaddress, sys
iface = ipaddress.ip_interface(sys.argv[1])
n = iface.network
hosts = list(n.hosts()) if n.num_addresses <= 65536 else None
print(f"network   {n.network_address}/{n.prefixlen}")
print(f"netmask   {n.netmask}   wildcard {n.hostmask}")
print(f"broadcast {n.broadcast_address}")
if hosts:
    print(f"hosts     {hosts[0]} - {hosts[-1]} ({len(hosts)} usable)")
else:
    print(f"usable    {n.num_addresses - 2}")
EOF
```

**Step 2.** Solve each practice problem by hand, then check:

```bash
python3 subnet.py 192.168.100.33/28
```

```text
network   192.168.100.32/28
netmask   255.255.255.240   wildcard 0.0.0.15
broadcast 192.168.100.47
hosts     192.168.100.33 - 192.168.100.46 (14 usable)
```

**Part B — Build a NAT router with namespaces.**

```text
  ns: lan (10.10.0.2/24) --veth-- ns: router (10.10.0.1 | 192.0.2.1) --veth-- ns: wan (192.0.2.2/24)
```

**Step 3.** Create namespaces and links:

```bash
for ns in lan router wan; do sudo ip netns add $ns; done
sudo ip link add v-lan type veth peer name v-rlan
sudo ip link add v-wan type veth peer name v-rwan
sudo ip link set v-lan netns lan;  sudo ip link set v-rlan netns router
sudo ip link set v-wan netns wan;  sudo ip link set v-rwan netns router
sudo ip -n lan addr add 10.10.0.2/24 dev v-lan
sudo ip -n router addr add 10.10.0.1/24 dev v-rlan
sudo ip -n router addr add 192.0.2.1/24 dev v-rwan
sudo ip -n wan addr add 192.0.2.2/24 dev v-wan
for pair in "lan v-lan" "router v-rlan" "router v-rwan" "wan v-wan"; do
  set -- $pair; sudo ip -n $1 link set $2 up; sudo ip -n $1 link set lo up
done
```

**Step 4.** Give the LAN host a default route, enable forwarding on the router:

```bash
sudo ip -n lan route add default via 10.10.0.1
sudo ip netns exec router sysctl -w net.ipv4.ip_forward=1
```

**Step 5.** Ping from LAN to WAN. It fails: the `wan` namespace has no route back to 10.10.0.0/24 (just like the real Internet has no route to your private network).

```bash
sudo ip netns exec lan ping -c 2 -W 1 192.0.2.2
```

**Step 6.** Add masquerading on the router and retry:

```bash
sudo ip netns exec router iptables -t nat -A POSTROUTING -o v-rwan -j MASQUERADE
sudo ip netns exec lan ping -c 2 192.0.2.2
sudo ip netns exec wan tcpdump -n -i v-wan icmp -c 2
```

```text
13:10:02.551 IP 192.0.2.1 > 192.0.2.2: ICMP echo request, id 4121, seq 1, length 64
13:10:02.551 IP 192.0.2.2 > 192.0.2.1: ICMP echo reply, id 4121, seq 1, length 64
```

The WAN side sees only the router's address. Inspect the translation with `sudo ip netns exec router conntrack -L` (install `conntrack-tools` if needed).

**Step 7.** Clean up: `for ns in lan router wan; do sudo ip netns del $ns; done`.

## Key takeaways

- An IP address splits into network and host bits; the prefix length defines the split. Usable hosts = 2^host-bits - 2 (except /31 and /32).
- Use block size (256 - mask octet) to find network and broadcast addresses quickly; verify with binary when unsure.
- VLSM: allocate largest subnets first, always aligned to their own size.
- Summarization requires contiguous, aligned blocks; over-summarizing in firewall rules grants more access than intended.
- Plan non-overlapping CIDRs across every VPC, VPN, and container network before you build anything. AWS reserves 5 IPs per subnet.
- PAT lets many private hosts share one public IP via a stateful port mapping table; watch for idle timeouts and port exhaustion.
- IPv6 uses /64 subnets, NDP instead of ARP, SLAAC/DHCPv6, and firewalls instead of NAT. Never block ICMPv6 wholesale.
