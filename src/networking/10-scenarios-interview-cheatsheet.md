# Part 10 — Real-World Scenarios, Interview Q&A, and Cheat Sheet

This final Part ties the course together. The scenarios are composites of real incidents; each walkthrough shows the questions to ask, the commands to run, and how the evidence narrows the cause. Then comes an interview question bank with answers and a dense reference sheet to keep next to your terminal.

## 10.1 Scenario: "The Site Is Slow"

**Report:** Users say the web app has been slow since this morning. Dashboards show p99 latency up from 300 ms to 2.5 s. No obvious errors.

**Step 1 — Define and scope.** Slow for whom? All users or one region? All endpoints or some? Since when, and what changed? Suppose: all regions, mostly API calls, started around 09:10, and a deploy went out at 09:05.

**Step 2 — Break the request into phases** from a client close to users:

```bash
for i in $(seq 5); do
  curl -o /dev/null -s -w 'dns=%{time_namelookup} tcp=%{time_connect} tls=%{time_appconnect} ttfb=%{time_starttransfer} total=%{time_total}\n' \
  https://shop.example.com/api/products
done
```

```text
dns=0.004 tcp=0.021 tls=0.058 ttfb=2.310 total=2.312
dns=0.003 tcp=0.020 tls=0.055 ttfb=0.290 total=0.291
dns=0.004 tcp=0.022 tls=0.057 ttfb=2.402 total=2.405
```

DNS, TCP, and TLS are fast and stable. Time-to-first-byte is bimodal: some requests are normal, some take more than 2 s. The network to the edge is fine; the delay is behind the load balancer.

**Step 3 — Follow the hops.** Load balancer metrics show `TargetResponseTime` high, so the targets are slow. Are all targets slow or some? Per-target metrics or access logs (`upstream_addr` and `upstream_response_time` in Nginx) show requests to two of six pods are slow.

**Step 4 — Look at the slow pods.** What do they depend on? Their traces show a 2 s gap on a call to an external pricing API. From inside a slow pod:

```bash
kubectl exec -it api-7f9c-abcde -- sh -c 'time getent ahosts pricing.partner.com'
```

```text
real    0m2.012s
```

DNS resolution inside the pod takes 2 s. On the fast pods it takes 3 ms. The two slow pods are on the same node. On that node, the NodeLocal DNSCache pod was evicted during the 09:05 deploy (memory pressure), and the glibc resolver is timing out on the dead local address before retrying the next server.

**Root cause and fix:** DNS cache eviction on one node. Restore the DNS cache DaemonSet with proper resource requests and a priority class; reduce lookup pressure (lower `ndots`, use FQDNs); add a DNS latency SLO and alert.

**Lessons:** Measure phases before guessing. Bimodal latency usually means some instances, connections, or paths differ. Slowness is often a dependency, and DNS timeouts manifest as round numbers (1 s, 2 s, 5 s).

## 10.2 Scenario: "Can't Reach the Database from a Pod"

**Report:** A new service in namespace `orders` cannot connect to the PostgreSQL RDS instance. The error: `connection to server at "orders-db.abc123.us-east-1.rds.amazonaws.com" (10.0.64.25), port 5432 failed: Connection timed out`.

**Step 1 — Read the error.** It resolved the name (DNS works) and got an IP; the connection **timed out**, not refused. Something is dropping packets: security group, NACL, NetworkPolicy, routing.

**Step 2 — Reproduce from the pod's network namespace:**

```bash
kubectl -n orders debug -it orders-api-6d4b-xyz --image=nicolaka/netshoot --target=app -- bash
nc -zv -w 3 10.0.64.25 5432          # timed out
ip route get 10.0.64.25              # via the node/CNI, as expected
```

**Step 3 — Compare with a working path.** Another service in namespace `payments` reaches the same database. Test from the node hosting the failing pod:

```bash
nc -zv -w 3 10.0.64.25 5432          # from the node: succeeded
```

The node can reach the DB; the pod cannot. That points to something pod-specific: NetworkPolicy, security groups for pods, or SNAT behavior.

**Step 4 — Check NetworkPolicy:**

```bash
kubectl -n orders get networkpolicy
```

```text
NAME           POD-SELECTOR   AGE
default-deny   <none>         12d
allow-dns      <none>         12d
```

There is a default-deny egress policy in `orders` and only DNS is allowed. The `payments` namespace has an explicit egress rule to `10.0.64.0/22:5432`; `orders` does not.

**Fix:** add an egress policy allowing TCP 5432 to the database subnet CIDRs for the `orders-api` pods. Verify with `nc`, then the application.

**Other causes to check in this scenario** (in order of likelihood):

| Check | How |
|-------|-----|
| DB security group allows the source | With the AWS VPC CNI, traffic to destinations inside the VPC keeps the **pod IP** as source, and the ENI carries the node's security groups (or the pod's own, with security groups for pods); allow that SG or the pod subnet CIDRs |
| NACLs on DB subnets allow return traffic | Ephemeral ports outbound from the DB subnet |
| Route from pod CIDR to DB | Overlay CNIs SNAT pod traffic to the node IP; with custom networking (100.64.0.0/10 pods) the DB must allow that CIDR or traffic must be SNATed |
| DB listening/accepting | RDS status, `max_connections`, `pg_hba.conf` (gives an auth error, not a timeout) |
| Wrong endpoint | Reader vs writer endpoint, a different region or VPC |

**Lesson:** "Works from the node, fails from the pod" localizes the problem to pod-level controls in one test.

## 10.3 Scenario: "Intermittent 502s"

**Report:** About 0.5% of requests through the ALB return 502 Bad Gateway. No pattern by endpoint. Application logs show no errors for those requests; it is as if they never arrived.

**Step 1 — Who generates the 502?** The ALB access logs show `elb_status_code=502` and `target_status_code=-`. The target never returned a status: the ALB's connection to the target failed or was reset.

**Step 2 — Look for timing patterns.** The errors cluster on low-traffic paths and after idle periods. Correlate with connection reuse: the ALB keeps idle keep-alive connections to targets and reuses them.

**Step 3 — Check timeouts at each hop:**

```text
ALB idle timeout:                 60 s
Target: Node.js server.keepAliveTimeout   5 s   (default)
```

The target closes idle connections after 5 s. If the ALB sends a new request on a connection at the instant the target closes it, the target responds with a FIN or RST and the ALB returns 502. Capture on a target to confirm:

```bash
sudo tcpdump -ni eth0 'tcp port 8080 and (tcp[tcpflags] & (tcp-fin|tcp-rst) != 0)'
```

```text
11:02:15.501 IP 10.0.1.40.8080 > 10.0.48.12.31544: Flags [F.], seq 9381, ack 1202
11:02:15.502 IP 10.0.48.12.31544 > 10.0.1.40.8080: Flags [P.], seq 1202:1789, ack 9382  <- request on closing conn
11:02:15.502 IP 10.0.1.40.8080 > 10.0.48.12.31544: Flags [R], seq 9382
```

**Fix:** set the target's keep-alive timeout **greater** than the ALB idle timeout (e.g., `server.keepAliveTimeout = 65000` and `headersTimeout = 66000` in Node.js; `keepalive_timeout 75s` in Nginx; `--timeout-keep-alive` in Uvicorn).

**Other common causes of intermittent 502s:**

| Cause | Evidence |
|-------|----------|
| Pods terminated before LB deregistration | 502s spike during deploys; fix with `preStop` sleep, readiness gates, graceful SIGTERM handling |
| Target crashes or OOM kills | Restart counts, `OOMKilled` |
| Response headers too large for proxy buffers | Nginx `upstream sent too big header`; raise `proxy_buffer_size` |
| Protocol mismatch | LB speaks HTTPS to an HTTP target, or HTTP/2 to an HTTP/1-only target |
| PROXY protocol enabled on one side only | Every request fails, not 0.5% |
| Conntrack table full on nodes | `dmesg` shows `nf_conntrack: table full` |

**Lesson:** a 502 means "the proxy's upstream misbehaved". Find which proxy, read its upstream error, and check timeout ordering first.

## 10.4 Scenario: "DNS Works on One Host but Not Another"

**Report:** `curl https://internal-api.corp.example.com` works on `build-01` but fails on `build-02` with `Could not resolve host`. Both are in the same subnet.

**Step 1 — Compare what each host's application stack sees:**

```bash
# on both hosts
getent ahosts internal-api.corp.example.com
cat /etc/resolv.conf
resolvectl status | sed -n '1,25p'
grep hosts /etc/nsswitch.conf
grep -i corp /etc/hosts
```

Findings:

```text
build-01:  /etc/resolv.conf -> nameserver 127.0.0.53, search corp.example.com
           resolvectl: DNS Servers 10.0.0.2, plus per-link route ~corp.example.com -> 10.50.0.53 (VPN)
           /etc/hosts: (no entry)
build-02:  /etc/resolv.conf -> nameserver 10.0.0.2  (static file, not systemd-resolved)
```

**Step 2 — Query each resolver directly:**

```bash
dig @10.0.0.2 internal-api.corp.example.com +short      # empty, NXDOMAIN
dig @10.50.0.53 internal-api.corp.example.com +short    # 10.50.12.7
```

The VPC resolver does not know the corporate zone. `build-01` works because systemd-resolved has a split-DNS route sending `corp.example.com` to the on-prem DNS server over the VPN interface. `build-02` was configured by a provisioning script that overwrote `/etc/resolv.conf` with a static file, bypassing systemd-resolved.

**Fix options:**

1. Restore `/etc/resolv.conf` as a symlink to `/run/systemd/resolve/stub-resolv.conf` on `build-02`.
2. Better, make it host-independent: create a Route 53 Resolver **outbound endpoint** with a forwarding rule for `corp.example.com` to `10.50.0.53`, associated with the VPC. Now every host using the VPC resolver resolves corporate names.

**Other causes for "one host works, one does not":** an `/etc/hosts` override on one host, different search domains (short names), IPv6 preference (AAAA answered but no IPv6 route), a local caching daemon with a stale entry (`resolvectl flush-caches`), a container with a different resolv.conf than its host, or DNS traffic blocked from one host's security group or NetworkPolicy (UDP and TCP 53).

**Lesson:** `dig` asks a server; `getent` shows what the app sees. Compare both on the working and failing hosts, and diff the resolver configuration.

## 10.5 Scenario: "New VPC Peering, Nothing Flows"

**Report:** Peering `pcx-0a1` between `vpc-app` (10.0.0.0/16) and `vpc-data` (10.20.0.0/16) is `Active`, but the app cannot reach `10.20.5.10:6379`.

**Walkthrough:**

```text
1. Active peering only means the link exists. Check routes on BOTH sides:
   vpc-app route tables for the app subnets:   10.20.0.0/16 -> pcx-0a1 ?   (present)
   vpc-data route table for the Redis subnet:  10.0.0.0/16  -> pcx-0a1 ?   (MISSING)
2. Requests arrive at Redis; replies have no route back -> timeout.
3. Also verify: Redis SG allows 6379 from 10.0.0.0/16 (or the peer SG id),
   NACLs, and that the app uses the private DNS name with
   "allow DNS resolution from accepter VPC" enabled if it resolves via public names.
```

**Fix:** add the return route in `vpc-data`. Reachability Analyzer would have reported "no route to destination" at the `vpc-data` route table. **Lesson:** routing is per direction (Part 3), and cloud consoles showing "Active" or "Available" prove configuration exists, not that traffic flows.

## 10.6 Scenario: "Outbound Calls Sporadically Time Out from Private Subnets"

**Report:** Workers calling a third-party API from private subnets see sporadic connect timeouts at peak hours. Other egress is fine.

**Walkthrough:** NAT gateway CloudWatch metric `ErrorPortAllocation` is non-zero at peak and `ActiveConnectionCount` is high. All workers hit one destination IP:port through one NAT gateway public IP, exceeding about 55,000 simultaneous connections to that destination. Workers create a new HTTPS connection per request and close it.

**Fix:** enable connection pooling and keep-alive in the HTTP client (biggest win), add secondary public IPs to the NAT gateway, and spread egress across per-AZ NAT gateways. **Lesson:** NAT is stateful and finite. TIME_WAIT and ephemeral port math from Part 3 apply to every middlebox.

## 10.7 Capstone Lab: Run an Incident

**Step 1.** Rebuild the Part 8 kind cluster with Calico and the `shop` app, and the Part 6 HAProxy lab.

**Step 2.** Ask a colleague to inject one fault without telling you which: a default-deny NetworkPolicy without DNS egress; a CoreDNS deployment scaled to 0; a Service selector typo (`app: wbe`); a readiness probe on the wrong port; HAProxy `timeout server 1s` with a backend that sleeps 2 s; or `tc netem loss 10%` on a kind node (`docker exec <node> tc qdisc add dev eth0 root netem loss 10%`).

**Step 3.** Diagnose using Section 9.2's methodology. Record each command and what it ruled in or out.

**Step 4.** Write a short postmortem: impact, timeline, root cause, evidence, fix, and one prevention action (an alert, a test, or a policy).

Expected signals: empty EndpointSlices for the selector typo and readiness failures; `SERVFAIL` or timeouts for DNS faults; HAProxy `sH--` termination state (server timeout) with 504s for the timeout fault; mtr loss at the final hop for netem.

## 10.8 Interview Questions and Answers

### 10.8.1 Beginner

**1. What is the difference between a switch and a router?**
A switch forwards frames within a LAN/VLAN using MAC addresses (L2). A router forwards packets between networks using IP addresses and a route table (L3), rewriting the L2 header and decrementing TTL at each hop.

**2. What is the difference between TCP and UDP?**
TCP is connection-oriented, reliable, ordered, with flow and congestion control (HTTP/1.1, HTTP/2, SSH, databases). UDP is connectionless best-effort datagrams with an 8-byte header (DNS, DHCP, VoIP, QUIC). Choose UDP when latency matters more than reliability or when the application implements its own reliability.

**3. Describe the TCP three-way handshake.**
Client sends SYN with its initial sequence number; server replies SYN-ACK with its own ISN and acknowledges the client's; client sends ACK. Both sides confirm sequence numbers and negotiate options (MSS, window scale, SACK). It costs one RTT.

**4. What is a subnet mask and what does /24 mean?**
The mask marks which bits of an address are the network portion. /24 means 24 network bits (255.255.255.0), leaving 8 host bits: 256 addresses, 254 usable.

**5. What are the RFC 1918 private ranges?**
10.0.0.0/8, 172.16.0.0/12 (172.16.0.0–172.31.255.255), and 192.168.0.0/16.

**6. What does ARP do?**
It maps an IPv4 address to a MAC address on the local link by broadcasting a request and receiving a unicast reply. Hosts ARP for the destination if it is on-link, otherwise for their default gateway.

**7. What is DNS and which port does it use?**
The system that resolves names to records (such as IP addresses). UDP 53 primarily, TCP 53 for large responses and zone transfers; DoT uses 853 and DoH uses 443.

**8. What is the difference between an A record and a CNAME?**
An A record maps a name directly to an IPv4 address. A CNAME aliases one name to another name, which must then be resolved. CNAMEs cannot exist at the zone apex or alongside other records at the same name.

**9. What is a default gateway?**
The next-hop router for destinations not matched by a more specific route (0.0.0.0/0). It must be on a directly connected subnet.

**10. What is NAT and why is it used?**
Network Address Translation rewrites IP addresses (and ports, for PAT) as packets pass through a device. It lets many private hosts share public IPs and can connect overlapping networks.

**11. What are well-known ports for HTTP, HTTPS, SSH, and DNS?**
80, 443, 22, and 53.

**12. What does ping test, and why might it fail even if a server is up?**
ICMP echo reachability and RTT at L3. Firewalls and security groups often block ICMP, so failure does not prove the host is down; test the real TCP port.

**13. What is DHCP and what is DORA?**
DHCP automatically assigns IP configuration. DORA: Discover (client broadcast), Offer (server), Request (client), Acknowledge (server). UDP 67/68.

**14. What is the difference between HTTP and HTTPS?**
HTTPS is HTTP inside TLS, providing encryption, integrity, and server authentication via certificates. Default port 443 instead of 80.

**15. What is a VLAN?**
A logical L2 broadcast domain on shared switches, identified by a 12-bit 802.1Q tag. Traffic between VLANs must be routed.

### 10.8.2 Intermediate

**16. Walk through what happens when you type `https://example.com` in a browser.**
Browser and OS caches, then a stub query to the recursive resolver, which iterates root, TLD, and authoritative servers. The OS routes to the IP (ARP for the gateway), TCP handshake, TLS handshake (SNI, certificate validation, key exchange, ALPN), HTTP request with Host header, possibly through CDN and load balancer to a backend, response, then rendering with additional requests over the same connection(s).

**17. How many usable hosts are in a /27, and what is the network and broadcast of 10.1.1.100/27?**
30 usable. Block size 32: 100 falls in 96–127, so network 10.1.1.96, broadcast 10.1.1.127, hosts .97–.126.

**18. Security group vs NACL?**
Security groups are stateful, allow-only, attached to ENIs, evaluate all rules, and can reference other security groups. NACLs are stateless, subnet-level, ordered allow/deny rules, and require explicit return rules for ephemeral ports.

**19. What is the difference between L4 and L7 load balancing?**
L4 balances connections using IP/port (any TCP/UDP protocol, very fast, TLS passthrough possible). L7 understands HTTP and routes each request by host, path, or headers, terminates TLS, and supports retries, rewrites, and auth.

**20. What is TIME_WAIT and when is it a problem?**
The state on the side that closes first, lasting 2 x MSL (60 s on Linux), to absorb delayed packets and retransmitted FINs. It becomes a problem when a client opens many short connections to one destination and runs out of ephemeral ports. Fix with connection reuse; `tcp_tw_reuse` helps outbound.

**21. What does a large number of CLOSE_WAIT sockets indicate?**
The peer closed the connection but the local application never called `close()`: a socket leak in the application.

**22. Connection refused vs connection timed out?**
Refused: an RST came back; the host is reachable but nothing listens on the port (or a firewall actively rejects). Timed out: no reply; packets are being dropped (security group, firewall, routing, host down).

**23. Explain a public vs private subnet in AWS.**
A public subnet's route table has a 0.0.0.0/0 route to an Internet Gateway; instances with public IPs are reachable. A private subnet does not; outbound IPv4 goes via a NAT gateway, and inbound from the Internet is impossible without a load balancer.

**24. Why is VPC peering not transitive, and what do you use instead?**
Peering is a point-to-point relationship and AWS does not route through a VPC to another peer (no edge-to-edge routing). For many VPCs use Transit Gateway (or Cloud WAN), which acts as a routed hub with segmented route tables.

**25. What is the TLS chain of trust and the most common chain problem?**
The leaf certificate is signed by an intermediate, which is signed by a root in the client's trust store. Servers must send the leaf plus intermediates. The most common problem is a missing intermediate, which breaks non-browser clients.

**26. What is SNI and why does it matter?**
Server Name Indication: the client sends the hostname in the ClientHello so a server hosting many sites can present the right certificate. Testing without SNI (`openssl s_client` without `-servername`) often returns the wrong default certificate.

**27. What are the Kubernetes Service types?**
ClusterIP (internal virtual IP), NodePort (port 30000–32767 on every node), LoadBalancer (external LB via cloud or MetalLB), ExternalName (DNS CNAME), plus headless (`clusterIP: None`) returning pod IPs directly.

**28. How does a DNS TTL affect a migration?**
Resolvers cache answers for the TTL, so changes take up to the old TTL to propagate. Lower the TTL well ahead of the change (at least one old-TTL period), migrate, then raise it. Remember negative caching and clients that ignore TTLs or hold connections.

**29. What does HTTP/2 improve over HTTP/1.1, and what does HTTP/3 add?**
HTTP/2 adds binary framing, multiplexed streams on one connection, and header compression, removing application-level head-of-line blocking. HTTP/3 runs over QUIC on UDP, eliminating TCP head-of-line blocking, integrating TLS 1.3 for 1-RTT/0-RTT setup, and supporting connection migration.

**30. What is MTU and how do you detect a PMTUD black hole?**
The largest packet a link carries (1500 for Ethernet). A black hole occurs when ICMP "fragmentation needed" is blocked, so large packets are silently dropped. Symptoms: handshakes and small requests work, large transfers hang. Test with `ping -M do -s <size>` and `tracepath`.

### 10.8.3 Advanced

**31. How does kube-proxy implement a ClusterIP in iptables mode, and what are its limitations?**
It programs nat-table rules on every node: a KUBE-SERVICES match on VIP:port jumps to a per-Service chain that randomly selects a per-endpoint chain which DNATs to a pod IP:port; conntrack handles replies. Limitations: linear rule evaluation and slow updates at very large scale, per-connection (not per-request) balancing, and no ICMP to the VIP. IPVS, nftables, or eBPF (Cilium) address scale.

**32. Why does gRPC load-balance poorly behind a Kubernetes Service, and how do you fix it?**
gRPC uses long-lived HTTP/2 connections that multiplex all requests; Services balance per connection, so each client pins to one pod. Fix with client-side balancing over a headless Service (gRPC `round_robin` with DNS or xDS), an L7 proxy or service mesh that balances per request, or periodic connection recycling (max connection age on the server).

**33. Explain BGP path selection basics and where you would use BGP as a DevOps engineer.**
Routes are chosen by policy: highest LOCAL_PREF, shortest AS_PATH, lowest origin, lowest MED, eBGP over iBGP, lowest IGP metric to next hop, then tie-breakers. DevOps uses: Direct Connect and VPN dynamic routing (prefer one path with AS-path prepending or local preference), Calico peering with ToR switches, MetalLB/Cilium advertising LoadBalancer IPs, and anycast services.

**34. A service has p99 latency spikes of about 200 ms and exactly 1 s. What do you suspect?**
TCP retransmissions: ~200 ms is the Linux minimum RTO for retransmitted data, 1 s is the initial SYN retransmission timeout (lost SYN or SYN-ACK, often from SYN backlog overflow or packet loss). Check `nstat` for retransmits and ListenOverflows, `ss -ti`, mtr for loss, and accept queue saturation on the server.

**35. How would you design multi-region active-active ingress?**
Global entry via anycast (Global Accelerator, cloud global LB, or CDN) or latency-based DNS with health checks; regional L7 load balancers; stateless services; data replication strategy (multi-region database or region-pinned users); health checks that represent regional health; failover tested regularly; and awareness that DNS failover is bounded by TTL and client caching.

**36. What is the bandwidth-delay product and why does it matter for cross-region transfers?**
BDP = bandwidth x RTT, the amount of data in flight needed to fill a path. A single TCP connection's throughput is limited to window/RTT; with 100 ms RTT, a 1 Gbit/s path needs 12.5 MB of window. Small buffers or a lossy path (CUBIC backs off) cap throughput; fixes include buffer tuning, BBR, and parallel streams.

**37. How do you preserve client source IPs through an AWS NLB into Kubernetes?**
Use NLB with client IP preservation (instance or IP targets), `externalTrafficPolicy: Local` to avoid SNAT between nodes (or IP-mode targets that hit pods directly), or PROXY protocol v2 if a proxy is in the path, with the backend configured to parse it. Ensure security groups allow the real client CIDRs.

**38. Explain how PrivateLink differs from VPC peering.**
PrivateLink exposes a single service (behind an NLB/GWLB) via interface endpoint ENIs in consumer VPCs: unidirectional, consumer-initiated, works with overlapping CIDRs, and scales to many consumers. Peering connects entire networks bidirectionally, requires non-overlapping CIDRs and routes on both sides, and is non-transitive.

**39. What is conntrack and how can it cause outages?**
The netfilter connection tracking table used for stateful filtering and NAT (Docker, kube-proxy, NAT gateways). When it fills (`nf_conntrack_max`), new connections are dropped (`table full, dropping packet`). UDP-heavy workloads (DNS) and short-lived connections are common culprits; fixes include raising limits, reducing timeouts, NodeLocal DNSCache, or eBPF data planes that bypass conntrack for some flows.

**40. What does zero trust mean in a Kubernetes environment?**
No implicit trust based on network location: workload identity (ServiceAccount-based SPIFFE IDs), mutual TLS for all service-to-service traffic, identity-based authorization policies (mesh AuthorizationPolicy), default-deny NetworkPolicies, egress controls, short-lived credentials, and comprehensive auditing.

**41. How do you debug a pod that can resolve DNS but intermittently takes 5 s to do so?**
Classic causes: a conntrack race when A and AAAA queries are sent in parallel over the same UDP socket, one CoreDNS replica unhealthy, or upstream resolver throttling (AWS limit 1,024 packets/s per ENI). Check CoreDNS metrics and logs, capture UDP 53 on the node, try `single-request-reopen`, deploy NodeLocal DNSCache, lower ndots, and scale CoreDNS.

**42. What happens during a TLS 1.3 handshake, and what is forward secrecy?**
ClientHello with key shares, SNI, and ALPN; ServerHello with its key share; then encrypted extensions, certificate, CertificateVerify, and Finished; client Finished. Keys come from ephemeral ECDHE, so compromising the server's long-term private key later cannot decrypt past sessions: that is forward secrecy.

**43. How do you prevent connection errors during rolling deployments behind a load balancer?**
Readiness probes so new pods receive traffic only when ready; `preStop` delay so endpoints are removed from kube-proxy and the LB before shutdown; graceful SIGTERM handling that drains in-flight requests; LB deregistration delay aligned with request durations; `maxUnavailable: 0` and surge; keep-alive timeouts ordered correctly.

**44. Explain the AND vs OR semantics in NetworkPolicy selectors.**
In a `from`/`to` entry, a `namespaceSelector` and `podSelector` in the same list element are ANDed (matching pods in matching namespaces). As separate list elements they are ORed. A misplaced dash can open access to all pods in a namespace or to every namespace.

**45. You see 38% loss at hop 3 in mtr but 0% at the destination. Is there a problem?**
No. Intermediate routers deprioritize or rate-limit ICMP generation for probes addressed to them. Only loss that continues to the final hop affects traffic.

**46. How would you reduce NAT gateway costs in a large AWS environment?**
Gateway endpoints for S3 and DynamoDB, interface endpoints for heavily used services (ECR, STS, CloudWatch), pull-through caches for container images, keeping traffic within an AZ (per-AZ NAT), IPv6 with egress-only Internet gateways where possible, and centralized egress via Transit Gateway only if the processing charges net out.

## 10.9 Cheat Sheet

### 10.9.1 Common ports

| Port | Proto | Service | Port | Proto | Service |
|------|-------|---------|------|-------|---------|
| 20/21 | TCP | FTP data/control | 1194 | UDP | OpenVPN |
| 22 | TCP | SSH, SFTP, Git over SSH | 1433 | TCP | Microsoft SQL Server |
| 23 | TCP | Telnet (insecure) | 1521 | TCP | Oracle DB |
| 25 | TCP | SMTP | 2049 | TCP/UDP | NFS |
| 53 | UDP/TCP | DNS | 2181 | TCP | ZooKeeper |
| 67/68 | UDP | DHCP server/client | 2379/2380 | TCP | etcd client/peer |
| 80 | TCP | HTTP | 3000 | TCP | Grafana (common default) |
| 110 | TCP | POP3 | 3306 | TCP | MySQL / MariaDB |
| 123 | UDP | NTP | 3389 | TCP | RDP |
| 143 | TCP | IMAP | 4789 | UDP | VXLAN |
| 161/162 | UDP | SNMP / traps | 5000 | TCP | Docker registry (common) |
| 179 | TCP | BGP | 5201 | TCP | iperf3 |
| 389 / 636 | TCP | LDAP / LDAPS | 5432 | TCP | PostgreSQL |
| 443 | TCP | HTTPS | 5672 | TCP | AMQP (RabbitMQ) |
| 443 | UDP | HTTP/3 (QUIC) | 6379 | TCP | Redis |
| 465 / 587 | TCP | SMTP over TLS / submission | 6443 | TCP | Kubernetes API server |
| 500 / 4500 | UDP | IPsec IKE / NAT-T | 8080 | TCP | Alternate HTTP |
| 514 | UDP | Syslog | 8443 | TCP | Alternate HTTPS |
| 853 | TCP | DNS over TLS | 9090 | TCP | Prometheus |
| 993 / 995 | TCP | IMAPS / POP3S | 9092 | TCP | Kafka |
| 10250 | TCP | kubelet API | 9100 | TCP | node_exporter |
| 30000–32767 | TCP/UDP | Kubernetes NodePort range | 9200/9300 | TCP | Elasticsearch HTTP/transport |
| 51820 | UDP | WireGuard | 27017 | TCP | MongoDB |
| 15000–15090 | TCP | Istio sidecar ports (15001 outbound, 15006 inbound, 15090 metrics) | 8500 | TCP | Consul HTTP |

### 10.9.2 Subnet quick reference

| Prefix | Mask | Addresses | Usable | AWS usable |
|--------|------|-----------|--------|-----------|
| /16 | 255.255.0.0 | 65,536 | 65,534 | 65,531 |
| /20 | 255.255.240.0 | 4,096 | 4,094 | 4,091 |
| /22 | 255.255.252.0 | 1,024 | 1,022 | 1,019 |
| /24 | 255.255.255.0 | 256 | 254 | 251 |
| /26 | 255.255.255.192 | 64 | 62 | 59 |
| /27 | 255.255.255.224 | 32 | 30 | 27 |
| /28 | 255.255.255.240 | 16 | 14 | 11 |
| /30 | 255.255.255.252 | 4 | 2 | n/a (min /28) |

Block size = 256 - mask octet. Network = largest multiple of block size not above the address. Broadcast = next network - 1.

### 10.9.3 Commands by task

```bash
# Interfaces, addresses, routes, neighbors
ip -br addr                    ; ip -br link
ip route                       ; ip route get 10.0.64.10
ip -6 route                    ; ip neigh
ip -s link show eth0           ; ethtool -S eth0 | grep -iE 'err|drop'
ip netns list                  ; ip netns exec <ns> <cmd>
nsenter -t <pid> -n ip addr    # enter a container's network namespace by PID

# DNS
dig +short name                ; dig @server name TYPE
dig +trace name                ; dig -x 203.0.113.10
getent ahosts name             ; resolvectl status ; resolvectl flush-caches

# Reachability and path
ping -c 4 host                 ; ping -M do -s 1472 host
mtr -n -r -c 100 -T -P 443 host
traceroute -n -T -p 443 host   ; tracepath -n host
nc -zv -w 3 host port          ; nc -u -zv host 53

# Sockets and TCP health
ss -tlnp                       ; ss -tunap
ss -tan state time-wait | wc -l
ss -ti dst 10.0.2.40           ; ss -s
nstat -az | grep -E 'Retrans|ListenOverflows|ListenDrops'
conntrack -L | head            ; conntrack -C
cat /proc/sys/net/netfilter/nf_conntrack_max

# HTTP and TLS
curl -v https://host/
curl -o /dev/null -s -w 'dns=%{time_namelookup} tcp=%{time_connect} tls=%{time_appconnect} ttfb=%{time_starttransfer} total=%{time_total}\n' https://host/
curl --resolve host:443:10.0.0.5 https://host/
openssl s_client -connect host:443 -servername host -showcerts </dev/null
openssl x509 -in cert.pem -noout -subject -issuer -dates -ext subjectAltName

# Capture
tcpdump -ni any host 10.0.0.5 and port 443
tcpdump -ni eth0 'tcp[tcpflags] & (tcp-syn|tcp-rst) != 0'
tcpdump -ni eth0 -w cap.pcap -s 0 -c 10000 port 5432

# Firewall
iptables -L -n -v --line-numbers ; iptables -t nat -S
nft list ruleset

# Throughput and impairment
iperf3 -s                      ; iperf3 -c host -P 4 -t 10
tc qdisc add dev eth0 root netem delay 100ms loss 1% ; tc qdisc del dev eth0 root

# Kubernetes
kubectl get svc,endpointslices -n ns
kubectl get networkpolicy -A
kubectl debug -it pod/<p> --image=nicolaka/netshoot --target=<container>
kubectl -n kube-system logs -l k8s-app=kube-dns --tail=100
kubectl get pods -o wide       # pod IPs and nodes
```

### 10.9.4 Useful sysctls

| Sysctl | Default | Purpose |
|--------|---------|---------|
| `net.ipv4.ip_forward` | 0 | Route between interfaces (1 on routers, Docker and Kubernetes nodes) |
| `net.ipv4.ip_local_port_range` | 32768 60999 | Ephemeral source ports |
| `net.ipv4.tcp_tw_reuse` | 2 (loopback only) | Reuse TIME_WAIT for outbound connections (set 1) |
| `net.core.somaxconn` | 4096 (kernel 5.4+) | Max accept backlog |
| `net.ipv4.tcp_max_syn_backlog` | varies | SYN queue size |
| `net.ipv4.tcp_syncookies` | 1 | SYN flood protection |
| `net.ipv4.tcp_keepalive_time` | 7200 | Seconds before first keepalive probe |
| `net.ipv4.tcp_congestion_control` | cubic | Set `bbr` for long, lossy paths |
| `net.ipv4.tcp_mtu_probing` | 0 | 1 = probe when black hole detected |
| `net.netfilter.nf_conntrack_max` | memory-based | Conntrack table size |
| `net.ipv4.conf.all.rp_filter` | 0–2 by distro | Reverse path filtering (strict = 1) |

### 10.9.5 Error message decoder

| Message | Layer | First suspects |
|---------|-------|----------------|
| `Name or service not known` / `Could not resolve host` | DNS | resolv.conf, search domains, resolver reachability, split-horizon |
| `Network is unreachable` | L3 (local) | No route on this host |
| `No route to host` | L3 | ICMP unreachable from a router or firewall reject; ARP failure on local subnet |
| `Connection timed out` | L3/L4 | Firewall, SG, NACL, NetworkPolicy drop; host down; return route |
| `Connection refused` | L4 | Nothing listening; wrong port; bound to 127.0.0.1 |
| `Connection reset by peer` | L4 | App crash, idle timeout on a middlebox, keep-alive race, proxy protocol mismatch |
| `certificate verify failed` | TLS | Expired, missing intermediate, untrusted CA, hostname not in SANs |
| `wrong version number` | TLS | TLS spoken to a non-TLS port |
| `502 Bad Gateway` | Proxy | Upstream refused/reset/invalid response; keep-alive mismatch |
| `503 Service Unavailable` | Proxy | No healthy targets; overload |
| `504 Gateway Timeout` | Proxy | Upstream too slow; timeout too short |

## Key takeaways

- Break every problem into phases and hops; measure before you guess.
- "Works from X, not from Y" is the most powerful localization technique: node vs pod, host A vs host B, inside vs outside.
- Read the exact error; it usually names the layer.
- Many intermittent failures are timeout mismatches, finite state (NAT ports, conntrack), or DNS latency.
- Cloud "Active" or "Available" status proves configuration, not traffic flow; verify routes both ways and use flow logs and reachability tools.
- Interviewers reward layered reasoning: explain why, then name the command that proves it.
- Keep the cheat sheet handy, but understand every line of it: that is what makes you an advanced DevOps engineer.
